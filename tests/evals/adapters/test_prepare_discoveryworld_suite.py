"""CLI contract for freezing the DiscoveryWorld public-validation mini-suite."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.paths import WORKSPACES_ROOT

pytestmark = pytest.mark.docker


@pytest.fixture
def workspace_tmp_path():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"dw-cli-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def test_prepare_cli_emits_sanitized_bundle_and_evaluator_only_receipt(workspace_tmp_path):
    settings = get_settings()
    for image in (
        settings.discoveryworld_candidate_docker_image,
        settings.discoveryworld_docker_image,
    ):
        inspect = subprocess.run(
            [settings.sandbox_docker_command, "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            pytest.skip(f"DiscoveryWorld image unavailable: {image}")

    output = workspace_tmp_path / "prepared"
    command = [
        "conda",
        "run",
        "-n",
        "aletheia",
        "python",
        "scripts/prepare_discoveryworld_suite.py",
        "--output-root",
        str(output),
        "--instance",
        "chem-easy-cli=0",
        "--candidate-image",
        settings.discoveryworld_candidate_docker_image,
        "--environment-image",
        settings.discoveryworld_docker_image,
        "--candidate-wall-time-s",
        "20",
        "--candidate-cpu-seconds",
        "15",
        "--environment-wall-time-s",
        "35",
        "--environment-cpu-seconds",
        "30",
        "--max-world-actions",
        "12",
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd())
    result = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    assert result.returncode == 0, result.stderr

    bundle = json.loads((output / "discoveryworld_suite.v1.json").read_text())
    assert bundle["hidden_receipts_embedded_in_bundle"] is False
    assert bundle["candidate_receives_world_seed_rule_scorecard_or_scorer"] is False
    assert bundle["official_source_or_art_assets_vendored_into_suite"] is False
    assert bundle["harness_manifest"]["candidate_environment"]["discoveryworld"] == (
        "not-installed"
    )
    assert len(bundle["tasks"]) == 1
    assert len(bundle["public_tasks"]) == 1
    assert len(bundle["hidden_asset_paths"]) == 1
    hidden = output / bundle["hidden_asset_paths"][0]
    assert hidden.is_file()
    hidden_payload = json.loads(hidden.read_text())
    assert "world_seed" in hidden_payload
    assert "correct_hypothesis_id" in hidden_payload
    assert "world_seed" not in bundle["public_tasks"][0]
    assert "correct_hypothesis_id" not in bundle["public_tasks"][0]
