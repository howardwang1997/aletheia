from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.registration_custody import QualificationExecutionRegistrationConfig
from aletheia.execution.terminal_source import VerifiedQualificationRawRunMaterialReader
from aletheia.execution.terminal_runtime import QualificationTerminalReaderConfig
from aletheia.observations import adapters as adapters_module
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import ScientificSlotLookupRPCPayload
from aletheia.research_controller.step_executor import ControllerStepAuthorityBinding
from aletheia.research_controller_raw_run_source_runtime import (
    build_raw_run_source_rpc_service,
)
from aletheia.research_controller_rpc_runtime import (
    ControllerWorkerRPCProcessError,
    ControllerWorkerRPCServerDeployment,
    build_controller_worker_rpc_server_runtime,
)
from aletheia.research_kernel.schemas import canonical_json_bytes

_TEST_ROOT = Path(__file__).resolve().parent
_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
for _path in (_TEST_ROOT, _OBSERVATION_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_execution_registration_rpc_runtime import _fixture as _registration_fixture  # noqa: E402
from test_scientific_bridge import _bridge_case, _raw_run  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _reader_config(registration_payload) -> QualificationTerminalReaderConfig:
    registration = QualificationExecutionRegistrationConfig.model_validate(registration_payload)
    custody = registration.qualification_custody
    return QualificationTerminalReaderConfig(
        artifact_store_root=custody.artifact_store_root,
        artifact_verifier_principal_id=custody.artifact_verifier_principal_id,
        artifact_object_store_id=custody.artifact_object_store_id,
        artifact_max_object_bytes=custody.artifact_max_object_bytes,
        authority_registry_root=custody.authority_registry_root,
        authority_registry_filesystem_pin=custody.authority_registry_filesystem_pin,
        pricing_authority_pin=custody.pricing_authority_pin,
        source_budget_authority_pin=custody.source_budget_authority_pin,
        qualification_authority_pin=custody.qualification_authority_pin,
        terminal_verification_authority_pin=custody.terminal_verification_authority_pin,
        runtime_control_authority_pin=registration.runtime_control_authority_pin,
        node_authorities=registration.node_authorities,
        allowed_rate_card_sha256s=registration.allowed_rate_card_sha256s,
        allowed_currency_codes=registration.allowed_currency_codes,
        allocator_principal_id=registration.allocator_principal_id,
        input_resolver_principal_id=custody.input_resolver_principal_id,
        prepared_at=registration.prepared_at,
    )


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    registration_root = tmp_path / "registration-fixture"
    registration_root.mkdir()
    (
        _registration_deployment,
        registration_config,
        _registration_path,
        authorization,
    ) = _registration_fixture(monkeypatch, registration_root)
    reader = _reader_config(registration_config["qualification_registration"])
    prepared_at = reader.prepared_at
    repository_root = Path(__file__).resolve().parents[2]
    factory = (repository_root / "aletheia/research_controller_raw_run_source_runtime.py").resolve()
    implementation = (repository_root / "aletheia/observations/adapters.py").resolve()

    socket_root = (tmp_path / "raw-run-socket").resolve()
    config_root = (tmp_path / "raw-run-config").resolve()
    receipt_secret_root = (tmp_path / "raw-run-receipt-secret").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"raw-run-source-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    binding = ControllerStepAuthorityBinding.model_validate(
        registration_config["authority_binding"]
    )
    pin = ControllerWorkerRPCServicePin(
        service_principal_id="principal:raw-run-source-service",
        service_manifest_sha256=_sha("raw-run-source-service-manifest"),
        service_policy_sha256=_sha("raw-run-source-service-policy"),
        operations=(ControllerWorkerRPCOperation.LOAD_RAW_RUN,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "raw-run-source.sock"),
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(receipt_public_key.hex()),
        receipt_public_key_ed25519_hex=receipt_public_key.hex(),
        valid_from=prepared_at - timedelta(minutes=1),
        expires_at=prepared_at + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=8 * 1024**2,
        max_response_bytes=8 * 1024**2,
    )
    config = {
        "schema_name": "aletheia.raw_run_source_rpc_service_config",
        "schema_version": 1,
        "controller_id": "rctl_" + "3" * 32,
        "controller_manifest_sha256": _sha("controller-manifest"),
        "worker_process_principal_id": "principal.controller.worker",
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "database_url_sha256": hashlib.sha256(
            get_settings().database_url.encode("utf-8")
        ).hexdigest(),
        "schema_revision": expected_schema_revision(),
        "authority_binding": binding.model_dump(mode="json"),
        "execution_authority_pin": registration_config["execution_authority_pin"],
        "validator_authority_pin": registration_config["validator_authority_pin"],
        "admission_authority_pin": registration_config["admission_authority_pin"],
        "qualification_reader": reader.model_dump(mode="json"),
        "source_implementation_source_path": str(implementation),
        "source_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "private_domain_signing_key_loaded": False,
        "execution_mutation_allowed": False,
        "database_mutation_allowed": False,
        "validation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }
    config_path = (config_root / "raw-run-source.json").resolve()
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
        composition_factory_module="aletheia.research_controller_raw_run_source_runtime",
        composition_factory_attribute="build_raw_run_source_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, authorization


def test_checked_in_raw_run_source_factory_is_keyless_read_only_and_operation_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, authorization = _fixture(monkeypatch, tmp_path)
    expected = _raw_run(_bridge_case())
    calls = []

    class _Source:
        def __init__(self, *, execution_material, **_kwargs) -> None:
            assert isinstance(execution_material, VerifiedQualificationRawRunMaterialReader)
            assert not hasattr(execution_material, "admit_and_reserve")

        def load_raw_run(self, **scope):
            calls.append(scope)
            return expected

    monkeypatch.setattr(adapters_module, "PostgreSQLRawRunEnvelopeSourceAdapter", _Source)
    handlers = build_raw_run_source_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == (ControllerWorkerRPCOperation.LOAD_RAW_RUN,)
    handler = handlers.handler_for(ControllerWorkerRPCOperation.LOAD_RAW_RUN)
    message = authorization.message
    binding = message.action_protocol_binding
    payload = ScientificSlotLookupRPCPayload(
        quest_id=binding.action.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=message.scientific_slot_id,
    )
    assert handler(payload) == expected
    assert calls == [payload.model_dump(mode="python")]
    with pytest.raises(TypeError, match="another payload"):
        handler(object())
    assert config["private_domain_signing_key_loaded"] is False
    assert config["execution_mutation_allowed"] is False
    assert "signing_key" not in config


def test_guarded_rpc_runtime_loads_raw_run_source_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _authorization = _fixture(monkeypatch, tmp_path)

    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_raw_run_source_factory_rejects_duplicate_authority_and_source_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _authorization = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_raw_run_source_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["worker_process_principal_id"] = config["execution_authority_pin"]["principal_id"]
    with pytest.raises(ValueError, match="config is invalid"):
        build_raw_run_source_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    config["worker_process_principal_id"] = deployment.worker_process_principal_id
    sandbox_policy = config["qualification_reader"]["node_authorities"][0]["manifest"][
        "sandbox_policy_sha256"
    ]
    conflicting_pin = ControllerWorkerRPCServicePin.model_validate(
        {
            **deployment.service_pin.model_dump(mode="python", exclude={"service_id"}),
            "service_policy_sha256": sandbox_policy,
        }
    )
    conflicting_deployment = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(
                mode="python",
                exclude={"runtime_id", "service_pin"},
            ),
            "service_pin": conflicting_pin,
        }
    )
    config["service_id"] = conflicting_pin.service_id
    config["service_pin_sha256"] = conflicting_pin.pin_sha256
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_raw_run_source_rpc_service(
            deployment=conflicting_deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    config["service_id"] = deployment.service_pin.service_id
    config["service_pin_sha256"] = deployment.service_pin.pin_sha256
    config["source_implementation_source_sha256"] = _sha("raw-run-source-drift")
    with pytest.raises(ValueError, match="byte pin"):
        build_raw_run_source_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )


def test_raw_run_source_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _authorization = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-raw-run-source-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
