#!/usr/bin/env python3
"""Inspect F11 scientific receipts and reconcile expired one-time external actions."""

from __future__ import annotations

import argparse

from aletheia.db import require_schema_current
from aletheia.jobs import OneTimeExternalActionStore, ScientificTransitionStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="resource", required=True)

    command = commands.add_parser("command", help="inspect a committed scientific command")
    command.add_argument("command_id")

    action = commands.add_parser("action", help="inspect one external action intent")
    action.add_argument("action_id")

    recover = commands.add_parser(
        "recover-actions",
        help="mark expired claimed actions for reconciliation without reissuing them",
    )
    recover.add_argument("--limit", type=int, default=100)
    recover.add_argument("--principal", default="cli:external-action-recovery")
    return parser


def main() -> int:
    args = _parser().parse_args()
    require_schema_current()
    if args.resource == "command":
        print(ScientificTransitionStore().get(args.command_id).model_dump_json(indent=2))
    elif args.resource == "action":
        print(OneTimeExternalActionStore().get(args.action_id).model_dump_json(indent=2))
    else:
        receipt = OneTimeExternalActionStore().recover_stale(
            principal=args.principal,
            limit=args.limit,
        )
        print(receipt.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
