"""F11-S6 deterministic fault injection, recovery evidence, and zero-loss acceptance."""

from __future__ import annotations

import asyncio
import errno
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.jobs import (
    CORE_ZERO_METRICS,
    DurableTaskQueue,
    EnqueueReceipt,
    ExternalActionStatus,
    FaultBoundary,
    FaultCampaignCommitContext,
    FaultCampaignContractError,
    FaultCampaignDisposition,
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
    InvalidTaskTransition,
    LeaseMismatch,
    OneTimeExternalActionSpec,
    OneTimeExternalActionStore,
    RetryPolicy,
    ScientificCommandSpec,
    ScientificMutation,
    ScientificTransitionStore,
    TaskExecutionResult,
    TaskLease,
    TaskSpec,
    TaskStatus,
    evaluate_fault_campaign,
    evaluate_fault_scenario,
    fault_campaign_order,
    run_fault_campaign,
    validate_fault_campaign_report,
)
from aletheia.jobs.persistence import (
    DurableTaskRecord,
    ExternalActionReceiptRecord,
    FaultInjectionCampaignRecord,
    OneTimeExternalActionRecord,
    ScientificCommandRecord,
)
from aletheia.jobs.worker import DurableWorker, InfrastructureTaskFailure
from aletheia.memory.ledger import Decision, Event
from aletheia.memory.service import create_run
from aletheia.programs import (
    GraphCommandContext,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    ProgramGraphStore,
    QuestSpec,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
)
from aletheia.programs.persistence import (
    ResearchMemoryCompactionRecord,
    ResearchMemoryFactRecord,
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


def _task_spec(label: str, *, lease_seconds: int = 2) -> TaskSpec:
    identity = _identity(label)
    return TaskSpec(
        task_id=f"task-{identity}",
        task_type=f"fault.{identity}",
        inputs={"label": label},
        owner="pytest:f11s6",
        idempotency_key=f"idem:{identity}",
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=1,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
    )


def _measured(
    spec: FaultScenarioSpec,
    *,
    specific: dict[FaultMetric, int],
    core: dict[FaultMetric, int] | None = None,
) -> dict[FaultMetric, int]:
    values = {metric: 0 for metric in CORE_ZERO_METRICS}
    if core is not None:
        values.update(core)
    values.update(specific)
    assert set(values) == {item.metric for item in spec.expectations}
    return values


def _result(label: str) -> TaskExecutionResult:
    return TaskExecutionResult(
        result_artifact_id=f"artifact:{label}",
        result={"label": label, "valid": True},
    )


def test_real_ten_boundary_campaign_recovers_without_loss_or_duplicate_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _create_quest("real-matrix")
    manifest = _manifest(
        quest_id=quest.node_id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    specs = {item.boundary: item for item in manifest.scenarios}

    def api_process(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = _task_spec("api-process")
        script = "\n".join(
            (
                "import os",
                "from aletheia.jobs import DurableTaskQueue, TaskSpec",
                f"spec=TaskSpec.model_validate_json({task.model_dump_json()!r})",
                "receipt=DurableTaskQueue(principal='fault-api-child').enqueue(spec)",
                "print(receipt.model_dump_json(), flush=True)",
                "os._exit(51)",
            )
        )
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        assert child.returncode == 51, child.stderr
        child_receipt = EnqueueReceipt.model_validate_json(child.stdout.strip().splitlines()[-1])
        replay = DurableTaskQueue(principal="fault-api-recovery").enqueue(task)
        assert child_receipt.created is True and replay.created is False
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(DurableTaskRecord)
                .where(DurableTaskRecord.task_id == task.task_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == f"durable-task:{task.task_id}:1")
            )
        assert state_count is not None and event_count is not None
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(event_count != state_count),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(not replay.created),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "child_returncode": child.returncode,
                "task_id": task.task_id,
                "state_count": state_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def worker_process(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = _task_spec("worker-process", lease_seconds=2)
        queue = DurableTaskQueue(principal="fault-worker-parent")
        queue.enqueue(task)
        script = "\n".join(
            (
                "import os",
                "from aletheia.jobs import DurableTaskQueue",
                "queue=DurableTaskQueue(principal='fault-worker-child')",
                (
                    "lease=queue.claim(worker_id='killed-fault-worker',"
                    "worker_manifest_sha256='a'*64,"
                    f"task_types=[{task.task_type!r}])"
                ),
                "print(lease.model_dump_json(), flush=True)",
                "os._exit(52)",
            )
        )
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        assert child.returncode == 52, child.stderr
        killed_lease = TaskLease.model_validate_json(child.stdout.strip().splitlines()[-1])
        recovered_at = killed_lease.lease_expires_at + timedelta(microseconds=1)
        recovered = queue.recover_expired(now=recovered_at)
        replacement = queue.claim(
            worker_id="replacement-fault-worker",
            worker_manifest_sha256="b" * 64,
            task_types=(task.task_type,),
            now=recovered_at,
        )
        assert replacement is not None
        rejected_stale = 0
        with pytest.raises((InvalidTaskTransition, LeaseMismatch)):
            queue.complete(killed_lease, _result("stale-worker"), now=recovered_at)
        rejected_stale += 1
        completed = queue.complete(
            replacement,
            _result("replacement-worker"),
            now=recovered_at + timedelta(microseconds=1),
        )
        attempts = queue.attempts(task.task_id)
        task_count = 1 if queue.get(task.task_id).status is TaskStatus.SUCCEEDED else 0
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - task_count,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RECOVERED_TASK_COUNT: int(
                    task.task_id in recovered.recovered_task_ids
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: int(
                    completed.task.status is TaskStatus.SUCCEEDED
                ),
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
                FaultMetric.REJECTED_STALE_CALLBACK_COUNT: rejected_stale,
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "child_returncode": child.returncode,
                "task_id": task.task_id,
                "attempt_ids": [item.attempt_id for item in attempts],
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def database_connection(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        run_id = create_run("F11-S6 database reconnect", domain="resilience")
        command = ScientificCommandSpec(
            run_id=run_id,
            command_type="scientific.generic",
            aggregate_type="fault_state",
            aggregate_id=_identity("database-state"),
            idempotency_key=_identity("database-command"),
            source_event_key=_identity("database-source"),
            input={"scenario": spec.scenario_id},
            principal="pytest:f11s6-database",
            event_type="fault_database_state_committed",
        )
        assert command.command_id is not None

        def apply(session):
            row = Decision(
                run_id=run_id,
                stage_from="fault",
                stage_to="recovered",
                rationale=command.aggregate_id,
                actor="pytest:f11s6",
                scientific_command_id=command.command_id,
            )
            session.add(row)
            session.flush()
            return ScientificMutation(
                result={"decision_id": row.id},
                event_projection={"decision_id": row.id},
            )

        def disconnect(point, _session):
            if point == "after_event_before_receipt":
                raise ConnectionError("injected database connection loss")

        transition = ScientificTransitionStore()
        with pytest.raises(ConnectionError, match="injected"):
            transition.execute(command, apply, fault_hook=disconnect)
        with session_scope() as session:
            assert session.get(ScientificCommandRecord, command.command_id) is None
            assert session.scalar(
                select(Decision).where(Decision.scientific_command_id == command.command_id)
            ) is None
            assert session.scalar(
                select(Event).where(Event.event_key == command.output_event_key)
            ) is None

        engine().dispose()
        committed = transition.execute(command, apply)
        replay = transition.execute(command, lambda _session: pytest.fail("replay applied"))
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(Decision)
                .where(Decision.scientific_command_id == command.command_id)
            )
            command_count = session.scalar(
                select(func.count())
                .select_from(ScientificCommandRecord)
                .where(ScientificCommandRecord.command_id == command.command_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == command.output_event_key)
            )
        assert state_count is not None and command_count is not None and event_count is not None
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(
                    len({state_count, command_count, event_count}) != 1
                ),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.COMMITTED_COMMAND_COUNT: command_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(
                    committed.created and not replay.created
                ),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "command_id": command.command_id,
                "state_count": state_count,
                "command_count": command_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def retrying_worker(
        spec: FaultScenarioSpec,
        *,
        label: str,
        first_failure: type[Exception],
    ) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = _task_spec(label)
        queue = DurableTaskQueue(principal=f"fault-{label}")
        queue.enqueue(task)
        calls = 0

        async def handler(_task):
            nonlocal calls
            calls += 1
            if calls == 1:
                if first_failure is TimeoutError:
                    raise TimeoutError("injected evaluator timeout")
                raise InfrastructureTaskFailure("injected provider unavailable")
            return _result(f"{label}-success")

        worker = DurableWorker(
            worker_id=f"fault-worker-{label}",
            worker_manifest_sha256=_sha(f"worker-{label}"),
            handlers={task.task_type: handler},
            queue=queue,
        )
        first = asyncio.run(worker.run_once())
        second = asyncio.run(worker.run_once())
        assert first is not None and second is not None
        attempts = queue.attempts(task.task_id)
        infrastructure_failures = sum(
            item.terminal_category is not None
            and item.terminal_category.value == "infrastructure"
            for item in attempts
        )
        succeeded = int(second.task.status is TaskStatus.SUCCEEDED)
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RETRYABLE_INFRASTRUCTURE_FAILURE_COUNT: (
                    infrastructure_failures
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "task_id": task.task_id,
                "calls": calls,
                "attempt_categories": [
                    item.terminal_category.value if item.terminal_category else None
                    for item in attempts
                ],
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def evaluator(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        return retrying_worker(spec, label="evaluator-timeout", first_failure=TimeoutError)

    def provider(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        return retrying_worker(
            spec,
            label="provider-unavailable",
            first_failure=InfrastructureTaskFailure,
        )

    def duplicate_delivery(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        run_id = create_run("F11-S6 duplicate delivery", domain="resilience")
        command = ScientificCommandSpec(
            run_id=run_id,
            command_type="scientific.generic",
            aggregate_type="fault_state",
            aggregate_id=_identity("duplicate-state"),
            idempotency_key=_identity("duplicate-command"),
            source_event_key=_identity("duplicate-source"),
            input={"scenario": spec.scenario_id},
            principal="pytest:f11s6-duplicate",
            event_type="fault_duplicate_state_committed",
        )
        assert command.command_id is not None
        calls = 0

        def apply(session):
            nonlocal calls
            calls += 1
            row = Decision(
                run_id=run_id,
                stage_from="delivery",
                stage_to="committed",
                rationale=command.aggregate_id,
                actor="pytest:f11s6",
                scientific_command_id=command.command_id,
            )
            session.add(row)
            session.flush()
            return ScientificMutation(
                result={"decision_id": row.id},
                event_projection={"decision_id": row.id},
            )

        transition = ScientificTransitionStore()
        first = transition.execute(command, apply)
        replay = transition.execute(command, apply)
        with session_scope() as session:
            state_count = session.scalar(
                select(func.count())
                .select_from(Decision)
                .where(Decision.scientific_command_id == command.command_id)
            )
            command_count = session.scalar(
                select(func.count())
                .select_from(ScientificCommandRecord)
                .where(ScientificCommandRecord.command_id == command.command_id)
            )
            event_count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_key == command.output_event_key)
            )
        assert state_count is not None and command_count is not None and event_count is not None
        assert calls == 1
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - state_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(state_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: int(
                    len({state_count, command_count, event_count}) != 1
                ),
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: state_count,
                FaultMetric.COMMITTED_COMMAND_COUNT: command_count,
                FaultMetric.KEYED_EVENT_COUNT: event_count,
                FaultMetric.REPLAYED_RECEIPT_COUNT: int(
                    first.created and not replay.created
                ),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "command_id": command.command_id,
                "callback_count": calls,
                "state_count": state_count,
                "event_count": event_count,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def stale_lease(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = _task_spec("stale-lease", lease_seconds=2)
        queue = DurableTaskQueue(principal="fault-stale-lease")
        queue.enqueue(task)
        stale = queue.claim(
            worker_id="stale-owner",
            worker_manifest_sha256="c" * 64,
            task_types=(task.task_type,),
        )
        assert stale is not None
        recovered_at = stale.lease_expires_at + timedelta(microseconds=1)
        recovered = queue.recover_expired(now=recovered_at)
        replacement = queue.claim(
            worker_id="lease-replacement",
            worker_manifest_sha256="d" * 64,
            task_types=(task.task_type,),
            now=recovered_at,
        )
        assert replacement is not None
        rejected_stale = 0
        with pytest.raises((InvalidTaskTransition, LeaseMismatch)):
            queue.complete(stale, _result("late-stale"), now=recovered_at)
        rejected_stale += 1
        completed = queue.complete(
            replacement,
            _result("lease-recovered"),
            now=recovered_at + timedelta(microseconds=1),
        )
        attempts = queue.attempts(task.task_id)
        succeeded = int(completed.task.status is TaskStatus.SUCCEEDED)
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.RECOVERED_TASK_COUNT: int(
                    task.task_id in recovered.recovered_task_ids
                ),
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
                FaultMetric.REJECTED_STALE_CALLBACK_COUNT: rejected_stale,
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={"task_id": task.task_id, "attempt_count": len(attempts)},
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def archive_storage(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        archive_root = tmp_path / f"fault-archive-{uuid.uuid4().hex}"
        memory = ResearchMemoryStore(archive_root=archive_root)
        fact = ResearchMemoryFactSpec(
            scope_node_id=quest.node_id,
            kind=MemoryFactKind.NEGATIVE_RESULT,
            statement="The failed mechanism remains exact after archive exhaustion.",
            detail={"scenario": spec.scenario_id},
            task_bindings=(
                MemoryTaskBindingSpec(
                    task_key="fault-resume",
                    context_role=MemoryContextRole.REQUIRED,
                ),
            ),
            sources=(
                MemorySourceRef(
                    kind=MemorySourceKind.ARTIFACT,
                    source_id=_identity("fault-memory-source"),
                    sha256=_sha("fault-memory-source"),
                ),
            ),
        )
        memory.register_fact(
            fact,
            GraphCommandContext(
                idempotency_key=_identity("fault-memory-fact"),
                principal="pytest:f11s6-memory",
            ),
        )
        draft = MemorySummaryDraft(
            producer_provider="fault-harness",
            producer_model="archive-exhaustion",
            prompt_sha256=_sha("archive-prompt"),
            summary_text="The negative result remains preserved exactly.",
            covered_fact_ids=(fact.fact_id,),
        )

        def exhausted(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "injected archive quota exhausted")

        monkeypatch.setattr(memory._archive, "store", exhausted)
        with pytest.raises(OSError) as failure:
            memory.compact(
                scope_node_id=quest.node_id,
                task_key="fault-resume",
                draft=draft,
                context=GraphCommandContext(
                    idempotency_key=_identity("fault-memory-compact"),
                    principal="pytest:f11s6-memory",
                ),
            )
        assert failure.value.errno == errno.ENOSPC
        facts = memory.eligible_facts(quest.node_id, "fault-resume")
        with session_scope() as session:
            compaction_count = session.scalar(
                select(func.count())
                .select_from(ResearchMemoryCompactionRecord)
                .where(
                    ResearchMemoryCompactionRecord.scope_node_id == quest.node_id,
                    ResearchMemoryCompactionRecord.task_key == "fault-resume",
                )
            )
            fact_count = session.scalar(
                select(func.count())
                .select_from(ResearchMemoryFactRecord)
                .where(ResearchMemoryFactRecord.fact_id == fact.fact_id)
            )
        files = tuple(path for path in archive_root.rglob("*") if path.is_file())
        assert compaction_count is not None and fact_count is not None
        assert tuple(item.fact_id for item in facts) == (fact.fact_id,)
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: max(1 - fact_count, 0),
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: max(fact_count - 1, 0),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.COMMITTED_SCIENTIFIC_STATE_COUNT: fact_count,
                FaultMetric.COMMITTED_ARCHIVE_COUNT: compaction_count,
                FaultMetric.ORPHAN_ARCHIVE_COUNT: len(files),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "fact_id": fact.fact_id,
                "fact_count": fact_count,
                "compaction_count": compaction_count,
                "orphan_files": [str(item) for item in files],
                "errno": failure.value.errno,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def runtime_identity(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        task = _task_spec("runtime-identity")
        queue = DurableTaskQueue(principal="fault-runtime-identity")
        queue.enqueue(task)
        lease = queue.claim(
            worker_id="identity-worker",
            worker_manifest_sha256="e" * 64,
            task_types=(task.task_type,),
        )
        assert lease is not None
        forged = lease.model_copy(update={"worker_manifest_sha256": "f" * 64})
        rejected = 0
        with pytest.raises(LeaseMismatch):
            queue.complete(forged, _result("forged-runtime"))
        rejected += 1
        assert queue.get(task.task_id).status is TaskStatus.LEASED
        completed = queue.complete(lease, _result("verified-runtime"))
        attempts = queue.attempts(task.task_id)
        succeeded = int(completed.task.status is TaskStatus.SUCCEEDED)
        values = _measured(
            spec,
            core={
                FaultMetric.SCIENTIFIC_STATE_LOSS_COUNT: 1 - succeeded,
                FaultMetric.DUPLICATE_SCIENTIFIC_STATE_COUNT: 0,
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.REJECTED_RUNTIME_MISMATCH_COUNT: rejected,
                FaultMetric.SUCCEEDED_TASK_COUNT: succeeded,
                FaultMetric.TASK_ATTEMPT_COUNT: len(attempts),
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "task_id": task.task_id,
                "accepted_manifest": lease.worker_manifest_sha256,
                "rejected_manifest": forged.worker_manifest_sha256,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    def outward_action(spec: FaultScenarioSpec) -> FaultScenarioObservation:
        started = datetime.now(timezone.utc)
        run_id = create_run("F11-S6 ambiguous outward action", domain="resilience")
        action = OneTimeExternalActionSpec(
            run_id=run_id,
            action_type="provider.request",
            scope_key=_identity("fault-outward"),
            request={"scenario": spec.scenario_id, "payload_sha256": _sha("outward")},
            principal="pytest:f11s6-outward",
            claim_ttl_seconds=1,
        )
        store = OneTimeExternalActionStore()
        claim = store.claim(action, claim_owner="fault-outward-worker")
        assert claim.execution_token is not None
        recovered = store.recover_stale(
            now=claim.action.reconcile_after + timedelta(microseconds=1)
        )
        replay = store.claim(
            action,
            claim_owner="fault-outward-replacement",
            now=claim.action.reconcile_after + timedelta(seconds=1),
        )
        assert replay.execution_token is None
        with session_scope() as session:
            authorization_count = session.scalar(
                select(func.count())
                .select_from(OneTimeExternalActionRecord)
                .where(OneTimeExternalActionRecord.action_id == action.action_id)
            )
            receipt_count = session.scalar(
                select(func.count())
                .select_from(ExternalActionReceiptRecord)
                .where(ExternalActionReceiptRecord.action_id == action.action_id)
            )
        assert authorization_count is not None and receipt_count is not None
        reconciliation_count = int(
            replay.action.status is ExternalActionStatus.RECONCILIATION_REQUIRED
            and action.action_id in recovered.action_ids
        )
        values = _measured(
            spec,
            core={
                FaultMetric.DUPLICATE_OUTWARD_AUTHORIZATION_COUNT: max(
                    authorization_count - 1, 0
                ),
                FaultMetric.UNRESOLVED_AMBIGUITY_WITHOUT_BLOCK_COUNT: int(
                    reconciliation_count == 0
                ),
                FaultMetric.EVENT_STATE_MISMATCH_COUNT: 0,
            },
            specific={
                FaultMetric.OUTWARD_AUTHORIZATION_COUNT: authorization_count,
                FaultMetric.OUTWARD_RECEIPT_COUNT: receipt_count,
                FaultMetric.RECONCILIATION_REQUIRED_COUNT: reconciliation_count,
            },
        )
        return _observation(
            spec,
            measured=values,
            detail={
                "action_id": action.action_id,
                "authorization_count": authorization_count,
                "receipt_count": receipt_count,
                "status": replay.action.status.value,
            },
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    by_boundary = {
        FaultBoundary.API_PROCESS: api_process,
        FaultBoundary.WORKER_PROCESS: worker_process,
        FaultBoundary.DATABASE_CONNECTION: database_connection,
        FaultBoundary.EVALUATOR: evaluator,
        FaultBoundary.PROVIDER: provider,
        FaultBoundary.DUPLICATE_DELIVERY: duplicate_delivery,
        FaultBoundary.STALE_LEASE: stale_lease,
        FaultBoundary.ARCHIVE_STORAGE: archive_storage,
        FaultBoundary.RUNTIME_IDENTITY: runtime_identity,
        FaultBoundary.OUTWARD_ACTION: outward_action,
    }
    report = run_fault_campaign(
        manifest,
        {
            specs[boundary].scenario_id: executor
            for boundary, executor in by_boundary.items()
        },
        clock=lambda: datetime.now(timezone.utc),
    )
    assert report.disposition is FaultCampaignDisposition.PASSED
    assert report.scenario_count == report.passed_count == len(FaultBoundary)
    assert report.failed_count == report.blocked_count == 0
    assert all(
        getattr(report, metric.value) == 0
        for metric in CORE_ZERO_METRICS
    )
    assert validate_fault_campaign_report(report) == report

    receipt = FaultCampaignStore().commit(
        report,
        FaultCampaignCommitContext(
            idempotency_key=_identity("real-fault-campaign"),
            principal="pytest:f11s6-real-harness",
        ),
        now=report.completed_at + timedelta(microseconds=1),
    )
    snapshot = FaultCampaignStore().get(receipt.campaign_id)
    assert snapshot.report == report
    audit = FaultCampaignStore().audit(quest.node_id)
    assert audit.latest_campaign_id == receipt.campaign_id
    assert audit.eligible_for_endurance_gate_review is True
    assert audit.autonomous_allocation_enabled is False
