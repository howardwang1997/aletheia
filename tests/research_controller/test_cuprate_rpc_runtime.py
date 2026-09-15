"""Guarded-loader and handler regressions for the cuprate diagnostic RPC factory."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    CuprateDiagnosticResult,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import CuprateDiagnosticRPCPayload
from aletheia.research_controller_cuprate_runtime import build_cuprate_diagnostic_rpc_service
from aletheia.research_controller_rpc_runtime import ControllerWorkerRPCServerDeployment
from aletheia.research_kernel.schemas import canonical_json_bytes
from tests.execution.cuprate.test_diagnostics import _fixture_batch, synthetic_featurizer

_PREPARED_AT = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    factory = (repository_root / "aletheia/research_controller_cuprate_runtime.py").resolve()
    implementation = (repository_root / "aletheia/execution/cuprate/service.py").resolve()

    socket_root = (tmp_path / "cuprate-socket").resolve()
    config_root = (tmp_path / "cuprate-config").resolve()
    receipt_secret_root = (tmp_path / "cuprate-receipt-secret").resolve()
    data_root = (tmp_path / "cuprate-data").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
        (data_root, 0o750),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    formulas, targets = _fixture_batch()
    header = b"material,critical_temp\n"
    body = b"".join(
        f"{formula},{target!r}\n".encode() for formula, target in zip(formulas, targets)
    )
    csv_bytes = header + body
    staged_csv = (data_root / "superconduct_unique_m.csv").resolve()
    staged_csv.write_bytes(csv_bytes)
    staged_csv.chmod(0o400)

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"cuprate-diagnostic-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid, process_gid = os.geteuid(), os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id="principal:cuprate-diagnostic-service",
        service_manifest_sha256=_sha("cuprate-diagnostic-service-manifest"),
        service_policy_sha256=_sha("cuprate-diagnostic-service-policy"),
        operations=(ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC,),
        authority_binding_sha256s=(),
        socket_path=str(socket_root / "cuprate-diagnostic.sock"),
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(receipt_public_key.hex()),
        receipt_public_key_ed25519_hex=receipt_public_key.hex(),
        valid_from=_PREPARED_AT - timedelta(minutes=1),
        expires_at=_PREPARED_AT + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=8 * 1024**2,
        max_response_bytes=8 * 1024**2,
    )
    config = {
        "schema_name": "aletheia.cuprate_diagnostic_rpc_service_config",
        "schema_version": 1,
        "controller_id": "rctl_" + "3" * 32,
        "controller_manifest_sha256": _sha("controller-manifest"),
        "worker_process_principal_id": "principal.controller.worker",
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "dataset_csv_path": str(staged_csv),
        "dataset_content_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "source_implementation_source_path": str(implementation),
        "source_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": _PREPARED_AT.isoformat().replace("+00:00", "Z"),
        "private_domain_signing_key_loaded": False,
        "database_access_allowed": False,
        "execution_mutation_allowed": False,
        "validation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
        "network_egress_allowed": False,
    }
    config_path = (config_root / "cuprate-diagnostic.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    receipt_key_path = (receipt_secret_root / "receipt.key").resolve()
    receipt_key_path.write_bytes(receipt_private_key.private_bytes_raw())
    receipt_key_path.chmod(0o400)
    socket_metadata = socket_root.stat()
    deployment = ControllerWorkerRPCServerDeployment(
        service_pin=pin,
        controller_id=config["controller_id"],
        controller_manifest_sha256=config["controller_manifest_sha256"],
        worker_process_principal_id=config["worker_process_principal_id"],
        worker_peer_uid=process_uid + 1,
        worker_peer_gid=process_gid,
        process_uid=process_uid,
        process_gid=process_gid,
        socket_parent_path=str(socket_root),
        socket_parent_owner_uid=socket_metadata.st_uid,
        socket_parent_owner_gid=socket_metadata.st_gid,
        socket_parent_mode=stat.S_IMODE(socket_metadata.st_mode),
        socket_parent_device_id=socket_metadata.st_dev,
        socket_parent_inode=socket_metadata.st_ino,
        receipt_private_key_path=str(receipt_key_path),
        receipt_private_key_sha256=hashlib.sha256(receipt_key_path.read_bytes()).hexdigest(),
        reviewed_code_root=str(repository_root),
        composition_factory_module="aletheia.research_controller_cuprate_runtime",
        composition_factory_attribute="build_cuprate_diagnostic_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=_PREPARED_AT,
    )
    return deployment, config, config_path, tuple(formulas), staged_csv


def _payload(config, formulas) -> CuprateDiagnosticRPCPayload:
    return CuprateDiagnosticRPCPayload(
        expected_content_sha256=config["dataset_content_sha256"],
        composition_column="material",
        target_column="critical_temp",
        bound_batch_group_ids=tuple(sorted(set(formulas))),
        doping_optimum=0.16,
    )


@pytest.mark.parametrize(
    "batch",
    [
        ("SiO2", "Fe2O3"),  # unsorted
        ("SiO2", "SiO2"),  # duplicated
        ("SiO2", ""),  # empty id
        ("SiO2", "Fe2O3\nNb3Sn"),  # newline in id
    ],
)
def test_payload_rejects_non_canonical_batch_group_ids(batch):
    with pytest.raises(ValidationError, match="bound batch group ids"):
        CuprateDiagnosticRPCPayload(
            expected_content_sha256="0" * 64,
            composition_column="material",
            target_column="critical_temp",
            bound_batch_group_ids=batch,
            doping_optimum=0.16,
        )


def test_cuprate_factory_is_operation_closed_and_runs_the_pinned_dataset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aletheia.execution.cuprate import featurization

    monkeypatch.setattr(featurization, "magpie_features", synthetic_featurizer)
    deployment, config, config_path, formulas, _staged = _fixture(tmp_path)
    handlers = build_cuprate_diagnostic_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )
    assert handlers.operations == (ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC,)
    handler = handlers.handler_for(ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC)

    result = handler(_payload(config, formulas))
    assert isinstance(result, CuprateDiagnosticResult)
    assert result.dataset_content_sha256 == config["dataset_content_sha256"]
    assert result.analyzed_rows == len(formulas)
    assert result.dropped_off_batch_rows == 0
    assert result.d1_matched_control.n_cuprate >= 1
    assert result.d2_doping_stratification.family_holdout_rows >= 1

    other = CuprateDiagnosticRPCPayload(
        expected_content_sha256="0" * 64,
        composition_column="material",
        target_column="critical_temp",
        bound_batch_group_ids=tuple(sorted(set(formulas))),
        doping_optimum=0.16,
    )
    with pytest.raises(ValueError, match="another dataset content"):
        handler(other)
    with pytest.raises(TypeError, match="another payload"):
        handler(object())
    assert config["private_domain_signing_key_loaded"] is False
    assert config["database_access_allowed"] is False
    assert config["network_egress_allowed"] is False


def test_cuprate_factory_rejects_config_rebinds_and_custody_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aletheia.execution.cuprate import featurization

    monkeypatch.setattr(featurization, "magpie_features", synthetic_featurizer)
    deployment, config, config_path, _formulas, staged = _fixture(tmp_path)

    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_cuprate_diagnostic_rpc_service(deployment=deployment, configuration_bytes=duplicate)

    config["service_pin_sha256"] = _sha("other-pin")
    with pytest.raises(ValueError, match="differs from deployment"):
        build_cuprate_diagnostic_rpc_service(
            deployment=deployment, configuration_bytes=canonical_json_bytes(config)
        )
    config["service_pin_sha256"] = deployment.service_pin.pin_sha256

    config["source_implementation_source_sha256"] = _sha("drifted-implementation")
    with pytest.raises(ValueError, match="byte pin"):
        build_cuprate_diagnostic_rpc_service(
            deployment=deployment, configuration_bytes=canonical_json_bytes(config)
        )
    config["source_implementation_source_sha256"] = hashlib.sha256(
        Path(config["source_implementation_source_path"]).read_bytes()
    ).hexdigest()

    config["dataset_csv_path"] = str(Path(deployment.reviewed_code_root) / "staged.csv")
    with pytest.raises(ValueError, match="reviewed code custody"):
        build_cuprate_diagnostic_rpc_service(
            deployment=deployment, configuration_bytes=canonical_json_bytes(config)
        )
    config["dataset_csv_path"] = str(staged)
    staged.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe custody"):
        build_cuprate_diagnostic_rpc_service(
            deployment=deployment, configuration_bytes=canonical_json_bytes(config)
        )
    staged.chmod(0o400)
