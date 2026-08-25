from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.execution.schemas import (
    DataLocality,
    ExecutionIntent,
    ExecutionResourceRequest,
    ExecutionRetryMode,
    ExecutionRetryPolicy,
    InfrastructureAttempt,
    InputArtifactBinding,
    NetworkPolicy,
    ScientificReplicateKind,
    ScientificReplicateSlot,
)
from aletheia.legacy_evaluation.capability import (
    INVOCATION_PORT_ID,
    TABLE_PORT_ID,
    build_legacy_evaluation_protocol_manifest,
    freeze_legacy_evaluation_harness,
    legacy_evaluation_expected_artifacts,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInputTable,
    LegacyEvaluationInvocation,
    LegacyEvaluationRawResult,
    LegacyEvaluationValidatorPin,
    LegacyMetricProjection,
    build_legacy_evaluation_invocation,
    legacy_evaluation_key_id,
)
from aletheia.protocols.capabilities import CapabilityManifestV2

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
SOURCE_ROOT = Path(__file__).resolve().parents[2]

_ROWS = (
    ("Si", 1.12),
    ("Ge", 0.67),
    ("GaAs", 1.42),
    ("GaN", 3.4),
    ("ZnO", 3.37),
    ("ZnS", 3.6),
    ("CdTe", 1.49),
    ("CdS", 2.42),
    ("NaCl", 8.5),
    ("MgO", 7.8),
    ("TiO2", 3.2),
    ("SiC", 3.0),
    ("AlN", 6.2),
    ("InP", 1.35),
    ("InAs", 0.36),
    ("PbS", 0.37),
    ("Cu2O", 2.1),
    ("Fe2O3", 2.2),
    ("SnO2", 3.6),
    ("WO3", 2.7),
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _metric_projections() -> tuple[LegacyMetricProjection, ...]:
    paths = {
        "mae": ("splits", "grouped_cv", "mae"),
        "mae_cv_mean": ("splits", "repeated_kfold_5x5", "mae_mean"),
        "mae_cv_std": ("splits", "repeated_kfold_5x5", "mae_std"),
        "mae_holdout": ("splits", "random_holdout", "mae"),
        "mae_lcso": ("splits", "grouped_cv", "mae"),
        "r2": ("splits", "grouped_cv", "r2"),
        "r2_cv_mean": ("splits", "repeated_kfold_5x5", "r2_mean"),
        "r2_holdout": ("splits", "random_holdout", "r2"),
        "r2_lcso": ("splits", "grouped_cv", "r2"),
        "rmse": ("splits", "grouped_cv", "rmse"),
        "rmse_holdout": ("splits", "random_holdout", "rmse"),
        "rmse_lcso": ("splits", "grouped_cv", "rmse"),
    }
    return tuple(
        LegacyMetricProjection(metric_name=name, eval_json_path=paths[name])
        for name in sorted(paths)
    )


def build_harness(plugin: MaterialsBandGapPlugin) -> LegacyEvaluationHarnessManifest:
    return freeze_legacy_evaluation_harness(
        plugin=plugin,
        source_root=SOURCE_ROOT,
        capability_id="capability.legacy-evaluation.materials.v1",
        semantic_version="1.0.0",
        allowed_design_keys=(
            "composition_column",
            "model",
            "model_params",
            "random_state",
            "target_column",
            "test_size",
        ),
        required_design_keys=(
            "composition_column",
            "model",
            "model_params",
            "random_state",
            "target_column",
            "test_size",
        ),
        allowed_model_names=("gradient_boosting", "random_forest"),
        frozen_random_seed=0,
        input_table_schema_sha256=digest("materials-band-gap-csv-v1"),
        metric_projections=_metric_projections(),
        maximum_input_bytes=1_048_576,
        maximum_rows=10_000,
        maximum_artifact_bytes=16_777_216,
        executor_principal_id="principal.legacy-evaluation-executor",
        frozen_by_principal_id="principal.capability-reviewer",
        frozen_at=NOW,
        additional_source_paths=(
            "aletheia/domains/materials/datasets.py",
            "aletheia/domains/materials/featurizers.py",
        ),
    )


def build_manifest(harness: LegacyEvaluationHarnessManifest) -> CapabilityManifestV2:
    return build_legacy_evaluation_protocol_manifest(
        harness=harness,
        environment_sha256=digest("pr6-conda-container-environment"),
        authority_policy_sha256=digest("pr6-execution-authority-policy"),
        qualification_rule_sha256=digest("pr6-capability-qualification-rule"),
        qualification_evidence_receipt_sha256s=tuple(
            sorted(
                digest(
                    "qualification-audit:capability.legacy-evaluation.materials.v1:" + audit_kind
                )
                for audit_kind in (
                    "applicability",
                    "failure_modes",
                    "license_egress",
                    "runtime",
                    "safety",
                    "sample_floor",
                )
            )
        ),
        qualified_by_principal_id="principal.capability-qualifier",
        qualified_at=NOW,
        safety_policy_sha256=digest("pr6-safety-policy"),
        license_policy_sha256=digest("pr6-license-policy"),
        egress_policy_sha256=digest("pr6-no-egress-policy"),
        retention_policy_sha256=digest("pr6-retention-policy"),
    )


@dataclass(frozen=True)
class LegacyEvaluationCase:
    plugin: MaterialsBandGapPlugin
    harness: LegacyEvaluationHarnessManifest
    manifest: CapabilityManifestV2
    invocation: LegacyEvaluationInvocation
    intent: ExecutionIntent
    invocation_receipt_sha256: str
    input_table_path: Path
    validator_private_key: bytes
    validator_pin: LegacyEvaluationValidatorPin


@pytest.fixture(scope="session")
def legacy_evaluation_case(tmp_path_factory: pytest.TempPathFactory) -> LegacyEvaluationCase:
    tmp_path = tmp_path_factory.mktemp("pr6-legacy-evaluation")
    plugin = MaterialsBandGapPlugin()
    harness = build_harness(plugin)
    manifest = build_manifest(harness)

    table_payload = "composition,band_gap\n" + "".join(
        f"{composition},{band_gap}\n" for composition, band_gap in _ROWS
    )
    input_table_path = tmp_path / "materials.csv"
    input_table_path.write_text(table_payload, encoding="utf-8")
    table_bytes = table_payload.encode("utf-8")
    table_receipt_sha256 = digest("verified-materials-table")
    table = LegacyEvaluationInputTable(
        artifact_verified_receipt_sha256=table_receipt_sha256,
        content_sha256=hashlib.sha256(table_bytes).hexdigest(),
        bytes=len(table_bytes),
        schema_sha256=harness.input_table_schema_sha256,
    )

    quest_id = "qst_" + digest("pr6-quest")[:32]
    protocol_sha256 = digest("pr6-protocol")
    work_order_id = "work-order.legacy-evaluation"
    work_order_sha256 = digest("pr6-work-order")
    node_id = "node.legacy-evaluation"
    node_sha256 = digest("pr6-work-order-node")
    slot = ScientificReplicateSlot(
        quest_id=quest_id,
        protocol_sha256=protocol_sha256,
        work_order_id=work_order_id,
        work_order_node_id=node_id,
        work_order_node_sha256=node_sha256,
        slot_count=1,
        slot_index=1,
        replicate_kind=ScientificReplicateKind.CONFIRMATION,
        preregistration_sha256=digest("pr6-preregistration"),
        randomization_seed_sha256=digest("pr6-randomization-seed"),
    )
    issued_at = NOW + timedelta(minutes=1)
    deadline = NOW + timedelta(hours=1)
    invocation = build_legacy_evaluation_invocation(
        quest_id=quest_id,
        protocol_sha256=protocol_sha256,
        work_order_id=work_order_id,
        work_order_sha256=work_order_sha256,
        work_order_node_id=node_id,
        work_order_node_sha256=node_sha256,
        replicate_slot_id=slot.replicate_slot_id,
        capability_id=manifest.capability_id,
        capability_manifest_sha256=manifest.manifest_sha256,
        harness_manifest_sha256=harness.manifest_sha256,
        input_table=table,
        design={
            "composition_column": "composition",
            "model": "random_forest",
            "model_params": {"n_estimators": 20},
            "random_state": 0,
            "target_column": "band_gap",
            "test_size": 0.25,
        },
        issued_at=issued_at,
        deadline=deadline,
    )
    invocation_receipt_sha256 = digest("verified-pr6-invocation")
    resource_class_id = "rsc_" + digest("pr6-cpu-resource-class")[:32]
    intent = ExecutionIntent(
        quest_id=quest_id,
        protocol_sha256=protocol_sha256,
        work_order_id=work_order_id,
        work_order_sha256=work_order_sha256,
        work_order_node_id=node_id,
        work_order_node_sha256=node_sha256,
        capability_id=manifest.capability_id,
        capability_manifest_sha256=manifest.manifest_sha256,
        resource_catalog_sha256=digest("pr6-resource-catalog"),
        resource_request=ExecutionResourceRequest(
            accepted_resource_class_ids=(resource_class_id,),
            cpu_cores=1,
            memory_bytes=2 * 1024**3,
            scratch_bytes=128 * 1024**2,
            wall_time_seconds=3_600,
            data_locality=DataLocality.ANY,
            network_policy=NetworkPolicy.NONE,
            max_infrastructure_attempts=1,
            artifact_quota_bytes=64 * 1024**2,
        ),
        retry_policy=ExecutionRetryPolicy(
            mode=ExecutionRetryMode.NEVER,
            maximum_attempts_per_scientific_slot=1,
        ),
        replicate_slot=slot,
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=(
            InputArtifactBinding(
                input_port_id=INVOCATION_PORT_ID,
                source_kind="protocol_input",
                artifact_verified_receipt_sha256=invocation_receipt_sha256,
            ),
            InputArtifactBinding(
                input_port_id=TABLE_PORT_ID,
                source_kind="protocol_input",
                artifact_verified_receipt_sha256=table_receipt_sha256,
            ),
        ),
        expected_artifacts=legacy_evaluation_expected_artifacts(
            harness=harness,
            retention_policy_sha256=manifest.license_egress.retention_policy_sha256,
        ),
        environment_sha256=manifest.runtime.environment_sha256,
        command_sha256=digest("pr6-command"),
        execution_parameters_sha256=invocation.execution_parameters_sha256,
        authorized_at=issued_at,
        deadline=deadline,
    )

    private_key = bytes(range(32))
    public_hex = (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    validator_pin = LegacyEvaluationValidatorPin(
        validator_principal_id="principal.independent-evaluation-validator",
        policy_sha256=digest("pr6-independent-validator-policy"),
        key_id=legacy_evaluation_key_id(public_hex),
        public_key_ed25519_hex=public_hex,
        trusted_harness_manifest_sha256s=(harness.manifest_sha256,),
        valid_from=NOW,
        expires_at=NOW + timedelta(days=30),
    )
    return LegacyEvaluationCase(
        plugin=plugin,
        harness=harness,
        manifest=manifest,
        invocation=invocation,
        intent=intent,
        invocation_receipt_sha256=invocation_receipt_sha256,
        input_table_path=input_table_path,
        validator_private_key=private_key,
        validator_pin=validator_pin,
    )


@dataclass(frozen=True)
class LegacyEvaluationRun:
    case: LegacyEvaluationCase
    raw_result: LegacyEvaluationRawResult
    output_root: Path


@pytest.fixture(scope="session")
def legacy_evaluation_run(
    legacy_evaluation_case: LegacyEvaluationCase,
    tmp_path_factory: pytest.TempPathFactory,
) -> LegacyEvaluationRun:
    from aletheia.legacy_evaluation.capability import LegacyEvaluationCapability

    case = legacy_evaluation_case

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the compatibility leaf called a forbidden end-to-end plugin method")

    case.plugin.load_data = forbidden  # type: ignore[method-assign]
    case.plugin.run_experiment = forbidden  # type: ignore[method-assign]
    timestamps = iter((NOW + timedelta(minutes=2), NOW + timedelta(minutes=3)))
    output_root = tmp_path_factory.mktemp("pr6-legacy-evaluation-output") / "artifacts"
    raw_result = LegacyEvaluationCapability(
        plugin=case.plugin,
        harness=case.harness,
        protocol_manifest=case.manifest,
        source_root=SOURCE_ROOT,
        clock=lambda: next(timestamps),
    ).execute(
        invocation=case.invocation,
        intent=case.intent,
        invocation_artifact_verified_receipt_sha256=case.invocation_receipt_sha256,
        input_table_path=case.input_table_path,
        output_root=output_root,
    )
    return LegacyEvaluationRun(case=case, raw_result=raw_result, output_root=output_root)
