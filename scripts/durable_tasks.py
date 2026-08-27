#!/usr/bin/env python3
"""Operator CLI for the F11 Postgres-backed durable task control plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aletheia.schema_migrations import require_schema_exact
from aletheia.jobs import DurableTaskQueue, TaskSpec, TaskStatus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--principal", default="cli:durable_tasks")
    commands = parser.add_subparsers(dest="command", required=True)

    enqueue = commands.add_parser("enqueue", help="enqueue a frozen TaskSpec JSON file")
    enqueue.add_argument("--spec", type=Path, required=True)

    get = commands.add_parser("get", help="read one durable task")
    get.add_argument("task_id")

    attempts = commands.add_parser("attempts", help="read all attempts for one task")
    attempts.add_argument("task_id")

    listing = commands.add_parser("list", help="list durable tasks")
    listing.add_argument("--run-id")
    listing.add_argument("--status", choices=[item.value for item in TaskStatus])
    listing.add_argument("--limit", type=int, default=500)

    recover = commands.add_parser("recover", help="reclaim expired leases")
    recover.add_argument("--limit", type=int, default=1_000)
    return parser


def _print_models(values) -> None:
    print(
        json.dumps(
            [value.model_dump(mode="json") for value in values],
            indent=2,
            sort_keys=True,
        )
    )


def main() -> int:
    args = _parser().parse_args()
    require_schema_exact()
    queue = DurableTaskQueue(principal=args.principal)
    if args.command == "enqueue":
        spec = TaskSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        print(queue.enqueue(spec).model_dump_json(indent=2))
    elif args.command == "get":
        print(queue.get(args.task_id).model_dump_json(indent=2))
    elif args.command == "attempts":
        _print_models(queue.attempts(args.task_id))
    elif args.command == "list":
        status = None if args.status is None else TaskStatus(args.status)
        _print_models(queue.list(run_id=args.run_id, status=status, limit=args.limit))
    elif args.command == "recover":
        print(queue.recover_expired(limit=args.limit).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
