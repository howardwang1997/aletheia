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
from aletheia.observations import admission_service as admission_module
from aletheia.observations.adapters import (
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.admission_service import IndependentAdmissionVerificationContext
from aletheia.observations.f9_v2_validation import (
    WriteOnceF9V2ValidationCampaignArchive,
)
from aletheia.observations.scientific_bridge import ObservationAdmissionDisposition
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    RPCIndependentObservationAdmission,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    AdmissionDecisionIssuanceRPCPayload,
    ControllerWorkerRPCService,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_independent_admission_runtime import (
    build_independent_admission_rpc_service,
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

from test_committed_validation_rpc_runtime import (  # noqa: E402
    _DirectTransport,
    _fixture as _source_fixture,
)
from test_scientific_bridge import (  # noqa: E402
    ADMISSION_PRIVATE_KEY,
    _issue_admission_decision,
    _validated_receipt,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source_root = tmp_path / "source-base"
    source_root.mkdir()
    _source_deployment, source, _source_path, case, _source_bindings = _source_fixture(
        monkeypatch,
        source_root,
    )
    prepared_at = case.authorization.message.authorized_at
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_independent_admission_runtime.py"
    ).resolve()
    implementation = (repository_root / "aletheia/observations/admission_service.py").resolve()

    socket_root = (tmp_path / "independent-admission-socket").resolve()
    config_root = (tmp_path / "independent-admission-config").resolve()
    receipt_secret_root = (tmp_path / "independent-admission-receipt-secret").resolve()
    admission_secret_root = (tmp_path / "independent-admission-domain-secret").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
        (admission_secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        principal_id=case.admission_pin.principal_id,
        key_id=case.admission_pin.key_id,
        policy_sha256=case.admission_pin.policy_sha256,
        service_manifest_sha256=_sha("independent-admission-service-manifest"),
        externally_deployed=True,
    )
    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"independent-admission-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id=binding.principal_id,
        service_manifest_sha256=binding.service_manifest_sha256,
        service_policy_sha256=binding.policy_sha256,
        operations=(ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "independent-admission.sock"),
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
    key_path = (admission_secret_root / "admission.key").resolve()
    key_path.write_bytes(ADMISSION_PRIVATE_KEY)
    key_path.chmod(0o400)
    config = {
        "schema_name": "aletheia.independent_admission_rpc_service_config",
        "schema_version": 1,
        "controller_id": source["controller_id"],
        "controller_manifest_sha256": source["controller_manifest_sha256"],
        "worker_process_principal_id": source["worker_process_principal_id"],
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "database_url_sha256": hashlib.sha256(
            get_settings().database_url.encode("utf-8")
        ).hexdigest(),
        "schema_revision": expected_schema_revision(),
        "kernel_reader": source["kernel_reader"],
        "authority_binding": binding.model_dump(mode="json"),
        "database_authority_pin": case.database_pin.model_dump(mode="json"),
        "execution_authority_pin": case.execution_pin.model_dump(mode="json"),
        "validator_authority_pin": case.validator_pin.model_dump(mode="json"),
        "admission_authority_pin": case.admission_pin.model_dump(mode="json"),
        "qualification_reader": source["qualification_reader"],
        "artifact_verification_authority": source["artifact_verification_authority"],
        "validation_archive": source["validation_archive"],
        "admission_signing_key": {
            "path": str(key_path),
            "file_sha256": hashlib.sha256(ADMISSION_PRIVATE_KEY).hexdigest(),
            "key_id": case.admission_pin.key_id,
            "owner_uid": process_uid,
            "group_gid": process_gid,
            "file_mode": 0o400,
        },
        "service_implementation_source_path": str(implementation),
        "service_implementation_source_sha256": hashlib.sha256(
            implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "admission_signing_key_loaded": True,
        "database_signing_key_loaded": False,
        "execution_signing_key_loaded": False,
        "validator_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "database_mutation_allowed": False,
        "execution_mutation_allowed": False,
        "campaign_publication_allowed": False,
        "validation_receipt_issuance_allowed": False,
        "scientific_slot_commit_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }
    config_path = (config_root / "independent-admission.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_independent_admission_runtime"),
        composition_factory_attribute="build_independent_admission_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, case, binding


def test_checked_in_independent_admission_factory_owns_only_admitter_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case, binding = _fixture(monkeypatch, tmp_path)
    receipt = _validated_receipt(case)
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    calls = []

    class _Service:
        def __init__(self, *, verification, admission_private_key, clock) -> None:
            assert isinstance(verification, IndependentAdmissionVerificationContext)
            assert admission_private_key == ADMISSION_PRIVATE_KEY
            assert callable(clock)
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

        def issue_admission_decision(self, **payload):
            calls.append(payload)
            return expected

    monkeypatch.setattr(admission_module, "IndependentObservationAdmissionService", _Service)
    handlers = build_independent_admission_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )
    payload = AdmissionDecisionIssuanceRPCPayload(
        committed_validation=committed,
        issuance_challenge=expected.message.issuance_challenge,
    )
    assert handlers.operations == (ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,)
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION)(payload)
        == expected
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
    assert (
        RPCIndependentObservationAdmission(client, binding).issue_admission_decision(
            committed_validation=committed,
            issuance_challenge=expected.message.issuance_challenge,
        )
        == expected
    )
    expected_call = {
        "committed_validation": committed,
        "issuance_challenge": expected.message.issuance_challenge,
    }
    assert calls == [expected_call, expected_call]
    with pytest.raises(TypeError, match="another payload"):
        handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION)(object())
    assert config["admission_signing_key_loaded"] is True
    assert config["database_signing_key_loaded"] is False
    assert config["execution_signing_key_loaded"] is False
    assert config["validator_signing_key_loaded"] is False
    assert config["kernel_signing_key_loaded"] is False
    assert config["scientific_slot_commit_allowed"] is False
    assert config["direct_kernel_mutation_allowed"] is False


def test_guarded_runtime_loads_independent_admission_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _binding = _fixture(monkeypatch, tmp_path)

    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_independent_admission_factory_rejects_duplicate_config_and_key_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _binding = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_independent_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    key_path = Path(config["admission_signing_key"]["path"])
    key_path.chmod(0o600)
    with pytest.raises(ValueError, match="unsafe file custody"):
        build_independent_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_independent_admission_factory_rejects_archive_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _binding = _fixture(monkeypatch, tmp_path)
    archive = Path(config["validation_archive"]["root"])
    moved = archive.with_name("independent-admission-archive-old")
    archive.chmod(0o700)
    archive.rename(moved)
    moved.chmod(0o500)
    archive.mkdir(mode=0o500)
    archive.chmod(0o500)

    with pytest.raises(ValueError, match="differs from its custody pin"):
        build_independent_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_independent_admission_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _binding = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-admission-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
