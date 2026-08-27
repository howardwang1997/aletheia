#!/usr/bin/env python3
"""Read/verify the durable scientific program graph from its authoritative ledger."""

from __future__ import annotations

import argparse
import json
from typing import Any

from aletheia.schema_migrations import require_schema_exact
from aletheia.programs import ProgramGraphError, ProgramGraphStore


def _json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("list", help="rebuild and summarize every Quest")
    show = subcommands.add_parser("show", help="rebuild one complete Quest snapshot")
    show.add_argument("quest_id")
    verify = subcommands.add_parser("verify", help="fail closed or print the rebuilt graph hash")
    verify.add_argument("quest_id")
    return parser


def main() -> int:
    args = _parser().parse_args()
    require_schema_exact()
    store = ProgramGraphStore()
    try:
        if args.command == "list":
            snapshots = store.list_quests()
            _json(
                [
                    {
                        "quest_id": item.quest_id,
                        "graph_sha256": item.graph_sha256,
                        "nodes": len(item.nodes),
                        "programs": sum(node.node_type.value == "program" for node in item.nodes),
                        "campaigns": sum(
                            node.node_type.value == "campaign" for node in item.nodes
                        ),
                        "scientific_families": len(item.scientific_families),
                    }
                    for item in snapshots
                ]
            )
            return 0
        snapshot = store.get_quest(args.quest_id)
        if args.command == "show":
            _json(snapshot.model_dump(mode="json"))
        else:
            _json(
                {
                    "quest_id": snapshot.quest_id,
                    "graph_sha256": snapshot.graph_sha256,
                    "verified": True,
                }
            )
        return 0
    except ProgramGraphError as exc:
        _json({"verified": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
