from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aletheia.observations.scientific_bridge import ScientificObservationOutcome
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    derive_continuation_v2,
)
from aletheia.research_kernel.schemas import ActionKind

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import fixture_by_name  # noqa: E402


def _world_model():
    model = fixture_by_name("structural_intervention_simulation").request.protocol.world_model
    assert model is not None
    return model


def _observation(*, outcome: ScientificObservationOutcome = ScientificObservationOutcome.NEGATIVE):
    prediction = _world_model().predictions[0]
    return ScientificObservationProjection(
        scientific_slot_id="sos_" + "1" * 32,
        committed_admission_sha256="2" * 64,
        scientific_observation_sha256="3" * 64,
        source_world_model_sha256=_world_model().world_model_sha256,
        outcome=outcome,
        observable_spec_sha256=prediction.observable_spec_sha256,
        measurement_protocol_sha256=prediction.measurement_protocol_sha256,
        outcome_space_sha256=prediction.outcome_space_sha256,
        observed_outcome_sha256="4" * 64,
    )


def _assessments(*fits: PredictionFit):
    predictions = sorted(_world_model().predictions, key=lambda item: item.hypothesis_sha256)
    return tuple(
        HypothesisPredictionAssessment(
            hypothesis_sha256=prediction.hypothesis_sha256,
            prediction_sha256=prediction.prediction_sha256,
            prediction_fit=fit,
            fit_rule_sha256="5" * 64,
            assessment_artifact_sha256=f"{index + 6:x}" * 64,
        )
        for index, (prediction, fit) in enumerate(zip(predictions, fits, strict=True))
    )


def test_all_model_miss_requires_hypothesis_fork() -> None:
    receipt = derive_continuation_v2(
        world_model=_world_model(),
        observation=_observation(),
        assessments=_assessments(PredictionFit.OUT_OF_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
    )
    assert receipt.disposition is ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED
    assert receipt.proposed_action_kind is ActionKind.FORK
    assert receipt.reason_codes == ("all_active_hypotheses_out_of_support",)
    assert receipt.legacy_run_synthesized is False
    assert receipt.legacy_optimize_used is False


def test_negative_supporting_an_alternative_does_not_imply_fork() -> None:
    receipt = derive_continuation_v2(
        world_model=_world_model(),
        observation=_observation(outcome=ScientificObservationOutcome.NEGATIVE),
        assessments=_assessments(PredictionFit.OUT_OF_SUPPORT, PredictionFit.IN_SUPPORT),
    )
    assert receipt.disposition is ContinuationDisposition.READY
    assert receipt.proposed_action_kind is ActionKind.CONTINUE


@pytest.mark.parametrize(
    "assessments",
    (
        (),
        _assessments(PredictionFit.INDETERMINATE, PredictionFit.IN_SUPPORT),
    ),
)
def test_missing_or_indeterminate_fit_requests_observable_redesign(assessments) -> None:
    receipt = derive_continuation_v2(
        world_model=_world_model(),
        observation=_observation(),
        assessments=assessments,
    )
    assert receipt.disposition is ContinuationDisposition.REDESIGN_OBSERVABLE
    assert receipt.proposed_action_kind is ActionKind.REFINE


def test_assessment_cannot_escape_the_frozen_prediction_context() -> None:
    model = _world_model()
    assessments = list(_assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT))
    foreign_prediction = next(
        item
        for item in model.predictions
        if item.hypothesis_sha256 != assessments[0].hypothesis_sha256
    )
    assessments[0] = assessments[0].model_copy(
        update={"prediction_sha256": foreign_prediction.prediction_sha256}
    )
    with pytest.raises(ValueError, match="changed the prediction hypothesis"):
        derive_continuation_v2(
            world_model=model,
            observation=_observation(),
            assessments=tuple(assessments),
        )


def test_observation_cannot_be_rebound_to_another_world_model() -> None:
    observation = _observation().model_copy(update={"source_world_model_sha256": "f" * 64})
    with pytest.raises(ValueError, match="admitted observation source"):
        derive_continuation_v2(
            world_model=_world_model(),
            observation=observation,
            assessments=_assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        )


def test_assessment_order_and_receipt_hash_are_canonical() -> None:
    assessments = _assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT)
    first = derive_continuation_v2(
        world_model=_world_model(), observation=_observation(), assessments=assessments
    )
    second = derive_continuation_v2(
        world_model=_world_model(), observation=_observation(), assessments=assessments
    )
    assert first.receipt_sha256 == second.receipt_sha256
    with pytest.raises(ValueError, match="canonically ordered"):
        derive_continuation_v2(
            world_model=_world_model(),
            observation=_observation(),
            assessments=tuple(reversed(assessments)),
        )
