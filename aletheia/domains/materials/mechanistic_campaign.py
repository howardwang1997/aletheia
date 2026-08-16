"""F10-S6 fail-closed mechanistic campaign composition and evidence scoring."""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.registry import CapabilityRegistrySnapshot
from aletheia.capabilities.schemas import (
    CapabilityActionType,
    CapabilityClaimType,
    CapabilityEvidenceLevel,
    CapabilityLifecycle,
    CapabilityRole,
    ExperimentCapabilityManifest,
    evidence_level_rank,
)
from aletheia.capabilities.validators import (
    CapabilityObservationDisposition,
    CommittedCapabilityObservationPipeline,
)
from aletheia.epistemics.causal import (
    CausalAuditCampaign,
    CausalClaimCeiling,
)
from aletheia.epistemics.hypotheses import HypothesisGenerationDisposition
from aletheia.epistemics.prediction import (
    PredictionCommitmentCampaign,
    PredictionCommitmentDisposition,
    PredictionMode,
)
from aletheia.epistemics.schemas import HypothesisRole, ResearchQuestionKind
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ADAPTER_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$"
_VALIDATED_DISPOSITIONS = {
    CapabilityObservationDisposition.VALIDATED_POSITIVE,
    CapabilityObservationDisposition.VALIDATED_NEGATIVE,
    CapabilityObservationDisposition.VALIDATED_INCONCLUSIVE,
}


class MechanisticExperimentFamily(str, Enum):
    MEASUREMENT_AUDIT = "c1_measurement_audit"
    STRUCTURE_DISCRIMINATION = "c2_structure_discrimination"
    STRUCTURAL_INTERVENTION = "c3_structural_intervention"
    SIMULATION = "c4_simulation"


_FAMILY_ACTION = {
    MechanisticExperimentFamily.MEASUREMENT_AUDIT: CapabilityActionType.DATA_AUDIT,
    MechanisticExperimentFamily.STRUCTURE_DISCRIMINATION: (
        CapabilityActionType.COMPUTATIONAL_EXPERIMENT
    ),
    MechanisticExperimentFamily.STRUCTURAL_INTERVENTION: (
        CapabilityActionType.STRUCTURAL_INTERVENTION
    ),
    MechanisticExperimentFamily.SIMULATION: CapabilityActionType.SIMULATION,
}


class MechanisticCapabilityQualification(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_capability_qualification"] = (
        "aletheia.mechanistic_capability_qualification"
    )
    schema_version: Literal[1] = 1
    qualification_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    family: MechanisticExperimentFamily
    expected_action: CapabilityActionType
    qualification_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    domain_reviewer_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _action_matches_family(self) -> "MechanisticCapabilityQualification":
        if self.expected_action is not _FAMILY_ACTION[self.family]:
            raise ValueError("mechanistic qualification action does not match its family")
        return self

    @property
    def qualification_sha256(self) -> str:
        return content_sha256(self)


class MechanisticEvidenceRole(str, Enum):
    INTERNAL_DISCRIMINATION = "internal_discrimination"
    FRESH_CONFIRMATION = "fresh_confirmation"


class ConfirmationIndependenceKind(str, Enum):
    FRESH_DATASET = "fresh_dataset"
    INDEPENDENT_IMPLEMENTATION = "independent_implementation"
    EXTERNAL_SITE = "external_site"


class MechanisticCampaignDisposition(str, Enum):
    INVALID_EVIDENCE = "invalid_evidence"
    INCONCLUSIVE = "inconclusive"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    NULL_SUPPORTED = "null_supported"
    BOUNDED_PATTERN_SUPPORTED = "bounded_pattern_supported"
    WITHIN_MODEL_MECHANISM_SUPPORTED = "within_model_mechanism_supported"
    MECHANISM_CANDIDATE_SUPPORTED = "mechanism_candidate_supported"


class MechanisticClaimCeiling(str, Enum):
    NONE = "none"
    DESCRIPTIVE_PATTERN = "descriptive_pattern"
    WITHIN_MODEL_MECHANISM_CANDIDATE = "within_model_mechanism_candidate"
    MECHANISM_CANDIDATE = "mechanism_candidate"


class FreshConfirmationReservation(FrozenModel):
    schema_name: Literal["aletheia.fresh_confirmation_reservation"] = (
        "aletheia.fresh_confirmation_reservation"
    )
    schema_version: Literal[1] = 1
    reservation_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    independence_kind: ConfirmationIndependenceKind
    data_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    custody_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_access_before_freeze: Literal["none"] = "none"
    reserved_at: AwareDatetime
    state: Literal["reserved_before_observation"] = "reserved_before_observation"

    @property
    def reservation_sha256(self) -> str:
        return content_sha256(self)


class MechanisticDecisionPolicy(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_decision_policy"] = (
        "aletheia.mechanistic_decision_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    minimum_distinct_experiment_families: Literal[2] = 2
    minimum_probability_margin: float = Field(ge=0.0, le=1.0)
    require_probabilistic_predictions: Literal[True] = True
    require_all_slots_valid: Literal[True] = True
    require_robust_winner_across_sensitivity: Literal[True] = True
    require_fresh_confirmation: Literal[True] = True
    require_independent_confirmation: Literal[True] = True
    require_registered_capabilities_for_release: Literal[True] = True
    require_confirmatory_observations_for_release: Literal[True] = True
    joint_posterior_forbidden: Literal[True] = True
    aggregation_rule: Literal[
        "concordant_per_slot_robust_winner_without_joint_pseudoreplication"
    ] = "concordant_per_slot_robust_winner_without_joint_pseudoreplication"
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _margin_is_nontrivial(self) -> "MechanisticDecisionPolicy":
        if not math.isfinite(self.minimum_probability_margin):
            raise ValueError("mechanistic probability margin must be finite")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class MechanisticExperimentSlot(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_experiment_slot"] = (
        "aletheia.mechanistic_experiment_slot"
    )
    schema_version: Literal[1] = 1
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    family: MechanisticExperimentFamily
    evidence_role: MechanisticEvidenceRole
    capability_manifest: ExperimentCapabilityManifest
    family_qualification: MechanisticCapabilityQualification
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    data_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    implementation_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    maximum_cost_usd: float = Field(ge=0.0)
    maximum_wall_time_seconds: int = Field(gt=0)
    confirmation_reservation: FreshConfirmationReservation | None = None
    observation_access_before_freeze: Literal["none"] = "none"
    planned_at: AwareDatetime
    state: Literal["frozen_before_observation"] = "frozen_before_observation"

    @model_validator(mode="after")
    def _slot_matches_capability_and_confirmation_role(self) -> "MechanisticExperimentSlot":
        if self.capability_manifest.domain != "materials":
            raise ValueError("mechanistic materials slot requires a materials capability")
        if self.capability_manifest.action_type is not _FAMILY_ACTION[self.family]:
            raise ValueError("mechanistic experiment family does not match capability action")
        qualification = self.family_qualification
        if (
            qualification.capability_manifest_sha256 != self.capability_manifest.manifest_sha256
            or qualification.family is not self.family
        ):
            raise ValueError("mechanistic family qualification changed its slot binding")
        if qualification.domain_reviewer_principal_sha256 in {
            role.principal_sha256 for role in self.capability_manifest.roles
        }:
            raise ValueError("mechanistic family reviewer must be capability-role independent")
        if (
            qualification.frozen_at < self.capability_manifest.frozen_at
            or qualification.frozen_at > self.planned_at
        ):
            raise ValueError("mechanistic family qualification chronology is invalid")
        executor = next(
            role for role in self.capability_manifest.roles if role.role is CapabilityRole.EXECUTOR
        )
        if self.implementation_identity_sha256 != executor.implementation_sha256:
            raise ValueError(
                "mechanistic implementation identity must bind the capability executor"
            )
        if not math.isfinite(self.maximum_cost_usd):
            raise ValueError("mechanistic slot cost must be finite")
        fresh = self.evidence_role is MechanisticEvidenceRole.FRESH_CONFIRMATION
        if fresh != (self.confirmation_reservation is not None):
            raise ValueError("fresh-confirmation slot requires exactly one reservation")
        if self.confirmation_reservation is not None:
            reservation = self.confirmation_reservation
            if (
                reservation.data_identity_sha256 != self.data_identity_sha256
                or reservation.implementation_identity_sha256 != self.implementation_identity_sha256
                or reservation.reserved_at > self.planned_at
            ):
                raise ValueError("fresh-confirmation reservation differs from the slot")
        if self.capability_manifest.frozen_at > self.planned_at:
            raise ValueError("mechanistic slot predates its capability manifest")
        return self

    @property
    def slot_sha256(self) -> str:
        return content_sha256(self)


def _scientific_principals(
    causal: CausalAuditCampaign,
    predictions: tuple[PredictionCommitmentCampaign, ...],
    slots: tuple[MechanisticExperimentSlot, ...],
) -> set[str]:
    source = causal.source_campaign
    principals = {
        source.generator_manifest.generator_principal_sha256,
        source.deduplicator_manifest.reviewer_principal_sha256,
        causal.author_manifest.author_principal_sha256,
        causal.reviewer_manifest.reviewer_principal_sha256,
    }
    for prediction in predictions:
        principals.update(
            {
                prediction.author_manifest.author_principal_sha256,
                prediction.calibration_evaluator_manifest.evaluator_principal_sha256,
            }
        )
    for slot in slots:
        principals.update(role.principal_sha256 for role in slot.capability_manifest.roles)
        principals.add(slot.family_qualification.domain_reviewer_principal_sha256)
        if slot.confirmation_reservation is not None:
            principals.add(slot.confirmation_reservation.custody_principal_sha256)
    return principals


def _derive_protocol_gates(
    *,
    causal: CausalAuditCampaign,
    predictions: tuple[PredictionCommitmentCampaign, ...],
    slots: tuple[MechanisticExperimentSlot, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    execution: set[str] = set()
    release: set[str] = set()
    source = causal.source_campaign
    snapshot = source.world_model_snapshot
    if not source.direction_gate.experiment_authorized:
        execution.add("f8_direction_not_authorized")
    if source.disposition is not HypothesisGenerationDisposition.READY or snapshot is None:
        execution.add("f9_hypothesis_campaign_not_ready")
    elif snapshot.question.kind not in {
        ResearchQuestionKind.MECHANISM,
        ResearchQuestionKind.CAUSAL_EFFECT,
    }:
        execution.add("research_question_not_mechanistic")
    if not causal.prediction_planning_authorized:
        execution.add("f9_causal_campaign_not_prediction_authorized")
    families = {slot.family for slot in slots}
    if len(families) < 2:
        execution.add("fewer_than_two_experiment_families")
    if not families.intersection(
        {
            MechanisticExperimentFamily.STRUCTURAL_INTERVENTION,
            MechanisticExperimentFamily.SIMULATION,
        }
    ):
        execution.add("no_intervention_or_simulation_family")
    campaign_by_hash = {item.campaign_sha256: item for item in predictions}
    for slot in slots:
        prediction = campaign_by_hash.get(slot.prediction_campaign_sha256)
        if prediction is None:
            execution.add(f"prediction_campaign_missing:{slot.slot_id}")
            continue
        if (
            prediction.disposition is not PredictionCommitmentDisposition.READY
            or not prediction.eig_eligible
            or prediction.prediction_batch is None
        ):
            execution.add(f"prediction_campaign_not_ready:{slot.slot_id}")
        if prediction.generated_at > slot.planned_at:
            execution.add(f"slot_predates_prediction_commitment:{slot.slot_id}")
        if prediction.request.prediction_mode is not PredictionMode.PROBABILISTIC:
            execution.add(f"prediction_not_probabilistic:{slot.slot_id}")

        manifest = slot.capability_manifest
        if manifest.lifecycle is not CapabilityLifecycle.REGISTERED:
            release.add(f"capability_not_registered:{slot.slot_id}")
        if evidence_level_rank(manifest.maximum_evidence_level) < evidence_level_rank(
            CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL
        ):
            release.add(f"capability_evidence_level_insufficient:{slot.slot_id}")

    mechanism_claim_types = {
        CapabilityClaimType.WITHIN_MODEL_CAUSAL,
        CapabilityClaimType.MECHANISM_CANDIDATE,
        CapabilityClaimType.EXPERIMENTAL_CAUSAL,
    }
    if not any(
        mechanism_claim_types.intersection(slot.capability_manifest.claim_types_supported)
        for slot in slots
    ):
        release.add("mechanism_capable_claim_type_missing")

    fresh_slots = [
        slot for slot in slots if slot.evidence_role is MechanisticEvidenceRole.FRESH_CONFIRMATION
    ]
    if not fresh_slots:
        release.add("fresh_confirmation_missing")
    for fresh in fresh_slots:
        reservation = fresh.confirmation_reservation
        assert reservation is not None
        other_slots = [slot for slot in slots if slot.slot_id != fresh.slot_id]
        if reservation.independence_kind is ConfirmationIndependenceKind.FRESH_DATASET and any(
            item.data_identity_sha256 == fresh.data_identity_sha256 for item in other_slots
        ):
            release.add(f"fresh_dataset_identity_reused:{fresh.slot_id}")
        if (
            reservation.independence_kind is ConfirmationIndependenceKind.INDEPENDENT_IMPLEMENTATION
            and any(
                item.implementation_identity_sha256 == fresh.implementation_identity_sha256
                for item in other_slots
            )
        ):
            release.add(f"independent_implementation_identity_reused:{fresh.slot_id}")
        if reservation.independence_kind is ConfirmationIndependenceKind.EXTERNAL_SITE:
            other_principals = {
                role.principal_sha256
                for item in other_slots
                for role in item.capability_manifest.roles
            }
            other_principals.update(
                item.confirmation_reservation.custody_principal_sha256
                for item in other_slots
                if item.confirmation_reservation is not None
            )
            if reservation.custody_principal_sha256 in other_principals:
                release.add(f"external_site_custody_reused:{fresh.slot_id}")
    if causal.claim_ceiling in {
        CausalClaimCeiling.NONE,
        CausalClaimCeiling.DESCRIPTIVE_ONLY,
        CausalClaimCeiling.ASSOCIATION_ONLY,
    }:
        release.add("causal_claim_ceiling_too_low")
    release.update(f"execution:{item}" for item in execution)
    return tuple(sorted(execution)), tuple(sorted(release))


class MechanisticCampaignProtocol(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_campaign_protocol"] = (
        "aletheia.mechanistic_campaign_protocol"
    )
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    capability_registry: CapabilityRegistrySnapshot
    causal_campaign: CausalAuditCampaign
    prediction_campaigns: tuple[PredictionCommitmentCampaign, ...] = Field(min_length=2)
    slots: tuple[MechanisticExperimentSlot, ...] = Field(min_length=2)
    policy: MechanisticDecisionPolicy
    evaluator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_blockers: tuple[str, ...]
    mechanism_release_blockers: tuple[str, ...]
    execution_authorized: bool
    mechanism_release_eligible: bool
    observation_access_before_freeze: Literal["none"] = "none"
    frozen_at: AwareDatetime
    state: Literal["frozen_before_observation"] = "frozen_before_observation"

    @model_validator(mode="after")
    def _protocol_is_lineage_closed_and_gate_derived(self) -> "MechanisticCampaignProtocol":
        prediction_hashes = tuple(item.campaign_sha256 for item in self.prediction_campaigns)
        if prediction_hashes != tuple(sorted(set(prediction_hashes))):
            raise ValueError("prediction campaigns must be unique and canonically ordered")
        slot_ids = tuple(item.slot_id for item in self.slots)
        if slot_ids != tuple(sorted(set(slot_ids))):
            raise ValueError("mechanistic slots must be unique and canonically ordered")
        if {item.prediction_campaign_sha256 for item in self.slots} != set(prediction_hashes):
            raise ValueError("every prediction campaign must bind exactly one mechanistic slot")
        if len({item.prediction_campaign_sha256 for item in self.slots}) != len(self.slots):
            raise ValueError("mechanistic slots cannot reuse a prediction campaign")
        namespaces = tuple(
            item.request.experiment_protocol.experiment_namespace_sha256
            for item in self.prediction_campaigns
        )
        if len(namespaces) != len(set(namespaces)):
            raise ValueError("mechanistic slots require distinct experiment namespaces")
        if any(
            item.source_causal_campaign != self.causal_campaign
            for item in self.prediction_campaigns
        ):
            raise ValueError("prediction campaign changed the source causal campaign")
        if any(self.capability_registry.created_at > item.planned_at for item in self.slots):
            raise ValueError("mechanistic slot was planned before the registry snapshot")
        if any(self.policy.frozen_at > item.planned_at for item in self.slots):
            raise ValueError("mechanistic slot was planned before the decision policy")
        registry_manifest_hashes = {
            item.manifest_sha256 for item in self.capability_registry.manifests
        }
        if any(
            item.capability_manifest.manifest_sha256 not in registry_manifest_hashes
            for item in self.slots
        ):
            raise ValueError("mechanistic slot capability is absent from the frozen registry")
        latest = max(
            self.capability_registry.created_at,
            self.policy.frozen_at,
            self.causal_campaign.generated_at,
            *(item.generated_at for item in self.prediction_campaigns),
            *(item.planned_at for item in self.slots),
        )
        if self.frozen_at < latest:
            raise ValueError("mechanistic protocol predates a frozen source or slot")
        if self.evaluator_principal_sha256 in _scientific_principals(
            self.causal_campaign, self.prediction_campaigns, self.slots
        ):
            raise ValueError("mechanistic evaluator must be independent of prior campaign roles")
        execution, release = _derive_protocol_gates(
            causal=self.causal_campaign,
            predictions=self.prediction_campaigns,
            slots=self.slots,
        )
        if (
            self.execution_blockers != execution
            or self.mechanism_release_blockers != release
            or self.execution_authorized != (not execution)
            or self.mechanism_release_eligible != (not release)
        ):
            raise ValueError("mechanistic protocol gates are not mechanically derived")
        return self

    @property
    def direction_gate_sha256(self) -> str:
        return self.causal_campaign.source_campaign.direction_gate.gate_sha256

    @property
    def registry_snapshot_sha256(self) -> str:
        return self.capability_registry.snapshot_sha256

    @property
    def hypothesis_campaign_sha256(self) -> str:
        return self.causal_campaign.source_campaign.campaign_sha256

    @property
    def causal_campaign_sha256(self) -> str:
        return self.causal_campaign.campaign_sha256

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


def build_mechanistic_campaign_protocol(
    *,
    protocol_id: str,
    capability_registry: CapabilityRegistrySnapshot,
    causal_campaign: CausalAuditCampaign,
    prediction_campaigns: tuple[PredictionCommitmentCampaign, ...],
    slots: tuple[MechanisticExperimentSlot, ...],
    policy: MechanisticDecisionPolicy,
    evaluator_principal_sha256: str,
    frozen_at: datetime,
) -> MechanisticCampaignProtocol:
    predictions = tuple(sorted(prediction_campaigns, key=lambda item: item.campaign_sha256))
    frozen_slots = tuple(sorted(slots, key=lambda item: item.slot_id))
    execution, release = _derive_protocol_gates(
        causal=causal_campaign,
        predictions=predictions,
        slots=frozen_slots,
    )
    return MechanisticCampaignProtocol(
        protocol_id=protocol_id,
        capability_registry=capability_registry,
        causal_campaign=causal_campaign,
        prediction_campaigns=predictions,
        slots=frozen_slots,
        policy=policy,
        evaluator_principal_sha256=evaluator_principal_sha256,
        execution_blockers=execution,
        mechanism_release_blockers=release,
        execution_authorized=not execution,
        mechanism_release_eligible=not release,
        frozen_at=frozen_at,
    )


class OutcomeMappingManifest(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_outcome_mapping_manifest"] = (
        "aletheia.mechanistic_outcome_mapping_manifest"
    )
    schema_version: Literal[1] = 1
    mapper_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter_ref: str = Field(pattern=_ADAPTER_PATTERN)
    implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class MechanisticSlotEvidence(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_slot_evidence"] = (
        "aletheia.mechanistic_slot_evidence"
    )
    schema_version: Literal[1] = 1
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    pipeline: CommittedCapabilityObservationPipeline
    observed_outcome_bin_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    mapping_manifest: OutcomeMappingManifest
    mapping_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_opened_at: AwareDatetime
    mapped_at: AwareDatetime
    independence_attestation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    state: Literal["mapped_from_validated_observation"] = "mapped_from_validated_observation"

    @model_validator(mode="after")
    def _mapping_chronology_is_valid(self) -> "MechanisticSlotEvidence":
        if self.mapping_manifest.frozen_at > self.source_opened_at:
            raise ValueError("outcome mapper froze after source opening")
        if self.source_opened_at > self.mapped_at or self.pipeline.committed_at > self.mapped_at:
            raise ValueError("outcome mapping predates its source evidence")
        return self

    @property
    def evidence_sha256(self) -> str:
        return content_sha256(self)


class MechanisticScenarioLikelihood(FrozenModel):
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    likelihood: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _likelihood_is_finite(self) -> "MechanisticScenarioLikelihood":
        if not math.isfinite(self.likelihood):
            raise ValueError("mechanistic likelihood must be finite")
        return self


class MechanisticSlotAssessment(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_slot_assessment"] = (
        "aletheia.mechanistic_slot_assessment"
    )
    schema_version: Literal[1] = 1
    slot_id: str
    slot_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_bin_id: str
    likelihoods: tuple[MechanisticScenarioLikelihood, ...]
    robust_winner_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{32}$")
    robust_winner_role: HypothesisRole | None = None
    minimum_winner_probability_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_valid: bool
    passes_discrimination_rule: bool
    blockers: tuple[str, ...]
    discrimination_failures: tuple[str, ...]

    @model_validator(mode="after")
    def _assessment_is_canonical(self) -> "MechanisticSlotAssessment":
        likelihood_keys = tuple((item.scenario_id, item.hypothesis_id) for item in self.likelihoods)
        if likelihood_keys != tuple(sorted(set(likelihood_keys))):
            raise ValueError("mechanistic likelihoods must be unique and canonical")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("mechanistic slot blockers must be unique and canonical")
        if self.discrimination_failures != tuple(sorted(set(self.discrimination_failures))):
            raise ValueError("mechanistic discrimination failures must be unique and canonical")
        if self.evidence_valid != (not self.blockers):
            raise ValueError("mechanistic slot validity must be derived from blockers")
        if self.passes_discrimination_rule != (
            self.evidence_valid
            and not self.discrimination_failures
            and self.robust_winner_hypothesis_id is not None
        ):
            raise ValueError("mechanistic discrimination flag is inconsistent")
        if (self.robust_winner_hypothesis_id is None) != (self.robust_winner_role is None) or (
            self.robust_winner_hypothesis_id is None
        ) != (self.minimum_winner_probability_margin is None):
            raise ValueError("mechanistic winner fields must be all present or all absent")
        return self

    @property
    def assessment_sha256(self) -> str:
        return content_sha256(self)


def _pipeline_blockers(
    *,
    protocol: MechanisticCampaignProtocol,
    slot: MechanisticExperimentSlot,
    evidence: MechanisticSlotEvidence,
    prediction: PredictionCommitmentCampaign,
) -> set[str]:
    blockers: set[str] = set()
    result = evidence.pipeline.result
    raw_run = result.parse_result.raw_run
    if result.manifest != slot.capability_manifest:
        blockers.add("capability_manifest_mismatch")
    if raw_run.preregistration_sha256 != protocol.protocol_sha256:
        blockers.add("preregistration_mismatch")
    if raw_run.input_sha256 != slot.input_identity_sha256:
        blockers.add("input_identity_mismatch")
    if raw_run.run_purpose.value != "measurement":
        blockers.add("nonmeasurement_run")
    if raw_run.started_at <= protocol.frozen_at:
        blockers.add("execution_not_after_protocol_freeze")
    if evidence.source_opened_at <= protocol.frozen_at:
        blockers.add("source_not_opened_after_protocol_freeze")
    if raw_run.started_at < evidence.source_opened_at:
        blockers.add("execution_started_before_source_open")
    if result.validation is None or result.disposition not in _VALIDATED_DISPOSITIONS:
        blockers.add("observation_not_validated")
    elif not result.validation.admissible_for_f9_exploratory_update:
        blockers.add("observation_not_admissible")
    if evidence.mapping_manifest.principal_sha256 == protocol.evaluator_principal_sha256:
        blockers.add("outcome_mapper_is_campaign_evaluator")
    if evidence.mapping_manifest.principal_sha256 in {
        role.principal_sha256 for role in slot.capability_manifest.roles
    }:
        blockers.add("outcome_mapper_reuses_capability_role")
    if evidence.mapping_manifest.principal_sha256 in _scientific_principals(
        protocol.causal_campaign,
        protocol.prediction_campaigns,
        protocol.slots,
    ):
        blockers.add("outcome_mapper_reuses_scientific_role")
    if evidence.mapping_manifest.prediction_campaign_sha256 != prediction.campaign_sha256:
        blockers.add("outcome_mapper_prediction_mismatch")
    if (
        evidence.mapping_manifest.outcome_schema_sha256
        != prediction.request.outcome_schema.outcome_schema_sha256
    ):
        blockers.add("outcome_mapper_schema_mismatch")
    outcome_ids = {item.bin_id for item in prediction.request.outcome_schema.bins}
    if evidence.observed_outcome_bin_id not in outcome_ids:
        blockers.add("observed_outcome_not_preregistered")
    reservation = slot.confirmation_reservation
    if reservation is not None:
        if evidence.source_opened_at <= reservation.reserved_at:
            blockers.add("fresh_source_not_opened_after_reservation")
        if evidence.independence_attestation_sha256 is None:
            blockers.add("independence_attestation_missing")
    elif evidence.independence_attestation_sha256 is not None:
        blockers.add("unexpected_independence_attestation")
    return blockers


def _probability_for_bin(probabilities: object, bin_id: str) -> float | None:
    for item in probabilities:  # type: ignore[union-attr]
        if item.bin_id == bin_id:
            return float(item.probability)
    return None


def _derive_slot_assessment(
    *,
    protocol: MechanisticCampaignProtocol,
    slot: MechanisticExperimentSlot,
    evidence: MechanisticSlotEvidence,
) -> MechanisticSlotAssessment:
    prediction = next(
        item
        for item in protocol.prediction_campaigns
        if item.campaign_sha256 == slot.prediction_campaign_sha256
    )
    blockers = _pipeline_blockers(
        protocol=protocol,
        slot=slot,
        evidence=evidence,
        prediction=prediction,
    )
    discrimination_failures: set[str] = set()
    likelihoods: list[MechanisticScenarioLikelihood] = []
    robust_winner: str | None = None
    robust_role: HypothesisRole | None = None
    minimum_margin: float | None = None
    batch = prediction.prediction_batch
    if batch is None or prediction.request.prediction_mode is not PredictionMode.PROBABILISTIC:
        blockers.add("probabilistic_prediction_batch_missing")
    else:
        scenario_sets = [
            tuple(item.scenario_id for item in hypothesis.sensitivity_predictions)
            for hypothesis in batch.predictions
        ]
        if len(set(scenario_sets)) != 1:
            blockers.add("sensitivity_scenarios_mismatch")
        else:
            scenario_ids = ("nominal", *scenario_sets[0])
            scenario_winners: list[str] = []
            scenario_margins: list[float] = []
            for scenario_id in scenario_ids:
                values: list[tuple[str, float]] = []
                for hypothesis in batch.predictions:
                    probabilities = (
                        hypothesis.probabilities
                        if scenario_id == "nominal"
                        else next(
                            item.probabilities
                            for item in hypothesis.sensitivity_predictions
                            if item.scenario_id == scenario_id
                        )
                    )
                    likelihood = _probability_for_bin(
                        probabilities, evidence.observed_outcome_bin_id
                    )
                    if likelihood is None:
                        blockers.add(f"outcome_probability_missing:{scenario_id}")
                        continue
                    values.append((hypothesis.hypothesis_id, likelihood))
                    likelihoods.append(
                        MechanisticScenarioLikelihood(
                            scenario_id=scenario_id,
                            hypothesis_id=hypothesis.hypothesis_id,
                            likelihood=likelihood,
                        )
                    )
                if len(values) != len(batch.predictions):
                    continue
                ranked = sorted(values, key=lambda item: (-item[1], item[0]))
                if len(ranked) < 2 or math.isclose(
                    ranked[0][1], ranked[1][1], rel_tol=0.0, abs_tol=1e-12
                ):
                    discrimination_failures.add(f"no_unique_winner:{scenario_id}")
                    continue
                scenario_winners.append(ranked[0][0])
                scenario_margins.append(ranked[0][1] - ranked[1][1])
            if len(scenario_winners) == len(scenario_ids):
                if len(set(scenario_winners)) != 1:
                    discrimination_failures.add("winner_not_robust_across_sensitivity_scenarios")
                elif min(scenario_margins) < protocol.policy.minimum_probability_margin:
                    discrimination_failures.add("winner_probability_margin_below_minimum")
                else:
                    robust_winner = scenario_winners[0]
                    minimum_margin = min(scenario_margins)
                    snapshot = protocol.causal_campaign.source_campaign.world_model_snapshot
                    assert snapshot is not None
                    robust_role = next(
                        item.role
                        for item in snapshot.hypotheses
                        if item.hypothesis_id == robust_winner
                    )
    canonical_likelihoods = tuple(
        sorted(likelihoods, key=lambda item: (item.scenario_id, item.hypothesis_id))
    )
    canonical_blockers = tuple(sorted(blockers))
    canonical_discrimination_failures = tuple(sorted(discrimination_failures))
    return MechanisticSlotAssessment(
        slot_id=slot.slot_id,
        slot_sha256=slot.slot_sha256,
        evidence_sha256=evidence.evidence_sha256,
        observed_outcome_bin_id=evidence.observed_outcome_bin_id,
        likelihoods=canonical_likelihoods,
        robust_winner_hypothesis_id=robust_winner,
        robust_winner_role=robust_role,
        minimum_winner_probability_margin=minimum_margin,
        evidence_valid=not canonical_blockers,
        passes_discrimination_rule=(
            not canonical_blockers
            and not canonical_discrimination_failures
            and robust_winner is not None
        ),
        blockers=canonical_blockers,
        discrimination_failures=canonical_discrimination_failures,
    )


class MechanisticCampaignDecision(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_campaign_decision"] = (
        "aletheia.mechanistic_campaign_decision"
    )
    schema_version: Literal[1] = 1
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessment_sha256s: tuple[str, ...] = Field(min_length=2)
    supported_hypothesis_id: str | None = Field(default=None, pattern=r"^hyp_[0-9a-f]{32}$")
    supported_hypothesis_role: HypothesisRole | None = None
    disposition: MechanisticCampaignDisposition
    claim_ceiling: MechanisticClaimCeiling
    release_blockers: tuple[str, ...]
    joint_posterior_computed: Literal[False] = False
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _decision_is_canonical(self) -> "MechanisticCampaignDecision":
        if self.assessment_sha256s != tuple(sorted(set(self.assessment_sha256s))):
            raise ValueError("mechanistic assessment hashes must be unique and canonical")
        if self.release_blockers != tuple(sorted(set(self.release_blockers))):
            raise ValueError("mechanistic release blockers must be unique and canonical")
        if (self.supported_hypothesis_id is None) != (self.supported_hypothesis_role is None):
            raise ValueError("supported hypothesis ID and role must appear together")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


def _derive_decision(
    *,
    protocol: MechanisticCampaignProtocol,
    evidences: tuple[MechanisticSlotEvidence, ...],
    assessments: tuple[MechanisticSlotAssessment, ...],
    decided_at: datetime,
) -> MechanisticCampaignDecision:
    release_blockers = set(protocol.mechanism_release_blockers)
    evidence_by_slot = {item.slot_id: item for item in evidences}
    for slot in protocol.slots:
        evidence = evidence_by_slot[slot.slot_id]
        validation = evidence.pipeline.result.validation
        if validation is None or not validation.admissible_for_f9_confirmatory_update:
            release_blockers.add(f"observation_not_confirmatory:{slot.slot_id}")
        if (
            slot.evidence_role is MechanisticEvidenceRole.FRESH_CONFIRMATION
            and evidence.independence_attestation_sha256 is None
        ):
            release_blockers.add(f"independence_attestation_missing:{slot.slot_id}")

    supported_id: str | None = None
    supported_role: HypothesisRole | None = None
    capability_claim_types = {
        claim_type
        for slot in protocol.slots
        for claim_type in slot.capability_manifest.claim_types_supported
    }
    if any(not item.evidence_valid for item in assessments):
        disposition = MechanisticCampaignDisposition.INVALID_EVIDENCE
        ceiling = MechanisticClaimCeiling.NONE
    elif any(not item.passes_discrimination_rule for item in assessments):
        disposition = MechanisticCampaignDisposition.INCONCLUSIVE
        ceiling = MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
    else:
        winners = {item.robust_winner_hypothesis_id for item in assessments}
        if len(winners) != 1:
            disposition = MechanisticCampaignDisposition.CONFLICTING_EVIDENCE
            ceiling = MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
        else:
            supported_id = next(iter(winners))
            assert supported_id is not None
            supported_role = assessments[0].robust_winner_role
            assert supported_role is not None
            if supported_role is HypothesisRole.NULL:
                disposition = MechanisticCampaignDisposition.NULL_SUPPORTED
                ceiling = MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
            elif release_blockers:
                disposition = MechanisticCampaignDisposition.BOUNDED_PATTERN_SUPPORTED
                ceiling = MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
            elif (
                protocol.causal_campaign.claim_ceiling is CausalClaimCeiling.CAUSAL_CANDIDATE
                and capability_claim_types.intersection(
                    {
                        CapabilityClaimType.MECHANISM_CANDIDATE,
                        CapabilityClaimType.EXPERIMENTAL_CAUSAL,
                    }
                )
            ):
                disposition = MechanisticCampaignDisposition.MECHANISM_CANDIDATE_SUPPORTED
                ceiling = MechanisticClaimCeiling.MECHANISM_CANDIDATE
            elif protocol.causal_campaign.claim_ceiling in {
                CausalClaimCeiling.CAUSAL_CANDIDATE,
                CausalClaimCeiling.WITHIN_MODEL_CAUSAL_ONLY,
            } and capability_claim_types.intersection(
                {
                    CapabilityClaimType.WITHIN_MODEL_CAUSAL,
                    CapabilityClaimType.MECHANISM_CANDIDATE,
                    CapabilityClaimType.EXPERIMENTAL_CAUSAL,
                }
            ):
                disposition = MechanisticCampaignDisposition.WITHIN_MODEL_MECHANISM_SUPPORTED
                ceiling = MechanisticClaimCeiling.WITHIN_MODEL_MECHANISM_CANDIDATE
            else:
                disposition = MechanisticCampaignDisposition.BOUNDED_PATTERN_SUPPORTED
                ceiling = MechanisticClaimCeiling.DESCRIPTIVE_PATTERN
    return MechanisticCampaignDecision(
        protocol_sha256=protocol.protocol_sha256,
        assessment_sha256s=tuple(sorted(item.assessment_sha256 for item in assessments)),
        supported_hypothesis_id=supported_id,
        supported_hypothesis_role=supported_role,
        disposition=disposition,
        claim_ceiling=ceiling,
        release_blockers=tuple(sorted(release_blockers)),
        decided_at=decided_at,
    )


class MechanisticCampaignEvidenceBundle(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_campaign_evidence_bundle"] = (
        "aletheia.mechanistic_campaign_evidence_bundle"
    )
    schema_version: Literal[1] = 1
    protocol: MechanisticCampaignProtocol
    slot_evidences: tuple[MechanisticSlotEvidence, ...] = Field(min_length=2)
    slot_assessments: tuple[MechanisticSlotAssessment, ...] = Field(min_length=2)
    decision: MechanisticCampaignDecision
    assembled_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _bundle_is_fully_rederived(self) -> "MechanisticCampaignEvidenceBundle":
        evidence_ids = tuple(item.slot_id for item in self.slot_evidences)
        if evidence_ids != tuple(sorted(set(evidence_ids))):
            raise ValueError("mechanistic evidence must be unique and canonical")
        if evidence_ids != tuple(item.slot_id for item in self.protocol.slots):
            raise ValueError("mechanistic evidence does not cover every frozen slot")
        expected_assessments = tuple(
            _derive_slot_assessment(protocol=self.protocol, slot=slot, evidence=evidence)
            for slot, evidence in zip(self.protocol.slots, self.slot_evidences, strict=True)
        )
        if self.slot_assessments != expected_assessments:
            raise ValueError("mechanistic slot assessments are not derived from evidence")
        expected_decision = _derive_decision(
            protocol=self.protocol,
            evidences=self.slot_evidences,
            assessments=self.slot_assessments,
            decided_at=self.decision.decided_at,
        )
        if self.decision != expected_decision:
            raise ValueError("mechanistic campaign decision is not derived")
        latest = max(
            self.protocol.frozen_at,
            self.decision.decided_at,
            *(item.mapped_at for item in self.slot_evidences),
        )
        if self.assembled_at < latest:
            raise ValueError("mechanistic bundle predates its evidence or decision")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)


def evaluate_mechanistic_campaign(
    *,
    protocol: MechanisticCampaignProtocol,
    slot_evidences: tuple[MechanisticSlotEvidence, ...],
    evaluated_at: datetime,
) -> MechanisticCampaignEvidenceBundle:
    evidences = tuple(sorted(slot_evidences, key=lambda item: item.slot_id))
    if tuple(item.slot_id for item in evidences) != tuple(item.slot_id for item in protocol.slots):
        raise ValueError("mechanistic evaluation requires exactly every frozen slot")
    assessments = tuple(
        _derive_slot_assessment(protocol=protocol, slot=slot, evidence=evidence)
        for slot, evidence in zip(protocol.slots, evidences, strict=True)
    )
    decision = _derive_decision(
        protocol=protocol,
        evidences=evidences,
        assessments=assessments,
        decided_at=evaluated_at,
    )
    return MechanisticCampaignEvidenceBundle(
        protocol=protocol,
        slot_evidences=evidences,
        slot_assessments=assessments,
        decision=decision,
        assembled_at=evaluated_at,
    )


class CapabilityReadinessItem(FrozenModel):
    capability_id: str
    version: str
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_type: CapabilityActionType
    family: MechanisticExperimentFamily | None
    family_qualification_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    lifecycle: CapabilityLifecycle
    maximum_evidence_level: CapabilityEvidenceLevel
    claim_types_supported: tuple[CapabilityClaimType, ...]


class MechanisticCampaignReadinessAudit(FrozenModel):
    schema_name: Literal["aletheia.mechanistic_campaign_readiness_audit"] = (
        "aletheia.mechanistic_campaign_readiness_audit"
    )
    schema_version: Literal[1] = 1
    audit_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    registry_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    family_qualifications: tuple[MechanisticCapabilityQualification, ...]
    available_capabilities: tuple[CapabilityReadinessItem, ...]
    production_direction_gate_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    ready_hypothesis_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    ready_causal_campaign_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    fresh_confirmation_reservation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    independent_confirmation_kind: ConfirmationIndependenceKind | None = None
    engineering_template_available: Literal[True] = True
    execution_ready: bool
    scientific_release_ready: bool
    blockers: tuple[str, ...]
    audited_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _readiness_is_mechanically_derived(self) -> "MechanisticCampaignReadinessAudit":
        identities = tuple(
            (item.capability_id, item.version) for item in self.available_capabilities
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError("readiness capabilities must be unique and canonical")
        qualification_keys = tuple(
            item.capability_manifest_sha256 for item in self.family_qualifications
        )
        if qualification_keys != tuple(sorted(set(qualification_keys))):
            raise ValueError("readiness family qualifications must be unique and canonical")
        qualifications = {
            item.capability_manifest_sha256: item for item in self.family_qualifications
        }
        for item in self.available_capabilities:
            qualification = qualifications.get(item.manifest_sha256)
            if qualification is None:
                if item.family is not None or item.family_qualification_sha256 is not None:
                    raise ValueError("readiness family lacks a qualification artifact")
            elif (
                item.family is not qualification.family
                or item.family_qualification_sha256 != qualification.qualification_sha256
                or item.action_type is not qualification.expected_action
            ):
                raise ValueError("readiness family changed its qualification artifact")
        if set(qualification_keys) != {
            item.manifest_sha256 for item in self.available_capabilities if item.family is not None
        }:
            raise ValueError("readiness qualification does not bind a latest capability")
        blockers: set[str] = set()
        if self.production_direction_gate_sha256 is None:
            blockers.add("production_f8_direction_missing")
        if self.ready_hypothesis_campaign_sha256 is None:
            blockers.add("ready_f9_hypothesis_campaign_missing")
        if self.ready_causal_campaign_sha256 is None:
            blockers.add("ready_f9_causal_campaign_missing")
        registered_families = {
            item.family
            for item in self.available_capabilities
            if item.family is not None
            and item.lifecycle is CapabilityLifecycle.REGISTERED
            and evidence_level_rank(item.maximum_evidence_level)
            >= evidence_level_rank(CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL)
        }
        if len(registered_families) < 2:
            blockers.add("fewer_than_two_registered_confirmatory_families")
        if not registered_families.intersection(
            {
                MechanisticExperimentFamily.STRUCTURAL_INTERVENTION,
                MechanisticExperimentFamily.SIMULATION,
            }
        ):
            blockers.add("registered_intervention_or_simulation_missing")
        if not any(
            item.family is not None
            and item.lifecycle is CapabilityLifecycle.REGISTERED
            and evidence_level_rank(item.maximum_evidence_level)
            >= evidence_level_rank(CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL)
            and {
                CapabilityClaimType.WITHIN_MODEL_CAUSAL,
                CapabilityClaimType.MECHANISM_CANDIDATE,
                CapabilityClaimType.EXPERIMENTAL_CAUSAL,
            }.intersection(item.claim_types_supported)
            for item in self.available_capabilities
        ):
            blockers.add("registered_mechanism_capability_missing")
        if self.fresh_confirmation_reservation_sha256 is None:
            blockers.add("fresh_confirmation_reservation_missing")
        if self.independent_confirmation_kind is None:
            blockers.add("independent_confirmation_missing")
        expected = tuple(sorted(blockers))
        execution_blockers = {
            "production_f8_direction_missing",
            "ready_f9_hypothesis_campaign_missing",
            "ready_f9_causal_campaign_missing",
            "fewer_than_two_registered_confirmatory_families",
            "registered_intervention_or_simulation_missing",
        }
        if (
            self.blockers != expected
            or self.execution_ready != (not blockers.intersection(execution_blockers))
            or self.scientific_release_ready != (not blockers)
        ):
            raise ValueError("mechanistic readiness is not mechanically derived")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


def build_mechanistic_campaign_readiness_audit(
    *,
    audit_id: str,
    registry: CapabilityRegistrySnapshot,
    audited_at: datetime,
    family_qualifications: tuple[MechanisticCapabilityQualification, ...] = (),
    production_direction_gate_sha256: str | None = None,
    ready_hypothesis_campaign_sha256: str | None = None,
    ready_causal_campaign_sha256: str | None = None,
    fresh_confirmation_reservation_sha256: str | None = None,
    independent_confirmation_kind: ConfirmationIndependenceKind | None = None,
) -> MechanisticCampaignReadinessAudit:
    latest: dict[str, ExperimentCapabilityManifest] = {}
    for manifest in registry.manifests:
        previous = latest.get(manifest.capability_id)
        if previous is None or manifest.semantic_version > previous.semantic_version:
            latest[manifest.capability_id] = manifest
    qualifications = tuple(
        sorted(
            family_qualifications,
            key=lambda item: item.capability_manifest_sha256,
        )
    )
    qualification_hashes = tuple(item.capability_manifest_sha256 for item in qualifications)
    if qualification_hashes != tuple(sorted(set(qualification_hashes))):
        raise ValueError("family qualifications must bind unique capability manifests")
    latest_by_hash = {item.manifest_sha256: item for item in latest.values()}
    for qualification in qualifications:
        manifest = latest_by_hash.get(qualification.capability_manifest_sha256)
        if manifest is None:
            raise ValueError("family qualification does not bind a latest registry manifest")
        if qualification.expected_action is not manifest.action_type:
            raise ValueError("family qualification changed the capability action")
        if qualification.domain_reviewer_principal_sha256 in {
            role.principal_sha256 for role in manifest.roles
        }:
            raise ValueError("family qualification reviewer reuses a capability role")
        if qualification.frozen_at < manifest.frozen_at or qualification.frozen_at > audited_at:
            raise ValueError("family qualification chronology is invalid")
    qualification_by_manifest = {item.capability_manifest_sha256: item for item in qualifications}
    items = tuple(
        CapabilityReadinessItem(
            capability_id=manifest.capability_id,
            version=manifest.version,
            manifest_sha256=manifest.manifest_sha256,
            action_type=manifest.action_type,
            family=(
                qualification_by_manifest[manifest.manifest_sha256].family
                if manifest.manifest_sha256 in qualification_by_manifest
                else None
            ),
            family_qualification_sha256=(
                qualification_by_manifest[manifest.manifest_sha256].qualification_sha256
                if manifest.manifest_sha256 in qualification_by_manifest
                else None
            ),
            lifecycle=manifest.lifecycle,
            maximum_evidence_level=manifest.maximum_evidence_level,
            claim_types_supported=manifest.claim_types_supported,
        )
        for manifest in sorted(latest.values(), key=lambda item: item.capability_id)
    )
    provisional = {
        "audit_id": audit_id,
        "registry_snapshot_sha256": registry.snapshot_sha256,
        "family_qualifications": qualifications,
        "available_capabilities": items,
        "production_direction_gate_sha256": production_direction_gate_sha256,
        "ready_hypothesis_campaign_sha256": ready_hypothesis_campaign_sha256,
        "ready_causal_campaign_sha256": ready_causal_campaign_sha256,
        "fresh_confirmation_reservation_sha256": fresh_confirmation_reservation_sha256,
        "independent_confirmation_kind": independent_confirmation_kind,
        "execution_ready": False,
        "scientific_release_ready": False,
        "blockers": (),
        "audited_at": audited_at,
    }
    blockers: set[str] = set()
    if production_direction_gate_sha256 is None:
        blockers.add("production_f8_direction_missing")
    if ready_hypothesis_campaign_sha256 is None:
        blockers.add("ready_f9_hypothesis_campaign_missing")
    if ready_causal_campaign_sha256 is None:
        blockers.add("ready_f9_causal_campaign_missing")
    registered_families = {
        item.family
        for item in items
        if item.family is not None
        and item.lifecycle is CapabilityLifecycle.REGISTERED
        and evidence_level_rank(item.maximum_evidence_level)
        >= evidence_level_rank(CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL)
    }
    if len(registered_families) < 2:
        blockers.add("fewer_than_two_registered_confirmatory_families")
    if not registered_families.intersection(
        {
            MechanisticExperimentFamily.STRUCTURAL_INTERVENTION,
            MechanisticExperimentFamily.SIMULATION,
        }
    ):
        blockers.add("registered_intervention_or_simulation_missing")
    if not any(
        item.family is not None
        and item.lifecycle is CapabilityLifecycle.REGISTERED
        and evidence_level_rank(item.maximum_evidence_level)
        >= evidence_level_rank(CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL)
        and {
            CapabilityClaimType.WITHIN_MODEL_CAUSAL,
            CapabilityClaimType.MECHANISM_CANDIDATE,
            CapabilityClaimType.EXPERIMENTAL_CAUSAL,
        }.intersection(item.claim_types_supported)
        for item in items
    ):
        blockers.add("registered_mechanism_capability_missing")
    if fresh_confirmation_reservation_sha256 is None:
        blockers.add("fresh_confirmation_reservation_missing")
    if independent_confirmation_kind is None:
        blockers.add("independent_confirmation_missing")
    execution_blockers = {
        "production_f8_direction_missing",
        "ready_f9_hypothesis_campaign_missing",
        "ready_f9_causal_campaign_missing",
        "fewer_than_two_registered_confirmatory_families",
        "registered_intervention_or_simulation_missing",
    }
    provisional.update(
        {
            "blockers": tuple(sorted(blockers)),
            "execution_ready": not blockers.intersection(execution_blockers),
            "scientific_release_ready": not blockers,
        }
    )
    return MechanisticCampaignReadinessAudit.model_validate(provisional)


__all__ = [
    "CapabilityReadinessItem",
    "ConfirmationIndependenceKind",
    "FreshConfirmationReservation",
    "MechanisticCampaignDecision",
    "MechanisticCampaignDisposition",
    "MechanisticCampaignEvidenceBundle",
    "MechanisticCampaignProtocol",
    "MechanisticCampaignReadinessAudit",
    "MechanisticCapabilityQualification",
    "MechanisticClaimCeiling",
    "MechanisticDecisionPolicy",
    "MechanisticEvidenceRole",
    "MechanisticExperimentFamily",
    "MechanisticExperimentSlot",
    "MechanisticScenarioLikelihood",
    "MechanisticSlotAssessment",
    "MechanisticSlotEvidence",
    "OutcomeMappingManifest",
    "build_mechanistic_campaign_protocol",
    "build_mechanistic_campaign_readiness_audit",
    "evaluate_mechanistic_campaign",
]
