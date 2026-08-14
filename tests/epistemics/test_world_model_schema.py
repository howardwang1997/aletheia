from __future__ import annotations

from pydantic import ValidationError
import pytest

import aletheia.epistemics as e

from .f9s1_fixtures import build_world_model, revise_primary_hypothesis


RUN_ID = "a" * 32


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def test_snapshot_closes_three_competing_hypotheses_and_exact_beliefs() -> None:
    snapshot = build_world_model(RUN_ID)

    assert len(snapshot.hypotheses) == 3
    assert {item.role for item in snapshot.hypotheses} == {
        e.HypothesisRole.NULL,
        e.HypothesisRole.PRIMARY,
        e.HypothesisRole.ALTERNATIVE,
    }
    assert {item.hypothesis_id for item in snapshot.hypotheses} == {
        item.hypothesis_id for item in snapshot.belief_state.hypotheses
    }
    assert sum(item.probability for item in snapshot.belief_state.hypotheses) == pytest.approx(1.0)
    assert len(snapshot.snapshot_sha256) == 64


def test_epistemic_objects_are_frozen() -> None:
    snapshot = build_world_model(RUN_ID)

    with pytest.raises(ValidationError, match="frozen"):
        snapshot.question.statement = "post hoc mutation"
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.belief_state.hypotheses = ()


def test_revision_retains_stable_lineages_and_names_exact_parents() -> None:
    initial = build_world_model(RUN_ID)
    revised = revise_primary_hypothesis(initial)
    old = next(item for item in initial.hypotheses if item.role is e.HypothesisRole.PRIMARY)
    new = next(item for item in revised.hypotheses if item.role is e.HypothesisRole.PRIMARY)

    assert new.hypothesis_id == old.hypothesis_id
    assert new.version == 2
    assert new.parent_hypothesis_sha256 == old.hypothesis_sha256
    assert new.hypothesis_sha256 != old.hypothesis_sha256
    assert revised.belief_state.parent_belief_state_sha256 == initial.belief_state.belief_state_sha256
    assert revised.snapshot_sha256 != initial.snapshot_sha256


@pytest.mark.parametrize(
    ("model_type", "model", "updates", "message"),
    [
        (
            e.ResearchQuestion,
            build_world_model(RUN_ID).question,
            {"version": 2},
            "requires its exact parent",
        ),
        (
            e.HypothesisVersion,
            build_world_model(RUN_ID).hypotheses[0],
            {"parent_hypothesis_sha256": "f" * 64},
            "initial hypothesis version cannot have a parent",
        ),
        (
            e.BeliefState,
            build_world_model(RUN_ID).belief_state,
            {
                "version": 2,
                "parent_belief_state_sha256": None,
            },
            "requires its exact parent",
        ),
    ],
)
def test_version_shape_rejects_missing_or_spurious_parent(
    model_type, model, updates, message
) -> None:
    with pytest.raises(ValidationError, match=message):
        _revalidate(model_type, model, **updates)


def test_world_model_rejects_single_story_or_missing_alternative() -> None:
    snapshot = build_world_model(RUN_ID)
    only_two = tuple(
        item for item in snapshot.hypotheses if item.role is not e.HypothesisRole.ALTERNATIVE
    )

    with pytest.raises(ValidationError, match="at least 3 items"):
        _revalidate(e.WorldModelSnapshot, snapshot, hypotheses=only_two)


def test_world_model_rejects_assumption_rebound_to_wrong_hypothesis_version() -> None:
    snapshot = build_world_model(RUN_ID)
    assumption = snapshot.assumptions[0]
    wrong = next(
        item for item in snapshot.hypotheses if item.hypothesis_id != assumption.hypothesis_id
    )
    forged = _revalidate(
        e.Assumption,
        assumption,
        hypothesis_version_sha256=wrong.hypothesis_sha256,
    )

    with pytest.raises(ValidationError, match="exact hypothesis version"):
        _revalidate(
            e.WorldModelSnapshot,
            snapshot,
            assumptions=(forged, *snapshot.assumptions[1:]),
        )


def test_belief_state_rejects_non_normalized_or_noncanonical_probabilities() -> None:
    belief = build_world_model(RUN_ID).belief_state
    changed = list(belief.hypotheses)
    changed[0] = _revalidate(e.HypothesisBelief, changed[0], probability=0.9)
    with pytest.raises(ValidationError, match="sum to one"):
        _revalidate(e.BeliefState, belief, hypotheses=tuple(changed))
    with pytest.raises(ValidationError, match="canonical"):
        _revalidate(e.BeliefState, belief, hypotheses=tuple(reversed(belief.hypotheses)))


def test_observation_update_requires_both_observation_and_likelihood_receipts() -> None:
    belief = build_world_model(RUN_ID).belief_state
    common = {
        "version": 2,
        "parent_belief_state_sha256": belief.belief_state_sha256,
        "update_kind": e.BeliefUpdateKind.VALIDATED_OBSERVATION,
    }
    with pytest.raises(ValidationError, match="observation and likelihood receipts"):
        _revalidate(e.BeliefState, belief, **common)
    with pytest.raises(ValidationError, match="observation and likelihood receipts"):
        _revalidate(
            e.BeliefState,
            belief,
            source_observation_receipt_sha256="1" * 64,
        )

    observed = _revalidate(
        e.BeliefState,
        belief,
        **common,
        source_observation_receipt_sha256="1" * 64,
        likelihood_model_sha256="2" * 64,
    )
    assert observed.update_kind is e.BeliefUpdateKind.VALIDATED_OBSERVATION


def test_snapshot_member_order_is_canonical_and_hash_stable() -> None:
    snapshot = build_world_model(RUN_ID)
    with pytest.raises(ValidationError, match="canonical lineage/version order"):
        _revalidate(
            e.WorldModelSnapshot,
            snapshot,
            assumptions=tuple(reversed(snapshot.assumptions)),
        )
    with pytest.raises(ValidationError, match="canonical lineage/version order"):
        _revalidate(
            e.WorldModelSnapshot,
            snapshot,
            predictions=tuple(reversed(snapshot.predictions)),
        )


def test_prediction_must_name_a_real_discriminating_outcome() -> None:
    prediction = build_world_model(RUN_ID).predictions[0]
    with pytest.raises(ValidationError, match="member of its outcome space"):
        _revalidate(e.Prediction, prediction, expected_outcome="not-preregistered")
    with pytest.raises(ValidationError, match="from itself"):
        _revalidate(
            e.Prediction,
            prediction,
            discriminates_from_hypothesis_ids=(prediction.hypothesis_id,),
        )
