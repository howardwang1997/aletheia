"""F7-S1 evaluation schemas bind every result to exact content identities."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.evals.schemas import (
    ArtifactRequirement,
    AttemptStatus,
    EvaluationAttempt,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationTask,
    EvalLayer,
    InvalidReason,
    ResourceBudget,
    ScorerReceipt,
    SubmittedArtifact,
)

H = "a" * 64
P = "b" * 64


def _task():
    return EvaluationTask(
        task_id="hidden-rule-1",
        version="1.0.0",
        layer=EvalLayer.HIDDEN_RULE_DISCOVERY,
        public_prompt="Discover the governing rule using experiments.",
        hidden_asset_ref="evaluator://private/task-1",
        hidden_asset_sha256=H,
        resource_budget=ResourceBudget(wall_time_s=60, cpu_seconds=30, memory_mb=512),
        expected_artifacts=(
            ArtifactRequirement(kind="answer", media_type="application/json", max_bytes=4096),
        ),
        scorer_ref="evaluator://scorers/hidden-rule-v1",
        scorer_sha256=H,
    )


def test_public_task_view_cannot_leak_hidden_asset_or_scorer():
    task = _task()
    public = task.public_view().model_dump(mode="json")
    assert "hidden_asset_ref" not in public
    assert "hidden_asset_sha256" not in public
    assert "scorer_ref" not in public
    assert "scorer_sha256" not in public
    assert public["task_manifest_sha256"] == task.manifest_sha256


def test_task_rejects_duplicate_artifact_contracts():
    kwargs = _task().model_dump()
    kwargs["expected_artifacts"] = (
        ArtifactRequirement(kind="answer", media_type="text/plain", max_bytes=5),
        ArtifactRequirement(kind="answer", media_type="application/json", max_bytes=5),
    )
    with pytest.raises(ValidationError, match="must be unique"):
        EvaluationTask(**kwargs)


def test_submission_hash_binds_artifact_bytes():
    base = dict(
        attempt_id="attempt-1",
        task_manifest_sha256=H,
        system_manifest_sha256=H,
        submitted_at=datetime.now(timezone.utc),
    )
    one = EvaluationSubmission(
        **base,
        artifacts=(
            SubmittedArtifact(
                kind="answer",
                media_type="application/json",
                uri="inbox/a",
                sha256=H,
                bytes=10,
            ),
        ),
    )
    two = EvaluationSubmission(
        **base,
        artifacts=(
            SubmittedArtifact(
                kind="answer",
                media_type="application/json",
                uri="inbox/a",
                sha256="b" * 64,
                bytes=10,
            ),
        ),
    )
    assert one.submission_sha256 != two.submission_sha256


def test_invalid_attempt_cannot_receive_scientific_verdict():
    with pytest.raises(ValidationError, match="cannot also receive"):
        EvaluationScore(
            invalid_reasons=(InvalidReason.PROTOCOL_BREACH,), scientific_success=False
        )


def test_valid_negative_result_is_distinct_from_invalid():
    score = EvaluationScore(scientific_success=False)
    assert score.invalid_reasons == ()
    assert score.scientific_success is False


def test_score_evidence_object_must_match_its_declared_hash():
    evidence = {"value": 1}
    from aletheia.evals.schemas import content_sha256

    score = EvaluationScore(
        evidence_sha256s={"run": content_sha256(evidence)},
        evidence_objects={"run": evidence},
        scientific_success=False,
    )
    assert score.evidence_objects["run"] == evidence
    with pytest.raises(ValidationError, match="does not match"):
        EvaluationScore(
            evidence_sha256s={"run": H},
            evidence_objects={"run": evidence},
            scientific_success=False,
        )


def test_scorer_receipt_cannot_be_replayed_onto_another_submission():
    submission = EvaluationSubmission(
        attempt_id="attempt-1",
        task_manifest_sha256=H,
        system_manifest_sha256=H,
        artifacts=(
            SubmittedArtifact(
                kind="answer",
                media_type="application/json",
                uri="inbox/a",
                sha256=H,
                bytes=10,
            ),
        ),
        submitted_at=datetime.now(timezone.utc),
    )
    receipt = ScorerReceipt(
        attempt_id="attempt-1",
        run_plan_sha256=P,
        attempt_manifest_sha256=H,
        task_manifest_sha256=H,
        system_manifest_sha256=H,
        submission_sha256=submission.submission_sha256,
        execution_receipt_sha256=H,
        scorer_sha256=H,
        evaluator_manifest_sha256=H,
        score=EvaluationScore(scientific_success=True),
        scored_at=datetime.now(timezone.utc),
    )
    receipt.verify_submission(submission)
    forged = submission.model_copy(update={"attempt_id": "attempt-2"})
    with pytest.raises(ValueError, match="attempt_id"):
        receipt.verify_submission(forged)


def test_infrastructure_retry_lineage_survives_a_successful_retry():
    retry = EvaluationAttempt(
        attempt_id="attempt-2",
        suite_manifest_sha256=H,
        run_plan_sha256=P,
        task_manifest_sha256=H,
        system_manifest_sha256=H,
        repeat_index=0,
        seed=7,
        status=AttemptStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        retry_of_attempt_id="attempt-1",
        retry_reason="infra_failure",
    )
    assert retry.retry_of_attempt_id == "attempt-1"
    assert len(retry.attempt_sha256) == 64


def test_retry_without_evaluator_authorized_reason_is_invalid():
    with pytest.raises(ValidationError, match="both required"):
        EvaluationAttempt(
            attempt_id="attempt-2",
            suite_manifest_sha256=H,
            run_plan_sha256=P,
            task_manifest_sha256=H,
            system_manifest_sha256=H,
            repeat_index=0,
            seed=7,
            retry_of_attempt_id="attempt-1",
        )
