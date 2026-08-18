#!/usr/bin/env python3
"""Operate the restart-safe, supervised run-once research endurance controller.

There is intentionally no finalize subcommand.  Production finalization is a separate explicit
scientific review after the database-clock duration and all evidence obligations are satisfied.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from aletheia.db import REPO_ROOT
from aletheia.programs.endurance_controller import (
    EnduranceControllerAction,
    EnduranceControllerConflict,
    EnduranceControllerManifest,
    controller_status,
    preflight_endurance_controller,
    prepare_controller_spool,
    prepare_endurance_controller_manifest,
    run_controller_tick,
    start_endurance_controller_gate,
    submit_controller_evidence,
    verify_endurance_controller_code_identity,
)
from aletheia.programs.endurance_schemas import (
    EnduranceCheckpointEvidence,
    EnduranceGateManifest,
)


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _manifest(path: str) -> EnduranceControllerManifest:
    return EnduranceControllerManifest.model_validate(_read(path))


def _render(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _print(value: Any) -> None:
    print(_render(value).decode("utf-8"), end="")


def _write_new(target: Path, payload: bytes) -> None:
    """Durably create a controller manifest without replacing prior identity."""

    destination = target.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("controller manifest write made no progress")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _artifact_root(args: argparse.Namespace) -> Path:
    return Path(args.artifact_root).resolve()


def _prepare(args: argparse.Namespace) -> int:
    gate = EnduranceGateManifest.model_validate(_read(args.gate_manifest))
    output = Path(args.output)
    if output.exists():
        manifest = EnduranceControllerManifest.model_validate(_read(args.output))
        expected = {
            "gate_manifest": gate,
            "controller_key": args.controller_key,
            "principal": args.principal,
            "spool_root": args.spool_root,
            "supervisor_poll_seconds": args.poll_seconds,
        }
        actual = {key: getattr(manifest, key) for key in expected}
        if actual != expected:
            raise EnduranceControllerConflict(
                "existing controller manifest differs from requested preparation"
            )
        verify_endurance_controller_code_identity(manifest.code_identity)
        created = False
    else:
        manifest = prepare_endurance_controller_manifest(
            gate,
            controller_key=args.controller_key,
            principal=args.principal,
            spool_root=args.spool_root,
            supervisor_poll_seconds=args.poll_seconds,
        )
        _write_new(output, _render(manifest))
        created = True
    spool = prepare_controller_spool(manifest, artifact_root=_artifact_root(args))
    _print(
        {
            "controller_id": manifest.controller_id,
            "gate_id": manifest.gate_manifest.gate_id,
            "manifest_sha256": manifest.manifest_sha256,
            "code_sha256": manifest.code_identity.aggregate_sha256,
            "manifest_path": str(output.resolve()),
            "spool_path": str(spool),
            "created": created,
            "automatic_finalization": False,
        }
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    report = preflight_endurance_controller(
        _manifest(args.manifest),
        artifact_root=_artifact_root(args),
    )
    _print(report)
    return 0 if report.eligible_to_start else 2


def _start(args: argparse.Namespace) -> int:
    tick = start_endurance_controller_gate(
        _manifest(args.manifest),
        artifact_root=_artifact_root(args),
    )
    _print(tick)
    return 0 if tick.action is not EnduranceControllerAction.LOCK_BUSY else 75


def _tick(args: argparse.Namespace) -> int:
    tick = run_controller_tick(
        _manifest(args.manifest),
        artifact_root=_artifact_root(args),
    )
    _print(tick)
    # Lock contention is normal under overlapping supervisor invocations; the durable owner runs.
    return 0


def _submit(args: argparse.Namespace) -> int:
    envelope, created = submit_controller_evidence(
        _manifest(args.manifest),
        EnduranceCheckpointEvidence.model_validate(_read(args.evidence)),
        producer=args.producer,
        artifact_root=_artifact_root(args),
    )
    _print(
        {
            "envelope": envelope.model_dump(mode="json"),
            "created": created,
        }
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    _print(
        controller_status(
            _manifest(args.manifest),
            artifact_root=_artifact_root(args),
        )
    )
    return 0


def _artifact_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifact-root",
        default=str(REPO_ROOT),
        help="root used to resolve the manifest's safe relative spool path",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="freeze controller code and gate identity")
    prepare.add_argument("gate_manifest")
    prepare.add_argument("--controller-key", required=True)
    prepare.add_argument("--principal", required=True)
    prepare.add_argument("--spool-root", required=True)
    prepare.add_argument("--poll-seconds", type=int, default=300)
    prepare.add_argument("--output", required=True)
    _artifact_argument(prepare)
    prepare.set_defaults(handler=_prepare)

    preflight = commands.add_parser(
        "preflight",
        help="verify code, frozen scientific sources, empty spool, and start eligibility",
    )
    preflight.add_argument("manifest")
    _artifact_argument(preflight)
    preflight.set_defaults(handler=_preflight)

    start = commands.add_parser(
        "start",
        help="explicitly start (or safely replay) the database-clock gate",
    )
    start.add_argument("manifest")
    _artifact_argument(start)
    start.set_defaults(handler=_start)

    tick = commands.add_parser(
        "tick",
        help="run one locked recovery/checkpoint decision; suitable for an external supervisor",
    )
    tick.add_argument("manifest")
    _artifact_argument(tick)
    tick.set_defaults(handler=_tick)

    submit = commands.add_parser(
        "submit",
        help="durably spool typed evidence for the next checkpoint",
    )
    submit.add_argument("manifest")
    submit.add_argument("evidence")
    submit.add_argument("--producer", required=True)
    _artifact_argument(submit)
    submit.set_defaults(handler=_submit)

    status = commands.add_parser("status", help="inspect the database-clock gate schedule")
    status.add_argument("manifest")
    _artifact_argument(status)
    status.set_defaults(handler=_status)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
