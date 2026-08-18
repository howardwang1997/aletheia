#!/usr/bin/env python3
"""Prepare, resume, finalize, and audit the F11-S7 research endurance gate."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aletheia.programs import (
    REAL_72H_SECONDS,
    EnduranceCheckpointEvidence,
    EnduranceCommandContext,
    EnduranceEfficiencyReceipt,
    EnduranceEvidenceClass,
    EnduranceGateManifest,
    ResearchEnduranceStore,
    prepare_endurance_gate_manifest,
)


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def _print(value: Any, output: str | None = None) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
    else:
        Path(output).write_text(rendered, encoding="utf-8")


def _context(args: argparse.Namespace) -> EnduranceCommandContext:
    return EnduranceCommandContext(
        idempotency_key=args.idempotency_key,
        principal=args.principal,
        source_event_key=args.source_event_key,
    )


def _prepare(args: argparse.Namespace) -> None:
    evidence_class = EnduranceEvidenceClass(args.evidence_class)
    duration = args.duration_seconds
    interval = args.checkpoint_interval_seconds
    maximum_gap = args.maximum_checkpoint_gap_seconds
    if evidence_class is EnduranceEvidenceClass.REAL_TIME_72H:
        duration = duration or REAL_72H_SECONDS
        interval = interval or 60 * 60
        maximum_gap = maximum_gap or 2 * 60 * 60
    else:
        if duration is None or interval is None or maximum_gap is None:
            raise ValueError(
                "accelerated preparation requires duration, checkpoint interval, and maximum gap"
            )
    manifest = prepare_endurance_gate_manifest(
        gate_key=args.gate_key,
        quest_id=args.quest_id,
        evidence_class=evidence_class,
        required_duration_seconds=duration,
        checkpoint_interval_seconds=interval,
        maximum_checkpoint_gap_seconds=maximum_gap,
        prerequisite_fault_campaign_id=args.fault_campaign_id,
        harness_code_sha256=args.harness_code_sha256,
        environment_manifest_sha256=args.environment_manifest_sha256,
        minimum_efficiency_improvement_ppm=args.minimum_efficiency_improvement_ppm,
    )
    _print(manifest, args.output)


def _start(args: argparse.Namespace) -> None:
    manifest = EnduranceGateManifest.model_validate(_read(args.manifest))
    _print(
        ResearchEnduranceStore().start(
            manifest,
            _context(args),
            now=args.accelerated_now,
        )
    )


def _checkpoint(args: argparse.Namespace) -> None:
    evidence = (
        EnduranceCheckpointEvidence.model_validate(_read(args.evidence))
        if args.evidence is not None
        else EnduranceCheckpointEvidence()
    )
    _print(
        ResearchEnduranceStore().append_checkpoint(
            args.gate_id,
            evidence,
            _context(args),
            now=args.accelerated_now,
        )
    )


def _finalize(args: argparse.Namespace) -> None:
    efficiency = (
        EnduranceEfficiencyReceipt.model_validate(_read(args.efficiency))
        if args.efficiency is not None
        else None
    )
    _print(
        ResearchEnduranceStore().finalize(
            args.gate_id,
            _context(args),
            efficiency=efficiency,
            now=args.accelerated_now,
        )
    )


def _show(args: argparse.Namespace) -> None:
    _print(ResearchEnduranceStore().get(args.gate_id))


def _list(args: argparse.Namespace) -> None:
    snapshots = ResearchEnduranceStore().list(quest_id=args.quest_id, limit=args.limit)
    _print([item.model_dump(mode="json") for item in snapshots])


def _audit(args: argparse.Namespace) -> None:
    _print(ResearchEnduranceStore().audit(args.quest_id))


def _command_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--source-event-key")
    parser.add_argument(
        "--accelerated-now",
        type=_timestamp,
        help=(
            "test-only clock; real_time_72h manifests reject this and always use PostgreSQL "
            "clock_timestamp()"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="freeze Quest and prerequisite identities")
    prepare.add_argument("quest_id")
    prepare.add_argument("--gate-key", required=True)
    prepare.add_argument("--fault-campaign-id", required=True)
    prepare.add_argument(
        "--evidence-class",
        choices=[item.value for item in EnduranceEvidenceClass],
        default=EnduranceEvidenceClass.REAL_TIME_72H.value,
    )
    prepare.add_argument("--duration-seconds", type=int)
    prepare.add_argument("--checkpoint-interval-seconds", type=int)
    prepare.add_argument("--maximum-checkpoint-gap-seconds", type=int)
    prepare.add_argument("--harness-code-sha256", required=True)
    prepare.add_argument("--environment-manifest-sha256", required=True)
    prepare.add_argument("--minimum-efficiency-improvement-ppm", type=int, default=100_000)
    prepare.add_argument("--output")
    prepare.set_defaults(handler=_prepare)

    start = commands.add_parser("start", help="commit the database-clock start receipt")
    start.add_argument("manifest")
    _command_context(start)
    start.set_defaults(handler=_start)

    checkpoint = commands.add_parser(
        "checkpoint",
        help="append the next parent-hashed ledger observation",
    )
    checkpoint.add_argument("gate_id")
    checkpoint.add_argument("--evidence", help="new receipts to add at this checkpoint")
    _command_context(checkpoint)
    checkpoint.set_defaults(handler=_checkpoint)

    finalize = commands.add_parser(
        "finalize",
        help="commit a terminal pass/blocked/failed report without erasing evidence",
    )
    finalize.add_argument("gate_id")
    finalize.add_argument("--efficiency", help="independent efficiency receipt JSON")
    _command_context(finalize)
    finalize.set_defaults(handler=_finalize)

    show = commands.add_parser("show", help="reconstruct one gate and its checkpoint chain")
    show.add_argument("gate_id")
    show.set_defaults(handler=_show)

    list_command = commands.add_parser("list", help="list replay-verified gates")
    list_command.add_argument("--quest-id")
    list_command.add_argument("--limit", type=int, default=100)
    list_command.set_defaults(handler=_list)

    audit = commands.add_parser("audit", help="evaluate real 72-hour F11 exit eligibility")
    audit.add_argument("quest_id")
    audit.set_defaults(handler=_audit)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
