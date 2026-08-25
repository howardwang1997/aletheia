"""Outer PR-6 adapter for one isolated legacy tabular-evaluation operation.

The adapter deliberately calls only ``featurize`` and ``train_evaluate`` on an explicitly injected,
source-pinned ``DomainPlugin``.  It never calls the plugin data loader, ``run_experiment``, profile,
demonstration hooks, the compute factory, or ``ExperimentDriver``.  Its result is an untrusted raw
artifact index suitable only for a separately composed validator.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import stat
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from aletheia.domains.base import DomainPlugin, ExperimentResult
from aletheia.execution.schemas import (
    ArtifactRole,
    ExecutionEffectClass,
    ExecutionIntent,
    ExecutionRetryMode,
    ExpectedArtifact,
    NetworkPolicy,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyArtifactKind,
    LegacyEvaluationArtifact,
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInvocation,
    LegacyEvaluationMetric,
    LegacyEvaluationRawResult,
    LegacyEvaluationSourceBinding,
    LegacyMetricProjection,
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
)
from aletheia.protocols.base import JsonSchemaRef
from aletheia.protocols.capabilities import (
    ApplicabilityContract,
    ArtifactKind,
    CalibrationContract,
    CalibrationMode,
    CapabilityManifestV2,
    CapabilityPort,
    DataClassification,
    DeterminismClass,
    FailureCategory,
    FailureDisposition,
    FailureMode,
    LicenseEgressContract,
    NetworkEgressMode,
    PortDirection,
    PortMultiplicity,
    PrincipalContract,
    PrincipalKind,
    QualificationContract,
    QualificationStatus,
    RetryContract,
    RetryMode,
    RuntimeContract,
    RuntimeKind,
    SafetyClass,
    SafetyContract,
    SideEffectClass,
)
from aletheia.protocols.claim_contracts import (
    ClaimAllowance,
    ClaimCeiling,
    ClaimKind,
    ClaimStrength,
    EpistemicKind,
    EvidenceModality,
    ReplicationTier,
)

INVOCATION_PORT_ID = "legacy.evaluation.invocation"
TABLE_PORT_ID = "legacy.evaluation.table"
EVAL_ARTIFACT_KEY = "legacy.evaluation.eval"
MODEL_ARTIFACT_KEY = "legacy.evaluation.model"
RAW_RESULT_ARTIFACT_KEY = "legacy.evaluation.raw_result"

EVAL_RELATIVE_PATH = "eval.json"
MODEL_RELATIVE_PATH = "model.bin"
RAW_RESULT_RELATIVE_PATH = "raw-result.json"

_OUTPUT_CONTRACT = (
    (EVAL_ARTIFACT_KEY, ArtifactKind.JSON, "application/json", EVAL_RELATIVE_PATH, True),
    (
        MODEL_ARTIFACT_KEY,
        ArtifactKind.MODEL,
        "application/octet-stream",
        MODEL_RELATIVE_PATH,
        True,
    ),
    (
        RAW_RESULT_ARTIFACT_KEY,
        ArtifactKind.JSON,
        "application/json",
        RAW_RESULT_RELATIVE_PATH,
        True,
    ),
)
_SOURCE_BASE = "aletheia/domains/base.py"
_SOURCE_PROTOCOL = "aletheia/domains/protocol.py"
_SOURCE_MATERIALS_INIT = "aletheia/domains/materials/__init__.py"
_SOURCE_CAPABILITY = "aletheia/legacy_evaluation/capability.py"
_SOURCE_CONTRACTS = "aletheia/legacy_evaluation/contracts.py"
_SOURCE_HANDLER = "aletheia/legacy_evaluation/handler.py"
_SOURCE_LAUNCH = "aletheia/legacy_evaluation/launch.py"
_SAFE_DESIGN_KEY = r"^[a-z][a-z0-9_]{0,63}$"
_SENSITIVE_PARAMETER_FRAGMENTS = (
    "credential",
    "file",
    "path",
    "secret",
    "token",
    "url",
)


class LegacyEvaluationCapabilityError(RuntimeError):
    """The compatibility operation or one of its frozen bindings failed closed."""


def _read_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise LegacyEvaluationCapabilityError(f"{label} cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise LegacyEvaluationCapabilityError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise LegacyEvaluationCapabilityError(f"{label} must be a nonempty regular file")
        if before.st_size > maximum_bytes:
            raise LegacyEvaluationCapabilityError(f"{label} exceeds its frozen byte ceiling")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise LegacyEvaluationCapabilityError(f"{label} changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LegacyEvaluationCapabilityError(f"{label} grew while it was read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise LegacyEvaluationCapabilityError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_once(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o400)
    except OSError as exc:
        raise LegacyEvaluationCapabilityError(
            "raw evaluation result cannot be published once"
        ) from exc
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise LegacyEvaluationCapabilityError(
                    "raw evaluation result write made no progress"
                )
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_relative_path(plugin: DomainPlugin, source_root: Path) -> str:
    source = inspect.getsourcefile(type(plugin))
    if source is None:
        raise LegacyEvaluationCapabilityError("legacy plugin source cannot be resolved")
    try:
        return (
            Path(source)
            .resolve(strict=True)
            .relative_to(source_root.resolve(strict=True))
            .as_posix()
        )
    except (OSError, ValueError) as exc:
        raise LegacyEvaluationCapabilityError(
            "legacy plugin source escaped the reviewed tree"
        ) from exc


def _source_bindings(
    *,
    plugin: DomainPlugin,
    source_root: Path,
    additional_source_paths: tuple[str, ...],
) -> tuple[LegacyEvaluationSourceBinding, ...]:
    if additional_source_paths != tuple(sorted(set(additional_source_paths))):
        raise LegacyEvaluationCapabilityError(
            "additional legacy evaluation sources must be unique and canonical"
        )
    paths = {
        _SOURCE_BASE,
        _SOURCE_PROTOCOL,
        _SOURCE_MATERIALS_INIT,
        _SOURCE_CAPABILITY,
        _SOURCE_CONTRACTS,
        _SOURCE_HANDLER,
        _SOURCE_LAUNCH,
        _source_relative_path(plugin, source_root),
        *additional_source_paths,
    }
    bindings: list[LegacyEvaluationSourceBinding] = []
    root = source_root.resolve(strict=True)
    for relative in sorted(paths):
        binding_path = LegacyEvaluationSourceBinding(
            relative_path=relative,
            content_sha256="0" * 64,
        ).relative_path
        path = root / binding_path
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise LegacyEvaluationCapabilityError(
                "legacy evaluation source escaped the reviewed tree"
            ) from exc
        payload = _read_regular_file(path, maximum_bytes=16 * 1024 * 1024, label=relative)
        bindings.append(
            LegacyEvaluationSourceBinding(
                relative_path=relative,
                content_sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(bindings)


def _plugin_class_ref(plugin: DomainPlugin) -> str:
    cls = type(plugin)
    return f"{cls.__module__}:{cls.__qualname__}"


def _assert_isolated_plugin(plugin: DomainPlugin) -> None:
    if not isinstance(plugin, DomainPlugin):
        raise LegacyEvaluationCapabilityError("legacy evaluation requires a DomainPlugin instance")
    if type(plugin).run_experiment is not DomainPlugin.run_experiment:
        raise LegacyEvaluationCapabilityError(
            "plugins that own an end-to-end control flow cannot become an evaluation capability"
        )
    if not type(plugin).__module__.startswith("aletheia.domains."):
        raise LegacyEvaluationCapabilityError("legacy evaluation plugin is outside the domain tree")


def freeze_legacy_evaluation_harness(
    *,
    plugin: DomainPlugin,
    source_root: Path,
    capability_id: str,
    semantic_version: str,
    allowed_design_keys: tuple[str, ...],
    required_design_keys: tuple[str, ...],
    allowed_model_names: tuple[str, ...],
    frozen_random_seed: int,
    input_table_schema_sha256: str,
    metric_projections: tuple[LegacyMetricProjection, ...],
    maximum_input_bytes: int,
    maximum_rows: int,
    maximum_artifact_bytes: int,
    executor_principal_id: str,
    frozen_by_principal_id: str,
    frozen_at: datetime,
    additional_source_paths: tuple[str, ...] = (),
) -> LegacyEvaluationHarnessManifest:
    """Freeze exact source bytes for one plugin that inherits the base evaluation composition."""

    _assert_isolated_plugin(plugin)
    return LegacyEvaluationHarnessManifest(
        capability_id=capability_id,
        semantic_version=semantic_version,
        plugin_name=plugin.name,
        plugin_class_ref=_plugin_class_ref(plugin),
        source_bindings=_source_bindings(
            plugin=plugin,
            source_root=source_root,
            additional_source_paths=additional_source_paths,
        ),
        allowed_design_keys=allowed_design_keys,
        required_design_keys=required_design_keys,
        allowed_model_names=allowed_model_names,
        frozen_random_seed=frozen_random_seed,
        input_table_schema_sha256=input_table_schema_sha256,
        metric_projections=metric_projections,
        maximum_input_bytes=maximum_input_bytes,
        maximum_rows=maximum_rows,
        maximum_artifact_bytes=maximum_artifact_bytes,
        executor_principal_id=executor_principal_id,
        frozen_by_principal_id=frozen_by_principal_id,
        frozen_at=frozen_at,
    )


def verify_legacy_evaluation_harness(
    *,
    plugin: DomainPlugin,
    manifest: LegacyEvaluationHarnessManifest,
    source_root: Path,
) -> None:
    """Fresh-rehash the adapter/base/shared-harness/plugin files without dynamic loading."""

    _assert_isolated_plugin(plugin)
    mandatory_paths = {
        _SOURCE_BASE,
        _SOURCE_PROTOCOL,
        _SOURCE_MATERIALS_INIT,
        _SOURCE_CAPABILITY,
        _SOURCE_CONTRACTS,
        _SOURCE_HANDLER,
        _SOURCE_LAUNCH,
        _source_relative_path(plugin, source_root),
    }
    frozen_paths = {item.relative_path for item in manifest.source_bindings}
    if not mandatory_paths.issubset(frozen_paths):
        raise LegacyEvaluationCapabilityError("legacy evaluation source freeze is incomplete")
    fresh_bindings = _source_bindings(
        plugin=plugin,
        source_root=source_root,
        additional_source_paths=tuple(sorted(frozen_paths - mandatory_paths)),
    )
    if (
        manifest.plugin_name != plugin.name
        or manifest.plugin_class_ref != _plugin_class_ref(plugin)
        or manifest.source_bindings != fresh_bindings
    ):
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation implementation differs from its freeze"
        )


def _schema_ref(schema_id: str, schema_sha256: str) -> JsonSchemaRef:
    return JsonSchemaRef(
        schema_id=schema_id,
        semantic_version="1.0.0",
        schema_sha256=schema_sha256,
    )


def _output_schema_sha256(artifact_key: str) -> str:
    if artifact_key == RAW_RESULT_ARTIFACT_KEY:
        return canonical_sha256(LegacyEvaluationRawResult.model_json_schema())
    return canonical_sha256(
        {
            "schema_name": "aletheia.legacy_evaluation_opaque_output",
            "schema_version": 1,
            "artifact_key": artifact_key,
            "raw_artifact_only": True,
        }
    )


def build_legacy_evaluation_protocol_manifest(
    *,
    harness: LegacyEvaluationHarnessManifest,
    environment_sha256: str,
    authority_policy_sha256: str,
    qualification_rule_sha256: str,
    qualification_evidence_receipt_sha256s: tuple[str, ...],
    qualified_by_principal_id: str,
    qualified_at: datetime,
    safety_policy_sha256: str,
    license_policy_sha256: str,
    egress_policy_sha256: str,
    retention_policy_sha256: str,
) -> CapabilityManifestV2:
    """Project a frozen leaf into the ordinary compiler catalog without Kernel special cases."""

    input_ports = tuple(
        sorted(
            (
                CapabilityPort(
                    port_id=INVOCATION_PORT_ID,
                    direction=PortDirection.INPUT,
                    schema_ref=_schema_ref(
                        "schema.legacy_evaluation.invocation",
                        canonical_sha256(LegacyEvaluationInvocation.model_json_schema()),
                    ),
                    artifact_kind=ArtifactKind.JSON,
                    data_classification=DataClassification.INTERNAL,
                    identity_lineage_required=True,
                    description="Exact PR-6 invocation derived from a compiled WorkOrder node.",
                ),
                CapabilityPort(
                    port_id=TABLE_PORT_ID,
                    direction=PortDirection.INPUT,
                    schema_ref=_schema_ref(
                        "schema.legacy_evaluation.table",
                        harness.input_table_schema_sha256,
                    ),
                    artifact_kind=ArtifactKind.TABLE,
                    data_classification=DataClassification.INTERNAL,
                    identity_lineage_required=True,
                    description="Already-verified, local CSV table; the legacy loader is not called.",
                ),
            ),
            key=lambda item: item.port_id,
        )
    )
    output_ports = tuple(
        CapabilityPort(
            port_id=artifact_key,
            direction=PortDirection.OUTPUT,
            schema_ref=_schema_ref(
                f"schema.{artifact_key.replace('.', '_')}",
                _output_schema_sha256(artifact_key),
            ),
            artifact_kind=kind,
            data_classification=DataClassification.INTERNAL,
            multiplicity=(PortMultiplicity.OPTIONAL if not required else PortMultiplicity.ONE),
            identity_lineage_required=True,
            description="Uninterpreted compatibility output; independent validation is mandatory.",
        )
        for artifact_key, kind, _media_type, _path, required in _OUTPUT_CONTRACT
    )
    failure_modes = tuple(
        sorted(
            (
                FailureMode(
                    failure_id="failure.legacy_evaluation.invalid_output",
                    category=FailureCategory.INVALID_OUTPUT,
                    description="The legacy harness returned an incomplete or structurally invalid output.",
                    detection_rule_sha256=canonical_sha256("legacy-evaluation-invalid-output-v1"),
                    disposition=FailureDisposition.TERMINAL,
                ),
                FailureMode(
                    failure_id="failure.legacy_evaluation.policy",
                    category=FailureCategory.POLICY,
                    description="The invocation differs from the frozen design/source policy.",
                    detection_rule_sha256=canonical_sha256("legacy-evaluation-policy-v1"),
                    disposition=FailureDisposition.BLOCKED,
                ),
                FailureMode(
                    failure_id="failure.legacy_evaluation.process",
                    category=FailureCategory.EXECUTION,
                    description="The isolated evaluation process did not complete.",
                    detection_rule_sha256=canonical_sha256("legacy-evaluation-process-v1"),
                    disposition=FailureDisposition.TERMINAL,
                ),
            ),
            key=lambda item: item.failure_id,
        )
    )
    return CapabilityManifestV2(
        capability_id=harness.capability_id,
        semantic_version=harness.semantic_version,
        operation_id="legacy.evaluation.tabular",
        title="Legacy tabular evaluation compatibility leaf",
        description=(
            "One source-pinned featurize/train/evaluate operation. It emits raw artifacts only; "
            "it is not a planner, validator, observation authority, or claim authority."
        ),
        input_ports=input_ports,
        output_ports=output_ports,
        side_effect_class=SideEffectClass.EPHEMERAL_WRITE,
        principal=PrincipalContract(
            executor_principal_id=harness.executor_principal_id,
            principal_kind=PrincipalKind.SERVICE,
            authority_policy_sha256=authority_policy_sha256,
            credential_class="credential.none",
            required_independence_groups=(
                "group.claim-approver",
                "group.parser",
                "group.validator",
            ),
        ),
        runtime=RuntimeContract(
            runtime_kind=RuntimeKind.DIGEST_PINNED_CONTAINER,
            adapter_ref=harness.adapter_ref,
            implementation_sha256=harness.implementation_sha256,
            environment_sha256=environment_sha256,
            determinism=DeterminismClass.FROZEN_SEEDS,
            frozen_seeds=(harness.frozen_random_seed,),
            maximum_wall_time_seconds=3_600,
            checkpoint_supported=False,
            reconciliation_supported=False,
        ),
        applicability=ApplicabilityContract(
            epistemic_kinds=(EpistemicKind.CHARACTERIZATION, EpistemicKind.ESTIMATION),
            domain_tags=tuple(sorted(("legacy-evaluation", harness.plugin_name))),
            required_condition_sha256s=(harness.manifest_sha256,),
            minimum_batch_size=1,
            maximum_batch_size=1,
        ),
        calibration=CalibrationContract(mode=CalibrationMode.NOT_APPLICABLE),
        failure_modes=failure_modes,
        retry=RetryContract(mode=RetryMode.NEVER, maximum_attempts_per_scientific_slot=1),
        safety=SafetyContract(
            safety_class=SafetyClass.CONTROLLED_COMPUTE,
            approval_policy_sha256=safety_policy_sha256,
        ),
        license_egress=LicenseEgressContract(
            license_policy_sha256=license_policy_sha256,
            permitted_input_classes=(DataClassification.INTERNAL,),
            output_license_ids=("LicenseRef-Legacy-Compatible",),
            network_egress=NetworkEgressMode.NONE,
            egress_policy_sha256=egress_policy_sha256,
            retention_policy_sha256=retention_policy_sha256,
        ),
        qualification=QualificationContract(
            status=QualificationStatus.QUALIFIED,
            qualification_rule_sha256=qualification_rule_sha256,
            evidence_receipt_sha256s=qualification_evidence_receipt_sha256s,
            qualified_by_principal_id=qualified_by_principal_id,
            qualified_at=qualified_at,
        ),
        claim_ceiling=ClaimCeiling(
            allowances=(
                ClaimAllowance(
                    kind=ClaimKind.DESCRIPTIVE,
                    maximum_strength=ClaimStrength.EXPLORATORY,
                ),
            ),
            required_evidence_modalities=(EvidenceModality.COMPUTATIONAL,),
            required_replication_tier=ReplicationTier.NONE,
            independent_validation_required=True,
            rationale=(
                "The legacy leaf supplies raw computational artifacts only. Any scientific use "
                "requires a graph-scoped independent validator and ordinary observation admission."
            ),
        ),
        frozen_by_principal_id=harness.frozen_by_principal_id,
        frozen_at=harness.frozen_at,
    )


def legacy_evaluation_expected_artifacts(
    *, harness: LegacyEvaluationHarnessManifest, retention_policy_sha256: str
) -> tuple[ExpectedArtifact, ...]:
    return tuple(
        ExpectedArtifact(
            artifact_key=artifact_key,
            role=ArtifactRole.RAW_OUTPUT,
            media_type=media_type,
            schema_sha256=_output_schema_sha256(artifact_key),
            required=required,
            max_bytes=harness.maximum_artifact_bytes,
            data_classification=DataClassification.INTERNAL.value,
            retention_policy_sha256=retention_policy_sha256,
        )
        for artifact_key, _kind, media_type, _path, required in _OUTPUT_CONTRACT
    )


def legacy_evaluation_artifact_paths() -> Mapping[str, str]:
    return {artifact_key: path for artifact_key, _kind, _media, path, _required in _OUTPUT_CONTRACT}


def _validate_design(
    invocation: LegacyEvaluationInvocation,
    harness: LegacyEvaluationHarnessManifest,
) -> dict[str, Any]:
    design = dict(invocation.design)
    keys = set(design)
    if keys - set(harness.allowed_design_keys) or not set(harness.required_design_keys).issubset(
        keys
    ):
        raise LegacyEvaluationCapabilityError("legacy evaluation design escaped its frozen keys")
    if any(re.fullmatch(_SAFE_DESIGN_KEY, key) is None for key in keys):
        raise LegacyEvaluationCapabilityError("legacy evaluation design key is not canonical")
    if design.get("model") not in harness.allowed_model_names:
        raise LegacyEvaluationCapabilityError("legacy evaluation requested an unreviewed model")
    if type(design.get("random_state")) is not int or design["random_state"] != (
        harness.frozen_random_seed
    ):
        raise LegacyEvaluationCapabilityError("legacy evaluation changed its frozen random seed")
    test_size = design.get("test_size")
    if type(test_size) not in {int, float} or not 0.1 <= float(test_size) <= 0.5:
        raise LegacyEvaluationCapabilityError("legacy evaluation test size is outside policy")
    parameters = design.get("model_params", {})
    if not isinstance(parameters, dict) or len(parameters) > 64:
        raise LegacyEvaluationCapabilityError("legacy evaluation model parameters are not bounded")
    for key, value in parameters.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(_SAFE_DESIGN_KEY, key) is None
            or any(fragment in key.lower() for fragment in _SENSITIVE_PARAMETER_FRAGMENTS)
            or type(value) not in {bool, int, float, str}
            or (type(value) is float and not math.isfinite(value))
            or (isinstance(value, str) and len(value) > 256)
        ):
            raise LegacyEvaluationCapabilityError("legacy evaluation model parameter is unsafe")
    for key, value in design.items():
        if key in {"model", "model_params", "random_state", "test_size"}:
            continue
        if not isinstance(value, (str, int, float, bool)) or (
            isinstance(value, str) and (not value or len(value) > 256)
        ):
            raise LegacyEvaluationCapabilityError("legacy evaluation design value is not scalar")
    return design


def _validate_execution_binding(
    *,
    invocation: LegacyEvaluationInvocation,
    intent: ExecutionIntent,
    harness: LegacyEvaluationHarnessManifest,
    protocol_manifest: CapabilityManifestV2,
    invocation_artifact_verified_receipt_sha256: str,
) -> None:
    intent = ExecutionIntent.model_validate(intent.model_dump(mode="python"))
    protocol_manifest = CapabilityManifestV2.model_validate(
        protocol_manifest.model_dump(mode="python")
    )
    if (
        protocol_manifest.manifest_sha256 != invocation.capability_manifest_sha256
        or protocol_manifest.manifest_sha256 != intent.capability_manifest_sha256
        or protocol_manifest.capability_id != harness.capability_id
        or protocol_manifest.capability_id != invocation.capability_id
        or protocol_manifest.capability_id != intent.capability_id
        or protocol_manifest.runtime.adapter_ref != harness.adapter_ref
        or protocol_manifest.runtime.implementation_sha256 != harness.implementation_sha256
        or protocol_manifest.principal.executor_principal_id != harness.executor_principal_id
        or protocol_manifest.runtime.environment_sha256 != intent.environment_sha256
    ):
        raise LegacyEvaluationCapabilityError("legacy evaluation capability binding changed")
    if (
        protocol_manifest.execution_effect_class is not ExecutionEffectClass.REPLAY_SAFE
        or intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        or intent.resource_request.network_policy is not NetworkPolicy.NONE
        or protocol_manifest.license_egress.network_egress is not NetworkEgressMode.NONE
    ):
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation requires replay-safe no-network execution"
        )
    if (
        intent.retry_policy.mode is not ExecutionRetryMode.NEVER
        or intent.retry_policy.maximum_attempts_per_scientific_slot != 1
        or intent.resource_request.max_infrastructure_attempts != 1
        or intent.authorized_at != invocation.issued_at
        or intent.deadline != invocation.deadline
    ):
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation authorization or retry envelope changed"
        )
    scope = (
        intent.quest_id,
        intent.protocol_sha256,
        intent.work_order_id,
        intent.work_order_sha256,
        intent.work_order_node_id,
        intent.work_order_node_sha256,
        intent.replicate_slot.replicate_slot_id,
        intent.execution_parameters_sha256,
    )
    expected_scope = (
        invocation.quest_id,
        invocation.protocol_sha256,
        invocation.work_order_id,
        invocation.work_order_sha256,
        invocation.work_order_node_id,
        invocation.work_order_node_sha256,
        invocation.replicate_slot_id,
        invocation.execution_parameters_sha256,
    )
    if scope != expected_scope:
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation invocation escaped its WorkOrder scope"
        )
    input_ids = tuple(item.input_port_id for item in intent.input_artifact_bindings)
    if input_ids != (INVOCATION_PORT_ID, TABLE_PORT_ID):
        raise LegacyEvaluationCapabilityError("legacy evaluation input ports are not exact")
    invocation_binding, table_binding = intent.input_artifact_bindings
    if (
        invocation_binding.source_kind != "protocol_input"
        or invocation_binding.artifact_verified_receipt_sha256
        != invocation_artifact_verified_receipt_sha256
        or table_binding.artifact_verified_receipt_sha256
        != invocation.input_table.artifact_verified_receipt_sha256
    ):
        raise LegacyEvaluationCapabilityError("legacy evaluation input custody changed")
    expected_artifacts = legacy_evaluation_expected_artifacts(
        harness=harness,
        retention_policy_sha256=protocol_manifest.license_egress.retention_policy_sha256,
    )
    if intent.expected_artifacts != expected_artifacts:
        raise LegacyEvaluationCapabilityError("legacy evaluation output contract changed")


def _prepare_output_root(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise LegacyEvaluationCapabilityError("legacy evaluation output root cannot be a symlink")
    try:
        candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
        root = candidate.resolve(strict=True)
        metadata = root.lstat()
    except OSError as exc:
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation output root is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or any(root.iterdir()):
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation output root must be an empty directory"
        )
    return root


def _artifact_path(root: Path, value: object, *, kind: str) -> Path:
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise LegacyEvaluationCapabilityError(
            f"legacy evaluation did not return one {kind} artifact"
        )
    uri = value.get("uri")
    if not isinstance(uri, str) or not uri:
        raise LegacyEvaluationCapabilityError(f"legacy evaluation {kind} artifact has no path")
    candidate = Path(uri)
    if candidate.is_symlink():
        raise LegacyEvaluationCapabilityError(f"legacy evaluation {kind} artifact is a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise LegacyEvaluationCapabilityError(
            f"legacy evaluation {kind} artifact is missing"
        ) from exc
    if not resolved.is_relative_to(root) or not stat.S_ISREG(metadata.st_mode):
        raise LegacyEvaluationCapabilityError(f"legacy evaluation {kind} artifact escaped output")
    return resolved


def _normalize_artifacts(
    *, root: Path, result: ExperimentResult, maximum_bytes: int
) -> tuple[LegacyEvaluationArtifact, ...]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in result.artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            raise LegacyEvaluationCapabilityError(
                "legacy evaluation returned an invalid artifact row"
            )
        grouped.setdefault(item["kind"], []).append(item)
    if set(grouped) - {"eval", "model", "plot"} or any(
        len(items) != 1 for items in grouped.values()
    ):
        raise LegacyEvaluationCapabilityError(
            "legacy evaluation returned unknown or repeated artifacts"
        )
    if any(kind not in grouped for kind in ("eval", "model")):
        raise LegacyEvaluationCapabilityError("legacy evaluation omitted a required artifact")

    normalized: list[LegacyEvaluationArtifact] = []
    mapping = {
        "eval": (EVAL_ARTIFACT_KEY, EVAL_RELATIVE_PATH, "application/json", True),
        "model": (MODEL_ARTIFACT_KEY, MODEL_RELATIVE_PATH, "application/octet-stream", True),
    }
    for kind in sorted(grouped):
        source = _artifact_path(root, grouped[kind][0], kind=kind)
        if kind == "plot":
            try:
                source.unlink()
            except OSError as exc:
                raise LegacyEvaluationCapabilityError(
                    "legacy evaluation optional plot could not be discarded"
                ) from exc
            continue
        artifact_key, relative_path, media_type, required = mapping[kind]
        target = root / relative_path
        if source != target:
            if target.exists() or target.is_symlink():
                raise LegacyEvaluationCapabilityError(
                    "legacy evaluation artifact target already exists"
                )
            os.replace(source, target)
        payload = _read_regular_file(target, maximum_bytes=maximum_bytes, label=f"{kind} artifact")
        normalized.append(
            LegacyEvaluationArtifact(
                artifact_key=artifact_key,
                legacy_kind=LegacyArtifactKind(kind),
                relative_path=relative_path,
                content_sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),
                media_type=media_type,
                required=required,
            )
        )
    return tuple(sorted(normalized, key=lambda item: item.artifact_key))


def execute_qualified_legacy_evaluation_workload(
    *,
    plugin: DomainPlugin,
    harness: LegacyEvaluationHarnessManifest,
    source_root: Path,
    invocation: LegacyEvaluationInvocation,
    input_table_path: Path,
    output_root: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LegacyEvaluationRawResult:
    """Run the launch-gated inner workload after PR-4 has bound the full Intent.

    The container receives no caller-selectable resource, retry, network, WorkOrder, or output
    policy. It independently rehashes the frozen source closure and staged table before invoking
    only the reviewed plugin's ``featurize`` and ``train_evaluate`` methods.
    """

    try:
        invocation = LegacyEvaluationInvocation.model_validate(invocation.model_dump(mode="python"))
        harness = LegacyEvaluationHarnessManifest.model_validate(harness.model_dump(mode="python"))
        verify_legacy_evaluation_harness(
            plugin=plugin,
            manifest=harness,
            source_root=source_root,
        )
        if (
            invocation.harness_manifest_sha256 != harness.manifest_sha256
            or invocation.capability_id != harness.capability_id
        ):
            raise LegacyEvaluationCapabilityError(
                "qualified workload invocation selected another frozen harness"
            )
        design = _validate_design(invocation, harness)
        table_bytes = _read_regular_file(
            input_table_path,
            maximum_bytes=harness.maximum_input_bytes,
            label="legacy evaluation input table",
        )
        if (
            len(table_bytes) != invocation.input_table.bytes
            or hashlib.sha256(table_bytes).hexdigest() != invocation.input_table.content_sha256
            or invocation.input_table.schema_sha256 != harness.input_table_schema_sha256
        ):
            raise LegacyEvaluationCapabilityError("legacy evaluation input table changed")
        root = _prepare_output_root(output_root)
        started_at = clock()
        if started_at < invocation.issued_at or started_at >= invocation.deadline:
            raise LegacyEvaluationCapabilityError(
                "legacy evaluation started outside its authorization"
            )

        import pandas as pd

        frame = pd.read_csv(BytesIO(table_bytes))
        if not 10 <= len(frame) <= harness.maximum_rows:
            raise LegacyEvaluationCapabilityError("legacy evaluation row count is outside policy")
        if len(frame.columns) != len(set(str(item) for item in frame.columns)):
            raise LegacyEvaluationCapabilityError("legacy evaluation input repeats a column")
        frame.attrs["data_spec"] = {}
        features, target, feature_names, groups = plugin.featurize(frame, design)
        result = plugin.train_evaluate(
            features,
            target,
            design,
            root,
            groups=groups,
        )
        if not isinstance(result, ExperimentResult):
            raise LegacyEvaluationCapabilityError("legacy evaluation returned another result type")
        result.info.setdefault("feature_count", len(feature_names))
        result.info.setdefault("n_rows", int(getattr(frame, "shape", [0])[0]))
        metric_names = tuple(item.metric_name for item in harness.metric_projections)
        if set(result.metrics) != set(metric_names):
            raise LegacyEvaluationCapabilityError("legacy evaluation metric surface changed")
        metrics = tuple(
            LegacyEvaluationMetric(name=name, value=float(result.metrics[name]))
            for name in sorted(result.metrics)
        )
        info_json = canonical_json_text(result.info)
        artifacts = _normalize_artifacts(
            root=root,
            result=result,
            maximum_bytes=harness.maximum_artifact_bytes,
        )
        ended_at = clock()
        if ended_at < started_at or ended_at >= invocation.deadline:
            raise LegacyEvaluationCapabilityError(
                "legacy evaluation ended outside its authorization"
            )
        raw_result = LegacyEvaluationRawResult(
            invocation_sha256=invocation.invocation_sha256,
            harness_manifest_sha256=harness.manifest_sha256,
            capability_manifest_sha256=invocation.capability_manifest_sha256,
            plugin_name=harness.plugin_name,
            executor_principal_id=harness.executor_principal_id,
            metrics=metrics,
            info_json=info_json,
            artifacts=artifacts,
            started_at=started_at,
            ended_at=ended_at,
        )
        _write_once(root / RAW_RESULT_RELATIVE_PATH, canonical_json_bytes(raw_result))
        declared_paths = {item.relative_path for item in artifacts} | {RAW_RESULT_RELATIVE_PATH}
        actual_paths = {
            item.relative_to(root).as_posix()
            for item in root.iterdir()
            if item.is_file() and not item.is_symlink()
        }
        if actual_paths != declared_paths or any(item.is_dir() for item in root.iterdir()):
            raise LegacyEvaluationCapabilityError("legacy evaluation left undeclared outputs")
        return raw_result
    except LegacyEvaluationCapabilityError:
        raise
    except Exception as exc:  # the source-pinned legacy plugin is an untrusted leaf
        raise LegacyEvaluationCapabilityError("legacy evaluation failed closed") from exc


class LegacyEvaluationCapability:
    """Source-pinned adapter that executes only the isolated tabular evaluation methods."""

    def __init__(
        self,
        *,
        plugin: DomainPlugin,
        harness: LegacyEvaluationHarnessManifest,
        protocol_manifest: CapabilityManifestV2,
        source_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        verify_legacy_evaluation_harness(
            plugin=plugin,
            manifest=harness,
            source_root=source_root,
        )
        if (
            protocol_manifest.capability_id != harness.capability_id
            or protocol_manifest.runtime.adapter_ref != harness.adapter_ref
            or protocol_manifest.runtime.implementation_sha256 != harness.implementation_sha256
            or protocol_manifest.principal.executor_principal_id != harness.executor_principal_id
        ):
            raise LegacyEvaluationCapabilityError(
                "protocol manifest differs from the legacy harness"
            )
        self._plugin = plugin
        self._harness = harness
        self._protocol_manifest = CapabilityManifestV2.model_validate(
            protocol_manifest.model_dump(mode="python")
        )
        self._source_root = Path(source_root)
        self._clock = clock

    def execute(
        self,
        *,
        invocation: LegacyEvaluationInvocation,
        intent: ExecutionIntent,
        invocation_artifact_verified_receipt_sha256: str,
        input_table_path: Path,
        output_root: Path,
    ) -> LegacyEvaluationRawResult:
        try:
            invocation = LegacyEvaluationInvocation.model_validate(
                invocation.model_dump(mode="python")
            )
            if invocation.harness_manifest_sha256 != self._harness.manifest_sha256:
                raise LegacyEvaluationCapabilityError("invocation selected another harness")
            _validate_execution_binding(
                invocation=invocation,
                intent=intent,
                harness=self._harness,
                protocol_manifest=self._protocol_manifest,
                invocation_artifact_verified_receipt_sha256=(
                    invocation_artifact_verified_receipt_sha256
                ),
            )
            return execute_qualified_legacy_evaluation_workload(
                plugin=self._plugin,
                harness=self._harness,
                source_root=self._source_root,
                invocation=invocation,
                input_table_path=input_table_path,
                output_root=output_root,
                clock=self._clock,
            )
        except LegacyEvaluationCapabilityError:
            raise
        except Exception as exc:  # the source-pinned legacy plugin is an untrusted leaf
            raise LegacyEvaluationCapabilityError("legacy evaluation failed closed") from exc


def execute_legacy_evaluation(
    *,
    plugin: DomainPlugin,
    harness: LegacyEvaluationHarnessManifest,
    protocol_manifest: CapabilityManifestV2,
    source_root: Path,
    invocation: LegacyEvaluationInvocation,
    intent: ExecutionIntent,
    invocation_artifact_verified_receipt_sha256: str,
    input_table_path: Path,
    output_root: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LegacyEvaluationRawResult:
    """Stateless adapter entry point named by ``CapabilityManifestV2.runtime.adapter_ref``."""

    return LegacyEvaluationCapability(
        plugin=plugin,
        harness=harness,
        protocol_manifest=protocol_manifest,
        source_root=source_root,
        clock=clock,
    ).execute(
        invocation=invocation,
        intent=intent,
        invocation_artifact_verified_receipt_sha256=(invocation_artifact_verified_receipt_sha256),
        input_table_path=input_table_path,
        output_root=output_root,
    )


__all__ = [
    "EVAL_ARTIFACT_KEY",
    "EVAL_RELATIVE_PATH",
    "INVOCATION_PORT_ID",
    "LegacyEvaluationCapability",
    "LegacyEvaluationCapabilityError",
    "MODEL_ARTIFACT_KEY",
    "MODEL_RELATIVE_PATH",
    "RAW_RESULT_ARTIFACT_KEY",
    "RAW_RESULT_RELATIVE_PATH",
    "TABLE_PORT_ID",
    "build_legacy_evaluation_protocol_manifest",
    "execute_legacy_evaluation",
    "execute_qualified_legacy_evaluation_workload",
    "freeze_legacy_evaluation_harness",
    "legacy_evaluation_artifact_paths",
    "legacy_evaluation_expected_artifacts",
    "verify_legacy_evaluation_harness",
]
