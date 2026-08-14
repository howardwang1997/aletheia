"""Pre-registered, paired baseline and ablation matrices for Frontier Gate evaluation.

The matrix is evaluator-owned.  It binds four exact system treatments to the same frozen suite,
task/repeat seeds, evaluator identity, and analysis policy before any attempt starts.  Execution is
delegated to :class:`~aletheia.evals.runner.IndependentEvaluationRunner`, so every cell retains the
existing hidden-asset boundary, append-only attempt lineage, execution receipt, and signed score.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.runner import EvaluationOutcome, IndependentEvaluationRunner
from aletheia.evals.schemas import (
    AttemptStatus,
    EvaluationAttempt,
    EvaluationAttemptManifest,
    EvaluationAttemptSlot,
    EvaluationExecutionReceipt,
    EvaluationRunPlan,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    FrozenModel,
    SignedScorerReceipt,
    content_sha256,
)


class BaselineMatrixError(RuntimeError):
    """A baseline matrix cannot be executed or audited without changing its contract."""


class BaselineArmId(str, Enum):
    DIRECT_MODEL = "direct_model"
    GENERIC_AGENT = "generic_agent"
    ALETHEIA_NO_K2 = "aletheia_no_k2"
    ALETHEIA_FULL_K2 = "aletheia_full_k2"


class AgentScaffold(str, Enum):
    DIRECT = "direct"
    GENERIC = "generic_agent"
    ALETHEIA = "aletheia"


class MatrixPhase(str, Enum):
    VALIDATION = "validation"
    TEST = "test"


class ComparabilityDimension(str, Enum):
    BUDGET = "budget"
    TOOLS = "tools"
    WALL_TIME = "wall_time"


class BaselineArm(FrozenModel):
    """One frozen system treatment; labels are checked against actual treatment switches."""

    schema_version: Literal[1] = 1
    arm_id: BaselineArmId
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_model_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_scaffold: AgentScaffold
    campaign_learning_enabled: bool
    k2_enabled: bool
    prompt_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wall_time_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_names: tuple[str, ...] = ()
    description: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _treatment_matches_arm_label(self) -> "BaselineArm":
        expected = {
            BaselineArmId.DIRECT_MODEL: (AgentScaffold.DIRECT, False, False),
            BaselineArmId.GENERIC_AGENT: (AgentScaffold.GENERIC, False, False),
            BaselineArmId.ALETHEIA_NO_K2: (AgentScaffold.ALETHEIA, False, False),
            BaselineArmId.ALETHEIA_FULL_K2: (AgentScaffold.ALETHEIA, True, True),
        }[self.arm_id]
        actual = (
            self.agent_scaffold,
            self.campaign_learning_enabled,
            self.k2_enabled,
        )
        if actual != expected:
            raise ValueError(
                f"arm {self.arm_id.value} requires scaffold/campaign/K2={expected}, got {actual}"
            )
        if self.k2_enabled and not self.campaign_learning_enabled:
            raise ValueError("K2 cannot be enabled while campaign learning is disabled")
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("arm tool names must be unique")
        if any(not name.strip() for name in self.tool_names):
            raise ValueError("arm tool names cannot be blank")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class BaselineMismatchDisclosure(FrozenModel):
    """A non-model mismatch relative to full K2 that forbids an unconditional superiority claim."""

    schema_version: Literal[1] = 1
    comparator_arm: BaselineArmId
    dimension: ComparabilityDimension
    rationale: str = Field(min_length=1, max_length=2048)
    mitigation: str = Field(min_length=1, max_length=2048)
    interpretation: Literal["conditional_only"] = "conditional_only"

    @model_validator(mode="after")
    def _compares_to_full_system(self) -> "BaselineMismatchDisclosure":
        if self.comparator_arm is BaselineArmId.ALETHEIA_FULL_K2:
            raise ValueError("full K2 cannot be its own mismatch comparator")
        return self


class BaselineAnalysisPolicy(FrozenModel):
    """Analysis choices frozen before test access; no result-dependent endpoint selection."""

    schema_version: Literal[1] = 1
    primary_endpoint: Literal["scientific_success"] = "scientific_success"
    secondary_objective_metrics: tuple[str, ...] = ()
    primary_comparators: tuple[BaselineArmId, ...] = (
        BaselineArmId.DIRECT_MODEL,
        BaselineArmId.GENERIC_AGENT,
        BaselineArmId.ALETHEIA_NO_K2,
    )
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0)
    bootstrap_resamples: int = Field(default=10_000, ge=100, le=1_000_000)
    bootstrap_seed: int
    resampling_scheme: Literal["paired_hierarchical_task_repeat_v1"] = (
        "paired_hierarchical_task_repeat_v1"
    )
    missing_cell_policy: Literal["fail_closed"] = "fail_closed"
    invalid_pair_policy: Literal["exclude_from_scientific_effect_and_report"] = (
        "exclude_from_scientific_effect_and_report"
    )
    attempt_selection_policy: Literal["all_preregistered_slots_no_best_of_n"] = (
        "all_preregistered_slots_no_best_of_n"
    )
    multiplicity_correction: Literal["holm"] = "holm"

    @model_validator(mode="after")
    def _endpoints_are_predeclared_once(self) -> "BaselineAnalysisPolicy":
        expected = {
            BaselineArmId.DIRECT_MODEL,
            BaselineArmId.GENERIC_AGENT,
            BaselineArmId.ALETHEIA_NO_K2,
        }
        if len(self.primary_comparators) != len(set(self.primary_comparators)):
            raise ValueError("primary baseline comparators must be unique")
        if set(self.primary_comparators) != expected:
            raise ValueError(
                "primary comparisons must include full K2 versus all other four-arm baselines"
            )
        if len(self.secondary_objective_metrics) != len(set(self.secondary_objective_metrics)):
            raise ValueError("secondary objective metrics must be unique")
        if any(not metric.strip() for metric in self.secondary_objective_metrics):
            raise ValueError("secondary objective metric names cannot be blank")
        return self


class BaselineMatrixPlan(FrozenModel):
    """Content-addressed four-arm preregistration shared by every paired run plan."""

    schema_version: Literal[1] = 1
    matrix_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,79}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: MatrixPhase
    parent_validation_matrix_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    arms: tuple[BaselineArm, ...] = Field(min_length=4, max_length=4)
    slots: tuple[EvaluationAttemptSlot, ...] = Field(min_length=1)
    max_infra_retries_per_slot: int = Field(default=1, ge=0, le=10)
    block_randomization_seed: int
    block_ordering: Literal["sha256_within_task_repeat_v1"] = "sha256_within_task_repeat_v1"
    analysis: BaselineAnalysisPolicy
    mismatch_disclosures: tuple[BaselineMismatchDisclosure, ...] = ()
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _matrix_is_complete_paired_and_comparable(self) -> "BaselineMatrixPlan":
        canonical_arm_order = tuple(BaselineArmId)
        arm_ids = tuple(arm.arm_id for arm in self.arms)
        if arm_ids != canonical_arm_order:
            raise ValueError(
                "baseline arms must appear once in canonical order: "
                + ", ".join(arm.value for arm in canonical_arm_order)
            )
        system_hashes = [arm.system_manifest_sha256 for arm in self.arms]
        if len(system_hashes) != len(set(system_hashes)):
            raise ValueError("each baseline arm must bind a distinct system manifest")
        if len({arm.base_model_manifest_sha256 for arm in self.arms}) != 1:
            raise ValueError("all baseline arms must use the same frozen base model identity")

        identities = [(slot.task_manifest_sha256, slot.repeat_index) for slot in self.slots]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "each task/repeat pair must appear exactly once in the shared slot block"
            )
        by_task: dict[str, list[EvaluationAttemptSlot]] = {}
        for slot in self.slots:
            by_task.setdefault(slot.task_manifest_sha256, []).append(slot)
        minimum_repeats = 3 if self.phase is MatrixPhase.VALIDATION else 5
        for task_hash, slots in by_task.items():
            repeats = sorted(slot.repeat_index for slot in slots)
            if repeats != list(range(len(slots))):
                raise ValueError(
                    f"task {task_hash} repeat indices must be contiguous and start at zero"
                )
            if len(slots) < minimum_repeats:
                raise ValueError(
                    f"{self.phase.value} matrices require at least {minimum_repeats} repeats per task"
                )
            seeds = [slot.seed for slot in slots]
            if len(seeds) != len(set(seeds)):
                raise ValueError("paired repeats for one task must use unique seeds")

        if self.phase is MatrixPhase.TEST and self.parent_validation_matrix_sha256 is None:
            raise ValueError("test preregistration must reference its frozen validation matrix")
        if (
            self.phase is MatrixPhase.VALIDATION
            and self.parent_validation_matrix_sha256 is not None
        ):
            raise ValueError("validation matrices cannot claim a parent validation matrix")

        arms = {arm.arm_id: arm for arm in self.arms}
        full = arms[BaselineArmId.ALETHEIA_FULL_K2]
        needed: set[tuple[BaselineArmId, ComparabilityDimension]] = set()
        identity_fields = {
            ComparabilityDimension.BUDGET: "budget_policy_sha256",
            ComparabilityDimension.TOOLS: "tool_policy_sha256",
            ComparabilityDimension.WALL_TIME: "wall_time_policy_sha256",
        }
        for comparator_id in self.analysis.primary_comparators:
            comparator = arms[comparator_id]
            for dimension, field_name in identity_fields.items():
                if getattr(comparator, field_name) != getattr(full, field_name):
                    needed.add((comparator_id, dimension))

        declared = {
            (disclosure.comparator_arm, disclosure.dimension)
            for disclosure in self.mismatch_disclosures
        }
        if len(declared) != len(self.mismatch_disclosures):
            raise ValueError("a comparability mismatch may be disclosed only once")
        if declared != needed:
            missing = sorted(
                f"{arm.value}:{dimension.value}" for arm, dimension in needed - declared
            )
            extra = sorted(f"{arm.value}:{dimension.value}" for arm, dimension in declared - needed)
            raise ValueError(
                f"mismatch disclosures must exactly match frozen identities; missing={missing}, extra={extra}"
            )

        policies: dict[str, tuple[str, ...]] = {}
        for arm in self.arms:
            previous = policies.setdefault(arm.tool_policy_sha256, arm.tool_names)
            if previous != arm.tool_names:
                raise ValueError("one tool policy hash cannot describe conflicting tool-name sets")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    def arm(self, arm_id: BaselineArmId) -> BaselineArm:
        return next(arm for arm in self.arms if arm.arm_id is arm_id)


class BaselineScheduleCell(FrozenModel):
    schema_version: Literal[1] = 1
    block_index: int = Field(ge=0)
    position_in_block: int = Field(ge=0, le=3)
    arm_id: BaselineArmId
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_index: int = Field(ge=0)
    seed: int


class BaselineArmRunPlan(FrozenModel):
    schema_version: Literal[1] = 1
    arm_id: BaselineArmId
    run_plan: EvaluationRunPlan


class BaselineAttemptRecord(FrozenModel):
    """One retained attempt, including failed infrastructure attempts in a retry lineage."""

    schema_version: Literal[1] = 1
    arm_id: BaselineArmId
    attempt: EvaluationAttempt
    attempt_manifest: EvaluationAttemptManifest
    execution_receipt: EvaluationExecutionReceipt | None = None
    submission: EvaluationSubmission | None = None
    scorer_receipt: SignedScorerReceipt | None = None
    detail: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _receipts_bind_one_terminal_attempt(self) -> "BaselineAttemptRecord":
        terminal = {
            AttemptStatus.COMPLETED,
            AttemptStatus.SCIENTIFIC_FAILURE,
            AttemptStatus.INVALID,
            AttemptStatus.INFRA_FAILURE,
            AttemptStatus.TIMEOUT,
        }
        if self.attempt.status not in terminal:
            raise ValueError("baseline attempt records must contain a terminal attempt")
        created = self.attempt_manifest.attempt
        stable_fields = (
            "attempt_id",
            "suite_manifest_sha256",
            "run_plan_sha256",
            "task_manifest_sha256",
            "system_manifest_sha256",
            "repeat_index",
            "seed",
            "intervention_count",
            "retry_of_attempt_id",
            "retry_reason",
        )
        if any(getattr(created, field) != getattr(self.attempt, field) for field in stable_fields):
            raise ValueError("attempt identity differs from its frozen attempt manifest")
        if self.execution_receipt is not None:
            if self.execution_receipt.attempt_id != self.attempt.attempt_id:
                raise ValueError("execution receipt belongs to another attempt")
            if (
                self.execution_receipt.attempt_manifest_sha256
                != self.attempt_manifest.manifest_sha256
            ):
                raise ValueError("execution receipt does not bind the frozen attempt manifest")
        if self.submission is not None:
            if self.submission.attempt_id != self.attempt.attempt_id:
                raise ValueError("submission belongs to another attempt")
            if self.submission.system_manifest_sha256 != self.attempt.system_manifest_sha256:
                raise ValueError("submission system identity differs from the attempt")
        if self.scorer_receipt is not None:
            if self.execution_receipt is None or self.submission is None:
                raise ValueError("a scorer receipt requires its execution receipt and submission")
            receipt = self.scorer_receipt.receipt
            receipt.verify_attempt(self.attempt)
            receipt.verify_submission(self.submission)
            if receipt.attempt_manifest_sha256 != self.attempt_manifest.manifest_sha256:
                raise ValueError("scorer receipt does not bind the attempt manifest")
            if receipt.execution_receipt_sha256 != self.execution_receipt.receipt_sha256:
                raise ValueError("scorer receipt does not bind the execution receipt")
            expected_status = (
                AttemptStatus.INVALID
                if receipt.score.invalid_reasons
                else AttemptStatus.COMPLETED
                if receipt.score.scientific_success is True
                else AttemptStatus.SCIENTIFIC_FAILURE
                if receipt.score.scientific_success is False
                else AttemptStatus.INVALID
            )
            if self.attempt.status is not expected_status:
                raise ValueError("terminal attempt status disagrees with its signed score")
        elif self.attempt.status in {AttemptStatus.COMPLETED, AttemptStatus.SCIENTIFIC_FAILURE}:
            raise ValueError("scientific terminal states require a signed scorer receipt")
        return self

    @classmethod
    def from_outcome(
        cls, *, arm_id: BaselineArmId, outcome: EvaluationOutcome
    ) -> "BaselineAttemptRecord":
        return cls(
            arm_id=arm_id,
            attempt=outcome.attempt,
            attempt_manifest=outcome.attempt_manifest,
            execution_receipt=outcome.execution_receipt,
            submission=outcome.submission,
            scorer_receipt=outcome.scorer_receipt,
            detail=outcome.detail,
        )


class EvaluationLedgerReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    path: str
    events: int = Field(ge=0)
    head_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BaselineMatrixResult(FrozenModel):
    """Serializable execution output.  Statistical auditing still reconciles it to the ledger."""

    schema_version: Literal[1] = 1
    matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schedule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_plans: tuple[BaselineArmRunPlan, ...] = Field(min_length=4, max_length=4)
    attempts: tuple[BaselineAttemptRecord, ...]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    ledger: EvaluationLedgerReceipt

    @model_validator(mode="after")
    def _result_has_canonical_unique_identities(self) -> "BaselineMatrixResult":
        if self.ended_at < self.started_at:
            raise ValueError("matrix execution ended before it started")
        arm_ids = tuple(item.arm_id for item in self.run_plans)
        if arm_ids != tuple(BaselineArmId):
            raise ValueError("matrix results must contain four run plans in canonical arm order")
        attempt_ids = [record.attempt.attempt_id for record in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("an attempt may appear only once in matrix results")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


def load_baseline_attempt_record(
    *,
    evaluator_root: Path,
    ledger: EvaluationLedger,
    arm_id: BaselineArmId,
    attempt_id: str,
) -> BaselineAttemptRecord:
    """Rebuild one terminal record after an evaluator-process restart.

    The append-only ledger supplies the terminal state.  Content-addressed artifacts are loaded
    only from the evaluator-owned attempt directory and the unique sealed submission inbox.
    Final aggregation still performs the complete ledger and signature audit.
    """

    attempt = ledger.terminal_attempt(attempt_id)
    if attempt is None:
        raise BaselineMatrixError(
            f"cannot resume matrix with nonterminal attempt {attempt_id!r}; evaluator adjudication is required"
        )
    root = Path(evaluator_root).expanduser().resolve(strict=False)
    workspace = root / "evaluator_attempts" / attempt_id
    if workspace.is_symlink():
        raise BaselineMatrixError(f"attempt {attempt_id!r} evaluator workspace is a symlink")
    manifest_path = workspace / "attempt_manifest.v1.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BaselineMatrixError(f"attempt {attempt_id!r} has no regular frozen manifest")
    attempt_manifest = EvaluationAttemptManifest.model_validate_json(manifest_path.read_bytes())

    execution_path = workspace / "execution_receipt.v1.json"
    execution_receipt = (
        EvaluationExecutionReceipt.model_validate_json(execution_path.read_bytes())
        if execution_path.is_file() and not execution_path.is_symlink()
        else None
    )
    scorer_path = workspace / "scorer_receipt.signed.v1.json"
    scorer_receipt = (
        SignedScorerReceipt.model_validate_json(scorer_path.read_bytes())
        if scorer_path.is_file() and not scorer_path.is_symlink()
        else None
    )

    inbox_parent = root / "submission_inbox"
    inboxes = [
        path
        for path in inbox_parent.glob(f"{attempt_id}-*")
        if path.is_dir() and not path.is_symlink()
    ]
    if len(inboxes) != 1:
        raise BaselineMatrixError(
            f"attempt {attempt_id!r} must have exactly one sealed submission inbox"
        )
    submission_path = inboxes[0] / "submission.json"
    submission = (
        EvaluationSubmission.model_validate_json(submission_path.read_bytes())
        if submission_path.is_file() and not submission_path.is_symlink()
        else None
    )

    detail: str | None = None
    for failure_name in (
        "infrastructure_failure.json",
        "protocol_failure.json",
        "scorer_failure.json",
    ):
        failure_path = workspace / failure_name
        if not failure_path.is_file() or failure_path.is_symlink():
            continue
        try:
            raw_detail = json.loads(failure_path.read_text(encoding="utf-8")).get("detail")
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineMatrixError(
                f"attempt {attempt_id!r} has an unreadable evaluator failure record"
            ) from exc
        if raw_detail is not None:
            detail = str(raw_detail)[:1024]
        break

    return BaselineAttemptRecord(
        arm_id=arm_id,
        attempt=attempt,
        attempt_manifest=attempt_manifest,
        execution_receipt=execution_receipt,
        submission=submission,
        scorer_receipt=scorer_receipt,
        detail=detail,
    )


def validate_matrix_suite(matrix: BaselineMatrixPlan, suite: EvaluationSuite) -> None:
    if matrix.suite_manifest_sha256 != suite.manifest_sha256:
        raise BaselineMatrixError("baseline matrix is not bound to this frozen suite")
    planned_tasks = {slot.task_manifest_sha256 for slot in matrix.slots}
    suite_tasks = set(suite.task_manifest_sha256s)
    if planned_tasks != suite_tasks:
        raise BaselineMatrixError(
            "baseline matrix slots must cover every suite task and no task outside it; "
            f"missing={sorted(suite_tasks - planned_tasks)}, extra={sorted(planned_tasks - suite_tasks)}"
        )


def build_baseline_run_plans(
    matrix: BaselineMatrixPlan, suite: EvaluationSuite
) -> tuple[BaselineArmRunPlan, ...]:
    validate_matrix_suite(matrix, suite)
    return tuple(
        BaselineArmRunPlan(
            arm_id=arm.arm_id,
            run_plan=EvaluationRunPlan(
                plan_id=f"{matrix.matrix_id}-{arm.arm_id.value}",
                suite_manifest_sha256=matrix.suite_manifest_sha256,
                system_manifest_sha256=arm.system_manifest_sha256,
                evaluator_manifest_sha256=matrix.evaluator_manifest_sha256,
                slots=matrix.slots,
                max_infra_retries_per_slot=matrix.max_infra_retries_per_slot,
            ),
        )
        for arm in matrix.arms
    )


def baseline_execution_schedule(matrix: BaselineMatrixPlan) -> tuple[BaselineScheduleCell, ...]:
    """Derive a deterministic, blocked arm order without storing mutable scheduler state."""

    schedule: list[BaselineScheduleCell] = []
    ordered_slots = sorted(
        matrix.slots, key=lambda slot: (slot.task_manifest_sha256, slot.repeat_index)
    )
    for block_index, slot in enumerate(ordered_slots):
        arm_order = sorted(
            BaselineArmId,
            key=lambda arm: hashlib.sha256(
                (
                    f"aletheia-baseline-order-v1\0{matrix.block_randomization_seed}\0"
                    f"{slot.task_manifest_sha256}\0{slot.repeat_index}\0{slot.seed}\0{arm.value}"
                ).encode("utf-8")
            ).digest(),
        )
        for position, arm_id in enumerate(arm_order):
            schedule.append(
                BaselineScheduleCell(
                    block_index=block_index,
                    position_in_block=position,
                    arm_id=arm_id,
                    task_manifest_sha256=slot.task_manifest_sha256,
                    repeat_index=slot.repeat_index,
                    seed=slot.seed,
                )
            )
    return tuple(schedule)


def baseline_schedule_sha256(matrix: BaselineMatrixPlan) -> str:
    return content_sha256(
        {
            "schema_name": "aletheia.baseline_schedule",
            "cells": [cell.model_dump(mode="json") for cell in baseline_execution_schedule(matrix)],
        }
    )


class BaselineMatrixRunner:
    """Execute every preregistered cell once, with retries only for retained infra failures."""

    def __init__(
        self,
        *,
        matrix: BaselineMatrixPlan,
        suite: EvaluationSuite,
        tasks: Mapping[str, EvaluationTask],
        runners: Mapping[BaselineArmId, IndependentEvaluationRunner],
    ) -> None:
        validate_matrix_suite(matrix, suite)
        task_map = dict(tasks)
        if set(task_map) != set(suite.task_manifest_sha256s):
            raise BaselineMatrixError("task map must contain every frozen suite task exactly once")
        for task_hash, task in task_map.items():
            if task_hash != task.manifest_sha256:
                raise BaselineMatrixError("task map key does not match the task manifest")

        runner_map = dict(runners)
        if set(runner_map) != set(BaselineArmId):
            raise BaselineMatrixError(
                "matrix execution requires exactly one runner per baseline arm"
            )
        roots = {runner.root for runner in runner_map.values()}
        ledger_paths = {runner.ledger.path for runner in runner_map.values()}
        if len(roots) != 1 or len(ledger_paths) != 1:
            raise BaselineMatrixError("all baseline arms must share one evaluator root and ledger")
        for runner in runner_map.values():
            if not runner.formal:
                raise BaselineMatrixError("baseline matrix execution requires formal evaluators")
            if runner.evaluator_manifest_sha256 != matrix.evaluator_manifest_sha256:
                raise BaselineMatrixError(
                    "arm runner is not bound to the matrix evaluator manifest"
                )

        self.matrix = matrix
        self.suite = suite
        self.tasks = task_map
        self.runners = runner_map
        self.run_plans = build_baseline_run_plans(matrix, suite)
        self._plans_by_arm = {item.arm_id: item.run_plan for item in self.run_plans}

    @property
    def ledger(self) -> EvaluationLedger:
        return next(iter(self.runners.values())).ledger

    @property
    def evaluator_root(self) -> Path:
        return next(iter(self.runners.values())).root

    def _existing_records(self) -> tuple[BaselineAttemptRecord, ...]:
        plan_arm = {item.run_plan.manifest_sha256: item.arm_id for item in self.run_plans}
        created: list[tuple[BaselineArmId, str]] = []
        latest_status: dict[str, AttemptStatus] = {}
        for event in self.ledger.events():
            if event.event_type != "attempt_state":
                continue
            attempt = EvaluationAttempt.model_validate(event.payload["attempt"])
            arm_id = plan_arm.get(attempt.run_plan_sha256)
            if arm_id is None:
                continue
            latest_status[attempt.attempt_id] = attempt.status
            if attempt.status is AttemptStatus.CREATED:
                created.append((arm_id, attempt.attempt_id))
        nonterminal = [
            attempt_id
            for _arm_id, attempt_id in created
            if latest_status.get(attempt_id)
            not in {
                AttemptStatus.COMPLETED,
                AttemptStatus.SCIENTIFIC_FAILURE,
                AttemptStatus.INVALID,
                AttemptStatus.INFRA_FAILURE,
                AttemptStatus.TIMEOUT,
            }
        ]
        if nonterminal:
            raise BaselineMatrixError(
                "matrix ledger contains nonterminal attempts that require evaluator adjudication: "
                + ", ".join(nonterminal)
            )
        records = tuple(
            load_baseline_attempt_record(
                evaluator_root=self.evaluator_root,
                ledger=self.ledger,
                arm_id=arm_id,
                attempt_id=attempt_id,
            )
            for arm_id, attempt_id in created
        )
        self._validate_resume_prefix(records)
        return records

    def _validate_resume_prefix(self, records: tuple[BaselineAttemptRecord, ...]) -> None:
        plan_by_arm = {item.arm_id: item.run_plan for item in self.run_plans}
        expected_order = [
            (cell.arm_id, cell.task_manifest_sha256, cell.repeat_index)
            for cell in baseline_execution_schedule(self.matrix)
        ]
        grouped: dict[tuple[BaselineArmId, str, int], list[BaselineAttemptRecord]] = {}
        observed_order: list[tuple[BaselineArmId, str, int]] = []
        for record in records:
            attempt = record.attempt
            cell = (record.arm_id, attempt.task_manifest_sha256, attempt.repeat_index)
            if cell not in expected_order:
                raise BaselineMatrixError("existing ledger attempt is outside the frozen schedule")
            plan = plan_by_arm[record.arm_id]
            arm = self.matrix.arm(record.arm_id)
            if (
                attempt.run_plan_sha256 != plan.manifest_sha256
                or attempt.system_manifest_sha256 != arm.system_manifest_sha256
                or attempt.suite_manifest_sha256 != self.suite.manifest_sha256
            ):
                raise BaselineMatrixError("existing attempt identity differs from its matrix cell")
            slot = next(
                item
                for item in self.matrix.slots
                if item.task_manifest_sha256 == attempt.task_manifest_sha256
                and item.repeat_index == attempt.repeat_index
            )
            if attempt.seed != slot.seed:
                raise BaselineMatrixError("existing attempt changed its paired seed")
            if cell not in grouped:
                grouped[cell] = []
                observed_order.append(cell)
            grouped[cell].append(record)
        if observed_order != expected_order[: len(observed_order)]:
            raise BaselineMatrixError(
                "existing attempts are not a contiguous frozen-schedule prefix"
            )
        for cell, lineage in grouped.items():
            plan = plan_by_arm[cell[0]]
            if lineage[0].attempt.retry_of_attempt_id is not None:
                raise BaselineMatrixError("existing cell begins with a retry")
            previous = lineage[0]
            for retry in lineage[1:]:
                if (
                    previous.attempt.status is not AttemptStatus.INFRA_FAILURE
                    or retry.attempt.retry_of_attempt_id != previous.attempt.attempt_id
                ):
                    raise BaselineMatrixError("existing retry lineage is invalid")
                previous = retry
            if len(lineage) - 1 > plan.max_infra_retries_per_slot:
                raise BaselineMatrixError("existing cell exceeds its frozen retry allowance")

    def run(self) -> BaselineMatrixResult:
        records = list(self._existing_records())
        started_candidates = [
            record.attempt.started_at for record in records if record.attempt.started_at is not None
        ]
        started_at = min(started_candidates) if started_candidates else datetime.now(timezone.utc)
        grouped: dict[tuple[BaselineArmId, str, int], list[BaselineAttemptRecord]] = {}
        for record in records:
            grouped.setdefault(
                (
                    record.arm_id,
                    record.attempt.task_manifest_sha256,
                    record.attempt.repeat_index,
                ),
                [],
            ).append(record)
        for cell in baseline_execution_schedule(self.matrix):
            runner = self.runners[cell.arm_id]
            plan = self._plans_by_arm[cell.arm_id]
            task = self.tasks[cell.task_manifest_sha256]
            cell_key = (cell.arm_id, cell.task_manifest_sha256, cell.repeat_index)
            lineage = grouped.setdefault(cell_key, [])
            if lineage and (
                lineage[-1].attempt.status is not AttemptStatus.INFRA_FAILURE
                or len(lineage) - 1 >= plan.max_infra_retries_per_slot
            ):
                continue
            retry_of_attempt_id = lineage[-1].attempt.attempt_id if lineage else None
            retries_used = max(0, len(lineage) - 1)
            while True:
                outcome = runner.run(
                    suite=self.suite,
                    plan=plan,
                    task=task,
                    repeat_index=cell.repeat_index,
                    retry_of_attempt_id=retry_of_attempt_id,
                )
                record = BaselineAttemptRecord.from_outcome(arm_id=cell.arm_id, outcome=outcome)
                records.append(record)
                lineage.append(record)
                if outcome.attempt.status is not AttemptStatus.INFRA_FAILURE:
                    break
                if retries_used >= plan.max_infra_retries_per_slot:
                    break
                retries_used += 1
                retry_of_attempt_id = outcome.attempt.attempt_id

        integrity = self.ledger.assert_integrity()
        return BaselineMatrixResult(
            matrix_manifest_sha256=self.matrix.manifest_sha256,
            suite_manifest_sha256=self.suite.manifest_sha256,
            schedule_sha256=baseline_schedule_sha256(self.matrix),
            run_plans=self.run_plans,
            attempts=tuple(records),
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            ledger=EvaluationLedgerReceipt.model_validate(integrity),
        )
