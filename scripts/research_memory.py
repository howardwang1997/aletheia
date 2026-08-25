#!/usr/bin/env python3
"""Rebuild and verify receipt-backed scientific memory from the authoritative ledger."""

from __future__ import annotations

import argparse
import json
from typing import Any

from aletheia.db import require_schema_current
from aletheia.programs import ResearchMemoryError, ResearchMemoryStore


def _json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("show", "rebuild one scope/task memory snapshot"),
        ("verify", "fail closed or print the reconstructed memory hash"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("scope_node_id")
        command.add_argument("task_key")
    artifact = commands.add_parser("artifact", help="rehash and print one compaction artifact")
    artifact.add_argument("compaction_id")
    context = commands.add_parser("context", help="rehydrate one task-context delivery receipt")
    context.add_argument("context_receipt_id")
    context.add_argument("--prompt-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    require_schema_current()
    store = ResearchMemoryStore()
    try:
        if args.command in {"show", "verify"}:
            snapshot = store.rebuild_memory(args.scope_node_id, args.task_key)
            if args.command == "show":
                _json(snapshot.model_dump(mode="json"))
            else:
                _json(
                    {
                        "scope_node_id": snapshot.scope_node_id,
                        "task_key": snapshot.task_key,
                        "memory_sha256": snapshot.memory_sha256,
                        "fact_count": len(snapshot.facts),
                        "compaction_count": len(snapshot.compactions),
                        "verified": True,
                    }
                )
            return 0
        if args.command == "artifact":
            _json(store.recover_compaction(args.compaction_id).model_dump(mode="json"))
            return 0
        receipt = store.load_task_context(args.context_receipt_id)
        if args.prompt_only:
            print(receipt.context.prompt_text)
        else:
            _json(receipt.model_dump(mode="json"))
        return 0
    except ResearchMemoryError as exc:
        _json({"verified": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
