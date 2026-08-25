from __future__ import annotations

from pathlib import Path

import pytest

from aletheia.legacy_evaluation.capability import (
    LegacyEvaluationCapability,
    LegacyEvaluationCapabilityError,
)
from aletheia.legacy_evaluation.contracts import canonical_sha256

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
    assert {item.name: item.value for item in result.metrics}["mae"] == {
        item.name: item.value for item in result.metrics
    }["mae_lcso"]
    assert {item.relative_path for item in result.artifacts} | {"raw-result.json"} == {
        child.name for child in run.output_root.iterdir()
    }
    assert canonical_sha256(result.metrics) == (
        "7bb0323c36f700527c580d669ce51e0d52e19cd5e4224d9d71eaeedaa712197e"
    )


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
