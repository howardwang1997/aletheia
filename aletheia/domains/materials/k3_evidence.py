"""Frozen, authenticated K3 evidence chain for a real materials experiment.

This module deliberately sits beside the generic F9 epistemic machinery.  The generic
machinery starts from a fully closed F8 novelty graph; this adapter supplies a narrow real-domain
scientific-exit limb without pretending that a retrospective public benchmark is fresh laboratory
evidence.  It provides the same essential boundaries:

* competing hypotheses and likelihoods are frozen before measurement;
* an observation-blind EIG selector chooses one discriminating experiment;
* a measurement principal signs the exact result;
* a separately keyed validator physically recomputes the result; and
* only the validated outcome can produce an immutable Bayesian update.

The first protocol uses ``matbench_expt_gap`` and asks whether the predicted band-gap spread is
more compressed for entirely unseen chemical systems than for a within-system holdout control.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_strings(values: tuple[str, ...] | list[str]) -> str:
    return content_sha256({"values": list(values)})


def materials_k3_implementation_sha256() -> str:
    """Identity of the exact implementation bytes used by measurement and validation."""

    return _hash_bytes(Path(__file__).read_bytes())


class MaterialsHypothesisRole(str, Enum):
    NULL = "null"
    PRIMARY = "primary"
    ALTERNATIVE = "alternative"


class MaterialsOutcomeId(str, Enum):
    UNSEEN_SPECIFIC = "unseen_system_specific_compression"
    GENERIC_SHRINKAGE = "generic_model_shrinkage"
    NO_MATERIAL_COMPRESSION = "no_material_compression"
    AMBIGUOUS = "ambiguous_pattern"


class MaterialsRevisionAction(str, Enum):
    RETAIN = "retain"
    NARROW = "narrow"
    RETIRE = "retire"


class MaterialsChainDisposition(str, Enum):
    QUALIFIED_COMPLETE = "qualified_real_materials_chain_complete"
    INSUFFICIENT_CONTRACTION = "valid_update_without_robust_contraction"


class MaterialsHypothesis(FrozenModel):
    schema_version: Literal[1] = 1
    hypothesis_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: int = Field(default=1, ge=1)
    role: MaterialsHypothesisRole
    statement: str = Field(min_length=1, max_length=4096)
    prior_probability: float = Field(gt=0, lt=1)


class MaterialsOutcome(FrozenModel):
    schema_version: Literal[1] = 1
    outcome_id: MaterialsOutcomeId
    description: str = Field(min_length=1, max_length=2048)


class MaterialsOutcomeProbability(FrozenModel):
    schema_version: Literal[1] = 1
    outcome_id: MaterialsOutcomeId
    probability: float = Field(ge=0, le=1)


class MaterialsHypothesisLikelihood(FrozenModel):
    schema_version: Literal[1] = 1
    hypothesis_id: str
    probabilities: tuple[MaterialsOutcomeProbability, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _probability_mass_is_complete(self) -> "MaterialsHypothesisLikelihood":
        outcome_ids = tuple(item.outcome_id for item in self.probabilities)
        if outcome_ids != tuple(sorted(set(outcome_ids), key=lambda item: item.value)):
            raise ValueError("likelihood outcomes must be unique and canonically ordered")
        if not math.isclose(
            sum(item.probability for item in self.probabilities),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("likelihood probabilities must sum to one")
        return self


class MaterialsLikelihoodScenario(FrozenModel):
    schema_version: Literal[1] = 1
    scenario_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    description: str = Field(min_length=1, max_length=2048)
    likelihoods: tuple[MaterialsHypothesisLikelihood, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _hypotheses_are_unique_and_ordered(self) -> "MaterialsLikelihoodScenario":
        hypothesis_ids = tuple(item.hypothesis_id for item in self.likelihoods)
        if hypothesis_ids != tuple(sorted(set(hypothesis_ids))):
            raise ValueError("scenario hypotheses must be unique and canonically ordered")
        return self


class MaterialsExperimentCandidate(FrozenModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    title: str = Field(min_length=1, max_length=512)
    protocol_kind: Literal[
        "unseen_system_vs_within_system_control",
        "random_holdout_compression_only",
    ]
    measurement_valid: bool
    capabilities_available: bool
    estimated_cost_usd: float = Field(ge=0)
    estimated_duration_minutes: float = Field(gt=0)
    likelihood_scenarios: tuple[MaterialsLikelihoodScenario, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def _scenario_set_is_canonical(self) -> "MaterialsExperimentCandidate":
        scenario_ids = tuple(item.scenario_id for item in self.likelihood_scenarios)
        if scenario_ids != tuple(sorted(set(scenario_ids))):
            raise ValueError("likelihood scenarios must be unique and canonically ordered")
        if "nominal" not in scenario_ids:
            raise ValueError("candidate requires a nominal likelihood scenario")
        hypothesis_sets = {
            tuple(item.hypothesis_id for item in scenario.likelihoods)
            for scenario in self.likelihood_scenarios
        }
        outcome_sets = {
            tuple(probability.outcome_id for probability in likelihood.probabilities)
            for scenario in self.likelihood_scenarios
            for likelihood in scenario.likelihoods
        }
        if len(hypothesis_sets) != 1 or len(outcome_sets) != 1:
            raise ValueError("all likelihood scenarios must cover the same hypotheses/outcomes")
        return self


class MaterialsModelSpec(FrozenModel):
    schema_version: Literal[1] = 1
    estimator: Literal["random_forest_regressor"] = "random_forest_regressor"
    n_estimators: int = Field(ge=50, le=2000)
    min_samples_leaf: int = Field(ge=1, le=100)
    max_features: float = Field(gt=0, le=1)
    random_state: int = Field(ge=0)
    n_jobs: Literal[1] = 1


class MaterialsSplitSpec(FrozenModel):
    schema_version: Literal[1] = 1
    algorithm: Literal["chemical_system_hash_partition_v1"] = "chemical_system_hash_partition_v1"
    partition_seed: int = Field(ge=0)
    unseen_row_fraction: float = Field(gt=0, lt=0.5)
    within_system_holdout_fraction: float = Field(gt=0, lt=0.5)


class MaterialsBootstrapSpec(FrozenModel):
    schema_version: Literal[1] = 1
    algorithm: Literal["chemical_system_cluster_bootstrap_v1"] = (
        "chemical_system_cluster_bootstrap_v1"
    )
    seed: int = Field(ge=0)
    resamples: int = Field(ge=200, le=100_000)
    confidence_level: float = Field(gt=0.5, lt=1)


class MaterialsOutcomeRule(FrozenModel):
    schema_version: Literal[1] = 1
    minimum_unseen_compression: float = Field(ge=0, le=1)
    minimum_unseen_minus_control_delta: float = Field(ge=0, le=1)
    minimum_delta_ci_lower: float = Field(ge=-1, le=1)
    minimum_generic_compression: float = Field(ge=0, le=1)
    generic_delta_tolerance: float = Field(ge=0, le=1)


class MaterialsEvidencePolicy(FrozenModel):
    schema_version: Literal[1] = 1
    evidence_scope: Literal["retrospective_internal_confirmation"]
    measurement_key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    validation_key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    measurement_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    update_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimum_effective_hypothesis_contraction: float = Field(ge=0, lt=1)
    retire_below_probability: float = Field(gt=0, lt=0.5)
    contamination_disclosure: str = Field(min_length=1, max_length=4096)
    custody_disclosure: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _principals_and_keys_are_independent(self) -> "MaterialsEvidencePolicy":
        if self.measurement_key_id == self.validation_key_id:
            raise ValueError("measurement and validation key IDs must differ")
        principals = {
            self.measurement_principal_sha256,
            self.validation_principal_sha256,
            self.update_principal_sha256,
        }
        if len(principals) != 3:
            raise ValueError("measurement, validation, and update principals must differ")
        return self


class MaterialsK3Protocol(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_protocol"] = "aletheia.materials_k3_protocol"
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    state: Literal["protocol_frozen"] = "protocol_frozen"
    research_question: str = Field(min_length=1, max_length=4096)
    dataset_ref: Literal["matbench_expt_gap"] = "matbench_expt_gap"
    composition_column: Literal["composition"] = "composition"
    target_column: Literal["gap expt"] = "gap expt"
    hypotheses: tuple[MaterialsHypothesis, ...] = Field(min_length=3)
    outcomes: tuple[MaterialsOutcome, ...] = Field(min_length=4, max_length=4)
    candidates: tuple[MaterialsExperimentCandidate, ...] = Field(min_length=2)
    model: MaterialsModelSpec
    split: MaterialsSplitSpec
    bootstrap: MaterialsBootstrapSpec
    outcome_rule: MaterialsOutcomeRule
    evidence_policy: MaterialsEvidencePolicy
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _protocol_is_a_closed_competing_model(self) -> "MaterialsK3Protocol":
        hypothesis_ids = tuple(item.hypothesis_id for item in self.hypotheses)
        if hypothesis_ids != tuple(sorted(set(hypothesis_ids))):
            raise ValueError("hypotheses must be unique and canonically ordered")
        roles = {item.role for item in self.hypotheses}
        if roles != set(MaterialsHypothesisRole):
            raise ValueError("protocol requires null, primary, and alternative hypotheses")
        if not math.isclose(
            sum(item.prior_probability for item in self.hypotheses),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("hypothesis priors must sum to one")
        outcome_ids = tuple(item.outcome_id for item in self.outcomes)
        expected_outcomes = tuple(sorted(MaterialsOutcomeId, key=lambda item: item.value))
        if outcome_ids != expected_outcomes:
            raise ValueError("protocol outcomes must be complete and canonically ordered")
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("candidates must be unique and canonically ordered")
        expected_hypotheses = tuple(sorted(hypothesis_ids))
        for candidate in self.candidates:
            for scenario in candidate.likelihood_scenarios:
                if (
                    tuple(item.hypothesis_id for item in scenario.likelihoods)
                    != expected_hypotheses
                ):
                    raise ValueError("candidate likelihoods do not cover protocol hypotheses")
                for likelihood in scenario.likelihoods:
                    if tuple(item.outcome_id for item in likelihood.probabilities) != outcome_ids:
                        raise ValueError("candidate likelihoods do not cover protocol outcomes")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class MaterialsCandidateSelectionAudit(FrozenModel):
    schema_version: Literal[1] = 1
    candidate_id: str
    feasible: bool
    blockers: tuple[str, ...]
    expected_information_gain_nats: float = Field(ge=0)
    normalized_information_gain: float = Field(ge=0, le=1)
    rank: int = Field(ge=1)
    selected: bool


class MaterialsPreregistration(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_preregistration"] = (
        "aletheia.materials_k3_preregistration"
    )
    schema_version: Literal[1] = 1
    preregistration_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    protocol: MaterialsK3Protocol
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_audits: tuple[MaterialsCandidateSelectionAudit, ...]
    selected_candidate_id: str
    observation_access_during_selection: Literal["none"] = "none"
    preregistered_at: AwareDatetime
    state: Literal["frozen_before_measurement"] = "frozen_before_measurement"

    @model_validator(mode="after")
    def _preregistration_is_mechanically_selected(self) -> "MaterialsPreregistration":
        if self.protocol_sha256 != self.protocol.protocol_sha256:
            raise ValueError("preregistration protocol hash is invalid")
        if self.preregistered_at < self.protocol.frozen_at:
            raise ValueError("preregistration predates the frozen protocol")
        expected = derive_materials_candidate_audits(self.protocol)
        if self.candidate_audits != expected:
            raise ValueError("candidate audits are not derived from the frozen protocol")
        selected = [item.candidate_id for item in expected if item.selected]
        if selected != [self.selected_candidate_id]:
            raise ValueError("selected candidate is not the unique EIG winner")
        return self

    @property
    def preregistration_sha256(self) -> str:
        return content_sha256(self)


def _entropy(probabilities: tuple[float, ...]) -> float:
    return -sum(value * math.log(value) for value in probabilities if value > 0)


def _nominal(candidate: MaterialsExperimentCandidate) -> MaterialsLikelihoodScenario:
    return next(
        scenario for scenario in candidate.likelihood_scenarios if scenario.scenario_id == "nominal"
    )


def _scenario_probability_map(
    scenario: MaterialsLikelihoodScenario,
) -> dict[str, dict[MaterialsOutcomeId, float]]:
    return {
        likelihood.hypothesis_id: {
            item.outcome_id: item.probability for item in likelihood.probabilities
        }
        for likelihood in scenario.likelihoods
    }


def expected_information_gain(
    *, protocol: MaterialsK3Protocol, candidate: MaterialsExperimentCandidate
) -> float:
    priors = {item.hypothesis_id: item.prior_probability for item in protocol.hypotheses}
    likelihoods = _scenario_probability_map(_nominal(candidate))
    prior_entropy = _entropy(tuple(priors.values()))
    expected_posterior_entropy = 0.0
    for outcome in MaterialsOutcomeId:
        marginal = sum(
            priors[hypothesis_id] * likelihoods[hypothesis_id][outcome] for hypothesis_id in priors
        )
        if marginal <= 0:
            continue
        posterior = tuple(
            priors[hypothesis_id] * likelihoods[hypothesis_id][outcome] / marginal
            for hypothesis_id in priors
        )
        expected_posterior_entropy += marginal * _entropy(posterior)
    return max(0.0, prior_entropy - expected_posterior_entropy)


def derive_materials_candidate_audits(
    protocol: MaterialsK3Protocol,
) -> tuple[MaterialsCandidateSelectionAudit, ...]:
    prior_entropy = _entropy(tuple(item.prior_probability for item in protocol.hypotheses))
    derived: list[tuple[MaterialsExperimentCandidate, tuple[str, ...], float]] = []
    for candidate in protocol.candidates:
        blockers: list[str] = []
        if not candidate.measurement_valid:
            blockers.append("measurement_not_validated")
        if not candidate.capabilities_available:
            blockers.append("capability_unavailable")
        derived.append(
            (
                candidate,
                tuple(blockers),
                expected_information_gain(protocol=protocol, candidate=candidate),
            )
        )
    ordered = sorted(
        derived,
        key=lambda item: (
            bool(item[1]),
            -item[2],
            item[0].estimated_cost_usd,
            item[0].estimated_duration_minutes,
            item[0].candidate_id,
        ),
    )
    feasible = [item for item in ordered if not item[1]]
    winner = feasible[0][0].candidate_id if feasible else None
    return tuple(
        MaterialsCandidateSelectionAudit(
            candidate_id=candidate.candidate_id,
            feasible=not blockers,
            blockers=blockers,
            expected_information_gain_nats=information_gain,
            normalized_information_gain=(
                information_gain / prior_entropy if prior_entropy else 0.0
            ),
            rank=rank,
            selected=candidate.candidate_id == winner,
        )
        for rank, (candidate, blockers, information_gain) in enumerate(ordered, start=1)
    )


def build_materials_preregistration(
    *,
    preregistration_id: str,
    protocol: MaterialsK3Protocol,
    preregistered_at: datetime | None = None,
) -> MaterialsPreregistration:
    audits = derive_materials_candidate_audits(protocol)
    selected = [item.candidate_id for item in audits if item.selected]
    if len(selected) != 1:
        raise ValueError("frozen materials protocol has no unique feasible experiment")
    return MaterialsPreregistration(
        preregistration_id=preregistration_id,
        protocol=protocol,
        protocol_sha256=protocol.protocol_sha256,
        implementation_sha256=materials_k3_implementation_sha256(),
        candidate_audits=audits,
        selected_candidate_id=selected[0],
        preregistered_at=preregistered_at or _utcnow(),
    )


class MaterialsDatasetReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    dataset_ref: str
    composition_column: str
    target_column: str
    row_count: int = Field(gt=0)
    feature_count: int = Field(gt=0)
    chemical_system_count: int = Field(gt=0)
    logical_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_names_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_matrix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_vector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chemical_system_vector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_versions: dict[str, str]


class MaterialsSplitReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    algorithm: str
    partition_seed: int
    train_rows: int = Field(gt=0)
    unseen_test_rows: int = Field(gt=1)
    within_system_control_rows: int = Field(gt=1)
    train_chemical_systems: int = Field(gt=0)
    unseen_chemical_systems: int = Field(gt=0)
    control_chemical_systems: int = Field(gt=0)
    train_membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unseen_membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_membership_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partitions_cover_dataset: Literal[True] = True
    unseen_groups_disjoint: Literal[True] = True
    control_groups_seen_in_training: Literal[True] = True


class MaterialsCompressionMetrics(FrozenModel):
    schema_version: Literal[1] = 1
    unseen_true_sd_ev: float = Field(gt=0)
    unseen_predicted_sd_ev: float = Field(ge=0)
    unseen_compression: float
    control_true_sd_ev: float = Field(gt=0)
    control_predicted_sd_ev: float = Field(ge=0)
    control_compression: float
    unseen_minus_control_delta: float
    delta_ci_lower: float
    delta_ci_upper: float
    bootstrap_probability_delta_above_zero: float = Field(ge=0, le=1)
    unseen_mae_ev: float = Field(ge=0)
    control_mae_ev: float = Field(ge=0)
    bootstrap_resamples: int = Field(gt=0)
    confidence_level: float = Field(gt=0.5, lt=1)

    @model_validator(mode="after")
    def _derived_metrics_are_consistent(self) -> "MaterialsCompressionMetrics":
        expected_unseen = 1.0 - self.unseen_predicted_sd_ev / self.unseen_true_sd_ev
        expected_control = 1.0 - self.control_predicted_sd_ev / self.control_true_sd_ev
        if not math.isclose(self.unseen_compression, expected_unseen, abs_tol=2e-11):
            raise ValueError("unseen compression is not derived from standard deviations")
        if not math.isclose(self.control_compression, expected_control, abs_tol=2e-11):
            raise ValueError("control compression is not derived from standard deviations")
        if not math.isclose(
            self.unseen_minus_control_delta,
            self.unseen_compression - self.control_compression,
            abs_tol=2e-11,
        ):
            raise ValueError("compression delta is not derived from component metrics")
        if self.delta_ci_lower > self.delta_ci_upper:
            raise ValueError("bootstrap interval is reversed")
        return self


class MaterialsExperimentResult(FrozenModel):
    schema_version: Literal[1] = 1
    dataset: MaterialsDatasetReceipt
    split: MaterialsSplitReceipt
    metrics: MaterialsCompressionMetrics
    outcome_id: MaterialsOutcomeId
    unseen_predictions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_predictions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fitted_model_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _split_matches_dataset(self) -> "MaterialsExperimentResult":
        split_rows = (
            self.split.train_rows
            + self.split.unseen_test_rows
            + self.split.within_system_control_rows
        )
        if split_rows != self.dataset.row_count:
            raise ValueError("materials split row counts do not cover the dataset")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


class MaterialsObservation(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_observation"] = "aletheia.materials_k3_observation"
    schema_version: Literal[1] = 1
    observation_id: str
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_id: str
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: MaterialsExperimentResult
    measurement_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def _chronology_is_valid(self) -> "MaterialsObservation":
        if self.ended_at < self.started_at:
            raise ValueError("materials observation ended before it started")
        return self

    @property
    def observation_sha256(self) -> str:
        return content_sha256(self)


class SignedMaterialsObservation(FrozenModel):
    schema_version: Literal[1] = 1
    observation: MaterialsObservation
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _message(observation: MaterialsObservation, key_id: str) -> bytes:
        payload = {"observation": observation.model_dump(mode="json"), "key_id": key_id}
        return b"aletheia-materials-k3-observation-v1\0" + canonical_json_bytes(payload)

    @classmethod
    def issue(
        cls, observation: MaterialsObservation, *, key_id: str, key: bytes
    ) -> "SignedMaterialsObservation":
        if len(key) < 32:
            raise ValueError("materials observation signing keys require at least 32 bytes")
        signature = hmac.new(key, cls._message(observation, key_id), hashlib.sha256).hexdigest()
        return cls(observation=observation, key_id=key_id, hmac_sha256=signature)

    def verify(self, *, key: bytes, expected_key_id: str) -> None:
        if self.key_id != expected_key_id:
            raise ValueError("materials observation key ID is not trusted")
        expected = hmac.new(
            key, self._message(self.observation, self.key_id), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, self.hmac_sha256):
            raise ValueError("materials observation signature is invalid")

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)


class MaterialsValidationReceipt(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_validation"] = "aletheia.materials_k3_validation"
    schema_version: Literal[1] = 1
    validation_id: str
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recomputed_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_signature_verified: Literal[True] = True
    physical_recomputation_performed: Literal[True] = True
    exact_result_match: Literal[True] = True
    protocol_adherence: Literal["exact"] = "exact"
    measurement_validity: Literal["valid"] = "valid"
    validated_at: AwareDatetime

    @property
    def validation_sha256(self) -> str:
        return content_sha256(self)


class SignedMaterialsValidation(FrozenModel):
    schema_version: Literal[1] = 1
    receipt: MaterialsValidationReceipt
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _message(receipt: MaterialsValidationReceipt, key_id: str) -> bytes:
        payload = {"receipt": receipt.model_dump(mode="json"), "key_id": key_id}
        return b"aletheia-materials-k3-validation-v1\0" + canonical_json_bytes(payload)

    @classmethod
    def issue(
        cls, receipt: MaterialsValidationReceipt, *, key_id: str, key: bytes
    ) -> "SignedMaterialsValidation":
        if len(key) < 32:
            raise ValueError("materials validation signing keys require at least 32 bytes")
        signature = hmac.new(key, cls._message(receipt, key_id), hashlib.sha256).hexdigest()
        return cls(receipt=receipt, key_id=key_id, hmac_sha256=signature)

    def verify(self, *, key: bytes, expected_key_id: str) -> None:
        if self.key_id != expected_key_id:
            raise ValueError("materials validation key ID is not trusted")
        expected = hmac.new(
            key, self._message(self.receipt, self.key_id), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, self.hmac_sha256):
            raise ValueError("materials validation signature is invalid")

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)


class MaterialsPosteriorProbability(FrozenModel):
    schema_version: Literal[1] = 1
    hypothesis_id: str
    prior_probability: float = Field(ge=0, le=1)
    realized_likelihood: float = Field(ge=0, le=1)
    posterior_probability: float = Field(ge=0, le=1)


class MaterialsSensitivityPosterior(FrozenModel):
    schema_version: Literal[1] = 1
    scenario_id: str
    probabilities: tuple[MaterialsPosteriorProbability, ...]
    winner_hypothesis_ids: tuple[str, ...]
    posterior_entropy_nats: float = Field(ge=0)
    effective_hypothesis_count: float = Field(ge=1)
    effective_count_contraction: float


class MaterialsHypothesisRevision(FrozenModel):
    schema_version: Literal[1] = 1
    hypothesis_id: str
    source_version: int = Field(ge=1)
    action: MaterialsRevisionAction
    rationale_code: str


class MaterialsBeliefUpdate(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_belief_update"] = (
        "aletheia.materials_k3_belief_update"
    )
    schema_version: Literal[1] = 1
    update_id: str
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_outcome_id: MaterialsOutcomeId
    prior_entropy_nats: float = Field(ge=0)
    prior_effective_hypothesis_count: float = Field(ge=1)
    scenario_posteriors: tuple[MaterialsSensitivityPosterior, ...] = Field(min_length=3)
    nominal_winner_hypothesis_ids: tuple[str, ...]
    winner_stable_across_sensitivity: bool
    minimum_effective_count_contraction: float
    hypothesis_space_contracted: bool
    revisions: tuple[MaterialsHypothesisRevision, ...]
    mechanism_claim_disposition: Literal["withheld_observational_model_diagnostic"] = (
        "withheld_observational_model_diagnostic"
    )
    update_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: AwareDatetime

    @property
    def update_sha256(self) -> str:
        return content_sha256(self)


class MaterialsScientificDecision(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_decision"] = "aletheia.materials_k3_decision"
    schema_version: Literal[1] = 1
    decision_id: str
    update_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: MaterialsChainDisposition
    alternatives_experiment_update_complete: Literal[True] = True
    real_dataset_used: Literal[True] = True
    hypothesis_space_contracted: bool
    formal_external_replication: Literal[False] = False
    formal_prospective_evidence: Literal[False] = False
    rationale_codes: tuple[str, ...] = Field(min_length=1)
    decided_at: AwareDatetime

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class MaterialsK3EvidenceBundle(FrozenModel):
    schema_name: Literal["aletheia.materials_k3_evidence_bundle"] = (
        "aletheia.materials_k3_evidence_bundle"
    )
    schema_version: Literal[1] = 1
    preregistration: MaterialsPreregistration
    signed_observation: SignedMaterialsObservation
    signed_validation: SignedMaterialsValidation
    update: MaterialsBeliefUpdate
    decision: MaterialsScientificDecision
    assembled_at: AwareDatetime

    @model_validator(mode="after")
    def _lineage_and_chronology_are_closed(self) -> "MaterialsK3EvidenceBundle":
        prereg = self.preregistration
        protocol = prereg.protocol
        policy = protocol.evidence_policy
        observation = self.signed_observation.observation
        validation = self.signed_validation.receipt
        if observation.preregistration_sha256 != prereg.preregistration_sha256:
            raise ValueError("observation is bound to another preregistration")
        if validation.preregistration_sha256 != prereg.preregistration_sha256:
            raise ValueError("validation is bound to another preregistration")
        if validation.observation_envelope_sha256 != self.signed_observation.envelope_sha256:
            raise ValueError("validation is bound to another observation envelope")
        if validation.recomputed_result_sha256 != observation.result.result_sha256:
            raise ValueError("validated result identity does not match the observation")
        if (
            observation.protocol_sha256 != protocol.protocol_sha256
            or observation.selected_candidate_id != prereg.selected_candidate_id
            or observation.implementation_sha256 != prereg.implementation_sha256
            or observation.measurement_principal_sha256 != policy.measurement_principal_sha256
            or self.signed_observation.key_id != policy.measurement_key_id
        ):
            raise ValueError("observation does not match the frozen protocol/measurement identity")
        if (
            observation.result.dataset.dataset_ref != protocol.dataset_ref
            or observation.result.dataset.composition_column != protocol.composition_column
            or observation.result.dataset.target_column != protocol.target_column
            or observation.result.split.algorithm != protocol.split.algorithm
            or observation.result.split.partition_seed != protocol.split.partition_seed
            or observation.result.metrics.bootstrap_resamples != protocol.bootstrap.resamples
            or observation.result.metrics.confidence_level != protocol.bootstrap.confidence_level
        ):
            raise ValueError("observation result does not match the frozen data/analysis protocol")
        expected_outcome = classify_materials_outcome(
            metrics=observation.result.metrics, rule=protocol.outcome_rule
        )
        if observation.result.outcome_id is not expected_outcome:
            raise ValueError("observed outcome is not derived from the frozen outcome rule")
        if (
            validation.implementation_sha256 != prereg.implementation_sha256
            or validation.validation_principal_sha256 != policy.validation_principal_sha256
            or self.signed_validation.key_id != policy.validation_key_id
        ):
            raise ValueError("validation does not match the frozen validator identity")
        if self.update.preregistration_sha256 != prereg.preregistration_sha256:
            raise ValueError("update is bound to another preregistration")
        if self.update.observation_envelope_sha256 != self.signed_observation.envelope_sha256:
            raise ValueError("update is bound to another observation")
        if self.update.validation_envelope_sha256 != self.signed_validation.envelope_sha256:
            raise ValueError("update is bound to another validation")
        if (
            self.update.observed_outcome_id is not observation.result.outcome_id
            or self.update.update_principal_sha256 != policy.update_principal_sha256
        ):
            raise ValueError("belief update does not match the validated outcome/update identity")
        if self.decision.update_sha256 != self.update.update_sha256:
            raise ValueError("decision is bound to another update")
        if not (
            prereg.preregistered_at
            < observation.started_at
            <= observation.ended_at
            < validation.validated_at
            < self.update.updated_at
            < self.decision.decided_at
            <= self.assembled_at
        ):
            raise ValueError("materials evidence chronology is invalid")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)


def _selected_candidate(preregistration: MaterialsPreregistration) -> MaterialsExperimentCandidate:
    return next(
        item
        for item in preregistration.protocol.candidates
        if item.candidate_id == preregistration.selected_candidate_id
    )


def _float_array_sha256(values: Any) -> str:
    import numpy as np

    array = np.asarray(values, dtype="<f8")
    return _hash_bytes(
        canonical_json_bytes({"shape": list(array.shape), "dtype": "float64-le"})
        + array.tobytes(order="C")
    )


def _dataset_inputs(protocol: MaterialsK3Protocol):
    import importlib.metadata

    import numpy as np

    from aletheia.domains.materials.datasets import load_benchmark
    from aletheia.domains.materials.featurizers import composition_groups, magpie_features

    frame = load_benchmark(protocol.dataset_ref)
    features, feature_names, work = magpie_features(frame, protocol.composition_column)
    targets = work[protocol.target_column].to_numpy(dtype=float)
    groups = np.asarray(composition_groups(work), dtype=str)
    compositions = tuple(str(value) for value in work[protocol.composition_column].tolist())
    row_ids = tuple(
        _hash_bytes(
            canonical_json_bytes(
                {
                    "ordinal": ordinal,
                    "composition": composition,
                    "target_float_hex": float(target).hex(),
                }
            )
        )
        for ordinal, (composition, target) in enumerate(zip(compositions, targets, strict=True))
    )
    logical_rows = tuple(
        {
            "row_id": row_id,
            "composition": composition,
            "target_float_hex": float(target).hex(),
        }
        for row_id, composition, target in zip(row_ids, compositions, targets, strict=True)
    )
    versions = {}
    for package in ("matminer", "pymatgen", "scikit-learn", "numpy", "pandas"):
        versions[package] = importlib.metadata.version(package)
    receipt = MaterialsDatasetReceipt(
        dataset_ref=protocol.dataset_ref,
        composition_column=protocol.composition_column,
        target_column=protocol.target_column,
        row_count=len(targets),
        feature_count=len(feature_names),
        chemical_system_count=len(set(groups)),
        logical_rows_sha256=content_sha256({"rows": logical_rows}),
        feature_names_sha256=_hash_strings(tuple(feature_names)),
        feature_matrix_sha256=_float_array_sha256(features.to_numpy(dtype=float)),
        target_vector_sha256=_float_array_sha256(targets),
        chemical_system_vector_sha256=_hash_strings(groups.tolist()),
        package_versions=versions,
    )
    return features, targets, groups, row_ids, receipt


def _partition(
    *, groups: Any, row_ids: tuple[str, ...], spec: MaterialsSplitSpec
) -> tuple[Any, Any, Any, MaterialsSplitReceipt]:
    import numpy as np

    group_array = np.asarray(groups, dtype=str)
    unique_groups = sorted(
        set(group_array),
        key=lambda value: _hash_bytes(f"{spec.partition_seed}:group:{value}".encode()),
    )
    target_unseen_rows = round(spec.unseen_row_fraction * len(group_array))
    unseen_groups: list[str] = []
    unseen_rows = 0
    for group in unique_groups:
        if unseen_rows >= target_unseen_rows:
            break
        unseen_groups.append(group)
        unseen_rows += int((group_array == group).sum())
    unseen_set = set(unseen_groups)
    unseen_indices = np.flatnonzero(np.isin(group_array, list(unseen_set)))
    remaining_indices = np.flatnonzero(~np.isin(group_array, list(unseen_set)))
    train: list[int] = []
    control: list[int] = []
    for group in sorted(set(group_array[remaining_indices])):
        group_indices = remaining_indices[group_array[remaining_indices] == group]
        ordered = sorted(
            group_indices.tolist(),
            key=lambda index: _hash_bytes(f"{spec.partition_seed}:row:{row_ids[index]}".encode()),
        )
        if len(ordered) >= 2:
            held = max(1, round(spec.within_system_holdout_fraction * len(ordered)))
            held = min(held, len(ordered) - 1)
            control.extend(ordered[:held])
            train.extend(ordered[held:])
        else:
            train.extend(ordered)
    train_indices = np.asarray(sorted(train), dtype=int)
    control_indices = np.asarray(sorted(control), dtype=int)
    unseen_indices = np.asarray(sorted(unseen_indices.tolist()), dtype=int)
    partition_sets = [set(train_indices), set(unseen_indices), set(control_indices)]
    if any(
        left & right
        for index, left in enumerate(partition_sets)
        for right in partition_sets[index + 1 :]
    ):
        raise ValueError("materials split partitions overlap")
    if set().union(*partition_sets) != set(range(len(group_array))):
        raise ValueError("materials split does not cover the dataset")
    train_groups = set(group_array[train_indices])
    unseen_group_values = set(group_array[unseen_indices])
    control_groups = set(group_array[control_indices])
    if train_groups & unseen_group_values:
        raise ValueError("unseen chemical systems leaked into training")
    if not control_groups.issubset(train_groups):
        raise ValueError("control contains a chemical system absent from training")
    receipt = MaterialsSplitReceipt(
        algorithm=spec.algorithm,
        partition_seed=spec.partition_seed,
        train_rows=len(train_indices),
        unseen_test_rows=len(unseen_indices),
        within_system_control_rows=len(control_indices),
        train_chemical_systems=len(train_groups),
        unseen_chemical_systems=len(unseen_group_values),
        control_chemical_systems=len(control_groups),
        train_membership_sha256=_hash_strings([row_ids[index] for index in train_indices]),
        unseen_membership_sha256=_hash_strings([row_ids[index] for index in unseen_indices]),
        control_membership_sha256=_hash_strings([row_ids[index] for index in control_indices]),
    )
    return train_indices, unseen_indices, control_indices, receipt


def _compression(target: Any, prediction: Any) -> tuple[float, float, float]:
    import numpy as np

    target_sd = float(np.std(target, ddof=1))
    prediction_sd = float(np.std(prediction, ddof=1))
    if target_sd <= 0:
        raise ValueError("target standard deviation must be positive")
    return target_sd, prediction_sd, 1.0 - prediction_sd / target_sd


def _cluster_bootstrap_delta(
    *,
    unseen_targets: Any,
    unseen_predictions: Any,
    unseen_groups: Any,
    control_targets: Any,
    control_predictions: Any,
    control_groups: Any,
    spec: MaterialsBootstrapSpec,
) -> tuple[float, float, float]:
    import numpy as np

    rng = np.random.default_rng(spec.seed)

    def blocks(groups: Any) -> tuple[Any, ...]:
        values = np.asarray(groups, dtype=str)
        return tuple(np.flatnonzero(values == group) for group in sorted(set(values)))

    unseen_blocks = blocks(unseen_groups)
    control_blocks = blocks(control_groups)
    deltas = np.empty(spec.resamples, dtype=float)
    for iteration in range(spec.resamples):
        sampled_unseen = np.concatenate(
            [
                unseen_blocks[index]
                for index in rng.integers(0, len(unseen_blocks), len(unseen_blocks))
            ]
        )
        sampled_control = np.concatenate(
            [
                control_blocks[index]
                for index in rng.integers(0, len(control_blocks), len(control_blocks))
            ]
        )
        unseen_compression = _compression(
            unseen_targets[sampled_unseen], unseen_predictions[sampled_unseen]
        )[2]
        control_compression = _compression(
            control_targets[sampled_control], control_predictions[sampled_control]
        )[2]
        deltas[iteration] = unseen_compression - control_compression
    tail = (1.0 - spec.confidence_level) / 2.0
    return (
        float(np.quantile(deltas, tail)),
        float(np.quantile(deltas, 1.0 - tail)),
        float(np.mean(deltas > 0.0)),
    )


def classify_materials_outcome(
    *, metrics: MaterialsCompressionMetrics, rule: MaterialsOutcomeRule
) -> MaterialsOutcomeId:
    if (
        metrics.unseen_compression >= rule.minimum_unseen_compression
        and metrics.unseen_minus_control_delta >= rule.minimum_unseen_minus_control_delta
        and metrics.delta_ci_lower > rule.minimum_delta_ci_lower
    ):
        return MaterialsOutcomeId.UNSEEN_SPECIFIC
    if (
        metrics.unseen_compression >= rule.minimum_generic_compression
        and metrics.control_compression >= rule.minimum_generic_compression
        and abs(metrics.unseen_minus_control_delta) <= rule.generic_delta_tolerance
    ):
        return MaterialsOutcomeId.GENERIC_SHRINKAGE
    if (
        metrics.unseen_compression < rule.minimum_generic_compression
        and metrics.control_compression < rule.minimum_generic_compression
    ):
        return MaterialsOutcomeId.NO_MATERIAL_COMPRESSION
    return MaterialsOutcomeId.AMBIGUOUS


def run_materials_experiment(
    preregistration: MaterialsPreregistration,
) -> MaterialsExperimentResult:
    """Load the real benchmark and execute only the preregistered selected experiment."""

    import numpy as np
    from sklearn.ensemble import RandomForestRegressor

    protocol = preregistration.protocol
    candidate = _selected_candidate(preregistration)
    if candidate.protocol_kind != "unseen_system_vs_within_system_control":
        raise ValueError("the selected materials candidate has no registered executor")
    features, targets, groups, row_ids, dataset_receipt = _dataset_inputs(protocol)
    train, unseen, control, split_receipt = _partition(
        groups=groups, row_ids=row_ids, spec=protocol.split
    )
    model = RandomForestRegressor(
        n_estimators=protocol.model.n_estimators,
        min_samples_leaf=protocol.model.min_samples_leaf,
        max_features=protocol.model.max_features,
        random_state=protocol.model.random_state,
        n_jobs=protocol.model.n_jobs,
    )
    model.fit(features.iloc[train], targets[train])
    unseen_predictions = model.predict(features.iloc[unseen])
    control_predictions = model.predict(features.iloc[control])
    unseen_true_sd, unseen_predicted_sd, unseen_compression = _compression(
        targets[unseen], unseen_predictions
    )
    control_true_sd, control_predicted_sd, control_compression = _compression(
        targets[control], control_predictions
    )
    ci_lower, ci_upper, probability_above_zero = _cluster_bootstrap_delta(
        unseen_targets=targets[unseen],
        unseen_predictions=unseen_predictions,
        unseen_groups=groups[unseen],
        control_targets=targets[control],
        control_predictions=control_predictions,
        control_groups=groups[control],
        spec=protocol.bootstrap,
    )

    def rounded(value: float) -> float:
        return round(float(value), 12)

    metrics = MaterialsCompressionMetrics(
        unseen_true_sd_ev=rounded(unseen_true_sd),
        unseen_predicted_sd_ev=rounded(unseen_predicted_sd),
        unseen_compression=rounded(unseen_compression),
        control_true_sd_ev=rounded(control_true_sd),
        control_predicted_sd_ev=rounded(control_predicted_sd),
        control_compression=rounded(control_compression),
        unseen_minus_control_delta=rounded(unseen_compression - control_compression),
        delta_ci_lower=rounded(ci_lower),
        delta_ci_upper=rounded(ci_upper),
        bootstrap_probability_delta_above_zero=rounded(probability_above_zero),
        unseen_mae_ev=rounded(np.mean(np.abs(unseen_predictions - targets[unseen]))),
        control_mae_ev=rounded(np.mean(np.abs(control_predictions - targets[control]))),
        bootstrap_resamples=protocol.bootstrap.resamples,
        confidence_level=protocol.bootstrap.confidence_level,
    )
    outcome = classify_materials_outcome(metrics=metrics, rule=protocol.outcome_rule)
    fit_identity = content_sha256(
        {
            "model": protocol.model.model_dump(mode="json"),
            "dataset_sha256": dataset_receipt.logical_rows_sha256,
            "train_membership_sha256": split_receipt.train_membership_sha256,
            "unseen_predictions_sha256": _float_array_sha256(unseen_predictions),
            "control_predictions_sha256": _float_array_sha256(control_predictions),
        }
    )
    return MaterialsExperimentResult(
        dataset=dataset_receipt,
        split=split_receipt,
        metrics=metrics,
        outcome_id=outcome,
        unseen_predictions_sha256=_float_array_sha256(unseen_predictions),
        control_predictions_sha256=_float_array_sha256(control_predictions),
        fitted_model_identity_sha256=fit_identity,
    )


def measure_materials_experiment(
    *,
    preregistration: MaterialsPreregistration,
    signing_key: bytes,
    started_at: datetime | None = None,
) -> SignedMaterialsObservation:
    implementation = materials_k3_implementation_sha256()
    if implementation != preregistration.implementation_sha256:
        raise ValueError("measurement implementation differs from preregistration")
    start = started_at or _utcnow()
    if start <= preregistration.preregistered_at:
        raise ValueError("measurement must start after preregistration")
    result = run_materials_experiment(preregistration)
    ended_at = _utcnow()
    if ended_at < start:
        ended_at = start
    policy = preregistration.protocol.evidence_policy
    observation = MaterialsObservation(
        observation_id=f"{preregistration.preregistration_id}.observation",
        preregistration_sha256=preregistration.preregistration_sha256,
        protocol_sha256=preregistration.protocol_sha256,
        selected_candidate_id=preregistration.selected_candidate_id,
        implementation_sha256=implementation,
        result=result,
        measurement_principal_sha256=policy.measurement_principal_sha256,
        started_at=start,
        ended_at=ended_at,
    )
    return SignedMaterialsObservation.issue(
        observation, key_id=policy.measurement_key_id, key=signing_key
    )


def validate_materials_observation(
    *,
    preregistration: MaterialsPreregistration,
    signed_observation: SignedMaterialsObservation,
    observation_key: bytes,
    validation_key: bytes,
    validated_at: datetime | None = None,
) -> SignedMaterialsValidation:
    policy = preregistration.protocol.evidence_policy
    signed_observation.verify(key=observation_key, expected_key_id=policy.measurement_key_id)
    observation = signed_observation.observation
    if observation.preregistration_sha256 != preregistration.preregistration_sha256:
        raise ValueError("observation is bound to another preregistration")
    implementation = materials_k3_implementation_sha256()
    if implementation != preregistration.implementation_sha256:
        raise ValueError("validation implementation differs from preregistration")
    recomputed = run_materials_experiment(preregistration)
    if recomputed != observation.result:
        raise ValueError("physical validation does not reproduce the signed materials result")
    timestamp = validated_at or _utcnow()
    if timestamp <= observation.ended_at:
        raise ValueError("validation must postdate measurement")
    receipt = MaterialsValidationReceipt(
        validation_id=f"{preregistration.preregistration_id}.validation",
        preregistration_sha256=preregistration.preregistration_sha256,
        observation_envelope_sha256=signed_observation.envelope_sha256,
        recomputed_result_sha256=recomputed.result_sha256,
        implementation_sha256=implementation,
        validation_principal_sha256=policy.validation_principal_sha256,
        validated_at=timestamp,
    )
    return SignedMaterialsValidation.issue(
        receipt, key_id=policy.validation_key_id, key=validation_key
    )


def _posterior_for_scenario(
    *,
    protocol: MaterialsK3Protocol,
    scenario: MaterialsLikelihoodScenario,
    outcome: MaterialsOutcomeId,
    prior_effective_count: float,
) -> MaterialsSensitivityPosterior:
    priors = {item.hypothesis_id: item.prior_probability for item in protocol.hypotheses}
    likelihoods = _scenario_probability_map(scenario)
    masses = {
        hypothesis_id: priors[hypothesis_id] * likelihoods[hypothesis_id][outcome]
        for hypothesis_id in priors
    }
    normalizer = sum(masses.values())
    if normalizer <= 0:
        raise ValueError(f"scenario {scenario.scenario_id!r} assigns zero mass to outcome")
    posteriors = {key: value / normalizer for key, value in masses.items()}
    maximum = max(posteriors.values())
    winners = tuple(sorted(key for key, value in posteriors.items() if value == maximum))
    posterior_entropy = _entropy(tuple(posteriors.values()))
    effective_count = math.exp(posterior_entropy)
    return MaterialsSensitivityPosterior(
        scenario_id=scenario.scenario_id,
        probabilities=tuple(
            MaterialsPosteriorProbability(
                hypothesis_id=hypothesis_id,
                prior_probability=priors[hypothesis_id],
                realized_likelihood=likelihoods[hypothesis_id][outcome],
                posterior_probability=posteriors[hypothesis_id],
            )
            for hypothesis_id in sorted(priors)
        ),
        winner_hypothesis_ids=winners,
        posterior_entropy_nats=posterior_entropy,
        effective_hypothesis_count=effective_count,
        effective_count_contraction=(prior_effective_count - effective_count)
        / prior_effective_count,
    )


def derive_materials_belief_update(
    *,
    preregistration: MaterialsPreregistration,
    signed_observation: SignedMaterialsObservation,
    signed_validation: SignedMaterialsValidation,
    observation_key: bytes,
    validation_key: bytes,
    updated_at: datetime | None = None,
) -> MaterialsBeliefUpdate:
    policy = preregistration.protocol.evidence_policy
    signed_observation.verify(key=observation_key, expected_key_id=policy.measurement_key_id)
    signed_validation.verify(key=validation_key, expected_key_id=policy.validation_key_id)
    validation = signed_validation.receipt
    if validation.observation_envelope_sha256 != signed_observation.envelope_sha256:
        raise ValueError("validation does not bind the supplied observation")
    if validation.preregistration_sha256 != preregistration.preregistration_sha256:
        raise ValueError("validation does not bind the supplied preregistration")
    if validation.recomputed_result_sha256 != signed_observation.observation.result.result_sha256:
        raise ValueError("validated result differs from the signed observation")
    timestamp = updated_at or _utcnow()
    if timestamp <= validation.validated_at:
        raise ValueError("belief update must postdate validation")
    protocol = preregistration.protocol
    candidate = _selected_candidate(preregistration)
    priors = tuple(item.prior_probability for item in protocol.hypotheses)
    prior_entropy = _entropy(priors)
    prior_effective_count = math.exp(prior_entropy)
    outcome = signed_observation.observation.result.outcome_id
    scenario_posteriors = tuple(
        _posterior_for_scenario(
            protocol=protocol,
            scenario=scenario,
            outcome=outcome,
            prior_effective_count=prior_effective_count,
        )
        for scenario in candidate.likelihood_scenarios
    )
    nominal = next(item for item in scenario_posteriors if item.scenario_id == "nominal")
    winner_stable = all(
        item.winner_hypothesis_ids == nominal.winner_hypothesis_ids for item in scenario_posteriors
    )
    minimum_contraction = min(item.effective_count_contraction for item in scenario_posteriors)
    contracted = (
        winner_stable and minimum_contraction >= policy.minimum_effective_hypothesis_contraction
    )
    maximum_sensitivity_probabilities = {
        hypothesis.hypothesis_id: max(
            probability.posterior_probability
            for scenario in scenario_posteriors
            for probability in scenario.probabilities
            if probability.hypothesis_id == hypothesis.hypothesis_id
        )
        for hypothesis in protocol.hypotheses
    }
    winner_set = set(nominal.winner_hypothesis_ids)
    revisions = tuple(
        MaterialsHypothesisRevision(
            hypothesis_id=hypothesis.hypothesis_id,
            source_version=hypothesis.version,
            action=(
                MaterialsRevisionAction.RETAIN
                if hypothesis.hypothesis_id in winner_set
                else MaterialsRevisionAction.RETIRE
                if maximum_sensitivity_probabilities[hypothesis.hypothesis_id]
                < policy.retire_below_probability
                else MaterialsRevisionAction.NARROW
            ),
            rationale_code=(
                "posterior_winner_stable"
                if hypothesis.hypothesis_id in winner_set
                else "posterior_below_retirement_floor_in_all_scenarios"
                if maximum_sensitivity_probabilities[hypothesis.hypothesis_id]
                < policy.retire_below_probability
                else "nonwinner_retained_under_likelihood_sensitivity"
            ),
        )
        for hypothesis in protocol.hypotheses
    )
    return MaterialsBeliefUpdate(
        update_id=f"{preregistration.preregistration_id}.update",
        preregistration_sha256=preregistration.preregistration_sha256,
        observation_envelope_sha256=signed_observation.envelope_sha256,
        validation_envelope_sha256=signed_validation.envelope_sha256,
        observed_outcome_id=outcome,
        prior_entropy_nats=prior_entropy,
        prior_effective_hypothesis_count=prior_effective_count,
        scenario_posteriors=scenario_posteriors,
        nominal_winner_hypothesis_ids=nominal.winner_hypothesis_ids,
        winner_stable_across_sensitivity=winner_stable,
        minimum_effective_count_contraction=minimum_contraction,
        hypothesis_space_contracted=contracted,
        revisions=revisions,
        update_principal_sha256=policy.update_principal_sha256,
        updated_at=timestamp,
    )


def derive_materials_scientific_decision(
    *, update: MaterialsBeliefUpdate, decided_at: datetime | None = None
) -> MaterialsScientificDecision:
    timestamp = decided_at or _utcnow()
    if timestamp <= update.updated_at:
        raise ValueError("materials decision must postdate belief update")
    contracted = update.hypothesis_space_contracted
    disposition = (
        MaterialsChainDisposition.QUALIFIED_COMPLETE
        if contracted
        else MaterialsChainDisposition.INSUFFICIENT_CONTRACTION
    )
    codes = [
        "real_matbench_expt_gap_dataset",
        "three_competing_hypotheses",
        "observation_blind_eig_selection",
        "preobservation_likelihood_commitment",
        "signed_measurement_receipt",
        "separately_keyed_physical_validation",
        "validated_outcome_only_bayesian_update",
        "mechanism_claim_withheld",
        "retrospective_internal_confirmation",
        "local_operator_key_custody_not_external_replication",
    ]
    codes.append(
        "robust_hypothesis_space_contraction"
        if contracted
        else "hypothesis_space_contraction_below_policy"
    )
    return MaterialsScientificDecision(
        decision_id=f"{update.update_id}.decision",
        update_sha256=update.update_sha256,
        disposition=disposition,
        hypothesis_space_contracted=contracted,
        rationale_codes=tuple(codes),
        decided_at=timestamp,
    )


def assemble_materials_evidence_bundle(
    *,
    preregistration: MaterialsPreregistration,
    signed_observation: SignedMaterialsObservation,
    signed_validation: SignedMaterialsValidation,
    update: MaterialsBeliefUpdate,
    decision: MaterialsScientificDecision,
    assembled_at: datetime | None = None,
) -> MaterialsK3EvidenceBundle:
    timestamp = assembled_at or _utcnow()
    if timestamp < decision.decided_at:
        raise ValueError("materials bundle cannot predate its decision")
    return MaterialsK3EvidenceBundle(
        preregistration=preregistration,
        signed_observation=signed_observation,
        signed_validation=signed_validation,
        update=update,
        decision=decision,
        assembled_at=timestamp,
    )


def verify_materials_evidence_bundle(
    *,
    bundle: MaterialsK3EvidenceBundle,
    observation_key: bytes,
    validation_key: bytes,
    require_current_implementation: bool = True,
) -> None:
    policy = bundle.preregistration.protocol.evidence_policy
    bundle.signed_observation.verify(key=observation_key, expected_key_id=policy.measurement_key_id)
    bundle.signed_validation.verify(key=validation_key, expected_key_id=policy.validation_key_id)
    if require_current_implementation and (
        bundle.preregistration.implementation_sha256 != materials_k3_implementation_sha256()
    ):
        raise ValueError("current materials implementation differs from preregistration")
    expected_update = derive_materials_belief_update(
        preregistration=bundle.preregistration,
        signed_observation=bundle.signed_observation,
        signed_validation=bundle.signed_validation,
        observation_key=observation_key,
        validation_key=validation_key,
        updated_at=bundle.update.updated_at,
    )
    if expected_update != bundle.update:
        raise ValueError("materials belief update is not mechanically derived")
    expected_decision = derive_materials_scientific_decision(
        update=bundle.update, decided_at=bundle.decision.decided_at
    )
    if expected_decision != bundle.decision:
        raise ValueError("materials scientific decision is not mechanically derived")


__all__ = [
    "MaterialsBeliefUpdate",
    "MaterialsCandidateSelectionAudit",
    "MaterialsChainDisposition",
    "MaterialsCompressionMetrics",
    "MaterialsDatasetReceipt",
    "MaterialsEvidencePolicy",
    "MaterialsExperimentCandidate",
    "MaterialsExperimentResult",
    "MaterialsHypothesis",
    "MaterialsHypothesisLikelihood",
    "MaterialsHypothesisRevision",
    "MaterialsHypothesisRole",
    "MaterialsK3EvidenceBundle",
    "MaterialsK3Protocol",
    "MaterialsLikelihoodScenario",
    "MaterialsModelSpec",
    "MaterialsObservation",
    "MaterialsOutcome",
    "MaterialsOutcomeId",
    "MaterialsOutcomeProbability",
    "MaterialsOutcomeRule",
    "MaterialsPosteriorProbability",
    "MaterialsPreregistration",
    "MaterialsScientificDecision",
    "MaterialsSensitivityPosterior",
    "MaterialsSplitReceipt",
    "MaterialsSplitSpec",
    "MaterialsValidationReceipt",
    "SignedMaterialsObservation",
    "SignedMaterialsValidation",
    "assemble_materials_evidence_bundle",
    "build_materials_preregistration",
    "classify_materials_outcome",
    "derive_materials_belief_update",
    "derive_materials_candidate_audits",
    "derive_materials_scientific_decision",
    "expected_information_gain",
    "materials_k3_implementation_sha256",
    "measure_materials_experiment",
    "run_materials_experiment",
    "validate_materials_observation",
    "verify_materials_evidence_bundle",
]
