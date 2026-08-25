"""Real no-network two-plane proof for the CORE-Bench reproduction adapter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.evals.adapters.corebench import (
    CoreBenchAdapter,
    CoreBenchScorer,
    DockerCoreBenchHarness,
)
from aletheia.evals.schemas import ExecutionExitReason, InvalidReason
from aletheia.paths import WORKSPACES_ROOT

from .test_corebench_contract import instance, source_for, write_capsule
from .test_corebench_scoring import submission, task

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _image_available():
    settings = get_settings()
    inspect = subprocess.run(
        [settings.sandbox_docker_command, "image", "inspect", settings.evaluator_agent_docker_image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip("evaluator image unavailable")


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"corebench-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def build_harness(workspace: Path):
    annotation = workspace / "core_train.json"
    annotation.write_text(json.dumps([instance().model_dump(mode="json")]), encoding="utf-8")
    source = source_for(annotation)
    adapter = CoreBenchAdapter(source)
    capsule = workspace / "capsule-0000001.tar.gz"
    write_capsule(capsule)
    receipt = adapter.freeze_capsule(
        instance=instance(), archive_path=capsule, asset_root=workspace / "frozen"
    )
    settings = get_settings()
    harness = DockerCoreBenchHarness.from_image_refs(
        candidate_image_ref=settings.evaluator_agent_docker_image,
        scorer_image_ref=settings.evaluator_agent_docker_image,
        source_manifest_sha256=source.manifest_sha256,
        public_asset_root=workspace / "frozen/public_assets",
        scratch_root=workspace / "scratch",
        supported_capsule_requirements={"capsule-0000001": ("numpy",)},
        candidate_wall_time_s=30,
        candidate_cpu_seconds=10,
        candidate_memory_mb=256,
        scorer_wall_time_s=30,
        scorer_cpu_seconds=10,
        scorer_memory_mb=256,
    )
    scorer = CoreBenchScorer(harness=harness, source_manifest_sha256=source.manifest_sha256)
    return receipt, harness, scorer


def score_program(scorer, receipt, program: bytes):
    return scorer.score(
        task=task(),
        hidden_asset=receipt.to_bytes(),
        submission=submission(program),
        artifacts={"reproduction_program": program},
    )


def program(answer: int, *, artifact: bool = True) -> bytes:
    source = f"""
import json
from pathlib import Path
capsule = Path('capsule')
assert (capsule / 'code/run.py').is_file()
assert not (capsule / 'environment').exists()
(capsule / 'report.json').write_text(json.dumps({{'Report the fitted slope.': {answer}}}, sort_keys=True))
"""
    if artifact:
        source += "(capsule / 'reproduction_artifacts').mkdir()\n"
        source += "(capsule / 'reproduction_artifacts/output.txt').write_text('computed slope=2\\n')\n"
    return source.encode()


def test_real_docker_correct_wrong_and_missing_artifact(workspace_tmp_path):
    receipt, harness, scorer = build_harness(workspace_tmp_path)
    correct = score_program(scorer, receipt, program(2))
    wrong = score_program(scorer, receipt, program(9))
    missing = score_program(scorer, receipt, program(2, artifact=False))
    assert correct.scientific_success is True
    assert wrong.scientific_success is False
    assert wrong.objective_scores["question_accuracy"] == 0
    assert missing.scientific_success is False
    assert missing.objective_scores["artifact_present"] == 0
    assert harness.manifest.network_mode == "none"
    assert harness.manifest.results_policy == "never-mounted-to-candidate"


def test_real_docker_nondeterministic_artifact_is_invalid(workspace_tmp_path):
    receipt, _harness, scorer = build_harness(workspace_tmp_path)
    random_program = b"""
import json, os
from pathlib import Path
capsule = Path('capsule')
(capsule / 'report.json').write_text(json.dumps({'Report the fitted slope.': 2}))
(capsule / 'reproduction_artifacts').mkdir()
(capsule / 'reproduction_artifacts/output.txt').write_bytes(os.urandom(16))
"""
    score = score_program(scorer, receipt, random_program)
    assert score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)


def test_candidate_exit_125_is_scientific_failure_not_infrastructure(workspace_tmp_path):
    receipt, _harness, scorer = build_harness(workspace_tmp_path)
    score = score_program(scorer, receipt, b"raise SystemExit(125)\n")
    assert score.invalid_reasons == ()
    assert score.scientific_success is False
    assert {item["program_exit_reason"] for item in score.evidence_objects.values()} == {
        ExecutionExitReason.PROCESS_ERROR.value
    }


def test_public_capsule_tampering_fails_closed(workspace_tmp_path):
    receipt, harness, _scorer = build_harness(workspace_tmp_path)
    public = (
        workspace_tmp_path
        / "frozen/public_assets/corebench"
        / receipt.source_manifest_sha256
        / "capsule-0000001.tar.gz"
    )
    os.chmod(public, 0o600)
    public.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="differs from the frozen receipt"):
        harness.evaluate(receipt=receipt, program=program(2), run_index=0)


def test_program_evidence_hash_matches_submission(workspace_tmp_path):
    receipt, _harness, scorer = build_harness(workspace_tmp_path)
    submitted = program(2)
    score = score_program(scorer, receipt, submitted)
    assert score.evidence_sha256s["submitted_program"] == hashlib.sha256(submitted).hexdigest()
    assert set(score.evidence_objects) == {"harness_run_0", "harness_run_1"}
