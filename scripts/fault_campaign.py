#!/usr/bin/env python3
"""Evaluate, commit, and audit append-only F11 fault-injection campaigns."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aletheia.jobs import (
    FaultCampaignCommitContext,
    FaultCampaignManifest,
    FaultCampaignReport,
    FaultCampaignStore,
    FaultScenarioObservation,
    evaluate_fault_campaign,
)


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def _print(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, sort_keys=True))


def _evaluate(args: argparse.Namespace) -> None:
    manifest = FaultCampaignManifest.model_validate(_read_json(args.manifest))
    raw = _read_json(args.observations)
    if isinstance(raw, dict):
        raw = raw.get("observations")
    if not isinstance(raw, list):
        raise ValueError("observation input must be an array or an observations wrapper")
    observations = tuple(FaultScenarioObservation.model_validate(item) for item in raw)
    completed_at = args.completed_at or max(item.completed_at for item in observations)
    report = evaluate_fault_campaign(
        manifest,
        observations,
        completed_at=completed_at,
    )
    rendered = report.model_dump_json(indent=2)
    if args.output is not None:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def _commit(args: argparse.Namespace) -> None:
    report = FaultCampaignReport.model_validate(_read_json(args.report))
    receipt = FaultCampaignStore().commit(
        report,
        FaultCampaignCommitContext(
            idempotency_key=args.idempotency_key,
            principal=args.principal,
            source_event_key=args.source_event_key,
        ),
        now=args.now,
    )
    _print(receipt)


def _show(args: argparse.Namespace) -> None:
    _print(FaultCampaignStore().get(args.campaign_id))


def _list(args: argparse.Namespace) -> None:
    snapshots = FaultCampaignStore().list(quest_id=args.quest_id, limit=args.limit)
    _print([item.model_dump(mode="json") for item in snapshots])


def _audit(args: argparse.Namespace) -> None:
    _print(FaultCampaignStore().audit(args.quest_id))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    evaluate = commands.add_parser(
        "evaluate",
        help="recompute a report from a frozen manifest and complete observations",
    )
    evaluate.add_argument("manifest")
    evaluate.add_argument("observations")
    evaluate.add_argument("--completed-at", type=_timestamp)
    evaluate.add_argument("--output")
    evaluate.set_defaults(handler=_evaluate)

    commit = commands.add_parser(
        "commit",
        help="commit a fully replayable report through the scientific outbox",
    )
    commit.add_argument("report")
    commit.add_argument("--idempotency-key", required=True)
    commit.add_argument("--principal", required=True)
    commit.add_argument("--source-event-key")
    commit.add_argument("--now", type=_timestamp)
    commit.set_defaults(handler=_commit)

    show = commands.add_parser("show", help="reconstruct one persisted campaign")
    show.add_argument("campaign_id")
    show.set_defaults(handler=_show)

    list_command = commands.add_parser("list", help="list replay-verified campaigns")
    list_command.add_argument("--quest-id")
    list_command.add_argument("--limit", type=int, default=100)
    list_command.set_defaults(handler=_list)

    audit = commands.add_parser(
        "audit",
        help="evaluate whether a Quest may enter endurance-gate review",
    )
    audit.add_argument("quest_id")
    audit.set_defaults(handler=_audit)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
