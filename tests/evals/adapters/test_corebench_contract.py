"""Frozen source, license, sanitization, and task contracts for CORE-Bench-Hard."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from aletheia.coder.executor import SandboxExecution
from aletheia.evals.adapters.corebench import (
    ASTABENCH_COMMIT,
    DEFAULT_COREBENCH_CAPSULE_IDS,
    CoreBenchAdapter,
    CoreBenchAssetReceipt,
    CoreBenchHarnessManifest,
    CoreBenchInstance,
    CoreBenchScorer,
    CoreBenchSourceManifest,
    _probe_docker_environment,
)
from aletheia.evals.schemas import EvaluationTask, ResourceBudget

ZERO_IMAGE = "sha256:" + "0" * 64
ONE_IMAGE = "sha256:" + "1" * 64
ENVIRONMENT = {
    "python": "3.11.0",
    "bash": "available",
    "numpy": "1",
    "pandas": "1",
    "scikit-learn": "1",
    "networkx": "1",
    "seaborn": "1",
    "jupyter": "1",
    "nbconvert": "1",
}


def instance(capsule_id: str = "capsule-0000001") -> CoreBenchInstance:
    return CoreBenchInstance(
        field="Computer Science",
        language="Python",
        capsule_title="Tiny reproducible relation",
        capsule_id=capsule_id,
        task_prompt="Run code/run.py.",
        results=(
            {"Report the fitted slope.": 2.0},
            {"Report the fitted slope.": 2.0},
            {"Report the fitted slope.": 2.0},
        ),
        capsule_doi="https://doi.org/10.0000/example",
    )


def source_for(annotation: Path) -> CoreBenchSourceManifest:
    return CoreBenchSourceManifest(
        astabench_commit=ASTABENCH_COMMIT,
        astabench_core_wrapper_sha256="1" * 64,
        inspect_evals_commit="2" * 40,
        inspect_evals_scorer_sha256="3" * 64,
        inspect_evals_utils_sha256="4" * 64,
        dataset_revision="5" * 40,
        annotation_sha256=hashlib.sha256(annotation.read_bytes()).hexdigest(),
        annotation_rows=1,
    )


def write_capsule(path: Path, capsule_id: str = "capsule-0000001", *, code_license=b"MIT License\n"):
    files = {
        f"{capsule_id}/code/LICENSE": code_license,
        f"{capsule_id}/code/run.py": b"print(2)\n",
        f"{capsule_id}/data/LICENSE": b"CC0 1.0 Universal\n",
        f"{capsule_id}/data/x.csv": b"x,y\n1,3\n",
        f"{capsule_id}/results/answer.txt": b"2.0\n",
        f"{capsule_id}/REPRODUCING.md": b"hidden convenience instructions\n",
        f"{capsule_id}/environment/Dockerfile": b"FROM python:3.11\n",
        f"{capsule_id}/.DS_Store": b"metadata",
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


@dataclass
class NeverHarness:
    capsule_id: str = "capsule-0000001"

    @property
    def manifest(self):
        return CoreBenchHarnessManifest(
            source_manifest_sha256="a" * 64,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            scorer_entrypoint_sha256="b" * 64,
            candidate_environment=ENVIRONMENT,
            supported_capsule_requirements={self.capsule_id: ("numpy",)},
        )

    def evaluate(self, **_kwargs):
        raise AssertionError("contract test must not execute the harness")


def test_official_source_is_public_validation_and_never_test():
    source = CoreBenchSourceManifest.official_validation()
    assert source.split == "validation"
    assert source.upstream_split == "train"
    assert source.test_data_policy == "never_downloaded-or-decrypted"
    assert source.astabench_commit == ASTABENCH_COMMIT
    assert DEFAULT_COREBENCH_CAPSULE_IDS == ("capsule-6460826", "capsule-0940461")


def test_load_subset_freeze_sanitizes_results_and_preserves_licenses(tmp_path):
    annotation = tmp_path / "core_train.json"
    annotation.write_text(json.dumps([instance().model_dump(mode="json")]), encoding="utf-8")
    source = source_for(annotation)
    adapter = CoreBenchAdapter(source)
    loaded = adapter.load_instances(annotation)
    selected, subset = adapter.select_subset(loaded, capsule_ids=("capsule-0000001",))
    capsule = tmp_path / "capsule-0000001.tar.gz"
    write_capsule(capsule)
    receipt = adapter.freeze_capsule(
        instance=selected[0], archive_path=capsule, asset_root=tmp_path / "frozen"
    )
    assert subset.code_license_by_capsule == {"capsule-0000001": "MIT"}
    assert receipt.code_license == "MIT"
    public = (
        tmp_path
        / "frozen/public_assets/corebench"
        / source.manifest_sha256
        / "capsule-0000001.tar.gz"
    )
    with tarfile.open(public, "r:gz") as archive:
        names = set(archive.getnames())
    assert "code/LICENSE" in names
    assert "data/LICENSE" in names
    assert not any(name == "results" or name.startswith("results/") for name in names)
    assert "REPRODUCING.md" not in names
    assert not any(name == "environment" or name.startswith("environment/") for name in names)

    harness = NeverHarness()
    scorer = CoreBenchScorer(harness=harness, source_manifest_sha256=source.manifest_sha256)
    task = adapter.build_task(
        receipt=receipt,
        scorer=scorer,
        resource_budget=ResourceBudget(wall_time_s=60, cpu_seconds=30, memory_mb=512),
    )
    public_view = task.public_view().model_dump(mode="json")
    serialized = json.dumps(public_view)
    assert "evaluator_ref" not in serialized
    assert '"results":' not in serialized
    assert '"Report the fitted slope.": 2.0' not in serialized
    assert public_view["public_assets"][0]["mount_path"] == "capsule"
    assert task.hidden_asset_sha256 == receipt.hidden_sha256


def test_hash_license_and_archive_path_drift_fail_closed(tmp_path):
    annotation = tmp_path / "core_train.json"
    annotation.write_text(json.dumps([instance().model_dump(mode="json")]), encoding="utf-8")
    adapter = CoreBenchAdapter(source_for(annotation))
    annotation.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        adapter.load_instances(annotation)

    bad_license = tmp_path / "bad-license.tar.gz"
    write_capsule(bad_license, code_license=b"unknown\n")
    with pytest.raises(ValueError, match="not the reviewed MIT"):
        adapter.freeze_capsule(
            instance=instance(), archive_path=bad_license, asset_root=tmp_path / "one"
        )

    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        info = tarfile.TarInfo("../gold.json")
        info.size = 6
        archive.addfile(info, io.BytesIO(b"secret"))
    with pytest.raises(ValueError, match="unsafe archive path"):
        adapter.freeze_capsule(
            instance=instance(), archive_path=traversal, asset_root=tmp_path / "two"
        )


def test_hidden_asset_staging_rejects_path_escape(tmp_path):
    task = EvaluationTask(
        task_id="corebench-hard-0000001",
        version="test",
        layer=2,
        public_prompt="Reproduce it.",
        hidden_asset_ref="evaluator://hidden/../escape.json",
        hidden_asset_sha256="a" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=(
            {"kind": "reproduction_program", "media_type": "text/x-python", "max_bytes": 100},
        ),
        scorer_ref="evaluator://scorers/core",
        scorer_sha256="b" * 64,
    )
    dummy = object.__new__(CoreBenchAssetReceipt)
    with pytest.raises(ValueError, match="escaped"):
        CoreBenchAdapter.stage_hidden_asset(evaluator_root=tmp_path, task=task, receipt=dummy)


def test_environment_probe_retries_only_stopped_container_client_hang(tmp_path, monkeypatch):
    payload = json.dumps(ENVIRONMENT)
    calls = []

    monkeypatch.setattr("aletheia.coder.executor.resolve_docker_image", lambda _ref: ZERO_IMAGE)

    def scripted(_command, **kwargs):
        calls.append(kwargs["container_name"])
        if len(calls) == 1:
            return SandboxExecution(
                returncode=-15,
                output=payload,
                image_id=ZERO_IMAGE,
                error="Docker client did not exit after its container stopped",
                container_started=True,
            )
        return SandboxExecution(
            returncode=0,
            output=payload,
            image_id=ZERO_IMAGE,
            container_started=True,
        )

    monkeypatch.setattr("aletheia.evals.adapters.corebench.run_hardened_container", scripted)
    observed = _probe_docker_environment(ZERO_IMAGE, scratch_root=tmp_path)
    assert observed["networkx"] == "1"
    assert len(calls) == 2 and calls[0] != calls[1]


def test_environment_probe_does_not_retry_running_timeout(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("aletheia.coder.executor.resolve_docker_image", lambda _ref: ZERO_IMAGE)

    def timeout(_command, **kwargs):
        calls.append(kwargs["container_name"])
        return SandboxExecution(
            returncode=-15,
            output="",
            image_id=ZERO_IMAGE,
            timed_out=True,
            error="hard sandbox timed out after 30s",
            container_started=True,
        )

    monkeypatch.setattr("aletheia.evals.adapters.corebench.run_hardened_container", timeout)
    with pytest.raises(RuntimeError, match="could not inspect"):
        _probe_docker_environment(ZERO_IMAGE, scratch_root=tmp_path)
    assert len(calls) == 1
