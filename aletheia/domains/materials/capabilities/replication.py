"""Pre-registered multi-partition replication for the provisional materials capability.

The matrix is deliberately descriptive.  Every frozen seed is retained, each observation is
recomputed twice, and the aggregation never treats same-dataset partitions as independent Bayesian
evidence.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities import (
    CapabilityEvidenceLevel,
    CapabilityLifecycle,
    CapabilityRegistrySnapshot,
    CapabilityRole,
    ExperimentCapabilityManifest,
)
from aletheia.domains.materials.k3_evidence import (
    MaterialsBeliefUpdate,
    MaterialsEvidencePolicy,
    MaterialsK3Protocol,
    MaterialsOutcomeId,
    MaterialsPreregistration,
    SignedMaterialsObservation,
    SignedMaterialsValidation,
    build_materials_preregistration,
    classify_materials_outcome,
    derive_materials_belief_update,
    derive_materials_candidate_audits,
    materials_k3_implementation_sha256,
    run_materials_experiment,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def materials_replication_implementation_sha256() -> str:
    """Return the physical identity of this replication implementation."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class MaterialsReplicationPattern(str, Enum):
    CONSENSUS_UNSEEN_SPECIFIC = "consensus_unseen_system_specific_compression"
    CONSENSUS_GENERIC_SHRINKAGE = "consensus_generic_model_shrinkage"
    CONSENSUS_NO_MATERIAL_COMPRESSION = "consensus_no_material_compression"
    CONSENSUS_AMBIGUOUS = "consensus_ambiguous_pattern"
    PARTITION_SENSITIVE = "partition_sensitive"


class MaterialsReplicationAggregationRule(FrozenModel):
    schema_version: Literal[1] = 1
    required_slots: int = Field(default=5, ge=3)
    consensus_slots: int = Field(default=4, ge=2)
    required_exact_reexecutions_per_slot: int = Field(default=2, ge=2)
    retain_every_slot: Literal[True] = True
    early_stopping_forbidden: Literal[True] = True
    best_of_n_forbidden: Literal[True] = True
    joint_bayesian_update_forbidden: Literal[True] = True
    primary_statistic: Literal["preregistered_outcome_consensus"] = (
        "preregistered_outcome_consensus"
    )
    heterogeneity_statistics: tuple[str, ...] = (
        "delta_mean",
        "delta_median",
        "delta_range",
        "delta_sample_sd",
        "outcome_counts",
        "positive_delta_slots",
    )

    @model_validator(mode="after")
    def _consensus_is_strict(self) -> "MaterialsReplicationAggregationRule":
        if self.consensus_slots <= self.required_slots / 2:
            raise ValueError("replication consensus must be a strict majority")
        if self.consensus_slots > self.required_slots:
            raise ValueError("replication consensus cannot exceed required slots")
        if self.heterogeneity_statistics != tuple(sorted(set(self.heterogeneity_statistics))):
            raise ValueError("heterogeneity statistics must be unique and sorted")
        return self


def _role(manifest: ExperimentCapabilityManifest, role: CapabilityRole):
    return next(item for item in manifest.roles if item.role is role)


def _update_principal(manifest_sha256: str) -> str:
    payload = b"aletheia-materials-capability-update-v1\0" + manifest_sha256.encode()
    return hashlib.sha256(payload).hexdigest()


def _derive_protocol(
    *,
    plan_id: str,
    base_protocol: MaterialsK3Protocol,
    manifest: ExperimentCapabilityManifest,
    seed: int,
    ordinal: int,
    frozen_at: datetime,
) -> MaterialsK3Protocol:
    executor = _role(manifest, CapabilityRole.EXECUTOR)
    validator = _role(manifest, CapabilityRole.VALIDATOR)
    policy_raw = base_protocol.evidence_policy.model_dump(mode="python")
    policy_raw.update(
        {
            "measurement_key_id": "materials-capability-measurement-local-v1",
            "validation_key_id": "materials-capability-validation-local-v1",
            "measurement_principal_sha256": executor.principal_sha256,
            "validation_principal_sha256": validator.principal_sha256,
            "update_principal_sha256": _update_principal(manifest.manifest_sha256),
            "contamination_disclosure": (
                base_protocol.evidence_policy.contamination_disclosure
                + " The 20260818-20260822 five-slot matrix, its 4/5 consensus rule, and every "
                "analysis seed were frozen together before any matrix slot was measured."
            ),
            "custody_disclosure": (
                base_protocol.evidence_policy.custody_disclosure
                + " The replication matrix remains a single-operator local computation even "
                "though measurement and validation roles and keys are separated."
            ),
        }
    )
    protocol_raw = base_protocol.model_dump(mode="python")
    protocol_raw.update(
        {
            "protocol_id": f"{plan_id}.slot-{ordinal:02d}",
            "model": {
                **base_protocol.model.model_dump(mode="python"),
                "random_state": seed,
            },
            "split": {
                **base_protocol.split.model_dump(mode="python"),
                "partition_seed": seed,
            },
            "bootstrap": {
                **base_protocol.bootstrap.model_dump(mode="python"),
                "seed": seed * 100 + 1,
            },
            "evidence_policy": MaterialsEvidencePolicy.model_validate(policy_raw),
            "frozen_at": frozen_at,
        }
    )
    return MaterialsK3Protocol.model_validate(protocol_raw)


class MaterialsReplicationSlotCommitment(FrozenModel):
    schema_version: Literal[1] = 1
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    ordinal: int = Field(ge=1)
    seed: int = Field(ge=0)
    maximum_attempts: Literal[1] = 1
    required: Literal[True] = True
    preregistration: MaterialsPreregistration

    @property
    def commitment_sha256(self) -> str:
        return content_sha256(self)


class MaterialsReplicationPlan(FrozenModel):
    schema_name: Literal["aletheia.materials_replication_plan"] = (
        "aletheia.materials_replication_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,95}$")
    capability_manifest: ExperimentCapabilityManifest
    capability_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_protocol: MaterialsK3Protocol
    base_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replication_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregation_rule: MaterialsReplicationAggregationRule
    slots: tuple[MaterialsReplicationSlotCommitment, ...] = Field(min_length=3)
    observation_access_before_freeze: Literal["none"] = "none"
    evidence_level: Literal["exploratory"] = "exploratory"
    protocol_frozen_at: AwareDatetime
    preregistered_at: AwareDatetime
    frozen_at: AwareDatetime
    state: Literal["all_slots_frozen_before_measurement"] = "all_slots_frozen_before_measurement"

    @model_validator(mode="after")
    def _matrix_is_exact_and_preobservational(self) -> "MaterialsReplicationPlan":
        manifest = self.capability_manifest
        if self.capability_manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("replication plan capability-manifest hash is invalid")
        if self.base_protocol_sha256 != self.base_protocol.protocol_sha256:
            raise ValueError("replication plan base-protocol hash is invalid")
        if manifest.lifecycle is not CapabilityLifecycle.PROVISIONAL:
            raise ValueError("this matrix is scoped to the provisional capability")
        if manifest.maximum_evidence_level is not CapabilityEvidenceLevel.EXPLORATORY:
            raise ValueError("provisional replication cannot exceed exploratory evidence")
        if (
            manifest.nondeterminism_policy.maximum_attempts_per_slot != 1
            or not manifest.nondeterminism_policy.best_of_n_forbidden
        ):
            raise ValueError("replication capability must forbid repeated slot attempts")
        if (
            manifest.reproduction_policy.minimum_exact_reexecutions
            < self.aggregation_rule.required_exact_reexecutions_per_slot
        ):
            raise ValueError("replication plan underfills the capability reproduction policy")
        if not (self.protocol_frozen_at < self.preregistered_at <= self.frozen_at):
            raise ValueError("replication plan chronology is invalid")
        seeds = manifest.nondeterminism_policy.frozen_seeds
        if len(self.slots) != self.aggregation_rule.required_slots or len(self.slots) != len(seeds):
            raise ValueError("replication plan must contain every frozen capability seed")
        if tuple(item.ordinal for item in self.slots) != tuple(range(1, len(self.slots) + 1)):
            raise ValueError("replication slot ordinals must be canonical")
        if tuple(item.seed for item in self.slots) != seeds:
            raise ValueError("replication slots do not match the capability seed commitment")
        if tuple(item.slot_id for item in self.slots) != tuple(
            f"slot-{index:02d}" for index in range(1, len(self.slots) + 1)
        ):
            raise ValueError("replication slot IDs must be canonical")
        executor = _role(manifest, CapabilityRole.EXECUTOR)
        validator = _role(manifest, CapabilityRole.VALIDATOR)
        if executor.implementation_sha256 != validator.implementation_sha256:
            raise ValueError("replication executor and validator implementations diverge")
        for slot in self.slots:
            expected_protocol = _derive_protocol(
                plan_id=self.plan_id,
                base_protocol=self.base_protocol,
                manifest=manifest,
                seed=slot.seed,
                ordinal=slot.ordinal,
                frozen_at=self.protocol_frozen_at,
            )
            preregistration = slot.preregistration
            if preregistration.protocol != expected_protocol:
                raise ValueError(f"replication {slot.slot_id} protocol is not derived exactly")
            if preregistration.implementation_sha256 != executor.implementation_sha256:
                raise ValueError(f"replication {slot.slot_id} implementation is not registered")
            if preregistration.preregistered_at != self.preregistered_at:
                raise ValueError(f"replication {slot.slot_id} was not frozen with the matrix")
            if preregistration.candidate_audits != derive_materials_candidate_audits(
                expected_protocol
            ):
                raise ValueError(f"replication {slot.slot_id} selection audit is invalid")
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


def build_materials_replication_plan(
    *,
    plan_id: str,
    manifest: ExperimentCapabilityManifest,
    registry: CapabilityRegistrySnapshot,
    base_protocol: MaterialsK3Protocol,
    protocol_frozen_at: datetime,
    preregistered_at: datetime,
    frozen_at: datetime,
    aggregation_rule: MaterialsReplicationAggregationRule | None = None,
) -> MaterialsReplicationPlan:
    """Freeze every registered stochastic slot before the first observation."""

    if not any(item.manifest_sha256 == manifest.manifest_sha256 for item in registry.manifests):
        raise ValueError("capability manifest is absent from the supplied registry snapshot")
    implementation = materials_k3_implementation_sha256()
    if _role(manifest, CapabilityRole.EXECUTOR).implementation_sha256 != implementation:
        raise ValueError("current materials executor differs from the capability manifest")
    if _role(manifest, CapabilityRole.VALIDATOR).implementation_sha256 != implementation:
        raise ValueError("current materials validator differs from the capability manifest")
    rule = aggregation_rule or MaterialsReplicationAggregationRule()
    seeds = manifest.nondeterminism_policy.frozen_seeds
    slots: list[MaterialsReplicationSlotCommitment] = []
    for ordinal, seed in enumerate(seeds, start=1):
        protocol = _derive_protocol(
            plan_id=plan_id,
            base_protocol=base_protocol,
            manifest=manifest,
            seed=seed,
            ordinal=ordinal,
            frozen_at=protocol_frozen_at,
        )
        preregistration = build_materials_preregistration(
            preregistration_id=f"{plan_id}.slot-{ordinal:02d}.preregistration",
            protocol=protocol,
            preregistered_at=preregistered_at,
        )
        slots.append(
            MaterialsReplicationSlotCommitment(
                slot_id=f"slot-{ordinal:02d}",
                ordinal=ordinal,
                seed=seed,
                preregistration=preregistration,
            )
        )
    return MaterialsReplicationPlan(
        plan_id=plan_id,
        capability_manifest=manifest,
        capability_manifest_sha256=manifest.manifest_sha256,
        registry_snapshot_sha256=registry.snapshot_sha256,
        base_protocol=base_protocol,
        base_protocol_sha256=base_protocol.protocol_sha256,
        replication_implementation_sha256=materials_replication_implementation_sha256(),
        aggregation_rule=rule,
        slots=tuple(slots),
        protocol_frozen_at=protocol_frozen_at,
        preregistered_at=preregistered_at,
        frozen_at=frozen_at,
    )


class MaterialsReplicationReexecution(FrozenModel):
    schema_version: Literal[1] = 1
    index: int = Field(ge=1)
    signed_validation: SignedMaterialsValidation

    @property
    def reexecution_sha256(self) -> str:
        return content_sha256(self)


class MaterialsReplicationSlotEvidence(FrozenModel):
    schema_name: Literal["aletheia.materials_replication_slot_evidence"] = (
        "aletheia.materials_replication_slot_evidence"
    )
    schema_version: Literal[1] = 1
    slot_id: str
    slot_commitment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_observation: SignedMaterialsObservation
    exact_reexecutions: tuple[MaterialsReplicationReexecution, ...] = Field(min_length=2)
    update: MaterialsBeliefUpdate
    completed_at: AwareDatetime
    state: Literal["complete_all_results_retained"] = "complete_all_results_retained"

    @model_validator(mode="after")
    def _evidence_is_complete(self) -> "MaterialsReplicationSlotEvidence":
        if tuple(item.index for item in self.exact_reexecutions) != tuple(
            range(1, len(self.exact_reexecutions) + 1)
        ):
            raise ValueError("replication reexecution indexes must be canonical")
        observation_hash = self.signed_observation.envelope_sha256
        previous = self.signed_observation.observation.ended_at
        validation_hashes: list[str] = []
        for reexecution in self.exact_reexecutions:
            receipt = reexecution.signed_validation.receipt
            if receipt.observation_envelope_sha256 != observation_hash:
                raise ValueError("replication validation is bound to another observation")
            if receipt.validated_at <= previous:
                raise ValueError("replication validations must be strictly chronological")
            previous = receipt.validated_at
            validation_hashes.append(reexecution.signed_validation.envelope_sha256)
        if len(validation_hashes) != len(set(validation_hashes)):
            raise ValueError("replication exact reexecution envelopes must be distinct")
        primary = self.exact_reexecutions[-1].signed_validation
        if self.update.validation_envelope_sha256 != primary.envelope_sha256:
            raise ValueError("replication update must bind the final exact reexecution")
        if self.update.updated_at <= previous or self.completed_at < self.update.updated_at:
            raise ValueError("replication evidence completion chronology is invalid")
        return self

    @property
    def evidence_sha256(self) -> str:
        return content_sha256(self)


def assemble_materials_replication_slot_evidence(
    *,
    commitment: MaterialsReplicationSlotCommitment,
    signed_observation: SignedMaterialsObservation,
    exact_reexecutions: tuple[SignedMaterialsValidation, ...],
    update: MaterialsBeliefUpdate,
    required_exact_reexecutions: int,
    completed_at: datetime | None = None,
) -> MaterialsReplicationSlotEvidence:
    if len(exact_reexecutions) != required_exact_reexecutions:
        raise ValueError("replication slot does not contain every required exact reexecution")
    return MaterialsReplicationSlotEvidence(
        slot_id=commitment.slot_id,
        slot_commitment_sha256=commitment.commitment_sha256,
        signed_observation=signed_observation,
        exact_reexecutions=tuple(
            MaterialsReplicationReexecution(index=index, signed_validation=validation)
            for index, validation in enumerate(exact_reexecutions, start=1)
        ),
        update=update,
        completed_at=completed_at or _utcnow(),
    )


class MaterialsReplicationSlotSummary(FrozenModel):
    schema_version: Literal[1] = 1
    slot_id: str
    seed: int
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_envelope_sha256es: tuple[str, ...] = Field(min_length=2)
    outcome_id: MaterialsOutcomeId
    unseen_compression: float
    control_compression: float
    unseen_minus_control_delta: float
    delta_ci_lower: float
    delta_ci_upper: float
    bootstrap_probability_delta_above_zero: float = Field(ge=0, le=1)


class MaterialsReplicationAggregation(FrozenModel):
    schema_name: Literal["aletheia.materials_replication_aggregation"] = (
        "aletheia.materials_replication_aggregation"
    )
    schema_version: Literal[1] = 1
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256es: tuple[str, ...] = Field(min_length=3)
    slot_summaries: tuple[MaterialsReplicationSlotSummary, ...] = Field(min_length=3)
    outcome_counts: dict[MaterialsOutcomeId, int]
    pattern: MaterialsReplicationPattern
    consensus_outcome_id: MaterialsOutcomeId | None = None
    positive_delta_slots: int = Field(ge=0)
    ci_strictly_above_zero_slots: int = Field(ge=0)
    delta_mean: float
    delta_median: float
    delta_sample_sd: float = Field(ge=0)
    delta_minimum: float
    delta_maximum: float
    all_slots_included: Literal[True] = True
    all_required_reexecutions_present: Literal[True] = True
    early_stopping_used: Literal[False] = False
    best_of_n_used: Literal[False] = False
    joint_bayesian_update_performed: Literal[False] = False
    evidence_level: Literal["exploratory"] = "exploratory"
    mechanism_claim_disposition: Literal["withheld"] = "withheld"
    supports_capability_promotion: Literal[False] = False
    interpretation_codes: tuple[str, ...] = Field(min_length=1)
    promotion_blockers: tuple[str, ...] = Field(min_length=1)
    aggregated_at: AwareDatetime

    @model_validator(mode="after")
    def _summary_counts_are_consistent(self) -> "MaterialsReplicationAggregation":
        if set(self.outcome_counts) != set(MaterialsOutcomeId):
            raise ValueError("replication outcome counts must cover every frozen outcome")
        expected_counts = {outcome: 0 for outcome in MaterialsOutcomeId}
        for summary in self.slot_summaries:
            expected_counts[summary.outcome_id] += 1
        if self.outcome_counts != expected_counts:
            raise ValueError("replication outcome counts do not match slot summaries")
        if len(self.evidence_sha256es) != len(self.slot_summaries):
            raise ValueError("replication aggregation evidence/summary counts differ")
        if len(self.evidence_sha256es) != len(set(self.evidence_sha256es)):
            raise ValueError("replication aggregation repeats slot evidence")
        if self.delta_minimum > self.delta_maximum:
            raise ValueError("replication delta range is reversed")
        if (self.pattern is MaterialsReplicationPattern.PARTITION_SENSITIVE) != (
            self.consensus_outcome_id is None
        ):
            raise ValueError("replication pattern and consensus outcome disagree")
        return self

    @property
    def aggregation_sha256(self) -> str:
        return content_sha256(self)


def _pattern(
    *, counts: dict[MaterialsOutcomeId, int], consensus_slots: int
) -> tuple[MaterialsReplicationPattern, MaterialsOutcomeId | None]:
    mappings = (
        (
            MaterialsOutcomeId.UNSEEN_SPECIFIC,
            MaterialsReplicationPattern.CONSENSUS_UNSEEN_SPECIFIC,
        ),
        (
            MaterialsOutcomeId.GENERIC_SHRINKAGE,
            MaterialsReplicationPattern.CONSENSUS_GENERIC_SHRINKAGE,
        ),
        (
            MaterialsOutcomeId.NO_MATERIAL_COMPRESSION,
            MaterialsReplicationPattern.CONSENSUS_NO_MATERIAL_COMPRESSION,
        ),
        (
            MaterialsOutcomeId.AMBIGUOUS,
            MaterialsReplicationPattern.CONSENSUS_AMBIGUOUS,
        ),
    )
    for outcome, pattern in mappings:
        if counts[outcome] >= consensus_slots:
            return pattern, outcome
    return MaterialsReplicationPattern.PARTITION_SENSITIVE, None


def derive_materials_replication_aggregation(
    *,
    plan: MaterialsReplicationPlan,
    evidence: tuple[MaterialsReplicationSlotEvidence, ...],
    aggregated_at: datetime | None = None,
) -> MaterialsReplicationAggregation:
    """Aggregate all slots descriptively without pseudo-replicating the shared dataset."""

    if tuple(item.slot_id for item in evidence) != tuple(item.slot_id for item in plan.slots):
        raise ValueError("replication aggregation requires every slot in canonical order")
    rule = plan.aggregation_rule
    if len(evidence) != rule.required_slots:
        raise ValueError("replication aggregation cannot omit a frozen slot")
    counts = {outcome: 0 for outcome in MaterialsOutcomeId}
    summaries: list[MaterialsReplicationSlotSummary] = []
    deltas: list[float] = []
    for commitment, item in zip(plan.slots, evidence, strict=True):
        if item.slot_commitment_sha256 != commitment.commitment_sha256:
            raise ValueError(f"replication {item.slot_id} is bound to another commitment")
        if len(item.exact_reexecutions) != rule.required_exact_reexecutions_per_slot:
            raise ValueError(f"replication {item.slot_id} lacks required reexecutions")
        result = item.signed_observation.observation.result
        metrics = result.metrics
        counts[result.outcome_id] += 1
        deltas.append(metrics.unseen_minus_control_delta)
        summaries.append(
            MaterialsReplicationSlotSummary(
                slot_id=item.slot_id,
                seed=commitment.seed,
                preregistration_sha256=commitment.preregistration.preregistration_sha256,
                observation_envelope_sha256=item.signed_observation.envelope_sha256,
                validation_envelope_sha256es=tuple(
                    value.signed_validation.envelope_sha256 for value in item.exact_reexecutions
                ),
                outcome_id=result.outcome_id,
                unseen_compression=metrics.unseen_compression,
                control_compression=metrics.control_compression,
                unseen_minus_control_delta=metrics.unseen_minus_control_delta,
                delta_ci_lower=metrics.delta_ci_lower,
                delta_ci_upper=metrics.delta_ci_upper,
                bootstrap_probability_delta_above_zero=(
                    metrics.bootstrap_probability_delta_above_zero
                ),
            )
        )
    pattern, consensus_outcome = _pattern(counts=counts, consensus_slots=rule.consensus_slots)
    mean = statistics.fmean(deltas)
    sample_sd = statistics.stdev(deltas)
    if not all(math.isfinite(value) for value in (*deltas, mean, sample_sd)):
        raise ValueError("replication aggregation contains non-finite metrics")
    return MaterialsReplicationAggregation(
        plan_sha256=plan.plan_sha256,
        evidence_sha256es=tuple(item.evidence_sha256 for item in evidence),
        slot_summaries=tuple(summaries),
        outcome_counts=counts,
        pattern=pattern,
        consensus_outcome_id=consensus_outcome,
        positive_delta_slots=sum(value > 0 for value in deltas),
        ci_strictly_above_zero_slots=sum(item.delta_ci_lower > 0 for item in summaries),
        delta_mean=mean,
        delta_median=statistics.median(deltas),
        delta_sample_sd=sample_sd,
        delta_minimum=min(deltas),
        delta_maximum=max(deltas),
        interpretation_codes=(
            "all_five_preregistered_partitions_retained",
            "two_exact_recomputations_per_partition",
            "four_of_five_outcome_consensus_rule",
            "same_public_dataset_partitions_not_independent_replicates",
            "joint_bayesian_posterior_deliberately_not_computed",
            "retrospective_single_operator_exploratory_evidence",
            "mechanism_claim_withheld",
        ),
        promotion_blockers=(
            "agent_authored_validator",
            "domain_review_not_performed",
            "external_independent_replication_not_performed",
            "local_single_operator_key_custody",
            "public_retrospective_dataset",
            "source_specific_license_review_not_independently_completed",
        ),
        aggregated_at=aggregated_at or _utcnow(),
    )


class MaterialsReplicationBundle(FrozenModel):
    schema_name: Literal["aletheia.materials_replication_bundle"] = (
        "aletheia.materials_replication_bundle"
    )
    schema_version: Literal[1] = 1
    plan: MaterialsReplicationPlan
    slot_evidence: tuple[MaterialsReplicationSlotEvidence, ...]
    aggregation: MaterialsReplicationAggregation
    assembled_at: AwareDatetime

    @model_validator(mode="after")
    def _lineage_is_closed(self) -> "MaterialsReplicationBundle":
        if tuple(item.slot_id for item in self.slot_evidence) != tuple(
            item.slot_id for item in self.plan.slots
        ):
            raise ValueError("replication bundle omits or reorders frozen slots")
        for commitment, evidence in zip(self.plan.slots, self.slot_evidence, strict=True):
            preregistration = commitment.preregistration
            protocol = preregistration.protocol
            policy = protocol.evidence_policy
            observation = evidence.signed_observation.observation
            if evidence.slot_commitment_sha256 != commitment.commitment_sha256:
                raise ValueError("replication evidence is bound to another slot")
            if observation.preregistration_sha256 != preregistration.preregistration_sha256:
                raise ValueError("replication observation is bound to another preregistration")
            if (
                observation.protocol_sha256 != protocol.protocol_sha256
                or observation.selected_candidate_id != preregistration.selected_candidate_id
                or observation.implementation_sha256 != preregistration.implementation_sha256
                or observation.measurement_principal_sha256 != policy.measurement_principal_sha256
                or evidence.signed_observation.key_id != policy.measurement_key_id
            ):
                raise ValueError("replication observation violates the frozen identity")
            result = observation.result
            if (
                result.dataset.dataset_ref != protocol.dataset_ref
                or result.dataset.composition_column != protocol.composition_column
                or result.dataset.target_column != protocol.target_column
                or result.split.algorithm != protocol.split.algorithm
                or result.split.partition_seed != protocol.split.partition_seed
                or result.metrics.bootstrap_resamples != protocol.bootstrap.resamples
                or result.metrics.confidence_level != protocol.bootstrap.confidence_level
            ):
                raise ValueError("replication result violates the frozen analysis protocol")
            if result.outcome_id is not classify_materials_outcome(
                metrics=result.metrics, rule=protocol.outcome_rule
            ):
                raise ValueError("replication outcome is not mechanically classified")
            if observation.started_at <= self.plan.frozen_at:
                raise ValueError("replication measurement began before the matrix freeze")
            for reexecution in evidence.exact_reexecutions:
                receipt = reexecution.signed_validation.receipt
                if receipt.preregistration_sha256 != preregistration.preregistration_sha256:
                    raise ValueError("replication validation is bound to another preregistration")
                if receipt.recomputed_result_sha256 != observation.result.result_sha256:
                    raise ValueError("replication exact reexecution did not match the result")
                if (
                    receipt.implementation_sha256 != preregistration.implementation_sha256
                    or receipt.validation_principal_sha256 != policy.validation_principal_sha256
                    or reexecution.signed_validation.key_id != policy.validation_key_id
                ):
                    raise ValueError("replication validation violates the frozen identity")
            if evidence.update.preregistration_sha256 != preregistration.preregistration_sha256:
                raise ValueError("replication update is bound to another preregistration")
            if (
                evidence.update.observation_envelope_sha256
                != evidence.signed_observation.envelope_sha256
            ):
                raise ValueError("replication update is bound to another observation")
            if (
                evidence.update.observed_outcome_id is not result.outcome_id
                or evidence.update.update_principal_sha256 != policy.update_principal_sha256
            ):
                raise ValueError("replication update violates the validated outcome identity")
        expected = derive_materials_replication_aggregation(
            plan=self.plan,
            evidence=self.slot_evidence,
            aggregated_at=self.aggregation.aggregated_at,
        )
        if expected != self.aggregation:
            raise ValueError("replication aggregation is not mechanically derived")
        if self.assembled_at < self.aggregation.aggregated_at:
            raise ValueError("replication bundle predates its aggregation")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)


def assemble_materials_replication_bundle(
    *,
    plan: MaterialsReplicationPlan,
    evidence: tuple[MaterialsReplicationSlotEvidence, ...],
    aggregation: MaterialsReplicationAggregation,
    assembled_at: datetime | None = None,
) -> MaterialsReplicationBundle:
    return MaterialsReplicationBundle(
        plan=plan,
        slot_evidence=evidence,
        aggregation=aggregation,
        assembled_at=assembled_at or _utcnow(),
    )


def verify_materials_replication_bundle(
    *,
    bundle: MaterialsReplicationBundle,
    observation_key: bytes,
    validation_key: bytes,
    require_current_implementation: bool = True,
    physically_recompute: bool = False,
) -> None:
    """Verify signatures, derivations, frozen identities, and optionally all physical results."""

    plan = bundle.plan
    if require_current_implementation:
        if plan.replication_implementation_sha256 != materials_replication_implementation_sha256():
            raise ValueError("current replication implementation differs from the frozen plan")
        if (
            _role(plan.capability_manifest, CapabilityRole.EXECUTOR).implementation_sha256
            != materials_k3_implementation_sha256()
        ):
            raise ValueError("current materials implementation differs from the capability")
    for commitment, evidence in zip(plan.slots, bundle.slot_evidence, strict=True):
        preregistration = commitment.preregistration
        policy = preregistration.protocol.evidence_policy
        evidence.signed_observation.verify(
            key=observation_key, expected_key_id=policy.measurement_key_id
        )
        for reexecution in evidence.exact_reexecutions:
            reexecution.signed_validation.verify(
                key=validation_key, expected_key_id=policy.validation_key_id
            )
        primary = evidence.exact_reexecutions[-1].signed_validation
        expected_update = derive_materials_belief_update(
            preregistration=preregistration,
            signed_observation=evidence.signed_observation,
            signed_validation=primary,
            observation_key=observation_key,
            validation_key=validation_key,
            updated_at=evidence.update.updated_at,
        )
        if expected_update != evidence.update:
            raise ValueError(f"replication {commitment.slot_id} update is not derived")
        if physically_recompute:
            recomputed = run_materials_experiment(preregistration)
            if recomputed != evidence.signed_observation.observation.result:
                raise ValueError(f"replication {commitment.slot_id} physical audit differs")
    expected_aggregation = derive_materials_replication_aggregation(
        plan=plan,
        evidence=bundle.slot_evidence,
        aggregated_at=bundle.aggregation.aggregated_at,
    )
    if expected_aggregation != bundle.aggregation:
        raise ValueError("replication bundle aggregation is not derived")


__all__ = [
    "MaterialsReplicationAggregation",
    "MaterialsReplicationAggregationRule",
    "MaterialsReplicationBundle",
    "MaterialsReplicationPattern",
    "MaterialsReplicationPlan",
    "MaterialsReplicationReexecution",
    "MaterialsReplicationSlotCommitment",
    "MaterialsReplicationSlotEvidence",
    "MaterialsReplicationSlotSummary",
    "assemble_materials_replication_bundle",
    "assemble_materials_replication_slot_evidence",
    "build_materials_replication_plan",
    "derive_materials_replication_aggregation",
    "materials_replication_implementation_sha256",
    "verify_materials_replication_bundle",
]
