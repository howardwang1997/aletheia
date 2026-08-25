#!/usr/bin/env python3
"""Validate, persist, or inspect an F8 corpus-ingestion bundle.

The CLI accepts normalized, license-explicit JSON only. It performs no network retrieval and the
contract contains no raw literature-text field.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aletheia.db import require_schema_current
from aletheia.knowledge.ingestion import CorpusIngestionBundle
from aletheia.knowledge.persistence import get_ingestion_bundle, store_ingestion_bundle

MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def _read_bundle(path: Path) -> CorpusIngestionBundle:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"knowledge bundle does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("knowledge bundle input must be a regular non-symlink file")
    if metadata.st_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"knowledge bundle exceeds {MAX_BUNDLE_BYTES} bytes")
    try:
        return CorpusIngestionBundle.model_validate_json(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid knowledge ingestion bundle: {path}") from exc


def _summary(
    bundle: CorpusIngestionBundle, *, action: str, created: bool | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": action,
        "bundle_id": bundle.bundle_id,
        "bundle_sha256": bundle.bundle_sha256,
        "corpus_sha256": bundle.corpus.snapshot_sha256,
        "policy_sha256": bundle.access_policy.policy_sha256,
        "source_count": len(bundle.corpus.sources),
        "paper_count": len(bundle.corpus.papers),
        "span_count": len(bundle.corpus.spans),
        "update_count": len(bundle.corpus.updates),
        "grant_count": len(bundle.access_grants),
        "provider_receipt_count": len(bundle.provider_receipts),
        "raw_literature_text_persisted": False,
    }
    if created is not None:
        result["created"] = created
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate", help="validate and hash a bundle without database IO"
    )
    validate.add_argument("bundle", type=Path)

    persist = commands.add_parser("persist", help="persist a bundle at the current Alembic head")
    persist.add_argument("bundle", type=Path)

    inspect_command = commands.add_parser(
        "inspect", help="load and revalidate one persisted bundle"
    )
    inspect_command.add_argument("bundle_sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        output = _summary(_read_bundle(args.bundle), action="validated")
    elif args.command == "persist":
        bundle = _read_bundle(args.bundle)
        require_schema_current()
        result = store_ingestion_bundle(bundle)
        output = _summary(bundle, action="persisted", created=result.created)
    else:
        if len(args.bundle_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in args.bundle_sha256
        ):
            raise ValueError("bundle_sha256 must be 64 lowercase hexadecimal characters")
        require_schema_current()
        output = _summary(get_ingestion_bundle(args.bundle_sha256), action="inspected")
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
