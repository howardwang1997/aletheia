from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.legacy_evaluation.capability import legacy_evaluation_artifact_paths
from aletheia.legacy_evaluation.contracts import canonical_json_bytes
from aletheia.legacy_evaluation.handler import (
    LegacyEvaluationHandlerError,
    run_qualified_legacy_evaluation_handler,
)
from aletheia.legacy_evaluation.launch import (
    INVOCATION_RELATIVE_PATH,
    TABLE_RELATIVE_PATH,
    WORKLOAD_EXECUTABLE_PATH,
    build_legacy_evaluation_launch_spec,
)

from conftest import LegacyEvaluationCase, SOURCE_ROOT, digest


def _handler_paths(tmp_path: Path, case: LegacyEvaluationCase) -> tuple[Path, Path, Path]:
    harness_path = tmp_path / "harness.json"
    harness_path.write_bytes(canonical_json_bytes(case.harness))
    invocation_path = tmp_path / "invocation.json"
    invocation_path.write_bytes(canonical_json_bytes(case.invocation))
    return harness_path, invocation_path, case.input_table_path


def test_fixed_handler_executes_only_the_qualified_materials_leaf(
    legacy_evaluation_case: LegacyEvaluationCase,
    tmp_path: Path,
) -> None:
    case = legacy_evaluation_case
    harness_path, invocation_path, table_path = _handler_paths(tmp_path, case)
    times = iter((case.invocation.issued_at + timedelta(seconds=1),) * 2)

    result = run_qualified_legacy_evaluation_handler(
        source_root=SOURCE_ROOT,
        harness_path=harness_path,
        invocation_path=invocation_path,
        table_path=table_path,
        output_root=tmp_path / "output",
        clock=lambda: next(times),
    )

    assert result.process_status == "process_succeeded"
    assert result.scientific_outcome == "not_assessed"
    assert result.scientific_admission_allowed is False
    assert {item.relative_path for item in result.artifacts} | {"raw-result.json"} == {
        "eval.json",
        "model.bin",
        "raw-result.json",
    }


@pytest.mark.parametrize(
    "payload",
    (
        b'{"schema_name":"x","schema_name":"x"}',
        b'{"value":NaN}',
        b'{ "value": 1 }',
    ),
)
def test_handler_rejects_noncanonical_or_ambiguous_invocation(
    legacy_evaluation_case: LegacyEvaluationCase,
    tmp_path: Path,
    payload: bytes,
) -> None:
    case = legacy_evaluation_case
    harness_path, invocation_path, table_path = _handler_paths(tmp_path, case)
    invocation_path.write_bytes(payload)

    with pytest.raises(LegacyEvaluationHandlerError, match="image inputs are invalid"):
        run_qualified_legacy_evaluation_handler(
            source_root=SOURCE_ROOT,
            harness_path=harness_path,
            invocation_path=invocation_path,
            table_path=table_path,
            output_root=tmp_path / "output",
        )


def test_launch_spec_is_direct_fixed_and_bound_to_the_full_intent(
    legacy_evaluation_case: LegacyEvaluationCase,
) -> None:
    case = legacy_evaluation_case
    executable_sha256 = digest("pr6-qualified-handler-executable")

    spec = build_legacy_evaluation_launch_spec(
        invocation=case.invocation,
        intent=case.intent,
        harness=case.harness,
        protocol_manifest=case.manifest,
        invocation_artifact_verified_receipt_sha256=case.invocation_receipt_sha256,
        executable_sha256=executable_sha256,
    )

    assert spec.argv == (WORKLOAD_EXECUTABLE_PATH,)
    assert spec.executable_sha256 == executable_sha256
    assert spec.direct_exec_only is True
    assert spec.read_only_root_filesystem is True
    assert spec.network_policy.value == "none"
    assert {item.input_port_id: item.relative_path for item in spec.input_paths} == {
        "legacy.evaluation.invocation": INVOCATION_RELATIVE_PATH,
        "legacy.evaluation.table": TABLE_RELATIVE_PATH,
    }
    assert {item.artifact_key: item.relative_path for item in spec.artifact_paths} == dict(
        legacy_evaluation_artifact_paths()
    )

    with pytest.raises(Exception, match="input custody changed"):
        build_legacy_evaluation_launch_spec(
            invocation=case.invocation,
            intent=case.intent,
            harness=case.harness,
            protocol_manifest=case.manifest,
            invocation_artifact_verified_receipt_sha256="f" * 64,
            executable_sha256=executable_sha256,
        )


def test_candidate_image_is_digest_and_dependency_pinned() -> None:
    dockerfile = (SOURCE_ROOT / "docker/legacy-evaluation-runtime.Dockerfile").read_text(
        encoding="utf-8"
    )

    assert dockerfile.startswith("# Candidate PR-6 qualification-only image.")
    assert "FROM python:3.11-slim@sha256:" in dockerfile
    assert "legacy-evaluation-runtime-constraints-v1.txt" in dockerfile
    for requirement in (
        "cloudpickle==3.1.2",
        "cryptography==48.0.0",
        "joblib==1.5.3",
        "matminer==0.10.1",
        "numpy==2.4.6",
        "pandas==2.3.3",
        "pydantic==2.13.4",
        "pymatgen==2026.5.4",
        "scikit-learn==1.8.0",
    ):
        assert f'"{requirement}"' in dockerfile
    assert "COPY aletheia /opt/aletheia/src/aletheia" in dockerfile
    assert "/opt/aletheia/bin/qualification-launch-gate" in dockerfile
    assert "/opt/aletheia/bin/legacy-evaluation-workload" in dockerfile
    assert "ENTRYPOINT" not in dockerfile
    assert "CMD" not in dockerfile

    constraints = (
        SOURCE_ROOT / "configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt"
    ).read_text(encoding="utf-8")
    pinned = {line for line in constraints.splitlines() if line and not line.startswith("#")}
    assert len(pinned) == 52
    assert all("==" in item and not item.endswith("==") for item in pinned)
