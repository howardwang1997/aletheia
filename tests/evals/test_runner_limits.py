"""F7-S2 runner limits and attempt isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aletheia.evals.runner import EvaluationRunnerError, IndependentEvaluationRunner
from aletheia.evals.sandbox import LocalProcessEvaluationExecutor
from aletheia.evals.schemas import (
    AttemptStatus,
    EvaluationAttemptSlot,
    EvaluationRunPlan,
    ExecutionExitReason,
    InvalidReason,
)
from .f7s2_fixtures import HardExecutor, build_case, write_submission


class FailIfScored:
    @property
    def scorer_sha256(self):
        from .f7s2_fixtures import SCORER_HASH

        return SCORER_HASH

    def score(self, **_kwargs):
        raise AssertionError("hidden scorer must not run after a trusted budget overage")


def test_successful_attempt_has_isolated_manifest_usage_and_signed_score(tmp_path):
    executor = HardExecutor(
        write_submission,
        output=b"retained-tail",
        output_total_bytes=999,
    )
    runner, suite, plan, task, ledger = build_case(tmp_path, executor=executor)

    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is AttemptStatus.COMPLETED
    assert outcome.execution_receipt is not None
    assert outcome.execution_receipt.stdout_total_bytes == 999
    assert outcome.execution_receipt.stdout_truncated is True
    assert outcome.scorer_receipt is not None
    outcome.scorer_receipt.verify(
        key=runner.receipt_signing_key, expected_key_id=runner.receipt_key_id
    )
    request = json.loads(executor.contexts[0].request_path.read_text())
    serialized = json.dumps(request)
    assert "hidden_asset" not in serialized
    assert "scorer" not in serialized
    assert outcome.research_workspace != outcome.submission_inbox
    assert outcome.evaluator_attempt_workspace not in outcome.research_workspace.parents
    assert (outcome.evaluator_attempt_workspace / "attempt_manifest.v1.json").exists()
    assert (outcome.evaluator_attempt_workspace / "execution_receipt.v1.json").exists()
    assert (outcome.evaluator_attempt_workspace / "scorer_receipt.signed.v1.json").exists()
    assert ledger.assert_integrity()["events"] >= 8


@pytest.mark.parametrize(
    ("reason", "returncode", "expected"),
    [
        (ExecutionExitReason.WALL_TIME_LIMIT, None, AttemptStatus.TIMEOUT),
        (ExecutionExitReason.RESOURCE_LIMIT, 137, AttemptStatus.INVALID),
        (ExecutionExitReason.PROCESS_ERROR, 7, AttemptStatus.INVALID),
    ],
)
def test_limits_and_authored_process_error_are_terminal_not_infra(
    tmp_path, reason, returncode, expected
):
    executor = HardExecutor(reason=reason, returncode=returncode)
    runner, suite, plan, task, _ledger = build_case(tmp_path, executor=executor)

    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is expected
    assert outcome.attempt.status is not AttemptStatus.INFRA_FAILURE


def test_formal_runner_rejects_host_process_executor(tmp_path):
    local = LocalProcessEvaluationExecutor(("true",))
    runner, suite, plan, task, ledger = build_case(tmp_path)
    formal = IndependentEvaluationRunner(
        root=runner.root,
        ledger=ledger,
        executor=local,
        scorer=runner.scorer,
        evaluator_manifest_sha256=runner.evaluator_manifest_sha256,
        receipt_key_id=runner.receipt_key_id,
        receipt_signing_key=runner.receipt_signing_key,
    )

    with pytest.raises(EvaluationRunnerError, match="hard executor"):
        formal.run(suite=suite, plan=plan, task=task, repeat_index=0)


def test_unmetered_executor_rejects_token_or_usd_budget(tmp_path):
    runner, suite, plan, task, _ledger = build_case(tmp_path, token_cap=100)
    with pytest.raises(EvaluationRunnerError, match="usage receipt"):
        runner.run(suite=suite, plan=plan, task=task, repeat_index=0)


def test_trusted_usage_overage_invalidates_scientific_success(tmp_path):
    executor = HardExecutor(
        write_submission,
        input_tokens=80,
        output_tokens=30,
        cost_usd=0.25,
        usage_metering="provider_receipt",
    )
    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=executor, token_cap=100, usd_cap=1.0
    )

    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is AttemptStatus.INVALID
    assert outcome.scorer_receipt is not None
    assert outcome.scorer_receipt.receipt.score.invalid_reasons == (
        InvalidReason.RESOURCE_LIMIT,
    )


def test_trusted_usage_overage_does_not_consume_hidden_scorer_access(tmp_path):
    executor = HardExecutor(
        write_submission,
        input_tokens=80,
        output_tokens=30,
        usage_metering="provider_receipt",
    )
    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=executor, scorer=FailIfScored(), token_cap=100
    )

    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)

    assert outcome.attempt.status is AttemptStatus.INVALID
    assert outcome.scorer_receipt.receipt.score.invalid_reasons == (
        InvalidReason.RESOURCE_LIMIT,
    )


def test_each_pre_registered_repeat_gets_a_distinct_workspace(tmp_path):
    executor = HardExecutor(write_submission)
    runner, suite, plan, task, _ledger = build_case(tmp_path, executor=executor, repeats=2)
    one = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    two = runner.run(suite=suite, plan=plan, task=task, repeat_index=1)

    assert one.research_workspace != two.research_workspace
    assert one.submission_inbox != two.submission_inbox
    assert one.attempt.seed != two.attempt.seed
    assert one.attempt.status is two.attempt.status is AttemptStatus.COMPLETED


def test_plan_cannot_exceed_hidden_test_access_limit(tmp_path):
    runner, suite, plan, task, _ledger = build_case(tmp_path)
    expanded = EvaluationRunPlan(
        plan_id="expanded-hidden-access-plan",
        suite_manifest_sha256=plan.suite_manifest_sha256,
        system_manifest_sha256=plan.system_manifest_sha256,
        evaluator_manifest_sha256=plan.evaluator_manifest_sha256,
        slots=(
            *plan.slots,
            EvaluationAttemptSlot(
                task_manifest_sha256=task.manifest_sha256, repeat_index=1, seed=1001
            ),
        ),
    )
    with pytest.raises(EvaluationRunnerError, match="access limit"):
        runner.run(suite=suite, plan=expanded, task=task, repeat_index=0)


def test_artifact_path_traversal_and_symlink_are_invalid(tmp_path):
    def traversal(context):
        write_submission(context, uri="inbox://../hidden_assets/task-1.json")

    runner, suite, plan, task, _ledger = build_case(
        tmp_path, executor=HardExecutor(traversal), repeats=2
    )
    first = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert first.attempt.status is AttemptStatus.INVALID
    assert "escaped" in (first.detail or "")

    def symlink(context):
        outside = context.research_workspace / "outside.json"
        outside.write_text('{"answer":"42"}')
        (context.submission_inbox / "answer.json").symlink_to(outside)
        write_submission(context)
        (context.submission_inbox / "answer.json").unlink()
        (context.submission_inbox / "answer.json").symlink_to(outside)

    runner.executor = HardExecutor(symlink)
    second = runner.run(suite=suite, plan=plan, task=task, repeat_index=1)
    assert second.attempt.status is AttemptStatus.INVALID
    assert "symlink" in (second.detail or "")


def test_submission_with_wrong_media_type_or_hash_is_invalid(tmp_path):
    runner, suite, plan, task, _ledger = build_case(
        tmp_path,
        executor=HardExecutor(lambda context: write_submission(context, media_type="text/plain")),
        repeats=2,
    )
    wrong_type = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert wrong_type.attempt.status is AttemptStatus.INVALID

    runner.executor = HardExecutor(
        lambda context: write_submission(context, sha256_override="f" * 64)
    )
    wrong_hash = runner.run(suite=suite, plan=plan, task=task, repeat_index=1)
    assert wrong_hash.attempt.status is AttemptStatus.INVALID
    assert "hash" in (wrong_hash.detail or "")


def test_public_request_is_not_written_into_authored_workspace(tmp_path):
    executor = HardExecutor(write_submission)
    runner, suite, plan, task, _ledger = build_case(tmp_path, executor=executor)
    runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    context = executor.contexts[0]
    assert context.request_path.parent != context.research_workspace
    assert context.request_path.parent.name == context.request.attempt_id
    assert context.request_path.stat().st_mode & 0o222 == 0
    assert not (context.research_workspace / "request.json").exists()


def test_evaluator_agent_image_never_copies_repository(tmp_path):
    dockerfile = Path("docker/evaluator-agent.Dockerfile").read_text()
    assert "COPY aletheia" not in dockerfile
    assert "COPY ." not in dockerfile
