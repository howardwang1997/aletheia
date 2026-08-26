from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.research_controller.continuation import (
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
)
from aletheia.research_controller.continuation_assessor import (
    EXACT_OUTCOME_BIN_FIT_RULE_SHA256,
)
from aletheia.research_controller.continuation_step import ContinuationAssessmentPolicyPin
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import ControllerTickRPCPayload
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
from aletheia.research_controller_continuation_runtime import (
    build_continuation_assessment_rpc_service,
)
from aletheia.research_controller_rpc_runtime import (
    ControllerWorkerRPCProcessError,
    ControllerWorkerRPCServerDeployment,
    build_controller_worker_rpc_server_runtime,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationTrustKey,
    ResearchAuthorizationTrustRootV1,
    ed25519_key_id,
)
from aletheia.research_kernel.schemas import canonical_json_bytes

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS / "research_controller"))
from test_external_rpc_server import _tick_payload  # noqa: E402

NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _public_key(label: str) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _kernel_reader(root: Path) -> ResearchKernelReadOnlyConfig:
    root.mkdir(mode=0o550)
    root.chmod(0o550)
    metadata = root.stat()
    public_key = _public_key("continuation-kernel-root")
    return ResearchKernelReadOnlyConfig(
        trust_root=ResearchAuthorizationTrustRootV1(
            trust_root_id=f"rat_{_sha('continuation-kernel-root')[:32]}",
            frozen_at=NOW,
            commissioning_keys=(
                ResearchAuthorizationTrustKey(
                    key_id=ed25519_key_id(public_key),
                    principal_id="principal.kernel.root",
                    public_key_ed25519_hex=public_key,
                    valid_from=NOW - timedelta(days=1),
                    expires_at=NOW + timedelta(days=1),
                ),
            ),
        ),
        cas_root=str(root.resolve()),
        cas_owner_uid=metadata.st_uid,
        cas_group_gid=metadata.st_gid,
        cas_device_id=metadata.st_dev,
        cas_inode=metadata.st_ino,
        cas_directory_mode=stat.S_IMODE(metadata.st_mode),
        max_object_bytes=1024**2,
    )


def _fixture(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    factory = (repository_root / "aletheia/research_controller_continuation_runtime.py").resolve()
    implementation = (
        repository_root / "aletheia/research_controller/continuation_assessor.py"
    ).resolve()
    implementation_sha256 = hashlib.sha256(implementation.read_bytes()).hexdigest()
    policy = ContinuationAssessmentPolicyPin(
        assessment_implementation_sha256=implementation_sha256,
        observed_outcome_identity_policy_sha256=OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
        allowed_assessor_principal_ids=("service:continuation-assessor",),
        allowed_fit_rule_sha256s=(EXACT_OUTCOME_BIN_FIT_RULE_SHA256,),
    )
    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,
        principal_id=policy.allowed_assessor_principal_ids[0],
        key_id=None,
        policy_sha256=policy.policy_sha256,
        service_manifest_sha256=_sha("continuation-service-manifest"),
        externally_deployed=True,
    )
    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"continuation-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    socket_root = (tmp_path / "socket").resolve()
    artifact_root = (tmp_path / "artifacts").resolve()
    config_root = (tmp_path / "config").resolve()
    secret_root = (tmp_path / "secrets").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (artifact_root, 0o700),
        (config_root, 0o700),
        (secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)
    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id=binding.principal_id,
        service_manifest_sha256=binding.service_manifest_sha256,
        service_policy_sha256=binding.policy_sha256,
        operations=(ControllerWorkerRPCOperation.DERIVE_CONTINUATION,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "continuation.sock"),
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(receipt_public_key.hex()),
        receipt_public_key_ed25519_hex=receipt_public_key.hex(),
        valid_from=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=1024**2,
        max_response_bytes=1024**2,
    )
    artifact_metadata = artifact_root.stat()
    config = {
        "schema_name": "aletheia.continuation_assessment_rpc_service_config",
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
        "kernel_reader": _kernel_reader((tmp_path / "kernel-cas").resolve()).model_dump(
            mode="json"
        ),
        "authority_binding": binding.model_dump(mode="json"),
        "assessment_policy": policy.model_dump(mode="json"),
        "assessment_implementation_source_path": str(implementation),
        "assessment_implementation_source_sha256": implementation_sha256,
        "artifact_root": {
            "path": str(artifact_root),
            "owner_uid": artifact_metadata.st_uid,
            "owner_gid": artifact_metadata.st_gid,
            "device_id": artifact_metadata.st_dev,
            "inode": artifact_metadata.st_ino,
            "directory_mode": 0o700,
        },
        "prepared_at": "2026-08-26T08:00:00Z",
        "direct_scientific_authority": False,
        "kernel_signing_key_loaded": False,
        "observation_signing_key_loaded": False,
        "execution_access_allowed": False,
        "generic_model_callback_allowed": False,
    }
    config_path = (config_root / "continuation.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    key_path = (secret_root / "receipt.key").resolve()
    key_path.write_bytes(receipt_private_key.private_bytes_raw())
    key_path.chmod(0o400)
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
        receipt_private_key_path=str(key_path),
        receipt_private_key_sha256=hashlib.sha256(key_path.read_bytes()).hexdigest(),
        reviewed_code_root=str(repository_root),
        composition_factory_module="aletheia.research_controller_continuation_runtime",
        composition_factory_attribute="build_continuation_assessment_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=NOW,
    )
    return deployment, config, config_path, artifact_root


def test_checked_in_continuation_factory_is_operation_closed(tmp_path: Path) -> None:
    deployment, _config, config_path, _artifact_root = _fixture(tmp_path)
    handlers = build_continuation_assessment_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == (ControllerWorkerRPCOperation.DERIVE_CONTINUATION,)
    handler = handlers.handler_for(ControllerWorkerRPCOperation.DERIVE_CONTINUATION)
    with pytest.raises(TypeError, match="another payload"):
        handler(object())
    assert type(_tick_payload()) is ControllerTickRPCPayload


def test_guarded_rpc_runtime_loads_exact_continuation_factory(tmp_path: Path) -> None:
    deployment, _config, _config_path, _artifact_root = _fixture(tmp_path)
    runtime = build_controller_worker_rpc_server_runtime(deployment, clock=lambda: NOW)

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_continuation_factory_rejects_duplicate_or_rebound_config(tmp_path: Path) -> None:
    deployment, config, config_path, artifact_root = _fixture(tmp_path)
    encoded = config_path.read_bytes()
    duplicate = encoded.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
    with pytest.raises(ValueError, match="config is invalid"):
        build_continuation_assessment_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["assessment_implementation_source_sha256"] = _sha("rebound-source")
    config["assessment_policy"]["assessment_implementation_sha256"] = _sha("rebound-source")
    with pytest.raises(ValueError, match="config is invalid"):
        build_continuation_assessment_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    artifact_root.chmod(0o750)
    with pytest.raises(ControllerWorkerRPCProcessError, match="factory failed"):
        build_controller_worker_rpc_server_runtime(deployment)


def test_runtime_config_source_drift_is_rejected_before_service_start(tmp_path: Path) -> None:
    deployment, _config, _config_path, _artifact_root = _fixture(tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
