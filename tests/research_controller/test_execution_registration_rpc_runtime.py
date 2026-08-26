from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import aletheia.observations.execution_registration as registration_module
from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
from aletheia.execution.registration_custody import QualificationExecutionRegistrationConfig
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    ScientificExecutionRegistrationRPCPayload,
)
from aletheia.research_controller.step_executor import ControllerStepAuthorityBinding
from aletheia.research_controller_execution_registration_runtime import (
    build_execution_registration_rpc_service,
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

from test_execution_authorization_rpc_runtime import _fixture as _authorization_fixture  # noqa: E402
from test_execution_authorization_service import _case as _authorization_case  # noqa: E402
from test_terminal_runtime import _config as _terminal_config  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    authorization_root = tmp_path / "authorization"
    terminal_root = tmp_path / "terminal"
    registration_root = tmp_path / "registration"
    for root in (authorization_root, terminal_root, registration_root):
        root.mkdir()
    _authorization_deployment, authorization_config, _config_path, _domain_key = (
        _authorization_fixture(authorization_root)
    )
    terminal, _controller_manifest = _terminal_config(monkeypatch, terminal_root)
    bridge, _source, _service, _wakeup, _projection, _plan, _catalog, _binding = (
        _authorization_case()
    )
    authorization = bridge.authorization
    prepared_at = terminal.prepared_at
    custody = QualificationPreAdmissionCustodyConfig(
        artifact_store_root=terminal.artifact_store_root,
        artifact_verifier_principal_id=terminal.artifact_verifier_principal_id,
        artifact_object_store_id=terminal.artifact_object_store_id,
        artifact_max_object_bytes=terminal.artifact_max_object_bytes,
        authority_registry_root=terminal.authority_registry_root,
        authority_registry_filesystem_pin=terminal.authority_registry_filesystem_pin,
        pricing_authority_pin=terminal.pricing_authority_pin,
        source_budget_authority_pin=terminal.source_budget_authority_pin,
        qualification_authority_pin=terminal.qualification_authority_pin,
        terminal_verification_authority_pin=terminal.terminal_verification_authority_pin,
        input_resolver_principal_id=terminal.input_resolver_principal_id,
        prepared_at=terminal.prepared_at,
    )
    registration = QualificationExecutionRegistrationConfig(
        qualification_custody=custody,
        runtime_control_authority_pin=terminal.runtime_control_authority_pin,
        node_authorities=terminal.node_authorities,
        allowed_rate_card_sha256s=terminal.allowed_rate_card_sha256s,
        allowed_currency_codes=terminal.allowed_currency_codes,
        allocator_principal_id="principal:scientific-execution-registration-allocator",
        prepared_at=prepared_at,
    )
    binding = ControllerStepAuthorityBinding.model_validate(
        authorization_config["authority_binding"]
    )

    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_execution_registration_runtime.py"
    ).resolve()
    implementation = (repository_root / "aletheia/observations/execution_registration.py").resolve()
    socket_root = (registration_root / "socket").resolve()
    config_root = (registration_root / "config").resolve()
    receipt_secret_root = (registration_root / "receipt-secret").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"execution-registration-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id="principal:scientific-execution-registration-service",
        service_manifest_sha256=_sha("execution-registration-service-manifest"),
        service_policy_sha256=_sha("execution-registration-service-policy"),
        operations=(ControllerWorkerRPCOperation.REGISTER_EXECUTION,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "execution-registration.sock"),
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
        "schema_name": "aletheia.execution_registration_rpc_service_config",
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
        "kernel_reader": authorization_config["kernel_reader"],
        "authority_binding": binding.model_dump(mode="json"),
        "execution_authority_pin": bridge.execution_pin.model_dump(mode="json"),
        "validator_authority_pin": bridge.validator_pin.model_dump(mode="json"),
        "admission_authority_pin": bridge.admission_pin.model_dump(mode="json"),
        "qualification_registration": registration.model_dump(mode="json"),
        "registrar_implementation_source_path": str(implementation),
        "registrar_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "private_domain_signing_key_loaded": False,
        "runtime_control_signing_key_loaded": False,
        "execution_launch_allowed": False,
        "node_registry_mutation_allowed": False,
        "terminal_commit_allowed": False,
        "direct_kernel_mutation_allowed": False,
        "direct_observation_admission_allowed": False,
    }
    config_path = (config_root / "execution-registration.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_execution_registration_runtime"),
        composition_factory_attribute="build_execution_registration_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, authorization


def _registration_receipt(authorization):
    message = authorization.message
    binding = message.action_protocol_binding
    intent = message.qualification_bundle.intent
    registered_at = message.authorized_at + timedelta(seconds=1)
    return AtomicScientificExecutionRegistrationReceipt(
        authorization_sha256=authorization.authorization_sha256,
        quest_id=binding.action.quest_id,
        scientific_slot_id=message.scientific_slot_id,
        action_sha256=binding.action.object_sha256,
        execution_id=intent.execution_id,
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        qualification_bundle_sha256=message.qualification_bundle.bundle_sha256,
        qualification_grant_sha256=message.qualification_grant.grant_sha256,
        registered_at=registered_at,
        qualification_admission_sha256="a" * 64,
        resource_reservation_sha256="b" * 64,
        reserved_at=registered_at + timedelta(seconds=1),
    )


def test_checked_in_execution_registration_factory_is_keyless_and_operation_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, authorization = _fixture(monkeypatch, tmp_path)
    expected = _registration_receipt(authorization)

    class _Registrar:
        def __init__(self, **_kwargs) -> None:
            pass

        def register_and_reserve(self, candidate):
            assert candidate == authorization
            return expected

    monkeypatch.setattr(
        registration_module,
        "PostgreSQLAtomicScientificExecutionRegistrar",
        _Registrar,
    )
    handlers = build_execution_registration_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == (ControllerWorkerRPCOperation.REGISTER_EXECUTION,)
    handler = handlers.handler_for(ControllerWorkerRPCOperation.REGISTER_EXECUTION)
    assert (
        handler(ScientificExecutionRegistrationRPCPayload(authorization=authorization)) == expected
    )
    with pytest.raises(TypeError, match="another payload"):
        handler(object())
    assert config["private_domain_signing_key_loaded"] is False
    assert "signing_key" not in config


def test_guarded_rpc_runtime_loads_execution_registration_factory(
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


def test_execution_registration_factory_rejects_duplicate_or_authority_rebind(
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
        build_execution_registration_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["worker_process_principal_id"] = config["execution_authority_pin"]["principal_id"]
    with pytest.raises(ValueError, match="config is invalid"):
        build_execution_registration_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )


def test_execution_registration_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _authorization = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-registration-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
