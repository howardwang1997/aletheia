"""Audited aggregation for paired Frontier Gate baseline matrices.

The report is derived from every preregistered slot.  Before computing statistics it reconciles
the result bundle against the evaluator's hash-chained ledger, verifies every signed score, checks
retry lineage, and rejects missing or extra attempts.  Scientific invalids remain distinct from
negative scientific results; an operational pass@1 view is reported separately.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.baselines import (
    BaselineArmId,
    BaselineAttemptRecord,
    BaselineMatrixError,
    BaselineMatrixPlan,
    BaselineMatrixResult,
    ComparabilityDimension,
    EvaluationLedgerReceipt,
    baseline_schedule_sha256,
    build_baseline_run_plans,
    validate_matrix_suite,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.schemas import (
    AttemptStatus,
    EvaluationAttempt,
    EvaluationScore,
    EvaluationSuite,
    FrozenModel,
    content_sha256,
)

CellKey = tuple[BaselineArmId, str, int]


class NumericSummary(FrozenModel):
    schema_version: Literal[1] = 1
    observed: int = Field(ge=0)
    missing: int = Field(ge=0)
    total: float | None = None
    mean: float | None = None
    median: float | None = None
    q1: float | None = None
    q3: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _empty_summary_has_no_statistics(self) -> "NumericSummary":
        values = (
            self.total,
            self.mean,
            self.median,
            self.q1,
            self.q3,
            self.minimum,
            self.maximum,
        )
        if self.observed == 0 and any(value is not None for value in values):
            raise ValueError("an empty numeric summary cannot contain statistics")
        if self.observed > 0 and any(value is None for value in values):
            raise ValueError("a non-empty numeric summary requires all statistics")
        return self


class ObjectiveMetricSummary(FrozenModel):
    schema_version: Literal[1] = 1
    metric: str = Field(min_length=1)
    distribution: NumericSummary


class BaselineArmSummary(FrozenModel):
    schema_version: Literal[1] = 1
    arm_id: BaselineArmId
    planned_cells: int = Field(ge=1)
    retained_attempts: int = Field(ge=1)
    infrastructure_retries: int = Field(ge=0)
    final_status_counts: dict[str, int]
    all_attempt_status_counts: dict[str, int]
    execution_exit_reason_counts: dict[str, int]
    invalid_reason_counts: dict[str, int]
    unscored_invalid_attempts: int = Field(ge=0)
    nonfinal_scored_invalid_attempts: int = Field(ge=0)
    scientific_valid_cells: int = Field(ge=0)
    scientific_successes: int = Field(ge=0)
    pass_at_1: float = Field(ge=0.0, le=1.0)
    scientific_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    objective_metrics: tuple[ObjectiveMetricSummary, ...]
    observed_cost_usd: NumericSummary
    wall_time_s: NumericSummary
    input_tokens: NumericSummary
    output_tokens: NumericSummary
    human_interventions: int = Field(ge=0)
    submissions_declaring_contamination: int = Field(ge=0)
    contamination_declarations: int = Field(ge=0)


class ObjectivePairedEffect(FrozenModel):
    schema_version: Literal[1] = 1
    metric: str = Field(min_length=1)
    valid_pairs: int = Field(ge=0)
    excluded_pairs: int = Field(ge=0)
    candidate_wins: int = Field(ge=0)
    comparator_wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    mean_difference: float | None = None
    median_difference: float | None = None
    confidence_interval_low: float | None = None
    confidence_interval_high: float | None = None


class BaselinePairwiseComparison(FrozenModel):
    schema_version: Literal[1] = 1
    candidate_arm: Literal[BaselineArmId.ALETHEIA_FULL_K2] = BaselineArmId.ALETHEIA_FULL_K2
    comparator_arm: BaselineArmId
    planned_pairs: int = Field(ge=1)
    valid_scientific_pairs: int = Field(ge=0)
    excluded_scientific_pairs: int = Field(ge=0)
    candidate_wins: int = Field(ge=0)
    comparator_wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    candidate_scientific_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    comparator_scientific_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    scientific_risk_difference: float | None = Field(default=None, ge=-1.0, le=1.0)
    scientific_ci_low: float | None = Field(default=None, ge=-1.0, le=1.0)
    scientific_ci_high: float | None = Field(default=None, ge=-1.0, le=1.0)
    raw_exact_p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    holm_adjusted_p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_operational_pass_at_1: float = Field(ge=0.0, le=1.0)
    comparator_operational_pass_at_1: float = Field(ge=0.0, le=1.0)
    operational_risk_difference: float = Field(ge=-1.0, le=1.0)
    operational_ci_low: float = Field(ge=-1.0, le=1.0)
    operational_ci_high: float = Field(ge=-1.0, le=1.0)
    cost_pairs: int = Field(ge=0)
    excluded_cost_pairs: int = Field(ge=0)
    mean_paired_cost_difference_usd: float | None = None
    comparability_mismatches: tuple[ComparabilityDimension, ...]
    unconditional_claim_allowed: bool
    secondary_objective_effects: tuple[ObjectivePairedEffect, ...]

    @model_validator(mode="after")
    def _comparison_counts_and_claim_scope_agree(self) -> "BaselinePairwiseComparison":
        if self.comparator_arm is BaselineArmId.ALETHEIA_FULL_K2:
            raise ValueError("full K2 cannot be its own comparator")
        if self.valid_scientific_pairs + self.excluded_scientific_pairs != self.planned_pairs:
            raise ValueError("scientific pair accounting must cover every preregistered pair")
        if self.candidate_wins + self.comparator_wins + self.ties != self.valid_scientific_pairs:
            raise ValueError("paired win/loss/tie counts must cover valid scientific pairs")
        if self.cost_pairs + self.excluded_cost_pairs != self.planned_pairs:
            raise ValueError("cost pair accounting must cover every preregistered pair")
        if self.unconditional_claim_allowed == bool(self.comparability_mismatches):
            raise ValueError("unconditional claims are allowed exactly when no mismatch is present")
        return self


class NoBestOfNAudit(FrozenModel):
    schema_version: Literal[1] = 1
    selection_policy: Literal["all_preregistered_slots_no_best_of_n"] = (
        "all_preregistered_slots_no_best_of_n"
    )
    planned_cells: int = Field(ge=1)
    observed_cells: int = Field(ge=1)
    retained_attempts: int = Field(ge=1)
    authorized_infrastructure_retries: int = Field(ge=0)
    omitted_ledger_attempts: Literal[0] = 0
    undeclared_attempts: Literal[0] = 0
    incomplete_cells: Literal[0] = 0
    complete: Literal[True] = True


class BaselineAggregateReport(FrozenModel):
    schema_version: Literal[1] = 1
    matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    confidence_level: float = Field(gt=0.5, lt=1.0)
    ledger: EvaluationLedgerReceipt
    selection_audit: NoBestOfNAudit
    arms: tuple[BaselineArmSummary, ...] = Field(min_length=4, max_length=4)
    comparisons: tuple[BaselinePairwiseComparison, ...] = Field(min_length=3, max_length=3)

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True)
class _AuditedAttempts:
    lineages: dict[CellKey, tuple[BaselineAttemptRecord, ...]]
    final: dict[CellKey, BaselineAttemptRecord]
    retries: int


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(values: Sequence[float], *, missing: int) -> NumericSummary:
    clean = [float(value) for value in values]
    if not clean:
        return NumericSummary(observed=0, missing=missing)
    return NumericSummary(
        observed=len(clean),
        missing=missing,
        total=sum(clean),
        mean=sum(clean) / len(clean),
        median=_quantile(clean, 0.5),
        q1=_quantile(clean, 0.25),
        q3=_quantile(clean, 0.75),
        minimum=min(clean),
        maximum=max(clean),
    )


def _score(record: BaselineAttemptRecord) -> EvaluationScore | None:
    if record.scorer_receipt is None:
        return None
    return record.scorer_receipt.receipt.score


def _hierarchical_ci(
    values_by_task: Mapping[str, Sequence[float]],
    *,
    confidence_level: float,
    resamples: int,
    seed: int,
) -> tuple[float, float]:
    if not values_by_task:
        raise ValueError("cannot bootstrap an empty paired sample")
    task_ids = sorted(values_by_task)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        sampled: list[float] = []
        for _task_index in range(len(task_ids)):
            task_id = task_ids[rng.randrange(len(task_ids))]
            values = values_by_task[task_id]
            for _repeat_index in range(len(values)):
                sampled.append(float(values[rng.randrange(len(values))]))
        estimates.append(sum(sampled) / len(sampled))
    tail = (1.0 - confidence_level) / 2.0
    return _quantile(estimates, tail), _quantile(estimates, 1.0 - tail)


def _comparison_seed(base_seed: int, comparator: BaselineArmId, label: str) -> int:
    digest = content_sha256(
        {"base_seed": base_seed, "comparator": comparator.value, "label": label}
    )
    return int(digest[:16], 16)


def _exact_two_sided_sign_p(wins: int, losses: int) -> float | None:
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    smaller = min(wins, losses)
    tail = sum(math.comb(discordant, index) for index in range(smaller + 1)) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _holm_adjust(raw: Mapping[BaselineArmId, float | None]) -> dict[BaselineArmId, float | None]:
    available = sorted(
        ((arm, value) for arm, value in raw.items() if value is not None),
        key=lambda item: item[1],
    )
    adjusted: dict[BaselineArmId, float | None] = {arm: None for arm in raw}
    running = 0.0
    count = len(available)
    for rank, (arm, value) in enumerate(available):
        running = max(running, min(1.0, (count - rank) * value))
        adjusted[arm] = running
    return adjusted


def _verify_result_and_attempts(
    *,
    matrix: BaselineMatrixPlan,
    suite: EvaluationSuite,
    result: BaselineMatrixResult,
    ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
) -> _AuditedAttempts:
    validate_matrix_suite(matrix, suite)
    if result.matrix_manifest_sha256 != matrix.manifest_sha256:
        raise BaselineMatrixError("result bundle is not bound to this baseline preregistration")
    if result.suite_manifest_sha256 != suite.manifest_sha256:
        raise BaselineMatrixError("result bundle is not bound to this suite")
    if result.schedule_sha256 != baseline_schedule_sha256(matrix):
        raise BaselineMatrixError("result bundle schedule differs from the frozen block order")

    expected_plans = build_baseline_run_plans(matrix, suite)
    if result.run_plans != expected_plans:
        raise BaselineMatrixError("materialized arm run plans differ from the frozen matrix")
    plans = {item.arm_id: item.run_plan for item in expected_plans}
    plan_arm = {item.run_plan.manifest_sha256: item.arm_id for item in expected_plans}

    current_ledger = EvaluationLedgerReceipt.model_validate(ledger.assert_integrity())
    if (
        current_ledger.events != result.ledger.events
        or current_ledger.head_sha256 != result.ledger.head_sha256
        or current_ledger.file_sha256 != result.ledger.file_sha256
    ):
        raise BaselineMatrixError("evaluator ledger changed after the matrix result was sealed")
    events = ledger.events()
    registered = {
        event.payload.get("plan_sha256")
        for event in events
        if event.event_type == "run_plan_registered"
    }
    missing_plans = set(plan_arm) - registered
    if missing_plans:
        raise BaselineMatrixError(
            f"matrix run plans are absent from the ledger: {sorted(missing_plans)}"
        )

    ledger_created_ids: set[str] = set()
    ledger_terminal: dict[str, EvaluationAttempt] = {}
    score_events: dict[str, str] = {}
    execution_events: dict[str, str] = {}
    manifest_events: dict[str, str] = {}
    submission_events: dict[str, str] = {}
    retry_events: dict[str, str] = {}
    for event in events:
        if event.event_type == "attempt_state":
            attempt = EvaluationAttempt.model_validate(event.payload["attempt"])
            if attempt.run_plan_sha256 not in plan_arm:
                continue
            if attempt.status is AttemptStatus.CREATED:
                ledger_created_ids.add(attempt.attempt_id)
            if attempt.status in {
                AttemptStatus.COMPLETED,
                AttemptStatus.SCIENTIFIC_FAILURE,
                AttemptStatus.INVALID,
                AttemptStatus.INFRA_FAILURE,
                AttemptStatus.TIMEOUT,
            }:
                ledger_terminal[attempt.attempt_id] = attempt
        elif event.event_type == "score_receipt_issued" and event.attempt_id:
            score_events[event.attempt_id] = event.payload["signed_envelope_sha256"]
        elif event.event_type == "execution_receipt_issued" and event.attempt_id:
            execution_events[event.attempt_id] = event.payload["execution_receipt_sha256"]
        elif event.event_type == "attempt_manifest_frozen" and event.attempt_id:
            manifest_events[event.attempt_id] = event.payload["attempt_manifest_sha256"]
        elif event.event_type == "submission_accepted" and event.attempt_id:
            submission_events[event.attempt_id] = event.payload["submission_sha256"]
        elif event.event_type == "retry_authorized" and event.attempt_id:
            retry_events[event.attempt_id] = event.payload["retry_of_attempt_id"]

    result_ids = {record.attempt.attempt_id for record in result.attempts}
    if result_ids != ledger_created_ids:
        raise BaselineMatrixError(
            "matrix result must retain every ledger attempt and no undeclared attempt; "
            f"omitted={sorted(ledger_created_ids - result_ids)}, "
            f"undeclared={sorted(result_ids - ledger_created_ids)}"
        )

    grouped: dict[CellKey, list[BaselineAttemptRecord]] = defaultdict(list)
    for record in result.attempts:
        attempt = record.attempt
        expected_arm = plan_arm.get(attempt.run_plan_sha256)
        if expected_arm is None or record.arm_id is not expected_arm:
            raise BaselineMatrixError("attempt arm does not match its frozen run plan")
        arm = matrix.arm(record.arm_id)
        if attempt.system_manifest_sha256 != arm.system_manifest_sha256:
            raise BaselineMatrixError("attempt system identity does not match its baseline arm")
        if attempt.suite_manifest_sha256 != suite.manifest_sha256:
            raise BaselineMatrixError("attempt suite identity differs from the matrix suite")
        slot_matches = [
            slot
            for slot in matrix.slots
            if slot.task_manifest_sha256 == attempt.task_manifest_sha256
            and slot.repeat_index == attempt.repeat_index
            and slot.seed == attempt.seed
        ]
        if len(slot_matches) != 1:
            raise BaselineMatrixError("attempt is outside the preregistered paired slots")
        terminal = ledger_terminal.get(attempt.attempt_id)
        if terminal is None or terminal.attempt_sha256 != attempt.attempt_sha256:
            raise BaselineMatrixError("result terminal attempt differs from the ledger")
        if manifest_events.get(attempt.attempt_id) != record.attempt_manifest.manifest_sha256:
            raise BaselineMatrixError("attempt manifest is missing or differs in the ledger")

        if record.execution_receipt is not None:
            expected_execution_hash = execution_events.get(attempt.attempt_id)
            if expected_execution_hash != record.execution_receipt.receipt_sha256:
                raise BaselineMatrixError("execution receipt is missing or differs in the ledger")
        elif attempt.attempt_id in execution_events:
            raise BaselineMatrixError("result omitted an execution receipt recorded in the ledger")

        if record.submission is not None:
            if submission_events.get(attempt.attempt_id) != record.submission.submission_sha256:
                raise BaselineMatrixError("submission is missing or differs in the ledger")
        elif attempt.attempt_id in submission_events:
            raise BaselineMatrixError("result omitted a submission recorded in the ledger")

        if record.scorer_receipt is not None:
            signed = record.scorer_receipt
            key = receipt_keys.get(signed.key_id)
            if key is None:
                raise BaselineMatrixError(
                    f"no trusted verification key for scorer key {signed.key_id!r}"
                )
            if len(key) < 32:
                raise BaselineMatrixError("trusted scorer verification keys must contain 32 bytes")
            try:
                signed.verify(key=key, expected_key_id=signed.key_id)
            except ValueError as exc:
                raise BaselineMatrixError(str(exc)) from exc
            receipt = signed.receipt
            if receipt.evaluator_manifest_sha256 != matrix.evaluator_manifest_sha256:
                raise BaselineMatrixError("score receipt belongs to another evaluator manifest")
            if score_events.get(attempt.attempt_id) != signed.envelope_sha256:
                raise BaselineMatrixError(
                    "signed score receipt is missing or differs in the ledger"
                )
        elif attempt.attempt_id in score_events:
            raise BaselineMatrixError(
                "result omitted a signed score receipt recorded in the ledger"
            )

        grouped[(record.arm_id, attempt.task_manifest_sha256, attempt.repeat_index)].append(record)

    expected_cells = {
        (arm_id, slot.task_manifest_sha256, slot.repeat_index)
        for arm_id in BaselineArmId
        for slot in matrix.slots
    }
    if set(grouped) != expected_cells:
        raise BaselineMatrixError(
            "matrix attempts do not cover every preregistered arm/task/repeat cell"
        )

    final: dict[CellKey, BaselineAttemptRecord] = {}
    lineages: dict[CellKey, tuple[BaselineAttemptRecord, ...]] = {}
    retry_total = 0
    for cell, records in grouped.items():
        plan = plans[cell[0]]
        first = records[0].attempt
        if first.retry_of_attempt_id is not None:
            raise BaselineMatrixError("the first attempt in a cell cannot be a retry")
        if first.attempt_id in retry_events:
            raise BaselineMatrixError("an initial attempt has an unexpected retry authorization")
        previous = records[0]
        for retry in records[1:]:
            retry_total += 1
            if previous.attempt.status is not AttemptStatus.INFRA_FAILURE:
                raise BaselineMatrixError("only an infrastructure failure can precede a retry")
            if retry.attempt.retry_of_attempt_id != previous.attempt.attempt_id:
                raise BaselineMatrixError("retry lineage is not contiguous")
            if retry_events.get(retry.attempt.attempt_id) != previous.attempt.attempt_id:
                raise BaselineMatrixError("retry is missing its evaluator authorization event")
            previous = retry
        if len(records) - 1 > plan.max_infra_retries_per_slot:
            raise BaselineMatrixError(
                "cell exceeds its preregistered infrastructure retry allowance"
            )
        if (
            records[-1].attempt.status is AttemptStatus.INFRA_FAILURE
            and len(records) - 1 != plan.max_infra_retries_per_slot
        ):
            raise BaselineMatrixError(
                "matrix stopped before exhausting an infrastructure retry allowance"
            )
        lineages[cell] = tuple(records)
        final[cell] = records[-1]

    return _AuditedAttempts(lineages=lineages, final=final, retries=retry_total)


def _arm_summary(
    arm_id: BaselineArmId,
    *,
    matrix: BaselineMatrixPlan,
    audited: _AuditedAttempts,
) -> BaselineArmSummary:
    cell_keys = [key for key in audited.final if key[0] is arm_id]
    final_records = [audited.final[key] for key in cell_keys]
    all_records = [record for key in cell_keys for record in audited.lineages[key]]
    final_statuses = Counter(record.attempt.status.value for record in final_records)
    all_statuses = Counter(record.attempt.status.value for record in all_records)
    exits = Counter(
        record.execution_receipt.exit_reason.value
        for record in all_records
        if record.execution_receipt is not None
    )
    invalid_reasons: Counter[str] = Counter()
    unscored_invalid = 0
    nonfinal_scored_invalid = 0
    valid_scores: list[EvaluationScore] = []
    for record in final_records:
        score = _score(record)
        if score is not None:
            invalid_reasons.update(reason.value for reason in score.invalid_reasons)
            if not score.invalid_reasons and score.scientific_success is not None:
                valid_scores.append(score)
            elif not score.invalid_reasons and score.scientific_success is None:
                nonfinal_scored_invalid += 1
        elif record.attempt.status is AttemptStatus.INVALID:
            unscored_invalid += 1

    successes = sum(score.scientific_success is True for score in valid_scores)
    metrics: dict[str, list[float]] = defaultdict(list)
    for score in valid_scores:
        for metric, value in score.objective_scores.items():
            metrics[metric].append(value)
    objective = tuple(
        ObjectiveMetricSummary(
            metric=metric,
            distribution=_summary(values, missing=len(final_records) - len(values)),
        )
        for metric, values in sorted(metrics.items())
    )

    costs = [
        record.execution_receipt.cost_usd
        for record in all_records
        if record.execution_receipt is not None and record.execution_receipt.cost_usd is not None
    ]
    walls = [
        record.execution_receipt.wall_time_s
        for record in all_records
        if record.execution_receipt is not None
    ]
    input_tokens = [
        record.execution_receipt.input_tokens
        for record in all_records
        if record.execution_receipt is not None
        and record.execution_receipt.input_tokens is not None
    ]
    output_tokens = [
        record.execution_receipt.output_tokens
        for record in all_records
        if record.execution_receipt is not None
        and record.execution_receipt.output_tokens is not None
    ]
    contaminated_submissions = [
        record.submission
        for record in all_records
        if record.submission is not None and record.submission.declared_contamination
    ]
    valid_count = len(valid_scores)
    return BaselineArmSummary(
        arm_id=arm_id,
        planned_cells=len(cell_keys),
        retained_attempts=len(all_records),
        infrastructure_retries=sum(
            record.attempt.retry_of_attempt_id is not None for record in all_records
        ),
        final_status_counts=dict(sorted(final_statuses.items())),
        all_attempt_status_counts=dict(sorted(all_statuses.items())),
        execution_exit_reason_counts=dict(sorted(exits.items())),
        invalid_reason_counts=dict(sorted(invalid_reasons.items())),
        unscored_invalid_attempts=unscored_invalid,
        nonfinal_scored_invalid_attempts=nonfinal_scored_invalid,
        scientific_valid_cells=valid_count,
        scientific_successes=successes,
        pass_at_1=successes / len(cell_keys),
        scientific_success_rate=successes / valid_count if valid_count else None,
        objective_metrics=objective,
        observed_cost_usd=_summary(costs, missing=len(all_records) - len(costs)),
        wall_time_s=_summary(walls, missing=len(all_records) - len(walls)),
        input_tokens=_summary(input_tokens, missing=len(all_records) - len(input_tokens)),
        output_tokens=_summary(output_tokens, missing=len(all_records) - len(output_tokens)),
        human_interventions=sum(record.attempt.intervention_count for record in all_records),
        submissions_declaring_contamination=len(contaminated_submissions),
        contamination_declarations=sum(
            len(submission.declared_contamination) for submission in contaminated_submissions
        ),
    )


def _lineage_cost(lineage: Sequence[BaselineAttemptRecord]) -> float | None:
    values: list[float] = []
    for record in lineage:
        if record.execution_receipt is None or record.execution_receipt.cost_usd is None:
            return None
        values.append(record.execution_receipt.cost_usd)
    return sum(values)


def _objective_effect(
    metric: str,
    *,
    comparator: BaselineArmId,
    matrix: BaselineMatrixPlan,
    audited: _AuditedAttempts,
) -> ObjectivePairedEffect:
    differences: dict[str, list[float]] = defaultdict(list)
    wins = losses = ties = 0
    valid = 0
    for slot in matrix.slots:
        candidate = audited.final[
            (BaselineArmId.ALETHEIA_FULL_K2, slot.task_manifest_sha256, slot.repeat_index)
        ]
        baseline = audited.final[(comparator, slot.task_manifest_sha256, slot.repeat_index)]
        candidate_score = _score(candidate)
        baseline_score = _score(baseline)
        if (
            candidate_score is None
            or baseline_score is None
            or candidate_score.invalid_reasons
            or baseline_score.invalid_reasons
            or candidate_score.scientific_success is None
            or baseline_score.scientific_success is None
            or metric not in candidate_score.objective_scores
            or metric not in baseline_score.objective_scores
        ):
            continue
        difference = (
            candidate_score.objective_scores[metric] - baseline_score.objective_scores[metric]
        )
        differences[slot.task_manifest_sha256].append(difference)
        valid += 1
        if difference > 0:
            wins += 1
        elif difference < 0:
            losses += 1
        else:
            ties += 1
    flat = [value for values in differences.values() for value in values]
    if not flat:
        return ObjectivePairedEffect(
            metric=metric,
            valid_pairs=0,
            excluded_pairs=len(matrix.slots),
            candidate_wins=0,
            comparator_wins=0,
            ties=0,
        )
    low, high = _hierarchical_ci(
        differences,
        confidence_level=matrix.analysis.confidence_level,
        resamples=matrix.analysis.bootstrap_resamples,
        seed=_comparison_seed(matrix.analysis.bootstrap_seed, comparator, f"objective:{metric}"),
    )
    return ObjectivePairedEffect(
        metric=metric,
        valid_pairs=valid,
        excluded_pairs=len(matrix.slots) - valid,
        candidate_wins=wins,
        comparator_wins=losses,
        ties=ties,
        mean_difference=sum(flat) / len(flat),
        median_difference=_quantile(flat, 0.5),
        confidence_interval_low=low,
        confidence_interval_high=high,
    )


def _pairwise_comparison(
    comparator: BaselineArmId,
    *,
    matrix: BaselineMatrixPlan,
    audited: _AuditedAttempts,
) -> BaselinePairwiseComparison:
    scientific_differences: dict[str, list[float]] = defaultdict(list)
    operational_differences: dict[str, list[float]] = defaultdict(list)
    candidate_successes = comparator_successes = 0
    candidate_operational = comparator_operational = 0
    wins = losses = ties = 0
    valid = 0
    cost_differences: list[float] = []
    for slot in matrix.slots:
        candidate_key = (
            BaselineArmId.ALETHEIA_FULL_K2,
            slot.task_manifest_sha256,
            slot.repeat_index,
        )
        comparator_key = (comparator, slot.task_manifest_sha256, slot.repeat_index)
        candidate = audited.final[candidate_key]
        baseline = audited.final[comparator_key]
        candidate_score = _score(candidate)
        baseline_score = _score(baseline)
        candidate_value = int(
            candidate_score is not None
            and not candidate_score.invalid_reasons
            and candidate_score.scientific_success is True
        )
        comparator_value = int(
            baseline_score is not None
            and not baseline_score.invalid_reasons
            and baseline_score.scientific_success is True
        )
        candidate_operational += candidate_value
        comparator_operational += comparator_value
        operational_differences[slot.task_manifest_sha256].append(
            float(candidate_value - comparator_value)
        )

        candidate_valid = (
            candidate_score is not None
            and not candidate_score.invalid_reasons
            and candidate_score.scientific_success is not None
        )
        comparator_valid = (
            baseline_score is not None
            and not baseline_score.invalid_reasons
            and baseline_score.scientific_success is not None
        )
        if candidate_valid and comparator_valid:
            candidate_scientific = int(candidate_score.scientific_success is True)
            comparator_scientific = int(baseline_score.scientific_success is True)
            difference = candidate_scientific - comparator_scientific
            scientific_differences[slot.task_manifest_sha256].append(float(difference))
            candidate_successes += candidate_scientific
            comparator_successes += comparator_scientific
            valid += 1
            if difference > 0:
                wins += 1
            elif difference < 0:
                losses += 1
            else:
                ties += 1

        candidate_cost = _lineage_cost(audited.lineages[candidate_key])
        comparator_cost = _lineage_cost(audited.lineages[comparator_key])
        if candidate_cost is not None and comparator_cost is not None:
            cost_differences.append(candidate_cost - comparator_cost)

    planned = len(matrix.slots)
    operational_flat = [value for values in operational_differences.values() for value in values]
    operational_low, operational_high = _hierarchical_ci(
        operational_differences,
        confidence_level=matrix.analysis.confidence_level,
        resamples=matrix.analysis.bootstrap_resamples,
        seed=_comparison_seed(matrix.analysis.bootstrap_seed, comparator, "operational"),
    )
    if scientific_differences:
        scientific_flat = [value for values in scientific_differences.values() for value in values]
        scientific_low, scientific_high = _hierarchical_ci(
            scientific_differences,
            confidence_level=matrix.analysis.confidence_level,
            resamples=matrix.analysis.bootstrap_resamples,
            seed=_comparison_seed(matrix.analysis.bootstrap_seed, comparator, "scientific"),
        )
        scientific_difference = sum(scientific_flat) / len(scientific_flat)
    else:
        scientific_low = scientific_high = scientific_difference = None

    mismatch_dimensions = tuple(
        sorted(
            (
                disclosure.dimension
                for disclosure in matrix.mismatch_disclosures
                if disclosure.comparator_arm is comparator
            ),
            key=lambda dimension: dimension.value,
        )
    )
    secondary = tuple(
        _objective_effect(metric, comparator=comparator, matrix=matrix, audited=audited)
        for metric in matrix.analysis.secondary_objective_metrics
    )
    return BaselinePairwiseComparison(
        comparator_arm=comparator,
        planned_pairs=planned,
        valid_scientific_pairs=valid,
        excluded_scientific_pairs=planned - valid,
        candidate_wins=wins,
        comparator_wins=losses,
        ties=ties,
        candidate_scientific_success_rate=(candidate_successes / valid if valid else None),
        comparator_scientific_success_rate=(comparator_successes / valid if valid else None),
        scientific_risk_difference=scientific_difference,
        scientific_ci_low=scientific_low,
        scientific_ci_high=scientific_high,
        raw_exact_p_value=_exact_two_sided_sign_p(wins, losses) if valid else None,
        candidate_operational_pass_at_1=candidate_operational / planned,
        comparator_operational_pass_at_1=comparator_operational / planned,
        operational_risk_difference=sum(operational_flat) / len(operational_flat),
        operational_ci_low=operational_low,
        operational_ci_high=operational_high,
        cost_pairs=len(cost_differences),
        excluded_cost_pairs=planned - len(cost_differences),
        mean_paired_cost_difference_usd=(
            sum(cost_differences) / len(cost_differences) if cost_differences else None
        ),
        comparability_mismatches=mismatch_dimensions,
        unconditional_claim_allowed=not mismatch_dimensions,
        secondary_objective_effects=secondary,
    )


def aggregate_baseline_matrix(
    *,
    matrix: BaselineMatrixPlan,
    suite: EvaluationSuite,
    result: BaselineMatrixResult,
    ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
    generated_at: datetime | None = None,
) -> BaselineAggregateReport:
    """Verify and aggregate one complete matrix; partial or cherry-picked results fail closed."""

    audited = _verify_result_and_attempts(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys=receipt_keys,
    )
    arm_summaries = tuple(
        _arm_summary(arm_id, matrix=matrix, audited=audited) for arm_id in BaselineArmId
    )
    comparisons = [
        _pairwise_comparison(comparator, matrix=matrix, audited=audited)
        for comparator in matrix.analysis.primary_comparators
    ]
    adjusted = _holm_adjust(
        {comparison.comparator_arm: comparison.raw_exact_p_value for comparison in comparisons}
    )
    corrected = tuple(
        comparison.model_copy(update={"holm_adjusted_p_value": adjusted[comparison.comparator_arm]})
        for comparison in comparisons
    )
    planned_cells = len(matrix.slots) * len(tuple(BaselineArmId))
    return BaselineAggregateReport(
        matrix_manifest_sha256=matrix.manifest_sha256,
        result_sha256=result.result_sha256,
        suite_manifest_sha256=suite.manifest_sha256,
        generated_at=generated_at or datetime.now(timezone.utc),
        confidence_level=matrix.analysis.confidence_level,
        ledger=result.ledger,
        selection_audit=NoBestOfNAudit(
            planned_cells=planned_cells,
            observed_cells=len(audited.final),
            retained_attempts=len(result.attempts),
            authorized_infrastructure_retries=audited.retries,
        ),
        arms=arm_summaries,
        comparisons=corrected,
    )
