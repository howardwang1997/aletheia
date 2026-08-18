#!/usr/bin/env python3
"""Prepare, run, evaluate, commit, and audit F11 fault-injection campaigns."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from aletheia.jobs import (
    FaultCampaignCommitContext,
    FaultHarnessEnvironmentManifest,
    FaultHarnessEvidenceBundle,
    FaultCampaignManifest,
    FaultCampaignReport,
    FaultCampaignStore,
    FaultScenarioObservation,
    capture_fault_harness_environment,
    evaluate_fault_campaign,
    prepare_durable_fault_campaign,
    run_durable_fault_campaign,
    validate_fault_harness_bundle,
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


def _write_new_json(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump_json"):
        rendered = value.model_dump_json(indent=2)
    else:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    payload = (rendered + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes either progress or raise
                raise OSError("fault-campaign output write made no progress")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _prepare(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest_output)
    environment_path = Path(args.environment_output)
    if manifest_path.exists() or environment_path.exists():
        raise FileExistsError("prepare outputs are write-once and must not already exist")
    environment = capture_fault_harness_environment()
    manifest = prepare_durable_fault_campaign(
        quest_id=args.quest_id,
        environment=environment,
        campaign_key=args.campaign_key,
        seed=args.seed,
        created_at=args.created_at,
    )
    _write_new_json(args.environment_output, environment)
    _write_new_json(args.manifest_output, manifest)
    _print(
        {
            "campaign_id": manifest.campaign_id,
            "environment_manifest_sha256": environment.environment_manifest_sha256,
            "environment_output": str(environment_path),
            "harness_code_sha256": environment.harness_code_sha256,
            "manifest_output": str(manifest_path),
            "quest_id": manifest.quest_id,
        }
    )


def _run_harness(args: argparse.Namespace) -> None:
    manifest = FaultCampaignManifest.model_validate(_read_json(args.manifest))
    environment = FaultHarnessEnvironmentManifest.model_validate(
        _read_json(args.environment)
    )
    bundle = run_durable_fault_campaign(
        manifest,
        environment=environment,
        principal=args.principal,
        archive_root=Path(args.archive_root) if args.archive_root else None,
    )
    if args.output is not None:
        _write_new_json(args.output, bundle)
    else:
        print(bundle.model_dump_json(indent=2))


def _verify_bundle(args: argparse.Namespace) -> None:
    bundle = FaultHarnessEvidenceBundle.model_validate(_read_json(args.bundle))
    verified = validate_fault_harness_bundle(bundle)
    _print(
        {
            "bundle_sha256": verified.bundle_sha256,
            "campaign_id": verified.report.manifest.campaign_id,
            "disposition": verified.report.disposition.value,
            "report_sha256": verified.report.report_sha256,
            "scenario_count": verified.report.scenario_count,
        }
    )


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
    raw = _read_json(args.report)
    if isinstance(raw, dict) and {"environment", "report", "diagnostics"}.issubset(raw):
        report = validate_fault_harness_bundle(
            FaultHarnessEvidenceBundle.model_validate(raw)
        ).report
    else:
        report = FaultCampaignReport.model_validate(raw)
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

    prepare = commands.add_parser(
        "prepare",
        help="freeze the production harness, environment, and ten-boundary manifest",
    )
    prepare.add_argument("quest_id")
    prepare.add_argument("--campaign-key")
    prepare.add_argument("--seed", type=int, default=17)
    prepare.add_argument("--created-at", type=_timestamp)
    prepare.add_argument("--manifest-output", required=True)
    prepare.add_argument("--environment-output", required=True)
    prepare.set_defaults(handler=_prepare)

    run = commands.add_parser(
        "run",
        help="execute the supported real ten-boundary harness from frozen inputs",
    )
    run.add_argument("manifest")
    run.add_argument("environment")
    run.add_argument("--principal", required=True)
    run.add_argument("--archive-root")
    run.add_argument("--output")
    run.set_defaults(handler=_run_harness)

    verify = commands.add_parser(
        "verify-bundle",
        help="rehash a production environment/report/diagnostic evidence bundle",
    )
    verify.add_argument("bundle")
    verify.set_defaults(handler=_verify_bundle)

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
