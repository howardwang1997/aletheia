"""Operator CLI implementation for research-object custody and Quest replay audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import TypeAdapter

from aletheia.research_kernel.policy import ResearchAuthorizationTrustRootV1
from aletheia.research_kernel.schemas import KernelObject, canonical_json_bytes
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore

_OBJECT_ADAPTER = TypeAdapter(KernelObject)


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")


def _archive_object(args: argparse.Namespace) -> int:
    payload = _OBJECT_ADAPTER.validate_json(args.input.read_bytes())
    archive = FilesystemResearchArchive(
        args.cas_root,
        max_object_bytes=args.max_object_bytes,
    )
    metadata = archive.archive_object(payload)
    archive.load_object(metadata.object_ref)
    _emit(metadata)
    return 0


def _trust_root(path: Path) -> ResearchAuthorizationTrustRootV1:
    return ResearchAuthorizationTrustRootV1.model_validate_json(path.read_bytes())


def _audit(args: argparse.Namespace) -> int:
    archive = FilesystemResearchArchive(
        args.cas_root,
        max_object_bytes=args.max_object_bytes,
    )
    result = ResearchKernelStore(
        trust_root=_trust_root(args.trust_root),
        archive=archive,
    ).audit(args.quest_id)
    _emit(result)
    return 0


def _replay(args: argparse.Namespace) -> int:
    archive = FilesystemResearchArchive(
        args.cas_root,
        max_object_bytes=args.max_object_bytes,
    )
    state = ResearchKernelStore(
        trust_root=_trust_root(args.trust_root),
        archive=archive,
    ).replay(args.quest_id)
    _emit(state)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage content-addressed kernel objects or verify a Quest event stream",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    archive = subcommands.add_parser(
        "archive-object",
        help="validate and write one typed kernel object into CAS",
    )
    archive.add_argument("--input", type=Path, required=True)
    archive.set_defaults(handler=_archive_object)

    audit = subcommands.add_parser(
        "audit",
        help="verify events, commands, object custody, snapshots, head, and outbox",
    )
    audit.add_argument("--quest-id", required=True)
    audit.add_argument("--trust-root", type=Path, required=True)
    audit.set_defaults(handler=_audit)

    replay = subcommands.add_parser(
        "replay",
        help="return the canonical graph after the same full fail-closed audit",
    )
    replay.add_argument("--quest-id", required=True)
    replay.add_argument("--trust-root", type=Path, required=True)
    replay.set_defaults(handler=_replay)

    for command in (archive, audit, replay):
        command.add_argument("--cas-root", type=Path, required=True)
        command.add_argument(
            "--max-object-bytes",
            type=int,
            default=64 * 1024 * 1024,
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return int(args.handler(args))


__all__ = ["main"]
