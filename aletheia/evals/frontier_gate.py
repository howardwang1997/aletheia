"""Validation-calibrated acceptance and receipt-linked Frontier Gate reporting.

Calibration consumes only a preregistered validation execution and independently reviewed
reference evidence.  Test results are accepted only after the resulting program configuration is
frozen.  Final reports re-run the complete baseline ledger/signature audit; aggregate JSON supplied
by an operator is never treated as authoritative input.
"""

from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.baselines import (
    BaselineArmId,
    BaselineMatrixPlan,
    BaselineMatrixResult,
    MatrixPhase,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.private_suite import (
    CustodyEventType,
    PrivateCustodyLedger,
    PrivateCustodyState,
    PrivateSuiteManifest,
    PrivateSuiteTier,
)
from aletheia.evals.schemas import EvaluationSuite, FrozenModel, content_sha256
from aletheia.evals.statistics import (
    BaselineAggregateReport,
    BaselineArmSummary,
    BaselinePairwiseComparison,
    ObjectiveMetricSummary,
    aggregate_baseline_matrix,
)


class FrontierGateError(RuntimeError):
    """Gate calibration or reporting would violate the frozen evaluation contract."""


class FrontierGateTier(str, Enum):
    PILOT = "pilot"
    FRONTIER_GATE = "frontier_gate"


class FrontierGateTrack(str, Enum):
    SCIENCEAGENTBENCH = "scienceagentbench"
    COREBENCH = "corebench"
    DISCOVERYWORLD = "discoveryworld"
    PRIVATE_PROSPECTIVE = "private_prospective"


class ThresholdDirection(str, Enum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class ComparisonRequirement(str, Enum):
    SUPERIORITY = "superiority"
    NONINFERIORITY = "noninferiority"


class GateVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class CriterionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


_FORMAL_OBJECTIVES = {
    "false_discovery_rate": ThresholdDirection.MAXIMUM,
    "calibration_error": ThresholdDirection.MAXIMUM,
    "evidence_provenance_completeness": ThresholdDirection.MINIMUM,
    "reproduction_fidelity": ThresholdDirection.MINIMUM,
}


def _finite(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


class ReferenceBaselineEvidence(FrozenModel):
    """Content identity for an independently reviewed expert/author/reference baseline."""

    schema_version: Literal[1] = 1
    reference_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    evaluation_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_type: Literal["domain_expert", "task_author", "reference_implementation"]
    covered_task_manifest_sha256s: tuple[str, ...] = Field(min_length=1)
    pass_at_1: float = Field(ge=0.0, le=1.0)
    scientific_valid_fraction: float = Field(ge=0.0, le=1.0)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measured_at: AwareDatetime

    @model_validator(mode="after")
    def _reference_coverage_is_unique(self) -> "ReferenceBaselineEvidence":
        if len(self.covered_task_manifest_sha256s) != len(set(self.covered_task_manifest_sha256s)):
            raise ValueError("reference baseline task coverage must be unique")
        if any(
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            for identity in self.covered_task_manifest_sha256s
        ):
            raise ValueError("reference baseline tasks must use SHA-256 identities")
        return self

    @property
    def reference_sha256(self) -> str:
        return content_sha256(self)


class ObjectiveCalibrationRule(FrozenModel):
    schema_version: Literal[1] = 1
    metric: str = Field(min_length=1, max_length=128)
    direction: ThresholdDirection
    absolute_boundary: float
    allowable_validation_degradation: float = Field(ge=0.0)
    maximum_missing_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _objective_values_are_finite(self) -> "ObjectiveCalibrationRule":
        _finite(self.absolute_boundary, "objective absolute boundary")
        _finite(
            self.allowable_validation_degradation,
            "objective validation degradation",
        )
        if not self.metric.strip():
            raise ValueError("objective metric cannot be blank")
        return self


class ComparisonCalibrationRule(FrozenModel):
    schema_version: Literal[1] = 1
    comparator_arm: BaselineArmId
    requirement: ComparisonRequirement
    minimum_practical_effect: float | None = Field(default=None, ge=0.0, le=1.0)
    validation_effect_retention: float = Field(default=0.8, ge=0.0, le=1.0)
    noninferiority_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    max_holm_adjusted_p_value: float | None = Field(default=None, gt=0.0, le=0.05)
    minimum_valid_pair_fraction: float = Field(default=0.8, gt=0.0, le=1.0)
    max_mean_paired_cost_increase_usd: float = Field(ge=0.0)
    require_unconditional_comparability: Literal[True] = True
    rationale: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _requirement_fields_are_exact(self) -> "ComparisonCalibrationRule":
        if self.comparator_arm is BaselineArmId.ALETHEIA_FULL_K2:
            raise ValueError("full K2 cannot be its own acceptance comparator")
        if self.requirement is ComparisonRequirement.SUPERIORITY:
            if self.minimum_practical_effect is None or self.minimum_practical_effect <= 0:
                raise ValueError("superiority requires a positive practical-effect floor")
            if self.max_holm_adjusted_p_value is None:
                raise ValueError("superiority requires a multiplicity-adjusted alpha")
            if self.noninferiority_margin is not None:
                raise ValueError("superiority cannot declare a noninferiority margin")
        else:
            if self.noninferiority_margin is None:
                raise ValueError("noninferiority requires an explicit margin")
            if self.minimum_practical_effect is not None:
                raise ValueError("noninferiority cannot declare a superiority effect")
            if self.max_holm_adjusted_p_value is not None:
                raise ValueError("noninferiority uses the frozen confidence bound, not a p-value")
        _finite(
            self.max_mean_paired_cost_increase_usd,
            "paired cost-increase ceiling",
        )
        return self


class GateCalibrationPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    minimum_absolute_pass_at_1: float = Field(default=0.5, ge=0.5, le=1.0)
    validation_pass_retention: float = Field(default=0.8, ge=0.5, le=1.0)
    reference_pass_retention: float = Field(default=0.9, ge=0.5, le=1.0)
    reference_valid_retention: float = Field(default=0.9, ge=0.5, le=1.0)
    minimum_scientific_valid_fraction: float = Field(default=0.8, ge=0.8, le=1.0)
    valid_fraction_drop_tolerance: float = Field(default=0.05, ge=0.0, le=0.2)
    max_final_invalid_fraction: float = Field(default=0.2, ge=0.0, le=0.2)
    invalid_fraction_increase_tolerance: float = Field(default=0.05, ge=0.0, le=0.2)
    max_infrastructure_retry_fraction: float = Field(default=0.1, ge=0.0, le=0.2)
    retry_fraction_increase_tolerance: float = Field(default=0.02, ge=0.0, le=0.1)
    absolute_max_mean_cost_usd: float = Field(gt=0.0)
    validation_cost_multiplier: float = Field(default=1.5, ge=1.0, le=3.0)
    max_total_human_interventions: Literal[0] = 0
    max_contamination_declarations: Literal[0] = 0
    require_complete_cost_receipts: Literal[True] = True
    minimum_reference_task_coverage: float = Field(default=0.5, gt=0.0, le=1.0)
    comparisons: tuple[ComparisonCalibrationRule, ...] = Field(min_length=3, max_length=3)
    objectives: tuple[ObjectiveCalibrationRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _policy_covers_all_comparators_once(self) -> "GateCalibrationPolicy":
        comparators = [rule.comparator_arm for rule in self.comparisons]
        expected = {
            BaselineArmId.DIRECT_MODEL,
            BaselineArmId.GENERIC_AGENT,
            BaselineArmId.ALETHEIA_NO_K2,
        }
        if len(comparators) != len(set(comparators)) or set(comparators) != expected:
            raise ValueError("gate policy must cover all three baseline comparators once")
        rules = {rule.comparator_arm: rule for rule in self.comparisons}
        for comparator in (BaselineArmId.DIRECT_MODEL, BaselineArmId.ALETHEIA_NO_K2):
            if rules[comparator].requirement is not ComparisonRequirement.SUPERIORITY:
                raise ValueError("direct-model and no-K2 comparisons require superiority")
        if (
            rules[BaselineArmId.GENERIC_AGENT].requirement
            is not ComparisonRequirement.NONINFERIORITY
        ):
            raise ValueError("the generic-agent comparison requires noninferiority")
        metrics = [rule.metric for rule in self.objectives]
        if len(metrics) != len(set(metrics)):
            raise ValueError("objective calibration metrics must be unique")
        if self.minimum_scientific_valid_fraction < 1.0 - self.max_final_invalid_fraction:
            raise ValueError("valid and invalid fraction policies contradict each other")
        _finite(self.absolute_max_mean_cost_usd, "absolute mean-cost ceiling")
        return self


class SuiteCalibrationPlan(FrozenModel):
    """Frozen before validation execution and never derived from test observations."""

    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    tier: FrontierGateTier
    track: FrontierGateTrack
    validation_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: GateCalibrationPolicy
    reference_baselines: tuple[ReferenceBaselineEvidence, ...] = Field(min_length=1)
    calibration_owner_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_reviewer_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _plan_is_independent_and_complete(self) -> "SuiteCalibrationPlan":
        if self.calibration_owner_principal_sha256 == self.independent_reviewer_principal_sha256:
            raise ValueError("gate calibration requires an independent reviewer")
        ids = [reference.reference_id for reference in self.reference_baselines]
        hashes = [reference.reference_sha256 for reference in self.reference_baselines]
        if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
            raise ValueError("reference baseline IDs and contents must be unique")
        if any(
            reference.evaluation_suite_manifest_sha256 != self.validation_suite_manifest_sha256
            for reference in self.reference_baselines
        ):
            raise ValueError("reference baselines must bind the validation suite")
        if any(
            reference.reviewer_principal_sha256 != self.independent_reviewer_principal_sha256
            for reference in self.reference_baselines
        ):
            raise ValueError("reference baselines require the frozen independent reviewer")
        if any(reference.measured_at > self.frozen_at for reference in self.reference_baselines):
            raise ValueError("reference baseline evidence must exist before calibration freeze")
        if self.track is FrontierGateTrack.PRIVATE_PROSPECTIVE and (
            self.validation_suite_manifest_sha256 == self.test_suite_manifest_sha256
        ):
            raise ValueError("private validation analog and test suite must differ")
        if self.tier is FrontierGateTier.FRONTIER_GATE:
            objectives = {rule.metric: rule.direction for rule in self.policy.objectives}
            missing = set(_FORMAL_OBJECTIVES) - set(objectives)
            wrong = {
                metric
                for metric, direction in _FORMAL_OBJECTIVES.items()
                if objectives.get(metric) is not None and objectives[metric] is not direction
            }
            if missing or wrong:
                raise ValueError(
                    "formal calibration requires correctly directed false-discovery, calibration, "
                    f"provenance, and reproduction objectives; missing={sorted(missing)}, "
                    f"wrong={sorted(wrong)}"
                )
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


class ComparisonAcceptanceThreshold(FrozenModel):
    schema_version: Literal[1] = 1
    comparator_arm: BaselineArmId
    requirement: ComparisonRequirement
    minimum_effect_or_ci_bound: float = Field(ge=-1.0, le=1.0)
    max_holm_adjusted_p_value: float | None = Field(default=None, gt=0.0, le=0.05)
    minimum_valid_pair_fraction: float = Field(gt=0.0, le=1.0)
    max_mean_paired_cost_increase_usd: float = Field(ge=0.0)
    require_unconditional_comparability: Literal[True] = True
    validation_observed_effect: float = Field(ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def _comparison_threshold_is_complete(self) -> "ComparisonAcceptanceThreshold":
        if self.comparator_arm is BaselineArmId.ALETHEIA_FULL_K2:
            raise ValueError("full K2 cannot be its own acceptance comparator")
        if self.requirement is ComparisonRequirement.SUPERIORITY:
            if self.minimum_effect_or_ci_bound <= 0:
                raise ValueError("superiority requires a positive effect threshold")
            if self.max_holm_adjusted_p_value is None:
                raise ValueError("superiority requires a multiplicity-adjusted alpha")
        elif self.max_holm_adjusted_p_value is not None:
            raise ValueError("noninferiority cannot use a superiority p-value threshold")
        _finite(
            self.max_mean_paired_cost_increase_usd,
            "paired cost-increase threshold",
        )
        return self


class ObjectiveAcceptanceThreshold(FrozenModel):
    schema_version: Literal[1] = 1
    metric: str = Field(min_length=1, max_length=128)
    direction: ThresholdDirection
    threshold: float
    maximum_missing_fraction: float = Field(ge=0.0, le=1.0)
    validation_observed_mean: float

    @model_validator(mode="after")
    def _objective_thresholds_are_finite(self) -> "ObjectiveAcceptanceThreshold":
        _finite(self.threshold, "objective threshold")
        _finite(self.validation_observed_mean, "validation objective mean")
        return self


class SuiteAcceptanceThresholds(FrozenModel):
    schema_version: Literal[1] = 1
    min_full_k2_pass_at_1: float = Field(ge=0.0, le=1.0)
    min_full_k2_scientific_success_rate: float = Field(ge=0.0, le=1.0)
    min_full_k2_scientific_valid_fraction: float = Field(ge=0.0, le=1.0)
    max_full_k2_final_invalid_fraction: float = Field(ge=0.0, le=1.0)
    max_full_k2_infrastructure_retry_fraction: float = Field(ge=0.0, le=1.0)
    max_full_k2_mean_cost_usd: float = Field(gt=0.0)
    max_total_human_interventions: Literal[0] = 0
    max_contamination_declarations: Literal[0] = 0
    require_complete_cost_receipts: Literal[True] = True
    comparisons: tuple[ComparisonAcceptanceThreshold, ...] = Field(min_length=3, max_length=3)
    objectives: tuple[ObjectiveAcceptanceThreshold, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _thresholds_cover_the_frozen_endpoints(self) -> "SuiteAcceptanceThresholds":
        comparators = [item.comparator_arm for item in self.comparisons]
        if len(comparators) != len(set(comparators)) or set(comparators) != {
            BaselineArmId.DIRECT_MODEL,
            BaselineArmId.GENERIC_AGENT,
            BaselineArmId.ALETHEIA_NO_K2,
        }:
            raise ValueError("acceptance thresholds must cover all three comparators once")
        metrics = [item.metric for item in self.objectives]
        if len(metrics) != len(set(metrics)):
            raise ValueError("acceptance objective thresholds must be unique")
        return self


class SuiteAcceptanceConfig(FrozenModel):
    schema_version: Literal[1] = 1
    suite_config_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    tier: FrontierGateTier
    track: FrontierGateTrack
    calibration_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_aggregate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_ledger_head_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_manifest_sha256s: tuple[str, ...] = Field(min_length=4, max_length=4)
    reference_baseline_sha256s: tuple[str, ...] = Field(min_length=1)
    thresholds: SuiteAcceptanceThresholds
    calibrated_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _formal_config_has_required_objectives(self) -> "SuiteAcceptanceConfig":
        if len(set(self.arm_manifest_sha256s)) != 4:
            raise ValueError("acceptance configuration requires four distinct arm manifests")
        if len(self.reference_baseline_sha256s) != len(set(self.reference_baseline_sha256s)):
            raise ValueError("acceptance reference identities must be unique")
        identities = self.arm_manifest_sha256s + self.reference_baseline_sha256s
        if any(
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            for identity in identities
        ):
            raise ValueError("acceptance arm/reference identities must use SHA-256")
        if self.tier is FrontierGateTier.FRONTIER_GATE:
            objectives = {item.metric: item.direction for item in self.thresholds.objectives}
            missing = set(_FORMAL_OBJECTIVES) - set(objectives)
            wrong = {
                metric
                for metric, direction in _FORMAL_OBJECTIVES.items()
                if objectives.get(metric) is not None and objectives[metric] is not direction
            }
            if missing or wrong:
                raise ValueError("formal acceptance configuration is missing required objectives")
        return self

    @property
    def config_sha256(self) -> str:
        return content_sha256(self)


def _arm(report: BaselineAggregateReport, arm_id: BaselineArmId) -> BaselineArmSummary:
    matches = [arm for arm in report.arms if arm.arm_id is arm_id]
    if len(matches) != 1:
        raise FrontierGateError(f"aggregate report does not uniquely contain {arm_id.value}")
    return matches[0]


def _comparison(
    report: BaselineAggregateReport, comparator: BaselineArmId
) -> BaselinePairwiseComparison:
    matches = [item for item in report.comparisons if item.comparator_arm is comparator]
    if len(matches) != 1:
        raise FrontierGateError(
            f"aggregate report does not uniquely contain comparison {comparator.value}"
        )
    return matches[0]


def _objective(arm: BaselineArmSummary, metric: str) -> ObjectiveMetricSummary:
    matches = [item for item in arm.objective_metrics if item.metric == metric]
    if len(matches) != 1:
        raise FrontierGateError(f"full-K2 validation does not uniquely contain metric {metric!r}")
    return matches[0]


def _validate_matrix_derivation(
    plan: SuiteCalibrationPlan,
    validation_matrix: BaselineMatrixPlan,
    validation_suite: EvaluationSuite,
    test_matrix: BaselineMatrixPlan,
    test_suite: EvaluationSuite,
) -> None:
    if validation_matrix.phase is not MatrixPhase.VALIDATION:
        raise FrontierGateError("calibration requires a validation-phase matrix")
    if test_matrix.phase is not MatrixPhase.TEST:
        raise FrontierGateError("acceptance requires a frozen test-phase matrix")
    identities = (
        validation_matrix.manifest_sha256,
        validation_suite.manifest_sha256,
        test_matrix.manifest_sha256,
        test_suite.manifest_sha256,
        validation_matrix.evaluator_manifest_sha256,
    )
    expected = (
        plan.validation_matrix_manifest_sha256,
        plan.validation_suite_manifest_sha256,
        plan.test_matrix_manifest_sha256,
        plan.test_suite_manifest_sha256,
        plan.evaluator_manifest_sha256,
    )
    if identities != expected:
        raise FrontierGateError("calibration inputs differ from the frozen plan")
    if validation_matrix.suite_manifest_sha256 != validation_suite.manifest_sha256:
        raise FrontierGateError("validation matrix and suite identities differ")
    if test_matrix.suite_manifest_sha256 != test_suite.manifest_sha256:
        raise FrontierGateError("test matrix and suite identities differ")
    if test_matrix.parent_validation_matrix_sha256 != validation_matrix.manifest_sha256:
        raise FrontierGateError("test matrix does not name the exact validation parent")
    if validation_matrix.frozen_at > plan.frozen_at or test_matrix.frozen_at > plan.frozen_at:
        raise FrontierGateError("calibration plan cannot predate either frozen matrix")
    if (
        test_matrix.evaluator_manifest_sha256 != validation_matrix.evaluator_manifest_sha256
        or test_matrix.arms != validation_matrix.arms
        or test_matrix.analysis != validation_matrix.analysis
        or test_matrix.mismatch_disclosures != validation_matrix.mismatch_disclosures
    ):
        raise FrontierGateError(
            "test evaluator, treatments, analysis, and mismatch policy must equal validation"
        )
    if test_matrix.frozen_at < validation_matrix.frozen_at:
        raise FrontierGateError("test matrix cannot freeze before its validation parent")


def calibrate_suite_acceptance(
    *,
    plan: SuiteCalibrationPlan,
    validation_matrix: BaselineMatrixPlan,
    validation_suite: EvaluationSuite,
    validation_result: BaselineMatrixResult,
    validation_ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
    test_matrix: BaselineMatrixPlan,
    test_suite: EvaluationSuite,
    suite_config_id: str,
    calibrated_at: datetime | None = None,
) -> SuiteAcceptanceConfig:
    """Audit validation evidence and deterministically derive test thresholds."""

    _validate_matrix_derivation(
        plan,
        validation_matrix,
        validation_suite,
        test_matrix,
        test_suite,
    )
    if validation_result.started_at < plan.frozen_at:
        raise FrontierGateError("calibration plan was frozen after validation execution started")
    timestamp = calibrated_at or datetime.now(timezone.utc)
    if timestamp < validation_result.ended_at:
        raise FrontierGateError("acceptance cannot be calibrated before validation ends")
    report = aggregate_baseline_matrix(
        matrix=validation_matrix,
        suite=validation_suite,
        result=validation_result,
        ledger=validation_ledger,
        receipt_keys=receipt_keys,
        generated_at=timestamp,
    )
    if report.ledger.head_sha256 is None:
        raise FrontierGateError("validation ledger must have a retained head identity")

    validation_tasks = {slot.task_manifest_sha256 for slot in validation_matrix.slots}
    covered = {
        task
        for reference in plan.reference_baselines
        for task in reference.covered_task_manifest_sha256s
    }
    if not covered <= validation_tasks:
        raise FrontierGateError("reference baseline covers tasks outside validation")
    coverage = len(covered) / len(validation_tasks)
    if coverage < plan.policy.minimum_reference_task_coverage:
        raise FrontierGateError("independent reference baseline coverage is below policy")
    if any(reference.measured_at > timestamp for reference in plan.reference_baselines):
        raise FrontierGateError("reference baseline evidence postdates calibration")

    full = _arm(report, BaselineArmId.ALETHEIA_FULL_K2)
    if full.scientific_success_rate is None:
        raise FrontierGateError("full K2 has no valid validation verdicts")
    valid_fraction = full.scientific_valid_cells / full.planned_cells
    invalid_fraction = 1.0 - valid_fraction
    retry_fraction = full.infrastructure_retries / full.planned_cells
    costs = full.observed_cost_usd
    if costs.mean is None or (plan.policy.require_complete_cost_receipts and costs.missing):
        raise FrontierGateError("validation lacks complete full-K2 cost receipts")

    reference_pass = max(reference.pass_at_1 for reference in plan.reference_baselines)
    min_pass = max(
        plan.policy.minimum_absolute_pass_at_1,
        full.pass_at_1 * plan.policy.validation_pass_retention,
        reference_pass * plan.policy.reference_pass_retention,
    )
    min_success = max(
        plan.policy.minimum_absolute_pass_at_1,
        full.scientific_success_rate * plan.policy.validation_pass_retention,
    )
    reference_valid = max(
        reference.scientific_valid_fraction for reference in plan.reference_baselines
    )
    min_valid = max(
        plan.policy.minimum_scientific_valid_fraction,
        valid_fraction - plan.policy.valid_fraction_drop_tolerance,
        reference_valid * plan.policy.reference_valid_retention,
    )
    max_invalid = min(
        plan.policy.max_final_invalid_fraction,
        invalid_fraction + plan.policy.invalid_fraction_increase_tolerance,
    )
    max_retry = min(
        plan.policy.max_infrastructure_retry_fraction,
        retry_fraction + plan.policy.retry_fraction_increase_tolerance,
    )
    max_cost = min(
        plan.policy.absolute_max_mean_cost_usd,
        costs.mean * plan.policy.validation_cost_multiplier,
    )
    validation_checks = (
        (full.pass_at_1 >= min_pass, "full-K2 pass@1"),
        (full.scientific_success_rate >= min_success, "full-K2 scientific success"),
        (valid_fraction >= min_valid, "full-K2 scientific validity"),
        (invalid_fraction <= max_invalid, "full-K2 final invalidity"),
        (retry_fraction <= max_retry, "full-K2 infrastructure retries"),
        (costs.mean <= max_cost, "full-K2 mean cost"),
        (
            full.human_interventions <= plan.policy.max_total_human_interventions,
            "full-K2 human intervention",
        ),
        (
            full.contamination_declarations <= plan.policy.max_contamination_declarations,
            "full-K2 contamination",
        ),
    )
    failed_validation_checks = [label for passed, label in validation_checks if not passed]
    if failed_validation_checks:
        raise FrontierGateError(
            "validation cannot justify held-out acceptance thresholds: "
            + ", ".join(failed_validation_checks)
        )

    comparison_thresholds: list[ComparisonAcceptanceThreshold] = []
    for rule in plan.policy.comparisons:
        observed = _comparison(report, rule.comparator_arm)
        if observed.scientific_risk_difference is None:
            raise FrontierGateError(
                f"validation comparison {rule.comparator_arm.value} has no scientific effect"
            )
        valid_pair_fraction = observed.valid_scientific_pairs / observed.planned_pairs
        if valid_pair_fraction < rule.minimum_valid_pair_fraction:
            raise FrontierGateError(
                f"validation comparison {rule.comparator_arm.value} lacks valid pairs"
            )
        if not observed.unconditional_claim_allowed:
            raise FrontierGateError(
                f"validation comparison {rule.comparator_arm.value} is only conditionally comparable"
            )
        if (
            observed.mean_paired_cost_difference_usd is None
            or observed.cost_pairs != observed.planned_pairs
        ):
            raise FrontierGateError(
                f"validation comparison {rule.comparator_arm.value} lacks complete paired cost"
            )
        if rule.requirement is ComparisonRequirement.SUPERIORITY:
            if observed.scientific_risk_difference <= 0:
                raise FrontierGateError(
                    f"validation does not support superiority over {rule.comparator_arm.value}"
                )
            assert rule.minimum_practical_effect is not None
            bound = max(
                rule.minimum_practical_effect,
                observed.scientific_risk_difference * rule.validation_effect_retention,
            )
            if observed.scientific_ci_low is None or observed.scientific_ci_low < bound:
                raise FrontierGateError(
                    f"validation confidence bound does not support superiority over "
                    f"{rule.comparator_arm.value}"
                )
            if (
                observed.holm_adjusted_p_value is None
                or observed.holm_adjusted_p_value > rule.max_holm_adjusted_p_value
            ):
                raise FrontierGateError(
                    f"validation multiplicity-adjusted result does not support superiority over "
                    f"{rule.comparator_arm.value}"
                )
        else:
            assert rule.noninferiority_margin is not None
            if (
                observed.scientific_ci_low is None
                or observed.scientific_ci_low < -rule.noninferiority_margin
            ):
                raise FrontierGateError(
                    f"validation does not support noninferiority to {rule.comparator_arm.value}"
                )
            bound = -rule.noninferiority_margin
        derived_cost = max(0.0, observed.mean_paired_cost_difference_usd)
        if derived_cost > rule.max_mean_paired_cost_increase_usd:
            raise FrontierGateError(
                f"validation paired cost exceeds policy for {rule.comparator_arm.value}"
            )
        comparison_thresholds.append(
            ComparisonAcceptanceThreshold(
                comparator_arm=rule.comparator_arm,
                requirement=rule.requirement,
                minimum_effect_or_ci_bound=bound,
                max_holm_adjusted_p_value=rule.max_holm_adjusted_p_value,
                minimum_valid_pair_fraction=rule.minimum_valid_pair_fraction,
                max_mean_paired_cost_increase_usd=min(
                    rule.max_mean_paired_cost_increase_usd,
                    derived_cost * plan.policy.validation_cost_multiplier,
                ),
                validation_observed_effect=observed.scientific_risk_difference,
            )
        )

    objective_thresholds: list[ObjectiveAcceptanceThreshold] = []
    for rule in plan.policy.objectives:
        observed = _objective(full, rule.metric).distribution
        if observed.mean is None:
            raise FrontierGateError(f"validation objective {rule.metric!r} has no observations")
        if observed.missing / full.planned_cells > rule.maximum_missing_fraction:
            raise FrontierGateError(f"validation objective {rule.metric!r} is too incomplete")
        threshold = (
            max(
                rule.absolute_boundary,
                observed.mean - rule.allowable_validation_degradation,
            )
            if rule.direction is ThresholdDirection.MINIMUM
            else min(
                rule.absolute_boundary,
                observed.mean + rule.allowable_validation_degradation,
            )
        )
        if (rule.direction is ThresholdDirection.MINIMUM and observed.mean < threshold) or (
            rule.direction is ThresholdDirection.MAXIMUM and observed.mean > threshold
        ):
            raise FrontierGateError(
                f"validation objective {rule.metric!r} does not meet its derived threshold"
            )
        objective_thresholds.append(
            ObjectiveAcceptanceThreshold(
                metric=rule.metric,
                direction=rule.direction,
                threshold=threshold,
                maximum_missing_fraction=rule.maximum_missing_fraction,
                validation_observed_mean=observed.mean,
            )
        )

    return SuiteAcceptanceConfig(
        suite_config_id=suite_config_id,
        tier=plan.tier,
        track=plan.track,
        calibration_plan_sha256=plan.plan_sha256,
        validation_matrix_manifest_sha256=validation_matrix.manifest_sha256,
        validation_result_sha256=validation_result.result_sha256,
        validation_aggregate_report_sha256=report.report_sha256,
        validation_ledger_head_sha256=report.ledger.head_sha256,
        test_matrix_manifest_sha256=test_matrix.manifest_sha256,
        test_suite_manifest_sha256=test_suite.manifest_sha256,
        evaluator_manifest_sha256=test_matrix.evaluator_manifest_sha256,
        analysis_policy_sha256=content_sha256(test_matrix.analysis),
        arm_manifest_sha256s=tuple(arm.manifest_sha256 for arm in test_matrix.arms),
        reference_baseline_sha256s=tuple(
            reference.reference_sha256 for reference in plan.reference_baselines
        ),
        thresholds=SuiteAcceptanceThresholds(
            min_full_k2_pass_at_1=min_pass,
            min_full_k2_scientific_success_rate=min_success,
            min_full_k2_scientific_valid_fraction=min_valid,
            max_full_k2_final_invalid_fraction=max_invalid,
            max_full_k2_infrastructure_retry_fraction=max_retry,
            max_full_k2_mean_cost_usd=max_cost,
            comparisons=tuple(comparison_thresholds),
            objectives=tuple(objective_thresholds),
        ),
        calibrated_at=timestamp,
    )


class FrontierGateAcceptanceConfig(FrozenModel):
    """Program-level acceptance contract frozen before any held-out test starts."""

    schema_version: Literal[1] = 1
    program_config_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: str = Field(min_length=1, max_length=128)
    tier: FrontierGateTier
    suites: tuple[SuiteAcceptanceConfig, ...] = Field(min_length=1, max_length=4)
    acceptance_owner_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_auditor_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    auditor_approval_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scientific_claim: str = Field(min_length=1, max_length=4096)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _program_is_independent_and_complete(self) -> "FrontierGateAcceptanceConfig":
        if self.acceptance_owner_principal_sha256 == self.independent_auditor_principal_sha256:
            raise ValueError("program acceptance requires an independent auditor")
        if self.owner_approval_evidence_sha256 == self.auditor_approval_evidence_sha256:
            raise ValueError("program acceptance requires two independent approval artifacts")
        if not self.scientific_claim.strip():
            raise ValueError("program scientific claim cannot be blank")
        ids = [item.suite_config_id for item in self.suites]
        tracks = [item.track for item in self.suites]
        hashes = [item.config_sha256 for item in self.suites]
        if (
            len(ids) != len(set(ids))
            or len(tracks) != len(set(tracks))
            or len(hashes) != len(set(hashes))
        ):
            raise ValueError("program suite IDs, tracks, and contents must be unique")
        if any(item.tier is not self.tier for item in self.suites):
            raise ValueError("program and suite acceptance tiers must agree")
        if any(item.calibrated_at > self.frozen_at for item in self.suites):
            raise ValueError("program acceptance cannot freeze before suite calibration")
        if len({item.evaluator_manifest_sha256 for item in self.suites}) != 1:
            raise ValueError("all program tracks must use one frozen evaluator identity")
        if len({item.arm_manifest_sha256s for item in self.suites}) != 1:
            raise ValueError("all program tracks must use the same four system arms")
        if self.tier is FrontierGateTier.FRONTIER_GATE and set(tracks) != set(FrontierGateTrack):
            raise ValueError("formal Frontier Gate acceptance requires exactly all four tracks")
        return self

    @property
    def config_sha256(self) -> str:
        return content_sha256(self)

    def suite(self, track: FrontierGateTrack) -> SuiteAcceptanceConfig:
        for item in self.suites:
            if item.track is track:
                return item
        raise KeyError(track)


def freeze_frontier_gate_acceptance(
    *,
    program_config_id: str,
    version: str,
    tier: FrontierGateTier,
    suites: Sequence[SuiteAcceptanceConfig],
    acceptance_owner_principal_sha256: str,
    independent_auditor_principal_sha256: str,
    owner_approval_evidence_sha256: str,
    auditor_approval_evidence_sha256: str,
    scientific_claim: str,
    frozen_at: datetime | None = None,
) -> FrontierGateAcceptanceConfig:
    """Freeze calibrated suite contracts into the pre-test program decision rule."""

    return FrontierGateAcceptanceConfig(
        program_config_id=program_config_id,
        version=version,
        tier=tier,
        suites=tuple(suites),
        acceptance_owner_principal_sha256=acceptance_owner_principal_sha256,
        independent_auditor_principal_sha256=independent_auditor_principal_sha256,
        owner_approval_evidence_sha256=owner_approval_evidence_sha256,
        auditor_approval_evidence_sha256=auditor_approval_evidence_sha256,
        scientific_claim=scientific_claim,
        frozen_at=frozen_at or datetime.now(timezone.utc),
    )


class GateCriterionResult(FrozenModel):
    schema_version: Literal[1] = 1
    criterion_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,191}$")
    category: Literal[
        "scientific",
        "comparison",
        "objective",
        "reliability",
        "cost",
        "integrity",
        "custody",
    ]
    status: CriterionStatus
    relation: Literal[">=", "<=", "==", "present", "after", "before"]
    expected: str = Field(min_length=1, max_length=256)
    observed: str | None = Field(default=None, max_length=256)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=2048)


class GateAttemptReceiptIdentity(FrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    submission_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scorer_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    signed_scorer_envelope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class GateReceiptIndex(FrozenModel):
    schema_version: Literal[1] = 1
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_events: int = Field(ge=1)
    ledger_head_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempts: tuple[GateAttemptReceiptIdentity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _attempt_receipts_are_unique(self) -> "GateReceiptIndex":
        ids = [item.attempt_id for item in self.attempts]
        if len(ids) != len(set(ids)):
            raise ValueError("gate receipt index cannot contain duplicate attempts")
        return self

    @property
    def index_sha256(self) -> str:
        return content_sha256(self)


class GateSuiteDecision(FrozenModel):
    schema_version: Literal[1] = 1
    track: FrontierGateTrack
    suite_acceptance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    aggregate_report: BaselineAggregateReport | None = None
    receipt_index: GateReceiptIndex | None = None
    criteria: tuple[GateCriterionResult, ...] = Field(min_length=1)
    verdict: GateVerdict

    @model_validator(mode="after")
    def _decision_matches_criteria_and_evidence(self) -> "GateSuiteDecision":
        ids = [item.criterion_id for item in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("suite decision criteria must be unique")
        statuses = {item.status for item in self.criteria}
        expected = (
            GateVerdict.BLOCKED
            if CriterionStatus.BLOCKED in statuses
            else GateVerdict.FAIL
            if CriterionStatus.FAIL in statuses
            else GateVerdict.PASS
        )
        if self.verdict is not expected:
            raise ValueError("suite verdict does not match its criterion results")
        has_evidence = (
            self.result_sha256 is not None
            and self.aggregate_report is not None
            and self.receipt_index is not None
        )
        if self.verdict is not GateVerdict.BLOCKED and not has_evidence:
            raise ValueError("measured suite decisions require aggregate and receipt evidence")
        if self.aggregate_report is not None and (
            self.aggregate_report.matrix_manifest_sha256 != self.test_matrix_manifest_sha256
            or self.aggregate_report.suite_manifest_sha256 != self.test_suite_manifest_sha256
            or self.aggregate_report.result_sha256 != self.result_sha256
        ):
            raise ValueError("suite decision aggregate identities differ")
        if (
            self.receipt_index is not None
            and self.receipt_index.result_sha256 != self.result_sha256
        ):
            raise ValueError("suite decision receipt index belongs to another result")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


class PrivateCustodyEvidence(FrozenModel):
    schema_version: Literal[1] = 1
    private_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    custody_ledger_events: int = Field(ge=0)
    custody_ledger_head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    custody_ledger_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: PrivateCustodyState | None = None
    materialization_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cleanup_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    access_opened_at: AwareDatetime | None = None
    access_closed_at: AwareDatetime | None = None
    criteria: tuple[GateCriterionResult, ...] = Field(min_length=1)
    verdict: GateVerdict

    @model_validator(mode="after")
    def _custody_verdict_matches_criteria(self) -> "PrivateCustodyEvidence":
        statuses = {item.status for item in self.criteria}
        expected = (
            GateVerdict.BLOCKED
            if CriterionStatus.BLOCKED in statuses
            else GateVerdict.FAIL
            if CriterionStatus.FAIL in statuses
            else GateVerdict.PASS
        )
        if self.verdict is not expected:
            raise ValueError("private custody verdict does not match its criteria")
        return self

    @property
    def evidence_sha256(self) -> str:
        return content_sha256(self)


class GatePlotPoint(FrozenModel):
    schema_version: Literal[1] = 1
    track: FrontierGateTrack
    metric: str = Field(min_length=1, max_length=128)
    observed: float
    threshold: float
    direction: ThresholdDirection

    @model_validator(mode="after")
    def _plot_values_are_finite(self) -> "GatePlotPoint":
        _finite(self.observed, "plot observation")
        _finite(self.threshold, "plot threshold")
        return self


class FrontierGateReport(FrozenModel):
    schema_version: Literal[1] = 1
    program_config_id: str
    acceptance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tier: FrontierGateTier
    scientific_claim: str
    suite_decisions: tuple[GateSuiteDecision, ...] = Field(min_length=1, max_length=4)
    private_custody: PrivateCustodyEvidence | None = None
    program_criteria: tuple[GateCriterionResult, ...] = Field(min_length=1)
    plot_points: tuple[GatePlotPoint, ...]
    missing_tracks: tuple[FrontierGateTrack, ...]
    overall_verdict: GateVerdict
    scientific_claim_allowed: bool
    limitations: tuple[str, ...] = Field(min_length=1)
    evidence_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def _report_verdict_is_fail_closed(self) -> "FrontierGateReport":
        tracks = [item.track for item in self.suite_decisions]
        if len(tracks) != len(set(tracks)):
            raise ValueError("Frontier Gate report tracks must be unique")
        statuses = [item.status for item in self.program_criteria]
        statuses.extend(
            criterion.status for decision in self.suite_decisions for criterion in decision.criteria
        )
        if self.private_custody is not None:
            statuses.extend(item.status for item in self.private_custody.criteria)
        expected = (
            GateVerdict.BLOCKED
            if self.missing_tracks or CriterionStatus.BLOCKED in statuses
            else GateVerdict.FAIL
            if CriterionStatus.FAIL in statuses
            else GateVerdict.PASS
        )
        if self.overall_verdict is not expected:
            raise ValueError("overall gate verdict does not match the complete evidence")
        if self.scientific_claim_allowed != (self.overall_verdict is GateVerdict.PASS):
            raise ValueError("scientific claims are allowed only after an overall pass")
        if self.tier is FrontierGateTier.FRONTIER_GATE and self.private_custody is None:
            if FrontierGateTrack.PRIVATE_PROSPECTIVE not in self.missing_tracks:
                raise ValueError("formal reports without custody evidence must block private track")
        expected_bundle = content_sha256(
            {
                "acceptance_config_sha256": self.acceptance_config_sha256,
                "suite_decision_sha256s": [item.decision_sha256 for item in self.suite_decisions],
                "private_custody_evidence_sha256": (
                    self.private_custody.evidence_sha256
                    if self.private_custody is not None
                    else None
                ),
                "program_criteria": [
                    item.model_dump(mode="json") for item in self.program_criteria
                ],
            }
        )
        if self.evidence_bundle_sha256 != expected_bundle:
            raise ValueError("Frontier Gate evidence-bundle hash is invalid")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True)
class GateEvaluationInput:
    track: FrontierGateTrack
    matrix: BaselineMatrixPlan
    suite: EvaluationSuite
    result: BaselineMatrixResult
    ledger: EvaluationLedger
    receipt_keys: Mapping[str, bytes]


@dataclass(frozen=True)
class PrivateGateInput:
    manifest: PrivateSuiteManifest
    ledger: PrivateCustodyLedger


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def _criterion(
    *,
    criterion_id: str,
    category: Literal[
        "scientific",
        "comparison",
        "objective",
        "reliability",
        "cost",
        "integrity",
        "custody",
    ],
    relation: Literal[">=", "<=", "==", "present", "after", "before"],
    expected: object,
    observed: object | None,
    passed: bool | None,
    detail: str,
    evidence_sha256: str | None,
) -> GateCriterionResult:
    status = (
        CriterionStatus.BLOCKED
        if passed is None
        else CriterionStatus.PASS
        if passed
        else CriterionStatus.FAIL
    )
    return GateCriterionResult(
        criterion_id=criterion_id,
        category=category,
        status=status,
        relation=relation,
        expected=_shown(expected),
        observed=None if observed is None else _shown(observed),
        evidence_sha256=evidence_sha256,
        detail=detail,
    )


def _verdict(criteria: Sequence[GateCriterionResult]) -> GateVerdict:
    statuses = {item.status for item in criteria}
    if CriterionStatus.BLOCKED in statuses:
        return GateVerdict.BLOCKED
    if CriterionStatus.FAIL in statuses:
        return GateVerdict.FAIL
    return GateVerdict.PASS


def _receipt_index(
    result: BaselineMatrixResult, report: BaselineAggregateReport
) -> GateReceiptIndex:
    attempts = tuple(
        GateAttemptReceiptIdentity(
            attempt_id=record.attempt.attempt_id,
            attempt_sha256=record.attempt.attempt_sha256,
            attempt_manifest_sha256=record.attempt_manifest.manifest_sha256,
            execution_receipt_sha256=(
                record.execution_receipt.receipt_sha256
                if record.execution_receipt is not None
                else None
            ),
            submission_sha256=(
                record.submission.submission_sha256 if record.submission is not None else None
            ),
            scorer_receipt_sha256=(
                record.scorer_receipt.receipt.receipt_sha256
                if record.scorer_receipt is not None
                else None
            ),
            signed_scorer_envelope_sha256=(
                record.scorer_receipt.envelope_sha256 if record.scorer_receipt is not None else None
            ),
        )
        for record in sorted(result.attempts, key=lambda item: item.attempt.attempt_id)
    )
    if report.ledger.head_sha256 is None:
        raise FrontierGateError("test ledger has no retained head identity")
    return GateReceiptIndex(
        result_sha256=result.result_sha256,
        ledger_events=report.ledger.events,
        ledger_head_sha256=report.ledger.head_sha256,
        ledger_file_sha256=report.ledger.file_sha256,
        attempts=attempts,
    )


def _validate_test_bindings(
    *,
    program: FrontierGateAcceptanceConfig,
    config: SuiteAcceptanceConfig,
    evaluation: GateEvaluationInput,
) -> None:
    matrix = evaluation.matrix
    suite = evaluation.suite
    if evaluation.track is not config.track:
        raise FrontierGateError("evaluation input is labeled as another Frontier Gate track")
    if matrix.phase is not MatrixPhase.TEST:
        raise FrontierGateError("Frontier Gate reports accept only test-phase matrices")
    actual = (
        matrix.manifest_sha256,
        suite.manifest_sha256,
        matrix.evaluator_manifest_sha256,
        content_sha256(matrix.analysis),
        tuple(arm.manifest_sha256 for arm in matrix.arms),
    )
    expected = (
        config.test_matrix_manifest_sha256,
        config.test_suite_manifest_sha256,
        config.evaluator_manifest_sha256,
        config.analysis_policy_sha256,
        config.arm_manifest_sha256s,
    )
    if actual != expected:
        raise FrontierGateError(
            f"{config.track.value} test evidence differs from frozen acceptance bindings"
        )
    if matrix.suite_manifest_sha256 != suite.manifest_sha256:
        raise FrontierGateError("test matrix and suite identities differ")
    if matrix.frozen_at > program.frozen_at:
        raise FrontierGateError("test matrix was frozen after the program acceptance contract")
    if evaluation.result.started_at < program.frozen_at:
        raise FrontierGateError("held-out test execution started before program acceptance freeze")


def _evaluate_suite(
    *,
    program: FrontierGateAcceptanceConfig,
    config: SuiteAcceptanceConfig,
    evaluation: GateEvaluationInput,
    generated_at: datetime,
) -> GateSuiteDecision:
    _validate_test_bindings(program=program, config=config, evaluation=evaluation)
    try:
        report = aggregate_baseline_matrix(
            matrix=evaluation.matrix,
            suite=evaluation.suite,
            result=evaluation.result,
            ledger=evaluation.ledger,
            receipt_keys=evaluation.receipt_keys,
            generated_at=generated_at,
        )
    except Exception as exc:
        raise FrontierGateError(
            f"{config.track.value} raw receipt/ledger audit failed: {exc}"
        ) from exc
    evidence_sha256 = report.report_sha256
    full = _arm(report, BaselineArmId.ALETHEIA_FULL_K2)
    thresholds = config.thresholds
    criteria: list[GateCriterionResult] = []

    def add(
        suffix: str,
        category: Literal[
            "scientific",
            "comparison",
            "objective",
            "reliability",
            "cost",
            "integrity",
            "custody",
        ],
        relation: Literal[">=", "<=", "==", "present", "after", "before"],
        expected: object,
        observed: object | None,
        passed: bool,
        detail: str,
    ) -> None:
        criteria.append(
            _criterion(
                criterion_id=f"{config.track.value}.{suffix}",
                category=category,
                relation=relation,
                expected=expected,
                observed=observed,
                passed=passed,
                detail=detail,
                evidence_sha256=evidence_sha256,
            )
        )

    add(
        "selection_audit",
        "integrity",
        "==",
        True,
        report.selection_audit.complete,
        report.selection_audit.complete,
        "Every preregistered cell and authorized retry must survive aggregation.",
    )
    add(
        "full_k2.pass_at_1",
        "scientific",
        ">=",
        thresholds.min_full_k2_pass_at_1,
        full.pass_at_1,
        full.pass_at_1 >= thresholds.min_full_k2_pass_at_1,
        "Operational first-attempt success over every preregistered full-K2 cell.",
    )
    success = full.scientific_success_rate
    add(
        "full_k2.scientific_success_rate",
        "scientific",
        ">=",
        thresholds.min_full_k2_scientific_success_rate,
        success,
        success is not None and success >= thresholds.min_full_k2_scientific_success_rate,
        "Scientific success is conditioned only on scientifically valid final cells.",
    )
    valid_fraction = full.scientific_valid_cells / full.planned_cells
    invalid_fraction = 1.0 - valid_fraction
    retry_fraction = full.infrastructure_retries / full.planned_cells
    add(
        "full_k2.scientific_valid_fraction",
        "reliability",
        ">=",
        thresholds.min_full_k2_scientific_valid_fraction,
        valid_fraction,
        valid_fraction >= thresholds.min_full_k2_scientific_valid_fraction,
        "Scientific invalids remain visible and cannot be recoded as negative findings.",
    )
    add(
        "full_k2.final_invalid_fraction",
        "reliability",
        "<=",
        thresholds.max_full_k2_final_invalid_fraction,
        invalid_fraction,
        invalid_fraction <= thresholds.max_full_k2_final_invalid_fraction,
        "All final cells without a scientific verdict count against reliability.",
    )
    add(
        "full_k2.infrastructure_retry_fraction",
        "reliability",
        "<=",
        thresholds.max_full_k2_infrastructure_retry_fraction,
        retry_fraction,
        retry_fraction <= thresholds.max_full_k2_infrastructure_retry_fraction,
        "Authorized infrastructure retries are reported separately from scientific invalids.",
    )
    cost = full.observed_cost_usd
    complete_cost = cost.missing == 0 and cost.mean is not None
    add(
        "full_k2.complete_cost_receipts",
        "cost",
        "==",
        True,
        complete_cost,
        complete_cost,
        "Every full-K2 cell must retain a trusted cost observation.",
    )
    add(
        "full_k2.mean_cost_usd",
        "cost",
        "<=",
        thresholds.max_full_k2_mean_cost_usd,
        cost.mean,
        cost.mean is not None and cost.mean <= thresholds.max_full_k2_mean_cost_usd,
        "Mean observed cost must remain below the validation-calibrated ceiling.",
    )
    add(
        "full_k2.human_interventions",
        "integrity",
        "<=",
        thresholds.max_total_human_interventions,
        full.human_interventions,
        full.human_interventions <= thresholds.max_total_human_interventions,
        "Formal held-out runs cannot depend on operator intervention.",
    )
    add(
        "full_k2.contamination_declarations",
        "integrity",
        "<=",
        thresholds.max_contamination_declarations,
        full.contamination_declarations,
        full.contamination_declarations <= thresholds.max_contamination_declarations,
        "Any declared contamination prevents a clean Frontier Gate pass.",
    )

    for threshold in thresholds.comparisons:
        comparison = _comparison(report, threshold.comparator_arm)
        prefix = f"comparison.{threshold.comparator_arm.value}"
        valid_pairs = comparison.valid_scientific_pairs / comparison.planned_pairs
        add(
            f"{prefix}.valid_pair_fraction",
            "comparison",
            ">=",
            threshold.minimum_valid_pair_fraction,
            valid_pairs,
            valid_pairs >= threshold.minimum_valid_pair_fraction,
            "The preregistered paired comparison must retain enough scientific verdicts.",
        )
        add(
            f"{prefix}.unconditional_comparability",
            "comparison",
            "==",
            threshold.require_unconditional_comparability,
            comparison.unconditional_claim_allowed,
            comparison.unconditional_claim_allowed == threshold.require_unconditional_comparability,
            "Undisclosed budget, tool, or wall-time mismatches forbid an unconditional claim.",
        )
        complete_paired_cost = (
            comparison.cost_pairs == comparison.planned_pairs
            and comparison.mean_paired_cost_difference_usd is not None
        )
        add(
            f"{prefix}.complete_paired_cost",
            "cost",
            "==",
            True,
            complete_paired_cost,
            complete_paired_cost,
            "Cost comparison must cover every preregistered pair.",
        )
        paired_cost = comparison.mean_paired_cost_difference_usd
        add(
            f"{prefix}.mean_paired_cost_increase_usd",
            "cost",
            "<=",
            threshold.max_mean_paired_cost_increase_usd,
            paired_cost,
            paired_cost is not None and paired_cost <= threshold.max_mean_paired_cost_increase_usd,
            "Full-K2 paired cost increase must remain within the calibrated budget.",
        )
        if threshold.requirement is ComparisonRequirement.SUPERIORITY:
            effect = comparison.scientific_risk_difference
            ci_low = comparison.scientific_ci_low
            add(
                f"{prefix}.risk_difference",
                "comparison",
                ">=",
                threshold.minimum_effect_or_ci_bound,
                effect,
                effect is not None and effect >= threshold.minimum_effect_or_ci_bound,
                "Full K2 must achieve the preregistered minimum practical superiority effect.",
            )
            add(
                f"{prefix}.confidence_interval_low",
                "comparison",
                ">=",
                threshold.minimum_effect_or_ci_bound,
                ci_low,
                ci_low is not None and ci_low >= threshold.minimum_effect_or_ci_bound,
                "The paired confidence bound, not only the point estimate, must clear the effect.",
            )
            adjusted = comparison.holm_adjusted_p_value
            alpha = threshold.max_holm_adjusted_p_value
            assert alpha is not None
            add(
                f"{prefix}.holm_adjusted_p_value",
                "comparison",
                "<=",
                alpha,
                adjusted,
                adjusted is not None and adjusted <= alpha,
                "Superiority must survive the frozen Holm multiplicity correction.",
            )
        else:
            ci_low = comparison.scientific_ci_low
            add(
                f"{prefix}.noninferiority_ci_low",
                "comparison",
                ">=",
                threshold.minimum_effect_or_ci_bound,
                ci_low,
                ci_low is not None and ci_low >= threshold.minimum_effect_or_ci_bound,
                "The paired lower confidence bound must clear the frozen noninferiority margin.",
            )

    objectives = {item.metric: item for item in full.objective_metrics}
    for threshold in thresholds.objectives:
        summary = objectives.get(threshold.metric)
        distribution = summary.distribution if summary is not None else None
        mean = distribution.mean if distribution is not None else None
        missing = distribution.missing / full.planned_cells if distribution is not None else 1.0
        add(
            f"objective.{threshold.metric}.coverage",
            "objective",
            "<=",
            threshold.maximum_missing_fraction,
            missing,
            missing <= threshold.maximum_missing_fraction,
            "Objective-score missingness is an acceptance endpoint, not silently dropped.",
        )
        passed = mean is not None and (
            mean >= threshold.threshold
            if threshold.direction is ThresholdDirection.MINIMUM
            else mean <= threshold.threshold
        )
        add(
            f"objective.{threshold.metric}.mean",
            "objective",
            ">=" if threshold.direction is ThresholdDirection.MINIMUM else "<=",
            threshold.threshold,
            mean,
            passed,
            "Objective threshold was derived from the frozen validation/reference policy.",
        )

    try:
        final_ledger = evaluation.ledger.assert_integrity()
    except Exception as exc:
        raise FrontierGateError(
            f"{config.track.value} evaluation ledger failed during report generation: {exc}"
        ) from exc
    expected_ledger = (
        report.ledger.events,
        report.ledger.head_sha256,
        report.ledger.file_sha256,
    )
    observed_ledger = (
        final_ledger["events"],
        final_ledger["head_sha256"],
        final_ledger["file_sha256"],
    )
    if observed_ledger != expected_ledger:
        raise FrontierGateError(
            f"{config.track.value} evaluation ledger changed during report generation"
        )
    receipt_index = _receipt_index(evaluation.result, report)
    return GateSuiteDecision(
        track=config.track,
        suite_acceptance_config_sha256=config.config_sha256,
        test_matrix_manifest_sha256=config.test_matrix_manifest_sha256,
        test_suite_manifest_sha256=config.test_suite_manifest_sha256,
        result_sha256=evaluation.result.result_sha256,
        aggregate_report=report,
        receipt_index=receipt_index,
        criteria=tuple(criteria),
        verdict=_verdict(criteria),
    )


def _blocked_suite(config: SuiteAcceptanceConfig) -> GateSuiteDecision:
    criterion = _criterion(
        criterion_id=f"{config.track.value}.raw_evidence",
        category="integrity",
        relation="present",
        expected="matrix + suite + result + ledger + receipt keys",
        observed=None,
        passed=None,
        detail="No raw evidence bundle was supplied for this frozen program track.",
        evidence_sha256=config.config_sha256,
    )
    return GateSuiteDecision(
        track=config.track,
        suite_acceptance_config_sha256=config.config_sha256,
        test_matrix_manifest_sha256=config.test_matrix_manifest_sha256,
        test_suite_manifest_sha256=config.test_suite_manifest_sha256,
        criteria=(criterion,),
        verdict=GateVerdict.BLOCKED,
    )


def _evaluate_private_custody(
    *,
    program: FrontierGateAcceptanceConfig,
    private_input: PrivateGateInput,
    evaluation: GateEvaluationInput | None,
) -> PrivateCustodyEvidence:
    manifest = private_input.manifest
    ledger = private_input.ledger
    try:
        integrity = ledger.assert_integrity()
        events = ledger.events()
    except Exception as exc:
        raise FrontierGateError(f"private custody ledger integrity audit failed: {exc}") from exc
    file_sha256 = str(integrity["file_sha256"])
    head_sha256 = integrity["head_sha256"]
    criteria: list[GateCriterionResult] = []
    private_config = program.suite(FrontierGateTrack.PRIVATE_PROSPECTIVE)

    def add(
        suffix: str,
        relation: Literal[">=", "<=", "==", "present", "after", "before"],
        expected: object,
        observed: object | None,
        passed: bool | None,
        detail: str,
    ) -> None:
        criteria.append(
            _criterion(
                criterion_id=f"private_custody.{suffix}",
                category="custody",
                relation=relation,
                expected=expected,
                observed=observed,
                passed=passed,
                detail=detail,
                evidence_sha256=file_sha256,
            )
        )

    add(
        "acceptance_config_binding",
        "==",
        program.config_sha256,
        manifest.acceptance_config_sha256,
        manifest.acceptance_config_sha256 == program.config_sha256,
        "The encrypted private suite must name this exact frozen program contract.",
    )
    expected_tier = PrivateSuiteTier(program.tier.value)
    add(
        "formal_tier",
        "==",
        expected_tier.value,
        manifest.tier.value,
        manifest.tier is expected_tier,
        "Private-suite rigor must equal the program acceptance tier.",
    )
    binding_actual = (
        manifest.evaluation_suite_manifest_sha256,
        manifest.evaluator_manifest_sha256,
        manifest.baseline_matrix_manifest_sha256,
    )
    binding_expected = (
        private_config.test_suite_manifest_sha256,
        private_config.evaluator_manifest_sha256,
        private_config.test_matrix_manifest_sha256,
    )
    add(
        "test_bindings",
        "==",
        content_sha256({"bindings": binding_expected}),
        content_sha256({"bindings": binding_actual}),
        binding_actual == binding_expected,
        "Custody must bind the same private suite, evaluator, and four-arm test matrix.",
    )
    add(
        "manifest_after_acceptance_freeze",
        "after",
        program.frozen_at.isoformat(),
        manifest.frozen_at.isoformat(),
        manifest.frozen_at >= program.frozen_at,
        "Private-suite custody is frozen only after the acceptance hash exists.",
    )

    registered_events = [
        event
        for event in events
        if event.event_type is CustodyEventType.SUITE_REGISTERED
        and event.private_suite_manifest_sha256 == manifest.manifest_sha256
    ]
    state: PrivateCustodyState | None = None
    state_error: str | None = None
    if len(registered_events) == 1:
        try:
            state = ledger.state(manifest)
        except Exception as exc:
            state_error = str(exc)
    add(
        "unique_registration",
        "==",
        1,
        len(registered_events),
        len(registered_events) == 1 and state is not None,
        "The exact private manifest must occur once in the append-only registry"
        + (f"; state error: {state_error}" if state_error else "."),
    )

    def state_check(
        suffix: str,
        actual: bool | int,
        expected: bool | int,
        detail: str,
        *,
        relation: Literal[">=", "<=", "=="] = "==",
    ) -> None:
        if state is None:
            add(suffix, relation, expected, None, False, detail)
        else:
            passed = (
                actual >= expected
                if relation == ">="
                else actual <= expected
                if relation == "<="
                else actual == expected
            )
            add(suffix, relation, expected, actual, passed, detail)

    state_check(
        "authorization_count",
        len(state.authorization_ids) if state is not None else 0,
        1,
        "At least one frozen two-person authorization must precede one-time access.",
        relation=">=",
    )
    state_check(
        "access_opened",
        state.opened_access_id is not None if state is not None else False,
        True,
        "Exactly one claimed access scope must have opened.",
    )
    state_check(
        "materialized",
        state.materialization_receipt_sha256 is not None if state is not None else False,
        True,
        "Ciphertext and decrypted identities require a verified materialization receipt.",
    )
    state_check(
        "materialization_failed",
        state.materialization_failed if state is not None else True,
        False,
        "Any post-open materialization failure is terminal and cannot pass.",
    )
    state_check(
        "contamination_reports",
        len(state.contamination_report_ids) if state is not None else 1,
        0,
        "Any private-test contamination report fails the clean scientific claim.",
        relation="<=",
    )
    state_check(
        "access_closed",
        state.access_closed if state is not None else False,
        True,
        "One-time private access must be closed after evaluation.",
    )
    state_check(
        "cleanup_receipt",
        state.cleanup_receipt_sha256 is not None if state is not None else False,
        True,
        "Plaintext disposal requires a verified cleanup receipt.",
    )
    state_check(
        "suite_retired",
        state.suite_retired if state is not None else False,
        True,
        "The one-time suite must be retired before the final report can pass.",
    )

    relevant = [
        event for event in events if event.private_suite_manifest_sha256 == manifest.manifest_sha256
    ]
    opened_events = [
        event for event in relevant if event.event_type is CustodyEventType.ACCESS_OPENED
    ]
    closed_events = [
        event for event in relevant if event.event_type is CustodyEventType.ACCESS_CLOSED
    ]
    opened_at = opened_events[0].occurred_at if len(opened_events) == 1 else None
    closed_at = closed_events[0].occurred_at if len(closed_events) == 1 else None
    add(
        "single_open_event",
        "==",
        1,
        len(opened_events),
        len(opened_events) == 1,
        "One-time access requires exactly one access-open event.",
    )
    add(
        "single_close_event",
        "==",
        1,
        len(closed_events),
        len(closed_events) == 1,
        "One-time access requires exactly one cleanup-bound close event.",
    )
    add(
        "access_after_acceptance_freeze",
        "after",
        program.frozen_at.isoformat(),
        opened_at.isoformat() if opened_at is not None else None,
        opened_at is not None and opened_at >= program.frozen_at,
        "Private plaintext cannot open until the final acceptance rule is frozen.",
    )
    add(
        "test_after_access_open",
        "after",
        opened_at.isoformat() if opened_at is not None else "access-open event",
        evaluation.result.started_at.isoformat() if evaluation is not None else None,
        (
            None
            if evaluation is None
            else opened_at is not None and evaluation.result.started_at >= opened_at
        ),
        "Private test execution must occur inside the authorized access interval.",
    )
    add(
        "cleanup_after_test",
        "after",
        evaluation.result.ended_at.isoformat() if evaluation is not None else "test result",
        closed_at.isoformat() if closed_at is not None else None,
        (
            None
            if evaluation is None
            else closed_at is not None and closed_at >= evaluation.result.ended_at
        ),
        "Plaintext cleanup and access closure must occur after the complete test execution.",
    )

    materialization_sha256: str | None = None
    cleanup_sha256: str | None = None
    materialization_valid = False
    cleanup_valid = False
    if state is not None and state.materialization_receipt_sha256 is not None:
        try:
            materialization = ledger.materialization_receipt(manifest)
            materialization_sha256 = materialization.receipt_sha256
            materialization_valid = (
                materialization.evaluation_suite_manifest_sha256
                == private_config.test_suite_manifest_sha256
                and materialization.baseline_matrix_manifest_sha256
                == private_config.test_matrix_manifest_sha256
                and materialization_sha256 == state.materialization_receipt_sha256
            )
        except Exception:
            materialization_valid = False
    add(
        "materialization_receipt_integrity",
        "==",
        True,
        materialization_valid,
        materialization_valid,
        "Embedded materialization content and its custody hash must bind the held-out test.",
    )
    if state is not None and state.cleanup_receipt_sha256 is not None:
        try:
            cleanup = ledger.cleanup_receipt(manifest)
            cleanup_sha256 = cleanup.receipt_sha256
            cleanup_valid = cleanup_sha256 == state.cleanup_receipt_sha256
        except Exception:
            cleanup_valid = False
    add(
        "cleanup_receipt_integrity",
        "==",
        True,
        cleanup_valid,
        cleanup_valid,
        "Embedded plaintext-cleanup content must match the hash-chained close event.",
    )

    try:
        final_integrity = ledger.assert_integrity()
    except Exception as exc:
        raise FrontierGateError(
            f"private custody ledger changed or failed during reporting: {exc}"
        ) from exc
    stable_fields = ("events", "head_sha256", "file_sha256")
    if any(final_integrity[field] != integrity[field] for field in stable_fields):
        raise FrontierGateError("private custody ledger changed during report generation")

    return PrivateCustodyEvidence(
        private_suite_manifest_sha256=manifest.manifest_sha256,
        custody_ledger_events=int(integrity["events"]),
        custody_ledger_head_sha256=head_sha256,
        custody_ledger_file_sha256=file_sha256,
        state=state,
        materialization_receipt_sha256=materialization_sha256,
        cleanup_receipt_sha256=cleanup_sha256,
        access_opened_at=opened_at,
        access_closed_at=closed_at,
        criteria=tuple(criteria),
        verdict=_verdict(criteria),
    )


def _plot_points(
    decisions: Sequence[GateSuiteDecision],
    config: FrontierGateAcceptanceConfig,
) -> tuple[GatePlotPoint, ...]:
    points: list[GatePlotPoint] = []
    for decision in decisions:
        if decision.aggregate_report is None:
            continue
        suite_config = config.suite(decision.track)
        full = _arm(decision.aggregate_report, BaselineArmId.ALETHEIA_FULL_K2)
        points.extend(
            (
                GatePlotPoint(
                    track=decision.track,
                    metric="pass_at_1",
                    observed=full.pass_at_1,
                    threshold=suite_config.thresholds.min_full_k2_pass_at_1,
                    direction=ThresholdDirection.MINIMUM,
                ),
                GatePlotPoint(
                    track=decision.track,
                    metric="scientific_valid_fraction",
                    observed=full.scientific_valid_cells / full.planned_cells,
                    threshold=suite_config.thresholds.min_full_k2_scientific_valid_fraction,
                    direction=ThresholdDirection.MINIMUM,
                ),
            )
        )
        if full.observed_cost_usd.mean is not None:
            points.append(
                GatePlotPoint(
                    track=decision.track,
                    metric="mean_cost_usd",
                    observed=full.observed_cost_usd.mean,
                    threshold=suite_config.thresholds.max_full_k2_mean_cost_usd,
                    direction=ThresholdDirection.MAXIMUM,
                )
            )
    return tuple(points)


def generate_frontier_gate_report(
    *,
    config: FrontierGateAcceptanceConfig,
    evaluations: Mapping[FrontierGateTrack, GateEvaluationInput],
    private_input: PrivateGateInput | None = None,
    generated_at: datetime | None = None,
) -> FrontierGateReport:
    """Re-audit raw test evidence and issue a fail-closed program decision."""

    configured = {item.track for item in config.suites}
    supplied = set(evaluations)
    extra = supplied - configured
    if extra:
        raise FrontierGateError(
            "unconfigured test tracks were supplied: "
            + ", ".join(sorted(item.value for item in extra))
        )
    for track, evaluation in evaluations.items():
        if evaluation.track is not track:
            raise FrontierGateError("evaluation mapping key and embedded track differ")

    timestamp = generated_at or datetime.now(timezone.utc)
    completed_after_report = [
        track.value
        for track, evaluation in evaluations.items()
        if evaluation.result.ended_at > timestamp
    ]
    if completed_after_report:
        raise FrontierGateError(
            "report timestamp predates completed test evidence: "
            + ", ".join(sorted(completed_after_report))
        )

    decisions: list[GateSuiteDecision] = []
    missing = configured - supplied
    for suite_config in config.suites:
        evaluation = evaluations.get(suite_config.track)
        decisions.append(
            _blocked_suite(suite_config)
            if evaluation is None
            else _evaluate_suite(
                program=config,
                config=suite_config,
                evaluation=evaluation,
                generated_at=timestamp,
            )
        )

    private_configured = FrontierGateTrack.PRIVATE_PROSPECTIVE in configured
    if private_input is not None and not private_configured:
        raise FrontierGateError("private custody evidence was supplied to a program without it")
    private_custody = (
        _evaluate_private_custody(
            program=config,
            private_input=private_input,
            evaluation=evaluations.get(FrontierGateTrack.PRIVATE_PROSPECTIVE),
        )
        if private_input is not None
        else None
    )
    if private_configured and private_input is None:
        missing.add(FrontierGateTrack.PRIVATE_PROSPECTIVE)

    if (
        private_custody is not None
        and private_custody.access_closed_at is not None
        and private_custody.access_closed_at > timestamp
    ):
        raise FrontierGateError("report timestamp predates private access closure")

    supplied_names = ", ".join(sorted(track.value for track in supplied)) or "none"
    program_criteria = [
        _criterion(
            criterion_id="program.acceptance_config_frozen",
            category="integrity",
            relation="==",
            expected="frozen",
            observed=config.state,
            passed=config.state == "frozen",
            detail="The accepted scientific claim and all suite thresholds are content-addressed.",
            evidence_sha256=config.config_sha256,
        ),
        _criterion(
            criterion_id="program.all_test_tracks_present",
            category="integrity",
            relation="present",
            expected=", ".join(sorted(track.value for track in configured)),
            observed=supplied_names,
            passed=None if configured - supplied else True,
            detail="A program conclusion requires raw evidence for every configured test track.",
            evidence_sha256=config.config_sha256,
        ),
    ]
    if private_configured:
        program_criteria.append(
            _criterion(
                criterion_id="program.private_custody_complete",
                category="custody",
                relation="present",
                expected="closed, cleaned, uncontaminated, retired custody chain",
                observed=(private_custody.verdict.value if private_custody is not None else None),
                passed=(
                    None if private_custody is None else private_custody.verdict is GateVerdict.PASS
                ),
                detail="Private prospective evidence is usable only after the one-time custody lifecycle closes.",
                evidence_sha256=(
                    private_custody.evidence_sha256
                    if private_custody is not None
                    else config.config_sha256
                ),
            )
        )

    statuses = [criterion.status for criterion in program_criteria]
    statuses.extend(criterion.status for decision in decisions for criterion in decision.criteria)
    if private_custody is not None:
        statuses.extend(criterion.status for criterion in private_custody.criteria)
    missing_tracks = tuple(sorted(missing, key=lambda item: item.value))
    overall = (
        GateVerdict.BLOCKED
        if missing_tracks or CriterionStatus.BLOCKED in statuses
        else GateVerdict.FAIL
        if CriterionStatus.FAIL in statuses
        else GateVerdict.PASS
    )
    decision_tuple = tuple(decisions)
    criterion_tuple = tuple(program_criteria)
    evidence_bundle_sha256 = content_sha256(
        {
            "acceptance_config_sha256": config.config_sha256,
            "suite_decision_sha256s": [item.decision_sha256 for item in decision_tuple],
            "private_custody_evidence_sha256": (
                private_custody.evidence_sha256 if private_custody is not None else None
            ),
            "program_criteria": [item.model_dump(mode="json") for item in criterion_tuple],
        }
    )
    limitations = (
        "The verdict applies only to the exact frozen suites, systems, evaluator, budgets, and analysis policy.",
        "A pass is evidence of benchmark readiness, not proof of unrestricted autonomous scientific competence.",
        "Paired comparisons remain conditional wherever a frozen comparability disclosure exists.",
        "New model weights, tools, prompts, evaluator logic, or contamination evidence require a new gate.",
    )
    return FrontierGateReport(
        program_config_id=config.program_config_id,
        acceptance_config_sha256=config.config_sha256,
        tier=config.tier,
        scientific_claim=config.scientific_claim,
        suite_decisions=decision_tuple,
        private_custody=private_custody,
        program_criteria=criterion_tuple,
        plot_points=_plot_points(decision_tuple, config),
        missing_tracks=missing_tracks,
        overall_verdict=overall,
        scientific_claim_allowed=overall is GateVerdict.PASS,
        limitations=limitations,
        evidence_bundle_sha256=evidence_bundle_sha256,
        generated_at=timestamp,
    )


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_frontier_gate_markdown(
    report: FrontierGateReport,
    *,
    plot_filename: str = "frontier_gate_report.svg",
) -> str:
    """Render the typed gate artifact without recomputing or weakening its verdict."""

    lines = [
        "# Frontier Gate report",
        "",
        f"**Overall verdict: {report.overall_verdict.value.upper()}**",
        "",
        f"Scientific claim allowed: `{'yes' if report.scientific_claim_allowed else 'no'}`  ",
        f"Program: `{_markdown(report.program_config_id)}`  ",
        f"Acceptance config: `{report.acceptance_config_sha256}`  ",
        f"Evidence bundle: `{report.evidence_bundle_sha256}`  ",
        f"Generated: `{report.generated_at.isoformat()}`",
        "",
        "## Frozen claim",
        "",
        _markdown(report.scientific_claim),
        "",
    ]
    if report.missing_tracks:
        lines.extend(
            (
                "## Blocking evidence gaps",
                "",
                ", ".join(f"`{item.value}`" for item in report.missing_tracks),
                "",
            )
        )
    lines.extend(
        (
            "## Track outcomes",
            "",
            "| Track | Verdict | Result | Aggregate | Receipt index |",
            "|---|---:|---|---|---|",
        )
    )
    for decision in report.suite_decisions:
        lines.append(
            "| "
            + " | ".join(
                (
                    decision.track.value,
                    decision.verdict.value.upper(),
                    decision.result_sha256 or "missing",
                    (
                        decision.aggregate_report.report_sha256
                        if decision.aggregate_report is not None
                        else "missing"
                    ),
                    (
                        decision.receipt_index.index_sha256
                        if decision.receipt_index is not None
                        else "missing"
                    ),
                )
            )
            + " |"
        )
    lines.extend(("", f"![Gate cost and reliability plot]({_markdown(plot_filename)})", ""))

    lines.extend(
        (
            "## Program criteria",
            "",
            "| Criterion | Status | Required | Observed | Detail |",
            "|---|---:|---|---|---|",
        )
    )
    for criterion in report.program_criteria:
        lines.append(
            "| "
            + " | ".join(
                _markdown(value)
                for value in (
                    criterion.criterion_id,
                    criterion.status.value.upper(),
                    f"{criterion.relation} {criterion.expected}",
                    criterion.observed or "missing",
                    criterion.detail,
                )
            )
            + " |"
        )
    lines.append("")

    for decision in report.suite_decisions:
        lines.extend(
            (
                f"## Track: {decision.track.value}",
                "",
                "| Criterion | Category | Status | Required | Observed |",
                "|---|---|---:|---|---|",
            )
        )
        for criterion in decision.criteria:
            lines.append(
                "| "
                + " | ".join(
                    _markdown(value)
                    for value in (
                        criterion.criterion_id,
                        criterion.category,
                        criterion.status.value.upper(),
                        f"{criterion.relation} {criterion.expected}",
                        criterion.observed or "missing",
                    )
                )
                + " |"
            )
        lines.append("")
        if decision.aggregate_report is not None:
            full = _arm(decision.aggregate_report, BaselineArmId.ALETHEIA_FULL_K2)
            lines.extend(
                (
                    "Failure decomposition (full K2):",
                    "",
                    "- Final statuses: `"
                    + _markdown(
                        ", ".join(
                            f"{key}={value}"
                            for key, value in sorted(full.final_status_counts.items())
                        )
                    )
                    + "`",
                    "- Invalid reasons: `"
                    + _markdown(
                        ", ".join(
                            f"{key}={value}"
                            for key, value in sorted(full.invalid_reason_counts.items())
                        )
                        or "none"
                    )
                    + "`",
                    f"- Infrastructure retries: `{full.infrastructure_retries}`",
                    f"- Human interventions: `{full.human_interventions}`",
                    f"- Contamination declarations: `{full.contamination_declarations}`",
                    "",
                )
            )
        if decision.receipt_index is not None:
            lines.extend(
                (
                    f"Receipt-linked attempts: `{len(decision.receipt_index.attempts)}`  ",
                    f"Ledger head: `{decision.receipt_index.ledger_head_sha256}`  ",
                    f"Ledger file: `{decision.receipt_index.ledger_file_sha256}`",
                    "",
                )
            )

    if report.private_custody is not None:
        custody = report.private_custody
        lines.extend(
            (
                "## Private-suite custody",
                "",
                f"Verdict: **{custody.verdict.value.upper()}**  ",
                f"Manifest: `{custody.private_suite_manifest_sha256}`  ",
                f"Ledger head: `{custody.custody_ledger_head_sha256 or 'missing'}`  ",
                f"Ledger file: `{custody.custody_ledger_file_sha256}`",
                "",
                "| Criterion | Status | Required | Observed |",
                "|---|---:|---|---|",
            )
        )
        for criterion in custody.criteria:
            lines.append(
                "| "
                + " | ".join(
                    _markdown(value)
                    for value in (
                        criterion.criterion_id,
                        criterion.status.value.upper(),
                        f"{criterion.relation} {criterion.expected}",
                        criterion.observed or "missing",
                    )
                )
                + " |"
            )
        lines.append("")

    lines.extend(("## Limitations", ""))
    lines.extend(f"- {_markdown(item)}" for item in report.limitations)
    lines.extend(("", f"Report SHA-256: `{report.report_sha256}`", ""))
    return "\n".join(lines)


def render_frontier_gate_svg(report: FrontierGateReport) -> str:
    """Render a deterministic, dependency-free threshold/observation plot."""

    width = 1040
    row_height = 34
    top = 92
    height = max(180, top + row_height * len(report.plot_points) + 54)
    status_color = {
        GateVerdict.PASS: "#14804A",
        GateVerdict.FAIL: "#C0362C",
        GateVerdict.BLOCKED: "#B26A00",
    }[report.overall_verdict]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Frontier Gate cost and reliability thresholds</title>',
        '<desc id="desc">Observed values versus validation-calibrated acceptance thresholds.</desc>',
        '<rect width="100%" height="100%" fill="#FBFCFE"/>',
        '<text x="28" y="36" font-family="system-ui,sans-serif" font-size="22" '
        'font-weight="700" fill="#17212B">Frontier Gate evidence profile</text>',
        f'<text x="28" y="64" font-family="system-ui,sans-serif" font-size="15" '
        f'font-weight="700" fill="{status_color}">{report.overall_verdict.value.upper()}</text>',
        '<text x="220" y="64" font-family="system-ui,sans-serif" font-size="13" '
        'fill="#52606D">bar = observed · marker = threshold</text>',
    ]
    for index, point in enumerate(report.plot_points):
        y = top + index * row_height
        maximum = max(abs(point.observed), abs(point.threshold), 1e-12)
        if point.metric != "mean_cost_usd":
            maximum = max(maximum, 1.0)
        observed_width = 350 * max(0.0, point.observed) / maximum
        threshold_x = 610 + 350 * max(0.0, point.threshold) / maximum
        passed = (
            point.observed >= point.threshold
            if point.direction is ThresholdDirection.MINIMUM
            else point.observed <= point.threshold
        )
        bar_color = "#2D8C61" if passed else "#D85B4A"
        label = html.escape(f"{point.track.value} · {point.metric}")
        values = html.escape(f"observed {point.observed:.4g} / threshold {point.threshold:.4g}")
        parts.extend(
            (
                f'<text x="28" y="{y + 15}" font-family="ui-monospace,monospace" '
                f'font-size="12" fill="#263442">{label}</text>',
                f'<rect x="610" y="{y + 3}" width="350" height="13" rx="3" fill="#E4E9EF"/>',
                f'<rect x="610" y="{y + 3}" width="{observed_width:.2f}" height="13" '
                f'rx="3" fill="{bar_color}"/>',
                f'<line x1="{threshold_x:.2f}" y1="{y}" x2="{threshold_x:.2f}" '
                f'y2="{y + 20}" stroke="#17212B" stroke-width="2"/>',
                f'<text x="972" y="{y + 15}" text-anchor="end" '
                f'font-family="system-ui,sans-serif" font-size="11" fill="#52606D">{values}</text>',
            )
        )
    if not report.plot_points:
        parts.append(
            '<text x="28" y="112" font-family="system-ui,sans-serif" font-size="15" '
            'fill="#B26A00">No complete raw track evidence was available to plot.</text>'
        )
    parts.extend(
        (
            f'<text x="28" y="{height - 22}" font-family="ui-monospace,monospace" '
            f'font-size="10" fill="#6B7785">evidence {report.evidence_bundle_sha256}</text>',
            "</svg>",
        )
    )
    return "\n".join(parts) + "\n"
