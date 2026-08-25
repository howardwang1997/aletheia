"""F11-S6 deterministic fault injection, recovery evidence, and zero-loss acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.jobs import (
    CORE_ZERO_METRICS,
    FaultBoundary,
    FaultCampaignCommitContext,
    FaultCampaignContractError,
    FaultCampaignDisposition,
    FaultHarnessEnvironmentMismatch,
    FaultHarnessEvidenceBundle,
    FaultCampaignManifest,
    FaultCampaignReport,
    FaultCampaignStore,
    FaultComparator,
    FaultInjectionOutcome,
    FaultInvariantExpectation,
    FaultMetric,
    FaultMetricObservation,
    FaultRecoveryAction,
    FaultScenarioDisposition,
    FaultScenarioObservation,
    FaultScenarioSpec,
    capture_fault_harness_environment,
    evaluate_fault_campaign,
    evaluate_fault_scenario,
    fault_campaign_order,
    prepare_durable_fault_campaign,
    run_durable_fault_campaign,
    run_fault_campaign,
    validate_fault_campaign_report,
    validate_fault_harness_bundle,
    validate_fault_harness_environment,
)
from aletheia.jobs.persistence import FaultInjectionCampaignRecord
from aletheia.programs import (
    GraphCommandContext,
    ProgramGraphStore,
    QuestSpec,
)
from aletheia.reproducibility.manifest import content_sha256
from aletheia.schema_migrations import schema_diffs

T0 = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _sha(label: str) -> str:
    return content_sha256({"f11s6": label})


def _identity(label: str) -> str:
    return f"{label}:{uuid.uuid4().hex}"


_SCENARIO_CONFIG = {
    FaultBoundary.API_PROCESS: (
        FaultInjectionOutcome.PROCESS_EXIT,
        (FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.WORKER_PROCESS: (
        FaultInjectionOutcome.PROCESS_EXIT,
        (
            FaultRecoveryAction.RECLAIM_EXPIRED_LEASE,
            FaultRecoveryAction.REJECT_STALE_CALLBACK,
        ),
        {
            FaultMetric.RECOVERED_TASK_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
            FaultMetric.REJECTED_STALE_CALLBACK_COUNT: 1,
        },
    ),
    FaultBoundary.DATABASE_CONNECTION: (
        FaultInjectionOutcome.CONNECTION_LOST,
        (
            FaultRecoveryAction.RECONNECT_DATABASE,
            FaultRecoveryAction.REPLAY_EXACT_COMMAND,
        ),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_COMMAND_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.EVALUATOR: (
        FaultInjectionOutcome.TIMEOUT,
        (FaultRecoveryAction.RETRY_INFRASTRUCTURE_ATTEMPT,),
        {
            FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
        },
    ),
    FaultBoundary.PROVIDER: (
        FaultInjectionOutcome.UNAVAILABLE,
        (FaultRecoveryAction.RETRY_INFRASTRUCTURE_ATTEMPT,),
        {
            FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
        },
    ),
    FaultBoundary.DUPLICATE_DELIVERY: (
        FaultInjectionOutcome.DUPLICATE_DELIVERED,
        (FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_COMMAND_COUNT: 1,
            FaultMetric.KEYED_EVENT_COUNT: 1,
            FaultMetric.REPLAYED_RECEIPT_COUNT: 1,
        },
    ),
    FaultBoundary.STALE_LEASE: (
        FaultInjectionOutcome.LEASE_EXPIRED,
        (
            FaultRecoveryAction.RECLAIM_EXPIRED_LEASE,
            FaultRecoveryAction.REJECT_STALE_CALLBACK,
        ),
        {
            FaultMetric.RECOVERED_TASK_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 2,
            FaultMetric.REJECTED_STALE_CALLBACK_COUNT: 1,
        },
    ),
    FaultBoundary.ARCHIVE_STORAGE: (
        FaultInjectionOutcome.STORAGE_EXHAUSTED,
        (FaultRecoveryAction.VERIFY_ARCHIVE,),
        {
            FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: 1,
            FaultMetric.COMMITTED_ARCHIVE_COUNT: 0,
            FaultMetric.ORPHAN_ARCHIVE_COUNT: 0,
        },
    ),
    FaultBoundary.RUNTIME_IDENTITY: (
        FaultInjectionOutcome.IDENTITY_MISMATCH,
        (FaultRecoveryAction.REJECT_RUNTIME_MISMATCH,),
        {
            FaultMetric.REJECTED_RUNTIME_MISMATCH_COUNT: 1,
            FaultMetric.SUCCEEDED_TASK_COUNT: 1,
            FaultMetric.TASK_ATTEMPT_COUNT: 1,
        },
    ),
    FaultBoundary.OUTWARD_ACTION: (
        FaultInjectionOutcome.AMBIGUOUS_REMOTE_RESULT,
        (FaultRecoveryAction.REQUIRE_OUTWARD_RECONCILIATION,),
        {
            FaultMetric.OUTWARD_AUTHORIZATION_COUNT: 1,
            FaultMetric.OUTWARD_RECEIPT_COUNT: 0,
            FaultMetric.RECONCILIATION_REQUIRED_COUNT: 1,
        },
    ),
}


def _expectations(extra: dict[FaultMetric, int]) -> tuple[FaultInvariantExpectation, ...]:
    values = {metric: 0 for metric in CORE_ZERO_METRICS}
    values.update(extra)
    return tuple(
        FaultInvariantExpectation(
            metric=metric,
            comparator=FaultComparator.EXACT,
            expected_value=value,
        )
        for metric, value in sorted(values.items(), key=lambda item: item[0].value)
    )


def _manifest(
    *,
    seed: int = 17,
    quest_id: str | None = None,
    created_at: datetime = T0,
) -> FaultCampaignManifest:
    suffix = uuid.uuid4().hex
    scenarios = []
    for boundary, (outcome, actions, extra) in _SCENARIO_CONFIG.items():
        scenarios.append(
            FaultScenarioSpec(
                scenario_id=f"f11s6.{boundary.value}",
                boundary=boundary,
                injection_point=f"durable.{boundary.value}",
                expected_outcome=outcome,
                required_recovery_actions=actions,
                expectations=_expectations(extra),
                timeout_seconds=120,
                tags=("acceptance", "f11s6"),
            )
        )
    return FaultCampaignManifest(
        campaign_key=f"f11s6-{suffix}",
        quest_id=quest_id,
        seed=seed,
        harness_code_sha256=_sha("harness-v1"),
        environment_manifest_sha256=_sha(f"environment-{suffix}"),
        scenarios=tuple(scenarios),
        created_at=created_at,
    )


def _observation(
    spec: FaultScenarioSpec,
    *,
    measured: dict[FaultMetric, int] | None = None,
    outcome: FaultInjectionOutcome | None = None,
    actions: tuple[FaultRecoveryAction, ...] | None = None,
    injection_confirmed: bool = True,
    detail: object | None = None,
    started_at: datetime = T0 + timedelta(seconds=1),
    completed_at: datetime = T0 + timedelta(seconds=2),
) -> FaultScenarioObservation:
    values = (
        measured
        if measured is not None
        else {item.metric: item.expected_value for item in spec.expectations}
    )
    assert set(values) == {item.metric for item in spec.expectations}
    diagnostic = content_sha256(
        {
            "scenario_id": spec.scenario_id,
            "detail": detail if detail is not None else "synthetic-fixture",
        }
    )
    metric_items = tuple(
        FaultMetricObservation(
            metric=metric,
            observed_value=value,
            evidence_sha256=content_sha256(
                {
                    "scenario_id": spec.scenario_id,
                    "metric": metric.value,
                    "observed_value": value,
                    "diagnostic": diagnostic,
                }
            ),
        )
        for metric, value in sorted(values.items(), key=lambda item: item[0].value)
    )
    return FaultScenarioObservation(
        scenario_id=spec.scenario_id,
        observed_outcome=outcome if outcome is not None else spec.expected_outcome,
        injection_confirmed=injection_confirmed,
        recovery_actions=actions if actions is not None else spec.required_recovery_actions,
        metrics=metric_items,
        evidence_sha256s=(diagnostic, *(item.evidence_sha256 for item in metric_items)),
        diagnostic_sha256=diagnostic,
        started_at=started_at,
        completed_at=completed_at,
    )


def _synthetic_report(manifest: FaultCampaignManifest) -> FaultCampaignReport:
    return evaluate_fault_campaign(
        manifest,
        tuple(_observation(spec) for spec in manifest.scenarios),
        completed_at=T0 + timedelta(seconds=3),
    )


def _create_quest(label: str) -> QuestSpec:
    quest = QuestSpec(
        identity_key=f"fault-quest-{label}-{uuid.uuid4().hex}",
        title="Fault recovery acceptance Quest",
        direction="Preserve scientific state across infrastructure failure.",
        value_boundary="A caught exception is not recovery evidence.",
        safety_boundary=("No autonomous external action",),
    )
    ProgramGraphStore().create_quest(
        quest,
        GraphCommandContext(
            idempotency_key=_identity("f11s6-quest"),
            principal="pytest:f11s6",
        ),
    )
    return quest


def test_fault_contract_requires_complete_boundaries_and_six_exact_zero_invariants() -> None:
    manifest = _manifest()
    assert {item.boundary for item in manifest.scenarios} == set(FaultBoundary)
    assert all(
        {
            item.metric
            for item in scenario.expectations
            if item.comparator is FaultComparator.EXACT and item.expected_value == 0
        }.issuperset(CORE_ZERO_METRICS)
        for scenario in manifest.scenarios
    )

    duplicate_boundary = FaultScenarioSpec(
        **{
            **manifest.scenarios[0].model_dump(),
            "scenario_id": "f11s6.api_process.extra",
        }
    )
    with pytest.raises(ValidationError, match="boundary matrix is incomplete"):
        FaultCampaignManifest(
            **{
                **manifest.model_dump(exclude={"campaign_id"}),
                "scenarios": (*manifest.scenarios[:-1], duplicate_boundary),
            }
        )
    first = manifest.scenarios[0]
    with pytest.raises(ValidationError, match="must require exact zero"):
        FaultScenarioSpec(
            **{
                **first.model_dump(),
                "expectations": tuple(
                    item
                    for item in first.expectations
                    if item.metric is not FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT
                ),
            }
        )


def test_production_harness_rejects_environment_drift_before_execution() -> None:
    quest = _create_quest("environment-drift")
    environment = capture_fault_harness_environment()
    manifest = prepare_durable_fault_campaign(
        quest_id=quest.node_id,
        environment=environment,
        campaign_key=f"f11s6.environment-drift.{uuid.uuid4().hex}",
    )
    changed = environment.model_copy(
        update={"platform_release": f"{environment.platform_release}-changed"}
    )
    with pytest.raises(FaultHarnessEnvironmentMismatch, match="environment hash differs"):
        validate_fault_harness_environment(manifest, changed, current=changed)


def test_evaluator_rejects_missing_injection_recovery_evidence_and_failed_invariant() -> None:
    spec = _manifest().scenarios[0]
    blocked = evaluate_fault_scenario(
        spec,
        _observation(spec, injection_confirmed=False),
    )
    assert blocked.disposition is FaultScenarioDisposition.BLOCKED
    assert "injection:not_confirmed" in blocked.blockers

    values = {item.metric: item.expected_value for item in spec.expectations}
    values[FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT] = 1
    failed = evaluate_fault_scenario(
        spec,
        _observation(spec, measured=values, actions=(FaultRecoveryAction.REBUILD_FROM_LEDGER,)),
    )
    assert failed.disposition is FaultScenarioDisposition.FAILED
    assert any(item.startswith("recovery:missing:") for item in failed.blockers)
    assert any(
        item.startswith("invariant:failed:scientific_state_loss_count")
        for item in failed.blockers
    )

    incomplete = _observation(spec).model_copy(update={"evidence_sha256s": (_sha("only"),)})
    result = evaluate_fault_scenario(spec, incomplete)
    assert result.disposition is FaultScenarioDisposition.FAILED
    assert any(item.startswith("evidence:missing:") for item in result.blockers)


def test_campaign_order_is_seeded_and_report_is_recomputed() -> None:
    manifest = _manifest(seed=9127)
    first = fault_campaign_order(manifest)
    second = fault_campaign_order(
        FaultCampaignManifest.model_validate(manifest.model_dump(mode="python"))
    )
    assert first == second
    assert {item.scenario_id for item in first} == {
        item.scenario_id for item in manifest.scenarios
    }

    report = run_fault_campaign(
        manifest,
        {
            spec.scenario_id: (lambda item, spec=spec: _observation(spec))
            for spec in manifest.scenarios
        },
        clock=lambda: T0 + timedelta(seconds=3),
    )
    assert report.disposition is FaultCampaignDisposition.PASSED
    assert report.passed_count == 10
    assert validate_fault_campaign_report(report) == report

    with pytest.raises(FaultCampaignContractError, match="executor matrix"):
        run_fault_campaign(manifest, {}, clock=lambda: T0 + timedelta(seconds=3))


def test_campaign_store_exact_replay_append_only_and_audit() -> None:
    quest = _create_quest("persistence")
    manifest = _manifest(quest_id=quest.node_id)
    report = _synthetic_report(manifest)
    context = FaultCampaignCommitContext(
        idempotency_key=_identity("fault-campaign"),
        principal="pytest:f11s6-harness",
    )
    store = FaultCampaignStore()
    first = store.commit(report, context, now=T0 + timedelta(seconds=4))
    replay = store.commit(report, context, now=T0 + timedelta(days=1))
    assert first.created is True
    assert replay.created is False
    assert replay.command_id == first.command_id
    assert store.get(first.campaign_id).report == report
    assert store.list(quest_id=quest.node_id)[0].report == report

    audit = store.audit(quest.node_id)
    assert audit.latest_campaign_passed is True
    assert audit.eligible_for_endurance_gate_review is True
    assert audit.autonomous_allocation_enabled is False

    with pytest.raises(DBAPIError, match="append-only"):
        with session_scope() as session:
            session.execute(
                update(FaultInjectionCampaignRecord)
                .where(FaultInjectionCampaignRecord.campaign_id == first.campaign_id)
                .values(passed_count=9, failed_count=1, disposition="failed")
            )


def test_failed_campaign_is_preserved_and_blocks_endurance_review() -> None:
    quest = _create_quest("failed-campaign")
    manifest = _manifest(quest_id=quest.node_id)
    observations = []
    for index, spec in enumerate(manifest.scenarios):
        values = {item.metric: item.expected_value for item in spec.expectations}
        if index == 0:
            values[FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT] = 1
        observations.append(_observation(spec, measured=values))
    report = evaluate_fault_campaign(
        manifest,
        tuple(observations),
        completed_at=T0 + timedelta(seconds=3),
    )
    assert report.disposition is FaultCampaignDisposition.FAILED
    assert report.scientific_state_loss_count == 1
    receipt = FaultCampaignStore().commit(
        report,
        FaultCampaignCommitContext(
            idempotency_key=_identity("failed-fault-campaign"),
            principal="pytest:f11s6-failed-harness",
        ),
        now=T0 + timedelta(seconds=4),
    )
    assert FaultCampaignStore().get(receipt.campaign_id).report == report
    audit = FaultCampaignStore().audit(quest.node_id)
    assert audit.latest_campaign_passed is False
    assert audit.eligible_for_endurance_gate_review is False
    assert audit.blockers == ("campaign:latest_not_passed:failed",)
    assert audit.autonomous_allocation_enabled is False


def test_fault_campaign_migration_matches_orm_and_has_guards() -> None:
    with engine().connect() as connection:
        assert "fault_injection_campaigns" in inspect(connection).get_table_names()
        assert schema_diffs(connection) == []
        triggers = set(
            connection.execute(
                __import__("sqlalchemy").text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND "
                    "tgname LIKE 'trg_fault_injection_campaign%'"
                )
            ).scalars()
        )
    assert triggers == {
        "trg_fault_injection_campaign_guard",
        "trg_fault_injection_campaigns_append_only",
    }



def test_real_ten_boundary_campaign_recovers_without_loss_or_duplicate_effects(
    tmp_path: Path,
) -> None:
    quest = _create_quest("production-harness")
    environment = capture_fault_harness_environment()
    manifest = prepare_durable_fault_campaign(
        quest_id=quest.node_id,
        environment=environment,
        campaign_key=f"f11s6.production.{uuid.uuid4().hex}",
        seed=17,
    )
    bundle = run_durable_fault_campaign(
        manifest,
        environment=environment,
        principal="pytest:f11s6-production",
        archive_root=tmp_path / "fault-archive",
    )
    report = bundle.report
    assert report.disposition is FaultCampaignDisposition.PASSED
    assert report.scenario_count == report.passed_count == len(FaultBoundary)
    assert report.failed_count == report.blocked_count == 0
    assert all(getattr(report, metric.value) == 0 for metric in CORE_ZERO_METRICS)
    assert validate_fault_campaign_report(report) == report
    assert validate_fault_harness_bundle(bundle) == bundle
    assert set(bundle.diagnostics) == {item.scenario_id for item in manifest.scenarios}

    changed = bundle.model_dump(mode="python")
    changed["diagnostics"]["f11s6.api_process"]["state_count"] = 99
    with pytest.raises(ValidationError, match="diagnostic changed"):
        FaultHarnessEvidenceBundle.model_validate(changed)

    receipt = FaultCampaignStore().commit(
        report,
        FaultCampaignCommitContext(
            idempotency_key=_identity("real-fault-campaign"),
            principal="pytest:f11s6-production",
        ),
        now=report.completed_at + timedelta(microseconds=1),
    )
    snapshot = FaultCampaignStore().get(receipt.campaign_id)
    assert snapshot.report == report
    audit = FaultCampaignStore().audit(quest.node_id)
    assert audit.latest_campaign_id == receipt.campaign_id
    assert audit.eligible_for_endurance_gate_review is True
    assert audit.autonomous_allocation_enabled is False


def test_fault_campaign_cli_prepares_runs_and_verifies_production_bundle(
    tmp_path: Path,
) -> None:
    quest = _create_quest("production-cli")
    manifest_path = tmp_path / "manifest.json"
    environment_path = tmp_path / "environment.json"
    bundle_path = tmp_path / "evidence-bundle.json"
    script = Path(__file__).parents[2] / "scripts" / "fault_campaign.py"

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        return completed

    prepared = invoke(
        "prepare",
        quest.node_id,
        "--campaign-key",
        f"f11s6.production-cli.{uuid.uuid4().hex}",
        "--manifest-output",
        str(manifest_path),
        "--environment-output",
        str(environment_path),
    )
    prepared_summary = json.loads(prepared.stdout)
    assert prepared_summary["quest_id"] == quest.node_id
    invoke(
        "run",
        str(manifest_path),
        str(environment_path),
        "--principal",
        "pytest:f11s6-cli",
        "--archive-root",
        str(tmp_path / "fault-archive"),
        "--output",
        str(bundle_path),
    )
    bundle = FaultHarnessEvidenceBundle.model_validate_json(bundle_path.read_text())
    assert bundle.report.disposition is FaultCampaignDisposition.PASSED
    verified = json.loads(invoke("verify-bundle", str(bundle_path)).stdout)
    assert verified == {
        "bundle_sha256": bundle.bundle_sha256,
        "campaign_id": bundle.report.manifest.campaign_id,
        "disposition": "passed",
        "report_sha256": bundle.report.report_sha256,
        "scenario_count": 10,
    }
