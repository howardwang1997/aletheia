"""Trusted test doubles for F7-S2; none are valid production executors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.sandbox import (
    EvaluationExecution,
    EvaluationExecutionContext,
    EvaluationExecutorError,
)
from aletheia.evals.schemas import (
    ArtifactRequirement,
    ContaminationPolicy,
    EvaluationAttemptSlot,
    EvaluationRunPlan,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    EvalLayer,
    ExecutionExitReason,
    ExecutorContract,
    ResourceBudget,
    SubmittedArtifact,
)

SYSTEM_HASH = "1" * 64
EVALUATOR_HASH = "2" * 64
SCORER_HASH = "3" * 64
IMAGE_ID = "sha256:" + "4" * 64
SIGNING_KEY = b"evaluator-only-test-signing-key-32+"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExactAnswerScorer:
    fail: Exception | None = None

    @property
    def scorer_sha256(self) -> str:
        return SCORER_HASH

    def score(self, *, hidden_asset, artifacts, **_kwargs) -> EvaluationScore:
        if self.fail is not None:
            raise self.fail
        expected = json.loads(hidden_asset)["answer"]
        actual = json.loads(artifacts["answer"])["answer"]
        return EvaluationScore(
            objective_scores={"exact": float(actual == expected)},
            scientific_success=actual == expected,
        )


class HardExecutor:
    def __init__(
        self,
        action: Callable[[EvaluationExecutionContext], None] | None = None,
        *,
        reason: ExecutionExitReason = ExecutionExitReason.COMPLETED,
        returncode: int | None = 0,
        error: Exception | None = None,
        output: bytes = b"ok",
        output_total_bytes: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        usage_metering: str = "unavailable",
        tools: tuple[str, ...] = (),
    ) -> None:
        self.action = action
        self.reason = reason
        self.returncode = returncode
        self.error = error
        self.output = output
        self.output_total_bytes = output_total_bytes
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.contexts: list[EvaluationExecutionContext] = []
        self._contract = ExecutorContract(
            executor_id="test-hard-executor-v1",
            security_level="hard",
            sandbox_image_id=IMAGE_ID,
            exposed_tools=tools,
            usage_metering=usage_metering,
        )

    @property
    def contract(self) -> ExecutorContract:
        return self._contract

    def execute(self, context, _budget) -> EvaluationExecution:
        self.contexts.append(context)
        if self.error:
            raise self.error
        if self.action:
            self.action(context)
        started = utcnow()
        ended = utcnow()
        total = self.output_total_bytes if self.output_total_bytes is not None else len(self.output)
        return EvaluationExecution(
            returncode=self.returncode,
            output=self.output,
            output_total_bytes=total,
            output_truncated=total > len(self.output),
            started_at=started,
            ended_at=ended,
            wall_time_s=0.01,
            exit_reason=self.reason,
            timed_out=self.reason is ExecutionExitReason.WALL_TIME_LIMIT,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            infrastructure_detail=(str(self.error) if self.error else None),
        )


def write_submission(
    context: EvaluationExecutionContext,
    *,
    answer: str = "42",
    uri: str = "inbox://answer.json",
    sha256_override: str | None = None,
    attempt_id: str | None = None,
    media_type: str = "application/json",
) -> None:
    raw = json.dumps({"answer": answer}, sort_keys=True).encode()
    (context.submission_inbox / "answer.json").write_bytes(raw)
    artifact = SubmittedArtifact(
        kind="answer",
        media_type=media_type,
        uri=uri,
        sha256=sha256_override or hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
    )
    submission = EvaluationSubmission(
        attempt_id=attempt_id or context.request.attempt_id,
        task_manifest_sha256=context.request.public_task.task_manifest_sha256,
        system_manifest_sha256=context.request.system_manifest_sha256,
        artifacts=(artifact,),
        submitted_at=utcnow(),
    )
    (context.submission_inbox / "submission.json").write_text(submission.model_dump_json())


def build_case(
    tmp_path: Path,
    *,
    executor: HardExecutor | None = None,
    scorer: ExactAnswerScorer | None = None,
    repeats: int = 1,
    token_cap: int | None = None,
    usd_cap: float | None = None,
):
    root = tmp_path / "evaluation"
    hidden = root / "hidden_assets" / "task-1.json"
    hidden.parent.mkdir(parents=True)
    hidden_bytes = b'{"answer":"42"}'
    hidden.write_bytes(hidden_bytes)
    task = EvaluationTask(
        task_id="hidden-rule-1",
        version="1.0.0",
        layer=EvalLayer.HIDDEN_RULE_DISCOVERY,
        public_prompt="Return the hidden rule answer.",
        hidden_asset_ref="evaluator://hidden/task-1.json",
        hidden_asset_sha256=hashlib.sha256(hidden_bytes).hexdigest(),
        resource_budget=ResourceBudget(
            wall_time_s=10,
            cpu_seconds=5,
            memory_mb=128,
            token_cap=token_cap,
            usd_cap=usd_cap,
        ),
        expected_artifacts=(
            ArtifactRequirement(kind="answer", media_type="application/json", max_bytes=1024),
        ),
        scorer_ref="evaluator://scorers/exact-v1",
        scorer_sha256=SCORER_HASH,
        contamination_policy=ContaminationPolicy(test_access_limit=repeats),
    )
    suite = EvaluationSuite(
        suite_id="frontier-gate-test",
        version="1.0.0",
        task_manifest_sha256s=(task.manifest_sha256,),
        scoring_policy_sha256="5" * 64,
    )
    plan = EvaluationRunPlan(
        plan_id="frontier-gate-test-plan",
        suite_manifest_sha256=suite.manifest_sha256,
        system_manifest_sha256=SYSTEM_HASH,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        slots=tuple(
            EvaluationAttemptSlot(
                task_manifest_sha256=task.manifest_sha256,
                repeat_index=index,
                seed=1000 + index,
            )
            for index in range(repeats)
        ),
    )
    ledger = EvaluationLedger(root / "evaluator_ledger" / "events.jsonl")
    runner = IndependentEvaluationRunner(
        root=root,
        ledger=ledger,
        executor=executor or HardExecutor(write_submission),
        scorer=scorer or ExactAnswerScorer(),
        evaluator_manifest_sha256=EVALUATOR_HASH,
        receipt_key_id="test-evaluator-key",
        receipt_signing_key=SIGNING_KEY,
    )
    return runner, suite, plan, task, ledger


def infra_error(message: str = "daemon unavailable") -> EvaluationExecutorError:
    return EvaluationExecutorError(message)
