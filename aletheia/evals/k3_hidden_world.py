"""Frozen K3-versus-K2 hidden-world ablation and scientific-exit decision.

This protocol is deliberately separate from the F7 four-arm baseline matrix.  F7's ``full_k2``
label denotes the historical campaign-learning system; reusing that label for the F9 competing-
hypothesis treatment would make the ablation uninterpretable.  Here three exact treatments share
one base model, public task prompt, tools, budgets, task/repeat seeds, evaluator, and scorer:

* a headline-metric optimizer with no explicit epistemic state;
* the historical K2 single-proposition belief state; and
* the F9/K3 versioned competing-hypothesis state.

All scientific endpoints come from a signed DiscoveryWorld scorer receipt.  Candidate-authored
summaries are never accepted as evaluation evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.adapters.discoveryworld import (
    DiscoveryWorldHarnessResult,
    DiscoveryWorldScientificExitMetrics,
)
from aletheia.evals.baselines import EvaluationLedgerReceipt
from aletheia.evals.frontier_gate import (
    GateVerdict,
    PrivateCustodyEvidence,
    ThresholdDirection,
)
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
from aletheia.evals.statistics import NumericSummary


class K3HiddenWorldError(RuntimeError):
    """The ablation cannot be run or interpreted without changing its frozen contract."""


class K3HiddenWorldArmId(str, Enum):
    HEADLINE_METRIC = "headline_metric"
    K2_SINGLE_HYPOTHESIS = "k2_single_hypothesis"
    K3_COMPETING_HYPOTHESES = "k3_competing_hypotheses"


class K3HiddenWorldPhase(str, Enum):
    VALIDATION = "validation"
    TEST = "test"


class K3HiddenWorldSuiteKind(str, Enum):
    PUBLIC_DIAGNOSTIC = "public_diagnostic"
    PRIVATE_PROSPECTIVE = "private_prospective"


class K3CriterionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class K3HiddenWorldArm(FrozenModel):
    """One exact treatment in the matched three-arm ablation."""

    schema_version: Literal[1] = 1
    arm_id: K3HiddenWorldArmId
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_model_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_task_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    treatment_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wall_time_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sampling_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_names: tuple[str, ...] = ()
    epistemic_state: Literal[
        "none",
        "single_proposition_beta",
        "versioned_competing_hypotheses",
    ]
    prediction_policy: Literal["none", "single_proposition", "per_hypothesis_preobservation"]
    experiment_selection_policy: Literal[
        "headline_metric",
        "single_proposition_eig",
        "multi_hypothesis_eig_discrimination",
    ]
    alternative_exclusion_gate: bool
    description: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _treatment_matches_arm(self) -> "K3HiddenWorldArm":
        expected = {
            K3HiddenWorldArmId.HEADLINE_METRIC: (
                "none",
                "none",
                "headline_metric",
                False,
            ),
            K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS: (
                "single_proposition_beta",
                "single_proposition",
                "single_proposition_eig",
                False,
            ),
            K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES: (
                "versioned_competing_hypotheses",
                "per_hypothesis_preobservation",
                "multi_hypothesis_eig_discrimination",
                True,
            ),
        }[self.arm_id]
        observed = (
            self.epistemic_state,
            self.prediction_policy,
            self.experiment_selection_policy,
            self.alternative_exclusion_gate,
        )
        if observed != expected:
            raise ValueError(
                f"arm {self.arm_id.value} requires epistemic treatment {expected}, got {observed}"
            )
        if len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("hidden-world arm tool names must be unique")
        if any(not name.strip() for name in self.tool_names):
            raise ValueError("hidden-world arm tool names cannot be blank")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class K3HiddenWorldAnalysisPolicy(FrozenModel):
    """Analysis choices frozen before validation or held-out test execution."""

    schema_version: Literal[1] = 1
    primary_endpoints: tuple[str, str] = (
        "wrong_explanation_elimination_score",
        "discriminating_trial_rate",
    )
    calibration_endpoint: Literal["top_label_ece"] = "top_label_ece"
    proper_scoring_endpoint: Literal["posterior_brier_score"] = "posterior_brier_score"
    false_mechanism_endpoint: Literal["false_mechanism_rate"] = "false_mechanism_rate"
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0)
    bootstrap_resamples: int = Field(default=10_000, ge=100, le=1_000_000)
    bootstrap_seed: int
    calibration_bins: int = Field(default=10, ge=2, le=100)
    resampling_scheme: Literal["paired_hierarchical_task_repeat_v1"] = (
        "paired_hierarchical_task_repeat_v1"
    )
    missing_cell_policy: Literal["fail_closed"] = "fail_closed"
    attempt_selection_policy: Literal["all_preregistered_slots_no_best_of_n"] = (
        "all_preregistered_slots_no_best_of_n"
    )
    multiplicity_correction: Literal["holm_two_primary_comparisons"] = (
        "holm_two_primary_comparisons"
    )

    @model_validator(mode="after")
    def _endpoints_are_exact(self) -> "K3HiddenWorldAnalysisPolicy":
        if self.primary_endpoints != (
            "wrong_explanation_elimination_score",
            "discriminating_trial_rate",
        ):
            raise ValueError("K3 hidden-world primary endpoints cannot be selected post hoc")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class K3HiddenWorldMatrixPlan(FrozenModel):
    """Content-addressed matched treatment plan with blocked randomization."""

    schema_version: Literal[1] = 1
    matrix_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,79}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discoveryworld_source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    harness_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_kind: K3HiddenWorldSuiteKind
    private_suite_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    phase: K3HiddenWorldPhase
    parent_validation_matrix_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    arms: tuple[K3HiddenWorldArm, ...] = Field(min_length=3, max_length=3)
    slots: tuple[EvaluationAttemptSlot, ...] = Field(min_length=1)
    required_reproduction_runs: int = Field(default=2, ge=2, le=5)
    max_infra_retries_per_slot: int = Field(default=1, ge=0, le=10)
    block_randomization_seed: int
    analysis: K3HiddenWorldAnalysisPolicy
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _matrix_is_paired_and_unconfounded(self) -> "K3HiddenWorldMatrixPlan":
        if tuple(arm.arm_id for arm in self.arms) != tuple(K3HiddenWorldArmId):
            raise ValueError(
                "K3 hidden-world arms must appear once in canonical headline/K2/K3 order"
            )
        if len({arm.system_manifest_sha256 for arm in self.arms}) != 3:
            raise ValueError("each K3 hidden-world treatment needs a distinct system manifest")
        matched_fields = (
            "base_model_manifest_sha256",
            "base_task_prompt_sha256",
            "tool_policy_sha256",
            "budget_policy_sha256",
            "wall_time_policy_sha256",
            "sampling_policy_sha256",
            "tool_names",
        )
        for field_name in matched_fields:
            if len({getattr(arm, field_name) for arm in self.arms}) != 1:
                raise ValueError(f"all K3 hidden-world arms must share the same {field_name}")

        identities = [(slot.task_manifest_sha256, slot.repeat_index) for slot in self.slots]
        if len(identities) != len(set(identities)):
            raise ValueError("each hidden-world task/repeat slot must appear exactly once")
        by_task: dict[str, list[EvaluationAttemptSlot]] = defaultdict(list)
        for slot in self.slots:
            by_task[slot.task_manifest_sha256].append(slot)
        if len(by_task) < 4:
            raise ValueError("K3 hidden-world matrices require at least four hidden-law tasks")
        minimum_repeats = 3 if self.phase is K3HiddenWorldPhase.VALIDATION else 5
        for task_hash, task_slots in by_task.items():
            repeats = sorted(slot.repeat_index for slot in task_slots)
            if repeats != list(range(len(task_slots))):
                raise ValueError(
                    f"task {task_hash} repeat indices must be contiguous and start at zero"
                )
            if len(task_slots) < minimum_repeats:
                raise ValueError(
                    f"{self.phase.value} matrices require at least {minimum_repeats} repeats per task"
                )
            if len({slot.seed for slot in task_slots}) != len(task_slots):
                raise ValueError("paired repeats for one hidden-law task require unique seeds")

        if self.phase is K3HiddenWorldPhase.TEST:
            if self.parent_validation_matrix_sha256 is None:
                raise ValueError("held-out K3 test matrices require a frozen validation parent")
        elif self.parent_validation_matrix_sha256 is not None:
            raise ValueError("validation matrices cannot claim a validation parent")
        if self.suite_kind is K3HiddenWorldSuiteKind.PRIVATE_PROSPECTIVE:
            if self.private_suite_manifest_sha256 is None:
                raise ValueError("private K3 suites require a custody manifest identity")
        elif self.private_suite_manifest_sha256 is not None:
            raise ValueError("public diagnostic matrices cannot claim private custody")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    def arm(self, arm_id: K3HiddenWorldArmId) -> K3HiddenWorldArm:
        return next(arm for arm in self.arms if arm.arm_id is arm_id)


class K3HiddenWorldScheduleCell(FrozenModel):
    schema_version: Literal[1] = 1
    block_index: int = Field(ge=0)
    position_in_block: int = Field(ge=0, le=2)
    arm_id: K3HiddenWorldArmId
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_index: int = Field(ge=0)
    seed: int


class K3HiddenWorldArmRunPlan(FrozenModel):
    schema_version: Literal[1] = 1
    arm_id: K3HiddenWorldArmId
    run_plan: EvaluationRunPlan


class K3HiddenWorldAttemptRecord(FrozenModel):
    """One terminal attempt; infrastructure retry failures remain in the lineage."""

    schema_version: Literal[1] = 1
    arm_id: K3HiddenWorldArmId
    attempt: EvaluationAttempt
    attempt_manifest: EvaluationAttemptManifest
    execution_receipt: EvaluationExecutionReceipt | None = None
    submission: EvaluationSubmission | None = None
    scorer_receipt: SignedScorerReceipt | None = None
    detail: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _record_binds_one_terminal_attempt(self) -> "K3HiddenWorldAttemptRecord":
        terminal = {
            AttemptStatus.COMPLETED,
            AttemptStatus.SCIENTIFIC_FAILURE,
            AttemptStatus.INVALID,
            AttemptStatus.INFRA_FAILURE,
            AttemptStatus.TIMEOUT,
        }
        if self.attempt.status not in terminal:
            raise ValueError("K3 hidden-world records must contain terminal attempts")
        created = self.attempt_manifest.attempt
        stable = (
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
        if any(getattr(created, name) != getattr(self.attempt, name) for name in stable):
            raise ValueError("K3 attempt identity differs from its frozen manifest")
        if self.execution_receipt is not None:
            if self.execution_receipt.attempt_id != self.attempt.attempt_id:
                raise ValueError("execution receipt belongs to another K3 attempt")
            if (
                self.execution_receipt.attempt_manifest_sha256
                != self.attempt_manifest.manifest_sha256
            ):
                raise ValueError("execution receipt does not bind the K3 attempt manifest")
        if self.submission is not None:
            if self.submission.attempt_id != self.attempt.attempt_id:
                raise ValueError("submission belongs to another K3 attempt")
            if self.submission.system_manifest_sha256 != self.attempt.system_manifest_sha256:
                raise ValueError("submission system identity differs from the K3 attempt")
        if self.scorer_receipt is not None:
            if self.execution_receipt is None or self.submission is None:
                raise ValueError("a scorer receipt requires execution and submission receipts")
            receipt = self.scorer_receipt.receipt
            receipt.verify_attempt(self.attempt)
            receipt.verify_submission(self.submission)
            if receipt.attempt_manifest_sha256 != self.attempt_manifest.manifest_sha256:
                raise ValueError("scorer receipt does not bind the K3 attempt manifest")
            if receipt.execution_receipt_sha256 != self.execution_receipt.receipt_sha256:
                raise ValueError("scorer receipt does not bind the K3 execution receipt")
        elif self.attempt.status in {AttemptStatus.COMPLETED, AttemptStatus.SCIENTIFIC_FAILURE}:
            raise ValueError("scientific terminal states require a signed scorer receipt")
        return self

    @classmethod
    def from_outcome(
        cls, *, arm_id: K3HiddenWorldArmId, outcome: EvaluationOutcome
    ) -> "K3HiddenWorldAttemptRecord":
        return cls(
            arm_id=arm_id,
            attempt=outcome.attempt,
            attempt_manifest=outcome.attempt_manifest,
            execution_receipt=outcome.execution_receipt,
            submission=outcome.submission,
            scorer_receipt=outcome.scorer_receipt,
            detail=outcome.detail,
        )


class K3HiddenWorldMatrixResult(FrozenModel):
    schema_version: Literal[1] = 1
    matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schedule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_plans: tuple[K3HiddenWorldArmRunPlan, ...] = Field(min_length=3, max_length=3)
    attempts: tuple[K3HiddenWorldAttemptRecord, ...]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    ledger: EvaluationLedgerReceipt

    @model_validator(mode="after")
    def _result_is_unique_and_ordered(self) -> "K3HiddenWorldMatrixResult":
        if self.ended_at < self.started_at:
            raise ValueError("K3 hidden-world execution ended before it started")
        if tuple(item.arm_id for item in self.run_plans) != tuple(K3HiddenWorldArmId):
            raise ValueError("K3 result run plans must use canonical arm order")
        attempt_ids = [record.attempt.attempt_id for record in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("a K3 hidden-world attempt may appear only once")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


def validate_k3_hidden_world_matrix_suite(
    matrix: K3HiddenWorldMatrixPlan, suite: EvaluationSuite
) -> None:
    if matrix.suite_manifest_sha256 != suite.manifest_sha256:
        raise K3HiddenWorldError("K3 hidden-world matrix belongs to another suite")
    planned = {slot.task_manifest_sha256 for slot in matrix.slots}
    actual = set(suite.task_manifest_sha256s)
    if planned != actual:
        raise K3HiddenWorldError(
            "K3 hidden-world slots must cover every suite task and no other task; "
            f"missing={sorted(actual - planned)}, extra={sorted(planned - actual)}"
        )


def build_k3_hidden_world_run_plans(
    matrix: K3HiddenWorldMatrixPlan, suite: EvaluationSuite
) -> tuple[K3HiddenWorldArmRunPlan, ...]:
    validate_k3_hidden_world_matrix_suite(matrix, suite)
    return tuple(
        K3HiddenWorldArmRunPlan(
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


def k3_hidden_world_execution_schedule(
    matrix: K3HiddenWorldMatrixPlan,
) -> tuple[K3HiddenWorldScheduleCell, ...]:
    """Derive deterministic blocked arm order for every paired task/repeat slot."""

    schedule: list[K3HiddenWorldScheduleCell] = []
    slots = sorted(matrix.slots, key=lambda slot: (slot.task_manifest_sha256, slot.repeat_index))
    for block_index, slot in enumerate(slots):
        order = sorted(
            K3HiddenWorldArmId,
            key=lambda arm_id: hashlib.sha256(
                (
                    f"aletheia-k3-hidden-world-order-v1\0{matrix.block_randomization_seed}\0"
                    f"{slot.task_manifest_sha256}\0{slot.repeat_index}\0{slot.seed}\0"
                    f"{arm_id.value}"
                ).encode()
            ).digest(),
        )
        for position, arm_id in enumerate(order):
            schedule.append(
                K3HiddenWorldScheduleCell(
                    block_index=block_index,
                    position_in_block=position,
                    arm_id=arm_id,
                    task_manifest_sha256=slot.task_manifest_sha256,
                    repeat_index=slot.repeat_index,
                    seed=slot.seed,
                )
            )
    return tuple(schedule)


def k3_hidden_world_schedule_sha256(matrix: K3HiddenWorldMatrixPlan) -> str:
    return content_sha256(
        {
            "schema_name": "aletheia.k3_hidden_world_schedule",
            "cells": [
                cell.model_dump(mode="json") for cell in k3_hidden_world_execution_schedule(matrix)
            ],
        }
    )


def load_k3_hidden_world_attempt_record(
    *,
    evaluator_root: Path,
    ledger: EvaluationLedger,
    arm_id: K3HiddenWorldArmId,
    attempt_id: str,
) -> K3HiddenWorldAttemptRecord:
    """Physically reload one terminal attempt for resumable matrix execution."""

    attempt = ledger.terminal_attempt(attempt_id)
    if attempt is None:
        raise K3HiddenWorldError(f"cannot resume K3 matrix with nonterminal attempt {attempt_id!r}")
    root = Path(evaluator_root).expanduser().resolve(strict=False)
    workspace = root / "evaluator_attempts" / attempt_id
    if workspace.is_symlink():
        raise K3HiddenWorldError(f"attempt {attempt_id!r} workspace is a symlink")

    def optional_model(path: Path, model_type):
        if not path.is_file() or path.is_symlink():
            return None
        return model_type.model_validate_json(path.read_bytes())

    manifest_path = workspace / "attempt_manifest.v1.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise K3HiddenWorldError(f"attempt {attempt_id!r} has no regular frozen manifest")
    manifest = EvaluationAttemptManifest.model_validate_json(manifest_path.read_bytes())
    execution = optional_model(workspace / "execution_receipt.v1.json", EvaluationExecutionReceipt)
    scorer = optional_model(workspace / "scorer_receipt.signed.v1.json", SignedScorerReceipt)

    inbox_parent = root / "submission_inbox"
    inboxes = [
        path
        for path in inbox_parent.glob(f"{attempt_id}-*")
        if path.is_dir() and not path.is_symlink()
    ]
    if len(inboxes) != 1:
        raise K3HiddenWorldError(
            f"attempt {attempt_id!r} must have exactly one sealed submission inbox"
        )
    submission = optional_model(inboxes[0] / "submission.json", EvaluationSubmission)
    detail: str | None = None
    for name in ("infrastructure_failure.json", "protocol_failure.json", "scorer_failure.json"):
        path = workspace / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            raw_detail = json.loads(path.read_text(encoding="utf-8")).get("detail")
        except (OSError, json.JSONDecodeError) as exc:
            raise K3HiddenWorldError(
                f"attempt {attempt_id!r} has an unreadable failure record"
            ) from exc
        if raw_detail is not None:
            detail = str(raw_detail)[:1024]
        break
    return K3HiddenWorldAttemptRecord(
        arm_id=arm_id,
        attempt=attempt,
        attempt_manifest=manifest,
        execution_receipt=execution,
        submission=submission,
        scorer_receipt=scorer,
        detail=detail,
    )


class K3HiddenWorldMatrixRunner:
    """Execute every frozen paired cell, retaining only authorized infrastructure retries."""

    def __init__(
        self,
        *,
        matrix: K3HiddenWorldMatrixPlan,
        suite: EvaluationSuite,
        tasks: Mapping[str, EvaluationTask],
        runners: Mapping[K3HiddenWorldArmId, IndependentEvaluationRunner],
    ) -> None:
        validate_k3_hidden_world_matrix_suite(matrix, suite)
        task_map = dict(tasks)
        if set(task_map) != set(suite.task_manifest_sha256s):
            raise K3HiddenWorldError("task map must contain every K3 suite task exactly once")
        if any(key != task.manifest_sha256 for key, task in task_map.items()):
            raise K3HiddenWorldError("K3 task map key differs from its task manifest")
        if any(task.scorer_sha256 != matrix.scorer_sha256 for task in task_map.values()):
            raise K3HiddenWorldError("K3 task scorer differs from the frozen matrix scorer")

        runner_map = dict(runners)
        if set(runner_map) != set(K3HiddenWorldArmId):
            raise K3HiddenWorldError("K3 matrix requires exactly one runner per treatment arm")
        if (
            len({runner.root for runner in runner_map.values()}) != 1
            or len({runner.ledger.path for runner in runner_map.values()}) != 1
        ):
            raise K3HiddenWorldError("all K3 arms must share one evaluator root and ledger")
        for runner in runner_map.values():
            if not runner.formal:
                raise K3HiddenWorldError("K3 matrix execution requires formal evaluators")
            if runner.evaluator_manifest_sha256 != matrix.evaluator_manifest_sha256:
                raise K3HiddenWorldError("K3 arm runner belongs to another evaluator")
            if runner.scorer.scorer_sha256 != matrix.scorer_sha256:
                raise K3HiddenWorldError("K3 arm runner uses a different hidden scorer")

        self.matrix = matrix
        self.suite = suite
        self.tasks = task_map
        self.runners = runner_map
        self.run_plans = build_k3_hidden_world_run_plans(matrix, suite)
        self._plans_by_arm = {item.arm_id: item.run_plan for item in self.run_plans}

    @property
    def ledger(self) -> EvaluationLedger:
        return next(iter(self.runners.values())).ledger

    @property
    def evaluator_root(self) -> Path:
        return next(iter(self.runners.values())).root

    def _existing_records(self) -> tuple[K3HiddenWorldAttemptRecord, ...]:
        plan_arm = {item.run_plan.manifest_sha256: item.arm_id for item in self.run_plans}
        created: list[tuple[K3HiddenWorldArmId, str]] = []
        latest: dict[str, AttemptStatus] = {}
        for event in self.ledger.events():
            if event.event_type != "attempt_state":
                continue
            attempt = EvaluationAttempt.model_validate(event.payload["attempt"])
            arm_id = plan_arm.get(attempt.run_plan_sha256)
            if arm_id is None:
                continue
            latest[attempt.attempt_id] = attempt.status
            if attempt.status is AttemptStatus.CREATED:
                created.append((arm_id, attempt.attempt_id))
        terminal = {
            AttemptStatus.COMPLETED,
            AttemptStatus.SCIENTIFIC_FAILURE,
            AttemptStatus.INVALID,
            AttemptStatus.INFRA_FAILURE,
            AttemptStatus.TIMEOUT,
        }
        nonterminal = [
            attempt_id for _arm, attempt_id in created if latest.get(attempt_id) not in terminal
        ]
        if nonterminal:
            raise K3HiddenWorldError(
                "K3 ledger contains nonterminal attempts requiring adjudication: "
                + ", ".join(nonterminal)
            )
        records = tuple(
            load_k3_hidden_world_attempt_record(
                evaluator_root=self.evaluator_root,
                ledger=self.ledger,
                arm_id=arm_id,
                attempt_id=attempt_id,
            )
            for arm_id, attempt_id in created
        )
        self._validate_resume_prefix(records)
        return records

    def _validate_resume_prefix(self, records: tuple[K3HiddenWorldAttemptRecord, ...]) -> None:
        expected = [
            (cell.arm_id, cell.task_manifest_sha256, cell.repeat_index)
            for cell in k3_hidden_world_execution_schedule(self.matrix)
        ]
        observed_order: list[tuple[K3HiddenWorldArmId, str, int]] = []
        grouped: dict[tuple[K3HiddenWorldArmId, str, int], list[K3HiddenWorldAttemptRecord]] = {}
        for record in records:
            attempt = record.attempt
            cell = (record.arm_id, attempt.task_manifest_sha256, attempt.repeat_index)
            if cell not in expected:
                raise K3HiddenWorldError("existing K3 attempt is outside the frozen schedule")
            plan = self._plans_by_arm[record.arm_id]
            arm = self.matrix.arm(record.arm_id)
            if (
                attempt.run_plan_sha256 != plan.manifest_sha256
                or attempt.system_manifest_sha256 != arm.system_manifest_sha256
                or attempt.suite_manifest_sha256 != self.suite.manifest_sha256
            ):
                raise K3HiddenWorldError("existing K3 attempt identity differs from its cell")
            slot = next(
                slot
                for slot in self.matrix.slots
                if slot.task_manifest_sha256 == attempt.task_manifest_sha256
                and slot.repeat_index == attempt.repeat_index
            )
            if attempt.seed != slot.seed:
                raise K3HiddenWorldError("existing K3 attempt changed its paired seed")
            if cell not in grouped:
                grouped[cell] = []
                observed_order.append(cell)
            grouped[cell].append(record)
        if observed_order != expected[: len(observed_order)]:
            raise K3HiddenWorldError("existing K3 attempts are not a schedule prefix")
        for cell, lineage in grouped.items():
            if lineage[0].attempt.retry_of_attempt_id is not None:
                raise K3HiddenWorldError("existing K3 cell begins with a retry")
            previous = lineage[0]
            for retry in lineage[1:]:
                if (
                    previous.attempt.status is not AttemptStatus.INFRA_FAILURE
                    or retry.attempt.retry_of_attempt_id != previous.attempt.attempt_id
                ):
                    raise K3HiddenWorldError("existing K3 retry lineage is invalid")
                previous = retry
            if len(lineage) - 1 > self._plans_by_arm[cell[0]].max_infra_retries_per_slot:
                raise K3HiddenWorldError("existing K3 cell exceeds its retry allowance")

    def run(self) -> K3HiddenWorldMatrixResult:
        records = list(self._existing_records())
        started_values = [
            record.attempt.started_at for record in records if record.attempt.started_at is not None
        ]
        started_at = min(started_values) if started_values else datetime.now(timezone.utc)
        grouped: dict[tuple[K3HiddenWorldArmId, str, int], list[K3HiddenWorldAttemptRecord]] = (
            defaultdict(list)
        )
        for record in records:
            grouped[
                (record.arm_id, record.attempt.task_manifest_sha256, record.attempt.repeat_index)
            ].append(record)

        for cell in k3_hidden_world_execution_schedule(self.matrix):
            plan = self._plans_by_arm[cell.arm_id]
            lineage = grouped[(cell.arm_id, cell.task_manifest_sha256, cell.repeat_index)]
            if lineage and (
                lineage[-1].attempt.status is not AttemptStatus.INFRA_FAILURE
                or len(lineage) - 1 >= plan.max_infra_retries_per_slot
            ):
                continue
            retry_of = lineage[-1].attempt.attempt_id if lineage else None
            retries_used = max(0, len(lineage) - 1)
            while True:
                outcome = self.runners[cell.arm_id].run(
                    suite=self.suite,
                    plan=plan,
                    task=self.tasks[cell.task_manifest_sha256],
                    repeat_index=cell.repeat_index,
                    retry_of_attempt_id=retry_of,
                )
                record = K3HiddenWorldAttemptRecord.from_outcome(
                    arm_id=cell.arm_id, outcome=outcome
                )
                records.append(record)
                lineage.append(record)
                if outcome.attempt.status is not AttemptStatus.INFRA_FAILURE:
                    break
                if retries_used >= plan.max_infra_retries_per_slot:
                    break
                retries_used += 1
                retry_of = outcome.attempt.attempt_id

        return K3HiddenWorldMatrixResult(
            matrix_manifest_sha256=self.matrix.manifest_sha256,
            suite_manifest_sha256=self.suite.manifest_sha256,
            schedule_sha256=k3_hidden_world_schedule_sha256(self.matrix),
            run_plans=self.run_plans,
            attempts=tuple(records),
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            ledger=EvaluationLedgerReceipt.model_validate(self.ledger.assert_integrity()),
        )


class K3HiddenWorldThresholds(FrozenModel):
    """Absolute and paired scientific-exit limits fixed before validation starts."""

    schema_version: Literal[1] = 1
    minimum_valid_cell_fraction: float = Field(default=0.80, gt=0.0, le=1.0)
    maximum_posterior_ece: float = Field(default=0.10, ge=0.0, le=1.0)
    maximum_mean_posterior_brier: float = Field(default=0.10, ge=0.0, le=1.0)
    maximum_false_mechanism_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    minimum_mechanism_claim_coverage: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_hypothesis_contraction_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_elimination_effect: float = Field(default=0.05, gt=0.0, le=1.0)
    minimum_discrimination_effect: float = Field(default=0.05, gt=0.0, le=1.0)
    scientific_success_noninferiority_margin: float = Field(default=0.05, ge=0.0, le=1.0)
    maximum_holm_adjusted_p_value: float = Field(default=0.05, gt=0.0, le=0.05)
    require_positive_hierarchical_ci: Literal[True] = True
    require_zero_human_interventions: Literal[True] = True
    require_zero_contamination_declarations: Literal[True] = True


class K3HiddenWorldThresholdPolicy(FrozenModel):
    """Pre-validation threshold policy mapping F9 outcomes to F7 objective dimensions."""

    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    false_discovery_mapping: Literal["false_mechanism_rate"] = "false_mechanism_rate"
    calibration_mapping: Literal["top_label_ece"] = "top_label_ece"
    false_discovery_direction: Literal[ThresholdDirection.MAXIMUM] = ThresholdDirection.MAXIMUM
    calibration_direction: Literal[ThresholdDirection.MAXIMUM] = ThresholdDirection.MAXIMUM
    thresholds: K3HiddenWorldThresholds = Field(default_factory=K3HiddenWorldThresholds)
    rationale: str = Field(min_length=1, max_length=4096)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class K3HiddenWorldNoBestOfNAudit(FrozenModel):
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


class K3HiddenWorldArmSummary(FrozenModel):
    schema_version: Literal[1] = 1
    arm_id: K3HiddenWorldArmId
    planned_cells: int = Field(ge=1)
    retained_attempts: int = Field(ge=1)
    infrastructure_retries: int = Field(ge=0)
    valid_endpoint_cells: int = Field(ge=0)
    invalid_endpoint_cells: int = Field(ge=0)
    scientific_successes: int = Field(ge=0)
    scientific_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    posterior_brier_score: NumericSummary
    discriminating_trial_rate: NumericSummary
    wrong_explanation_elimination_score: NumericSummary
    top_label_ece: float | None = Field(default=None, ge=0.0, le=1.0)
    false_mechanism_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    mechanism_claim_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    hypothesis_contraction_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    human_interventions: int = Field(ge=0)
    contamination_declarations: int = Field(ge=0)

    @model_validator(mode="after")
    def _cell_accounting_is_complete(self) -> "K3HiddenWorldArmSummary":
        if self.valid_endpoint_cells + self.invalid_endpoint_cells != self.planned_cells:
            raise ValueError("K3 endpoint accounting must cover every planned cell")
        return self


class K3HiddenWorldPairedEffect(FrozenModel):
    schema_version: Literal[1] = 1
    candidate_arm: Literal[K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES] = (
        K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES
    )
    comparator_arm: K3HiddenWorldArmId
    metric: str = Field(min_length=1, max_length=128)
    orientation: Literal["positive_favors_k3"] = "positive_favors_k3"
    planned_pairs: int = Field(ge=1)
    valid_pairs: int = Field(ge=0)
    excluded_pairs: int = Field(ge=0)
    candidate_wins: int = Field(ge=0)
    comparator_wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    mean_difference: float | None = None
    median_difference: float | None = None
    confidence_interval_low: float | None = None
    confidence_interval_high: float | None = None
    raw_exact_p_value: float | None = Field(default=None, ge=0.0, le=1.0)
    holm_adjusted_p_value: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _pair_accounting_is_complete(self) -> "K3HiddenWorldPairedEffect":
        if self.comparator_arm is K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES:
            raise ValueError("K3 cannot be its own ablation comparator")
        if self.valid_pairs + self.excluded_pairs != self.planned_pairs:
            raise ValueError("K3 paired accounting must cover every preregistered pair")
        if self.candidate_wins + self.comparator_wins + self.ties != self.valid_pairs:
            raise ValueError("K3 win/loss/tie accounting must cover all valid pairs")
        return self


class K3HiddenWorldAggregateReport(FrozenModel):
    schema_version: Literal[1] = 1
    matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    ledger: EvaluationLedgerReceipt
    selection_audit: K3HiddenWorldNoBestOfNAudit
    arms: tuple[K3HiddenWorldArmSummary, ...] = Field(min_length=3, max_length=3)
    effects: tuple[K3HiddenWorldPairedEffect, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def _report_uses_canonical_endpoints(self) -> "K3HiddenWorldAggregateReport":
        if tuple(arm.arm_id for arm in self.arms) != tuple(K3HiddenWorldArmId):
            raise ValueError("K3 report arms are not in canonical order")
        observed = tuple((effect.comparator_arm, effect.metric) for effect in self.effects)
        expected = (
            (
                K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS,
                "wrong_explanation_elimination_score",
            ),
            (K3HiddenWorldArmId.HEADLINE_METRIC, "discriminating_trial_rate"),
            (K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS, "scientific_success"),
        )
        if observed != expected:
            raise ValueError("K3 report changed or reordered its frozen endpoints")
        return self

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class K3HiddenWorldAcceptanceConfig(FrozenModel):
    """Validation-qualified test contract frozen before held-out access."""

    schema_version: Literal[1] = 1
    config_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    threshold_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_ledger_head_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_manifest_sha256s: tuple[str, ...] = Field(min_length=3, max_length=3)
    thresholds: K3HiddenWorldThresholds
    require_private_prospective_test: bool = True
    calibrated_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _arm_identities_are_distinct(self) -> "K3HiddenWorldAcceptanceConfig":
        if len(set(self.arm_manifest_sha256s)) != 3:
            raise ValueError("K3 acceptance requires three distinct arm manifests")
        return self

    @property
    def config_sha256(self) -> str:
        return content_sha256(self)


class K3HiddenWorldCriterion(FrozenModel):
    schema_version: Literal[1] = 1
    criterion_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    category: Literal["integrity", "custody", "comparison", "calibration", "mechanism"]
    relation: Literal[">=", "<=", "==", "present", "before"]
    expected: str
    observed: str | None = None
    status: K3CriterionStatus
    detail: str = Field(min_length=1, max_length=2048)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class K3HiddenWorldScientificExitDecision(FrozenModel):
    schema_version: Literal[1] = 1
    acceptance_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_matrix_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_custody_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    criteria: tuple[K3HiddenWorldCriterion, ...] = Field(min_length=1)
    verdict: GateVerdict
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _verdict_matches_criteria(self) -> "K3HiddenWorldScientificExitDecision":
        statuses = {criterion.status for criterion in self.criteria}
        expected = (
            GateVerdict.BLOCKED
            if K3CriterionStatus.BLOCKED in statuses
            else GateVerdict.FAIL
            if K3CriterionStatus.FAIL in statuses
            else GateVerdict.PASS
        )
        if self.verdict is not expected:
            raise ValueError("K3 scientific-exit verdict differs from its criteria")
        return self

    @property
    def decision_sha256(self) -> str:
        return content_sha256(self)


K3CellKey = tuple[K3HiddenWorldArmId, str, int]


@dataclass(frozen=True)
class _AuditedK3Attempts:
    lineages: dict[K3CellKey, tuple[K3HiddenWorldAttemptRecord, ...]]
    final: dict[K3CellKey, K3HiddenWorldAttemptRecord]
    metrics: dict[K3CellKey, DiscoveryWorldScientificExitMetrics | None]
    retries: int


def _score(record: K3HiddenWorldAttemptRecord):
    return record.scorer_receipt.receipt.score if record.scorer_receipt is not None else None


def _endpoint_metrics(
    record: K3HiddenWorldAttemptRecord,
    *,
    required_reproduction_runs: int,
) -> DiscoveryWorldScientificExitMetrics | None:
    score = _score(record)
    if score is None or score.invalid_reasons or score.scientific_success is None:
        return None
    raw_metrics = score.evidence_objects.get("scientific_exit_metrics")
    if raw_metrics is None:
        raise K3HiddenWorldError(
            "valid K3 cells require evaluator-owned scientific_exit_metrics; rerun the frozen scorer"
        )
    metrics = DiscoveryWorldScientificExitMetrics.model_validate(raw_metrics)
    result_rows = [
        (name, value)
        for name, value in score.evidence_objects.items()
        if name.startswith("harness_run_")
    ]
    if len(result_rows) != required_reproduction_runs:
        raise K3HiddenWorldError("K3 cell lacks the frozen number of harness reproductions")
    results = [
        DiscoveryWorldHarnessResult.model_validate(value)
        for name, value in sorted(result_rows, key=lambda item: item[0])
    ]
    if tuple(result.run_index for result in results) != tuple(range(required_reproduction_runs)):
        raise K3HiddenWorldError("K3 harness reproduction indices are not contiguous")
    reproduction_keys = {
        (
            result.trace_sha256,
            result.program_exit_reason,
            result.completed_successfully,
            result.final_hypothesis_id,
            result.explicit_rule_discovery,
        )
        for result in results
    }
    if len(reproduction_keys) != 1 or score.objective_scores.get("reproducible") != 1.0:
        raise K3HiddenWorldError("K3 valid cell is not exactly reproducible")
    if metrics.source_trace_sha256 != results[0].trace_sha256:
        raise K3HiddenWorldError("scientific-exit metrics belong to another action trace")
    if not metrics.correct_hypothesis_preserved:
        raise K3HiddenWorldError("trusted objective state removed the governing hidden rule")

    expected_scores = {
        "posterior_brier_score": metrics.posterior_brier_score,
        "top_label_confidence": metrics.top_label_confidence,
        "top_label_correct": float(metrics.top_label_correct),
        "mechanism_claim_coverage": float(metrics.mechanism_claimed),
        "false_mechanism_rate": float(metrics.false_mechanism_claim),
        "genuine_discriminating_trials": float(metrics.genuine_discriminating_trials),
        "discriminating_trial_rate": metrics.discriminating_trial_rate,
        "wrong_explanation_elimination_score": metrics.wrong_explanation_elimination_score,
        "hypothesis_space_contracted": float(metrics.hypothesis_space_contracted),
    }
    for name, expected in expected_scores.items():
        observed = score.objective_scores.get(name)
        if observed is None or not math.isclose(observed, expected, abs_tol=1e-12):
            raise K3HiddenWorldError(
                f"signed objective {name!r} differs from its evaluator-owned metrics object"
            )
    return metrics if metrics.trace_complete else None


def _audit_k3_hidden_world_attempts(
    *,
    matrix: K3HiddenWorldMatrixPlan,
    suite: EvaluationSuite,
    result: K3HiddenWorldMatrixResult,
    ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
) -> _AuditedK3Attempts:
    validate_k3_hidden_world_matrix_suite(matrix, suite)
    if result.matrix_manifest_sha256 != matrix.manifest_sha256:
        raise K3HiddenWorldError("K3 result belongs to another matrix")
    if result.suite_manifest_sha256 != suite.manifest_sha256:
        raise K3HiddenWorldError("K3 result belongs to another suite")
    if result.schedule_sha256 != k3_hidden_world_schedule_sha256(matrix):
        raise K3HiddenWorldError("K3 result schedule differs from preregistration")
    expected_plans = build_k3_hidden_world_run_plans(matrix, suite)
    if result.run_plans != expected_plans:
        raise K3HiddenWorldError("K3 result run plans differ from preregistration")
    plan_arm = {item.run_plan.manifest_sha256: item.arm_id for item in expected_plans}
    plans = {item.arm_id: item.run_plan for item in expected_plans}

    current_ledger = EvaluationLedgerReceipt.model_validate(ledger.assert_integrity())
    if current_ledger != result.ledger:
        raise K3HiddenWorldError("evaluator ledger changed after K3 result sealing")
    events = ledger.events()
    registered = {
        event.payload.get("plan_sha256")
        for event in events
        if event.event_type == "run_plan_registered"
    }
    if missing := set(plan_arm) - registered:
        raise K3HiddenWorldError(f"K3 run plans are absent from the ledger: {sorted(missing)}")

    created_ids: set[str] = set()
    terminal: dict[str, EvaluationAttempt] = {}
    score_events: dict[str, str] = {}
    execution_events: dict[str, str] = {}
    manifest_events: dict[str, str] = {}
    submission_events: dict[str, str] = {}
    retry_events: dict[str, str] = {}
    terminal_states = {
        AttemptStatus.COMPLETED,
        AttemptStatus.SCIENTIFIC_FAILURE,
        AttemptStatus.INVALID,
        AttemptStatus.INFRA_FAILURE,
        AttemptStatus.TIMEOUT,
    }
    for event in events:
        if event.event_type == "attempt_state":
            attempt = EvaluationAttempt.model_validate(event.payload["attempt"])
            if attempt.run_plan_sha256 not in plan_arm:
                continue
            if attempt.status is AttemptStatus.CREATED:
                created_ids.add(attempt.attempt_id)
            if attempt.status in terminal_states:
                terminal[attempt.attempt_id] = attempt
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
    if result_ids != created_ids:
        raise K3HiddenWorldError(
            "K3 result must retain every ledger attempt and no undeclared attempt; "
            f"omitted={sorted(created_ids - result_ids)}, "
            f"undeclared={sorted(result_ids - created_ids)}"
        )

    grouped: dict[K3CellKey, list[K3HiddenWorldAttemptRecord]] = defaultdict(list)
    for record in result.attempts:
        attempt = record.attempt
        expected_arm = plan_arm.get(attempt.run_plan_sha256)
        if expected_arm is None or record.arm_id is not expected_arm:
            raise K3HiddenWorldError("K3 attempt arm differs from its run plan")
        arm = matrix.arm(record.arm_id)
        if attempt.system_manifest_sha256 != arm.system_manifest_sha256:
            raise K3HiddenWorldError("K3 attempt system differs from its treatment")
        if attempt.suite_manifest_sha256 != suite.manifest_sha256:
            raise K3HiddenWorldError("K3 attempt suite differs from its matrix")
        matches = [
            slot
            for slot in matrix.slots
            if slot.task_manifest_sha256 == attempt.task_manifest_sha256
            and slot.repeat_index == attempt.repeat_index
            and slot.seed == attempt.seed
        ]
        if len(matches) != 1:
            raise K3HiddenWorldError("K3 attempt is outside the paired preregistration")
        ledger_attempt = terminal.get(attempt.attempt_id)
        if ledger_attempt is None or ledger_attempt.attempt_sha256 != attempt.attempt_sha256:
            raise K3HiddenWorldError("K3 terminal attempt differs from the ledger")
        if manifest_events.get(attempt.attempt_id) != record.attempt_manifest.manifest_sha256:
            raise K3HiddenWorldError("K3 attempt manifest is absent or differs in the ledger")

        if record.execution_receipt is not None:
            if execution_events.get(attempt.attempt_id) != record.execution_receipt.receipt_sha256:
                raise K3HiddenWorldError("K3 execution receipt differs from the ledger")
        elif attempt.attempt_id in execution_events:
            raise K3HiddenWorldError("K3 result omitted a ledger execution receipt")
        if record.submission is not None:
            if submission_events.get(attempt.attempt_id) != record.submission.submission_sha256:
                raise K3HiddenWorldError("K3 submission differs from the ledger")
        elif attempt.attempt_id in submission_events:
            raise K3HiddenWorldError("K3 result omitted a ledger submission")
        if record.scorer_receipt is not None:
            signed = record.scorer_receipt
            key = receipt_keys.get(signed.key_id)
            if key is None or len(key) < 32:
                raise K3HiddenWorldError(
                    f"no trusted verification key for K3 scorer key {signed.key_id!r}"
                )
            try:
                signed.verify(key=key, expected_key_id=signed.key_id)
            except ValueError as exc:
                raise K3HiddenWorldError(str(exc)) from exc
            receipt = signed.receipt
            if receipt.evaluator_manifest_sha256 != matrix.evaluator_manifest_sha256:
                raise K3HiddenWorldError("K3 score belongs to another evaluator")
            if receipt.scorer_sha256 != matrix.scorer_sha256:
                raise K3HiddenWorldError("K3 score belongs to another hidden scorer")
            if score_events.get(attempt.attempt_id) != signed.envelope_sha256:
                raise K3HiddenWorldError("K3 signed score differs from the ledger")
            score = receipt.score
            expected_status = (
                AttemptStatus.INVALID
                if score.invalid_reasons or score.scientific_success is None
                else AttemptStatus.COMPLETED
                if score.scientific_success
                else AttemptStatus.SCIENTIFIC_FAILURE
            )
            if attempt.status is not expected_status:
                raise K3HiddenWorldError("K3 terminal state disagrees with its signed score")
        elif attempt.attempt_id in score_events:
            raise K3HiddenWorldError("K3 result omitted a signed score recorded in the ledger")
        grouped[(record.arm_id, attempt.task_manifest_sha256, attempt.repeat_index)].append(record)

    expected_cells = {
        (arm_id, slot.task_manifest_sha256, slot.repeat_index)
        for arm_id in K3HiddenWorldArmId
        for slot in matrix.slots
    }
    if set(grouped) != expected_cells:
        raise K3HiddenWorldError("K3 attempts do not cover every paired treatment cell")

    final: dict[K3CellKey, K3HiddenWorldAttemptRecord] = {}
    lineages: dict[K3CellKey, tuple[K3HiddenWorldAttemptRecord, ...]] = {}
    metrics: dict[K3CellKey, DiscoveryWorldScientificExitMetrics | None] = {}
    retry_total = 0
    for cell, records in grouped.items():
        plan = plans[cell[0]]
        if records[0].attempt.retry_of_attempt_id is not None:
            raise K3HiddenWorldError("first K3 cell attempt cannot be a retry")
        previous = records[0]
        for retry in records[1:]:
            retry_total += 1
            if previous.attempt.status is not AttemptStatus.INFRA_FAILURE:
                raise K3HiddenWorldError("only K3 infrastructure failure can precede a retry")
            if retry.attempt.retry_of_attempt_id != previous.attempt.attempt_id:
                raise K3HiddenWorldError("K3 retry lineage is not contiguous")
            if retry_events.get(retry.attempt.attempt_id) != previous.attempt.attempt_id:
                raise K3HiddenWorldError("K3 retry lacks evaluator authorization")
            previous = retry
        if len(records) - 1 > plan.max_infra_retries_per_slot:
            raise K3HiddenWorldError("K3 cell exceeds its preregistered retries")
        if (
            records[-1].attempt.status is AttemptStatus.INFRA_FAILURE
            and len(records) - 1 != plan.max_infra_retries_per_slot
        ):
            raise K3HiddenWorldError("K3 matrix stopped before exhausting an infra retry")
        lineages[cell] = tuple(records)
        final[cell] = records[-1]
        metrics[cell] = _endpoint_metrics(
            records[-1], required_reproduction_runs=matrix.required_reproduction_runs
        )
    return _AuditedK3Attempts(
        lineages=lineages,
        final=final,
        metrics=metrics,
        retries=retry_total,
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot take a quantile of an empty sample")
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


def _hierarchical_ci(
    values_by_task: Mapping[str, Sequence[float]],
    *,
    confidence_level: float,
    resamples: int,
    seed: int,
) -> tuple[float, float]:
    if not values_by_task:
        raise ValueError("cannot bootstrap an empty K3 paired sample")
    task_ids = sorted(values_by_task)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resamples):
        sampled: list[float] = []
        for _task_index in range(len(task_ids)):
            task_id = task_ids[rng.randrange(len(task_ids))]
            task_values = values_by_task[task_id]
            for _repeat_index in range(len(task_values)):
                sampled.append(float(task_values[rng.randrange(len(task_values))]))
        estimates.append(sum(sampled) / len(sampled))
    tail = (1.0 - confidence_level) / 2.0
    return _quantile(estimates, tail), _quantile(estimates, 1.0 - tail)


def _effect_seed(
    policy: K3HiddenWorldAnalysisPolicy, comparator: K3HiddenWorldArmId, metric: str
) -> int:
    digest = content_sha256(
        {
            "bootstrap_seed": policy.bootstrap_seed,
            "comparator": comparator.value,
            "metric": metric,
        }
    )
    return int(digest[:16], 16)


def _exact_two_sided_sign_p(wins: int, losses: int) -> float:
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    smaller = min(wins, losses)
    tail = sum(math.comb(discordant, index) for index in range(smaller + 1)) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _top_label_ece(
    metrics: Sequence[DiscoveryWorldScientificExitMetrics], *, bins: int
) -> float | None:
    if not metrics:
        return None
    rows: dict[int, list[DiscoveryWorldScientificExitMetrics]] = defaultdict(list)
    for metric in metrics:
        bin_index = min(bins - 1, int(metric.top_label_confidence * bins))
        rows[bin_index].append(metric)
    total = len(metrics)
    return sum(
        (len(bin_rows) / total)
        * abs(
            sum(float(row.top_label_correct) for row in bin_rows) / len(bin_rows)
            - sum(row.top_label_confidence for row in bin_rows) / len(bin_rows)
        )
        for bin_rows in rows.values()
    )


def _arm_summary(
    arm_id: K3HiddenWorldArmId,
    *,
    matrix: K3HiddenWorldMatrixPlan,
    audited: _AuditedK3Attempts,
) -> K3HiddenWorldArmSummary:
    cells = [(arm_id, slot.task_manifest_sha256, slot.repeat_index) for slot in matrix.slots]
    metric_rows = [audited.metrics[cell] for cell in cells if audited.metrics[cell] is not None]
    metrics = [row for row in metric_rows if row is not None]
    valid = len(metrics)
    successes = sum(
        _score(audited.final[cell]).scientific_success is True
        for cell in cells
        if audited.metrics[cell] is not None and _score(audited.final[cell]) is not None
    )
    lineages = [record for cell in cells for record in audited.lineages[cell]]
    contamination = sum(
        len(record.submission.declared_contamination)
        for record in lineages
        if record.submission is not None
    )
    return K3HiddenWorldArmSummary(
        arm_id=arm_id,
        planned_cells=len(cells),
        retained_attempts=len(lineages),
        infrastructure_retries=len(lineages) - len(cells),
        valid_endpoint_cells=valid,
        invalid_endpoint_cells=len(cells) - valid,
        scientific_successes=successes,
        scientific_success_rate=successes / valid if valid else None,
        posterior_brier_score=_summary(
            [metric.posterior_brier_score for metric in metrics],
            missing=len(cells) - valid,
        ),
        discriminating_trial_rate=_summary(
            [metric.discriminating_trial_rate for metric in metrics],
            missing=len(cells) - valid,
        ),
        wrong_explanation_elimination_score=_summary(
            [metric.wrong_explanation_elimination_score for metric in metrics],
            missing=len(cells) - valid,
        ),
        top_label_ece=_top_label_ece(metrics, bins=matrix.analysis.calibration_bins),
        false_mechanism_rate=(
            sum(float(metric.false_mechanism_claim) for metric in metrics) / valid
            if valid
            else None
        ),
        mechanism_claim_coverage=(
            sum(float(metric.mechanism_claimed) for metric in metrics) / valid if valid else None
        ),
        hypothesis_contraction_rate=(
            sum(float(metric.hypothesis_space_contracted) for metric in metrics) / valid
            if valid
            else None
        ),
        human_interventions=sum(record.attempt.intervention_count for record in lineages),
        contamination_declarations=contamination,
    )


def _paired_effect(
    *,
    comparator: K3HiddenWorldArmId,
    metric: str,
    matrix: K3HiddenWorldMatrixPlan,
    audited: _AuditedK3Attempts,
) -> K3HiddenWorldPairedEffect:
    differences: dict[str, list[float]] = defaultdict(list)
    wins = losses = ties = 0
    for slot in matrix.slots:
        candidate_key = (
            K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES,
            slot.task_manifest_sha256,
            slot.repeat_index,
        )
        comparator_key = (comparator, slot.task_manifest_sha256, slot.repeat_index)
        candidate_metrics = audited.metrics[candidate_key]
        comparator_metrics = audited.metrics[comparator_key]
        if candidate_metrics is None or comparator_metrics is None:
            continue
        if metric == "scientific_success":
            candidate_score = _score(audited.final[candidate_key])
            comparator_score = _score(audited.final[comparator_key])
            if candidate_score is None or comparator_score is None:
                continue
            candidate_value = float(candidate_score.scientific_success is True)
            comparator_value = float(comparator_score.scientific_success is True)
        else:
            candidate_value = float(getattr(candidate_metrics, metric))
            comparator_value = float(getattr(comparator_metrics, metric))
        difference = candidate_value - comparator_value
        differences[slot.task_manifest_sha256].append(difference)
        if difference > 1e-12:
            wins += 1
        elif difference < -1e-12:
            losses += 1
        else:
            ties += 1
    flat = [value for task_values in differences.values() for value in task_values]
    if not flat:
        return K3HiddenWorldPairedEffect(
            comparator_arm=comparator,
            metric=metric,
            planned_pairs=len(matrix.slots),
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
        seed=_effect_seed(matrix.analysis, comparator, metric),
    )
    return K3HiddenWorldPairedEffect(
        comparator_arm=comparator,
        metric=metric,
        planned_pairs=len(matrix.slots),
        valid_pairs=len(flat),
        excluded_pairs=len(matrix.slots) - len(flat),
        candidate_wins=wins,
        comparator_wins=losses,
        ties=ties,
        mean_difference=sum(flat) / len(flat),
        median_difference=_quantile(flat, 0.5),
        confidence_interval_low=low,
        confidence_interval_high=high,
        raw_exact_p_value=_exact_two_sided_sign_p(wins, losses),
    )


def aggregate_k3_hidden_world_matrix(
    *,
    matrix: K3HiddenWorldMatrixPlan,
    suite: EvaluationSuite,
    result: K3HiddenWorldMatrixResult,
    ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
    generated_at: datetime | None = None,
) -> K3HiddenWorldAggregateReport:
    """Re-audit every raw receipt and compute the frozen paired endpoints."""

    audited = _audit_k3_hidden_world_attempts(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys=receipt_keys,
    )
    timestamp = generated_at or datetime.now(timezone.utc)
    if timestamp < result.ended_at:
        raise K3HiddenWorldError("K3 aggregate report predates matrix completion")
    effects = [
        _paired_effect(
            comparator=K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS,
            metric="wrong_explanation_elimination_score",
            matrix=matrix,
            audited=audited,
        ),
        _paired_effect(
            comparator=K3HiddenWorldArmId.HEADLINE_METRIC,
            metric="discriminating_trial_rate",
            matrix=matrix,
            audited=audited,
        ),
        _paired_effect(
            comparator=K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS,
            metric="scientific_success",
            matrix=matrix,
            audited=audited,
        ),
    ]
    primary = effects[:2]
    ordered = sorted(enumerate(primary), key=lambda item: item[1].raw_exact_p_value or 1.0)
    adjusted: dict[int, float] = {}
    running = 0.0
    for rank, (index, effect) in enumerate(ordered):
        raw = effect.raw_exact_p_value if effect.raw_exact_p_value is not None else 1.0
        running = max(running, min(1.0, (len(primary) - rank) * raw))
        adjusted[index] = running
    effects = [
        effect.model_copy(update={"holm_adjusted_p_value": adjusted[index]})
        if index < 2
        else effect
        for index, effect in enumerate(effects)
    ]
    planned_cells = len(matrix.slots) * len(tuple(K3HiddenWorldArmId))
    return K3HiddenWorldAggregateReport(
        matrix_manifest_sha256=matrix.manifest_sha256,
        result_sha256=result.result_sha256,
        suite_manifest_sha256=suite.manifest_sha256,
        analysis_policy_sha256=matrix.analysis.policy_sha256,
        scorer_sha256=matrix.scorer_sha256,
        generated_at=timestamp,
        ledger=result.ledger,
        selection_audit=K3HiddenWorldNoBestOfNAudit(
            planned_cells=planned_cells,
            observed_cells=len(audited.final),
            retained_attempts=len(result.attempts),
            authorized_infrastructure_retries=audited.retries,
        ),
        arms=tuple(
            _arm_summary(arm_id, matrix=matrix, audited=audited) for arm_id in K3HiddenWorldArmId
        ),
        effects=tuple(effects),
    )


def _report_arm(
    report: K3HiddenWorldAggregateReport, arm_id: K3HiddenWorldArmId
) -> K3HiddenWorldArmSummary:
    return next(arm for arm in report.arms if arm.arm_id is arm_id)


def _report_effect(
    report: K3HiddenWorldAggregateReport,
    comparator: K3HiddenWorldArmId,
    metric: str,
) -> K3HiddenWorldPairedEffect:
    matches = [
        effect
        for effect in report.effects
        if effect.comparator_arm is comparator and effect.metric == metric
    ]
    if len(matches) != 1:
        raise K3HiddenWorldError(
            f"K3 report does not uniquely contain {metric} versus {comparator.value}"
        )
    return matches[0]


def _measured_threshold_checks(
    report: K3HiddenWorldAggregateReport,
    thresholds: K3HiddenWorldThresholds,
) -> list[tuple[str, str, str, float, float | None, bool | None, str]]:
    checks: list[tuple[str, str, str, float, float | None, bool | None, str]] = []
    for arm in report.arms:
        observed = arm.valid_endpoint_cells / arm.planned_cells
        checks.append(
            (
                f"valid_fraction.{arm.arm_id.value}",
                "integrity",
                ">=",
                thresholds.minimum_valid_cell_fraction,
                observed,
                observed >= thresholds.minimum_valid_cell_fraction,
                "Signed, reproducible endpoint coverage must meet the frozen floor.",
            )
        )
    k3 = _report_arm(report, K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES)
    checks.extend(
        (
            (
                "posterior_ece",
                "calibration",
                "<=",
                thresholds.maximum_posterior_ece,
                k3.top_label_ece,
                (
                    None
                    if k3.top_label_ece is None
                    else k3.top_label_ece <= thresholds.maximum_posterior_ece
                ),
                "Top-label posterior ECE instantiates the F7 calibration-error dimension.",
            ),
            (
                "posterior_brier",
                "calibration",
                "<=",
                thresholds.maximum_mean_posterior_brier,
                k3.posterior_brier_score.mean,
                (
                    None
                    if k3.posterior_brier_score.mean is None
                    else k3.posterior_brier_score.mean <= thresholds.maximum_mean_posterior_brier
                ),
                "The proper multiclass score guards against confidence-bin gaming.",
            ),
            (
                "false_mechanism_rate",
                "mechanism",
                "<=",
                thresholds.maximum_false_mechanism_rate,
                k3.false_mechanism_rate,
                (
                    None
                    if k3.false_mechanism_rate is None
                    else k3.false_mechanism_rate <= thresholds.maximum_false_mechanism_rate
                ),
                "Wrong issued hidden-law claims instantiate the F7 false-discovery dimension.",
            ),
            (
                "mechanism_claim_coverage",
                "mechanism",
                ">=",
                thresholds.minimum_mechanism_claim_coverage,
                k3.mechanism_claim_coverage,
                (
                    None
                    if k3.mechanism_claim_coverage is None
                    else k3.mechanism_claim_coverage >= thresholds.minimum_mechanism_claim_coverage
                ),
                "A low false-mechanism rate cannot be obtained by universal abstention.",
            ),
            (
                "hypothesis_contraction",
                "mechanism",
                ">=",
                thresholds.minimum_hypothesis_contraction_rate,
                k3.hypothesis_contraction_rate,
                (
                    None
                    if k3.hypothesis_contraction_rate is None
                    else k3.hypothesis_contraction_rate
                    >= thresholds.minimum_hypothesis_contraction_rate
                ),
                "The hidden hypothesis space must substantively contract.",
            ),
        )
    )

    elimination = _report_effect(
        report,
        K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS,
        "wrong_explanation_elimination_score",
    )
    discrimination = _report_effect(
        report,
        K3HiddenWorldArmId.HEADLINE_METRIC,
        "discriminating_trial_rate",
    )
    noninferiority = _report_effect(
        report, K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS, "scientific_success"
    )
    for label, effect, floor in (
        ("elimination_vs_k2", elimination, thresholds.minimum_elimination_effect),
        (
            "discrimination_vs_headline",
            discrimination,
            thresholds.minimum_discrimination_effect,
        ),
    ):
        checks.extend(
            (
                (
                    f"{label}.effect",
                    "comparison",
                    ">=",
                    floor,
                    effect.mean_difference,
                    (None if effect.mean_difference is None else effect.mean_difference >= floor),
                    "The paired mean must exceed the preregistered practical-effect floor.",
                ),
                (
                    f"{label}.ci_low",
                    "comparison",
                    ">=",
                    0.0,
                    effect.confidence_interval_low,
                    (
                        None
                        if effect.confidence_interval_low is None
                        else effect.confidence_interval_low > 0.0
                    ),
                    "The paired hierarchical confidence interval must exclude no improvement.",
                ),
                (
                    f"{label}.holm_p",
                    "comparison",
                    "<=",
                    thresholds.maximum_holm_adjusted_p_value,
                    effect.holm_adjusted_p_value,
                    (
                        None
                        if effect.holm_adjusted_p_value is None
                        else effect.holm_adjusted_p_value
                        <= thresholds.maximum_holm_adjusted_p_value
                    ),
                    "Both primary comparisons use the frozen Holm multiplicity correction.",
                ),
            )
        )
    checks.append(
        (
            "scientific_success_noninferiority",
            "comparison",
            ">=",
            -thresholds.scientific_success_noninferiority_margin,
            noninferiority.confidence_interval_low,
            (
                None
                if noninferiority.confidence_interval_low is None
                else noninferiority.confidence_interval_low
                >= -thresholds.scientific_success_noninferiority_margin
            ),
            "Faster epistemic progress cannot be purchased by losing hidden-task success.",
        )
    )
    interventions = sum(arm.human_interventions for arm in report.arms)
    contamination = sum(arm.contamination_declarations for arm in report.arms)
    checks.extend(
        (
            (
                "human_interventions",
                "integrity",
                "<=",
                0.0,
                float(interventions),
                interventions == 0,
                "Formal scientific-exit runs permit no human intervention.",
            ),
            (
                "contamination_declarations",
                "integrity",
                "<=",
                0.0,
                float(contamination),
                contamination == 0,
                "Any declared answer overlap blocks an uncontaminated scientific claim.",
            ),
        )
    )
    return checks


def freeze_k3_hidden_world_acceptance(
    *,
    config_id: str,
    threshold_policy: K3HiddenWorldThresholdPolicy,
    validation_matrix: K3HiddenWorldMatrixPlan,
    validation_result: K3HiddenWorldMatrixResult,
    validation_report: K3HiddenWorldAggregateReport,
    test_matrix: K3HiddenWorldMatrixPlan,
    require_private_prospective_test: bool = True,
    calibrated_at: datetime | None = None,
) -> K3HiddenWorldAcceptanceConfig:
    """Qualify a held-out test using only a pre-validation policy and validation receipts."""

    if validation_matrix.phase is not K3HiddenWorldPhase.VALIDATION:
        raise K3HiddenWorldError("K3 acceptance calibration requires a validation matrix")
    if test_matrix.phase is not K3HiddenWorldPhase.TEST:
        raise K3HiddenWorldError("K3 acceptance must bind a held-out test matrix")
    if test_matrix.parent_validation_matrix_sha256 != validation_matrix.manifest_sha256:
        raise K3HiddenWorldError("K3 test matrix does not name this validation parent")
    if threshold_policy.frozen_at > validation_result.started_at:
        raise K3HiddenWorldError("K3 thresholds were frozen after validation access began")
    if validation_report.matrix_manifest_sha256 != validation_matrix.manifest_sha256:
        raise K3HiddenWorldError("K3 validation report belongs to another matrix")
    if validation_report.result_sha256 != validation_result.result_sha256:
        raise K3HiddenWorldError("K3 validation report belongs to another result")
    if validation_report.ledger.head_sha256 is None:
        raise K3HiddenWorldError("K3 validation requires a retained ledger head")

    stable_fields = (
        "discoveryworld_source_manifest_sha256",
        "harness_manifest_sha256",
        "scorer_sha256",
        "evaluator_manifest_sha256",
        "required_reproduction_runs",
        "analysis",
        "arms",
    )
    drift = [
        field_name
        for field_name in stable_fields
        if getattr(validation_matrix, field_name) != getattr(test_matrix, field_name)
    ]
    if drift:
        raise K3HiddenWorldError(f"K3 held-out treatments drifted after validation: {drift}")
    failed = [
        name
        for name, _category, _relation, _expected, _observed, passed, _detail in (
            _measured_threshold_checks(validation_report, threshold_policy.thresholds)
        )
        if passed is not True
    ]
    if failed:
        raise K3HiddenWorldError(
            "validation evidence does not qualify the frozen K3 test: " + ", ".join(failed)
        )
    timestamp = calibrated_at or datetime.now(timezone.utc)
    if timestamp < validation_report.generated_at or timestamp < test_matrix.frozen_at:
        raise K3HiddenWorldError("K3 acceptance config predates its validation/test inputs")
    return K3HiddenWorldAcceptanceConfig(
        config_id=config_id,
        threshold_policy_sha256=threshold_policy.policy_sha256,
        validation_matrix_manifest_sha256=validation_matrix.manifest_sha256,
        validation_result_sha256=validation_result.result_sha256,
        validation_report_sha256=validation_report.report_sha256,
        validation_ledger_head_sha256=validation_report.ledger.head_sha256,
        test_matrix_manifest_sha256=test_matrix.manifest_sha256,
        test_suite_manifest_sha256=test_matrix.suite_manifest_sha256,
        evaluator_manifest_sha256=test_matrix.evaluator_manifest_sha256,
        scorer_sha256=test_matrix.scorer_sha256,
        analysis_policy_sha256=test_matrix.analysis.policy_sha256,
        arm_manifest_sha256s=tuple(arm.manifest_sha256 for arm in test_matrix.arms),
        thresholds=threshold_policy.thresholds,
        require_private_prospective_test=require_private_prospective_test,
        calibrated_at=timestamp,
    )


def generate_k3_hidden_world_scientific_exit_decision(
    *,
    config: K3HiddenWorldAcceptanceConfig,
    threshold_policy: K3HiddenWorldThresholdPolicy,
    matrix: K3HiddenWorldMatrixPlan,
    suite: EvaluationSuite,
    result: K3HiddenWorldMatrixResult,
    ledger: EvaluationLedger,
    receipt_keys: Mapping[str, bytes],
    private_custody: PrivateCustodyEvidence | None = None,
    decided_at: datetime | None = None,
) -> tuple[K3HiddenWorldAggregateReport, K3HiddenWorldScientificExitDecision]:
    """Reaggregate raw evidence and issue a fail-closed K3 scientific-exit decision."""

    if config.threshold_policy_sha256 != threshold_policy.policy_sha256:
        raise K3HiddenWorldError("K3 acceptance config names another threshold policy")
    if config.thresholds != threshold_policy.thresholds:
        raise K3HiddenWorldError("K3 acceptance thresholds were mutated after calibration")
    if matrix.phase is not K3HiddenWorldPhase.TEST:
        raise K3HiddenWorldError("K3 scientific exit requires the held-out test phase")
    expected_bindings = (
        config.test_matrix_manifest_sha256 == matrix.manifest_sha256,
        config.test_suite_manifest_sha256 == suite.manifest_sha256,
        config.evaluator_manifest_sha256 == matrix.evaluator_manifest_sha256,
        config.scorer_sha256 == matrix.scorer_sha256,
        config.analysis_policy_sha256 == matrix.analysis.policy_sha256,
        config.arm_manifest_sha256s == tuple(arm.manifest_sha256 for arm in matrix.arms),
    )
    if not all(expected_bindings):
        raise K3HiddenWorldError("K3 held-out evidence differs from its acceptance config")
    timestamp = decided_at or datetime.now(timezone.utc)
    if timestamp < result.ended_at:
        raise K3HiddenWorldError("K3 decision predates held-out execution completion")
    report = aggregate_k3_hidden_world_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys=receipt_keys,
        generated_at=timestamp,
    )
    criteria: list[K3HiddenWorldCriterion] = []

    def add(
        *,
        criterion_id: str,
        category: Literal["integrity", "custody", "comparison", "calibration", "mechanism"],
        relation: Literal[">=", "<=", "==", "present", "before"],
        expected: object,
        observed: object | None,
        passed: bool | None,
        detail: str,
        evidence_sha256: str = report.report_sha256,
    ) -> None:
        criteria.append(
            K3HiddenWorldCriterion(
                criterion_id=criterion_id,
                category=category,
                relation=relation,
                expected=str(expected),
                observed=None if observed is None else str(observed),
                status=(
                    K3CriterionStatus.BLOCKED
                    if passed is None
                    else K3CriterionStatus.PASS
                    if passed
                    else K3CriterionStatus.FAIL
                ),
                detail=detail,
                evidence_sha256=evidence_sha256,
            )
        )

    add(
        criterion_id="acceptance_precedes_test",
        category="integrity",
        relation="before",
        expected=result.started_at,
        observed=config.calibrated_at,
        passed=config.calibrated_at <= result.started_at,
        detail="Validation-qualified thresholds must be frozen before held-out execution.",
        evidence_sha256=config.config_sha256,
    )
    if config.require_private_prospective_test:
        is_private = matrix.suite_kind is K3HiddenWorldSuiteKind.PRIVATE_PROSPECTIVE
        add(
            criterion_id="private_prospective_suite",
            category="custody",
            relation="==",
            expected=K3HiddenWorldSuiteKind.PRIVATE_PROSPECTIVE.value,
            observed=matrix.suite_kind.value,
            passed=True if is_private else None,
            detail=(
                "Public DiscoveryWorld tasks are diagnostics; scientific exit requires a private "
                "prospective hidden-law suite."
            ),
            evidence_sha256=matrix.manifest_sha256,
        )
        if private_custody is None:
            add(
                criterion_id="private_custody",
                category="custody",
                relation="present",
                expected="passing private custody evidence",
                observed=None,
                passed=None,
                detail="No independent encrypted-suite custody evidence was supplied.",
                evidence_sha256=matrix.manifest_sha256,
            )
        else:
            custody_bound = (
                matrix.private_suite_manifest_sha256
                == private_custody.private_suite_manifest_sha256
            )
            custody_passed = custody_bound and private_custody.verdict is GateVerdict.PASS
            add(
                criterion_id="private_custody",
                category="custody",
                relation="==",
                expected="bound/pass",
                observed=(f"bound={custody_bound},verdict={private_custody.verdict.value}"),
                passed=custody_passed,
                detail="The F7 private-suite custody audit must pass for this exact suite.",
                evidence_sha256=private_custody.evidence_sha256,
            )

    for name, category, relation, expected, observed, passed, detail in _measured_threshold_checks(
        report, config.thresholds
    ):
        add(
            criterion_id=name,
            category=category,  # type: ignore[arg-type]
            relation=relation,  # type: ignore[arg-type]
            expected=expected,
            observed=observed,
            passed=passed,
            detail=detail,
        )
    statuses = {criterion.status for criterion in criteria}
    verdict = (
        GateVerdict.BLOCKED
        if K3CriterionStatus.BLOCKED in statuses
        else GateVerdict.FAIL
        if K3CriterionStatus.FAIL in statuses
        else GateVerdict.PASS
    )
    return report, K3HiddenWorldScientificExitDecision(
        acceptance_config_sha256=config.config_sha256,
        test_matrix_manifest_sha256=matrix.manifest_sha256,
        aggregate_report_sha256=report.report_sha256,
        private_custody_evidence_sha256=(
            private_custody.evidence_sha256 if private_custody is not None else None
        ),
        criteria=tuple(criteria),
        verdict=verdict,
        decided_at=timestamp,
    )
