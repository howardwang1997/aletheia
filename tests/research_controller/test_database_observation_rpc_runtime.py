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
from aletheia.observations import service as service_module
from aletheia.observations.adapters import (
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.f9_v2_validation import (
    WriteOnceF9V2ValidationCampaignArchive,
)
from aletheia.observations.scientific_bridge import (
    VerifiedExecutionAuthorityProjection,
    issue_admission_issuance_challenge,
)
from aletheia.observations.service import (
    AdmissionChallengeRegistrationReceipt,
    ValidationChallengeRegistrationReceipt,
    ValidationCommitReceipt,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    RPCDatabaseObservationBridge,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    AdmissionChallengeIssuanceRPCPayload,
    ControllerWorkerRPCService,
    ValidationChallengeIssuanceRPCPayload,
    ValidationCommitRPCPayload,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_database_observation_runtime import (
    build_database_observation_rpc_service,
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
from test_raw_run_source_rpc_runtime import _reader_config  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    DATABASE_PRIVATE_KEY,
    _bridge_case,
    _commit_validation,
    _digest,
    _raw_run,
    _validated_receipt,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _DirectTransport:
    def __init__(self, service: ControllerWorkerRPCService) -> None:
        self.service = service

    def exchange(self, _pin, request_bytes):
        return self.service.handle(request_bytes)


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
    case = _bridge_case()
    assert authorization == case.authorization
    prepared_at = reader.prepared_at
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_database_observation_runtime.py"
    ).resolve()
    implementation = (repository_root / "aletheia/observations/service.py").resolve()

    socket_root = (tmp_path / "database-observation-socket").resolve()
    config_root = (tmp_path / "database-observation-config").resolve()
    receipt_secret_root = (tmp_path / "database-observation-receipt-secret").resolve()
    database_secret_root = (tmp_path / "database-observation-domain-secret").resolve()
    validation_archive_root = (tmp_path / "f9-v2-validation-archive").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
        (database_secret_root, 0o700),
        (validation_archive_root, 0o500),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"database-observation-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        principal_id=case.database_pin.principal_id,
        key_id=case.database_pin.key_id,
        policy_sha256=case.database_pin.policy_sha256,
        service_manifest_sha256=_sha("database-observation-service-manifest"),
        externally_deployed=True,
    )
    operations = tuple(
        sorted(
            (
                ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
                ControllerWorkerRPCOperation.COMMIT_VALIDATION,
                ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
            ),
            key=lambda item: item.value,
        )
    )
    pin = ControllerWorkerRPCServicePin(
        service_principal_id=binding.principal_id,
        service_manifest_sha256=binding.service_manifest_sha256,
        service_policy_sha256=binding.policy_sha256,
        operations=operations,
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "database-observation.sock"),
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
    database_key_path = (database_secret_root / "database-attestation.key").resolve()
    database_key_path.write_bytes(DATABASE_PRIVATE_KEY)
    database_key_path.chmod(0o400)
    archive_metadata = validation_archive_root.stat()
    artifact_authority = VerifiedExecutionAuthorityProjection(
        principal_id=reader.artifact_verifier_principal_id,
        key_id=_sha("artifact-verification-key"),
        policy_sha256=_sha("artifact-verification-policy"),
    )
    config = {
        "schema_name": "aletheia.database_observation_rpc_service_config",
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
        "kernel_reader": registration_config["kernel_reader"],
        "authority_binding": binding.model_dump(mode="json"),
        "database_authority_pin": case.database_pin.model_dump(mode="json"),
        "database_signing_key": {
            "path": str(database_key_path),
            "file_sha256": hashlib.sha256(DATABASE_PRIVATE_KEY).hexdigest(),
            "key_id": case.database_pin.key_id,
            "owner_uid": process_uid,
            "owner_gid": process_gid,
            "file_mode": 0o400,
        },
        "execution_authority_pin": case.execution_pin.model_dump(mode="json"),
        "validator_authority_pin": case.validator_pin.model_dump(mode="json"),
        "admission_authority_pin": case.admission_pin.model_dump(mode="json"),
        "qualification_reader": reader.model_dump(mode="json"),
        "artifact_verification_authority": artifact_authority.model_dump(mode="json"),
        "validation_archive": {
            "root": str(validation_archive_root),
            "owner_uid": archive_metadata.st_uid,
            "group_gid": archive_metadata.st_gid,
            "device_id": archive_metadata.st_dev,
            "inode": archive_metadata.st_ino,
            "directory_mode": stat.S_IMODE(archive_metadata.st_mode),
            "validator_manifest_sha256": (
                authorization.message.admission_policy.validator_manifest_sha256
            ),
            "read_only": True,
            "campaign_publication_allowed": False,
        },
        "challenge_ttl_seconds": 300,
        "service_implementation_source_path": str(implementation),
        "service_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "database_attestation_signing_key_loaded": True,
        "validator_signing_key_loaded": False,
        "admission_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "execution_mutation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }
    config_path = (config_root / "database-observation.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_database_observation_runtime"),
        composition_factory_attribute="build_database_observation_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, case


def _receipts(case):
    raw_run = _raw_run(case)
    validation = _validated_receipt(case)
    committed = _commit_validation(case, validation)
    validation_challenge = ValidationChallengeRegistrationReceipt(
        challenge=validation.message.issuance_challenge,
        recorded_at=validation.message.issuance_challenge.message.issued_at + timedelta(seconds=1),
    )
    validation_commit = ValidationCommitReceipt(committed_validation=committed)
    issued_at = committed.message.committed_at + timedelta(minutes=1)
    admission_challenge = issue_admission_issuance_challenge(
        committed_validation_receipt=committed,
        nonce_sha256=_digest("database-runtime-admission-challenge"),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
    )
    admission_registration = AdmissionChallengeRegistrationReceipt(
        challenge=admission_challenge,
        recorded_at=issued_at + timedelta(seconds=1),
    )
    return (
        raw_run,
        validation,
        committed,
        validation_challenge,
        validation_commit,
        admission_registration,
    )


def test_checked_in_database_factory_is_key_isolated_read_only_and_operation_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case = _fixture(monkeypatch, tmp_path)
    (
        raw_run,
        validation,
        committed,
        expected_validation_challenge,
        expected_validation_commit,
        expected_admission_challenge,
    ) = _receipts(case)
    calls = []

    class _Service:
        def __init__(self, *, verification, challenge_ttl) -> None:
            assert verification.database_private_key == DATABASE_PRIVATE_KEY
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
            assert challenge_ttl == timedelta(minutes=5)

        def issue_validation_challenge(self, **kwargs):
            calls.append(("validation_challenge", kwargs))
            return expected_validation_challenge

        def commit_validation(self, receipt):
            calls.append(("validation_commit", receipt))
            return expected_validation_commit

        def issue_admission_challenge(self, receipt):
            calls.append(("admission_challenge", receipt))
            return expected_admission_challenge

    monkeypatch.setattr(service_module, "PostgreSQLScientificBridgeService", _Service)
    handlers = build_database_observation_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == tuple(
        sorted(
            (
                ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
                ControllerWorkerRPCOperation.COMMIT_VALIDATION,
                ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
            ),
            key=lambda item: item.value,
        )
    )
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE)(
            ValidationChallengeIssuanceRPCPayload(
                raw_run=raw_run,
                validation_campaign_sha256=(
                    validation.message.issuance_challenge.message.validation_campaign_sha256
                ),
            )
        )
        == expected_validation_challenge
    )
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.COMMIT_VALIDATION)(
            ValidationCommitRPCPayload(receipt=validation)
        )
        == expected_validation_commit
    )
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE)(
            AdmissionChallengeIssuanceRPCPayload(committed_validation=committed)
        )
        == expected_admission_challenge
    )
    assert [item[0] for item in calls] == [
        "validation_challenge",
        "validation_commit",
        "admission_challenge",
    ]
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
    bridge = RPCDatabaseObservationBridge(
        client,
        ControllerStepAuthorityBinding.model_validate(config["authority_binding"]),
    )
    assert (
        bridge.issue_validation_challenge(
            raw_run=raw_run,
            validation_campaign_sha256=(
                validation.message.issuance_challenge.message.validation_campaign_sha256
            ),
        )
        == expected_validation_challenge
    )
    assert bridge.commit_validation(validation) == expected_validation_commit
    assert bridge.issue_admission_challenge(committed) == expected_admission_challenge
    assert [item[0] for item in calls[3:]] == [
        "validation_challenge",
        "validation_commit",
        "admission_challenge",
    ]
    with pytest.raises(TypeError, match="another payload"):
        handlers.handler_for(ControllerWorkerRPCOperation.COMMIT_VALIDATION)(object())
    assert config["database_attestation_signing_key_loaded"] is True
    assert config["validator_signing_key_loaded"] is False
    assert config["admission_signing_key_loaded"] is False
    assert config["kernel_signing_key_loaded"] is False
    assert config["execution_mutation_allowed"] is False
    assert config["direct_observation_admission_allowed"] is False
    assert config["direct_kernel_mutation_allowed"] is False


def test_guarded_rpc_runtime_loads_database_observation_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case = _fixture(monkeypatch, tmp_path)

    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_database_factory_rejects_duplicate_authority_key_reuse_and_source_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_database_observation_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["worker_process_principal_id"] = config["database_authority_pin"]["principal_id"]
    with pytest.raises(ValueError, match="config is invalid"):
        build_database_observation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    config["worker_process_principal_id"] = deployment.worker_process_principal_id
    receipt_key = Path(deployment.receipt_private_key_path)
    config["database_signing_key"]["path"] = str(receipt_key)
    config["database_signing_key"]["file_sha256"] = deployment.receipt_private_key_sha256
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_database_observation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    original_key = Path(config_path.parent.parent / "database-observation-domain-secret")
    database_key = original_key / "database-attestation.key"
    config["database_signing_key"]["path"] = str(database_key)
    config["database_signing_key"]["file_sha256"] = hashlib.sha256(
        database_key.read_bytes()
    ).hexdigest()
    config["service_implementation_source_sha256"] = _sha("database-service-drift")
    with pytest.raises(ValueError, match="byte pin"):
        build_database_observation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )


def test_database_factory_rejects_f9_archive_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case = _fixture(monkeypatch, tmp_path)
    archive = Path(config["validation_archive"]["root"])
    moved = archive.with_name("f9-v2-validation-archive-old")
    archive.chmod(0o700)
    archive.rename(moved)
    moved.chmod(0o500)
    archive.mkdir(mode=0o500)
    archive.chmod(0o500)

    with pytest.raises(ValueError, match="differs from its custody pin"):
        build_database_observation_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_database_observation_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-database-observation-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
