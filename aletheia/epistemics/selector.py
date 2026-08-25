"""F9-S5 observation-blind constrained experiment selection.

The selector consumes archived F9-S4 commitments, recomputes discrete expected information gain
from the exact F9-S1 prior and hypothesis likelihoods, applies hard scientific/operational
feasibility gates, and ranks only then by a frozen multi-attribute utility.  It never receives a
target observation and cannot turn a high-information invalid proxy into a selected experiment.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.causal import CausalAdapterRuntime
from aletheia.epistemics.prediction import (
    CommittedPredictionCommitmentCampaign,
    PredictionCommitmentCampaign,
    PredictionCommitmentDisposition,
    PredictionMode,
    load_prediction_commitment_campaign,
)
from aletheia.epistemics.schemas import EpistemicModel
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
    ResponseArchiveError,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"
_ACTOR_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$"
_METRIC_DIGITS = 12


class ExperimentRiskLevel(str, Enum):
    NEGLIGIBLE = "negligible"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    PROHIBITED = "prohibited"


class MeasurementValidityStatus(str, Enum):
    VALIDATED = "validated"
    BOUNDED = "bounded"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class ProxyRiskStatus(str, Enum):
    NONE = "none"
    BOUNDED_SURROGATE = "bounded_surrogate"
    INVALID_SURROGATE = "invalid_surrogate"


class ExperimentSelectionDisposition(str, Enum):
    READY_SELECTED = "ready_selected"
    NO_FEASIBLE_EXPERIMENT = "no_feasible_experiment"
    BLOCKED_EXECUTION = "blocked_execution"


class CandidateSelectionDisposition(str, Enum):
    SELECTED = "selected"
    FEASIBLE_NOT_SELECTED = "feasible_not_selected"
    INFEASIBLE = "infeasible"


class ExperimentSelectionFailureKind(str, Enum):
    PREDICTION_ARCHIVE_INVALID = "prediction_archive_invalid"


_RISK_ORDER = {
    ExperimentRiskLevel.NEGLIGIBLE: 0,
    ExperimentRiskLevel.LOW: 1,
    ExperimentRiskLevel.MODERATE: 2,
    ExperimentRiskLevel.HIGH: 3,
    ExperimentRiskLevel.PROHIBITED: 4,
}
_RISK_BURDEN = {
    ExperimentRiskLevel.NEGLIGIBLE: 0.0,
    ExperimentRiskLevel.LOW: 0.25,
    ExperimentRiskLevel.MODERATE: 0.5,
    ExperimentRiskLevel.HIGH: 0.75,
    ExperimentRiskLevel.PROHIBITED: 1.0,
}


class FreshConfirmationBatch(EpistemicModel):
    batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    partition_sha256: str = Field(pattern=_SHA256_PATTERN)
    custody_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    sealed_at: AwareDatetime
    available_until: AwareDatetime
    state: Literal["reserved_unused"] = "reserved_unused"

    @model_validator(mode="after")
    def _reservation_has_a_future_window(self) -> "FreshConfirmationBatch":
        if self.available_until <= self.sealed_at:
            raise ValueError("fresh confirmation reservation must expire after it is sealed")
        return self

    @property
    def reservation_sha256(self) -> str:
        return content_sha256(self)


class CandidateExperimentAssessment(EpistemicModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    experiment_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_process_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_error_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_validity_status: MeasurementValidityStatus
    measurement_validity_confidence: float = Field(ge=0.0, le=1.0)
    measurement_validity_evidence_sha256s: tuple[str, ...] = Field(max_length=128)
    proxy_risk_status: ProxyRiskStatus
    proxy_risk_rationale_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimated_cost_microunits: int = Field(ge=0)
    cost_currency: str = Field(min_length=3, max_length=16)
    estimated_duration_seconds: int = Field(ge=1)
    risk_level: ExperimentRiskLevel
    risk_assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    required_capability_sha256s: tuple[str, ...] = Field(max_length=128)
    available_capability_sha256s: tuple[str, ...] = Field(max_length=128)
    capability_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    fresh_confirmation_batches: tuple[FreshConfirmationBatch, ...] = Field(max_length=64)
    replication_debt_ledger_sha256: str = Field(pattern=_SHA256_PATTERN)
    replication_debt_before: int = Field(ge=0, le=1_000_000)
    expected_replication_debt_reduction: int = Field(ge=0, le=1_000_000)
    independent_replication: bool
    replication_protocol_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    assessment_evidence_sha256s: tuple[str, ...] = Field(min_length=1, max_length=256)
    assessor_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _assessment_is_canonical_and_coherent(self) -> "CandidateExperimentAssessment":
        for values, label in (
            (self.measurement_validity_evidence_sha256s, "measurement evidence"),
            (self.required_capability_sha256s, "required capabilities"),
            (self.available_capability_sha256s, "available capabilities"),
            (self.assessment_evidence_sha256s, "assessment evidence"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"candidate assessment {label} must be unique and canonical")
        batch_ids = [item.batch_sha256 for item in self.fresh_confirmation_batches]
        if batch_ids != sorted(set(batch_ids)):
            raise ValueError("fresh confirmation batches must be unique and canonical")
        partition_ids = [item.partition_sha256 for item in self.fresh_confirmation_batches]
        if len(partition_ids) != len(set(partition_ids)):
            raise ValueError("fresh confirmation partitions must be unique")
        if not self.cost_currency.strip() or self.cost_currency != self.cost_currency.upper():
            raise ValueError("experiment cost currency must be a non-blank uppercase code")
        if (
            self.measurement_validity_status is MeasurementValidityStatus.VALIDATED
            and not self.measurement_validity_evidence_sha256s
        ):
            raise ValueError("validated measurement requires evidence")
        if (
            self.measurement_validity_status is MeasurementValidityStatus.VALIDATED
            and self.proxy_risk_status is ProxyRiskStatus.INVALID_SURROGATE
        ):
            raise ValueError("invalid surrogate cannot be marked as validated measurement")
        if self.expected_replication_debt_reduction > self.replication_debt_before:
            raise ValueError("replication debt reduction cannot exceed existing debt")
        reducing_debt = self.expected_replication_debt_reduction > 0
        if reducing_debt and (
            not self.independent_replication or self.replication_protocol_sha256 is None
        ):
            raise ValueError("replication debt reduction requires an independent frozen protocol")
        if self.independent_replication != (self.replication_protocol_sha256 is not None):
            raise ValueError(
                "independent replication and protocol identity must be declared together"
            )
        if self.fresh_confirmation_batches and self.completed_at < max(
            item.sealed_at for item in self.fresh_confirmation_batches
        ):
            raise ValueError("candidate assessment predates a confirmation reservation")
        return self

    @property
    def assessment_sha256(self) -> str:
        return content_sha256(self)


class ExperimentAssessmentBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    assessor_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessments: tuple[CandidateExperimentAssessment, ...] = Field(min_length=2, max_length=128)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _batch_is_complete_and_canonical(self) -> "ExperimentAssessmentBatch":
        identities = [item.candidate_id for item in self.assessments]
        if identities != sorted(set(identities)):
            raise ValueError("experiment assessments require unique canonical candidate IDs")
        if any(
            item.assessor_manifest_sha256 != self.assessor_manifest_sha256
            for item in self.assessments
        ):
            raise ValueError("experiment assessment belongs to another assessor manifest")
        if self.completed_at < max(item.completed_at for item in self.assessments):
            raise ValueError("experiment assessment batch predates a candidate assessment")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


EXPERIMENT_ASSESSMENT_OUTPUT_SCHEMA_SHA256 = content_sha256(
    ExperimentAssessmentBatch.model_json_schema()
)


class ExperimentAssessmentManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    assessor_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    runtime: CausalAdapterRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessor_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    observation_access: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _assessor_is_frozen_and_observation_blind(self) -> "ExperimentAssessmentManifest":
        if self.output_schema_sha256 != EXPERIMENT_ASSESSMENT_OUTPUT_SCHEMA_SHA256:
            raise ValueError("experiment assessor uses another output schema")
        if self.tool_names:
            raise ValueError("experiment assessor cannot receive ambient tool authority")
        model_fields = (
            self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        )
        if self.runtime is CausalAdapterRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model experiment assessor requires frozen model transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic experiment assessor cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class SelectionUtilityWeights(EpistemicModel):
    expected_information_gain: float = Field(default=0.45, ge=0.0, le=1.0)
    minimum_pairwise_discrimination: float = Field(default=0.20, ge=0.0, le=1.0)
    fresh_confirmation: float = Field(default=0.10, ge=0.0, le=1.0)
    replication_debt_reduction: float = Field(default=0.10, ge=0.0, le=1.0)
    cost_penalty: float = Field(default=0.05, ge=0.0, le=1.0)
    duration_penalty: float = Field(default=0.05, ge=0.0, le=1.0)
    risk_penalty: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_are_finite_and_normalized(self) -> "SelectionUtilityWeights":
        values = tuple(self.model_dump(mode="python").values())
        if any(not math.isfinite(value) for value in values):
            raise ValueError("experiment utility weights must be finite")
        if not math.isclose(math.fsum(values), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("experiment utility weights must sum to one")
        return self


class ExperimentSelectionPolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    budget_microunits: int = Field(gt=0)
    budget_currency: str = Field(min_length=3, max_length=16)
    maximum_duration_seconds: int = Field(gt=0)
    maximum_risk_level: ExperimentRiskLevel = ExperimentRiskLevel.MODERATE
    minimum_measurement_validity_confidence: float = Field(default=0.8, gt=0.5, le=1.0)
    minimum_fresh_confirmation_batches: int = Field(default=1, ge=1, le=64)
    confirmation_saturation_batches: int = Field(default=2, ge=1, le=64)
    minimum_expected_information_gain_ratio: float = Field(default=0.01, ge=0.0, le=1.0)
    minimum_pairwise_total_variation: float = Field(default=0.1, gt=0.0, le=1.0)
    utility_weights: SelectionUtilityWeights = Field(default_factory=SelectionUtilityWeights)
    require_validated_measurement: Literal[True] = True
    require_no_proxy_risk: Literal[True] = True
    require_complete_capabilities: Literal[True] = True
    require_archived_prediction_commitments: Literal[True] = True
    selector_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_is_coherent(self) -> "ExperimentSelectionPolicy":
        if not self.budget_currency.strip() or self.budget_currency != self.budget_currency.upper():
            raise ValueError("selection budget currency must be a non-blank uppercase code")
        if self.maximum_risk_level is ExperimentRiskLevel.PROHIBITED:
            raise ValueError("selection policy cannot authorize prohibited risk")
        if self.confirmation_saturation_batches < self.minimum_fresh_confirmation_batches:
            raise ValueError("confirmation saturation cannot be below the hard minimum")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class ExperimentCandidate(EpistemicModel):
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    committed_prediction: CommittedPredictionCommitmentCampaign

    @property
    def candidate_sha256(self) -> str:
        return content_sha256(self)


class ExperimentSelectionRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    selection_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_hypothesis_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidates: tuple[ExperimentCandidate, ...] = Field(min_length=2, max_length=128)
    assessment_batch: ExperimentAssessmentBatch
    assessor_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["none"] = "none"

    @model_validator(mode="after")
    def _request_is_canonical(self) -> "ExperimentSelectionRequest":
        identities = [item.candidate_id for item in self.candidates]
        if identities != sorted(set(identities)):
            raise ValueError("selection candidates require unique canonical IDs")
        commitments = [
            item.committed_prediction.campaign.commitment_sha256 for item in self.candidates
        ]
        if len(commitments) != len(set(commitments)):
            raise ValueError("selection request cannot compare duplicate substantive commitments")
        namespaces = [
            item.committed_prediction.campaign.request.experiment_protocol.experiment_namespace_sha256
            for item in self.candidates
        ]
        if len(namespaces) != len(set(namespaces)):
            raise ValueError("selection candidates require unique experiment namespaces")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class HypothesisPosteriorProbability(EpistemicModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    probability: float = Field(ge=0.0, le=1.0)


class ExpectedOutcomePosterior(EpistemicModel):
    bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    marginal_probability: float = Field(ge=0.0, le=1.0)
    hypothesis_posteriors: tuple[HypothesisPosteriorProbability, ...]
    posterior_entropy_nats: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _posterior_is_normalized_and_canonical(self) -> "ExpectedOutcomePosterior":
        identities = [item.hypothesis_id for item in self.hypothesis_posteriors]
        if identities != sorted(set(identities)):
            raise ValueError("outcome posterior hypotheses must be unique and canonical")
        if self.marginal_probability > 0.0 and not math.isclose(
            math.fsum(item.probability for item in self.hypothesis_posteriors),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("outcome posterior probabilities must sum to one")
        return self


class ExpectedInformationGainAudit(EpistemicModel):
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    belief_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_entropy_nats: float = Field(ge=0.0)
    expected_posterior_entropy_nats: float = Field(ge=0.0)
    expected_information_gain_nats: float = Field(ge=0.0)
    expected_information_gain_ratio: float = Field(ge=0.0, le=1.0)
    minimum_pairwise_total_variation: float = Field(ge=0.0, le=1.0)
    maximum_pairwise_total_variation: float = Field(ge=0.0, le=1.0)
    outcome_posteriors: tuple[ExpectedOutcomePosterior, ...]

    @model_validator(mode="after")
    def _outcomes_are_canonical_and_normalized(self) -> "ExpectedInformationGainAudit":
        identities = [item.bin_id for item in self.outcome_posteriors]
        if identities != sorted(set(identities)):
            raise ValueError("EIG outcomes must use unique canonical bin IDs")
        if not math.isclose(
            math.fsum(item.marginal_probability for item in self.outcome_posteriors),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("EIG outcome marginals must sum to one")
        if self.minimum_pairwise_total_variation > self.maximum_pairwise_total_variation:
            raise ValueError("minimum pairwise discrimination exceeds maximum")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


class PredictionArchiveVerification(EpistemicModel):
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_custody_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_at: AwareDatetime

    @property
    def verification_sha256(self) -> str:
        return content_sha256(self)


class ExperimentCandidateScore(EpistemicModel):
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    prediction_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_sha256: str = Field(pattern=_SHA256_PATTERN)
    archive_verification_sha256: str = Field(pattern=_SHA256_PATTERN)
    information_audit: ExpectedInformationGainAudit | None = None
    cost_budget_ratio: float = Field(ge=0.0)
    duration_limit_ratio: float = Field(ge=0.0)
    risk_burden: float = Field(ge=0.0, le=1.0)
    fresh_confirmation_score: float = Field(ge=0.0, le=1.0)
    valid_fresh_confirmation_batches: int = Field(ge=0, le=64)
    replication_debt_reduction_score: float = Field(ge=0.0, le=1.0)
    replication_debt_after: int = Field(ge=0, le=1_000_000)
    constrained_utility: float | None = None
    feasible: bool
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _score_is_coherent_and_canonical(self) -> "ExperimentCandidateScore":
        numeric = (
            self.cost_budget_ratio,
            self.duration_limit_ratio,
            self.risk_burden,
            self.fresh_confirmation_score,
            self.replication_debt_reduction_score,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("candidate score components must be finite")
        if self.constrained_utility is not None and not math.isfinite(self.constrained_utility):
            raise ValueError("candidate constrained utility must be finite")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("candidate blockers must be unique and canonical")
        if self.feasible != (not self.blockers):
            raise ValueError("candidate feasibility must be exactly derived from blockers")
        if self.feasible and (self.information_audit is None or self.constrained_utility is None):
            raise ValueError("feasible candidate requires EIG audit and constrained utility")
        if not self.feasible and self.constrained_utility is not None:
            raise ValueError("infeasible candidate cannot receive constrained utility")
        return self

    @property
    def score_sha256(self) -> str:
        return content_sha256(self)


class ExperimentCandidateRanking(EpistemicModel):
    rank: int = Field(ge=1, le=128)
    candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    candidate_score_sha256: str = Field(pattern=_SHA256_PATTERN)
    disposition: CandidateSelectionDisposition
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reasons_are_canonical(self) -> "ExperimentCandidateRanking":
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("candidate ranking reasons must be unique and canonical")
        return self


class ExperimentSelectionDecision(EpistemicModel):
    selected_candidate_id: str | None = Field(default=None, pattern=_LOCAL_ID_PATTERN)
    rankings: tuple[ExperimentCandidateRanking, ...] = Field(min_length=2, max_length=128)
    disposition: ExperimentSelectionDisposition

    @model_validator(mode="after")
    def _decision_is_complete(self) -> "ExperimentSelectionDecision":
        ranks = [item.rank for item in self.rankings]
        identities = [item.candidate_id for item in self.rankings]
        if ranks != list(range(1, len(self.rankings) + 1)):
            raise ValueError("candidate rankings must use complete contiguous ranks")
        if len(identities) != len(set(identities)):
            raise ValueError("candidate rankings cannot repeat a candidate")
        selected = [
            item
            for item in self.rankings
            if item.disposition is CandidateSelectionDisposition.SELECTED
        ]
        if self.disposition is ExperimentSelectionDisposition.READY_SELECTED:
            if len(selected) != 1 or selected[0].candidate_id != self.selected_candidate_id:
                raise ValueError("ready selection requires one exact selected candidate")
        elif self.selected_candidate_id is not None or selected:
            raise ValueError("non-ready selection cannot name a selected candidate")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class ExperimentSelectionFailure(EpistemicModel):
    kind: ExperimentSelectionFailureKind
    failed_candidate_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class _DerivedSelection(EpistemicModel):
    candidate_scores: tuple[ExperimentCandidateScore, ...]
    decision: ExperimentSelectionDecision


class ExperimentSelectionCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    policy: ExperimentSelectionPolicy
    assessor_manifest: ExperimentAssessmentManifest
    request: ExperimentSelectionRequest
    archive_verifications: tuple[PredictionArchiveVerification, ...]
    candidate_scores: tuple[ExperimentCandidateScore, ...]
    decision: ExperimentSelectionDecision | None = None
    failure: ExperimentSelectionFailure | None = None
    blockers: tuple[str, ...]
    disposition: ExperimentSelectionDisposition
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "ExperimentSelectionCampaign":
        _validate_selection_request(
            policy=self.policy,
            assessor_manifest=self.assessor_manifest,
            request=self.request,
        )
        if self.failure is not None:
            expected = (f"execution_failure:{self.failure.kind.value}",)
            if (
                self.archive_verifications
                or self.candidate_scores
                or self.decision is not None
                or self.blockers != expected
                or self.disposition is not ExperimentSelectionDisposition.BLOCKED_EXECUTION
            ):
                raise ValueError("failed experiment selection outputs are not derived")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("experiment selection failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("experiment selection campaign predates its failure")
            return self
        if self.decision is None:
            raise ValueError("successful experiment selection requires a decision")
        identities = [item.candidate_id for item in self.archive_verifications]
        expected_ids = [item.candidate_id for item in self.request.candidates]
        if identities != expected_ids:
            raise ValueError("archive verifications must cover candidates in canonical order")
        for verification, candidate in zip(
            self.archive_verifications, self.request.candidates, strict=True
        ):
            committed = candidate.committed_prediction
            campaign = committed.campaign
            expected = {
                "prediction_campaign_sha256": campaign.campaign_sha256,
                "prediction_commitment_sha256": campaign.commitment_sha256,
                "commitment_receipt_sha256": committed.receipt_sha256,
                "ledger_receipt_sha256": committed.ledger.receipt_sha256,
                "archive_custody_sha256": self.request.prediction_archive_custody_sha256,
            }
            for field_name, expected_value in expected.items():
                if getattr(verification, field_name) != expected_value:
                    raise ValueError(f"prediction archive verification changed {field_name}")
            if verification.verified_at < self.request.issued_at:
                raise ValueError("prediction archive verification predates selection request")
        derived = _derive_selection(
            policy=self.policy,
            request=self.request,
            verifications=self.archive_verifications,
        )
        if self.candidate_scores != derived.candidate_scores or self.decision != derived.decision:
            raise ValueError("experiment selection outputs are not mechanically derived")
        if self.blockers:
            raise ValueError("successful experiment selection cannot have campaign blockers")
        if self.disposition is not self.decision.disposition:
            raise ValueError("selection campaign disposition differs from its decision")
        if self.generated_at < max(item.verified_at for item in self.archive_verifications):
            raise ValueError("selection campaign predates archive verification")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


class CommittedExperimentSelectionCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: ExperimentSelectionCampaign
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedExperimentSelectionCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("experiment selection ledger does not commit its campaign and time")
        if self.committed_at < self.campaign.generated_at:
            raise ValueError("experiment selection commitment predates campaign generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _source_snapshot(campaign: PredictionCommitmentCampaign):
    return campaign.source_causal_campaign.world_model_snapshot


def _measurement_process(campaign: PredictionCommitmentCampaign):
    causal = campaign.source_causal_campaign
    if causal.contract_batch is None:
        raise ValueError("prediction candidate lacks a causal contract")
    contract = causal.contract_batch.contract
    process = next(
        (
            item
            for item in contract.measurement_processes
            if item.process_id == contract.outcome_measurement_process_id
        ),
        None,
    )
    if process is None:
        raise ValueError("prediction candidate lacks its outcome measurement process")
    return process


def _validate_assessor_independence(
    *,
    assessor: ExperimentAssessmentManifest,
    campaigns: tuple[PredictionCommitmentCampaign, ...],
) -> None:
    forbidden_principals: set[str] = set()
    forbidden_models: set[str] = set()
    for campaign in campaigns:
        causal = campaign.source_causal_campaign
        hypothesis = causal.source_campaign
        forbidden_principals.update(
            {
                campaign.author_manifest.author_principal_sha256,
                campaign.calibration_evaluator_manifest.evaluator_principal_sha256,
                causal.author_manifest.author_principal_sha256,
                causal.reviewer_manifest.reviewer_principal_sha256,
                hypothesis.generator_manifest.generator_principal_sha256,
                hypothesis.deduplicator_manifest.reviewer_principal_sha256,
            }
        )
        for identity in (
            campaign.author_manifest.model_identity_sha256,
            campaign.calibration_evaluator_manifest.model_identity_sha256,
            causal.author_manifest.model_identity_sha256,
            causal.reviewer_manifest.model_identity_sha256,
            hypothesis.generator_manifest.model_identity_sha256,
            hypothesis.deduplicator_manifest.model_identity_sha256,
        ):
            if identity is not None:
                forbidden_models.add(identity)
    if assessor.assessor_principal_sha256 in forbidden_principals:
        raise ValueError("experiment assessor must be independent from proposal/review roles")
    if (
        assessor.model_identity_sha256 is not None
        and assessor.model_identity_sha256 in forbidden_models
    ):
        raise ValueError("experiment assessor must use an independent model identity")


def _validate_selection_request(
    *,
    policy: ExperimentSelectionPolicy,
    assessor_manifest: ExperimentAssessmentManifest,
    request: ExperimentSelectionRequest,
) -> None:
    if request.policy_sha256 != policy.policy_sha256:
        raise ValueError("selection request changed its policy binding")
    if request.assessor_manifest_sha256 != assessor_manifest.manifest_sha256:
        raise ValueError("selection request changed its assessor binding")
    if request.assessment_batch.assessor_manifest_sha256 != assessor_manifest.manifest_sha256:
        raise ValueError("selection assessment batch changed its assessor binding")
    campaigns = tuple(item.committed_prediction.campaign for item in request.candidates)
    _validate_assessor_independence(assessor=assessor_manifest, campaigns=campaigns)
    first_snapshot = _source_snapshot(campaigns[0])
    first_source = campaigns[0].source_causal_campaign.source_campaign
    expected_request = {
        "question_sha256": first_snapshot.question.question_sha256,
        "world_model_snapshot_sha256": first_snapshot.snapshot_sha256,
        "belief_state_sha256": first_snapshot.belief_state.belief_state_sha256,
        "source_hypothesis_campaign_sha256": first_source.campaign_sha256,
    }
    for field_name, expected_value in expected_request.items():
        if getattr(request, field_name) != expected_value:
            raise ValueError(f"selection request changed exact {field_name} binding")
    assessments = {item.candidate_id: item for item in request.assessment_batch.assessments}
    if set(assessments) != {item.candidate_id for item in request.candidates}:
        raise ValueError("experiment assessments must cover every candidate exactly once")
    for candidate in request.candidates:
        committed = candidate.committed_prediction
        campaign = committed.campaign
        snapshot = _source_snapshot(campaign)
        source = campaign.source_causal_campaign.source_campaign
        if (
            snapshot.snapshot_sha256 != first_snapshot.snapshot_sha256
            or snapshot.belief_state.belief_state_sha256
            != first_snapshot.belief_state.belief_state_sha256
            or source.campaign_sha256 != first_source.campaign_sha256
        ):
            raise ValueError("selection candidates must share one exact world model and prior")
        if committed.committed_at > request.issued_at:
            raise ValueError("selection request predates a candidate prediction commitment")
        assessment = assessments[candidate.candidate_id]
        process = _measurement_process(campaign)
        expected_assessment = {
            "prediction_campaign_sha256": campaign.campaign_sha256,
            "prediction_commitment_sha256": campaign.commitment_sha256,
            "experiment_protocol_sha256": campaign.request.experiment_protocol.protocol_sha256,
            "measurement_process_sha256": process.process_sha256,
            "measurement_error_model_sha256": process.error_model_sha256,
            "assessor_manifest_sha256": assessor_manifest.manifest_sha256,
        }
        for field_name, expected_value in expected_assessment.items():
            if getattr(assessment, field_name) != expected_value:
                raise ValueError(f"candidate assessment changed exact {field_name} binding")
        if assessment.completed_at > request.assessment_batch.completed_at:
            raise ValueError("assessment batch predates a candidate assessment")
    if (
        policy.frozen_at > request.issued_at
        or assessor_manifest.frozen_at > request.issued_at
        or request.assessment_batch.completed_at > request.issued_at
    ):
        raise ValueError("selection request predates a frozen dependency")
    if request.assessment_batch.completed_at < assessor_manifest.frozen_at:
        raise ValueError("experiment assessments predate the frozen assessor")


def build_experiment_selection_request(
    *,
    selection_id: str,
    candidates: tuple[ExperimentCandidate, ...],
    assessment_batch: ExperimentAssessmentBatch,
    assessor_manifest: ExperimentAssessmentManifest,
    policy: ExperimentSelectionPolicy,
    prediction_archive_custody_sha256: str,
    issued_at: datetime,
) -> ExperimentSelectionRequest:
    if not candidates:
        raise ValueError("experiment selection requires candidates")
    snapshot = _source_snapshot(candidates[0].committed_prediction.campaign)
    source = candidates[0].committed_prediction.campaign.source_causal_campaign.source_campaign
    request = ExperimentSelectionRequest(
        selection_id=selection_id,
        question_sha256=snapshot.question.question_sha256,
        world_model_snapshot_sha256=snapshot.snapshot_sha256,
        belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        source_hypothesis_campaign_sha256=source.campaign_sha256,
        candidates=candidates,
        assessment_batch=assessment_batch,
        assessor_manifest_sha256=assessor_manifest.manifest_sha256,
        policy_sha256=policy.policy_sha256,
        prediction_archive_custody_sha256=prediction_archive_custody_sha256,
        issued_at=issued_at,
    )
    _validate_selection_request(
        policy=policy,
        assessor_manifest=assessor_manifest,
        request=request,
    )
    return request


def _entropy(probabilities: tuple[float, ...]) -> float:
    return -math.fsum(value * math.log(value) for value in probabilities if value > 0.0)


def _probability_map(campaign: PredictionCommitmentCampaign) -> dict[str, dict[str, float]]:
    if campaign.prediction_batch is None:
        return {}
    return {
        item.hypothesis_id: {
            probability.bin_id: probability.probability for probability in item.probabilities
        }
        for item in campaign.prediction_batch.predictions
    }


def _derive_information_audit(
    *,
    candidate_id: str,
    campaign: PredictionCommitmentCampaign,
) -> ExpectedInformationGainAudit:
    if (
        campaign.disposition is not PredictionCommitmentDisposition.READY
        or not campaign.eig_eligible
        or campaign.request.prediction_mode is not PredictionMode.PROBABILISTIC
        or campaign.prediction_batch is None
    ):
        raise ValueError("EIG requires a ready probabilistic prediction commitment")
    snapshot = _source_snapshot(campaign)
    priors = {item.hypothesis_id: item.probability for item in snapshot.belief_state.hypotheses}
    versions = {
        item.hypothesis_id: item.hypothesis_version_sha256
        for item in snapshot.belief_state.hypotheses
    }
    likelihoods = _probability_map(campaign)
    hypothesis_ids = tuple(sorted(priors))
    bin_ids = tuple(sorted(item.bin_id for item in campaign.request.outcome_schema.bins))
    prior_entropy_raw = _entropy(tuple(priors[item] for item in hypothesis_ids))
    outcomes: list[ExpectedOutcomePosterior] = []
    expected_posterior_entropy_raw = 0.0
    for bin_id in bin_ids:
        marginal = math.fsum(
            priors[hypothesis_id] * likelihoods[hypothesis_id][bin_id]
            for hypothesis_id in hypothesis_ids
        )
        if marginal > 0.0:
            posterior_values = tuple(
                priors[hypothesis_id] * likelihoods[hypothesis_id][bin_id] / marginal
                for hypothesis_id in hypothesis_ids
            )
        else:
            posterior_values = tuple(0.0 for _ in hypothesis_ids)
        posterior_entropy_raw = _entropy(posterior_values)
        expected_posterior_entropy_raw += marginal * posterior_entropy_raw
        outcomes.append(
            ExpectedOutcomePosterior(
                bin_id=bin_id,
                marginal_probability=marginal,
                hypothesis_posteriors=tuple(
                    HypothesisPosteriorProbability(
                        hypothesis_id=hypothesis_id,
                        hypothesis_version_sha256=versions[hypothesis_id],
                        probability=posterior,
                    )
                    for hypothesis_id, posterior in zip(
                        hypothesis_ids, posterior_values, strict=True
                    )
                ),
                posterior_entropy_nats=round(posterior_entropy_raw, _METRIC_DIGITS),
            )
        )
    eig_raw = max(0.0, prior_entropy_raw - expected_posterior_entropy_raw)
    eig_ratio = min(1.0, max(0.0, eig_raw / prior_entropy_raw)) if prior_entropy_raw > 0.0 else 0.0
    pairwise_tv: list[float] = []
    for index, left_id in enumerate(hypothesis_ids):
        for right_id in hypothesis_ids[index + 1 :]:
            pairwise_tv.append(
                0.5
                * math.fsum(
                    abs(likelihoods[left_id][bin_id] - likelihoods[right_id][bin_id])
                    for bin_id in bin_ids
                )
            )
    return ExpectedInformationGainAudit(
        candidate_id=candidate_id,
        prediction_commitment_sha256=campaign.commitment_sha256,
        belief_state_sha256=snapshot.belief_state.belief_state_sha256,
        prior_entropy_nats=round(prior_entropy_raw, _METRIC_DIGITS),
        expected_posterior_entropy_nats=round(expected_posterior_entropy_raw, _METRIC_DIGITS),
        expected_information_gain_nats=round(eig_raw, _METRIC_DIGITS),
        expected_information_gain_ratio=round(eig_ratio, _METRIC_DIGITS),
        minimum_pairwise_total_variation=round(min(pairwise_tv), _METRIC_DIGITS),
        maximum_pairwise_total_variation=round(max(pairwise_tv), _METRIC_DIGITS),
        outcome_posteriors=tuple(outcomes),
    )


def _fresh_confirmation(
    *,
    campaign: PredictionCommitmentCampaign,
    assessment: CandidateExperimentAssessment,
    request: ExperimentSelectionRequest,
) -> tuple[int, tuple[str, ...]]:
    blockers: list[str] = []
    report = campaign.request.calibration_report
    calibration_split = report.validation_split_sha256 if report is not None else None
    target_namespace = campaign.request.experiment_protocol.experiment_namespace_sha256
    valid = 0
    for reservation in assessment.fresh_confirmation_batches:
        if reservation.partition_sha256 == calibration_split:
            blockers.append(f"fresh_confirmation:reuses_calibration:{reservation.batch_sha256}")
        elif reservation.partition_sha256 == target_namespace:
            blockers.append(
                f"fresh_confirmation:reuses_target_namespace:{reservation.batch_sha256}"
            )
        elif reservation.available_until <= request.issued_at:
            blockers.append(f"fresh_confirmation:expired:{reservation.batch_sha256}")
        elif reservation.sealed_at > assessment.completed_at:
            blockers.append(
                f"fresh_confirmation:sealed_after_assessment:{reservation.batch_sha256}"
            )
        else:
            valid += 1
    return valid, tuple(blockers)


def _candidate_score(
    *,
    policy: ExperimentSelectionPolicy,
    request: ExperimentSelectionRequest,
    candidate: ExperimentCandidate,
    assessment: CandidateExperimentAssessment,
    verification: PredictionArchiveVerification,
) -> ExperimentCandidateScore:
    campaign = candidate.committed_prediction.campaign
    blockers: list[str] = []
    information: ExpectedInformationGainAudit | None = None
    if campaign.disposition is not PredictionCommitmentDisposition.READY:
        blockers.append(f"prediction:not_ready:{campaign.disposition.value}")
    elif (
        not campaign.eig_eligible
        or campaign.request.prediction_mode is not PredictionMode.PROBABILISTIC
    ):
        blockers.append("prediction:not_eig_eligible")
    else:
        information = _derive_information_audit(
            candidate_id=candidate.candidate_id,
            campaign=campaign,
        )
        if (
            information.expected_information_gain_ratio
            < policy.minimum_expected_information_gain_ratio
        ):
            blockers.append("information:eig_below_floor")
        if information.minimum_pairwise_total_variation < policy.minimum_pairwise_total_variation:
            blockers.append("information:pairwise_discrimination_below_floor")
    if assessment.cost_currency != policy.budget_currency:
        blockers.append("cost:currency_mismatch")
    if assessment.estimated_cost_microunits > policy.budget_microunits:
        blockers.append("cost:budget_exceeded")
    if assessment.estimated_duration_seconds > policy.maximum_duration_seconds:
        blockers.append("duration:limit_exceeded")
    if _RISK_ORDER[assessment.risk_level] > _RISK_ORDER[policy.maximum_risk_level]:
        blockers.append("risk:level_exceeded")
    if assessment.risk_level is ExperimentRiskLevel.PROHIBITED:
        blockers.append("risk:prohibited")
    if assessment.measurement_validity_status is not MeasurementValidityStatus.VALIDATED:
        blockers.append(f"measurement:not_validated:{assessment.measurement_validity_status.value}")
    if assessment.measurement_validity_confidence < policy.minimum_measurement_validity_confidence:
        blockers.append("measurement:confidence_below_floor")
    if assessment.proxy_risk_status is not ProxyRiskStatus.NONE:
        blockers.append(f"measurement:proxy_risk:{assessment.proxy_risk_status.value}")
    missing_capabilities = sorted(
        set(assessment.required_capability_sha256s) - set(assessment.available_capability_sha256s)
    )
    blockers.extend(f"capability:missing:{item}" for item in missing_capabilities)
    valid_fresh, fresh_blockers = _fresh_confirmation(
        campaign=campaign,
        assessment=assessment,
        request=request,
    )
    blockers.extend(fresh_blockers)
    if valid_fresh < policy.minimum_fresh_confirmation_batches:
        blockers.append("fresh_confirmation:insufficient")
    cost_ratio = assessment.estimated_cost_microunits / policy.budget_microunits
    duration_ratio = assessment.estimated_duration_seconds / policy.maximum_duration_seconds
    fresh_score = min(valid_fresh / policy.confirmation_saturation_batches, 1.0)
    replication_score = (
        assessment.expected_replication_debt_reduction / assessment.replication_debt_before
        if assessment.replication_debt_before > 0
        else 0.0
    )
    canonical_blockers = tuple(sorted(set(blockers)))
    utility: float | None = None
    if information is not None and not canonical_blockers:
        weights = policy.utility_weights
        utility = round(
            weights.expected_information_gain * information.expected_information_gain_ratio
            + weights.minimum_pairwise_discrimination * information.minimum_pairwise_total_variation
            + weights.fresh_confirmation * fresh_score
            + weights.replication_debt_reduction * replication_score
            - weights.cost_penalty * cost_ratio
            - weights.duration_penalty * duration_ratio
            - weights.risk_penalty * _RISK_BURDEN[assessment.risk_level],
            _METRIC_DIGITS,
        )
    return ExperimentCandidateScore(
        candidate_id=candidate.candidate_id,
        prediction_commitment_sha256=campaign.commitment_sha256,
        assessment_sha256=assessment.assessment_sha256,
        archive_verification_sha256=verification.verification_sha256,
        information_audit=information,
        cost_budget_ratio=round(cost_ratio, _METRIC_DIGITS),
        duration_limit_ratio=round(duration_ratio, _METRIC_DIGITS),
        risk_burden=_RISK_BURDEN[assessment.risk_level],
        fresh_confirmation_score=round(fresh_score, _METRIC_DIGITS),
        valid_fresh_confirmation_batches=valid_fresh,
        replication_debt_reduction_score=round(replication_score, _METRIC_DIGITS),
        replication_debt_after=(
            assessment.replication_debt_before - assessment.expected_replication_debt_reduction
        ),
        constrained_utility=utility,
        feasible=not canonical_blockers,
        blockers=canonical_blockers,
    )


def _ranking_key(score: ExperimentCandidateScore) -> tuple[object, ...]:
    information = score.information_audit
    return (
        0 if score.feasible else 1,
        -(score.constrained_utility if score.constrained_utility is not None else -math.inf),
        -(information.expected_information_gain_nats if information is not None else -math.inf),
        score.cost_budget_ratio,
        score.duration_limit_ratio,
        score.candidate_id,
    )


def _derive_selection(
    *,
    policy: ExperimentSelectionPolicy,
    request: ExperimentSelectionRequest,
    verifications: tuple[PredictionArchiveVerification, ...],
) -> _DerivedSelection:
    assessments = {item.candidate_id: item for item in request.assessment_batch.assessments}
    verification_by_id = {item.candidate_id: item for item in verifications}
    scores = tuple(
        _candidate_score(
            policy=policy,
            request=request,
            candidate=candidate,
            assessment=assessments[candidate.candidate_id],
            verification=verification_by_id[candidate.candidate_id],
        )
        for candidate in request.candidates
    )
    ordered = sorted(scores, key=_ranking_key)
    selected = next((item for item in ordered if item.feasible), None)
    rankings: list[ExperimentCandidateRanking] = []
    for index, score in enumerate(ordered, start=1):
        if selected is not None and score.candidate_id == selected.candidate_id:
            disposition = CandidateSelectionDisposition.SELECTED
            reasons = ("highest_constrained_utility",)
        elif score.feasible:
            disposition = CandidateSelectionDisposition.FEASIBLE_NOT_SELECTED
            reasons = ("lower_constrained_utility",)
        else:
            disposition = CandidateSelectionDisposition.INFEASIBLE
            reasons = score.blockers
        rankings.append(
            ExperimentCandidateRanking(
                rank=index,
                candidate_id=score.candidate_id,
                candidate_score_sha256=score.score_sha256,
                disposition=disposition,
                reasons=tuple(sorted(reasons)),
            )
        )
    disposition = (
        ExperimentSelectionDisposition.READY_SELECTED
        if selected is not None
        else ExperimentSelectionDisposition.NO_FEASIBLE_EXPERIMENT
    )
    decision = ExperimentSelectionDecision(
        selected_candidate_id=selected.candidate_id if selected is not None else None,
        rankings=tuple(rankings),
        disposition=disposition,
    )
    return _DerivedSelection(candidate_scores=scores, decision=decision)


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("experiment selector clock must return a timezone-aware timestamp")
    return value


def _selection_failure(
    *,
    candidate_id: str,
    error: Exception,
    occurred_at: datetime,
) -> ExperimentSelectionFailure:
    return ExperimentSelectionFailure(
        kind=ExperimentSelectionFailureKind.PREDICTION_ARCHIVE_INVALID,
        failed_candidate_id=candidate_id,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        occurred_at=occurred_at,
    )


def run_experiment_selection(
    *,
    campaign_id: str,
    policy: ExperimentSelectionPolicy,
    assessor_manifest: ExperimentAssessmentManifest,
    request: ExperimentSelectionRequest,
    prediction_archive: ContentAddressedResponseArchive,
    clock: Callable[[], datetime] | None = None,
) -> ExperimentSelectionCampaign:
    """Rehash every candidate commitment, then score and rank without observations."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    _validate_selection_request(
        policy=policy,
        assessor_manifest=assessor_manifest,
        request=request,
    )
    verifications: list[PredictionArchiveVerification] = []
    for candidate in request.candidates:
        committed = candidate.committed_prediction
        try:
            loaded = load_prediction_commitment_campaign(
                archive=prediction_archive,
                ledger=committed.ledger,
            )
            if loaded != committed.campaign:
                raise ValueError("embedded prediction campaign differs from archived bytes")
        except (ResponseArchiveError, ValidationError, ValueError, TypeError) as exc:
            occurred_at = _now(clock)
            failure = _selection_failure(
                candidate_id=candidate.candidate_id,
                error=exc,
                occurred_at=occurred_at,
            )
            return ExperimentSelectionCampaign(
                campaign_id=campaign_id,
                policy=policy,
                assessor_manifest=assessor_manifest,
                request=request,
                archive_verifications=(),
                candidate_scores=(),
                failure=failure,
                blockers=(f"execution_failure:{failure.kind.value}",),
                disposition=ExperimentSelectionDisposition.BLOCKED_EXECUTION,
                generated_at=_now(clock),
            )
        verifications.append(
            PredictionArchiveVerification(
                candidate_id=candidate.candidate_id,
                prediction_campaign_sha256=loaded.campaign_sha256,
                prediction_commitment_sha256=loaded.commitment_sha256,
                commitment_receipt_sha256=committed.receipt_sha256,
                ledger_receipt_sha256=committed.ledger.receipt_sha256,
                archive_custody_sha256=request.prediction_archive_custody_sha256,
                verified_at=_now(clock),
            )
        )
    frozen_verifications = tuple(verifications)
    derived = _derive_selection(
        policy=policy,
        request=request,
        verifications=frozen_verifications,
    )
    return ExperimentSelectionCampaign(
        campaign_id=campaign_id,
        policy=policy,
        assessor_manifest=assessor_manifest,
        request=request,
        archive_verifications=frozen_verifications,
        candidate_scores=derived.candidate_scores,
        decision=derived.decision,
        blockers=(),
        disposition=derived.decision.disposition,
        generated_at=_now(clock),
    )


def commit_experiment_selection_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: ExperimentSelectionCampaign,
    committed_at: datetime,
) -> CommittedExperimentSelectionCampaign:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("experiment selection commitment time must be timezone-aware")
    if committed_at < campaign.generated_at:
        raise ValueError("experiment selection commitment cannot predate campaign generation")
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=committed_at,
    )
    return CommittedExperimentSelectionCampaign(
        campaign=campaign,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_experiment_selection_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> ExperimentSelectionCampaign:
    payload = archive.read_ledger(ledger)
    campaign = ExperimentSelectionCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived experiment selection campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived experiment selection campaign changed object identity")
    return campaign


__all__ = [
    "EXPERIMENT_ASSESSMENT_OUTPUT_SCHEMA_SHA256",
    "CandidateExperimentAssessment",
    "CandidateSelectionDisposition",
    "CommittedExperimentSelectionCampaign",
    "ExpectedInformationGainAudit",
    "ExpectedOutcomePosterior",
    "ExperimentAssessmentBatch",
    "ExperimentAssessmentManifest",
    "ExperimentCandidate",
    "ExperimentCandidateRanking",
    "ExperimentCandidateScore",
    "ExperimentRiskLevel",
    "ExperimentSelectionCampaign",
    "ExperimentSelectionDecision",
    "ExperimentSelectionDisposition",
    "ExperimentSelectionFailure",
    "ExperimentSelectionFailureKind",
    "ExperimentSelectionPolicy",
    "ExperimentSelectionRequest",
    "FreshConfirmationBatch",
    "HypothesisPosteriorProbability",
    "MeasurementValidityStatus",
    "PredictionArchiveVerification",
    "ProxyRiskStatus",
    "SelectionUtilityWeights",
    "build_experiment_selection_request",
    "commit_experiment_selection_campaign",
    "load_experiment_selection_campaign",
    "run_experiment_selection",
]
