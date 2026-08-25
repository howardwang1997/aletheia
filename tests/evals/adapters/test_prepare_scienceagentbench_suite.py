"""CLI contract for preparing, but not executing, the ScienceAgentBench mini-suite."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.evals.adapters.scienceagentbench import ANNOTATION_COLUMNS
from aletheia.evals.adapters.scienceagentbench import ScienceAgentBenchSourceManifest

from .test_scienceagentbench_contract import instance

pytestmark = pytest.mark.docker


def test_prepare_cli_emits_content_addressed_bundle_without_copying_assets(
    workspace_tmp_path, tmp_path
):
    settings = get_settings()
    inspect = subprocess.run(
        [settings.sandbox_docker_command, "image", "inspect", settings.evaluator_agent_docker_image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip("evaluator image unavailable")
    annotation = tmp_path / "verified.csv"
    with annotation.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_COLUMNS)
        writer.writeheader()
        writer.writerow(instance().model_dump())
    source = ScienceAgentBenchSourceManifest(
        repository_commit="a" * 40,
        dataset_revision="b" * 40,
        annotation_format="csv",
        annotation_sha256=hashlib.sha256(annotation.read_bytes()).hexdigest(),
        annotation_rows=1,
    )
    source_path = tmp_path / "source.json"
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    benchmark = workspace_tmp_path / "benchmark"
    data = benchmark / "datasets" / "tiny"
    evaluators = benchmark / "eval_programs"
    data.mkdir(parents=True)
    evaluators.mkdir()
    (data / "observations.csv").write_text("x,y\n1,3\n")
    (evaluators / "tiny_eval.py").write_text("def eval(): return 1, 'ok'\n")
    archive = workspace_tmp_path / "benchmark_verified.zip"
    archive.write_bytes(b"synthetic-test-archive")
    output = workspace_tmp_path / "prepared"

    command = [
        "conda",
        "run",
        "-n",
        "aletheia",
        "python",
        "scripts/prepare_scienceagentbench_suite.py",
        "--annotation",
        str(annotation),
        "--benchmark-root",
        str(benchmark),
        "--source-manifest",
        str(source_path),
        "--benchmark-archive",
        str(archive),
        "--output-root",
        str(output),
        "--instance-id",
        "1",
        "--required-distribution",
        "1=scikit-learn",
        "--candidate-image",
        settings.evaluator_agent_docker_image,
        "--scorer-image",
        settings.evaluator_agent_docker_image,
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd())
    result = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    assert result.returncode == 0, result.stderr
    bundle = json.loads((output / "scienceagentbench_suite.v1.json").read_text())
    assert bundle["benchmark_assets_copied"] is False
    assert bundle["subset_manifest"]["instance_ids"] == ["1"]
    assert bundle["suite"]["frozen"] is True
    assert len(bundle["tasks"]) == 1
    assert len(bundle["hidden_asset_paths"]) == 1
    assert not (output / "datasets").exists()
    assert not (output / "eval_programs").exists()


@pytest.fixture
def workspace_tmp_path():
    from aletheia.paths import WORKSPACES_ROOT

    import shutil
    import uuid

    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"sab-cli-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)
