"""Graph-scoped F9-v2 continuation decisions for the Research Kernel controller.

The scientific outcome label is deliberately insufficient to choose a continuation.  The caller
must supply one independently produced fit assessment for every active hypothesis, and every
assessment must bind a prediction in the exact observed measurement context.  This keeps a valid
negative that supports an alternative hypothesis distinct from an observation that misses the
entire frozen hypothesis set.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    CommittedObservationValidationReceipt,
    ScientificObservationOutcome,
)
from aletheia.protocols.world_models import HypothesisLifecycle, WorldModelSnapshotV2
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_kernel.schemas import (
    ActionKind,
    ObservationIncorporatedPayload,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SCIENTIFIC_SLOT_PATTERN = r"^sos_[0-9a-f]{32}$"
_OUTCOME_BIN_PATTERN = r"^[a-z][a-z0-9_.:/-]{1,127}$"
OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256 = canonical_sha256(
    {
        "schema_name": "aletheia.observed_scientific_outcome_identity_policy",
        "schema_version": 1,
        "source": "committed_f9_v2_validation_projection",
    }
)
EXACT_OUTCOME_BIN_PREDICTION_POLICY_SHA256 = canonical_sha256(
    {
        "schema_name": "aletheia.exact_outcome_bin_prediction_identity_policy",
        "schema_version": 1,
        "source": "frozen_f9_v2_prediction_and_admission_outcome_bins",
    }
)


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
    schema_version: Literal[2] = 2
    scientific_slot_id: str = Field(pattern=_SCIENTIFIC_SLOT_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    scientific_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_world_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: ScientificObservationOutcome
    observable_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_space_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_bin_id: str = Field(pattern=_OUTCOME_BIN_PATTERN)
    admissible_outcome_bin_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    admission_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _outcome_bins_are_canonical(self) -> "ScientificObservationProjection":
        if (
            self.admissible_outcome_bin_ids != tuple(sorted(set(self.admissible_outcome_bin_ids)))
            or self.observed_outcome_bin_id not in self.admissible_outcome_bin_ids
        ):
            raise ValueError("scientific observation outcome bins must be unique and canonical")
        return self

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


class ContinuationAssessmentProvenance(ControllerModel):
    """Persisted operational provenance for one powerless continuation assessment."""

    schema_name: Literal["aletheia.continuation_assessment_provenance"] = (
        "aletheia.continuation_assessment_provenance"
    )
    schema_version: Literal[1] = 1
    assessment_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessed_by_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$")
    assessed_at: AwareDatetime
    independent_from_executor: Literal[True] = True
    direct_scientific_authority: Literal[False] = False

    @property
    def provenance_sha256(self) -> str:
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
    assessment_provenance: ContinuationAssessmentProvenance | None = None
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


def exact_outcome_bin_prediction_sha256(
    *,
    observable_spec_sha256: str,
    measurement_protocol_sha256: str,
    outcome_space_sha256: str,
    outcome_bin_id: str,
) -> str:
    """Commit one exact admissible outcome bin under the F9-v2 measurement context."""

    if (
        any(
            re.fullmatch(_SHA256_PATTERN, value) is None
            for value in (
                observable_spec_sha256,
                measurement_protocol_sha256,
                outcome_space_sha256,
            )
        )
        or re.fullmatch(_OUTCOME_BIN_PATTERN, outcome_bin_id) is None
    ):
        raise ValueError("exact outcome-bin prediction inputs are invalid")

    return canonical_sha256(
        {
            "schema_name": "aletheia.exact_outcome_bin_prediction_identity",
            "schema_version": 1,
            "identity_policy_sha256": EXACT_OUTCOME_BIN_PREDICTION_POLICY_SHA256,
            "observable_spec_sha256": observable_spec_sha256,
            "measurement_protocol_sha256": measurement_protocol_sha256,
            "outcome_space_sha256": outcome_space_sha256,
            "outcome_bin_id": outcome_bin_id,
        }
    )


def continuation_assessment_source_sha256(
    *,
    quest_id: str,
    action_sha256: str,
    scientific_slot_id: str,
    incorporation_event_sha256: str,
    world_model_snapshot_sha256: str,
    observation_projection_sha256: str,
    compilation_sha256: str,
    committed_validation_receipt_sha256: str,
    validation_campaign_projection_sha256: str,
    committed_admission_sha256: str,
) -> str:
    """Canonical identity shared by the continuation writer and restart verifier."""

    return canonical_sha256(
        {
            "schema_name": "aletheia.continuation_assessment_source",
            "schema_version": 1,
            "quest_id": quest_id,
            "action_sha256": action_sha256,
            "scientific_slot_id": scientific_slot_id,
            "incorporation_event_sha256": incorporation_event_sha256,
            "world_model_snapshot_sha256": world_model_snapshot_sha256,
            "observation_projection_sha256": observation_projection_sha256,
            "compilation_sha256": compilation_sha256,
            "committed_validation_receipt_sha256": committed_validation_receipt_sha256,
            "validation_campaign_projection_sha256": validation_campaign_projection_sha256,
            "committed_admission_sha256": committed_admission_sha256,
        }
    )


def derive_continuation_v2(
    *,
    world_model: WorldModelSnapshotV2,
    observation: ScientificObservationProjection,
    assessments: tuple[HypothesisPredictionAssessment, ...],
    assessment_provenance: ContinuationAssessmentProvenance | None = None,
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
        assessment_provenance=assessment_provenance,
    )


def project_admitted_scientific_observation(
    *,
    incorporation: ObservationIncorporatedPayload,
    committed_validation: CommittedObservationValidationReceipt,
) -> ScientificObservationProjection:
    """Rebuild the only continuation observation projection from signed durable authority."""

    try:
        committed_validation = CommittedObservationValidationReceipt.model_validate(
            committed_validation.model_dump(mode="python")
        )
        message = committed_validation.message.receipt.message
        authorization = message.raw_run.scientific_authorization.message
        binding = authorization.action_protocol_binding
        protocol = binding.compilation_request.protocol
        world_model = protocol.world_model
        campaign = message.validation_campaign_projection
        artifact_binding = authorization.scientific_observation_artifact_binding
        admission_policy = authorization.admission_policy
        admissible_outcome_bin_ids = tuple(
            item.outcome_bin_id for item in admission_policy.outcome_bin_mappings
        )
        if (
            message.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
            or message.outcome is None
            or message.scientific_observation_sha256 is None
            or campaign is None
            or campaign.validation_batch_sha256 is None
            or campaign.outcome_bin_id is None
            or world_model is None
            or incorporation.action_id != binding.action.action_id
            or incorporation.branch_id != protocol.graph_scope.branch_id
            or incorporation.scientific_slot_id != message.scientific_slot_id
            or incorporation.scientific_observation_sha256 != message.scientific_observation_sha256
            or incorporation.outcome != message.outcome.value
            or incorporation.source_world_model_sha256 != world_model.world_model_sha256
        ):
            raise ValueError("incorporated observation differs from signed validation authority")
        observed_outcome_sha256 = canonical_sha256(
            {
                "schema_name": "aletheia.observed_scientific_outcome_identity",
                "schema_version": 1,
                "identity_policy_sha256": OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
                "scientific_slot_id": incorporation.scientific_slot_id,
                "scientific_observation_sha256": incorporation.scientific_observation_sha256,
                "validation_campaign_projection_sha256": campaign.projection_sha256,
                "validation_batch_sha256": campaign.validation_batch_sha256,
                "outcome_bin_id": campaign.outcome_bin_id,
                "outcome": message.outcome.value,
            }
        )
        return ScientificObservationProjection(
            scientific_slot_id=incorporation.scientific_slot_id,
            committed_admission_sha256=incorporation.committed_admission_sha256,
            scientific_observation_sha256=incorporation.scientific_observation_sha256,
            source_world_model_sha256=incorporation.source_world_model_sha256,
            outcome=message.outcome,
            observable_spec_sha256=artifact_binding.observable.observable_sha256,
            measurement_protocol_sha256=protocol.method.method_contract_sha256,
            outcome_space_sha256=protocol.analysis_plan.outcome_space_sha256,
            observed_outcome_bin_id=campaign.outcome_bin_id,
            admissible_outcome_bin_ids=admissible_outcome_bin_ids,
            admission_policy_sha256=admission_policy.policy_sha256,
            observed_outcome_sha256=observed_outcome_sha256,
        )
    except (TypeError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001 - nested signed authority fails closed
        raise ValueError("admitted observation projection reconstruction failed closed") from exc


__all__ = [
    "ContinuationAssessmentProvenance",
    "ContinuationDisposition",
    "ContinuationReceipt",
    "EXACT_OUTCOME_BIN_PREDICTION_POLICY_SHA256",
    "HypothesisPredictionAssessment",
    "OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256",
    "PredictionFit",
    "ScientificObservationProjection",
    "continuation_assessment_source_sha256",
    "continuation_to_action_kind",
    "derive_continuation_v2",
    "exact_outcome_bin_prediction_sha256",
    "project_admitted_scientific_observation",
]
