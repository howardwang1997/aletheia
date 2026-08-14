"""F7-S2 evaluator-owned retry semantics and best-of-N resistance."""

from __future__ import annotations

import threading

import pytest
from pydantic import ValidationError

from aletheia.evals.ledger import EvaluationLedgerError
from aletheia.evals.runner import (
    EvaluationRunnerError,
    EvaluationScorerInfrastructureError,
)
from aletheia.evals.schemas import AttemptStatus, ExecutionExitReason
from aletheia.evals.schemas import EvaluationAttemptSlot, EvaluationRunPlan
from .f7s2_fixtures import (
    ExactAnswerScorer,
    HardExecutor,
    build_case,
    infra_error,
    write_submission,
)


def test_infrastructure_failure_can_retry_once_and_preserves_original(tmp_path):
    failing = HardExecutor(error=infra_error())
    runner, suite, plan, task, ledger = build_case(tmp_path, executor=failing)
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert first.attempt.status is AttemptStatus.INFRA_FAILURE

    runner.executor = HardExecutor(write_submission)
    retry = runner.run(
        suite=suite,
        plan=plan,
        task=task,
        repeat_index=0,
        retry_of_attempt_id=first.attempt.attempt_id,
    )

    assert retry.attempt.status is AttemptStatus.COMPLETED
    assert retry.attempt.retry_of_attempt_id == first.attempt.attempt_id
    assert retry.attempt.seed == first.attempt.seed
    assert ledger.terminal_attempt(first.attempt.attempt_id).status is AttemptStatus.INFRA_FAILURE
    created = ledger.slot_attempts(
        plan_sha256=plan.manifest_sha256, slot_sha256=plan.slots[0].slot_sha256
    )
    assert [attempt.attempt_id for attempt in created] == [
        first.attempt.attempt_id,
        retry.attempt.attempt_id,
    ]


@pytest.mark.parametrize(
    ("answer", "reason", "returncode"),
    [
        ("wrong", ExecutionExitReason.COMPLETED, 0),
        (None, ExecutionExitReason.PROCESS_ERROR, 9),
        (None, ExecutionExitReason.WALL_TIME_LIMIT, None),
    ],
)
def test_scientific_failure_process_error_and_timeout_cannot_request_retry(
    tmp_path, answer, reason, returncode
):
    action = (lambda context: write_submission(context, answer=answer)) if answer else None
    runner, suite, plan, task, _ledger = build_case(
        tmp_path,
        executor=HardExecutor(action, reason=reason, returncode=returncode),
    )
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert first.attempt.status is not AttemptStatus.INFRA_FAILURE

    runner.executor = HardExecutor(write_submission)
    with pytest.raises((EvaluationRunnerError, EvaluationLedgerError), match="infrastructure"):
        runner.run(
            suite=suite,
            plan=plan,
            task=task,
            repeat_index=0,
            retry_of_attempt_id=first.attempt.attempt_id,
        )


def test_scorer_can_authorize_retry_only_with_explicit_infrastructure_exception(tmp_path):
    scorer = ExactAnswerScorer(fail=EvaluationScorerInfrastructureError("scorer host lost"))
    runner, suite, plan, task, _ledger = build_case(tmp_path, scorer=scorer)
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert first.attempt.status is AttemptStatus.INFRA_FAILURE

    runner.scorer = ExactAnswerScorer()
    retry = runner.run(
        suite=suite,
        plan=plan,
        task=task,
        repeat_index=0,
        retry_of_attempt_id=first.attempt.attempt_id,
    )
    assert retry.attempt.status is AttemptStatus.COMPLETED


def test_generic_scorer_bug_is_invalid_and_not_retryable(tmp_path):
    scorer = ExactAnswerScorer(fail=ValueError("bad scorer implementation"))
    runner, suite, plan, task, _ledger = build_case(tmp_path, scorer=scorer)
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert first.attempt.status is AttemptStatus.INVALID

    with pytest.raises(EvaluationRunnerError, match="infrastructure"):
        runner.run(
            suite=suite,
            plan=plan,
            task=task,
            repeat_index=0,
            retry_of_attempt_id=first.attempt.attempt_id,
        )


def test_a_successful_retry_cannot_be_retried_or_restarted(tmp_path):
    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=HardExecutor(error=infra_error())
    )
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    runner.executor = HardExecutor(write_submission)
    retry = runner.run(
        suite=suite,
        plan=plan,
        task=task,
        repeat_index=0,
        retry_of_attempt_id=first.attempt.attempt_id,
    )

    with pytest.raises(EvaluationRunnerError):
        runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    with pytest.raises(EvaluationRunnerError, match="infrastructure"):
        runner.run(
            suite=suite,
            plan=plan,
            task=task,
            repeat_index=0,
            retry_of_attempt_id=retry.attempt.attempt_id,
        )


def test_concurrent_slot_claim_has_exactly_one_winner(tmp_path):
    runner, _suite, plan, task, ledger = build_case(tmp_path)
    slot = plan.slots[0]
    attempts = [
        runner._new_attempt(  # noqa: SLF001 - exercise the atomic ledger primitive directly.
            plan=plan,
            task=task,
            slot=slot,
            retry_of=None,
            intervention_count=0,
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)
    winners: list[str] = []
    failures: list[Exception] = []

    def claim(attempt):
        barrier.wait()
        try:
            ledger.claim_attempt(
                attempt,
                slot_sha256=slot.slot_sha256,
                retry_of_attempt_id=None,
                max_infra_retries=1,
            )
            winners.append(attempt.attempt_id)
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=claim, args=(attempt,)) for attempt in attempts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(failures) == 1
    assert "already has an attempt" in str(failures[0])


def test_tampered_or_truncated_ledger_fails_closed(tmp_path):
    runner, suite, plan, task, ledger = build_case(tmp_path)
    runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    raw = ledger.path.read_text()
    ledger.path.chmod(0o600)
    ledger.path.write_text(raw.replace('"completed"', '"running"', 1))
    with pytest.raises(EvaluationLedgerError):
        ledger.events()


def test_repeat_plan_rejects_seed_reuse_and_index_gaps(tmp_path):
    _runner, _suite, plan, task, _ledger = build_case(tmp_path)
    base = dict(
        plan_id="invalid-repeat-plan",
        suite_manifest_sha256=plan.suite_manifest_sha256,
        system_manifest_sha256=plan.system_manifest_sha256,
        evaluator_manifest_sha256=plan.evaluator_manifest_sha256,
    )
    with pytest.raises(ValidationError, match="unique seeds"):
        EvaluationRunPlan(
            **base,
            slots=(
                EvaluationAttemptSlot(
                    task_manifest_sha256=task.manifest_sha256, repeat_index=0, seed=5
                ),
                EvaluationAttemptSlot(
                    task_manifest_sha256=task.manifest_sha256, repeat_index=1, seed=5
                ),
            ),
        )
    with pytest.raises(ValidationError, match="contiguous"):
        EvaluationRunPlan(
            **base,
            slots=(
                EvaluationAttemptSlot(
                    task_manifest_sha256=task.manifest_sha256, repeat_index=1, seed=5
                ),
            ),
        )


def test_same_system_suite_cannot_register_a_second_plan_for_more_attempts(tmp_path):
    runner, suite, plan, task, ledger = build_case(tmp_path)
    runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    second = plan.model_copy(update={"plan_id": "best-of-n-second-plan"})
    with pytest.raises(EvaluationLedgerError, match="another run plan"):
        ledger.register_plan(second)
