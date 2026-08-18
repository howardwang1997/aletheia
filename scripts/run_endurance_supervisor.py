#!/usr/bin/env python3
"""Prepare and operate a frozen launchd supervisor for one endurance controller.

This interface intentionally has no start or finalize command.  Loading the rendered plist before
the gate starts is safe: each cycle verifies the deployment and reports that it is waiting for the
separate explicit controller start.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from aletheia.db import REPO_ROOT
from aletheia.programs.endurance_controller import EnduranceControllerManifest
from aletheia.programs.endurance_supervisor import (
    EnduranceSupervisorConflict,
    EnduranceSupervisorManifest,
    capture_supervisor_runtime,
    preflight_endurance_supervisor,
    prepare_endurance_supervisor_manifest,
    render_endurance_launchd_plist,
    run_endurance_supervisor_cycle,
    verify_endurance_supervisor_files,
)


def _read(path: Path) -> Any:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def _render_json(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _print(value: Any) -> None:
    print(_render_json(value).decode("utf-8"), end="")


def _write_new(path: Path, payload: bytes, *, mode: int) -> bool:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == payload:
            return False
        raise EnduranceSupervisorConflict(f"refusing to replace deployment file: {destination}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("supervisor deployment write made no progress")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _manifest(path: Path) -> EnduranceSupervisorManifest:
    return EnduranceSupervisorManifest.model_validate(_read(path))


def _prepare(args: argparse.Namespace) -> int:
    root = Path(args.repository_root).resolve(strict=True)
    controller_path = Path(args.controller).resolve(strict=True)
    controller = EnduranceControllerManifest.model_validate(_read(controller_path))
    manifest_path = Path(args.manifest_output).resolve(strict=False)
    plist_path = Path(args.plist_output).resolve(strict=False)
    if not args.conda_executable:
        raise EnduranceSupervisorConflict(
            "Conda executable is not on PATH; pass --conda-executable"
        )
    runtime = capture_supervisor_runtime(
        conda_executable=Path(args.conda_executable),
        conda_environment=args.conda_environment,
    )
    existing = _manifest(manifest_path) if manifest_path.exists() else None
    requested = prepare_endurance_supervisor_manifest(
        controller,
        controller_manifest_path=controller_path,
        supervisor_manifest_path=manifest_path,
        launchd_plist_path=plist_path,
        stdout_log_path=Path(args.stdout_log),
        stderr_log_path=Path(args.stderr_log),
        supervisor_key=args.supervisor_key,
        launchd_label=args.launchd_label,
        launchd_domain=args.launchd_domain,
        runtime=runtime,
        repository_root=root,
        prepared_at=existing.prepared_at if existing is not None else None,
    )
    if existing is not None:
        manifest = existing
        if manifest != requested:
            raise EnduranceSupervisorConflict(
                "existing supervisor manifest differs from requested preparation"
            )
        manifest_created = False
    else:
        manifest = requested
        manifest_created = _write_new(manifest_path, _render_json(manifest), mode=0o600)
    plist_created = _write_new(
        plist_path,
        render_endurance_launchd_plist(manifest),
        mode=0o600,
    )
    for relative in (manifest.stdout_log_path, manifest.stderr_log_path):
        log = root / relative
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    verify_endurance_supervisor_files(manifest)
    _print(
        {
            "supervisor_id": manifest.supervisor_id,
            "controller_id": manifest.controller_id,
            "gate_id": manifest.gate_id,
            "manifest_sha256": manifest.manifest_sha256,
            "manifest_path": str(manifest_path),
            "manifest_created": manifest_created,
            "plist_path": str(plist_path),
            "plist_created": plist_created,
            "launchd_target": f"{manifest.launchd_domain}/{manifest.launchd_label}",
            "automatic_start": False,
            "automatic_finalization": False,
        }
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    report = preflight_endurance_supervisor(_manifest(Path(args.manifest)))
    _print(report)
    return 0 if report.eligible_for_explicit_start else 2


def _cycle(args: argparse.Namespace) -> int:
    cycle = run_endurance_supervisor_cycle(_manifest(Path(args.manifest)))
    _print(cycle)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze and render a launchd deployment")
    prepare.add_argument("controller")
    prepare.add_argument("--supervisor-key", required=True)
    prepare.add_argument("--launchd-label", required=True)
    prepare.add_argument("--launchd-domain", default=f"gui/{os.getuid()}")
    prepare.add_argument("--conda-executable", default=shutil.which("conda"))
    prepare.add_argument("--conda-environment", default="aletheia")
    prepare.add_argument("--manifest-output", required=True)
    prepare.add_argument("--plist-output", required=True)
    prepare.add_argument("--stdout-log", required=True)
    prepare.add_argument("--stderr-log", required=True)
    prepare.add_argument("--repository-root", default=str(REPO_ROOT))
    prepare.set_defaults(handler=_prepare)
    preflight = commands.add_parser(
        "preflight",
        help="require exact deployment bytes, loaded launchd job, and controller start eligibility",
    )
    preflight.add_argument("manifest")
    preflight.set_defaults(handler=_preflight)
    cycle = commands.add_parser(
        "cycle",
        help="wait before explicit start, otherwise invoke one advisory-locked controller tick",
    )
    cycle.add_argument("manifest")
    cycle.set_defaults(handler=_cycle)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
