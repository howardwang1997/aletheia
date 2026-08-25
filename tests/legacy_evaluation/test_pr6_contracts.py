from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from aletheia.domains.rag.plugin import RagEvalPlugin
from aletheia.legacy_evaluation.capability import (
    LegacyEvaluationCapabilityError,
    freeze_legacy_evaluation_harness,
    verify_legacy_evaluation_harness,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInvocation,
    LegacyMetricProjection,
)
from aletheia.protocols.capabilities import NetworkEgressMode, RuntimeKind

from conftest import NOW, SOURCE_ROOT, LegacyEvaluationCase, digest


def test_harness_freezes_the_full_reviewed_materials_source_surface(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    case = legacy_evaluation_case
    paths = {item.relative_path for item in case.harness.source_bindings}

    assert {
        "aletheia/domains/base.py",
        "aletheia/domains/materials/datasets.py",
        "aletheia/domains/materials/featurizers.py",
        "aletheia/domains/materials/__init__.py",
        "aletheia/domains/materials/matbench_task.py",
        "aletheia/domains/protocol.py",
        "aletheia/legacy_evaluation/capability.py",
        "aletheia/legacy_evaluation/contracts.py",
        "aletheia/legacy_evaluation/handler.py",
        "aletheia/legacy_evaluation/launch.py",
    } == paths
    verify_legacy_evaluation_harness(
        plugin=case.plugin,
        manifest=case.harness,
        source_root=SOURCE_ROOT,
    )

    first = case.harness.source_bindings[0]
    changed = first.model_copy(update={"content_sha256": "f" * 64})
    forged = case.harness.model_copy(
        update={"source_bindings": (changed, *case.harness.source_bindings[1:])}
    )
    with pytest.raises(LegacyEvaluationCapabilityError, match="differs from its freeze"):
        verify_legacy_evaluation_harness(
            plugin=case.plugin,
            manifest=forged,
            source_root=SOURCE_ROOT,
        )


def test_committed_materials_harness_is_exact_and_fresh(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    frozen = LegacyEvaluationHarnessManifest.model_validate_json(
        (SOURCE_ROOT / "configs/capabilities/legacy-evaluation-materials-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert frozen == legacy_evaluation_case.harness
    verify_legacy_evaluation_harness(
        plugin=legacy_evaluation_case.plugin,
        manifest=frozen,
        source_root=SOURCE_ROOT,
    )


def test_end_to_end_rag_override_cannot_be_registered_as_the_leaf() -> None:
    with pytest.raises(LegacyEvaluationCapabilityError, match="end-to-end control flow"):
        freeze_legacy_evaluation_harness(
            plugin=RagEvalPlugin(),
            source_root=SOURCE_ROOT,
            capability_id="capability.legacy-evaluation.rag.v1",
            semantic_version="1.0.0",
            allowed_design_keys=("model", "random_state", "test_size"),
            required_design_keys=("model", "random_state", "test_size"),
            allowed_model_names=("retrieval",),
            frozen_random_seed=0,
            input_table_schema_sha256=digest("rag-table"),
            metric_projections=(
                LegacyMetricProjection(metric_name="score", eval_json_path=("score",)),
            ),
            maximum_input_bytes=1_024,
            maximum_rows=10,
            maximum_artifact_bytes=1_024,
            executor_principal_id="principal.rag-evaluator",
            frozen_by_principal_id="principal.rag-reviewer",
            frozen_at=NOW,
        )


def test_invocation_hash_is_derived_and_closed(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    invocation = legacy_evaluation_case.invocation

    assert invocation.execution_parameters_sha256 == (
        invocation.derived_execution_parameters_sha256
    )
    with pytest.raises(ValidationError, match="parameters hash differs"):
        LegacyEvaluationInvocation.model_validate(
            invocation.model_copy(
                update={"execution_parameters_sha256": digest("caller-selected-hash")}
            ).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="deadline must follow"):
        LegacyEvaluationInvocation.model_validate(
            invocation.model_copy(
                update={"deadline": invocation.issued_at - timedelta(seconds=1)}
            ).model_dump(mode="python")
        )


def test_capability_manifest_is_atomic_raw_only_and_domain_neutral(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    manifest = legacy_evaluation_case.manifest

    assert manifest.atomic_operation is True
    assert manifest.operation_id == "legacy.evaluation.tabular"
    assert manifest.runtime.runtime_kind is RuntimeKind.DIGEST_PINNED_CONTAINER
    assert manifest.runtime.adapter_ref.endswith(":execute_legacy_evaluation")
    assert manifest.license_egress.network_egress is NetworkEgressMode.NONE
    assert manifest.claim_ceiling.independent_validation_required is True
    assert "ExperimentDriver" not in manifest.model_dump_json()
    assert "matbench" not in manifest.model_dump_json().lower()
    assert "mae" not in manifest.model_dump_json().lower()
