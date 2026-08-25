from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from aletheia.legacy_evaluation.contracts import canonical_json_bytes

from conftest import LegacyEvaluationCase, SOURCE_ROOT

_RUN_DOCKER = os.getenv("ALETHEIA_RUN_DOCKER_TESTS") == "1"


@pytest.fixture
def docker_shared_tmp_path():
    # Colima shares the workspace under /Users but not pytest's /private/var basetemp.
    root = Path(tempfile.mkdtemp(prefix=".pr6-container-test-", dir=SOURCE_ROOT))
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.mark.skipif(not _RUN_DOCKER, reason="set ALETHEIA_RUN_DOCKER_TESTS=1 for OCI smoke")
def test_candidate_image_runs_fixed_handler_with_read_only_root(
    legacy_evaluation_case: LegacyEvaluationCase,
    docker_shared_tmp_path: Path,
) -> None:
    case = legacy_evaluation_case
    image = os.getenv("ALETHEIA_PR6_IMAGE", "aletheia-pr6-container-test:latest")
    if "ALETHEIA_PR6_IMAGE" not in os.environ:
        subprocess.run(
            (
                "docker",
                "build",
                "-f",
                "docker/legacy-evaluation-runtime.Dockerfile",
                "-t",
                image,
                ".",
            ),
            cwd=SOURCE_ROOT,
            check=True,
        )

    constraints = (
        SOURCE_ROOT / "configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt"
    ).read_text(encoding="utf-8")
    expected_dependencies = {
        line for line in constraints.splitlines() if line and not line.startswith("#")
    }
    frozen_dependencies = subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            image,
            "/usr/local/bin/python3",
            "-m",
            "pip",
            "freeze",
            "--all",
            "--exclude",
            "pip",
            "--exclude",
            "setuptools",
            "--exclude",
            "wheel",
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(frozen_dependencies) == expected_dependencies
    subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            image,
            "/usr/local/bin/python3",
            "-m",
            "pip",
            "check",
        ),
        check=True,
    )

    input_root = docker_shared_tmp_path / "input"
    output_root = docker_shared_tmp_path / "output"
    input_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    invocation = case.invocation.model_copy(
        update={
            "issued_at": now - timedelta(minutes=1),
            "deadline": now + timedelta(hours=1),
        }
    )
    (input_root / "legacy-evaluation-invocation.json").write_bytes(canonical_json_bytes(invocation))
    shutil.copyfile(
        case.input_table_path,
        input_root / "legacy-evaluation-table.csv",
    )

    subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/opt/aletheia/scratch:rw,nodev,nosuid,size=67108864,mode=1777",
            "--mount",
            f"type=bind,src={input_root},dst=/opt/aletheia/input,readonly",
            "--mount",
            f"type=bind,src={output_root},dst=/opt/aletheia/output",
            image,
            "/opt/aletheia/bin/legacy-evaluation-workload",
        ),
        check=True,
    )

    assert {item.name for item in output_root.iterdir()} == {
        "eval.json",
        "model.bin",
        "raw-result.json",
    }
    raw = json.loads((output_root / "raw-result.json").read_text(encoding="utf-8"))
    assert raw["process_status"] == "process_succeeded"
    assert raw["scientific_outcome"] == "not_assessed"
    assert raw["scientific_admission_allowed"] is False
