from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError

import aletheia.epistemics as e
import aletheia.epistemics.continuation as continuation_service
from aletheia.db import create_all, engine, session_scope
from aletheia.epistemics.persistence import (
    EpistemicWorldModelSnapshotRecord,
    EpistemicWorldModelTransitionRecord,
)
from aletheia.knowledge.response_archive import (
    ContentAddressedResponseArchive,
    ResponseArchiveError,
)
from aletheia.memory.ledger import Event, Run
from aletheia.schema_migrations import schema_diffs
from aletheia.scheduler.k3_continuation import authorize_k3_next_round

from .f9s2_fixtures import StepClock, revalidate
from .f9s3_fixtures import build_f9s3_fixture
from .f9s8_fixtures import build_f9s8_fixture, build_f9s8_source


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    return build_f9s8_source(tmp_path_factory.mktemp("f9s8-source"))


@pytest.fixture(scope="module")
def continuation_fixture(source_fixture, tmp_path_factory):
    return build_f9s8_fixture(
        source_fixture,
        tmp_path_factory.mktemp("f9s8-continuation"),
    )


@pytest.fixture(scope="module")
def persisted_continuation(continuation_fixture):
    create_all()
    transition = continuation_fixture["transition"]
    _ensure_run(transition)
    receipt = e.persist_world_model_transition(transition)
    return {**continuation_fixture, "store_receipt": receipt}


def _ensure_run(transition: e.WorldModelTransition) -> None:
    run_id = transition.source_world_model_snapshot.question.run_id
    with session_scope() as session:
        if session.get(Run, run_id) is None:
            session.add(
                Run(
                    id=run_id,
                    goal="F9-S8 transactional next-round continuation test",
                    status="active",
                )
            )


def _stop_acceptance(parts, root):
    old_evidence = parts["evidence_ledger"]
    stopped_decision = revalidate(
        e.K3TerminalDecision,
        old_evidence.terminal_decision,
        action=e.K3TerminalAction.STOP_AND_ARCHIVE,
    )
    evidence = revalidate(
        e.K3EvidenceLedger,
        old_evidence,
        terminal_decision=stopped_decision,
    )
    evidence_archive = ContentAddressedResponseArchive(root / "evidence")
    committed_evidence = e.commit_k3_evidence_ledger(
        archive=evidence_archive,
        evidence=evidence,
        committed_at=parts["committed_evidence"].committed_at,
    )
    old_request = parts["acceptance_request"]
    request = e.build_k3_acceptance_request(
        acceptance_id="f9s8-stop-acceptance-request-v1",
        rounds=old_request.rounds,
        committed_evidence_ledger=committed_evidence,
        scorer_manifest=parts["scorer_manifest"],
        policy=parts["acceptance_policy"],
        selection_archive_custody_sha256=old_request.selection_archive_custody_sha256,
        validation_archive_custody_sha256=old_request.validation_archive_custody_sha256,
        update_archive_custody_sha256=old_request.update_archive_custody_sha256,
        evidence_archive_custody_sha256=old_request.evidence_archive_custody_sha256,
        issued_at=old_request.issued_at,
    )
    campaign = e.run_k3_acceptance(
        campaign_id="campaign:f9s8:stop-acceptance",
        policy=parts["acceptance_policy"],
        scorer_manifest=parts["scorer_manifest"],
        request=request,
        selection_archive=parts["selection_archive"],
        validation_archive=parts["validation_archive"],
        update_archive=parts["update_archive"],
        evidence_archive=evidence_archive,
        clock=StepClock(request.issued_at + timedelta(minutes=1)),
    )
    archive = ContentAddressedResponseArchive(root / "acceptance")
    committed = e.commit_k3_acceptance_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=campaign.generated_at + timedelta(minutes=1),
    )
    return committed, archive


def test_f9s8_migration_matches_orm_and_transition_table_is_immutable() -> None:
    create_all()
    with engine().connect() as connection:
        assert "epistemic_world_model_transitions" in inspect(connection).get_table_names()
        assert schema_diffs(connection) == []


def test_transition_closes_narrow_revisions_over_every_version_binding(
    continuation_fixture,
) -> None:
    transition = continuation_fixture["transition"]
    posterior = transition.posterior_world_model_snapshot
    next_snapshot = transition.next_round_world_model_snapshot

    assert transition.disposition is e.WorldModelTransitionDisposition.READY_NEXT_ROUND
    assert next_snapshot is not None
    assert next_snapshot.belief_state.parent_belief_state_sha256 == (
        posterior.belief_state.belief_state_sha256
    )
    assert next_snapshot.belief_state.update_kind is e.BeliefUpdateKind.HYPOTHESIS_REVISION
    revised_ids = {
        item.revised_hypothesis.hypothesis_id for item in transition.revision_materializations
    }
    assert revised_ids
    for hypothesis_id in revised_ids:
        hypothesis = next(
            item for item in next_snapshot.hypotheses if item.hypothesis_id == hypothesis_id
        )
        assert hypothesis.lifecycle is e.HypothesisLifecycle.NARROWED
        assert all(
            item.hypothesis_version_sha256 == hypothesis.hypothesis_sha256
            for item in next_snapshot.assumptions
            if item.hypothesis_id == hypothesis_id
        )
        assert all(
            item.hypothesis_version_sha256 == hypothesis.hypothesis_sha256
            for item in next_snapshot.predictions
            if item.hypothesis_id == hypothesis_id
        )


def test_transition_payload_rejects_a_detached_next_snapshot(continuation_fixture) -> None:
    transition = continuation_fixture["transition"]
    with pytest.raises(ValidationError, match="not mechanically derived"):
        revalidate(
            e.WorldModelTransition,
            transition,
            next_round_world_model_snapshot=transition.posterior_world_model_snapshot,
        )


def test_event_write_failure_rolls_back_transition_and_all_child_snapshots(
    continuation_fixture,
    monkeypatch,
) -> None:
    create_all()
    transition = continuation_fixture["transition"]
    _ensure_run(transition)

    def fail_event_write(**_values):
        raise RuntimeError("injected typed-event failure")

    monkeypatch.setattr(continuation_service, "Event", fail_event_write)
    with pytest.raises(RuntimeError, match="typed-event failure"):
        e.persist_world_model_transition(transition)

    snapshot_hashes = {
        transition.source_world_model_snapshot.snapshot_sha256,
        transition.posterior_world_model_snapshot.snapshot_sha256,
        transition.next_round_world_model_snapshot.snapshot_sha256,
    }
    with session_scope() as session:
        assert (
            session.get(EpistemicWorldModelTransitionRecord, transition.transition_sha256) is None
        )
        assert (
            session.scalars(
                select(EpistemicWorldModelSnapshotRecord).where(
                    EpistemicWorldModelSnapshotRecord.snapshot_sha256.in_(snapshot_hashes)
                )
            ).all()
            == []
        )


def test_atomic_transition_round_trip_is_exact_and_idempotent(
    persisted_continuation,
) -> None:
    transition = persisted_continuation["transition"]
    first = persisted_continuation["store_receipt"]
    second = e.persist_world_model_transition(transition)

    assert first.created is True
    assert second.created is False
    assert second.event_id == first.event_id
    assert e.get_world_model_transition(transition.transition_sha256) == transition
    with session_scope() as session:
        event = session.get(Event, first.event_id)
        assert event is not None
        projection = e.WorldModelTransitionEventProjection.model_validate(event.payload)
        assert projection.transition_sha256 == transition.transition_sha256
        assert projection.event_type == "f9_world_model_transition_committed"


def test_same_update_cannot_be_rebound_to_another_transition_identity(
    persisted_continuation,
) -> None:
    transition = persisted_continuation["transition"]
    rebound = e.build_world_model_transition(
        transition_id=(
            "f9s8-world-model-transition-rebound-"
            f"{transition.source_world_model_snapshot.question.run_id}"
        ),
        round_evidence=transition.round_evidence,
        revision_materializations=transition.revision_materializations,
        persistence_principal_sha256=transition.persistence_principal_sha256,
        persisted_at=transition.persisted_at,
    )
    with pytest.raises(e.ImmutableEpistemicConflict, match="already bound"):
        e.persist_world_model_transition(rebound)
    assert e.get_world_model_transition(transition.transition_sha256) == transition


def test_database_trigger_rejects_transition_mutation(persisted_continuation) -> None:
    transition = persisted_continuation["transition"]
    with pytest.raises(DBAPIError, match="immutable F9 epistemic row"):
        with session_scope() as session:
            session.execute(
                update(EpistemicWorldModelTransitionRecord)
                .where(
                    EpistemicWorldModelTransitionRecord.transition_sha256
                    == transition.transition_sha256
                )
                .values(disposition="hypothesis_set_fork_required")
            )


def test_k3_verdict_authorizes_exact_persisted_snapshot_for_second_causal_round(
    persisted_continuation,
) -> None:
    transition = persisted_continuation["transition"]
    acceptance = persisted_continuation["committed_acceptance"]
    authorized = e.load_authorized_next_round_source(
        transition_sha256=transition.transition_sha256,
        committed_acceptance=acceptance,
        acceptance_archive=persisted_continuation["acceptance_archive"],
        authorized_at=acceptance.committed_at + timedelta(minutes=1),
    )
    assert authorized.snapshot == transition.next_round_world_model_snapshot
    assert authorized.transition_sha256 == transition.transition_sha256
    assert authorized.acceptance_receipt_sha256 == acceptance.receipt_sha256
    assert (
        authorize_k3_next_round(
            transition_sha256=transition.transition_sha256,
            committed_acceptance=acceptance,
            acceptance_archive=persisted_continuation["acceptance_archive"],
            authorized_at=acceptance.committed_at + timedelta(minutes=1),
        )
        == authorized
    )

    base_campaign = persisted_continuation["source_campaign"].source_campaign
    second = build_f9s3_fixture(base_campaign, world_model_source=authorized)
    campaign = asyncio.run(
        e.run_causal_identification_audit(
            campaign_id="campaign:f9s8:second-causal-round",
            source_campaign=base_campaign,
            world_model_source=authorized,
            policy=second["policy"],
            request=second["request"],
            author=second["author"],
            reviewer=second["reviewer"],
            clock=second["clock"],
        )
    )
    assert campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert campaign.world_model_snapshot == authorized.snapshot
    assert campaign.request.world_model_source_sha256 == authorized.source_sha256
    assert {item.hypothesis_version_sha256 for item in campaign.request.hypothesis_bindings} == {
        item.hypothesis_sha256 for item in authorized.snapshot.hypotheses
    }


def test_stop_terminal_action_cannot_authorize_another_round(
    persisted_continuation,
    tmp_path,
) -> None:
    stopped, stopped_archive = _stop_acceptance(persisted_continuation, tmp_path / "stop")
    assert stopped.campaign.disposition is e.K3AcceptanceDisposition.ACCEPTED
    with pytest.raises(ValueError, match="does not authorize"):
        e.load_authorized_next_round_source(
            transition_sha256=persisted_continuation["transition"].transition_sha256,
            committed_acceptance=stopped,
            acceptance_archive=stopped_archive,
            authorized_at=stopped.committed_at + timedelta(minutes=1),
        )


def test_missing_acceptance_archive_blocks_next_round(
    persisted_continuation,
    tmp_path,
) -> None:
    acceptance = persisted_continuation["committed_acceptance"]
    with pytest.raises(ResponseArchiveError):
        e.load_authorized_next_round_source(
            transition_sha256=persisted_continuation["transition"].transition_sha256,
            committed_acceptance=acceptance,
            acceptance_archive=ContentAddressedResponseArchive(tmp_path / "empty-acceptance"),
            authorized_at=acceptance.committed_at + timedelta(minutes=1),
        )


def test_retirement_forces_hypothesis_set_fork_instead_of_reusing_stale_versions(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s8_fixture(
        source_fixture,
        tmp_path / "retirement",
        update_policy_updates={"retirement_posterior_ceiling": 0.4},
    )
    transition = parts["transition"]
    assert any(
        item.action is e.HypothesisRevisionAction.RETIRE
        for item in transition.committed_update.campaign.hypothesis_revisions
    )
    assert transition.disposition is (
        e.WorldModelTransitionDisposition.HYPOTHESIS_SET_FORK_REQUIRED
    )
    assert transition.next_round_world_model_snapshot is None


def test_transition_event_projection_is_unique(persisted_continuation) -> None:
    transition = persisted_continuation["transition"]
    with session_scope() as session:
        rows = session.scalars(
            select(Event).where(
                Event.type == "f9_world_model_transition_committed",
                Event.payload["transition_sha256"].astext == transition.transition_sha256,
            )
        ).all()
    assert len(rows) == 1
