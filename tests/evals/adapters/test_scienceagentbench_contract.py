"""F7-S3 ScienceAgentBench source, license, and task-contract tests."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from aletheia.coder.executor import SandboxExecution
from aletheia.evals.adapters.scienceagentbench import (
    ANNOTATION_COLUMNS,
    OFFICIAL_REPOSITORY_COMMIT,
    ORIGINAL_LICENSE_INSTANCE_IDS,
    SCIENCEAGENTBENCH_CANARY,
    ScienceAgentBenchAdapter,
    ScienceAgentBenchAssetReceipt,
    ScienceAgentBenchHarnessManifest,
    ScienceAgentBenchHarnessResult,
    ScienceAgentBenchInstance,
    ScienceAgentBenchScorer,
    ScienceAgentBenchSourceManifest,
    _probe_docker_environment,
)
from aletheia.evals.adapters.scienceagentbench_scorer_entrypoint import (
    _write_terminal_receipt,
)
from aletheia.evals.schemas import EvaluationTask, ExecutionExitReason, ResourceBudget

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


def test_trusted_evaluator_receipt_is_canonical_and_atomic(tmp_path):
    result = tmp_path / "result.json"
    _write_terminal_receipt(result, {"success_rate": 1.0, "log_info_sha256": "a" * 64})

    assert json.loads(result.read_bytes()) == {
        "success_rate": 1.0,
        "log_info_sha256": "a" * 64,
    }
    assert not any(path.name.startswith(".result.json.") for path in tmp_path.iterdir())


def instance(instance_id: str = "1") -> ScienceAgentBenchInstance:
    return ScienceAgentBenchInstance(
        instance_id=instance_id,
        domain="Bioinformatics",
        subtask_categories="Regression",
        github_name="example/public-science",
        task_inst="Fit a linear relation and save pred_results/result.json.",
        domain_knowledge="The expected relation is linear.",
        dataset_folder_tree="|-- tiny/\n|---- observations.csv",
        dataset_preview="x,y\n1,3\n2,5",
        src_file_or_path="examples/tiny",
        gold_program_name="tiny.py",
        output_fname="pred_results/result.json",
        eval_script_name="tiny_eval.py",
    )


def source_for(path: Path, *, rows: int = 1) -> ScienceAgentBenchSourceManifest:
    return ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="csv",
        annotation_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        annotation_rows=rows,
        contiguous_instance_ids=True,
    )


def write_csv(path: Path, records: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANNOTATION_COLUMNS)
        writer.writeheader()
        writer.writerows(records)


@dataclass
class NeverHarness:
    manifest: ScienceAgentBenchHarnessManifest

    def evaluate(self, **_kwargs):
        raise AssertionError("not used by contract tests")


def scorer(source: ScienceAgentBenchSourceManifest) -> ScienceAgentBenchScorer:
    manifest = ScienceAgentBenchHarnessManifest(
        official_repository_commit=source.repository_commit,
        candidate_image_id=ZERO_IMAGE,
        scorer_image_id=ONE_IMAGE,
        scorer_entrypoint_sha256="3" * 64,
        candidate_environment=ENVIRONMENT,
        scorer_environment=ENVIRONMENT,
        supported_instance_requirements={"1": ("scikit-learn",), "2": ("scikit-learn",)},
    )
    return ScienceAgentBenchScorer(
        harness=NeverHarness(manifest), source_manifest_sha256=source.manifest_sha256
    )


def test_loader_verifies_schema_hash_row_count_and_contiguous_ids(tmp_path):
    annotation = tmp_path / "verified.csv"
    records = [instance("1").model_dump(), instance("2").model_dump()]
    write_csv(annotation, records)
    source = source_for(annotation, rows=2)

    loaded = ScienceAgentBenchAdapter(source).load_instances(annotation)

    assert [item.instance_id for item in loaded] == ["1", "2"]
    annotation.write_bytes(annotation.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash"):
        ScienceAgentBenchAdapter(source).load_instances(annotation)


def test_loader_rejects_schema_drift_and_non_contiguous_ids(tmp_path):
    annotation = tmp_path / "verified.csv"
    record = instance("2").model_dump()
    write_csv(annotation, [record])
    source = source_for(annotation)
    with pytest.raises(ValueError, match="contiguous"):
        ScienceAgentBenchAdapter(source).load_instances(annotation)

    annotation.write_text("instance_id,domain\n1,Bioinformatics\n")
    source = source_for(annotation)
    with pytest.raises(ValueError, match="schema"):
        ScienceAgentBenchAdapter(source).load_instances(annotation)


def test_subset_defaults_to_cc_by_and_exception_licenses_require_opt_in(tmp_path):
    annotation = tmp_path / "verified.csv"
    records = [instance(str(index)).model_dump() for index in range(1, 85)]
    write_csv(annotation, records)
    source = source_for(annotation, rows=84).model_copy(update={"contiguous_instance_ids": True})
    source = ScienceAgentBenchSourceManifest.model_validate(source.model_dump())
    adapter = ScienceAgentBenchAdapter(source)
    loaded = adapter.load_instances(annotation)

    selected, manifest = adapter.select_subset(loaded, instance_ids=("16", "21", "29", "40"))
    assert [item.instance_id for item in selected] == ["16", "21", "29", "40"]
    assert set(manifest.license_by_instance.values()) == {"CC-BY-4.0"}

    for exceptional in ORIGINAL_LICENSE_INSTANCE_IDS:
        with pytest.raises(ValueError, match="opt-in"):
            adapter.select_subset(loaded, instance_ids=(exceptional,))
    selected, manifest = adapter.select_subset(
        loaded, instance_ids=("3",), allow_original_licenses=True
    )
    assert selected[0].instance_id == "3"
    assert manifest.license_by_instance == {"3": "upstream-original"}


def test_asset_receipt_and_task_hide_evaluator_details(tmp_path):
    root = tmp_path / "benchmark"
    data = root / "datasets" / "tiny"
    evaluators = root / "eval_programs"
    data.mkdir(parents=True)
    evaluators.mkdir()
    (data / "observations.csv").write_text("x,y\n1,3\n2,5\n")
    (evaluators / "tiny_eval.py").write_text("def eval(): return 1, 'ok'\n")
    source = ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="json",
        annotation_sha256="4" * 64,
        annotation_rows=1,
    )
    adapter = ScienceAgentBenchAdapter(source)
    receipt = adapter.freeze_assets(
        instance=instance(), benchmark_root=root, benchmark_archive_sha256="5" * 64
    )
    assert receipt.dataset_roots == ("tiny",)
    assert receipt.dataset_file_count == 1
    assert receipt.dataset_total_bytes == len("x,y\n1,3\n2,5\n".encode())
    benchmark_scorer = scorer(source)
    task = adapter.build_task(
        receipt=receipt,
        scorer=benchmark_scorer,
        resource_budget=ResourceBudget(wall_time_s=60, cpu_seconds=30, memory_mb=512),
    )

    public = json.dumps(task.public_view().model_dump(mode="json"))
    assert "eval_script_name" not in public
    assert "gold_program_name" not in public
    assert "tiny_eval.py" not in public
    assert "tiny.py" not in public
    assert SCIENCEAGENTBENCH_CANARY not in public
    assert task.expected_artifacts[0].kind == "program"
    assert task.scorer_sha256 == benchmark_scorer.scorer_sha256
    assert task.hidden_asset_sha256 == hashlib.sha256(receipt.to_bytes()).hexdigest()

    hidden = adapter.stage_hidden_asset(
        evaluator_root=tmp_path / "evaluation", task=task, receipt=receipt
    )
    assert hidden.read_bytes() == receipt.to_bytes()
    assert hidden.stat().st_mode & 0o222 == 0


def test_suite_is_bound_to_subset_order_tasks_and_scorer(tmp_path):
    annotation = tmp_path / "verified.csv"
    write_csv(annotation, [instance("1").model_dump(), instance("2").model_dump()])
    release = source_for(annotation, rows=2)
    adapter = ScienceAgentBenchAdapter(release)
    loaded = adapter.load_instances(annotation)
    selected, subset = adapter.select_subset(loaded, instance_ids=("1", "2"))
    benchmark_scorer = scorer(release)
    tasks = []
    for item in selected:
        asset = ScienceAgentBenchAssetReceipt(
            source_manifest_sha256=release.manifest_sha256,
            benchmark_archive_sha256="5" * 64,
            instance=item,
            task_license="CC-BY-4.0",
            dataset_roots=("tiny",),
            dataset_tree_sha256s={"tiny": "6" * 64},
            dataset_file_count=1,
            dataset_total_bytes=16,
            eval_program_sha256="7" * 64,
        )
        tasks.append(
            adapter.build_task(
                receipt=asset,
                scorer=benchmark_scorer,
                resource_budget=ResourceBudget(wall_time_s=60, cpu_seconds=30, memory_mb=512),
            )
        )

    suite = adapter.build_suite(tasks=tasks, subset_manifest=subset, scorer=benchmark_scorer)
    assert suite.task_manifest_sha256s == tuple(task.manifest_sha256 for task in tasks)
    assert suite.frozen is True
    with pytest.raises(ValueError, match="order"):
        adapter.build_suite(tasks=tasks[::-1], subset_manifest=subset, scorer=benchmark_scorer)


def test_asset_freeze_rejects_symlinks_and_path_escape(tmp_path):
    root = tmp_path / "benchmark"
    datasets = root / "datasets"
    evaluators = root / "eval_programs"
    datasets.mkdir(parents=True)
    evaluators.mkdir()
    (datasets / "tiny").mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("secret")
    (datasets / "tiny" / "escape.csv").symlink_to(outside)
    (evaluators / "tiny_eval.py").write_text("def eval(): return 1, 'ok'\n")
    source = ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="json",
        annotation_sha256="4" * 64,
        annotation_rows=1,
    )
    with pytest.raises(ValueError, match="symlink"):
        ScienceAgentBenchAdapter(source).freeze_assets(
            instance=instance(), benchmark_root=root, benchmark_archive_sha256="5" * 64
        )


def test_hidden_asset_staging_rejects_path_escape(tmp_path):
    release = ScienceAgentBenchSourceManifest(
        repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        dataset_revision="2" * 40,
        annotation_format="json",
        annotation_sha256="4" * 64,
        annotation_rows=1,
    )
    benchmark_scorer = scorer(release)
    escaped = EvaluationTask(
        task_id="scienceagentbench-1",
        version="test",
        layer=2,
        public_prompt="test",
        hidden_asset_ref="evaluator://hidden/../../outside.json",
        hidden_asset_sha256="7" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=({"kind": "program", "media_type": "text/x-python", "max_bytes": 100},),
        scorer_ref="evaluator://scorers/sab",
        scorer_sha256=benchmark_scorer.scorer_sha256,
    )
    fake_receipt = ScienceAgentBenchAssetReceipt(
        source_manifest_sha256=release.manifest_sha256,
        benchmark_archive_sha256="5" * 64,
        instance=instance(),
        task_license="CC-BY-4.0",
        dataset_roots=("tiny",),
        dataset_tree_sha256s={"tiny": "6" * 64},
        dataset_file_count=1,
        dataset_total_bytes=6,
        eval_program_sha256="7" * 64,
    )
    with pytest.raises(ValueError, match="escaped"):
        ScienceAgentBenchAdapter.stage_hidden_asset(
            evaluator_root=tmp_path / "evaluation", task=escaped, receipt=fake_receipt
        )


def test_harness_result_rejects_impossible_validity_claims():
    with pytest.raises(ValueError, match="valid programs"):
        ScienceAgentBenchHarnessResult(
            instance_id="1",
            run_index=0,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            valid_program=True,
            success_rate=1,
            program_returncode=2,
            program_exit_reason=ExecutionExitReason.PROCESS_ERROR,
            program_wall_time_s=0.01,
            program_log_sha256="6" * 64,
        )


def test_environment_contract_fails_closed_for_missing_base_or_task_packages():
    missing_base = dict(ENVIRONMENT, scipy="not-installed")
    with pytest.raises(ValueError, match="lacks required scientific packages"):
        ScienceAgentBenchHarnessManifest(
            official_repository_commit=OFFICIAL_REPOSITORY_COMMIT,
            candidate_image_id=ZERO_IMAGE,
            scorer_image_id=ONE_IMAGE,
            scorer_entrypoint_sha256="3" * 64,
            candidate_environment=missing_base,
            scorer_environment=ENVIRONMENT,
            supported_instance_requirements={"1": ("scikit-learn",)},
        )

    manifest = ScienceAgentBenchHarnessManifest(
        official_repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        candidate_image_id=ZERO_IMAGE,
        scorer_image_id=ONE_IMAGE,
        scorer_entrypoint_sha256="3" * 64,
        candidate_environment=ENVIRONMENT,
        scorer_environment=ENVIRONMENT,
        supported_instance_requirements={"1": ("deepchem",)},
    )
    with pytest.raises(ValueError, match="lacks reviewed requirements"):
        manifest.assert_instance_supported("1")
    with pytest.raises(ValueError, match="no reviewed environment contract"):
        manifest.assert_instance_supported("2")

    scorer_missing = ScienceAgentBenchHarnessManifest(
        official_repository_commit=OFFICIAL_REPOSITORY_COMMIT,
        candidate_image_id=ZERO_IMAGE,
        scorer_image_id=ONE_IMAGE,
        scorer_entrypoint_sha256="3" * 64,
        candidate_environment=ENVIRONMENT,
        scorer_environment=dict(ENVIRONMENT, rdkit="not-installed"),
        supported_instance_requirements={"16": ("rdkit",)},
    )
    with pytest.raises(ValueError, match="scorer image lacks reviewed requirements"):
        scorer_missing.assert_instance_supported("16")


def test_official_verified_csv_loads_exact_102_rows_when_available():
    annotation = Path("/private/tmp/scienceagentbench-verified.csv")
    if not annotation.exists():
        pytest.skip("pinned official public annotation is not staged")
    loaded = ScienceAgentBenchAdapter().load_instances(annotation)
    assert len(loaded) == 102
    assert loaded[0].instance_id == "1"
    assert loaded[-1].instance_id == "102"


def test_environment_probe_retries_only_stopped_container_closeout(tmp_path, monkeypatch):
    closeout = SandboxExecution(
        returncode=0,
        output='{"python":"3.11"}\n',
        image_id=ZERO_IMAGE,
        error="Docker client did not exit after its container stopped",
        container_started=True,
    )
    success = SandboxExecution(
        returncode=0,
        output='{"python":"3.11"}\n',
        image_id=ZERO_IMAGE,
        container_started=True,
    )
    results = iter((closeout, success))
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        return next(results)

    monkeypatch.setattr("aletheia.coder.executor.resolve_docker_image", lambda _ref: ZERO_IMAGE)
    monkeypatch.setattr("aletheia.evals.adapters.scienceagentbench.run_hardened_container", run)
    observed = _probe_docker_environment(ZERO_IMAGE, scratch_root=tmp_path)
    assert observed == {"python": "3.11"}
    assert len(calls) == 2


def test_environment_probe_does_not_retry_running_timeout(tmp_path, monkeypatch):
    calls = []

    def run(*_args, **_kwargs):
        calls.append(1)
        return SandboxExecution(
            returncode=None,
            image_id=ZERO_IMAGE,
            timed_out=True,
            error="hard sandbox timed out after 30s",
            container_started=True,
        )

    monkeypatch.setattr("aletheia.coder.executor.resolve_docker_image", lambda _ref: ZERO_IMAGE)
    monkeypatch.setattr("aletheia.evals.adapters.scienceagentbench.run_hardened_container", run)
    with pytest.raises(RuntimeError, match="timed out"):
        _probe_docker_environment(ZERO_IMAGE, scratch_root=tmp_path)
    assert len(calls) == 1
