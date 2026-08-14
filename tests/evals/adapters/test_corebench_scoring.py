"""Objective correctness, contamination, reproducibility, and resource verdicts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from aletheia.evals.adapters.corebench import (
    COREBENCH_CANARY,
    CoreBenchAssetReceipt,
    CoreBenchHarnessManifest,
    CoreBenchHarnessResult,
    CoreBenchScorer,
    CoreBenchSourceManifest,
)
from aletheia.evals.schemas import (
    EvaluationSubmission,
    EvaluationTask,
    ExecutionExitReason,
    InvalidReason,
    SubmittedArtifact,
)

from .test_corebench_contract import ENVIRONMENT, ONE_IMAGE, ZERO_IMAGE, instance


def source() -> CoreBenchSourceManifest:
    return CoreBenchSourceManifest(
        astabench_commit="1" * 40,
        astabench_core_wrapper_sha256="2" * 64,
        inspect_evals_commit="3" * 40,
        inspect_evals_scorer_sha256="4" * 64,
        inspect_evals_utils_sha256="5" * 64,
        dataset_revision="6" * 40,
        annotation_sha256="7" * 64,
        annotation_rows=1,
    )


def receipt(release: CoreBenchSourceManifest) -> CoreBenchAssetReceipt:
    return CoreBenchAssetReceipt(
        source_manifest_sha256=release.manifest_sha256,
        instance=instance(),
        source_archive_sha256="8" * 64,
        source_archive_bytes=100,
        source_archive_files=8,
        source_expanded_bytes=500,
        public_archive_sha256="9" * 64,
        public_archive_bytes=80,
        public_file_count=4,
        public_expanded_bytes=300,
        public_tree_sha256="a" * 64,
        code_license_sha256="b" * 64,
        data_license_sha256="c" * 64,
        environment_dockerfile_sha256="d" * 64,
    )


def task() -> EvaluationTask:
    return EvaluationTask(
        task_id="corebench-hard-0000001",
        version="test",
        layer=2,
        public_prompt="Reproduce and report.",
        hidden_asset_ref="evaluator://hidden/corebench/1.json",
        hidden_asset_sha256="e" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=(
            {
                "kind": "reproduction_program",
                "media_type": "text/x-python",
                "max_bytes": 1 << 20,
            },
        ),
        scorer_ref="evaluator://scorers/core",
        scorer_sha256="f" * 64,
    )


def submission(program: bytes, *, contamination: tuple[str, ...] = ()) -> EvaluationSubmission:
    return EvaluationSubmission(
        attempt_id="attempt-1",
        task_manifest_sha256="a" * 64,
        system_manifest_sha256="b" * 64,
        artifacts=(
            SubmittedArtifact(
                kind="reproduction_program",
                media_type="text/x-python",
                uri="inbox://reproduce.py",
                sha256=hashlib.sha256(program).hexdigest(),
                bytes=len(program),
            ),
        ),
        submitted_at=datetime.now(timezone.utc),
        declared_contamination=contamination,
    )


@dataclass
class ScriptedHarness:
    results: list[dict]
    calls: list[int] = field(default_factory=list)

    @property
    def manifest(self) -> CoreBenchHarnessManifest:
        return CoreBenchHarnessManifest(
            source_manifest_sha256=source().manifest_sha256,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            scorer_entrypoint_sha256="1" * 64,
            candidate_environment=ENVIRONMENT,
            supported_capsule_requirements={"capsule-0000001": ("numpy",)},
            reproduction_runs=len(self.results),
        )

    def evaluate(self, *, receipt, program, run_index):
        self.calls.append(run_index)
        payload = self.results[run_index]
        return CoreBenchHarnessResult(
            capsule_id=receipt.instance.capsule_id,
            run_index=run_index,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            program_log_sha256=hashlib.sha256(program + str(run_index).encode()).hexdigest(),
            program_wall_time_s=0.01,
            **payload,
        )


def outcome(
    *,
    correct: bool,
    report: str | None = '{"Report the fitted slope.":2}',
    artifact: str | None = "reproduced-output",
    exit_reason: ExecutionExitReason = ExecutionExitReason.COMPLETED,
) -> dict:
    return {
        "program_returncode": 0 if exit_reason is ExecutionExitReason.COMPLETED else 1,
        "program_exit_reason": exit_reason,
        "program_timed_out": exit_reason is ExecutionExitReason.WALL_TIME_LIMIT,
        "report_valid": report is not None,
        "report_sha256": hashlib.sha256(report.encode()).hexdigest() if report else None,
        "report_bytes": len(report.encode()) if report else 0,
        "artifact_tree_sha256": hashlib.sha256(artifact.encode()).hexdigest() if artifact else None,
        "artifact_file_count": 1 if artifact else 0,
        "artifact_total_bytes": len(artifact.encode()) if artifact else 0,
        "correct_written_answers": 1 if correct else 0,
        "total_written_questions": 1,
        "correct": correct,
    }


def run_score(results: list[dict], program: bytes = b"print('reproduce')\n", *, declared=()):
    release = source()
    harness = ScriptedHarness(results)
    scorer = CoreBenchScorer(harness=harness, source_manifest_sha256=release.manifest_sha256)
    score = scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program, contamination=declared),
        artifacts={"reproduction_program": program},
    )
    return score, harness


def test_correct_report_and_artifact_are_scientific_success():
    same = outcome(correct=True)
    score, harness = run_score([same, same])
    assert score.scientific_success is True
    assert score.objective_scores == {
        "runnable": 1.0,
        "valid_report": 1.0,
        "question_accuracy": 1.0,
        "artifact_present": 1.0,
        "reproducible": 1.0,
    }
    assert harness.calls == [0, 1]
    assert set(score.evidence_objects) == {"harness_run_0", "harness_run_1"}


def test_numerically_wrong_is_scientific_false_not_invalid():
    same = outcome(correct=False, report='{"Report the fitted slope.":9}')
    score, _ = run_score([same, same])
    assert score.scientific_success is False
    assert score.invalid_reasons == ()
    assert score.objective_scores["question_accuracy"] == 0


@pytest.mark.parametrize(
    "program",
    [
        f"print('{COREBENCH_CANARY}')\n".encode(),
        b"open('results/answer.txt').read()\n",
        b"open('REPRODUCING.md').read()\n",
        b"open('core_train.json').read()\n",
        b"print('COREBENCH_GOLD_PATH')\n",
    ],
)
def test_peeking_and_canary_are_contamination_without_harness_access(program):
    score, harness = run_score([outcome(correct=True)] * 2, program)
    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert score.scientific_success is None
    assert harness.calls == []


def test_declared_overlap_is_contamination_without_harness_access():
    score, harness = run_score(
        [outcome(correct=True)] * 2, declared=("memorized CORE-Bench validation answers",)
    )
    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert harness.calls == []


def test_missing_submitted_artifact_is_invalid():
    release = source()
    harness = ScriptedHarness([outcome(correct=True)] * 2)
    scorer = CoreBenchScorer(harness=harness, source_manifest_sha256=release.manifest_sha256)
    score = scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(b"print('x')"),
        artifacts={},
    )
    assert score.invalid_reasons == (InvalidReason.MISSING_ARTIFACT,)
    assert harness.calls == []


def test_missing_report_or_reproduction_artifact_is_scientific_false():
    no_report = outcome(correct=False, report=None)
    score, _ = run_score([no_report, no_report])
    assert score.scientific_success is False
    assert score.objective_scores["valid_report"] == 0

    no_artifact = outcome(correct=True, artifact=None)
    score, _ = run_score([no_artifact, no_artifact])
    assert score.scientific_success is False
    assert score.objective_scores["artifact_present"] == 0


def test_different_reports_or_artifacts_are_non_reproducible_no_best_of_two():
    one = outcome(correct=True)
    two = outcome(correct=False, report='{"Report the fitted slope.":9}', artifact="other")
    score, harness = run_score([one, two])
    assert score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)
    assert score.scientific_success is None
    assert harness.calls == [0, 1]


@pytest.mark.parametrize(
    "reason", [ExecutionExitReason.WALL_TIME_LIMIT, ExecutionExitReason.RESOURCE_LIMIT]
)
def test_resource_limit_is_invalid(reason):
    limited = outcome(correct=False, report=None, artifact=None, exit_reason=reason)
    score, _ = run_score([limited, limited])
    assert score.invalid_reasons == (InvalidReason.RESOURCE_LIMIT,)
    assert score.scientific_success is None


def test_authored_process_failure_is_scientific_false_not_infrastructure():
    failed = outcome(
        correct=False, report=None, artifact=None, exit_reason=ExecutionExitReason.PROCESS_ERROR
    )
    score, _ = run_score([failed, failed])
    assert score.invalid_reasons == ()
    assert score.scientific_success is False
    assert score.objective_scores["runnable"] == 0
