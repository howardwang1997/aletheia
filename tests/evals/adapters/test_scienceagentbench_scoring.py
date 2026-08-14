"""Six required F7-S3 outcomes for the ScienceAgentBench adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from aletheia.evals.adapters.scienceagentbench import (
    OFFICIAL_REPOSITORY_COMMIT,
    SCIENCEAGENTBENCH_CANARY,
    ScienceAgentBenchAssetReceipt,
    ScienceAgentBenchHarnessManifest,
    ScienceAgentBenchHarnessResult,
    ScienceAgentBenchInstance,
    ScienceAgentBenchScorer,
    ScienceAgentBenchSourceManifest,
)
from aletheia.evals.schemas import (
    EvaluationSubmission,
    EvaluationTask,
    ExecutionExitReason,
    InvalidReason,
    SubmittedArtifact,
)

ZERO_IMAGE = "sha256:" + "0" * 64
ONE_IMAGE = "sha256:" + "1" * 64
ENVIRONMENT = {
    "python": "3.11.0",
    "numpy": "1",
    "pandas": "1",
    "scikit-learn": "1",
    "scipy": "1",
    "rdkit": "1",
    "geopandas": "1",
    "neurokit2": "1",
}


def instance() -> ScienceAgentBenchInstance:
    return ScienceAgentBenchInstance(
        instance_id="1",
        domain="Bioinformatics",
        subtask_categories="Regression",
        github_name="example/public-science",
        task_inst="Fit a linear relation.",
        domain_knowledge="Use the provided observations.",
        dataset_folder_tree="|-- tiny/\n|---- observations.csv",
        dataset_preview="x,y\n1,3\n2,5",
        src_file_or_path="examples/tiny",
        gold_program_name="tiny.py",
        output_fname="pred_results/result.json",
        eval_script_name="tiny_eval.py",
    )


def source() -> ScienceAgentBenchSourceManifest:
    return ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="json",
        annotation_sha256="3" * 64,
        annotation_rows=1,
    )


def receipt(release: ScienceAgentBenchSourceManifest) -> ScienceAgentBenchAssetReceipt:
    return ScienceAgentBenchAssetReceipt(
        source_manifest_sha256=release.manifest_sha256,
        benchmark_archive_sha256="4" * 64,
        instance=instance(),
        task_license="CC-BY-4.0",
        dataset_roots=("tiny",),
        dataset_tree_sha256s={"tiny": "5" * 64},
        dataset_file_count=1,
        dataset_total_bytes=16,
        eval_program_sha256="6" * 64,
    )


def task() -> EvaluationTask:
    return EvaluationTask(
        task_id="scienceagentbench-1",
        version="test",
        layer=2,
        public_prompt="Fit the relation.",
        hidden_asset_ref="evaluator://hidden/sab/1.json",
        hidden_asset_sha256="7" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=(
            {"kind": "program", "media_type": "text/x-python", "max_bytes": 1 << 20},
        ),
        scorer_ref="evaluator://scorers/sab",
        scorer_sha256="8" * 64,
    )


def submission(program: bytes, *, contamination: tuple[str, ...] = ()) -> EvaluationSubmission:
    return EvaluationSubmission(
        attempt_id="attempt-1",
        task_manifest_sha256="7" * 64,
        system_manifest_sha256="8" * 64,
        artifacts=(
            SubmittedArtifact(
                kind="program",
                media_type="text/x-python",
                uri="inbox://program.py",
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
    def manifest(self) -> ScienceAgentBenchHarnessManifest:
        return ScienceAgentBenchHarnessManifest(
            official_repository_commit=OFFICIAL_REPOSITORY_COMMIT,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            scorer_entrypoint_sha256="9" * 64,
            candidate_environment=ENVIRONMENT,
            scorer_environment=ENVIRONMENT,
            supported_instance_requirements={"1": ("scikit-learn",)},
            reproduction_runs=len(self.results),
        )

    def evaluate(self, *, receipt, program, run_index):
        self.calls.append(run_index)
        payload = self.results[run_index]
        return ScienceAgentBenchHarnessResult(
            instance_id=receipt.instance.instance_id,
            run_index=run_index,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            program_log_sha256=hashlib.sha256(program + str(run_index).encode()).hexdigest(),
            program_exit_reason=(
                ExecutionExitReason.COMPLETED
                if payload["program_returncode"] == 0
                else ExecutionExitReason.PROCESS_ERROR
            ),
            program_wall_time_s=0.01,
            **payload,
        )


def outcome(*, valid: bool, success: float, output: str | None) -> dict:
    return {
        "valid_program": valid,
        "success_rate": success,
        "program_returncode": 0 if valid else 1,
        "output_sha256": hashlib.sha256(output.encode()).hexdigest() if output else None,
        "output_bytes": len(output.encode()) if output else 0,
    }


def run_score(results: list[dict], program: bytes = b"print('candidate')\n"):
    release = source()
    harness = ScriptedHarness(results)
    benchmark_scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=release.manifest_sha256
    )
    score = benchmark_scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program),
        artifacts={"program": program},
    )
    return score, harness


def test_correct_program_is_scientific_success_and_keeps_every_run_receipt():
    same = outcome(valid=True, success=1, output='{"slope":2}')
    score, harness = run_score([same, same])

    assert score.scientific_success is True
    assert score.objective_scores == {
        "valid_program": 1.0,
        "success_rate": 1.0,
        "reproducible": 1.0,
    }
    assert harness.calls == [0, 1]
    assert set(score.evidence_sha256s) == {
        "submitted_program",
        "harness_run_0",
        "harness_run_1",
    }


def test_numerically_wrong_program_is_scientific_false_not_invalid():
    same = outcome(valid=True, success=0, output='{"slope":9}')
    score, _harness = run_score([same, same])

    assert score.scientific_success is False
    assert score.invalid_reasons == ()
    assert score.objective_scores["valid_program"] == 1
    assert score.objective_scores["success_rate"] == 0


@pytest.mark.parametrize(
    "program",
    [
        f"print('{SCIENCEAGENTBENCH_CANARY}')\n".encode(),
        b"from pathlib import Path\nprint(Path('/benchmark/gold_programs/tiny.py'))\n",
        b"open('eval_programs/tiny_eval.py').read()\n",
        b"open('scoring_rubrics/answer.txt').read()\n",
    ],
)
def test_peeking_or_canary_submission_is_contamination_and_never_scored(program):
    same = outcome(valid=True, success=1, output="ok")
    score, harness = run_score([same, same], program)

    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert score.scientific_success is None
    assert harness.calls == []


def test_declared_contamination_is_invalid_and_never_scored():
    release = source()
    harness = ScriptedHarness([outcome(valid=True, success=1, output="ok")] * 2)
    benchmark_scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=release.manifest_sha256
    )
    program = b"print('candidate')\n"
    score = benchmark_scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program, contamination=("training overlap",)),
        artifacts={"program": program},
    )
    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert harness.calls == []


def test_missing_output_is_reproducible_scientific_failure():
    same = outcome(valid=False, success=0, output=None)
    score, _harness = run_score([same, same])

    assert score.scientific_success is False
    assert score.invalid_reasons == ()
    assert score.objective_scores["valid_program"] == 0


def test_missing_submitted_program_is_invalid_and_never_scored():
    release = source()
    harness = ScriptedHarness([outcome(valid=True, success=1, output="ok")] * 2)
    benchmark_scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=release.manifest_sha256
    )
    program = b"print('candidate')\n"
    score = benchmark_scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program),
        artifacts={},
    )
    assert score.invalid_reasons == (InvalidReason.MISSING_ARTIFACT,)
    assert harness.calls == []


def test_non_reproducible_output_is_invalid_not_best_of_two():
    one = outcome(valid=True, success=1, output='{"slope":2}')
    two = outcome(valid=True, success=0, output='{"slope":9}')
    score, harness = run_score([one, two])

    assert score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)
    assert score.scientific_success is None
    assert score.objective_scores == {"reproducible": 0.0}
    assert harness.calls == [0, 1]


def test_harness_resource_limit_is_invalid_not_scientific_failure():
    limited = outcome(valid=False, success=0, output=None)
    release = source()
    harness = ScriptedHarness([limited, limited])
    original_evaluate = harness.evaluate

    def resource_limited(**kwargs):
        result = original_evaluate(**kwargs)
        return result.model_copy(
            update={
                "program_exit_reason": ExecutionExitReason.WALL_TIME_LIMIT,
                "program_timed_out": True,
            }
        )

    harness.evaluate = resource_limited  # type: ignore[method-assign]
    benchmark_scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=release.manifest_sha256
    )
    program = b"while True: pass\n"
    score = benchmark_scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program),
        artifacts={"program": program},
    )
    assert score.invalid_reasons == (InvalidReason.RESOURCE_LIMIT,)
    assert score.scientific_success is None


def test_hidden_receipt_release_mismatch_is_infrastructure_failure():
    release = source()
    other = release.model_copy(update={"dataset_revision": "a" * 40})
    other = ScienceAgentBenchSourceManifest.model_validate(other.model_dump())
    harness = ScriptedHarness([outcome(valid=True, success=1, output="ok")] * 2)
    benchmark_scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=release.manifest_sha256
    )
    program = b"print('candidate')\n"
    with pytest.raises(RuntimeError, match="different annotation release"):
        benchmark_scorer.score(
            task=task(),
            hidden_asset=receipt(other).to_bytes(),
            submission=submission(program),
            artifacts={"program": program},
        )
    assert harness.calls == []
