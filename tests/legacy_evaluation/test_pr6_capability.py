from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from aletheia.legacy_evaluation.capability import (
    LegacyEvaluationCapability,
    LegacyEvaluationCapabilityError,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationRawResult,
    canonical_json_bytes,
)

from conftest import LegacyEvaluationCase, LegacyEvaluationRun, SOURCE_ROOT


def test_real_materials_evaluation_emits_only_raw_declared_artifacts(
    legacy_evaluation_run: LegacyEvaluationRun,
) -> None:
    run = legacy_evaluation_run
    result = run.raw_result

    assert result.process_status == "process_succeeded"
    assert result.scientific_outcome == "not_assessed"
    assert result.raw_artifact_only is True
    assert result.scientific_admission_allowed is False
    assert result.claim_authority is False
    assert {item.name for item in result.metrics} == {
        item.metric_name for item in run.case.harness.metric_projections
    }
    assert {item.legacy_kind.value for item in result.artifacts} >= {"eval", "model"}
    metrics = {item.name: item.value for item in result.metrics}
    assert metrics["mae"] == metrics["mae_lcso"]
    assert metrics["rmse"] == metrics["rmse_lcso"]
    assert metrics["r2"] == metrics["r2_lcso"]
    assert all(math.isfinite(value) for value in metrics.values())
    assert all(metrics[name] >= 0 for name in metrics if name.startswith(("mae", "rmse")))
    assert metrics["mae_cv_std"] >= 0
    assert metrics["rmse"] >= metrics["mae"]
    assert metrics["rmse_holdout"] >= metrics["mae_holdout"]
    assert all(metrics[name] <= 1 for name in metrics if name.startswith("r2"))
    assert {item.relative_path for item in result.artifacts} | {"raw-result.json"} == {
        child.name for child in run.output_root.iterdir()
    }
    raw_result_bytes = (run.output_root / "raw-result.json").read_bytes()
    assert raw_result_bytes == canonical_json_bytes(result)
    assert LegacyEvaluationRawResult.model_validate_json(raw_result_bytes) == result
    assert hashlib.sha256(raw_result_bytes).hexdigest() == result.raw_result_sha256


def test_invocation_receipt_and_authorization_window_are_exact(
    legacy_evaluation_case: LegacyEvaluationCase,
    tmp_path: Path,
) -> None:
    case = legacy_evaluation_case
    capability = LegacyEvaluationCapability(
        plugin=case.plugin,
        harness=case.harness,
        protocol_manifest=case.manifest,
        source_root=SOURCE_ROOT,
    )

    with pytest.raises(LegacyEvaluationCapabilityError, match="input custody changed"):
        capability.execute(
            invocation=case.invocation,
            intent=case.intent,
            invocation_artifact_verified_receipt_sha256="f" * 64,
            input_table_path=case.input_table_path,
            output_root=tmp_path / "wrong-receipt",
        )

    broadened = case.intent.model_copy(
        update={"deadline": case.intent.deadline.replace(year=case.intent.deadline.year + 1)}
    )
    with pytest.raises(LegacyEvaluationCapabilityError, match="authorization or retry"):
        capability.execute(
            invocation=case.invocation,
            intent=broadened,
            invocation_artifact_verified_receipt_sha256=case.invocation_receipt_sha256,
            input_table_path=case.input_table_path,
            output_root=tmp_path / "broadened-window",
        )
