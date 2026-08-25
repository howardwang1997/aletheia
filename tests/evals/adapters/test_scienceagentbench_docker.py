"""Real Docker proof for the ScienceAgentBench two-plane scorer boundary."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.evals.adapters.scienceagentbench import (
    DEFAULT_CC_BY_SUBSET_IDS,
    OFFICIAL_REPOSITORY_COMMIT,
    DockerScienceAgentBenchHarness,
    ScienceAgentBenchAdapter,
    ScienceAgentBenchScorer,
    ScienceAgentBenchSourceManifest,
)
from aletheia.evals.schemas import ExecutionExitReason, InvalidReason
from aletheia.paths import WORKSPACES_ROOT

from .test_scienceagentbench_contract import instance
from .test_scienceagentbench_scoring import submission, task

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _evaluator_image_available():
    settings = get_settings()
    inspect = subprocess.run(
        [settings.sandbox_docker_command, "image", "inspect", settings.evaluator_agent_docker_image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip(f"evaluator image unavailable: {settings.evaluator_agent_docker_image}")


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"sab-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def build_harness(workspace: Path):
    benchmark = workspace / "benchmark"
    datasets = benchmark / "datasets" / "tiny"
    evaluators = benchmark / "eval_programs"
    datasets.mkdir(parents=True)
    evaluators.mkdir()
    (datasets / "observations.csv").write_text("x,y\n1,3\n2,5\n3,7\n", encoding="utf-8")
    unrelated = benchmark / "datasets" / "unrelated"
    unrelated.mkdir()
    (unrelated / "other-task-secret.txt").write_text("not this task", encoding="utf-8")
    (evaluators / "tiny_eval.py").write_text(
        """
import json
from pathlib import Path

def eval():
    payload = json.loads(Path("pred_results/result.json").read_text())
    return int(payload == {"intercept": 1, "slope": 2}), "objective exact result"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    source = ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="json",
        annotation_sha256="3" * 64,
        annotation_rows=1,
    )
    adapter = ScienceAgentBenchAdapter(source)
    asset_receipt = adapter.freeze_assets(
        instance=instance(), benchmark_root=benchmark, benchmark_archive_sha256="4" * 64
    )
    harness = DockerScienceAgentBenchHarness.from_image_refs(
        candidate_image_ref=get_settings().evaluator_agent_docker_image,
        scorer_image_ref=get_settings().evaluator_agent_docker_image,
        benchmark_root=benchmark,
        scratch_root=workspace / "scratch",
        candidate_wall_time_s=45,
        candidate_cpu_seconds=10,
        candidate_memory_mb=256,
        scorer_wall_time_s=45,
        scorer_cpu_seconds=10,
        scorer_memory_mb=256,
        supported_instance_requirements={"1": ("scikit-learn",)},
    )
    scorer = ScienceAgentBenchScorer(
        harness=harness, source_manifest_sha256=source.manifest_sha256
    )
    return source, asset_receipt, harness, scorer


def test_docker_manifest_uses_probed_immutable_environment(workspace_tmp_path):
    _source, _receipt, harness, _scorer = build_harness(workspace_tmp_path)
    assert harness.manifest.candidate_image_id.startswith("sha256:")
    assert harness.manifest.candidate_environment["python"] != "not-installed"
    assert harness.manifest.candidate_environment["scikit-learn"] != "not-installed"
    harness.manifest.assert_instance_supported("1")


def test_reviewed_default_image_supports_all_four_domain_contracts(workspace_tmp_path):
    settings = get_settings()
    inspect = subprocess.run(
        [
            settings.sandbox_docker_command,
            "image",
            "inspect",
            settings.scienceagentbench_docker_image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip(
            f"ScienceAgentBench image unavailable: {settings.scienceagentbench_docker_image}"
        )
    benchmark = workspace_tmp_path / "reviewed-image-benchmark"
    (benchmark / "datasets").mkdir(parents=True)
    (benchmark / "eval_programs").mkdir()

    harness = DockerScienceAgentBenchHarness.from_image_refs(
        candidate_image_ref=settings.scienceagentbench_docker_image,
        scorer_image_ref=settings.scienceagentbench_docker_image,
        benchmark_root=benchmark,
        scratch_root=workspace_tmp_path / "reviewed-image-scratch",
    )

    for instance_id in DEFAULT_CC_BY_SUBSET_IDS:
        harness.manifest.assert_instance_supported(instance_id)
    assert harness.manifest.candidate_image_id == harness.manifest.scorer_image_id
    assert harness.manifest.candidate_environment["rdkit"] != "not-installed"
    assert harness.manifest.candidate_environment["geopandas"] != "not-installed"
    assert harness.manifest.candidate_environment["neurokit2"] != "not-installed"


def score_program(*, scorer, receipt, program: bytes):
    return scorer.score(
        task=task(),
        hidden_asset=receipt.to_bytes(),
        submission=submission(program),
        artifacts={"program": program},
    )


def test_real_docker_correct_and_numerically_wrong_programs(workspace_tmp_path):
    _source, receipt, harness, scorer = build_harness(workspace_tmp_path)
    correct = b"""
import csv, json
from pathlib import Path
rows = list(csv.DictReader(Path('benchmark/datasets/tiny/observations.csv').open()))
assert len(rows) == 3
Path('pred_results').mkdir()
Path('pred_results/result.json').write_text(json.dumps({'intercept': 1, 'slope': 2}))
"""
    wrong = correct.replace(b"'slope': 2", b"'slope': 9")

    correct_score = score_program(scorer=scorer, receipt=receipt, program=correct)
    wrong_score = score_program(scorer=scorer, receipt=receipt, program=wrong)

    assert correct_score.scientific_success is True
    assert wrong_score.scientific_success is False
    assert wrong_score.objective_scores["valid_program"] == 1
    assert wrong_score.objective_scores["success_rate"] == 0
    assert harness.manifest.gold_program_policy == "never_mounted"
    assert harness.manifest.network_mode == "none"


def test_real_docker_candidate_cannot_read_evaluator_and_nondeterminism_is_invalid(
    workspace_tmp_path,
):
    _source, receipt, _harness, scorer = build_harness(workspace_tmp_path)
    probe = b"""
import json
from pathlib import Path
assert not Path('benchmark/eval_programs').exists()
assert not Path('benchmark/gold_programs').exists()
assert not Path('benchmark/datasets/unrelated').exists()
Path('pred_results').mkdir()
Path('pred_results/result.json').write_text(json.dumps({'intercept': 1, 'slope': 2}))
"""
    probe_score = score_program(scorer=scorer, receipt=receipt, program=probe)
    assert probe_score.scientific_success is True

    nondeterministic = b"""
import json, os
from pathlib import Path
Path('pred_results').mkdir()
Path('pred_results/result.json').write_text(json.dumps({
    'intercept': 1,
    'slope': 2,
    'nonce': os.urandom(16).hex(),
}))
"""
    random_score = score_program(scorer=scorer, receipt=receipt, program=nondeterministic)
    assert random_score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)


def test_candidate_exit_125_is_scientific_process_failure_not_infrastructure_retry(
    workspace_tmp_path,
):
    _source, receipt, _harness, scorer = build_harness(workspace_tmp_path)

    score = score_program(
        scorer=scorer,
        receipt=receipt,
        program=b"raise SystemExit(125)\n",
    )

    assert score.invalid_reasons == ()
    assert score.scientific_success is False
    assert score.objective_scores == {
        "valid_program": 0.0,
        "success_rate": 0.0,
        "reproducible": 1.0,
    }
    assert {
        evidence["program_exit_reason"] for evidence in score.evidence_objects.values()
    } == {ExecutionExitReason.PROCESS_ERROR.value}


def test_real_docker_asset_tampering_fails_closed(workspace_tmp_path):
    _source, receipt, harness, _scorer = build_harness(workspace_tmp_path)
    evaluator = workspace_tmp_path / "benchmark" / "eval_programs" / "tiny_eval.py"
    evaluator.write_text("def eval(): return 1, 'tampered'\n", encoding="utf-8")
    program = b"print('candidate')\n"
    with pytest.raises(RuntimeError, match="differs from the frozen asset receipt"):
        harness.evaluate(receipt=receipt, program=program, run_index=0)


def test_program_evidence_hash_matches_submitted_bytes(workspace_tmp_path):
    _source, receipt, _harness, scorer = build_harness(workspace_tmp_path)
    program = b"""
import json
from pathlib import Path
Path('pred_results').mkdir()
Path('pred_results/result.json').write_text(json.dumps({'intercept': 1, 'slope': 2}))
"""
    score = score_program(scorer=scorer, receipt=receipt, program=program)
    assert score.evidence_sha256s["submitted_program"] == hashlib.sha256(program).hexdigest()
    assert set(score.evidence_objects) == {"harness_run_0", "harness_run_1"}
    assert score.evidence_objects["harness_run_0"]["program_wall_time_s"] > 0
