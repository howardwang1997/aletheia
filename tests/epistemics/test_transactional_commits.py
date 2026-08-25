"""F11-S2 prediction -> validation -> belief transaction boundary acceptance."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

import aletheia.epistemics as e
from aletheia.db import create_all, session_scope
from aletheia.epistemics.persistence import EpistemicWorldModelSnapshotRecord
from aletheia.jobs.persistence import ScientificCommandRecord
from aletheia.memory.ledger import Event, Run

from .f9s7_fixtures import build_f9s7_fixture
from .f9s8_fixtures import build_f9s8_source


@pytest.fixture(scope="module")
def transactional_fixture(tmp_path_factory) -> dict:
    root: Path = tmp_path_factory.mktemp("f11s2-epistemic-transactions")
    source = build_f9s8_source(root / "source")
    parts = build_f9s7_fixture(source, root / "pipeline")
    create_all()
    run_id = source.world_model_snapshot.question.run_id
    with session_scope() as session:
        if session.get(Run, run_id) is None:
            session.add(
                Run(
                    id=run_id,
                    goal="F11-S2 separate scientific transaction boundaries",
                    status="active",
                )
            )
    return {"root": root, "source": source, "parts": parts, "run_id": run_id}


def test_prediction_validation_and_belief_are_three_exact_transaction_boundaries(
    transactional_fixture,
) -> None:
    parts = transactional_fixture["parts"]
    selected = parts["selected_candidate"].committed_prediction
    validation = parts["committed_validation"]
    update = parts["committed_updates"][0]
    updated_snapshot = update.campaign.updated_world_model_snapshot
    assert updated_snapshot is not None

    prediction_commit = e.commit_prediction_transactionally(
        archive=parts["prediction_archive"],
        campaign=selected.campaign,
        committed_at=selected.committed_at,
        principal="f11s2-prediction-committer",
        source_event_key=f"f11s2:prediction:{selected.campaign.campaign_sha256}",
    )
    assert prediction_commit.committed == selected
    assert prediction_commit.transaction.created is True
    with session_scope() as session:
        assert (
            session.get(
                EpistemicWorldModelSnapshotRecord,
                updated_snapshot.snapshot_sha256,
            )
            is None
        )

    validation_commit = e.commit_observation_validation_transactionally(
        archive=parts["validation_archive"],
        campaign=validation.campaign,
        committed_at=validation.committed_at,
        principal="f11s2-validation-committer",
        source_event_key=f"f11s2:validation:{validation.campaign.campaign_sha256}",
    )
    assert validation_commit.committed == validation
    assert validation_commit.transaction.created is True
    with session_scope() as session:
        # Validation is a separate fact; it cannot silently advance the posterior.
        assert (
            session.get(
                EpistemicWorldModelSnapshotRecord,
                updated_snapshot.snapshot_sha256,
            )
            is None
        )

    belief_commit = e.commit_world_belief_update_transactionally(
        archive=parts["update_archive"],
        campaign=update.campaign,
        committed_at=update.committed_at,
        principal="f11s2-belief-committer",
        source_event_key=f"f11s2:belief:{update.campaign.campaign_sha256}",
    )
    assert belief_commit.committed == update
    assert belief_commit.transaction.created is True
    with session_scope() as session:
        assert (
            session.get(
                EpistemicWorldModelSnapshotRecord,
                updated_snapshot.snapshot_sha256,
            )
            is not None
        )
        command_ids = {
            prediction_commit.transaction.command_id,
            validation_commit.transaction.command_id,
            belief_commit.transaction.command_id,
        }
        rows = session.scalars(
            select(ScientificCommandRecord).where(
                ScientificCommandRecord.command_id.in_(command_ids)
            )
        ).all()
        assert {row.command_type for row in rows} == {
            "prediction.commit",
            "observation_validation.commit",
            "belief_update.commit",
        }
        assert all(row.status == "committed" and row.output_event_id for row in rows)
        events = session.scalars(
            select(Event).where(Event.id.in_([row.output_event_id for row in rows]))
        ).all()
        assert len(events) == 3
        assert all(event.event_key and event.event_sha256 for event in events)

    prediction_replay = e.commit_prediction_transactionally(
        archive=parts["prediction_archive"],
        campaign=selected.campaign,
        committed_at=selected.committed_at,
        principal="f11s2-prediction-committer",
        source_event_key=f"f11s2:prediction:{selected.campaign.campaign_sha256}",
    )
    validation_replay = e.commit_observation_validation_transactionally(
        archive=parts["validation_archive"],
        campaign=validation.campaign,
        committed_at=validation.committed_at,
        principal="f11s2-validation-committer",
        source_event_key=f"f11s2:validation:{validation.campaign.campaign_sha256}",
    )
    belief_replay = e.commit_world_belief_update_transactionally(
        archive=parts["update_archive"],
        campaign=update.campaign,
        committed_at=update.committed_at,
        principal="f11s2-belief-committer",
        source_event_key=f"f11s2:belief:{update.campaign.campaign_sha256}",
    )
    assert not prediction_replay.transaction.created
    assert not validation_replay.transaction.created
    assert not belief_replay.transaction.created
    assert prediction_replay.transaction.output_event_id == (
        prediction_commit.transaction.output_event_id
    )
    assert validation_replay.transaction.output_event_id == (
        validation_commit.transaction.output_event_id
    )
    assert belief_replay.transaction.output_event_id == belief_commit.transaction.output_event_id
