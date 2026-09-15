"""Stop-policy contract tests (ARL-2 keystone B, PR B3)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.observations.scientific_bridge import ScientificObservationOutcome
from aletheia.protocols.world_models import (
    BeliefStateVersionV2,
    BeliefUpdateBasis,
    HypothesisBeliefV2,
)
from aletheia.research_controller.continuation import (
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
    ContinuationDisposition,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    derive_continuation_v2,
)
from aletheia.research_controller.continuation_step import (
    ContinuationAssessmentPolicyPin,
)
from aletheia.research_controller.continuation_stop import (
    StopGrounds,
    StopPolicyPin,
    StopReasonMappingV1,
    StopTriggerCode,
    count_rounds_observed,
    evaluate_stop_policy,
)
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
    StopReason,
    ActionKind,
)

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import fixture_by_name  # noqa: E402


def _world_model():
    model = fixture_by_name("structural_intervention_simulation").request.protocol.world_model
    assert model is not None
    return model


def _belief_model(*probabilities: float):
    model = _world_model()
    hypotheses = tuple(sorted(item.hypothesis_sha256 for item in model.hypotheses))
    belief = BeliefStateVersionV2(
        belief_id="blf_" + "a" * 32,
        version=1,
        graph_scope_sha256="b" * 64,
        hypothesis_beliefs=tuple(
            HypothesisBeliefV2(hypothesis_sha256=hypothesis, probability=probability)
            for hypothesis, probability in zip(hypotheses, probabilities, strict=True)
        ),
        update_basis=BeliefUpdateBasis.VALIDATED_OBSERVATION,
        source_observation_receipt_sha256="c" * 64,
        update_rule_sha256="d" * 64,
        authored_by_principal_id="principal.fixture",
        authored_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    return model.model_copy(update={"belief_state": belief})


def _observation(model=None):
    model = model or _world_model()
    prediction = model.predictions[0]
    return ScientificObservationProjection(
        scientific_slot_id="sos_" + "1" * 32,
        committed_admission_sha256="2" * 64,
        scientific_observation_sha256="3" * 64,
        source_world_model_sha256=model.world_model_sha256,
        outcome=ScientificObservationOutcome.NEGATIVE,
        observable_spec_sha256=prediction.observable_spec_sha256,
        measurement_protocol_sha256=prediction.measurement_protocol_sha256,
        outcome_space_sha256=prediction.outcome_space_sha256,
        observed_outcome_bin_id="outcome.negative",
        admissible_outcome_bin_ids=(
            "outcome.inconclusive",
            "outcome.negative",
            "outcome.positive",
        ),
        admission_policy_sha256="a" * 64,
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


def _stop_policy(
    *,
    max_rounds: int = 5,
    eig_floor: float = 0.05,
    lower: float = 0.25,
    upper: float = 0.75,
) -> StopPolicyPin:
    return StopPolicyPin(
        max_rounds=max_rounds,
        eig_floor=eig_floor,
        belief_decision_threshold_lower=lower,
        belief_decision_threshold_upper=upper,
        stop_reason_mappings=(
            StopReasonMappingV1(
                trigger=StopTriggerCode.BELIEF_DECISION_THRESHOLD,
                stop_reason=StopReason.EVIDENCE_THRESHOLD_REACHED,
                reopen_conditions=("reassess under a revised measurement protocol",),
            ),
            StopReasonMappingV1(
                trigger=StopTriggerCode.BELIEF_INDISTINGUISHABLE,
                stop_reason=StopReason.CURRENTLY_INDISTINGUISHABLE,
                reopen_conditions=("a new discriminator enters the frozen set",),
            ),
            StopReasonMappingV1(
                trigger=StopTriggerCode.MARGINAL_INFORMATION_FLOOR,
                stop_reason=StopReason.LOW_MARGINAL_INFORMATION_VALUE,
                reopen_conditions=("a new candidate protocol clears the floor",),
            ),
            StopReasonMappingV1(
                trigger=StopTriggerCode.ROUND_BUDGET_EXHAUSTED,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
                reopen_conditions=("the preregistered round budget is renegotiated",),
            ),
        ),
    )


def _derive_with_stop(*probabilities, fits, policy, rounds, eig=1.0, authorized=True):
    model = _belief_model(*probabilities)
    return derive_continuation_v2(
        world_model=model,
        observation=_observation(model),
        assessments=_assessments(*fits),
        stop_policy=policy,
        allowed_stop_policy_sha256s=(policy.policy_sha256,) if authorized else ("0" * 64,),
        rounds_observed=rounds,
        max_available_eig=eig,
    )


def _incorporated_event(sequence: int, branch_id: str) -> ResearchEvent:
    return ResearchEvent(
        quest_id="qst_" + "1" * 32,
        sequence=sequence,
        parent_event_sha256="e" * 64,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=ObservationIncorporatedPayload(
            branch_id=branch_id,
            action_id="act.fixture.observation",
            scientific_slot_id="sos_" + "1" * 32,
            committed_admission_sha256="2" * 64,
            scientific_observation_sha256="3" * 64,
            outcome="negative",
            source_world_model_sha256=_world_model().world_model_sha256,
        ),
        command_sha256="f" * 64,
        principal_id="principal.fixture",
        authorization_receipt_sha256="9" * 64,
        committed_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )


def test_legacy_derivation_is_bit_identical() -> None:
    # No stop arguments: the receipt bytes are the pre-stop form — no stop_grounds
    # key in the canonical dump, and explicit None stop args hash identically.
    assessments = _assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT)
    legacy = derive_continuation_v2(
        world_model=_world_model(), observation=_observation(), assessments=assessments
    )
    explicit_none = derive_continuation_v2(
        world_model=_world_model(),
        observation=_observation(),
        assessments=assessments,
        stop_policy=None,
        allowed_stop_policy_sha256s=None,
        rounds_observed=0,
        max_available_eig=None,
    )
    assert legacy.receipt_sha256 == explicit_none.receipt_sha256
    assert "stop_grounds" not in legacy.model_dump(mode="json", exclude_none=True)
    assert ContinuationReceipt.model_validate_json(legacy.model_dump_json()).receipt_sha256 == (
        legacy.receipt_sha256
    )


def test_budget_trigger_stops_with_mapped_reason() -> None:
    policy = _stop_policy(max_rounds=2)
    receipt = _derive_with_stop(
        0.6, 0.4, fits=(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        policy=policy, rounds=2,
    )
    assert receipt.disposition is ContinuationDisposition.STOP_REQUIRED
    assert receipt.proposed_action_kind is ActionKind.STOP
    assert receipt.reason_codes == ("stop_policy_round_budget_exhausted",)
    grounds = receipt.stop_grounds
    assert grounds is not None
    assert grounds.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert grounds.rounds_observed == 2
    assert grounds.stop_policy_sha256 == policy.policy_sha256
    assert grounds.reopen_conditions == ("the preregistered round budget is renegotiated",)


def test_budget_dominates_other_firing_triggers() -> None:
    receipt = _derive_with_stop(
        0.9, 0.1, fits=(PredictionFit.IN_SUPPORT, PredictionFit.IN_SUPPORT),
        policy=_stop_policy(max_rounds=1, upper=0.7), rounds=3, eig=0.001,
    )
    assert receipt.stop_grounds is not None
    assert receipt.stop_grounds.trigger is StopTriggerCode.ROUND_BUDGET_EXHAUSTED


def test_belief_thresholds_fire_pinned_reasons() -> None:
    upper = _derive_with_stop(
        0.8, 0.2, fits=(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        policy=_stop_policy(), rounds=1,
    )
    assert upper.stop_grounds is not None
    assert upper.stop_grounds.trigger is StopTriggerCode.BELIEF_DECISION_THRESHOLD
    assert upper.stop_grounds.stop_reason is StopReason.EVIDENCE_THRESHOLD_REACHED
    assert upper.stop_grounds.max_hypothesis_belief == pytest.approx(0.8)

    lower = _derive_with_stop(
        0.5, 0.5, fits=(PredictionFit.IN_SUPPORT, PredictionFit.IN_SUPPORT),
        policy=_stop_policy(lower=0.6), rounds=1,
    )
    assert lower.stop_grounds is not None
    assert lower.stop_grounds.trigger is StopTriggerCode.BELIEF_INDISTINGUISHABLE


def test_eig_floor_fires_only_below_floor() -> None:
    clear = _derive_with_stop(
        0.6, 0.4, fits=(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        policy=_stop_policy(), rounds=1, eig=0.5,
    )
    assert clear.disposition is ContinuationDisposition.READY
    assert clear.stop_grounds is None

    below = _derive_with_stop(
        0.6, 0.4, fits=(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        policy=_stop_policy(), rounds=1, eig=0.01,
    )
    assert below.stop_grounds is not None
    assert below.stop_grounds.trigger is StopTriggerCode.MARGINAL_INFORMATION_FLOOR
    assert below.stop_grounds.stop_reason is StopReason.LOW_MARGINAL_INFORMATION_VALUE


def test_unauthorized_stop_policy_fails_closed() -> None:
    model = _belief_model(0.6, 0.4)
    common = dict(
        world_model=model,
        observation=_observation(model),
        assessments=_assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
        stop_policy=_stop_policy(),
        rounds_observed=9,
        max_available_eig=1.0,
    )
    with pytest.raises(ValueError, match="not authorized"):
        derive_continuation_v2(**common, allowed_stop_policy_sha256s=None)
    with pytest.raises(ValueError, match="not authorized"):
        derive_continuation_v2(**common, allowed_stop_policy_sha256s=("0" * 64,))


def test_missing_trigger_inputs_fail_closed() -> None:
    policy = _stop_policy()
    with pytest.raises(ValueError, match="hypothesis beliefs"):
        derive_continuation_v2(
            world_model=_world_model(),
            observation=_observation(),
            assessments=_assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
            stop_policy=policy,
            allowed_stop_policy_sha256s=(policy.policy_sha256,),
            rounds_observed=1,
            max_available_eig=1.0,
        )
    with pytest.raises(ValueError, match="next-experiment eig"):
        _derive_with_stop(
            0.6, 0.4, fits=(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
            policy=policy, rounds=1, eig=None,
        )


def test_operator_class_reasons_are_unreachable() -> None:
    with pytest.raises(ValidationError, match="maps to exactly"):
        StopReasonMappingV1(
            trigger=StopTriggerCode.ROUND_BUDGET_EXHAUSTED,
            stop_reason=StopReason.EMERGENCY_STOP,
            reopen_conditions=("never",),
        )
    with pytest.raises(ValidationError, match="every preregistered trigger"):
        StopPolicyPin(
            max_rounds=1,
            eig_floor=0.1,
            belief_decision_threshold_lower=0.25,
            belief_decision_threshold_upper=0.75,
            stop_reason_mappings=_stop_policy().stop_reason_mappings[:3],
        )


def test_grounds_require_their_trigger_readings() -> None:
    policy = _stop_policy()
    mapping = policy.mapping_for(StopTriggerCode.BELIEF_DECISION_THRESHOLD)
    with pytest.raises(ValidationError, match="belief triggers"):
        StopGrounds(
            stop_policy_sha256=policy.policy_sha256,
            trigger=StopTriggerCode.BELIEF_DECISION_THRESHOLD,
            stop_reason=mapping.stop_reason,
            rounds_observed=1,
            reopen_conditions=mapping.reopen_conditions,
        )


def test_stop_grounds_belong_only_to_stop_receipts() -> None:
    policy = _stop_policy()
    grounds = evaluate_stop_policy(
        stop_policy=policy,
        rounds_observed=5,
        hypothesis_beliefs=(
            HypothesisBeliefV2(hypothesis_sha256="1" * 64, probability=0.5),
            HypothesisBeliefV2(hypothesis_sha256="2" * 64, probability=0.5),
        ),
        max_available_eig=1.0,
    )
    assert grounds is not None
    assert grounds.trigger is StopTriggerCode.ROUND_BUDGET_EXHAUSTED
    ready = derive_continuation_v2(
        world_model=_world_model(),
        observation=_observation(),
        assessments=_assessments(PredictionFit.IN_SUPPORT, PredictionFit.OUT_OF_SUPPORT),
    )
    with pytest.raises(ValidationError, match="exactly the stop-required disposition"):
        ContinuationReceipt.model_validate(
            {**ready.model_dump(), "stop_grounds": grounds.model_dump()}
        )


def test_assessment_pin_stop_authorization_is_optional_and_canonical() -> None:
    legacy = ContinuationAssessmentPolicyPin(
        assessment_implementation_sha256="1" * 64,
        observed_outcome_identity_policy_sha256=OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
        allowed_assessor_principal_ids=("principal.fixture",),
        allowed_fit_rule_sha256s=("2" * 64,),
    )
    assert "allowed_stop_policy_sha256s" not in legacy.model_dump(exclude_none=True)
    authorized = ContinuationAssessmentPolicyPin.model_validate(
        {
            **legacy.model_dump(),
            "allowed_stop_policy_sha256s": ["3" * 64, "4" * 64],
        }
    )
    assert authorized.allowed_stop_policy_sha256s == ("3" * 64, "4" * 64)
    with pytest.raises(ValidationError, match="unique and canonical"):
        ContinuationAssessmentPolicyPin.model_validate(
            {
                **legacy.model_dump(),
                "allowed_stop_policy_sha256s": ["4" * 64, "3" * 64],
            }
        )


def test_rounds_observed_counts_branch_incorporations_only() -> None:
    branch = "rbr_" + "1" * 32
    other = "rbr_" + "2" * 32
    events = (
        _incorporated_event(2, branch),
        _incorporated_event(3, other),
        _incorporated_event(4, branch),
    )
    assert count_rounds_observed(events, branch_id=branch) == 2
    assert count_rounds_observed(events, branch_id=other) == 1
    assert count_rounds_observed((), branch_id=branch) == 0
    assert count_rounds_observed(events, branch_id="rbr_" + "3" * 32) == 0
