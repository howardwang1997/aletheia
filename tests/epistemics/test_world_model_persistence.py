from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.epistemics.persistence import (
    EpistemicBeliefStateRecord,
    EpistemicHypothesisVersionRecord,
    EpistemicLineageError,
    EpistemicResearchQuestionRecord,
    EpistemicWorldModelSnapshotRecord,
    ImmutableEpistemicConflict,
    get_world_model_snapshot,
    list_legacy_k2_belief_compat,
    store_world_model_snapshot,
)
from aletheia.memory.service import create_run, get_credence, list_credences, upsert_credence
from aletheia.schema_migrations import schema_diffs

from .f9s1_fixtures import build_world_model, revise_primary_hypothesis


def _seed(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex}"


def _run() -> str:
    create_all()
    return create_run(goal="F9-S1 immutable world-model persistence")


def test_f9s1_migration_matches_orm_and_exposes_compatibility_view() -> None:
    create_all()
    expected = {
        "epistemic_research_questions",
        "epistemic_hypothesis_versions",
        "epistemic_assumptions",
        "epistemic_predictions",
        "epistemic_belief_states",
        "epistemic_belief_state_members",
        "epistemic_world_model_snapshots",
    }
    with engine().connect() as connection:
        inspector = inspect(connection)
        assert expected.issubset(inspector.get_table_names())
        assert "k2_belief_state_compat" in inspector.get_view_names()
        assert schema_diffs(connection) == []


def test_world_model_round_trip_is_exact_and_idempotent() -> None:
    run_id = _run()
    snapshot = build_world_model(run_id, identity_seed=_seed("roundtrip"))

    first = store_world_model_snapshot(snapshot)
    second = store_world_model_snapshot(snapshot)
    loaded = get_world_model_snapshot(snapshot.snapshot_sha256)

    assert first.created is True
    assert second.created is False
    assert first.snapshot_sha256 == snapshot.snapshot_sha256
    assert loaded == snapshot


def test_same_lineage_version_cannot_be_rebound_to_changed_content() -> None:
    run_id = _run()
    identity = _seed("conflict")
    initial = build_world_model(run_id, identity_seed=identity, content_variant="original")
    conflicting = build_world_model(run_id, identity_seed=identity, content_variant="changed")
    store_world_model_snapshot(initial)

    with pytest.raises(ImmutableEpistemicConflict, match="stable lineage/version"):
        store_world_model_snapshot(conflicting)
    assert get_world_model_snapshot(initial.snapshot_sha256) == initial


def test_exact_child_revision_preserves_both_historical_snapshots() -> None:
    run_id = _run()
    initial = build_world_model(run_id, identity_seed=_seed("revision"))
    revised = revise_primary_hypothesis(initial)
    store_world_model_snapshot(initial)
    store_world_model_snapshot(revised)

    assert get_world_model_snapshot(initial.snapshot_sha256) == initial
    assert get_world_model_snapshot(revised.snapshot_sha256) == revised
    primary_id = next(
        item.hypothesis_id for item in initial.hypotheses if item.role.value == "primary"
    )
    with session_scope() as session:
        versions = session.scalars(
            select(EpistemicHypothesisVersionRecord.version)
            .where(EpistemicHypothesisVersionRecord.hypothesis_id == primary_id)
            .order_by(EpistemicHypothesisVersionRecord.version)
        ).all()
    assert versions == [1, 2]


@pytest.mark.parametrize(
    "revised",
    [
        lambda initial: revise_primary_hypothesis(initial, version=3),
        lambda initial: revise_primary_hypothesis(initial, parent_override="f" * 64),
    ],
)
def test_revision_cannot_skip_or_invent_a_parent(revised) -> None:
    run_id = _run()
    initial = build_world_model(run_id, identity_seed=_seed("bad-parent"))
    store_world_model_snapshot(initial)

    with pytest.raises(EpistemicLineageError, match="parent|exactly one"):
        store_world_model_snapshot(revised(initial))


def test_database_triggers_reject_epistemic_update_and_delete() -> None:
    run_id = _run()
    snapshot = build_world_model(run_id, identity_seed=_seed("trigger"))
    store_world_model_snapshot(snapshot)
    statements = (
        update(EpistemicResearchQuestionRecord)
        .where(
            EpistemicResearchQuestionRecord.question_sha256
            == snapshot.question.question_sha256
        )
        .values(kind="descriptive"),
        delete(EpistemicWorldModelSnapshotRecord).where(
            EpistemicWorldModelSnapshotRecord.snapshot_sha256 == snapshot.snapshot_sha256
        ),
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="immutable F9 epistemic row"):
            with session_scope() as session:
                session.execute(statement)

    assert get_world_model_snapshot(snapshot.snapshot_sha256) == snapshot


def test_k2_compatibility_view_preserves_legacy_read_write_api_without_f9_backfill() -> None:
    run_id = _run()
    question_key = f"legacy-question-{uuid.uuid4().hex[:12]}"
    upsert_credence(run_id, question_key, alpha=3.0, beta=2.0, n_updates=3)

    projected = list_legacy_k2_belief_compat(run_id)
    assert len(projected) == 1
    assert projected[0].belief_lineage_id == f"k2::{run_id}::{question_key}"
    assert projected[0].probability_holds == pytest.approx(0.6)
    assert projected[0].representation == "legacy_k2_beta_bernoulli"
    assert get_credence(run_id, question_key) == {
        "question_key": question_key,
        "alpha": 3.0,
        "beta": 2.0,
        "n_updates": 3,
    }
    assert list_credences(run_id) == [get_credence(run_id, question_key)]
    with session_scope() as session:
        assert session.scalar(
            select(EpistemicBeliefStateRecord).where(
                EpistemicBeliefStateRecord.run_id == run_id
            )
        ) is None


def test_k2_compatibility_projection_rejects_writes_to_legacy_state() -> None:
    run_id = _run()
    question_key = f"legacy-read-only-{uuid.uuid4().hex[:12]}"
    upsert_credence(run_id, question_key, alpha=1.0, beta=1.0)

    with pytest.raises(DBAPIError, match="immutable F9 epistemic row"):
        with session_scope() as session:
            session.execute(
                text(
                    """
                    UPDATE k2_belief_state_compat
                    SET alpha = 99.0
                    WHERE run_id = :run_id AND question_key = :question_key
                    """
                ),
                {"run_id": run_id, "question_key": question_key},
            )
    assert get_credence(run_id, question_key)["alpha"] == 1.0
