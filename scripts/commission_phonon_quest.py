#!/usr/bin/env python3
"""Prepare, apply, and audit the real structure/phonon research Quest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aletheia.domains.materials.phonon_commissioning import (
    PhononQuestCommissioningManifest,
    apply_phonon_quest_commissioning,
    audit_phonon_quest_commissioning,
    prepare_phonon_quest_commissioning,
    verify_commissioning_artifacts,
)

_DEFAULT_WORKSPACE = Path("workspaces/evaluator/materials-structure-phonons-v1")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def _read_manifest(path: Path) -> PhononQuestCommissioningManifest:
    return PhononQuestCommissioningManifest.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _print(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _write_new_json(path: Path, value: Any) -> None:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (value.model_dump_json(indent=2) + "\n").encode("utf-8")
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
                raise OSError("commissioning manifest write made no progress")
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


def _prepare(args: argparse.Namespace) -> None:
    manifest = prepare_phonon_quest_commissioning(
        args.workspace,
        prepared_at=args.prepared_at or datetime.now(timezone.utc),
        command_principal=args.principal,
        identity_namespace=args.identity_namespace,
    )
    _write_new_json(args.output, manifest)
    _print(
        {
            "commissioning_id": manifest.commissioning_id,
            "manifest_sha256": manifest.manifest_sha256,
            "output": str(args.output),
            "quest_id": manifest.quest.node_id,
            "program_id": manifest.program.node_id,
            "campaign_ids": [item.node_id for item in manifest.campaigns],
            "question_sha256s": [
                item.question.question_sha256 for item in manifest.world_models
            ],
            "source_evidence_sha256": manifest.evidence.evidence_sha256,
            "external_candidates_allocated": False,
            "durable_blockers": manifest.durable_blockers,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    manifest = _read_manifest(args.manifest)
    verify_commissioning_artifacts(manifest)
    _print(
        {
            "commissioning_id": manifest.commissioning_id,
            "manifest_sha256": manifest.manifest_sha256,
            "code_sha256": manifest.code_identity.aggregate_sha256,
            "evidence_sha256": manifest.evidence.evidence_sha256,
            "local_artifacts_rehashed": True,
            "database_mutated": False,
        }
    )


def _apply(args: argparse.Namespace) -> None:
    receipt = apply_phonon_quest_commissioning(_read_manifest(args.manifest))
    if args.output is not None:
        _write_new_json(args.output, receipt)
    _print(receipt)


def _audit(args: argparse.Namespace) -> None:
    receipt = audit_phonon_quest_commissioning(_read_manifest(args.manifest))
    if args.output is not None:
        _write_new_json(args.output, receipt)
    _print(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare",
        help="freeze real F10 evidence, world models, graph, data roles, and budgets",
    )
    prepare.add_argument("--workspace", type=Path, default=_DEFAULT_WORKSPACE)
    prepare.add_argument("--principal", required=True)
    prepare.add_argument(
        "--identity-namespace",
        default="phonon-structure-information-v1",
    )
    prepare.add_argument("--prepared-at", type=_timestamp)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    verify = commands.add_parser(
        "verify",
        help="rehash frozen code and evidence without changing the database",
    )
    verify.add_argument("manifest", type=Path)
    verify.set_defaults(handler=_verify)

    apply = commands.add_parser(
        "apply",
        help="idempotently commission and activate the bounded initial campaign",
    )
    apply.add_argument("manifest", type=Path)
    apply.add_argument("--output", type=Path, help="optional write-once replay receipt")
    apply.set_defaults(handler=_apply)

    audit = commands.add_parser(
        "audit",
        help="read-only verification of the exact initial Quest commissioning",
    )
    audit.add_argument("manifest", type=Path)
    audit.add_argument("--output", type=Path, help="optional write-once audit receipt")
    audit.set_defaults(handler=_audit)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
