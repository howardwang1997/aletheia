"""CLI contract for preparing—but not running—the official CORE-Bench mini-suite."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.evals.adapters.corebench import CoreBenchSourceManifest
from aletheia.paths import WORKSPACES_ROOT

from .test_corebench_contract import instance, write_capsule

pytestmark = pytest.mark.docker


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"corebench-cli-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def test_prepare_cli_emits_sanitized_public_and_hidden_assets(workspace_tmp_path, tmp_path):
    settings = get_settings()
    candidate_image = settings.corebench_docker_image
    inspect = subprocess.run(
        [settings.sandbox_docker_command, "image", "inspect", candidate_image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip("evaluator image unavailable")
    annotation = tmp_path / "core_train.json"
    annotation.write_text(json.dumps([instance().model_dump(mode="json")]), encoding="utf-8")
    source = CoreBenchSourceManifest(
        astabench_commit="1" * 40,
        astabench_core_wrapper_sha256="2" * 64,
        inspect_evals_commit="3" * 40,
        inspect_evals_scorer_sha256="4" * 64,
        inspect_evals_utils_sha256="5" * 64,
        dataset_revision="6" * 40,
        annotation_sha256=hashlib.sha256(annotation.read_bytes()).hexdigest(),
        annotation_rows=1,
    )
    source_path = tmp_path / "source.json"
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    capsule_root = workspace_tmp_path / "capsules"
    capsule_root.mkdir()
    write_capsule(capsule_root / "capsule-0000001.tar.gz")
    output = workspace_tmp_path / "prepared"

    # The official CLI permits only reviewed default IDs. Patch the contract in-process would
    # weaken that operator invariant, so this integration test exercises a copied official ID.
    annotation_payload = json.loads(annotation.read_text())
    annotation_payload[0]["capsule_id"] = "capsule-6460826"
    annotation.write_text(json.dumps(annotation_payload), encoding="utf-8")
    source = source.model_copy(
        update={"annotation_sha256": hashlib.sha256(annotation.read_bytes()).hexdigest()}
    )
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    write_capsule(capsule_root / "capsule-6460826.tar.gz", capsule_id="capsule-6460826")

    command = [
        "conda", "run", "-n", "aletheia", "python", "scripts/prepare_corebench_suite.py",
        "--annotation", str(annotation),
        "--source-manifest", str(source_path),
        "--capsule-root", str(capsule_root),
        "--output-root", str(output),
        "--capsule-id", "capsule-6460826",
        "--candidate-image", candidate_image,
        "--scorer-image", settings.evaluator_agent_docker_image,
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd())
    result = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    assert result.returncode == 0, result.stderr
    bundle = json.loads((output / "corebench_suite.v1.json").read_text())
    assert bundle["upstream_test_downloaded_or_decrypted"] is False
    assert bundle["source_capsules_copied"] is False
    assert bundle["subset_manifest"]["capsule_ids"] == ["capsule-6460826"]
    public = output / bundle["public_asset_paths"][0]
    hidden = output / bundle["hidden_asset_paths"][0]
    assert public.is_file() and hidden.is_file()
    assert b"Report the fitted slope" not in public.read_bytes()
    assert b"Report the fitted slope" in hidden.read_bytes()
