"""Public benchmark repositories cross the evaluator boundary by exact, safe archives only."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.schemas import (
    ArtifactRequirement,
    ContaminationPolicy,
    EvaluationAttemptSlot,
    EvaluationPublicAsset,
    EvaluationRunPlan,
    EvaluationSuite,
    EvaluationTask,
    EvalLayer,
    ResourceBudget,
)

from .f7s2_fixtures import (
    EVALUATOR_HASH,
    SCORER_HASH,
    SYSTEM_HASH,
    ExactAnswerScorer,
    HardExecutor,
    SIGNING_KEY,
    write_submission,
)
from aletheia.evals.ledger import EvaluationLedger


def _archive(path: Path, files: dict[str, bytes], *, link: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    raw = buffer.getvalue()
    path.write_bytes(raw)
    return raw


def test_public_asset_view_hides_evaluator_ref_and_exact_archive_is_staged(tmp_path):
    source = tmp_path / "capsule.tar.gz"
    raw = _archive(source, {"code/run.py": b"print('science')\n", "data/x.csv": b"x\n1\n"})
    asset = EvaluationPublicAsset(
        asset_id="capsule-0000001",
        evaluator_ref="evaluator://public/corebench/capsule.tar.gz",
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        file_count=2,
        expanded_bytes=len(b"print('science')\n") + len(b"x\n1\n"),
        mount_path="capsule",
    )
    assert "evaluator_ref" not in asset.public_view().model_dump()
    staged = IndependentEvaluationRunner.stage_public_asset(
        evaluator_root=tmp_path / "evaluation", asset=asset, source=source
    )
    assert staged.read_bytes() == raw

    destination = tmp_path / "expanded"
    IndependentEvaluationRunner._safe_extract_public_archive(
        raw,
        destination=destination,
        expected_files=asset.file_count,
        expected_bytes=asset.expanded_bytes,
    )
    assert (destination / "code" / "run.py").read_text() == "print('science')\n"


def test_public_archive_rejects_traversal_symlink_and_expansion_mismatch(tmp_path):
    from aletheia.evals.runner import EvaluationRunnerError

    traversal = _archive(tmp_path / "traversal.tar.gz", {"../gold.json": b"secret"})
    with pytest.raises(EvaluationRunnerError, match="unsafe path"):
        IndependentEvaluationRunner._safe_extract_public_archive(
            traversal, destination=tmp_path / "one", expected_files=1, expected_bytes=6
        )

    linked = _archive(tmp_path / "link.tar.gz", {}, link="capsule/leak")
    with pytest.raises(EvaluationRunnerError, match="only directories"):
        IndependentEvaluationRunner._safe_extract_public_archive(
            linked, destination=tmp_path / "two", expected_files=1, expected_bytes=1
        )

    normal = _archive(tmp_path / "normal.tar.gz", {"file": b"1234"})
    with pytest.raises(EvaluationRunnerError, match="exceeds"):
        IndependentEvaluationRunner._safe_extract_public_archive(
            normal, destination=tmp_path / "three", expected_files=1, expected_bytes=3
        )


def test_formal_runner_stages_public_asset_before_executor_and_records_event(tmp_path):
    root = tmp_path / "evaluation"
    source = tmp_path / "capsule.tar.gz"
    raw = _archive(source, {"code/run.py": b"print(2)\n"})
    asset = EvaluationPublicAsset(
        asset_id="capsule-0000001",
        evaluator_ref="evaluator://public/corebench/capsule.tar.gz",
        sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw),
        file_count=1,
        expanded_bytes=len(b"print(2)\n"),
        mount_path="capsule",
    )
    IndependentEvaluationRunner.stage_public_asset(evaluator_root=root, asset=asset, source=source)
    hidden = root / "hidden_assets/task.json"
    hidden.parent.mkdir(parents=True)
    hidden_bytes = b'{"answer":"42"}'
    hidden.write_bytes(hidden_bytes)
    task = EvaluationTask(
        task_id="public-asset-test",
        version="1",
        layer=EvalLayer.SCIENTIFIC_REPRODUCTION,
        public_prompt="Inspect the capsule and answer.",
        hidden_asset_ref="evaluator://hidden/task.json",
        hidden_asset_sha256=hashlib.sha256(hidden_bytes).hexdigest(),
        resource_budget=ResourceBudget(wall_time_s=10, cpu_seconds=5, memory_mb=128),
        public_assets=(asset,),
        expected_artifacts=(
            ArtifactRequirement(kind="answer", media_type="application/json", max_bytes=1024),
        ),
        scorer_ref="evaluator://scorers/exact",
        scorer_sha256=SCORER_HASH,
        contamination_policy=ContaminationPolicy(test_access_limit=1),
    )
    suite = EvaluationSuite(
        suite_id="public-asset-suite",
        version="1",
        task_manifest_sha256s=(task.manifest_sha256,),
        scoring_policy_sha256="5" * 64,
    )
    plan = EvaluationRunPlan(
        plan_id="public-asset-plan",
        suite_manifest_sha256=suite.manifest_sha256,
        system_manifest_sha256=SYSTEM_HASH,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        slots=(EvaluationAttemptSlot(task_manifest_sha256=task.manifest_sha256, repeat_index=0, seed=1),),
    )

    def inspect_and_submit(context):
        assert (context.research_workspace / "capsule/code/run.py").read_text() == "print(2)\n"
        write_submission(context)

    executor = HardExecutor(inspect_and_submit)
    ledger = EvaluationLedger(root / "evaluator_ledger/events.jsonl")
    runner = IndependentEvaluationRunner(
        root=root,
        ledger=ledger,
        executor=executor,
        scorer=ExactAnswerScorer(),
        evaluator_manifest_sha256=EVALUATOR_HASH,
        receipt_key_id="test-key",
        receipt_signing_key=SIGNING_KEY,
    )
    outcome = runner.run(suite=suite, plan=plan, task=task, repeat_index=0)
    assert outcome.scorer_receipt is not None
    events = (root / "evaluator_ledger/events.jsonl").read_text()
    assert "public_assets_staged" in events
    assert asset.evaluator_ref not in events
