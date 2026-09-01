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
from aletheia.observations import coordinator as coordinator_module
from aletheia.observations.adapters import (
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.coordinator import ObservationAdmissionVerificationContext
from aletheia.observations.f9_v2_validation import (
    WriteOnceF9V2ValidationCampaignArchive,
)
from aletheia.observations.kernel_authority import (
    ExactObservationKernelAuthority,
    ObservationKernelPolicyAssignment,
)
from aletheia.observations.scientific_bridge import ObservationAdmissionDisposition
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    RPCAtomicObservationAdmission,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    AdmissionCommitRPCPayload,
    ControllerWorkerRPCService,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_atomic_admission_runtime import (
    build_atomic_admission_rpc_service,
)
from aletheia.research_controller_rpc_runtime import (
    ControllerWorkerRPCProcessError,
    ControllerWorkerRPCServerDeployment,
    build_controller_worker_rpc_server_runtime,
)
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.schemas import canonical_json_bytes
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore

_TEST_ROOT = Path(__file__).resolve().parent
_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
_KERNEL_TESTS = Path(__file__).resolve().parents[1] / "research_kernel"
for _path in (_TEST_ROOT, _OBSERVATION_TESTS, _KERNEL_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_commands import (  # noqa: E402
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
    _role_key as command_role_key,
)
from test_independent_admission_rpc_runtime import (  # noqa: E402
    _DirectTransport,
    _fixture as _admission_fixture,
)
from test_observation_steps import _atomic_receipt  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    DATABASE_PRIVATE_KEY,
    _issue_admission_decision,
    _validated_receipt,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    source_root = tmp_path / "independent-admission-base"
    source_root.mkdir()
    _source_deployment, source, _source_path, case, admission_binding = _admission_fixture(
        monkeypatch,
        source_root,
    )
    prepared_at = case.authorization.message.authorized_at
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_atomic_admission_runtime.py"
    ).resolve()
    coordinator_source = (repository_root / "aletheia/observations/coordinator.py").resolve()
    authority_source = (repository_root / "aletheia/observations/kernel_authority.py").resolve()

    roots = {
        "socket": (tmp_path / "atomic-admission-socket").resolve(),
        "config": (tmp_path / "atomic-admission-config").resolve(),
        "receipt": (tmp_path / "atomic-admission-receipt-secret").resolve(),
        "database": (tmp_path / "atomic-admission-database-secret").resolve(),
        "kernel": (tmp_path / "atomic-admission-kernel-secret").resolve(),
        "cas": (tmp_path / "atomic-admission-kernel-cas").resolve(),
    }
    for label, path in roots.items():
        mode = 0o750 if label in {"socket", "cas"} else 0o700
        path.mkdir(mode=mode)
        path.chmod(mode)

    action_binding = case.authorization.message.action_protocol_binding
    trust_root, policy = command_authority(quest_id=action_binding.action.quest_id)
    kernel_key = command_role_key(policy, ResearchAuthorizationRole.ORDINARY)
    assignment = ObservationKernelPolicyAssignment(
        quest_id=action_binding.action.quest_id,
        scope_binding=action_binding.compilation_request.protocol.graph_scope.scope_binding,
        authorization_policy=policy,
    )
    database_binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        principal_id=case.database_pin.principal_id,
        key_id=case.database_pin.key_id,
        policy_sha256=case.database_pin.policy_sha256,
        service_manifest_sha256=_sha("database-observation-service-manifest"),
        externally_deployed=True,
    )
    kernel_binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.KERNEL_COMMAND,
        principal_id=kernel_key.principal_id,
        key_id=kernel_key.key_id,
        policy_sha256=policy.policy_sha256,
        service_manifest_sha256=_sha("observation-kernel-authority-service-manifest"),
        externally_deployed=True,
    )
    bindings = tuple(
        sorted(
            (database_binding, admission_binding, kernel_binding),
            key=lambda item: item.binding_sha256,
        )
    )
    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"atomic-admission-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes_raw()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id="principal.observation.atomic-admission-service",
        service_manifest_sha256=_sha("atomic-admission-service-manifest"),
        service_policy_sha256=_sha("atomic-admission-service-policy"),
        operations=(ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,),
        authority_binding_sha256s=tuple(item.binding_sha256 for item in bindings),
        socket_path=str(roots["socket"] / "atomic-admission.sock"),
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
    database_key_path = (roots["database"] / "database-attestation.key").resolve()
    database_key_path.write_bytes(DATABASE_PRIVATE_KEY)
    database_key_path.chmod(0o400)
    kernel_private_key = COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY]
    kernel_key_path = (roots["kernel"] / "kernel-command.key").resolve()
    kernel_key_path.write_bytes(kernel_private_key)
    kernel_key_path.chmod(0o400)
    cas_metadata = roots["cas"].stat()
    config = {
        "schema_name": "aletheia.atomic_admission_rpc_service_config",
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
        "kernel": {
            "trust_root": trust_root.model_dump(mode="json"),
            "cas_root": str(roots["cas"]),
            "cas_owner_uid": cas_metadata.st_uid,
            "cas_group_gid": cas_metadata.st_gid,
            "cas_device_id": cas_metadata.st_dev,
            "cas_inode": cas_metadata.st_ino,
            "cas_directory_mode": stat.S_IMODE(cas_metadata.st_mode),
            "max_object_bytes": source["kernel_reader"]["max_object_bytes"],
            "read_only": False,
            "snapshot_archive_write_allowed": True,
            "arbitrary_object_admission_allowed": False,
        },
        "kernel_policy_assignments": [assignment.model_dump(mode="json")],
        "authority_bindings": [item.model_dump(mode="json") for item in bindings],
        "database_authority_pin": case.database_pin.model_dump(mode="json"),
        "execution_authority_pin": case.execution_pin.model_dump(mode="json"),
        "validator_authority_pin": case.validator_pin.model_dump(mode="json"),
        "admission_authority_pin": case.admission_pin.model_dump(mode="json"),
        "qualification_reader": source["qualification_reader"],
        "artifact_verification_authority": source["artifact_verification_authority"],
        "validation_archive": source["validation_archive"],
        "database_signing_key": {
            "path": str(database_key_path),
            "file_sha256": hashlib.sha256(DATABASE_PRIVATE_KEY).hexdigest(),
            "key_id": case.database_pin.key_id,
            "owner_uid": process_uid,
            "group_gid": process_gid,
            "file_mode": 0o400,
        },
        "kernel_signing_key": {
            "path": str(kernel_key_path),
            "file_sha256": hashlib.sha256(kernel_private_key).hexdigest(),
            "key_id": kernel_key.key_id,
            "owner_uid": process_uid,
            "group_gid": process_gid,
            "file_mode": 0o400,
        },
        "coordinator_source_path": str(coordinator_source),
        "coordinator_source_sha256": hashlib.sha256(coordinator_source.read_bytes()).hexdigest(),
        "kernel_authority_source_path": str(authority_source),
        "kernel_authority_source_sha256": hashlib.sha256(authority_source.read_bytes()).hexdigest(),
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "database_signing_key_loaded": True,
        "kernel_signing_key_loaded": True,
        "admission_signing_key_loaded": False,
        "execution_signing_key_loaded": False,
        "validator_signing_key_loaded": False,
        "admission_row_and_kernel_commit_atomic": True,
        "independent_decision_required": True,
        "arbitrary_kernel_event_allowed": False,
        "campaign_publication_allowed": False,
        "execution_mutation_allowed": False,
    }
    config_path = (roots["config"] / "atomic-admission.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    receipt_key_path = (roots["receipt"] / "receipt.key").resolve()
    receipt_key_path.write_bytes(receipt_private_key.private_bytes_raw())
    receipt_key_path.chmod(0o400)
    socket_metadata = roots["socket"].stat()
    deployment = ControllerWorkerRPCServerDeployment(
        service_pin=pin,
        controller_id=config["controller_id"],
        controller_manifest_sha256=config["controller_manifest_sha256"],
        worker_process_principal_id=config["worker_process_principal_id"],
        worker_peer_uid=process_uid + 1,
        worker_peer_gid=process_gid,
        process_uid=process_uid,
        process_gid=process_gid,
        socket_parent_path=str(roots["socket"]),
        socket_parent_owner_uid=socket_metadata.st_uid,
        socket_parent_owner_gid=socket_metadata.st_gid,
        socket_parent_mode=stat.S_IMODE(socket_metadata.st_mode),
        socket_parent_device_id=socket_metadata.st_dev,
        socket_parent_inode=socket_metadata.st_ino,
        receipt_private_key_path=str(receipt_key_path),
        receipt_private_key_sha256=hashlib.sha256(receipt_key_path.read_bytes()).hexdigest(),
        reviewed_code_root=str(repository_root),
        composition_factory_module=("aletheia.research_controller_atomic_admission_runtime"),
        composition_factory_attribute="build_atomic_admission_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return deployment, config, config_path, case, bindings


def test_checked_in_atomic_admission_factory_owns_exact_database_and_kernel_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, case, bindings = _fixture(monkeypatch, tmp_path)
    decision, _committed_validation = _issue_admission_decision(
        case,
        receipt=_validated_receipt(case),
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    by_role = {item.role: item for item in bindings}
    expected = _atomic_receipt(
        case,
        decision,
        kernel_binding=by_role[ControllerStepAuthorityRole.KERNEL_COMMAND],
    )
    calls = []

    class _Coordinator:
        def __init__(
            self,
            *,
            kernel_store,
            kernel_authority,
            verification,
            controller_principal_id,
        ) -> None:
            assert isinstance(kernel_store, ResearchKernelStore)
            assert isinstance(kernel_store._archive, FilesystemResearchArchive)
            assert kernel_store._archive.read_only is False
            snapshot = b'{"shared_custody_probe":true}'
            snapshot_sha256 = hashlib.sha256(snapshot).hexdigest()
            archived = kernel_store._archive.archive_snapshot(
                quest_id=case.authorization.message.action_protocol_binding.action.quest_id,
                stream_version=1,
                snapshot_sha256=snapshot_sha256,
                payload=snapshot,
            )
            target = kernel_store._archive.root / archived.storage_key
            assert stat.S_IMODE((kernel_store._archive.root / "sha256").stat().st_mode) == 0o750
            assert stat.S_IMODE(target.parent.stat().st_mode) == 0o750
            assert stat.S_IMODE(target.stat().st_mode) == 0o440
            assert isinstance(kernel_authority, ExactObservationKernelAuthority)
            assert isinstance(verification, ObservationAdmissionVerificationContext)
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
            assert isinstance(
                verification.validation_campaign_custody,
                WriteOnceF9V2ValidationCampaignArchive,
            )
            assert verification.validation_campaign_custody.read_only is True
            assert controller_principal_id == deployment.worker_process_principal_id

        def commit_and_incorporate(self, observed_decision):
            calls.append(observed_decision)
            return expected

    monkeypatch.setattr(
        coordinator_module,
        "PostgreSQLAtomicObservationAdmissionCoordinator",
        _Coordinator,
    )
    handlers = build_atomic_admission_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )
    payload = AdmissionCommitRPCPayload(decision=decision)
    assert handlers.operations == (ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,)
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE)(payload)
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
        RPCAtomicObservationAdmission(
            client,
            database_binding=by_role[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
            kernel_binding=by_role[ControllerStepAuthorityRole.KERNEL_COMMAND],
            admission_binding=by_role[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION],
        ).commit_and_incorporate(decision)
        == expected
    )
    assert calls == [decision, decision]
    with pytest.raises(TypeError, match="another payload"):
        handlers.handler_for(ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE)(object())
    assert config["database_signing_key_loaded"] is True
    assert config["kernel_signing_key_loaded"] is True
    assert config["admission_signing_key_loaded"] is False
    assert config["admission_row_and_kernel_commit_atomic"] is True
    assert config["arbitrary_kernel_event_allowed"] is False


def test_guarded_runtime_loads_atomic_admission_factory(
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


def test_atomic_admission_factory_rejects_duplicate_config_and_key_rebind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_atomic_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    rebound = {
        **config,
        "authority_bindings": [dict(item) for item in config["authority_bindings"]],
    }
    kernel_index = next(
        index
        for index, item in enumerate(rebound["authority_bindings"])
        if item["role"] == ControllerStepAuthorityRole.KERNEL_COMMAND.value
    )
    rebound["authority_bindings"][kernel_index]["policy_sha256"] = _sha("rebound-kernel-policy")
    with pytest.raises(ValueError, match="config is invalid"):
        build_atomic_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(rebound),
        )

    key_path = Path(config["kernel_signing_key"]["path"])
    key_path.chmod(0o600)
    with pytest.raises(ValueError, match="unsafe file custody"):
        build_atomic_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_atomic_admission_factory_rejects_writable_cas_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, config, config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)
    archive = Path(config["kernel"]["cas_root"])
    moved = archive.with_name("atomic-admission-kernel-cas-old")
    archive.rename(moved)
    archive.mkdir(mode=0o700)
    archive.chmod(0o700)

    with pytest.raises(ValueError, match="differs from its custody pin"):
        build_atomic_admission_rpc_service(
            deployment=deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_atomic_admission_runtime_rejects_factory_source_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment, _config, _config_path, _case, _bindings = _fixture(monkeypatch, tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-atomic-admission-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
