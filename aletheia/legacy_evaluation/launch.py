"""Exact PR-4 launch specification for the qualified legacy-evaluation workload."""

from __future__ import annotations

from aletheia.execution.node_agent import (
    PinnedArtifactPath,
    PinnedEnvironmentVariable,
    PinnedInputPath,
    PinnedLaunchSpec,
)
from aletheia.execution.schemas import ExecutionIntent
from aletheia.legacy_evaluation.capability import (
    INVOCATION_PORT_ID,
    TABLE_PORT_ID,
    _validate_execution_binding,
    legacy_evaluation_artifact_paths,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInvocation,
)
from aletheia.protocols.capabilities import CapabilityManifestV2

WORKLOAD_EXECUTABLE_PATH = "/opt/aletheia/bin/legacy-evaluation-workload"
INVOCATION_RELATIVE_PATH = "legacy-evaluation-invocation.json"
TABLE_RELATIVE_PATH = "legacy-evaluation-table.csv"


def build_legacy_evaluation_launch_spec(
    *,
    invocation: LegacyEvaluationInvocation,
    intent: ExecutionIntent,
    harness: LegacyEvaluationHarnessManifest,
    protocol_manifest: CapabilityManifestV2,
    invocation_artifact_verified_receipt_sha256: str,
    executable_sha256: str,
    runtime_engine: str = "docker",
) -> PinnedLaunchSpec:
    """Freeze the direct executable, staged input names, and declared output tree."""

    invocation = LegacyEvaluationInvocation.model_validate(invocation.model_dump(mode="python"))
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    harness = LegacyEvaluationHarnessManifest.model_validate(harness.model_dump(mode="python"))
    protocol_manifest = CapabilityManifestV2.model_validate(
        protocol_manifest.model_dump(mode="python")
    )
    _validate_execution_binding(
        invocation=invocation,
        intent=intent,
        harness=harness,
        protocol_manifest=protocol_manifest,
        invocation_artifact_verified_receipt_sha256=(invocation_artifact_verified_receipt_sha256),
    )
    inputs = tuple(
        sorted(
            (
                PinnedInputPath(
                    input_port_id=INVOCATION_PORT_ID,
                    relative_path=INVOCATION_RELATIVE_PATH,
                ),
                PinnedInputPath(
                    input_port_id=TABLE_PORT_ID,
                    relative_path=TABLE_RELATIVE_PATH,
                ),
            ),
            key=lambda item: item.input_port_id,
        )
    )
    artifacts = tuple(
        PinnedArtifactPath(artifact_key=key, relative_path=relative_path)
        for key, relative_path in sorted(legacy_evaluation_artifact_paths().items())
    )
    environment = tuple(
        sorted(
            (
                PinnedEnvironmentVariable(name="LC_ALL", value="C.UTF-8"),
                PinnedEnvironmentVariable(name="MKL_NUM_THREADS", value="1"),
                PinnedEnvironmentVariable(name="NUMEXPR_NUM_THREADS", value="1"),
                PinnedEnvironmentVariable(name="OMP_NUM_THREADS", value="1"),
                PinnedEnvironmentVariable(name="OPENBLAS_NUM_THREADS", value="1"),
                PinnedEnvironmentVariable(name="PYTHONHASHSEED", value="0"),
            ),
            key=lambda item: item.name,
        )
    )
    return PinnedLaunchSpec(
        command_sha256=intent.command_sha256,
        environment_sha256=intent.environment_sha256,
        capability_manifest_sha256=intent.capability_manifest_sha256,
        executable_sha256=executable_sha256,
        runtime_engine=runtime_engine,
        argv=(WORKLOAD_EXECUTABLE_PATH,),
        environment=environment,
        input_paths=inputs,
        artifact_paths=artifacts,
    )


__all__ = [
    "INVOCATION_RELATIVE_PATH",
    "TABLE_RELATIVE_PATH",
    "WORKLOAD_EXECUTABLE_PATH",
    "build_legacy_evaluation_launch_spec",
]
