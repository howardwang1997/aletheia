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
from aletheia.execution.terminal_source import VerifiedQualificationRunLineageReader
from aletheia.observations import adapters as adapters_module
from aletheia.observations.adapters import (
    CommittedValidationSourceVerificationContext,
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.f9_v2_validation import (
    WriteOnceF9V2ValidationCampaignArchive,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    RPCCommittedValidationSource,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    ControllerWorkerRPCService,
    ScientificSlotLookupRPCPayload,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_committed_validation_runtime import (
    build_committed_validation_source_rpc_service,
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

from test_f9_v2_validation_rpc_runtime import _fixture as _validator_fixture  # noqa: E402
from test_scientific_bridge import _commit_validation, _validated_receipt  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _DirectTransport:
    def __init__(self, service: ControllerWorkerRPCService) -> None:
        self.service = service

    def exchange(self, _pin, request_bytes):
        return self.service.handle(request_bytes)


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    validator_root = tmp_path / "validator-base"
    validator_root.mkdir()
    validator_deployment, base, _base_path, case, _raw_run = _validator_fixture(
        monkeypatch,
        validator_root,
    )
    prepared_at = validator_deployment.prepared_at
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_committed_validation_runtime.py"
    ).resolve()
    implementation = (repository_root / "aletheia/observations/adapters.py").resolve()

    socket_root = (tmp_path / "committed-validation-socket").resolve()
    config_root = (tmp_path / "committed-validation-config").resolve()
    receipt_secret_root = (tmp_path / "committed-validation-receipt-secret").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    archive_root = Path(base["validation_archive"]["root"])
    archive_root.chmod(0o500)
    archive_metadata = archive_root.stat()
    validator_binding = ControllerStepAuthorityBinding.model_validate(base["authority_binding"])
    database_binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        principal_id=case.database_pin.principal_id,
        key_id=case.database_pin.key_id,
        policy_sha256=case.database_pin.policy_sha256,
        service_manifest_sha256=_sha("database-observation-service-manifest"),
        externally_deployed=True,
    )
    bindings = tuple(
        sorted((database_binding, validator_binding), key=lambda item: item.binding_sha256)
    )
    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"committed-validation-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id="principal.observation.committed-validation-source",
        service_manifest_sha256=_sha("committed-validation-source-service-manifest"),
        service_policy_sha256=_sha("committed-validation-source-service-policy"),
        operations=(ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION,),
        authority_binding_sha256s=tuple(item.binding_sha256 for item in bindings),
        socket_path=str(socket_root / "committed-validation.sock"),
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
        max_request_bytes=16 * 1024**2,
        max_response_bytes=16 * 1024**2,
    )
    config = {
        "schema_name": "aletheia.committed_validation_source_rpc_service_config",
        "schema_version": 1,
        "controller_id": base["controller_id"],
        "controller_manifest_sha256": base["controller_manifest_sha256"],
        "worker_process_principal_id": base["worker_process_principal_id"],
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "database_url_sha256": hashlib.sha256(
            get_settings().database_url.encode("utf-8")
        ).hexdigest(),
        "schema_revision": expected_schema_revision(),
        "kernel_reader": base["kernel_reader"],
        "authority_bindings": [item.model_dump(mode="json") for item in bindings],
        "database_authority_pin": case.database_pin.model_dump(mode="json"),
        "execution_authority_pin": case.execution_pin.model_dump(mode="json"),
        "validator_authority_pin": case.validator_pin.model_dump(mode="json"),
        "admission_authority_pin": case.admission_pin.model_dump(mode="json"),
        "qualification_reader": base["qualification_reader"],
        "artifact_verification_authority": base["artifact_verification_authority"],
        "validation_archive": {
            "root": str(archive_root),
            "owner_uid": archive_metadata.st_uid,
            "group_gid": archive_metadata.st_gid,
            "device_id": archive_metadata.st_dev,
            "inode": archive_metadata.st_ino,
            "directory_mode": stat.S_IMODE(archive_metadata.st_mode),
            "validator_manifest_sha256": validator_binding.service_manifest_sha256,
            "read_only": True,
            "campaign_publication_allowed": False,
        },
        "source_implementation_source_path": str(implementation),
        "source_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "private_domain_signing_key_loaded": False,
        "database_mutation_allowed": False,
        "execution_mutation_allowed": False,
        "campaign_publication_allowed": False,
        "validation_receipt_issuance_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }
    config_path = (config_root / "committed-validation.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_committed_validation_runtime"),
        composition_factory_attribute="build_committed_validation_source_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, case, bindings


def test_checked_in_committed_validation_factory_is_keyless_and_fully_verifying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case, bindings = _fixture(monkeypatch, tmp_path)
    committed = _commit_validation(case, _validated_receipt(case))
    action = case.authorization.message.action_protocol_binding.action
    slot = case.authorization.message.scientific_slot_id
    calls = []

    class _Source:
        def __init__(self, *, sessions, verification) -> None:
            assert callable(sessions)
            assert isinstance(verification, CommittedValidationSourceVerificationContext)
            assert isinstance(
                verification.action_authority,
                PostgreSQLResearchActionAuthorityAdapter,
            )
            assert isinstance(
                verification.raw_run_custody,
                PostgreSQLRawRunCustodyVerificationAdapter,
            )
            assert verification.qualification_custody is verification.raw_run_custody
            assert isinstance(
                verification.raw_run_custody._execution_lineage,
                VerifiedQualificationRunLineageReader,
            )
            assert not hasattr(verification.raw_run_custody._execution_lineage, "admit_and_reserve")
            assert isinstance(
                verification.validation_campaign_custody,
                WriteOnceF9V2ValidationCampaignArchive,
            )
            assert verification.validation_campaign_custody.read_only is True

        def load_committed_validation(self, **scope):
            calls.append(scope)
            return committed

    monkeypatch.setattr(
        adapters_module,
        "PostgreSQLCommittedObservationValidationSource",
        _Source,
    )
    handlers = build_committed_validation_source_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    payload = ScientificSlotLookupRPCPayload(
        quest_id=action.quest_id,
        action_sha256=action.object_sha256,
        scientific_slot_id=slot,
    )
    assert handlers.operations == (ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION,)
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION)(payload)
        == committed
    )
    rpc_service = ControllerWorkerRPCService(
        pin=deployment.service_pin,
        controller_id=deployment.controller_id,
        controller_manifest_sha256=deployment.controller_manifest_sha256,
        worker_process_principal_id=deployment.worker_process_principal_id,
        handlers=handlers,
        receipt_private_key=Path(deployment.receipt_private_key_path).read_bytes(),
        clock=lambda: deployment.prepared_at,
    )
    client = ControllerWorkerRPCClient(
        pin=deployment.service_pin,
        controller_id=deployment.controller_id,
        controller_manifest_sha256=deployment.controller_manifest_sha256,
        worker_process_principal_id=deployment.worker_process_principal_id,
        transport=_DirectTransport(rpc_service),
        clock=lambda: deployment.prepared_at,
    )
    source = RPCCommittedValidationSource(client, bindings)
    assert (
        source.load_committed_validation(
            quest_id=action.quest_id,
            action_sha256=action.object_sha256,
            scientific_slot_id=slot,
        )
        == committed
    )
    assert calls == [payload.model_dump(mode="python"), payload.model_dump(mode="python")]
    with pytest.raises(TypeError, match="another payload"):
        handlers.handler_for(ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION)(object())
    assert config["private_domain_signing_key_loaded"] is False
    assert config["database_mutation_allowed"] is False
    assert config["execution_mutation_allowed"] is False
    assert config["campaign_publication_allowed"] is False
    assert config["validation_receipt_issuance_allowed"] is False
    assert config["direct_observation_admission_allowed"] is False
    assert config["direct_kernel_mutation_allowed"] is False


def test_guarded_runtime_loads_committed_validation_source_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)

    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_committed_validation_factory_rejects_duplicate_identity_and_source_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case, _bindings = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_committed_validation_source_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    rebound = ControllerWorkerRPCServicePin.model_validate(
        {
            **deployment.service_pin.model_dump(mode="python", exclude={"service_id"}),
            "service_principal_id": case.database_pin.principal_id,
        }
    )
    rebound_deployment = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id", "service_pin"}),
            "service_pin": rebound,
        }
    )
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_committed_validation_source_rpc_service(
            deployment=rebound_deployment,
            configuration_bytes=canonical_json_bytes(
                {
                    **config,
                    "service_id": rebound.service_id,
                    "service_pin_sha256": rebound.pin_sha256,
                }
            ),
        )

    policy_rebound = ControllerWorkerRPCServicePin.model_validate(
        {
            **deployment.service_pin.model_dump(mode="python", exclude={"service_id"}),
            "service_policy_sha256": case.execution_pin.policy_sha256,
        }
    )
    policy_rebound_deployment = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id", "service_pin"}),
            "service_pin": policy_rebound,
        }
    )
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_committed_validation_source_rpc_service(
            deployment=policy_rebound_deployment,
            configuration_bytes=canonical_json_bytes(
                {
                    **config,
                    "service_id": policy_rebound.service_id,
                    "service_pin_sha256": policy_rebound.pin_sha256,
                }
            ),
        )

    config["source_implementation_source_sha256"] = _sha("source-drift")
    with pytest.raises(ValueError, match="byte pin"):
        build_committed_validation_source_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )


def test_committed_validation_factory_rejects_archive_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)
    archive = Path(config["validation_archive"]["root"])
    moved = archive.with_name("committed-validation-archive-old")
    archive.chmod(0o700)
    archive.rename(moved)
    moved.chmod(0o500)
    archive.mkdir(mode=0o500)
    archive.chmod(0o500)

    with pytest.raises(ValueError, match="differs from its custody pin"):
        build_committed_validation_source_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_committed_validation_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-committed-validation-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
