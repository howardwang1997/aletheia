"""F11-S2 transactional command/outbox and one-time external-action acceptance."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, session_scope
from aletheia.jobs import (
    ExternalActionStatus,
    ExternalActionTokenMismatch,
    InvalidExternalActionTransition,
    OneTimeExternalActionSpec,
    OneTimeExternalActionStore,
    ScientificCommandSpec,
    ScientificIdempotencyConflict,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import (
    ExternalActionReceiptRecord,
    OneTimeExternalActionRecord,
    ScientificCommandRecord,
)
from aletheia.memory.ledger import Decision, Event
from aletheia.memory.service import (
    create_run,
    finalize_plan,
    list_artifacts,
    record_artifacts,
)
from aletheia.scheduler.statemachine import record_transition

T0 = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _identity(label: str) -> str:
    return f"{label}:{uuid.uuid4().hex}"


def _command(run_id: str, label: str, **updates) -> ScientificCommandSpec:
    values = {
        "run_id": run_id,
        "command_type": "scientific.generic",
        "aggregate_type": "test_state",
        "aggregate_id": _identity(f"aggregate-{label}"),
        "idempotency_key": _identity(f"command-{label}"),
        "source_event_key": _identity(f"source-{label}"),
        "input": {"label": label, "value": 1},
        "principal": "pytest-scientific-command",
        "event_type": "scientific_test_committed",
    }
    values.update(updates)
    return ScientificCommandSpec(**values)


def _decision_mutation(spec: ScientificCommandSpec, calls: list[str]):
    def apply(session):
        calls.append(spec.command_id or "missing")
        row = Decision(
            run_id=spec.run_id,
            stage_from="before",
            stage_to="after",
            rationale=spec.aggregate_id,
            actor="pytest",
            scientific_command_id=spec.command_id,
        )
        session.add(row)
        session.flush()
        return ScientificMutation(
            result={"decision_id": row.id},
            event_projection={"decision_id": row.id, "label": spec.input["label"]},
        )

    return apply


def test_scientific_command_exact_replay_and_duplicate_source_event_apply_once():
    run_id = create_run("F11-S2 duplicate scientific event", domain="materials")
    spec = _command(run_id, "replay")
    calls: list[str] = []
    store = ScientificTransitionStore()

    first = store.execute(spec, _decision_mutation(spec, calls), now=T0)
    replay = store.execute(
        spec,
        lambda _session: pytest.fail("exact replay invoked its mutation"),
        now=T0 + timedelta(days=1),
    )

    assert first.created is True
    assert replay.created is False
    assert replay.result == first.result
    assert replay.output_event_id == first.output_event_id
    assert calls == [spec.command_id]
    with session_scope() as session:
        assert (
            session.scalar(
                select(Decision).where(Decision.scientific_command_id == spec.command_id)
            )
            is not None
        )
        events = session.scalars(
            select(Event).where(Event.event_key == spec.output_event_key)
        ).all()
        assert len(events) == 1
        assert events[0].event_sha256 is not None

    rebound = _command(
        run_id,
        "rebound",
        source_event_key=spec.source_event_key,
        input={"label": "rebound", "value": 2},
    )
    with pytest.raises(ScientificIdempotencyConflict, match="different content"):
        store.execute(rebound, _decision_mutation(rebound, calls), now=T0)
    assert calls == [spec.command_id]


@pytest.mark.parametrize("crash_point", ["after_state_before_event", "after_event_before_receipt"])
def test_scientific_command_crash_points_roll_back_state_command_and_event(crash_point):
    run_id = create_run(f"F11-S2 rollback {crash_point}", domain="materials")
    spec = _command(run_id, crash_point)
    calls: list[str] = []

    def crash(point, _session):
        if point == crash_point:
            raise RuntimeError(f"injected crash at {point}")

    with pytest.raises(RuntimeError, match="injected crash"):
        ScientificTransitionStore().execute(
            spec,
            _decision_mutation(spec, calls),
            now=T0,
            fault_hook=crash,
        )
    with session_scope() as session:
        assert session.get(ScientificCommandRecord, spec.command_id) is None
        assert (
            session.scalar(
                select(Decision).where(Decision.scientific_command_id == spec.command_id)
            )
            is None
        )
        assert session.scalar(select(Event).where(Event.event_key == spec.output_event_key)) is None

    recovered = ScientificTransitionStore().execute(
        spec,
        _decision_mutation(spec, calls),
        now=T0 + timedelta(seconds=1),
    )
    assert recovered.created is True
    assert len(calls) == 2  # the first callback ran, but every one of its writes rolled back


def test_concurrent_scientific_redelivery_has_one_mutation_and_one_receipt():
    run_id = create_run("F11-S2 concurrent scientific command", domain="materials")
    spec = _command(run_id, "concurrent")
    barrier = Barrier(2)
    calls: list[str] = []

    def invoke():
        barrier.wait()
        return ScientificTransitionStore().execute(
            spec,
            _decision_mutation(spec, calls),
            now=T0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _index: invoke(), range(2)))
    assert sorted(receipt.created for receipt in receipts) == [False, True]
    assert calls == [spec.command_id]


def test_artifact_and_stage_commits_are_content_bound_and_replay_safe():
    run_id = create_run("F11-S2 artifact and stage", domain="materials")
    experiment_id = finalize_plan(run_id, {"objective": "transaction test"})
    artifacts = [{"kind": "report", "uri": "artifact://one", "sha256": "a" * 64}]

    first = record_artifacts(experiment_id, artifacts)
    replay = record_artifacts(experiment_id, artifacts)
    assert first.created is True and replay.created is False
    assert len(list_artifacts(experiment_id)) == 1

    explicit_key = _identity("artifact-explicit")
    record_artifacts(
        experiment_id,
        [{"kind": "plot", "uri": "artifact://plot-a"}],
        idempotency_key=explicit_key,
    )
    with pytest.raises(ScientificIdempotencyConflict, match="different content"):
        record_artifacts(
            experiment_id,
            [{"kind": "plot", "uri": "artifact://plot-b"}],
            idempotency_key=explicit_key,
        )


@pytest.mark.asyncio
async def test_stage_decision_and_event_share_one_transactional_command():
    run_id = create_run("F11-S2 stage transition", domain="materials")
    experiment_id = finalize_plan(run_id, {"objective": "stage transaction"})
    first = await record_transition(
        run_id,
        experiment_id,
        "experiment_design",
        "execution",
        "approved exact design",
    )
    replay = await record_transition(
        run_id,
        experiment_id,
        "experiment_design",
        "execution",
        "approved exact design",
    )
    assert first.created is True and replay.created is False
    with session_scope() as session:
        assert (
            len(
                session.scalars(
                    select(Decision).where(Decision.scientific_command_id == first.command_id)
                ).all()
            )
            == 1
        )
        assert session.get(Event, first.output_event_id).type == "stage"


def _action_spec(run_id: str, label: str, *, ttl: int = 60) -> OneTimeExternalActionSpec:
    return OneTimeExternalActionSpec(
        run_id=run_id,
        action_type="provider.request",
        scope_key=_identity(f"external-{label}"),
        request={"label": label, "payload_sha256": "b" * 64},
        principal="pytest-action-authorizer",
        claim_ttl_seconds=ttl,
    )


def test_external_action_returns_one_token_and_one_immutable_receipt():
    run_id = create_run("F11-S2 external receipt", domain="materials")
    spec = _action_spec(run_id, "receipt")
    store = OneTimeExternalActionStore()
    first = store.claim(spec, claim_owner="worker-1", now=T0)
    replay = store.claim(spec, claim_owner="worker-2", now=T0 + timedelta(seconds=1))

    assert first.created is True and first.execution_token
    assert replay.created is False and replay.execution_token is None
    assert replay.action.action_id == first.action.action_id
    assert first.action.provider_idempotency_key == replay.action.provider_idempotency_key
    with session_scope() as session:
        row = session.get(OneTimeExternalActionRecord, first.action.action_id)
        assert row is not None
        assert row.execution_token_sha256 != first.execution_token
        assert first.execution_token not in json.dumps(row.__dict__, default=str)

    with pytest.raises(ExternalActionTokenMismatch):
        store.complete(
            action_id=first.action.action_id,
            execution_token="forged-token-" * 4,
            outcome={"ok": True},
            provider_receipt={"request_id": "provider-1"},
            completed_by="worker-1",
            now=T0 + timedelta(seconds=2),
        )

    completed = store.complete(
        action_id=first.action.action_id,
        execution_token=first.execution_token,
        outcome={"ok": True},
        provider_receipt={"request_id": "provider-1"},
        completed_by="worker-1",
        now=T0 + timedelta(seconds=2),
    )
    exact = store.complete(
        action_id=first.action.action_id,
        execution_token=first.execution_token,
        outcome={"ok": True},
        provider_receipt={"request_id": "provider-1"},
        completed_by="worker-replay",
        now=T0 + timedelta(days=1),
    )
    assert completed.replayed is False
    assert exact.replayed is True
    assert exact.receipt == completed.receipt
    assert exact.action.status is ExternalActionStatus.COMPLETED
    with pytest.raises(InvalidExternalActionTransition, match="different receipt"):
        store.complete(
            action_id=first.action.action_id,
            execution_token=first.execution_token,
            outcome={"ok": False},
            provider_receipt={"request_id": "provider-1"},
            completed_by="worker-1",
        )


def test_external_action_faults_rollback_local_state_and_stale_claim_never_reissues():
    run_id = create_run("F11-S2 external recovery", domain="materials")
    spec = _action_spec(run_id, "recover", ttl=2)
    store = OneTimeExternalActionStore()

    def claim_domain(session, action_id, _claimed_at):
        session.add(
            Decision(
                run_id=run_id,
                stage_to="outward_claim",
                rationale=action_id,
                actor="pytest",
            )
        )

    def crash_claim(point, _session):
        if point == "after_domain_claim_before_event":
            raise RuntimeError("claim crash")

    with pytest.raises(RuntimeError, match="claim crash"):
        store.claim(
            spec,
            claim_owner="worker-crash",
            now=T0,
            on_claim=claim_domain,
            fault_hook=crash_claim,
        )
    with session_scope() as session:
        assert session.get(OneTimeExternalActionRecord, spec.action_id) is None
        assert session.scalar(select(Decision).where(Decision.rationale == spec.action_id)) is None

    claim = store.claim(
        spec,
        claim_owner="worker-live",
        now=T0,
        on_claim=claim_domain,
    )
    assert claim.execution_token
    recovered = store.recover_stale(now=T0 + timedelta(seconds=3))
    assert spec.action_id in recovered.action_ids
    replay = store.claim(spec, claim_owner="replacement", now=T0 + timedelta(seconds=4))
    assert replay.execution_token is None
    assert replay.action.status is ExternalActionStatus.RECONCILIATION_REQUIRED

    def complete_domain(session, receipt):
        session.add(
            Decision(
                run_id=run_id,
                stage_to="outward_result",
                rationale=receipt.receipt_sha256,
                actor="pytest",
            )
        )

    def crash_completion(point, _session):
        if point == "after_domain_result_before_commit":
            raise RuntimeError("receipt crash")

    with pytest.raises(RuntimeError, match="receipt crash"):
        store.complete(
            action_id=spec.action_id,
            execution_token=claim.execution_token,
            outcome={"result": "negative"},
            provider_receipt={"request_id": "provider-recover"},
            completed_by="reconciler",
            now=T0 + timedelta(seconds=5),
            on_complete=complete_domain,
            fault_hook=crash_completion,
        )
    assert store.get(spec.action_id).status is ExternalActionStatus.RECONCILIATION_REQUIRED

    completed = store.complete(
        action_id=spec.action_id,
        execution_token=claim.execution_token,
        outcome={"result": "negative"},
        provider_receipt={"request_id": "provider-recover"},
        completed_by="reconciler",
        now=T0 + timedelta(seconds=6),
        on_complete=complete_domain,
    )
    assert completed.action.status is ExternalActionStatus.COMPLETED
    with session_scope() as session:
        result_rows = session.scalars(
            select(Decision).where(Decision.rationale == completed.receipt.receipt_sha256)
        ).all()
        assert len(result_rows) == 1


def test_concurrent_external_claim_has_exactly_one_raw_token():
    run_id = create_run("F11-S2 concurrent external claim", domain="materials")
    spec = _action_spec(run_id, "concurrent")
    barrier = Barrier(2)

    def invoke(worker: str):
        barrier.wait()
        return OneTimeExternalActionStore().claim(spec, claim_owner=worker, now=T0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(invoke, ("worker-a", "worker-b")))
    assert sum(claim.execution_token is not None for claim in claims) == 1
    assert sum(claim.created for claim in claims) == 1


def test_database_triggers_reject_command_action_intent_and_receipt_mutation():
    run_id = create_run("F11-S2 receipt immutability", domain="materials")
    spec = _command(run_id, "immutable")
    command = ScientificTransitionStore().execute(
        spec,
        _decision_mutation(spec, []),
        now=T0,
    )
    with pytest.raises(DBAPIError, match="immutable F11 scientific receipt"):
        with session_scope() as session:
            session.execute(
                update(ScientificCommandRecord)
                .where(ScientificCommandRecord.command_id == command.command_id)
                .values(result_sha256="0" * 64)
            )

    action_spec = _action_spec(run_id, "immutable")
    claim = OneTimeExternalActionStore().claim(
        action_spec,
        claim_owner="worker",
        now=T0,
    )
    with pytest.raises(DBAPIError, match="action identity cannot be mutated"):
        with session_scope() as session:
            session.execute(
                update(OneTimeExternalActionRecord)
                .where(OneTimeExternalActionRecord.action_id == action_spec.action_id)
                .values(execution_token_sha256="0" * 64)
            )
    completion = OneTimeExternalActionStore().complete(
        action_id=action_spec.action_id,
        execution_token=claim.execution_token,
        outcome={"ok": True},
        provider_receipt={"request_id": "immutable"},
        completed_by="worker",
        now=T0 + timedelta(seconds=1),
    )
    with pytest.raises(DBAPIError, match="immutable F11 scientific receipt"):
        with session_scope() as session:
            session.execute(
                update(ExternalActionReceiptRecord)
                .where(
                    ExternalActionReceiptRecord.receipt_sha256 == completion.receipt.receipt_sha256
                )
                .values(outcome_sha256="0" * 64)
            )
    with pytest.raises(DBAPIError, match="action intent cannot be deleted"):
        with session_scope() as session:
            session.execute(
                delete(OneTimeExternalActionRecord).where(
                    OneTimeExternalActionRecord.action_id == action_spec.action_id
                )
            )
