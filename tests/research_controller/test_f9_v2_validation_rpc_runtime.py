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
from aletheia.observations import f9_v2_validation as validation_module
from aletheia.observations.adapters import (
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.f9_v2_assessor import (
    ExactContentF9V2ObservationAssessor,
    FrozenF9V2ExactContentAssessmentCatalog,
    FrozenF9V2ExactContentAssessmentTemplate,
)
from aletheia.observations.f9_v2_validation import (
    WriteOnceF9V2ValidationCampaignArchive,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    VerifiedExecutionAuthorityProjection,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    RPCIndependentObservationValidator,
    ValidationCampaignResult,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    ControllerWorkerRPCService,
    RawRunRPCPayload,
    ValidationReceiptIssuanceRPCPayload,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_f9_v2_validation_runtime import (
    build_f9_v2_validation_rpc_service,
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

from test_database_observation_rpc_runtime import _fixture as _database_fixture  # noqa: E402
from test_f9_v2_validation import _f9_case  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    VALIDATOR_PRIVATE_KEY,
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
    base_root = tmp_path / "database-base"
    base_root.mkdir()
    base_deployment, base, _base_path, base_case = _database_fixture(
        monkeypatch,
        base_root,
    )
    case = _f9_case(monkeypatch)
    assert (
        case.execution_pin,
        case.validator_pin,
        case.admission_pin,
        case.database_pin,
        case.qualification.pin,
    ) == (
        base_case.execution_pin,
        base_case.validator_pin,
        base_case.admission_pin,
        base_case.database_pin,
        base_case.qualification.pin,
    )
    prepared_at = base_deployment.prepared_at
    assert base["prepared_at"] == prepared_at.isoformat().replace("+00:00", "Z")
    raw_run = _raw_run(case)
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_f9_v2_validation_runtime.py"
    ).resolve()
    service_implementation = (
        repository_root / "aletheia/observations/f9_v2_validation.py"
    ).resolve()
    assessor_implementation = (
        repository_root / "aletheia/observations/f9_v2_assessor.py"
    ).resolve()
    assessor_sha256 = hashlib.sha256(assessor_implementation.read_bytes()).hexdigest()
    template = FrozenF9V2ExactContentAssessmentTemplate.from_raw_run(
        raw_run=raw_run,
        disposition=BridgeValidationDisposition.VALIDATED_CONFIRMATION,
        outcome_bin_id="outcome.negative",
    )
    catalog = FrozenF9V2ExactContentAssessmentCatalog(
        catalog_id="catalog:f9-v2:runtime-test",
        assessor_implementation_sha256=assessor_sha256,
        templates=(template,),
    )

    socket_root = (tmp_path / "f9-v2-validation-socket").resolve()
    config_root = (tmp_path / "f9-v2-validation-config").resolve()
    receipt_secret_root = (tmp_path / "f9-v2-validation-receipt-secret").resolve()
    validator_secret_root = (tmp_path / "f9-v2-validator-secret").resolve()
    validation_archive_root = (tmp_path / "f9-v2-validation-write-archive").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
        (validator_secret_root, 0o700),
        (validation_archive_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"f9-v2-validation-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        principal_id=case.validator_pin.principal_id,
        key_id=case.validator_pin.key_id,
        policy_sha256=case.validator_pin.policy_sha256,
        service_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        externally_deployed=True,
    )
    operations = tuple(
        sorted(
            (
                ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
                ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
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
        socket_path=str(socket_root / "f9-v2-validation.sock"),
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
    validator_key_path = (validator_secret_root / "observation-validator.key").resolve()
    validator_key_path.write_bytes(VALIDATOR_PRIVATE_KEY)
    validator_key_path.chmod(0o400)
    archive_metadata = validation_archive_root.stat()
    reader = base["qualification_reader"]
    artifact_authority = VerifiedExecutionAuthorityProjection(
        principal_id=reader["artifact_verifier_principal_id"],
        key_id=_sha("f9-v2-artifact-verification-key"),
        policy_sha256=_sha("f9-v2-artifact-verification-policy"),
    )
    config = {
        "schema_name": "aletheia.independent_f9_v2_validation_rpc_service_config",
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
        "kernel_reader": base["kernel_reader"],
        "authority_binding": binding.model_dump(mode="json"),
        "validator_authority_pin": case.validator_pin.model_dump(mode="json"),
        "validator_signing_key": {
            "path": str(validator_key_path),
            "file_sha256": hashlib.sha256(VALIDATOR_PRIVATE_KEY).hexdigest(),
            "key_id": case.validator_pin.key_id,
            "owner_uid": process_uid,
            "owner_gid": process_gid,
            "file_mode": 0o400,
        },
        "execution_authority_pin": case.execution_pin.model_dump(mode="json"),
        "admission_authority_pin": case.admission_pin.model_dump(mode="json"),
        "database_authority_pin": case.database_pin.model_dump(mode="json"),
        "qualification_reader": reader,
        "artifact_verification_authority": artifact_authority.model_dump(mode="json"),
        "validation_archive": {
            "root": str(validation_archive_root),
            "owner_uid": archive_metadata.st_uid,
            "group_gid": archive_metadata.st_gid,
            "device_id": archive_metadata.st_dev,
            "inode": archive_metadata.st_ino,
            "directory_mode": stat.S_IMODE(archive_metadata.st_mode),
            "validator_manifest_sha256": binding.service_manifest_sha256,
            "read_only": False,
            "campaign_publication_allowed": True,
        },
        "assessment_catalog": catalog.model_dump(mode="json"),
        "assessor_implementation_source_path": str(assessor_implementation),
        "assessor_implementation_source_sha256": assessor_sha256,
        "service_implementation_source_path": str(service_implementation),
        "service_implementation_source_sha256": hashlib.sha256(
            service_implementation.read_bytes()
        ).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "validator_signing_key_loaded": True,
        "database_signing_key_loaded": False,
        "admission_signing_key_loaded": False,
        "execution_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "database_mutation_allowed": False,
        "execution_mutation_allowed": False,
        "artifact_mutation_allowed": False,
        "campaign_publication_allowed": True,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
        "generic_model_callback_allowed": False,
    }
    config_path = (config_root / "f9-v2-validation.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_f9_v2_validation_runtime"),
        composition_factory_attribute="build_f9_v2_validation_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, case, raw_run


def test_checked_in_f9_v2_factory_is_key_isolated_and_operation_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case, raw_run = _fixture(monkeypatch, tmp_path)
    expected_receipt = _validated_receipt(case)
    expected_campaign = (
        expected_receipt.message.issuance_challenge.message.validation_campaign_sha256
    )
    assert expected_campaign is not None
    calls = []

    class _Service:
        def __init__(
            self,
            *,
            archive,
            assessor,
            verification,
            validator_private_key,
            clock,
        ) -> None:
            assert isinstance(archive, WriteOnceF9V2ValidationCampaignArchive)
            assert archive.read_only is False
            assert isinstance(assessor, ExactContentF9V2ObservationAssessor)
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
            assert validator_private_key == VALIDATOR_PRIVATE_KEY
            assert callable(clock)

        def prepare_validation_campaign(self, **kwargs):
            calls.append(("campaign", kwargs))
            return expected_campaign

        def issue_validation_receipt(self, **kwargs):
            calls.append(("receipt", kwargs))
            return expected_receipt

    monkeypatch.setattr(validation_module, "F9V2IndependentValidationService", _Service)
    handlers = build_f9_v2_validation_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == tuple(
        sorted(
            (
                ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
                ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
            ),
            key=lambda item: item.value,
        )
    )
    assert handlers.handler_for(ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN)(
        RawRunRPCPayload(raw_run=raw_run)
    ) == ValidationCampaignResult(validation_campaign_sha256=expected_campaign)
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT)(
            ValidationReceiptIssuanceRPCPayload(
                raw_run=raw_run,
                validation_campaign_sha256=expected_campaign,
                issuance_challenge=expected_receipt.message.issuance_challenge,
            )
        )
        == expected_receipt
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
    validator = RPCIndependentObservationValidator(
        client,
        ControllerStepAuthorityBinding.model_validate(config["authority_binding"]),
    )
    assert validator.prepare_validation_campaign(raw_run=raw_run) == expected_campaign
    assert (
        validator.issue_validation_receipt(
            raw_run=raw_run,
            validation_campaign_sha256=expected_campaign,
            issuance_challenge=expected_receipt.message.issuance_challenge,
        )
        == expected_receipt
    )
    assert [item[0] for item in calls] == ["campaign", "receipt", "campaign", "receipt"]
    with pytest.raises(TypeError, match="another payload"):
        handlers.handler_for(ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN)(object())
    assert config["validator_signing_key_loaded"] is True
    assert config["database_signing_key_loaded"] is False
    assert config["admission_signing_key_loaded"] is False
    assert config["execution_signing_key_loaded"] is False
    assert config["kernel_signing_key_loaded"] is False
    assert config["database_mutation_allowed"] is False
    assert config["execution_mutation_allowed"] is False
    assert config["artifact_mutation_allowed"] is False
    assert config["campaign_publication_allowed"] is True
    assert config["direct_observation_admission_allowed"] is False
    assert config["direct_kernel_mutation_allowed"] is False
    assert config["generic_model_callback_allowed"] is False


def test_guarded_rpc_runtime_loads_f9_v2_validation_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _raw = _fixture(monkeypatch, tmp_path)

    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_f9_v2_factory_rejects_duplicate_key_reuse_and_source_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _raw = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_f9_v2_validation_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["worker_process_principal_id"] = config["validator_authority_pin"]["principal_id"]
    with pytest.raises(ValueError, match="config is invalid"):
        build_f9_v2_validation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    config["worker_process_principal_id"] = deployment.worker_process_principal_id
    receipt_key = Path(deployment.receipt_private_key_path)
    config["validator_signing_key"]["path"] = str(receipt_key)
    config["validator_signing_key"]["file_sha256"] = deployment.receipt_private_key_sha256
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_f9_v2_validation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    validator_key = config_path.parent.parent / "f9-v2-validator-secret/observation-validator.key"
    config["validator_signing_key"]["path"] = str(validator_key.resolve())
    config["validator_signing_key"]["file_sha256"] = hashlib.sha256(
        validator_key.read_bytes()
    ).hexdigest()
    config["assessor_implementation_source_sha256"] = _sha("assessor-source-drift")
    config["assessment_catalog"]["assessor_implementation_sha256"] = _sha("assessor-source-drift")
    with pytest.raises(ValueError, match="byte pin"):
        build_f9_v2_validation_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )


def test_f9_v2_factory_rejects_campaign_archive_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, config_path, _case, _raw = _fixture(monkeypatch, tmp_path)
    config = _config
    archive = Path(config["validation_archive"]["root"])
    moved = archive.with_name("f9-v2-validation-write-archive-old")
    archive.rename(moved)
    archive.mkdir(mode=0o700)
    archive.chmod(0o700)

    with pytest.raises(ValueError, match="differs from its custody pin"):
        build_f9_v2_validation_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_f9_v2_validation_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _raw = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-f9-v2-validation-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
