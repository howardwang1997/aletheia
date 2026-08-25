"""Database-independent checks for observation event store wiring."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Session

from aletheia.research_store import store as store_module
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    ResearchStateGraph,
)
from aletheia.research_kernel.schemas import (
    ActionKind,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ObservationIncorporatedPayload,
    ResearchActionProposal,
    ResearchEvent,
)
from aletheia.research_store.persistence import ResearchKernelEventRecord
from aletheia.research_store.store import (
    ResearchKernelOutboxIdentity,
    ResearchKernelOutboxItem,
    ResearchKernelStore,
    ResearchOutboxConflict,
    ResearchStoreInvariantError,
    _event_from_row,
    _resolved_action,
)

_QUEST_ID = "qst_" + "1" * 32
_BRANCH_ID = "rbr_" + "2" * 32
_EVENT_SHA256 = "3" * 64


class _RowSession(Session):
    def __init__(self, row: object) -> None:
        self.row = row
        self.flush_count = 0

    def scalar(self, _statement: object) -> object:
        return self.row

    def flush(self, _objects: object = None) -> None:
        self.flush_count += 1


def _action() -> ResearchActionProposal:
    return ResearchActionProposal(
        action_id="action:measurement",
        quest_id=_QUEST_ID,
        charter_ref=KernelObjectRef(
            object_kind=KernelObjectKind.CHARTER,
            object_id="charter:measurement",
            object_sha256="4" * 64,
            quest_id=_QUEST_ID,
        ),
        question_ref=KernelObjectRef(
            object_kind=KernelObjectKind.QUESTION,
            object_id="question:measurement",
            object_sha256="5" * 64,
            quest_id=_QUEST_ID,
        ),
        basis_tail_event_sha256="6" * 64,
        kind=ActionKind.CONTINUE,
        epistemic_purpose="Measure the bounded scientific outcome.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        cost_receipt_sha256="7" * 64,
        risk_receipt_sha256="8" * 64,
        requested_authority_class="analysis",
        proposed_by_principal_id="model:planner",
        proposed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def _payload() -> ObservationIncorporatedPayload:
    return ObservationIncorporatedPayload(
        branch_id=_BRANCH_ID,
        action_id="action:measurement",
        scientific_slot_id="sos_" + "9" * 32,
        committed_admission_sha256="a" * 64,
        scientific_observation_sha256="b" * 64,
        outcome="inconclusive",
        source_world_model_sha256="c" * 64,
    )


def _authorized_state(action: ResearchActionProposal) -> ResearchStateGraph:
    return ResearchStateGraph(
        quest_id=_QUEST_ID,
        stream_version=4,
        tail_event_sha256=_EVENT_SHA256,
        actions=(
            ActionSnapshot(
                action_ref=action.object_ref,
                branch_id=_BRANCH_ID,
                kind=action.kind,
                lifecycle=ActionLifecycle.AUTHORIZED,
                proposed_event_sha256="d" * 64,
                decided_event_sha256="e" * 64,
            ),
        ),
    )


def test_event_type_constraint_allows_observation_incorporated() -> None:
    constraint = next(
        item
        for item in ResearchKernelEventRecord.__table__.constraints
        if isinstance(item, CheckConstraint) and item.name == "ck_research_kernel_events_type"
    )

    assert "'observation_incorporated'" in str(constraint.sqltext)


def test_observation_resolves_exact_admitted_action_for_commit_and_replay() -> None:
    action = _action()

    resolved = _resolved_action(
        _authorized_state(action),
        _payload(),
        {action.object_sha256: action},
    )

    assert resolved == action


def test_observation_action_resolution_fails_without_exact_cas_custody() -> None:
    action = _action()

    with pytest.raises(ResearchStoreInvariantError, match="exact CAS custody"):
        _resolved_action(_authorized_state(action), _payload(), {})


def test_persisted_observation_event_round_trips_without_an_admitted_object() -> None:
    event = ResearchEvent(
        quest_id=_QUEST_ID,
        sequence=5,
        parent_event_sha256=_EVENT_SHA256,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=_payload(),
        command_sha256="f" * 64,
        principal_id="agent:operator",
        authorization_receipt_sha256="0" * 64,
        committed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    row = ResearchKernelEventRecord(
        event_sha256=event.event_sha256,
        event_id=event.event_id,
        quest_id=event.quest_id,
        sequence=event.sequence,
        parent_sequence=event.sequence - 1,
        parent_event_sha256=event.parent_event_sha256,
        event_schema_version=event.event_schema_version,
        reducer_version=event.reducer_version,
        event_type=event.event_type.value,
        event_json=event.model_dump(mode="json"),
        command_id="rkc_" + "1" * 32,
        command_sha256=event.command_sha256,
        principal_id=event.principal_id,
        authorization_receipt_sha256=event.authorization_receipt_sha256,
        admitted_object_sha256=None,
        admitted_object_kind=None,
        admitted_object_id=None,
        committed_at=event.committed_at,
    )

    assert _event_from_row(row) == event


def _outbox_item(*, event_sha256: str = "a" * 64) -> ResearchKernelOutboxItem:
    observed_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    return ResearchKernelOutboxItem(
        outbox_id=f"rko_{event_sha256[:32]}",
        quest_id=_QUEST_ID,
        sequence=5,
        event_sha256=event_sha256,
        delivery_key=f"{_QUEST_ID}:5",
        payload_sha256=event_sha256,
        delivery_status="pending",
        delivery_attempts=0,
        available_at=observed_at,
        created_at=observed_at,
    )


def test_outbox_identity_is_closed_and_exact() -> None:
    item = _outbox_item()

    assert item.identity == ResearchKernelOutboxIdentity(
        outbox_id=item.outbox_id,
        quest_id=item.quest_id,
        sequence=item.sequence,
        event_sha256=item.event_sha256,
        delivery_key=item.delivery_key,
        payload_sha256=item.payload_sha256,
    )
    with pytest.raises(ValueError, match="payload identity"):
        ResearchKernelOutboxIdentity(
            outbox_id=item.outbox_id,
            quest_id=item.quest_id,
            sequence=item.sequence,
            event_sha256=item.event_sha256,
            delivery_key=item.delivery_key,
            payload_sha256="b" * 64,
        )


def test_mark_outbox_published_is_exact_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _outbox_item()
    row = store_module._ResearchKernelOutboxRecord(**item.model_dump(mode="python"))
    session = _RowSession(row)
    store = object.__new__(ResearchKernelStore)
    published_at = datetime(2026, 8, 25, 0, 0, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "_transaction_time", lambda _session: published_at)

    published = store.mark_outbox_published_in_session(session, item)
    exact_retry = store.mark_outbox_published_in_session(session, item.identity)

    assert published.delivery_status == "published"
    assert published.delivery_attempts == 1
    assert published.last_attempt_at == published_at
    assert published.published_at == published_at
    assert exact_retry == published
    assert session.flush_count == 1


def test_mark_outbox_published_rejects_a_colliding_wrong_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _outbox_item()
    row = store_module._ResearchKernelOutboxRecord(**item.model_dump(mode="python"))
    session = _RowSession(row)
    store = object.__new__(ResearchKernelStore)
    monkeypatch.setattr(
        store_module,
        "_transaction_time",
        lambda _session: datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    colliding_event_sha256 = item.event_sha256[:32] + "b" * 32
    wrong = _outbox_item(event_sha256=colliding_event_sha256).identity

    with pytest.raises(ResearchOutboxConflict, match="immutable identity"):
        store.mark_outbox_published_in_session(session, wrong)

    assert row.delivery_status == "pending"
    assert session.flush_count == 0
