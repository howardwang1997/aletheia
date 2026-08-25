"""Graph-scoped F9-v2 continuation decisions for the Research Kernel controller.

The scientific outcome label is deliberately insufficient to choose a continuation.  The caller
must supply one independently produced fit assessment for every active hypothesis, and every
assessment must bind a prediction in the exact observed measurement context.  This keeps a valid
negative that supports an alternative hypothesis distinct from an observation that misses the
entire frozen hypothesis set.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.observations.scientific_bridge import ScientificObservationOutcome
from aletheia.protocols.world_models import HypothesisLifecycle, WorldModelSnapshotV2
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_kernel.schemas import ActionKind, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SCIENTIFIC_SLOT_PATTERN = r"^sos_[0-9a-f]{32}$"


class PredictionFit(str, Enum):
    """Independent comparison of the observation with one frozen prediction."""

    IN_SUPPORT = "in_support"
    OUT_OF_SUPPORT = "out_of_support"
    INDETERMINATE = "indeterminate"


class ContinuationDisposition(str, Enum):
    """Typed scientific disposition; the controller maps this to a proposed Kernel action."""

    READY = "ready"
    REDESIGN_OBSERVABLE = "redesign_observable"
    HYPOTHESIS_SET_FORK_REQUIRED = "hypothesis_set_fork_required"


class ScientificObservationProjection(ControllerModel):
    """Exact admitted observation context consumed by F9-v2 continuation."""

    schema_name: Literal["aletheia.scientific_observation_projection"] = (
        "aletheia.scientific_observation_projection"
    )
    schema_version: Literal[1] = 1
    scientific_slot_id: str = Field(pattern=_SCIENTIFIC_SLOT_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_world_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: ScientificObservationOutcome
    observable_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_space_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


class HypothesisPredictionAssessment(ControllerModel):
    """Independent fit assessment bound to one exact F9-v2 prediction."""

    schema_name: Literal["aletheia.hypothesis_prediction_assessment"] = (
        "aletheia.hypothesis_prediction_assessment"
    )
    schema_version: Literal[1] = 1
    hypothesis_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_fit: PredictionFit
    fit_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def assessment_sha256(self) -> str:
        return canonical_sha256(self)


class ContinuationReceipt(ControllerModel):
    """Content-addressed result that can be recovered without re-running a model."""

    schema_name: Literal["aletheia.graph_scoped_continuation_receipt"] = (
        "aletheia.graph_scoped_continuation_receipt"
    )
    schema_version: Literal[1] = 1
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_slot_id: str = Field(pattern=_SCIENTIFIC_SLOT_PATTERN)
    assessments: tuple[HypothesisPredictionAssessment, ...] = Field(max_length=64)
    disposition: ContinuationDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=64)
    proposed_action_kind: ActionKind
    legacy_run_synthesized: Literal[False] = False
    legacy_optimize_used: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_canonical(self) -> "ContinuationReceipt":
        order = tuple(
            (item.hypothesis_sha256, item.prediction_sha256, item.assessment_sha256)
            for item in self.assessments
        )
        if order != tuple(sorted(set(order))):
            raise ValueError("continuation assessments must be unique and canonical")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("continuation reason codes must be unique and canonical")
        if self.proposed_action_kind is not continuation_to_action_kind(self.disposition):
            raise ValueError("continuation action kind differs from its typed disposition")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


def continuation_to_action_kind(disposition: ContinuationDisposition) -> ActionKind:
    """Map a typed continuation result to the only compatible Kernel proposal kind."""

    return {
        ContinuationDisposition.READY: ActionKind.CONTINUE,
        ContinuationDisposition.REDESIGN_OBSERVABLE: ActionKind.REFINE,
        ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED: ActionKind.FORK,
    }[disposition]


def derive_continuation_v2(
    *,
    world_model: WorldModelSnapshotV2,
    observation: ScientificObservationProjection,
    assessments: tuple[HypothesisPredictionAssessment, ...],
) -> ContinuationReceipt:
    """Derive one deterministic continuation from an admitted observation and frozen F9-v2 model."""

    if observation.source_world_model_sha256 != world_model.world_model_sha256:
        raise ValueError("continuation world model differs from the admitted observation source")

    active_hypotheses = tuple(
        sorted(
            (
                item.hypothesis_sha256
                for item in world_model.hypotheses
                if item.lifecycle is HypothesisLifecycle.ACTIVE
            )
        )
    )
    if not active_hypotheses:
        raise ValueError("continuation requires at least one active hypothesis")

    canonical_assessments = tuple(
        sorted(
            assessments,
            key=lambda item: (
                item.hypothesis_sha256,
                item.prediction_sha256,
                item.assessment_sha256,
            ),
        )
    )
    if canonical_assessments != assessments:
        raise ValueError("continuation assessments must be canonically ordered")
    assessment_hypotheses = tuple(item.hypothesis_sha256 for item in assessments)
    if assessment_hypotheses != tuple(sorted(set(assessment_hypotheses))):
        raise ValueError("continuation requires at most one assessment per hypothesis")

    predictions_by_hash = {item.prediction_sha256: item for item in world_model.predictions}
    for assessment in assessments:
        prediction = predictions_by_hash.get(assessment.prediction_sha256)
        if prediction is None:
            raise ValueError("continuation assessment references an unknown prediction")
        if prediction.hypothesis_sha256 != assessment.hypothesis_sha256:
            raise ValueError("continuation assessment changed the prediction hypothesis")
        if (
            prediction.observable_spec_sha256,
            prediction.measurement_protocol_sha256,
            prediction.outcome_space_sha256,
        ) != (
            observation.observable_spec_sha256,
            observation.measurement_protocol_sha256,
            observation.outcome_space_sha256,
        ):
            raise ValueError("continuation assessment escaped the observed measurement context")

    missing = tuple(sorted(set(active_hypotheses) - set(assessment_hypotheses)))
    extra = tuple(sorted(set(assessment_hypotheses) - set(active_hypotheses)))
    if extra:
        raise ValueError("continuation assessment references a non-active hypothesis")
    if missing:
        disposition = ContinuationDisposition.REDESIGN_OBSERVABLE
        reason_codes = ("active_hypothesis_prediction_missing",)
    elif any(item.prediction_fit is PredictionFit.INDETERMINATE for item in assessments):
        disposition = ContinuationDisposition.REDESIGN_OBSERVABLE
        reason_codes = ("prediction_fit_indeterminate",)
    elif all(item.prediction_fit is PredictionFit.OUT_OF_SUPPORT for item in assessments):
        disposition = ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED
        reason_codes = ("all_active_hypotheses_out_of_support",)
    else:
        disposition = ContinuationDisposition.READY
        reason_codes = ("active_hypothesis_retains_support",)

    return ContinuationReceipt(
        world_model_snapshot_sha256=world_model.world_model_sha256,
        observation_projection_sha256=observation.projection_sha256,
        scientific_slot_id=observation.scientific_slot_id,
        assessments=assessments,
        disposition=disposition,
        reason_codes=reason_codes,
        proposed_action_kind=continuation_to_action_kind(disposition),
    )


__all__ = [
    "ContinuationDisposition",
    "ContinuationReceipt",
    "HypothesisPredictionAssessment",
    "PredictionFit",
    "ScientificObservationProjection",
    "continuation_to_action_kind",
    "derive_continuation_v2",
]
