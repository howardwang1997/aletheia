"""F9-S4 immutable pre-observation prediction and likelihood commitments.

This module turns an authorized F9-S3 causal audit into a content-addressed prediction
commitment.  Probabilistic commitments are admitted only when their historical calibration,
sharpness, pairwise discrimination, and sensitivity checks pass.  Ordinal commitments remain
available when calibrated probabilities are unavailable, but are explicitly ineligible for EIG.

The observation staging store is intentionally part of this boundary: it re-loads the committed
campaign from its immutable archive before accepting bytes, seals an experiment namespace on the
first observation, and records any later attempt to use a different substantive commitment as a
security and scientific-integrity violation.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.epistemics.causal import (
    CausalAdapterRuntime,
    CausalAuditCampaign,
    CausalAuditDisposition,
    CausalClaimCeiling,
    CausalEvidenceKind,
)
from aletheia.epistemics.schemas import EpistemicModel
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_LOCAL_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,79}$"
_ACTOR_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$"
_PROBABILITY_TOLERANCE = 1e-9
_METRIC_DIGITS = 12


class PredictionMode(str, Enum):
    PROBABILISTIC = "probabilistic"
    ORDINAL = "ordinal"


class OutcomeSchemaKind(str, Enum):
    CATEGORICAL = "categorical"
    CONTINUOUS_BINNED = "continuous_binned"


class PredictionCommitmentDisposition(str, Enum):
    READY = "ready"
    BLOCKED_CALIBRATION = "blocked_calibration"
    BLOCKED_DEGENERACY = "blocked_degeneracy"
    BLOCKED_EXECUTION = "blocked_execution"


class PredictionCommitmentFailureKind(str, Enum):
    AUTHOR_ERROR = "author_error"
    AUTHOR_OUTPUT_INVALID = "author_output_invalid"


class OutcomeBin(EpistemicModel):
    bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    label: str = Field(min_length=1, max_length=512)
    order_index: int = Field(ge=0, le=127)
    lower_bound: float | None = None
    upper_bound: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = False

    @model_validator(mode="after")
    def _bin_is_finite_and_nonempty(self) -> "OutcomeBin":
        if not self.label.strip():
            raise ValueError("outcome-bin label cannot be blank")
        for bound in (self.lower_bound, self.upper_bound):
            if bound is not None and not math.isfinite(bound):
                raise ValueError("outcome-bin finite bounds must be finite numbers")
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound >= self.upper_bound
        ):
            raise ValueError("outcome-bin lower bound must be below its upper bound")
        return self

    @property
    def bin_sha256(self) -> str:
        return content_sha256(self)


class OutcomeSchema(EpistemicModel):
    schema_version: Literal[1] = 1
    schema_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: OutcomeSchemaKind
    observable_id: str = Field(min_length=1, max_length=512)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_error_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    units: str | None = Field(default=None, max_length=256)
    bins: tuple[OutcomeBin, ...] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def _schema_is_canonical_and_complete(self) -> "OutcomeSchema":
        if not self.observable_id.strip():
            raise ValueError("outcome observable ID cannot be blank")
        if self.units is not None and not self.units.strip():
            raise ValueError("outcome units cannot be blank")
        identities = [item.bin_id for item in self.bins]
        orders = [item.order_index for item in self.bins]
        if len(identities) != len(set(identities)):
            raise ValueError("outcome bins require unique IDs")
        if orders != list(range(len(self.bins))):
            raise ValueError("outcome bins must be stored in contiguous preregistered order")
        has_bounds = any(
            item.lower_bound is not None or item.upper_bound is not None for item in self.bins
        )
        if self.kind is OutcomeSchemaKind.CATEGORICAL:
            if has_bounds:
                raise ValueError("categorical outcome bins cannot declare numeric bounds")
            return self
        if self.units is None:
            raise ValueError("continuous binned outcomes require units")
        if self.bins[0].lower_bound is not None or self.bins[-1].upper_bound is not None:
            raise ValueError("continuous bins must cover both open tails")
        for left, right in zip(self.bins, self.bins[1:]):
            if left.upper_bound is None or right.lower_bound is None:
                raise ValueError("only the outer continuous-bin bounds may be open")
            if left.upper_bound != right.lower_bound:
                raise ValueError("continuous bins must be contiguous without gaps or overlap")
            if left.upper_inclusive == right.lower_inclusive:
                raise ValueError("exactly one adjacent continuous bin must own each boundary")
        return self

    @property
    def outcome_schema_sha256(self) -> str:
        return content_sha256(self)


class ExperimentProtocol(EpistemicModel):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    experiment_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    causal_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    causal_contract_batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    causal_contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    estimand_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposed_evidence_kind: CausalEvidenceKind
    causal_claim_ceiling: CausalClaimCeiling
    intervention_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_population_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_measurement_process_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    observable_id: str = Field(min_length=1, max_length=512)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    analysis_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    exclusion_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    stopping_rule_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _protocol_text_is_explicit(self) -> "ExperimentProtocol":
        if not self.observable_id.strip():
            raise ValueError("experiment observable ID cannot be blank")
        expected_namespace = derive_experiment_namespace_sha256(
            experiment_id=self.experiment_id,
            causal_campaign_sha256=self.causal_campaign_sha256,
            estimand_sha256=self.estimand_sha256,
            intervention_protocol_sha256=self.intervention_protocol_sha256,
            target_population_sha256=self.target_population_sha256,
            outcome_schema_sha256=self.outcome_schema_sha256,
            analysis_plan_sha256=self.analysis_plan_sha256,
            exclusion_rule_sha256=self.exclusion_rule_sha256,
            stopping_rule_sha256=self.stopping_rule_sha256,
            observation_parser_sha256=self.observation_parser_sha256,
        )
        if self.experiment_namespace_sha256 != expected_namespace:
            raise ValueError("experiment namespace is not derived from the frozen protocol")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


def derive_experiment_namespace_sha256(
    *,
    experiment_id: str,
    causal_campaign_sha256: str,
    estimand_sha256: str,
    intervention_protocol_sha256: str,
    target_population_sha256: str,
    outcome_schema_sha256: str,
    analysis_plan_sha256: str,
    exclusion_rule_sha256: str,
    stopping_rule_sha256: str,
    observation_parser_sha256: str,
) -> str:
    """Derive the stable namespace sealed by the first corresponding observation."""

    return content_sha256(
        {
            "experiment_id": experiment_id,
            "causal_campaign_sha256": causal_campaign_sha256,
            "estimand_sha256": estimand_sha256,
            "intervention_protocol_sha256": intervention_protocol_sha256,
            "target_population_sha256": target_population_sha256,
            "outcome_schema_sha256": outcome_schema_sha256,
            "analysis_plan_sha256": analysis_plan_sha256,
            "exclusion_rule_sha256": exclusion_rule_sha256,
            "stopping_rule_sha256": stopping_rule_sha256,
            "observation_parser_sha256": observation_parser_sha256,
        }
    )


class OutcomeProbability(EpistemicModel):
    bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    probability: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _probability_is_finite(self) -> "OutcomeProbability":
        if not math.isfinite(self.probability):
            raise ValueError("outcome probability must be finite")
        return self


def _validate_probability_mass(
    probabilities: tuple[OutcomeProbability, ...], *, label: str
) -> None:
    identities = [item.bin_id for item in probabilities]
    if identities != sorted(set(identities)):
        raise ValueError(f"{label} probabilities must use unique canonical bin IDs")
    if not math.isclose(
        math.fsum(item.probability for item in probabilities),
        1.0,
        rel_tol=0.0,
        abs_tol=_PROBABILITY_TOLERANCE,
    ):
        raise ValueError(f"{label} probabilities must sum to one")


class CalibrationTrial(EpistemicModel):
    trial_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    validation_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    probabilities: tuple[OutcomeProbability, ...] = Field(min_length=2, max_length=128)
    observed_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    predicted_at: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _trial_is_pre_observation_and_normalized(self) -> "CalibrationTrial":
        _validate_probability_mass(self.probabilities, label="calibration-trial")
        if self.observed_bin_id not in {item.bin_id for item in self.probabilities}:
            raise ValueError("calibration observation is outside the predicted outcome space")
        if self.predicted_at >= self.observed_at:
            raise ValueError("calibration prediction must precede its observation")
        return self


class LikelihoodCalibrationMetrics(EpistemicModel):
    trial_count: int = Field(ge=1)
    multiclass_brier_score: float = Field(ge=0.0, le=2.0)
    mean_log_loss: float = Field(ge=0.0)
    top_label_ece: float = Field(ge=0.0, le=1.0)
    zero_probability_observations: int = Field(ge=0)

    @model_validator(mode="after")
    def _metrics_are_finite(self) -> "LikelihoodCalibrationMetrics":
        for value in (
            self.multiclass_brier_score,
            self.mean_log_loss,
            self.top_label_ece,
        ):
            if not math.isfinite(value):
                raise ValueError("calibration metrics must be finite")
        if self.zero_probability_observations > self.trial_count:
            raise ValueError("zero-probability observation count exceeds trial count")
        return self


def derive_likelihood_calibration_metrics(
    trials: tuple[CalibrationTrial, ...],
    *,
    scoring_epsilon: float,
    ece_bins: int,
) -> LikelihoodCalibrationMetrics:
    """Mechanically derive multiclass Brier, log loss, and top-label ECE."""

    if not trials:
        raise ValueError("calibration metrics require at least one trial")
    if not math.isfinite(scoring_epsilon) or not 0.0 < scoring_epsilon < 1.0:
        raise ValueError("calibration scoring epsilon must be finite and between zero and one")
    if not 2 <= ece_bins <= 100:
        raise ValueError("calibration ECE bin count must be between 2 and 100")
    brier_terms: list[float] = []
    log_terms: list[float] = []
    zero_count = 0
    ece_groups: list[list[tuple[float, float]]] = [[] for _ in range(ece_bins)]
    for trial in trials:
        by_bin = {item.bin_id: item.probability for item in trial.probabilities}
        observed_probability = by_bin[trial.observed_bin_id]
        if observed_probability == 0.0:
            zero_count += 1
        brier_terms.append(
            math.fsum(
                (probability - (1.0 if bin_id == trial.observed_bin_id else 0.0)) ** 2
                for bin_id, probability in by_bin.items()
            )
        )
        log_terms.append(-math.log(max(observed_probability, scoring_epsilon)))
        predicted_bin, confidence = max(sorted(by_bin.items()), key=lambda item: item[1])
        group_index = min(int(confidence * ece_bins), ece_bins - 1)
        ece_groups[group_index].append(
            (confidence, 1.0 if predicted_bin == trial.observed_bin_id else 0.0)
        )
    count = len(trials)
    ece = math.fsum(
        (len(group) / count)
        * abs(
            math.fsum(item[0] for item in group) / len(group)
            - math.fsum(item[1] for item in group) / len(group)
        )
        for group in ece_groups
        if group
    )
    return LikelihoodCalibrationMetrics(
        trial_count=count,
        multiclass_brier_score=round(math.fsum(brier_terms) / count, _METRIC_DIGITS),
        mean_log_loss=round(math.fsum(log_terms) / count, _METRIC_DIGITS),
        top_label_ece=round(ece, _METRIC_DIGITS),
        zero_probability_observations=zero_count,
    )


class LikelihoodCalibrationReport(EpistemicModel):
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    predictor_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_split_sha256: str = Field(pattern=_SHA256_PATTERN)
    trials: tuple[CalibrationTrial, ...] = Field(min_length=1, max_length=100_000)
    scoring_epsilon: float = Field(default=1e-12, gt=0.0, lt=1.0)
    ece_bins: int = Field(default=10, ge=2, le=100)
    metrics: LikelihoodCalibrationMetrics
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _report_is_complete_and_recomputed(self) -> "LikelihoodCalibrationReport":
        identities = [item.trial_id for item in self.trials]
        if identities != sorted(set(identities)):
            raise ValueError("calibration trials must use unique canonical IDs")
        namespaces = {item.validation_namespace_sha256 for item in self.trials}
        if namespaces != {self.validation_split_sha256}:
            raise ValueError("calibration trials must belong to the frozen validation split")
        bin_spaces = {tuple(item.bin_id for item in trial.probabilities) for trial in self.trials}
        if len(bin_spaces) != 1:
            raise ValueError("calibration trials must share one ordered outcome space")
        if self.completed_at < max(item.observed_at for item in self.trials):
            raise ValueError("calibration report predates a validation observation")
        expected = derive_likelihood_calibration_metrics(
            self.trials,
            scoring_epsilon=self.scoring_epsilon,
            ece_bins=self.ece_bins,
        )
        if self.metrics != expected:
            raise ValueError("calibration metrics are not mechanically derived from trials")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


CALIBRATION_REPORT_OUTPUT_SCHEMA_SHA256 = content_sha256(
    LikelihoodCalibrationReport.model_json_schema()
)


class CalibrationEvaluatorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    evaluator_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    runtime: CausalAdapterRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluator_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    observation_access: Literal["historical_validation_only"] = "historical_validation_only"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _evaluator_is_frozen_and_bounded(self) -> "CalibrationEvaluatorManifest":
        if self.output_schema_sha256 != CALIBRATION_REPORT_OUTPUT_SCHEMA_SHA256:
            raise ValueError("calibration evaluator uses another output schema")
        if self.tool_names:
            raise ValueError("calibration evaluator cannot receive ambient tool authority")
        model_fields = (
            self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        )
        if self.runtime is CausalAdapterRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model calibration evaluator requires frozen model transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic calibration evaluator cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class SensitivityPrediction(EpistemicModel):
    scenario_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    perturbation_sha256: str = Field(pattern=_SHA256_PATTERN)
    probabilities: tuple[OutcomeProbability, ...] = Field(min_length=2, max_length=128)

    @model_validator(mode="after")
    def _scenario_is_normalized(self) -> "SensitivityPrediction":
        _validate_probability_mass(self.probabilities, label="sensitivity-scenario")
        return self

    @property
    def scenario_sha256(self) -> str:
        return content_sha256(self)


class HypothesisPrediction(EpistemicModel):
    schema_version: Literal[1] = 1
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    hypothesis_version_sha256: str = Field(pattern=_SHA256_PATTERN)
    causal_graph_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_prediction_sha256: str = Field(pattern=_SHA256_PATTERN)
    observable_id: str = Field(min_length=1, max_length=512)
    measurement_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement_error_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_outcome_bin_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    mode: PredictionMode
    probabilities: tuple[OutcomeProbability, ...] = Field(default=(), max_length=128)
    ordinal_order: tuple[str, ...] = Field(default=(), max_length=128)
    likelihood_model_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    sensitivity_predictions: tuple[SensitivityPrediction, ...] = Field(default=(), max_length=32)
    rationale_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _prediction_has_exactly_one_mode(self) -> "HypothesisPrediction":
        if not self.observable_id.strip():
            raise ValueError("prediction observable ID cannot be blank")
        if self.mode is PredictionMode.PROBABILISTIC:
            if not self.probabilities or self.ordinal_order or self.likelihood_model_sha256 is None:
                raise ValueError(
                    "probabilistic prediction requires mass and a likelihood model only"
                )
            _validate_probability_mass(self.probabilities, label="hypothesis")
            scenario_ids = [item.scenario_id for item in self.sensitivity_predictions]
            if scenario_ids != sorted(set(scenario_ids)):
                raise ValueError("sensitivity scenarios require unique canonical IDs")
        elif self.probabilities or self.likelihood_model_sha256 is not None:
            raise ValueError("ordinal prediction cannot claim probabilities or a likelihood model")
        elif not self.ordinal_order or self.sensitivity_predictions:
            raise ValueError("ordinal prediction requires only a complete outcome ordering")
        if self.ordinal_order and (
            len(self.ordinal_order) != len(set(self.ordinal_order))
            or any(not item.strip() for item in self.ordinal_order)
        ):
            raise ValueError("ordinal prediction must contain unique non-blank bin IDs")
        return self

    @property
    def prediction_sha256(self) -> str:
        return content_sha256(self)


class PredictionBatch(EpistemicModel):
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    author_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    experiment_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    predictions: tuple[HypothesisPrediction, ...] = Field(min_length=3, max_length=64)
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _predictions_are_canonical(self) -> "PredictionBatch":
        identities = [item.hypothesis_id for item in self.predictions]
        if identities != sorted(set(identities)):
            raise ValueError("prediction batch requires unique canonical hypothesis IDs")
        return self

    @property
    def batch_sha256(self) -> str:
        return content_sha256(self)


PREDICTION_OUTPUT_SCHEMA_SHA256 = content_sha256(PredictionBatch.model_json_schema())


class PredictionAuthorManifest(EpistemicModel):
    schema_version: Literal[1] = 1
    author_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    runtime: CausalAdapterRuntime
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_sha256: str = Field(pattern=_SHA256_PATTERN)
    likelihood_family_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_schema_sha256: str = Field(pattern=_SHA256_PATTERN)
    instruction_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    model_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    author_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    maximum_hypotheses: int = Field(default=64, ge=3, le=64)
    maximum_outcome_bins: int = Field(default=128, ge=2, le=128)
    tool_names: tuple[str, ...] = ()
    tool_policy: Literal["none"] = "none"
    observation_access: Literal["none"] = "none"
    transport_policy: Literal["none", "model_transport_only"]
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _author_is_frozen_and_pre_observation(self) -> "PredictionAuthorManifest":
        if self.output_schema_sha256 != PREDICTION_OUTPUT_SCHEMA_SHA256:
            raise ValueError("prediction author uses another output schema")
        if self.tool_names:
            raise ValueError("prediction author cannot receive tool authority")
        model_fields = (
            self.instruction_sha256 is not None and self.model_identity_sha256 is not None
        )
        if self.runtime is CausalAdapterRuntime.MODEL:
            if not model_fields or self.transport_policy != "model_transport_only":
                raise ValueError("model prediction author requires frozen model transport")
        elif (
            self.instruction_sha256 is not None
            or self.model_identity_sha256 is not None
            or self.transport_policy != "none"
        ):
            raise ValueError("deterministic prediction author cannot declare model transport")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


def derive_hypothesis_likelihood_model_sha256(
    *,
    author_manifest: PredictionAuthorManifest,
    hypothesis_id: str,
    hypothesis_version_sha256: str,
    experiment_protocol_sha256: str,
    outcome_schema_sha256: str,
) -> str:
    """Lock one hypothesis likelihood to the calibrated frozen implementation family."""

    return content_sha256(
        {
            "schema": "aletheia.f9s4.hypothesis_likelihood.v1",
            "likelihood_family_sha256": author_manifest.likelihood_family_sha256,
            "hypothesis_id": hypothesis_id,
            "hypothesis_version_sha256": hypothesis_version_sha256,
            "experiment_protocol_sha256": experiment_protocol_sha256,
            "outcome_schema_sha256": outcome_schema_sha256,
        }
    )


class PredictionCommitmentPolicy(EpistemicModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    minimum_calibration_trials: int = Field(default=30, ge=10, le=100_000)
    maximum_brier_score: float = Field(default=0.8, gt=0.0, le=2.0)
    maximum_mean_log_loss: float = Field(default=1.5, gt=0.0)
    maximum_top_label_ece: float = Field(default=0.25, ge=0.0, le=1.0)
    maximum_zero_probability_observations: int = Field(default=0, ge=0)
    minimum_bin_probability: float = Field(default=0.01, ge=0.0, lt=0.5)
    maximum_bin_probability: float = Field(default=0.98, gt=0.5, le=1.0)
    minimum_entropy_nats: float = Field(default=0.2, ge=0.0)
    minimum_pairwise_total_variation: float = Field(default=0.1, gt=0.0, le=1.0)
    minimum_sensitivity_scenarios: int = Field(default=2, ge=1, le=32)
    maximum_sensitivity_total_variation: float = Field(default=0.25, ge=0.0, le=1.0)
    probability_tolerance: float = Field(default=_PROBABILITY_TOLERANCE, gt=0.0, le=1e-6)
    harness_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _thresholds_are_coherent(self) -> "PredictionCommitmentPolicy":
        numeric = (
            self.maximum_brier_score,
            self.maximum_mean_log_loss,
            self.maximum_top_label_ece,
            self.minimum_bin_probability,
            self.maximum_bin_probability,
            self.minimum_entropy_nats,
            self.minimum_pairwise_total_variation,
            self.maximum_sensitivity_total_variation,
            self.probability_tolerance,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("prediction policy thresholds must be finite")
        if self.minimum_bin_probability >= self.maximum_bin_probability:
            raise ValueError("prediction probability floor must be below its ceiling")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class PredictionCommitmentRequest(EpistemicModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    source_causal_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_hypothesis_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    world_model_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    causal_contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_mode: PredictionMode
    experiment_protocol: ExperimentProtocol
    outcome_schema: OutcomeSchema
    author_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    calibration_evaluator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    calibration_report: LikelihoodCalibrationReport | None = None
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    observation_access: Literal["none"] = "none"

    @model_validator(mode="after")
    def _request_mode_has_correct_evidence(self) -> "PredictionCommitmentRequest":
        if (
            self.experiment_protocol.outcome_schema_sha256
            != self.outcome_schema.outcome_schema_sha256
        ):
            raise ValueError("experiment protocol is not bound to the supplied outcome schema")
        if self.prediction_mode is PredictionMode.PROBABILISTIC:
            if self.calibration_report is None:
                raise ValueError("probabilistic prediction requires a frozen calibration report")
        elif self.calibration_report is not None:
            raise ValueError("ordinal prediction cannot attach a probability calibration report")
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class HypothesisPredictionDiagnostic(EpistemicModel):
    hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    prediction_sha256: str = Field(pattern=_SHA256_PATTERN)
    entropy_nats: float | None = Field(default=None, ge=0.0)
    minimum_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    maximum_sensitivity_total_variation: float | None = Field(default=None, ge=0.0, le=1.0)
    sensitivity_scenario_count: int = Field(ge=0, le=32)

    @property
    def diagnostic_sha256(self) -> str:
        return content_sha256(self)


class PairwisePredictionDiscrimination(EpistemicModel):
    left_hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    right_hypothesis_id: str = Field(pattern=r"^hyp_[0-9a-f]{32}$")
    total_variation_distance: float | None = Field(default=None, ge=0.0, le=1.0)
    ordinal_order_differs: bool | None = None

    @model_validator(mode="after")
    def _pair_is_canonical_and_typed(self) -> "PairwisePredictionDiscrimination":
        if self.left_hypothesis_id >= self.right_hypothesis_id:
            raise ValueError("prediction discrimination pair must use canonical order")
        if (self.total_variation_distance is None) == (self.ordinal_order_differs is None):
            raise ValueError("prediction discrimination requires exactly one mode-specific metric")
        return self

    @property
    def discrimination_sha256(self) -> str:
        return content_sha256(self)


class PredictionCommitmentProbe(EpistemicModel):
    prediction_batch_sha256: str = Field(pattern=_SHA256_PATTERN)
    calibration_report_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    hypothesis_diagnostics: tuple[HypothesisPredictionDiagnostic, ...]
    pairwise_discrimination: tuple[PairwisePredictionDiscrimination, ...]
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _probe_is_canonical(self) -> "PredictionCommitmentProbe":
        identities = [item.hypothesis_id for item in self.hypothesis_diagnostics]
        if identities != sorted(set(identities)):
            raise ValueError("prediction diagnostics must be unique and canonical")
        pairs = [
            (item.left_hypothesis_id, item.right_hypothesis_id)
            for item in self.pairwise_discrimination
        ]
        if pairs != sorted(set(pairs)):
            raise ValueError("pairwise prediction diagnostics must be unique and canonical")
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("prediction blockers must be unique and canonical")
        return self

    @property
    def probe_sha256(self) -> str:
        return content_sha256(self)


class PredictionCommitmentFailure(EpistemicModel):
    kind: PredictionCommitmentFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_output_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class _DerivedPredictionOutputs(EpistemicModel):
    probe: PredictionCommitmentProbe
    blockers: tuple[str, ...]
    disposition: PredictionCommitmentDisposition
    eig_eligible: bool


class PredictionCommitmentCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    source_causal_campaign: CausalAuditCampaign
    policy: PredictionCommitmentPolicy
    author_manifest: PredictionAuthorManifest
    calibration_evaluator_manifest: CalibrationEvaluatorManifest
    request: PredictionCommitmentRequest
    prediction_batch: PredictionBatch | None = None
    failure: PredictionCommitmentFailure | None = None
    probe: PredictionCommitmentProbe | None = None
    blockers: tuple[str, ...]
    disposition: PredictionCommitmentDisposition
    eig_eligible: bool
    generated_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_mechanically_derived(self) -> "PredictionCommitmentCampaign":
        _validate_request_bindings(
            source_causal_campaign=self.source_causal_campaign,
            policy=self.policy,
            author_manifest=self.author_manifest,
            evaluator_manifest=self.calibration_evaluator_manifest,
            request=self.request,
        )
        if self.failure is not None:
            expected = (f"execution_failure:{self.failure.kind.value}",)
            if (
                self.prediction_batch is not None
                or self.probe is not None
                or self.blockers != expected
                or self.disposition is not PredictionCommitmentDisposition.BLOCKED_EXECUTION
                or self.eig_eligible
            ):
                raise ValueError("failed prediction commitment outputs are not derived")
            if self.failure.occurred_at < self.request.issued_at:
                raise ValueError("prediction failure predates its request")
            if self.generated_at < self.failure.occurred_at:
                raise ValueError("prediction campaign predates its failure")
            return self
        if self.prediction_batch is None or self.probe is None:
            raise ValueError("successful prediction commitment requires a batch and probe")
        _validate_prediction_batch(
            source_causal_campaign=self.source_causal_campaign,
            request=self.request,
            manifest=self.author_manifest,
            policy=self.policy,
            batch=self.prediction_batch,
        )
        derived = _derive_prediction_outputs(
            request=self.request,
            policy=self.policy,
            batch=self.prediction_batch,
        )
        if (
            self.probe != derived.probe
            or self.blockers != derived.blockers
            or self.disposition is not derived.disposition
            or self.eig_eligible != derived.eig_eligible
        ):
            raise ValueError("prediction campaign outputs are not mechanically derived")
        if self.generated_at < self.prediction_batch.completed_at:
            raise ValueError("prediction campaign predates its completed batch")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)

    @property
    def commitment_sha256(self) -> str:
        """Substantive identity; campaign/request retry labels and timestamps are excluded."""

        return content_sha256(
            {
                "schema": "aletheia.f9s4.prediction_commitment.v1",
                "source_causal_campaign_sha256": self.source_causal_campaign.campaign_sha256,
                "policy_sha256": self.policy.policy_sha256,
                "author_manifest_sha256": self.author_manifest.manifest_sha256,
                "calibration_evaluator_manifest_sha256": (
                    self.calibration_evaluator_manifest.manifest_sha256
                ),
                "experiment_protocol": self.request.experiment_protocol.model_dump(mode="json"),
                "outcome_schema": self.request.outcome_schema.model_dump(mode="json"),
                "calibration_report_sha256": (
                    self.request.calibration_report.report_sha256
                    if self.request.calibration_report is not None
                    else None
                ),
                "predictions": (
                    [item.model_dump(mode="json") for item in self.prediction_batch.predictions]
                    if self.prediction_batch is not None
                    else None
                ),
                "probe": (
                    {
                        "calibration_report_sha256": self.probe.calibration_report_sha256,
                        "hypothesis_diagnostics": [
                            item.model_dump(mode="json")
                            for item in self.probe.hypothesis_diagnostics
                        ],
                        "pairwise_discrimination": [
                            item.model_dump(mode="json")
                            for item in self.probe.pairwise_discrimination
                        ],
                        "blockers": self.probe.blockers,
                    }
                    if self.probe is not None
                    else None
                ),
                "disposition": self.disposition.value,
                "eig_eligible": self.eig_eligible,
            }
        )


class CommittedPredictionCommitmentCampaign(EpistemicModel):
    schema_version: Literal[1] = 1
    campaign: PredictionCommitmentCampaign
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _ledger_commits_campaign(self) -> "CommittedPredictionCommitmentCampaign":
        payload = canonical_json_bytes(self.campaign)
        if (
            self.ledger.object_sha256 != self.campaign.campaign_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("prediction ledger does not commit its campaign and timestamp")
        if self.committed_at < self.campaign.generated_at:
            raise ValueError("prediction commitment predates campaign generation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class PredictionAuthorAdapter(Protocol):
    @property
    def manifest(self) -> PredictionAuthorManifest: ...

    async def predict(
        self,
        *,
        request: PredictionCommitmentRequest,
        source_causal_campaign: CausalAuditCampaign,
    ) -> object: ...


def _validate_actor_independence(
    *,
    source: CausalAuditCampaign,
    author: PredictionAuthorManifest,
    evaluator: CalibrationEvaluatorManifest,
) -> None:
    prior_principals = {
        source.source_campaign.generator_manifest.generator_principal_sha256,
        source.source_campaign.deduplicator_manifest.reviewer_principal_sha256,
        source.author_manifest.author_principal_sha256,
        source.reviewer_manifest.reviewer_principal_sha256,
    }
    if evaluator.evaluator_principal_sha256 in prior_principals | {author.author_principal_sha256}:
        raise ValueError("calibration evaluator must be independent from proposal/review roles")
    if (
        author.model_identity_sha256 is not None
        and evaluator.model_identity_sha256 == author.model_identity_sha256
    ):
        raise ValueError("calibration evaluator must use an independent model identity")


def _causal_components(source: CausalAuditCampaign):
    if source.contract_batch is None:
        raise ValueError("causal campaign has no contract")
    contract = source.contract_batch.contract
    process = next(
        (
            item
            for item in contract.measurement_processes
            if item.process_id == contract.outcome_measurement_process_id
        ),
        None,
    )
    if process is None:
        raise ValueError("causal outcome measurement process is missing")
    indicator = next(
        (item for item in contract.variables if item.variable_id == process.indicator_variable_id),
        None,
    )
    if indicator is None or indicator.observable_id is None:
        raise ValueError("causal outcome indicator is missing")
    return contract, process, indicator


def _validate_request_bindings(
    *,
    source_causal_campaign: CausalAuditCampaign,
    policy: PredictionCommitmentPolicy,
    author_manifest: PredictionAuthorManifest,
    evaluator_manifest: CalibrationEvaluatorManifest,
    request: PredictionCommitmentRequest,
) -> None:
    if not source_causal_campaign.prediction_planning_authorized or (
        source_causal_campaign.disposition
        not in {
            CausalAuditDisposition.READY_IDENTIFIED,
            CausalAuditDisposition.READY_BOUNDED,
        }
    ):
        raise ValueError("causal campaign does not authorize prediction planning")
    source_hypotheses = source_causal_campaign.source_campaign
    snapshot = source_hypotheses.world_model_snapshot
    if snapshot is None:
        raise ValueError("prediction source has no admitted world-model snapshot")
    contract, process, indicator = _causal_components(source_causal_campaign)
    expected = {
        "source_causal_campaign_sha256": source_causal_campaign.campaign_sha256,
        "source_hypothesis_campaign_sha256": source_hypotheses.campaign_sha256,
        "world_model_snapshot_sha256": snapshot.snapshot_sha256,
        "causal_contract_sha256": contract.contract_sha256,
        "author_manifest_sha256": author_manifest.manifest_sha256,
        "calibration_evaluator_manifest_sha256": evaluator_manifest.manifest_sha256,
        "policy_sha256": policy.policy_sha256,
    }
    for field_name, expected_value in expected.items():
        if getattr(request, field_name) != expected_value:
            raise ValueError(f"prediction request changed exact {field_name} binding")
    protocol = request.experiment_protocol
    protocol_expected = {
        "causal_campaign_sha256": source_causal_campaign.campaign_sha256,
        "causal_contract_batch_sha256": source_causal_campaign.contract_batch.batch_sha256,
        "causal_contract_sha256": contract.contract_sha256,
        "estimand_sha256": contract.estimand.estimand_sha256,
        "proposed_evidence_kind": contract.estimand.proposed_evidence_kind,
        "causal_claim_ceiling": source_causal_campaign.claim_ceiling,
        "target_population_sha256": contract.estimand.target_population_sha256,
        "outcome_measurement_process_id": process.process_id,
        "observable_id": indicator.observable_id,
        "measurement_protocol_sha256": process.measurement_protocol_sha256,
    }
    for field_name, expected_value in protocol_expected.items():
        if getattr(protocol, field_name) != expected_value:
            raise ValueError(f"experiment protocol changed causal {field_name} binding")
    schema = request.outcome_schema
    if (
        schema.observable_id != indicator.observable_id
        or schema.measurement_protocol_sha256 != process.measurement_protocol_sha256
        or schema.measurement_error_model_sha256 != process.error_model_sha256
    ):
        raise ValueError("outcome schema changed the causal measurement binding")
    expected_bins = tuple(item.bin_id for item in schema.bins)
    source_predictions = [
        item
        for item in snapshot.predictions
        if item.observable_id == indicator.observable_id
        and item.measurement_protocol_sha256 == process.measurement_protocol_sha256
    ]
    by_hypothesis: dict[str, list[object]] = {}
    for prediction in source_predictions:
        by_hypothesis.setdefault(prediction.hypothesis_id, []).append(prediction)
    if set(by_hypothesis) != {item.hypothesis_id for item in snapshot.hypotheses} or any(
        len(items) != 1 for items in by_hypothesis.values()
    ):
        raise ValueError("outcome schema lacks one exact F9-S2 prediction per hypothesis")
    for items in by_hypothesis.values():
        source_prediction = items[0]
        if set(expected_bins) != set(source_prediction.outcome_space):
            raise ValueError("outcome bins changed the F9-S2 prediction outcome space")
    _validate_actor_independence(
        source=source_causal_campaign,
        author=author_manifest,
        evaluator=evaluator_manifest,
    )
    if (
        policy.frozen_at > request.issued_at
        or author_manifest.frozen_at > request.issued_at
        or evaluator_manifest.frozen_at > request.issued_at
        or protocol.frozen_at > request.issued_at
        or source_causal_campaign.generated_at > request.issued_at
    ):
        raise ValueError("prediction request predates a frozen dependency")
    report = request.calibration_report
    if report is not None:
        if report.completed_at > request.issued_at:
            raise ValueError("prediction request predates its calibration report")
        report_expected = {
            "predictor_manifest_sha256": author_manifest.manifest_sha256,
            "evaluator_manifest_sha256": evaluator_manifest.manifest_sha256,
            "outcome_schema_sha256": schema.outcome_schema_sha256,
            "measurement_protocol_sha256": schema.measurement_protocol_sha256,
        }
        for field_name, expected_value in report_expected.items():
            if getattr(report, field_name) != expected_value:
                raise ValueError(f"calibration report changed exact {field_name} binding")
        if report.validation_split_sha256 == protocol.experiment_namespace_sha256:
            raise ValueError("target observation namespace cannot calibrate its own predictor")
        if report.completed_at < evaluator_manifest.frozen_at:
            raise ValueError("calibration report predates its frozen evaluator")
        if min(item.predicted_at for item in report.trials) < author_manifest.frozen_at:
            raise ValueError("calibration trial predates its frozen predictor")
        trial_bins = tuple(item.bin_id for item in report.trials[0].probabilities)
        if trial_bins != tuple(sorted(expected_bins)):
            raise ValueError("calibration report uses another outcome space")


def build_prediction_commitment_request(
    *,
    request_id: str,
    source_causal_campaign: CausalAuditCampaign,
    prediction_mode: PredictionMode,
    experiment_protocol: ExperimentProtocol,
    outcome_schema: OutcomeSchema,
    policy: PredictionCommitmentPolicy,
    author_manifest: PredictionAuthorManifest,
    calibration_evaluator_manifest: CalibrationEvaluatorManifest,
    calibration_report: LikelihoodCalibrationReport | None,
    issued_at: datetime,
) -> PredictionCommitmentRequest:
    source = source_causal_campaign.source_campaign
    snapshot = source.world_model_snapshot
    if snapshot is None:
        raise ValueError("prediction source has no admitted world-model snapshot")
    contract, _, _ = _causal_components(source_causal_campaign)
    request = PredictionCommitmentRequest(
        request_id=request_id,
        source_causal_campaign_sha256=source_causal_campaign.campaign_sha256,
        source_hypothesis_campaign_sha256=source.campaign_sha256,
        world_model_snapshot_sha256=snapshot.snapshot_sha256,
        causal_contract_sha256=contract.contract_sha256,
        prediction_mode=prediction_mode,
        experiment_protocol=experiment_protocol,
        outcome_schema=outcome_schema,
        author_manifest_sha256=author_manifest.manifest_sha256,
        calibration_evaluator_manifest_sha256=(calibration_evaluator_manifest.manifest_sha256),
        calibration_report=calibration_report,
        policy_sha256=policy.policy_sha256,
        issued_at=issued_at,
    )
    _validate_request_bindings(
        source_causal_campaign=source_causal_campaign,
        policy=policy,
        author_manifest=author_manifest,
        evaluator_manifest=calibration_evaluator_manifest,
        request=request,
    )
    return request


def _validate_prediction_batch(
    *,
    source_causal_campaign: CausalAuditCampaign,
    request: PredictionCommitmentRequest,
    manifest: PredictionAuthorManifest,
    policy: PredictionCommitmentPolicy,
    batch: PredictionBatch,
    received_at: datetime | None = None,
) -> None:
    if batch.request_sha256 != request.request_sha256:
        raise ValueError("prediction batch belongs to another request")
    if batch.author_manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("prediction batch belongs to another author manifest")
    if batch.experiment_protocol_sha256 != request.experiment_protocol.protocol_sha256:
        raise ValueError("prediction batch changed the experiment protocol")
    if batch.outcome_schema_sha256 != request.outcome_schema.outcome_schema_sha256:
        raise ValueError("prediction batch changed the outcome schema")
    if batch.completed_at < request.issued_at:
        raise ValueError("prediction batch predates its request")
    if received_at is not None and batch.completed_at > received_at:
        raise ValueError("prediction batch claims a future completion time")
    if len(batch.predictions) > manifest.maximum_hypotheses:
        raise ValueError("prediction batch exceeds author hypothesis capacity")
    if len(request.outcome_schema.bins) > manifest.maximum_outcome_bins:
        raise ValueError("outcome schema exceeds author bin capacity")
    snapshot = source_causal_campaign.world_model_snapshot
    contract, process, indicator = _causal_components(source_causal_campaign)
    hypotheses = {item.hypothesis_id: item for item in snapshot.hypotheses}
    graphs = {item.hypothesis_id: item for item in contract.hypothesis_graphs}
    source_predictions = {
        item.hypothesis_id: item
        for item in snapshot.predictions
        if item.observable_id == indicator.observable_id
        and item.measurement_protocol_sha256 == process.measurement_protocol_sha256
    }
    if [item.hypothesis_id for item in batch.predictions] != sorted(hypotheses):
        raise ValueError("prediction batch must cover every active hypothesis exactly once")
    schema_bins = tuple(item.bin_id for item in request.outcome_schema.bins)
    sorted_schema_bins = tuple(sorted(schema_bins))
    for prediction in batch.predictions:
        hypothesis = hypotheses[prediction.hypothesis_id]
        graph = graphs[prediction.hypothesis_id]
        source_prediction = source_predictions[prediction.hypothesis_id]
        bindings = {
            "hypothesis_version_sha256": hypothesis.hypothesis_sha256,
            "causal_graph_sha256": graph.graph_sha256,
            "source_prediction_sha256": source_prediction.prediction_sha256,
            "observable_id": indicator.observable_id,
            "measurement_protocol_sha256": process.measurement_protocol_sha256,
            "measurement_error_model_sha256": process.error_model_sha256,
            "expected_outcome_bin_id": source_prediction.expected_outcome,
            "mode": request.prediction_mode,
        }
        for field_name, expected_value in bindings.items():
            if getattr(prediction, field_name) != expected_value:
                raise ValueError(f"hypothesis prediction changed exact {field_name} binding")
        if prediction.mode is PredictionMode.PROBABILISTIC:
            if tuple(item.bin_id for item in prediction.probabilities) != sorted_schema_bins:
                raise ValueError("probabilistic prediction must cover every outcome bin")
            values = {item.bin_id: item.probability for item in prediction.probabilities}
            maximum = max(values.values())
            winners = sorted(key for key, value in values.items() if value == maximum)
            if winners != [prediction.expected_outcome_bin_id]:
                raise ValueError("probabilistic prediction expected bin must be its unique mode")
            expected_likelihood = derive_hypothesis_likelihood_model_sha256(
                author_manifest=manifest,
                hypothesis_id=prediction.hypothesis_id,
                hypothesis_version_sha256=prediction.hypothesis_version_sha256,
                experiment_protocol_sha256=request.experiment_protocol.protocol_sha256,
                outcome_schema_sha256=request.outcome_schema.outcome_schema_sha256,
            )
            if prediction.likelihood_model_sha256 != expected_likelihood:
                raise ValueError(
                    "hypothesis likelihood changed the calibrated implementation family"
                )
            for scenario in prediction.sensitivity_predictions:
                if tuple(item.bin_id for item in scenario.probabilities) != sorted_schema_bins:
                    raise ValueError("sensitivity scenario changed the outcome space")
        else:
            if set(prediction.ordinal_order) != set(schema_bins):
                raise ValueError("ordinal prediction must rank every outcome bin exactly once")
            if prediction.ordinal_order[0] != prediction.expected_outcome_bin_id:
                raise ValueError("ordinal prediction expected bin must rank first")
    report = request.calibration_report
    if report is not None:
        if report.metrics.trial_count < policy.minimum_calibration_trials:
            return
        # Report integrity is already rederived by its model; policy admission occurs in the probe.


def _distribution(prediction: HypothesisPrediction) -> dict[str, float]:
    return {item.bin_id: item.probability for item in prediction.probabilities}


def _total_variation(left: dict[str, float], right: dict[str, float]) -> float:
    return round(
        0.5 * math.fsum(abs(left[key] - right[key]) for key in sorted(left)),
        _METRIC_DIGITS,
    )


def _derive_prediction_outputs(
    *,
    request: PredictionCommitmentRequest,
    policy: PredictionCommitmentPolicy,
    batch: PredictionBatch,
) -> _DerivedPredictionOutputs:
    calibration_blockers: list[str] = []
    degeneracy_blockers: list[str] = []
    report = request.calibration_report
    if request.prediction_mode is PredictionMode.PROBABILISTIC:
        assert report is not None
        metrics = report.metrics
        thresholds = (
            (metrics.trial_count < policy.minimum_calibration_trials, "insufficient_trials"),
            (metrics.multiclass_brier_score > policy.maximum_brier_score, "brier_score"),
            (metrics.mean_log_loss > policy.maximum_mean_log_loss, "mean_log_loss"),
            (metrics.top_label_ece > policy.maximum_top_label_ece, "top_label_ece"),
            (
                metrics.zero_probability_observations
                > policy.maximum_zero_probability_observations,
                "zero_probability_observations",
            ),
        )
        calibration_blockers.extend(
            f"calibration:{label}" for blocked, label in thresholds if blocked
        )
    diagnostics: list[HypothesisPredictionDiagnostic] = []
    for prediction in batch.predictions:
        if prediction.mode is PredictionMode.ORDINAL:
            diagnostics.append(
                HypothesisPredictionDiagnostic(
                    hypothesis_id=prediction.hypothesis_id,
                    prediction_sha256=prediction.prediction_sha256,
                    sensitivity_scenario_count=0,
                )
            )
            continue
        base = _distribution(prediction)
        probabilities = tuple(base.values())
        entropy = round(
            -math.fsum(value * math.log(value) for value in probabilities if value > 0.0),
            _METRIC_DIGITS,
        )
        sensitivity_tv = tuple(
            _total_variation(base, _distribution_from_mass(item.probabilities))
            for item in prediction.sensitivity_predictions
        )
        maximum_sensitivity = max(sensitivity_tv, default=0.0)
        diagnostic = HypothesisPredictionDiagnostic(
            hypothesis_id=prediction.hypothesis_id,
            prediction_sha256=prediction.prediction_sha256,
            entropy_nats=entropy,
            minimum_probability=min(probabilities),
            maximum_probability=max(probabilities),
            maximum_sensitivity_total_variation=maximum_sensitivity,
            sensitivity_scenario_count=len(prediction.sensitivity_predictions),
        )
        diagnostics.append(diagnostic)
        checks = (
            (
                min(probabilities) < policy.minimum_bin_probability,
                "probability_below_floor",
            ),
            (
                max(probabilities) > policy.maximum_bin_probability,
                "probability_above_ceiling",
            ),
            (entropy < policy.minimum_entropy_nats, "entropy_below_floor"),
            (
                len(prediction.sensitivity_predictions) < policy.minimum_sensitivity_scenarios,
                "insufficient_sensitivity_scenarios",
            ),
            (
                maximum_sensitivity > policy.maximum_sensitivity_total_variation,
                "sensitivity_instability",
            ),
        )
        degeneracy_blockers.extend(
            f"degeneracy:{prediction.hypothesis_id}:{label}" for blocked, label in checks if blocked
        )
    pairwise: list[PairwisePredictionDiscrimination] = []
    predictions = batch.predictions
    for index, left in enumerate(predictions):
        for right in predictions[index + 1 :]:
            if request.prediction_mode is PredictionMode.PROBABILISTIC:
                distance = _total_variation(_distribution(left), _distribution(right))
                pairwise.append(
                    PairwisePredictionDiscrimination(
                        left_hypothesis_id=left.hypothesis_id,
                        right_hypothesis_id=right.hypothesis_id,
                        total_variation_distance=distance,
                    )
                )
                if distance < policy.minimum_pairwise_total_variation:
                    degeneracy_blockers.append(
                        f"degeneracy:{left.hypothesis_id}:{right.hypothesis_id}:pairwise_tv"
                    )
            else:
                differs = left.ordinal_order != right.ordinal_order
                pairwise.append(
                    PairwisePredictionDiscrimination(
                        left_hypothesis_id=left.hypothesis_id,
                        right_hypothesis_id=right.hypothesis_id,
                        ordinal_order_differs=differs,
                    )
                )
                if not differs:
                    degeneracy_blockers.append(
                        f"degeneracy:{left.hypothesis_id}:{right.hypothesis_id}:ordinal_identity"
                    )
    if calibration_blockers:
        disposition = PredictionCommitmentDisposition.BLOCKED_CALIBRATION
    elif degeneracy_blockers:
        disposition = PredictionCommitmentDisposition.BLOCKED_DEGENERACY
    else:
        disposition = PredictionCommitmentDisposition.READY
    blockers = tuple(sorted(set(calibration_blockers + degeneracy_blockers)))
    probe = PredictionCommitmentProbe(
        prediction_batch_sha256=batch.batch_sha256,
        calibration_report_sha256=report.report_sha256 if report is not None else None,
        hypothesis_diagnostics=tuple(diagnostics),
        pairwise_discrimination=tuple(pairwise),
        blockers=blockers,
    )
    return _DerivedPredictionOutputs(
        probe=probe,
        blockers=blockers,
        disposition=disposition,
        eig_eligible=(
            disposition is PredictionCommitmentDisposition.READY
            and request.prediction_mode is PredictionMode.PROBABILISTIC
        ),
    )


def _distribution_from_mass(
    probabilities: tuple[OutcomeProbability, ...],
) -> dict[str, float]:
    return {item.bin_id: item.probability for item in probabilities}


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prediction clock must return a timezone-aware timestamp")
    return value


def _opaque_output_sha256(value: object) -> str:
    try:
        return content_sha256(value)
    except (TypeError, ValueError, UnicodeError):
        return hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()


def _failure(
    *,
    kind: PredictionCommitmentFailureKind,
    error: Exception,
    occurred_at: datetime,
    raw_output: object | None = None,
) -> PredictionCommitmentFailure:
    return PredictionCommitmentFailure(
        kind=kind,
        error_class=type(error).__name__,
        error_detail_sha256=hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        raw_output_sha256=(_opaque_output_sha256(raw_output) if raw_output is not None else None),
        occurred_at=occurred_at,
    )


async def run_prediction_commitment(
    *,
    campaign_id: str,
    source_causal_campaign: CausalAuditCampaign,
    policy: PredictionCommitmentPolicy,
    request: PredictionCommitmentRequest,
    author: PredictionAuthorAdapter,
    calibration_evaluator_manifest: CalibrationEvaluatorManifest,
    clock: Callable[[], datetime] | None = None,
) -> PredictionCommitmentCampaign:
    """Generate and mechanically audit an observation-blind prediction commitment."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    if author.manifest.manifest_sha256 != request.author_manifest_sha256:
        raise ValueError("runtime prediction author differs from the frozen request")
    _validate_request_bindings(
        source_causal_campaign=source_causal_campaign,
        policy=policy,
        author_manifest=author.manifest,
        evaluator_manifest=calibration_evaluator_manifest,
        request=request,
    )
    try:
        raw_batch = await author.predict(
            request=request,
            source_causal_campaign=source_causal_campaign,
        )
    except Exception as exc:  # noqa: BLE001 - sanitized failure is part of the contract
        failure = _failure(
            kind=PredictionCommitmentFailureKind.AUTHOR_ERROR,
            error=exc,
            occurred_at=_now(clock),
        )
        return PredictionCommitmentCampaign(
            campaign_id=campaign_id,
            source_causal_campaign=source_causal_campaign,
            policy=policy,
            author_manifest=author.manifest,
            calibration_evaluator_manifest=calibration_evaluator_manifest,
            request=request,
            failure=failure,
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=PredictionCommitmentDisposition.BLOCKED_EXECUTION,
            eig_eligible=False,
            generated_at=_now(clock),
        )
    received_at = _now(clock)
    try:
        batch = (
            raw_batch
            if isinstance(raw_batch, PredictionBatch)
            else PredictionBatch.model_validate(raw_batch)
        )
        _validate_prediction_batch(
            source_causal_campaign=source_causal_campaign,
            request=request,
            manifest=author.manifest,
            policy=policy,
            batch=batch,
            received_at=received_at,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        failure = _failure(
            kind=PredictionCommitmentFailureKind.AUTHOR_OUTPUT_INVALID,
            error=exc,
            occurred_at=received_at,
            raw_output=raw_batch,
        )
        return PredictionCommitmentCampaign(
            campaign_id=campaign_id,
            source_causal_campaign=source_causal_campaign,
            policy=policy,
            author_manifest=author.manifest,
            calibration_evaluator_manifest=calibration_evaluator_manifest,
            request=request,
            failure=failure,
            blockers=(f"execution_failure:{failure.kind.value}",),
            disposition=PredictionCommitmentDisposition.BLOCKED_EXECUTION,
            eig_eligible=False,
            generated_at=_now(clock),
        )
    derived = _derive_prediction_outputs(request=request, policy=policy, batch=batch)
    return PredictionCommitmentCampaign(
        campaign_id=campaign_id,
        source_causal_campaign=source_causal_campaign,
        policy=policy,
        author_manifest=author.manifest,
        calibration_evaluator_manifest=calibration_evaluator_manifest,
        request=request,
        prediction_batch=batch,
        probe=derived.probe,
        blockers=derived.blockers,
        disposition=derived.disposition,
        eig_eligible=derived.eig_eligible,
        generated_at=_now(clock),
    )


def commit_prediction_commitment_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: PredictionCommitmentCampaign,
    committed_at: datetime,
) -> CommittedPredictionCommitmentCampaign:
    if committed_at.tzinfo is None or committed_at.utcoffset() is None:
        raise ValueError("prediction commitment time must be timezone-aware")
    if committed_at < campaign.generated_at:
        raise ValueError("prediction commitment cannot predate campaign generation")
    ledger = archive.store_ledger(
        value=campaign,
        object_sha256=campaign.campaign_sha256,
        archived_at=committed_at,
    )
    return CommittedPredictionCommitmentCampaign(
        campaign=campaign,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_prediction_commitment_campaign(
    *,
    archive: ContentAddressedResponseArchive,
    ledger: ArchivedKnowledgeLedger,
) -> PredictionCommitmentCampaign:
    payload = archive.read_ledger(ledger)
    campaign = PredictionCommitmentCampaign.model_validate_json(payload)
    if canonical_json_bytes(campaign) != payload:
        raise ValueError("archived prediction campaign is not canonical JSON")
    if campaign.campaign_sha256 != ledger.object_sha256:
        raise ValueError("archived prediction campaign changed object identity")
    return campaign


class ObservationStagingError(RuntimeError):
    """Raw observation bytes cannot cross the prediction-commitment boundary."""


class PostObservationPredictionMutation(ObservationStagingError):
    """An experiment namespace was already sealed by another prediction commitment."""

    def __init__(self, violation: "PredictionMutationViolation") -> None:
        super().__init__("post-observation prediction mutation was rejected and recorded")
        self.violation = violation


class ObservationNamespaceSeal(EpistemicModel):
    schema_version: Literal[1] = 1
    experiment_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    experiment_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    first_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    first_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    first_observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    sealed_at: AwareDatetime

    @property
    def seal_sha256(self) -> str:
        return content_sha256(self)


class ArchivedObservationNamespaceSeal(EpistemicModel):
    schema_version: Literal[1] = 1
    seal: ObservationNamespaceSeal
    seal_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _declared_hash_commits_seal(self) -> "ArchivedObservationNamespaceSeal":
        if self.seal_sha256 != self.seal.seal_sha256:
            raise ValueError("observation namespace seal hash changed")
        return self


class PredictionMutationViolation(EpistemicModel):
    schema_version: Literal[1] = 1
    violation_kind: Literal["post_observation_prediction_mutation"] = (
        "post_observation_prediction_mutation"
    )
    severity: Literal["security_and_scientific_integrity"] = "security_and_scientific_integrity"
    experiment_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    sealed_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    attempted_commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    sealed_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    attempted_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    seal_sha256: str = Field(pattern=_SHA256_PATTERN)
    detected_at: AwareDatetime

    @property
    def violation_sha256(self) -> str:
        return content_sha256(self)


class ObservationStagingReceipt(EpistemicModel):
    schema_version: Literal[1] = 1
    experiment_namespace_sha256: str = Field(pattern=_SHA256_PATTERN)
    experiment_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    commitment_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_campaign_sha256: str = Field(pattern=_SHA256_PATTERN)
    prediction_commitment_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    namespace_seal_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(
        pattern=r"^observations/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.observation$"
    )
    observed_at: AwareDatetime
    staged_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_path_and_time_are_bound(self) -> "ObservationStagingReceipt":
        expected = (
            f"observations/{self.observation_sha256[:2]}/{self.observation_sha256[2:4]}/"
            f"{self.observation_sha256}.observation"
        )
        if self.relative_path != expected:
            raise ValueError("observation path does not match its content identity")
        if self.staged_at < self.observed_at:
            raise ValueError("observation staging cannot precede observation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ObservationStagingStore:
    """Private write-once observation store with per-experiment commitment seals."""

    def __init__(
        self,
        root: Path,
        *,
        prediction_archive: ContentAddressedResponseArchive,
        max_observation_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if not 1 <= max_observation_bytes <= 64 * 1024 * 1024:
            raise ValueError("observation limit must be between 1 byte and 64 MiB")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ObservationStagingError("observation root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ObservationStagingError("observation root must be a regular directory")
        self.root = candidate.resolve(strict=True)
        self.prediction_archive = prediction_archive
        self.max_observation_bytes = max_observation_bytes

    def _path(self, relative_path: str) -> Path:
        parts = Path(relative_path).parts
        if not parts or any(item in {"", ".", ".."} for item in parts):
            raise ObservationStagingError("observation path is not canonical")
        target = self.root.joinpath(*parts)
        if self.root not in target.parents:
            raise ObservationStagingError("observation path escapes its root")
        return target

    def _ensure_parent(self, target: Path) -> None:
        current = self.root
        for part in target.parent.relative_to(self.root).parts:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if current.is_symlink() or not current.is_dir():
                raise ObservationStagingError("observation store contains an unsafe directory")

    def _read_bytes(self, relative_path: str, expected_sha256: str, expected_bytes: int) -> bytes:
        if not 1 <= expected_bytes <= self.max_observation_bytes:
            raise ObservationStagingError("stored observation size is outside configured bounds")
        target = self._path(relative_path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except (FileNotFoundError, OSError) as exc:
            raise ObservationStagingError("stored observation is missing or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_bytes:
                raise ObservationStagingError("stored observation metadata changed")
            chunks: list[bytes] = []
            remaining = expected_bytes
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise ObservationStagingError("stored observation ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ObservationStagingError("stored observation exceeds its receipt")
        finally:
            os.close(descriptor)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ObservationStagingError("stored observation content hash changed")
        return payload

    def _write_once(self, relative_path: str, payload: bytes) -> bool:
        target = self._path(relative_path)
        self._ensure_parent(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o400)
        except FileExistsError:
            return False
        except OSError as exc:
            raise ObservationStagingError("observation store refused a new object") from exc
        committed = False
        try:
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise ObservationStagingError("observation write made no progress")
                written += count
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            committed = True
        finally:
            os.close(descriptor)
            if not committed:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True

    def _seal_relative_path(self, namespace: str) -> str:
        return f"seals/{namespace[:2]}/{namespace[2:4]}/{namespace}.json"

    def _load_seal(self, namespace: str) -> ObservationNamespaceSeal | None:
        relative = self._seal_relative_path(namespace)
        target = self._path(relative)
        try:
            size = target.stat().st_size
        except FileNotFoundError:
            return None
        payload = self._read_bytes(relative, _file_sha256(target), size)
        try:
            archived = ArchivedObservationNamespaceSeal.model_validate_json(payload)
        except ValidationError as exc:
            raise ObservationStagingError("observation namespace seal is invalid") from exc
        if (
            canonical_json_bytes(archived) != payload
            or archived.seal.experiment_namespace_sha256 != namespace
        ):
            raise ObservationStagingError("observation namespace seal changed identity")
        return archived.seal

    def _record_violation(
        self,
        *,
        seal: ObservationNamespaceSeal,
        campaign: PredictionCommitmentCampaign,
        detected_at: datetime,
    ) -> PredictionMutationViolation:
        violation = PredictionMutationViolation(
            experiment_namespace_sha256=seal.experiment_namespace_sha256,
            sealed_commitment_sha256=seal.commitment_sha256,
            attempted_commitment_sha256=campaign.commitment_sha256,
            sealed_campaign_sha256=seal.first_campaign_sha256,
            attempted_campaign_sha256=campaign.campaign_sha256,
            seal_sha256=seal.seal_sha256,
            detected_at=detected_at,
        )
        payload = canonical_json_bytes(violation)
        relative = (
            f"violations/{seal.experiment_namespace_sha256[:2]}/"
            f"{seal.experiment_namespace_sha256[2:4]}/{violation.violation_sha256}.json"
        )
        if not self._write_once(relative, payload):
            self._read_bytes(relative, hashlib.sha256(payload).hexdigest(), len(payload))
        return violation

    def stage_observation(
        self,
        *,
        committed_campaign: CommittedPredictionCommitmentCampaign,
        payload: bytes,
        media_type: str,
        observed_at: datetime,
        staged_at: datetime,
    ) -> ObservationStagingReceipt:
        if not payload or len(payload) > self.max_observation_bytes:
            raise ObservationStagingError("observation is empty or exceeds the byte limit")
        if not media_type.strip():
            raise ObservationStagingError("observation media type cannot be blank")
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in (observed_at, staged_at)
        ):
            raise ObservationStagingError("observation timestamps must be timezone-aware")
        loaded = load_prediction_commitment_campaign(
            archive=self.prediction_archive,
            ledger=committed_campaign.ledger,
        )
        if loaded != committed_campaign.campaign:
            raise ObservationStagingError("committed prediction wrapper differs from its archive")
        campaign = loaded
        protocol = campaign.request.experiment_protocol
        namespace = protocol.experiment_namespace_sha256
        existing_seal = self._load_seal(namespace)
        if existing_seal is not None and (
            existing_seal.commitment_sha256 != campaign.commitment_sha256
        ):
            violation = self._record_violation(
                seal=existing_seal,
                campaign=campaign,
                detected_at=staged_at,
            )
            raise PostObservationPredictionMutation(violation)
        if campaign.disposition is not PredictionCommitmentDisposition.READY:
            raise ObservationStagingError(
                "only a ready prediction commitment can stage observation"
            )
        if observed_at <= committed_campaign.committed_at:
            raise ObservationStagingError("observation must occur after prediction commitment")
        if staged_at < observed_at:
            raise ObservationStagingError("observation staging cannot precede observation")
        observation_sha256 = hashlib.sha256(payload).hexdigest()
        relative_path = (
            f"observations/{observation_sha256[:2]}/{observation_sha256[2:4]}/"
            f"{observation_sha256}.observation"
        )
        if existing_seal is None:
            proposed_seal = ObservationNamespaceSeal(
                experiment_namespace_sha256=namespace,
                experiment_protocol_sha256=protocol.protocol_sha256,
                commitment_sha256=campaign.commitment_sha256,
                first_campaign_sha256=campaign.campaign_sha256,
                first_commitment_receipt_sha256=committed_campaign.receipt_sha256,
                first_observation_sha256=observation_sha256,
                sealed_at=staged_at,
            )
            seal_payload = canonical_json_bytes(
                ArchivedObservationNamespaceSeal(
                    seal=proposed_seal,
                    seal_sha256=proposed_seal.seal_sha256,
                )
            )
            created = self._write_once(self._seal_relative_path(namespace), seal_payload)
            if created:
                seal = proposed_seal
            else:
                seal = self._load_seal(namespace)
                assert seal is not None
                if seal.commitment_sha256 != campaign.commitment_sha256:
                    violation = self._record_violation(
                        seal=seal,
                        campaign=campaign,
                        detected_at=staged_at,
                    )
                    raise PostObservationPredictionMutation(violation)
        else:
            seal = existing_seal
        if not self._write_once(relative_path, payload):
            self._read_bytes(relative_path, observation_sha256, len(payload))
        receipt = ObservationStagingReceipt(
            experiment_namespace_sha256=namespace,
            experiment_protocol_sha256=protocol.protocol_sha256,
            commitment_sha256=campaign.commitment_sha256,
            prediction_campaign_sha256=campaign.campaign_sha256,
            prediction_commitment_receipt_sha256=committed_campaign.receipt_sha256,
            namespace_seal_sha256=seal.seal_sha256,
            observation_sha256=observation_sha256,
            observation_bytes=len(payload),
            media_type=media_type,
            relative_path=relative_path,
            observed_at=observed_at,
            staged_at=staged_at,
        )
        self._read_bytes(relative_path, observation_sha256, len(payload))
        return receipt

    def read_observation(self, receipt: ObservationStagingReceipt) -> bytes:
        seal = self._load_seal(receipt.experiment_namespace_sha256)
        if seal is None or seal.seal_sha256 != receipt.namespace_seal_sha256:
            raise ObservationStagingError("observation receipt is not bound to the namespace seal")
        if seal.commitment_sha256 != receipt.commitment_sha256:
            raise ObservationStagingError("observation receipt changed prediction commitment")
        return self._read_bytes(
            receipt.relative_path,
            receipt.observation_sha256,
            receipt.observation_bytes,
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ObservationStagingError("observation metadata is missing or unsafe") from exc
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


__all__ = [
    "CALIBRATION_REPORT_OUTPUT_SCHEMA_SHA256",
    "PREDICTION_OUTPUT_SCHEMA_SHA256",
    "CalibrationEvaluatorManifest",
    "CalibrationTrial",
    "CommittedPredictionCommitmentCampaign",
    "ArchivedObservationNamespaceSeal",
    "ExperimentProtocol",
    "HypothesisPrediction",
    "HypothesisPredictionDiagnostic",
    "LikelihoodCalibrationMetrics",
    "LikelihoodCalibrationReport",
    "ObservationNamespaceSeal",
    "ObservationStagingError",
    "ObservationStagingReceipt",
    "ObservationStagingStore",
    "OutcomeBin",
    "OutcomeProbability",
    "OutcomeSchema",
    "OutcomeSchemaKind",
    "PairwisePredictionDiscrimination",
    "PostObservationPredictionMutation",
    "PredictionAuthorAdapter",
    "PredictionAuthorManifest",
    "PredictionBatch",
    "PredictionCommitmentCampaign",
    "PredictionCommitmentDisposition",
    "PredictionCommitmentFailure",
    "PredictionCommitmentFailureKind",
    "PredictionCommitmentPolicy",
    "PredictionCommitmentProbe",
    "PredictionCommitmentRequest",
    "PredictionMode",
    "PredictionMutationViolation",
    "SensitivityPrediction",
    "build_prediction_commitment_request",
    "commit_prediction_commitment_campaign",
    "derive_likelihood_calibration_metrics",
    "derive_hypothesis_likelihood_model_sha256",
    "derive_experiment_namespace_sha256",
    "load_prediction_commitment_campaign",
    "run_prediction_commitment",
]
