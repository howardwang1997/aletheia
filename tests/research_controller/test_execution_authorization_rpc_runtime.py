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

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.authority_contracts import (
    AuthorityRegistryFilesystemPin,
    PricingAuthorityPin,
    SourceBudgetAuthorityPin,
    authority_key_id,
)
from aletheia.execution.qualification_custody import (
    QualificationPreAdmissionCustodyConfig,
)
from aletheia.execution.runtime_contracts import (
    TerminalVerificationAuthorityPin,
    qualification_key_id,
)
from aletheia.research_controller.execution_authorization_service import (
    FrozenScientificExecutionAuthorizationCatalog,
)
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
from aletheia.research_controller_execution_authorization_runtime import (
    build_execution_authorization_rpc_service,
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

_TEST_ROOT = Path(__file__).resolve().parent
_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
for _path in (_TEST_ROOT, _OBSERVATION_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_execution_authorization_service import _case as _service_case  # noqa: E402
from test_scientific_bridge import EXECUTION_AUTHORITY_PRIVATE_KEY  # noqa: E402


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


def _external_pin(
    model,
    *,
    label: str,
    principal: str,
    observed_at,
    policy_sha256: str | None = None,
):
    public_key = _public_key(label)
    return model(
        policy_sha256=policy_sha256 or _sha(f"{label}:policy"),
        principal_id=principal,
        key_id=authority_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )


def _empty_registry(root: Path) -> AuthorityRegistryFilesystemPin:
    for namespace in (
        "rate_cards",
        "execution_cost_quotes",
        "source_budgets",
        "source_budget_projections",
    ):
        sha_root = root / namespace / "sha256"
        sha_root.mkdir(parents=True, mode=0o555)
        (root / namespace).chmod(0o555)
        sha_root.chmod(0o555)
    root.chmod(0o555)
    metadata = root.stat()
    return AuthorityRegistryFilesystemPin(
        registry_id="registry:execution-authorization-test",
        owner_uid=metadata.st_uid,
        device_id=metadata.st_dev,
        directory_mode=stat.S_IMODE(metadata.st_mode),
        file_mode=0o444,
    )


def _kernel_reader(root: Path, observed_at) -> ResearchKernelReadOnlyConfig:
    root.mkdir(mode=0o550)
    root.chmod(0o550)
    metadata = root.stat()
    public_key = _public_key("execution-authorization-kernel-root")
    return ResearchKernelReadOnlyConfig(
        trust_root=ResearchAuthorizationTrustRootV1(
            trust_root_id=f"rat_{_sha('execution-authorization-kernel-root')[:32]}",
            frozen_at=observed_at,
            commissioning_keys=(
                ResearchAuthorizationTrustKey(
                    key_id=ed25519_key_id(public_key),
                    principal_id="principal.kernel.root",
                    public_key_ed25519_hex=public_key,
                    valid_from=observed_at - timedelta(days=1),
                    expires_at=observed_at + timedelta(days=1),
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
    bridge, _source, _service, _wakeup, _projection, _plan, base_catalog, _binding = _service_case()
    observed_at = bridge.authorization.message.authorized_at + timedelta(seconds=30)
    repository_root = Path(__file__).resolve().parents[2]
    factory = (
        repository_root / "aletheia/research_controller_execution_authorization_runtime.py"
    ).resolve()
    implementation = (
        repository_root / "aletheia/research_controller/execution_authorization_service.py"
    ).resolve()
    implementation_sha256 = hashlib.sha256(implementation.read_bytes()).hexdigest()
    catalog = FrozenScientificExecutionAuthorizationCatalog.model_validate(
        {
            **base_catalog.model_dump(mode="python"),
            "issuer_implementation_sha256": implementation_sha256,
        }
    )
    binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        principal_id=bridge.execution_pin.principal_id,
        key_id=bridge.execution_pin.key_id,
        policy_sha256=bridge.execution_pin.policy_sha256,
        service_manifest_sha256=_sha("execution-authorization-service-manifest"),
        externally_deployed=True,
    )

    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"execution-authorization-rpc-receipt").digest()
    )
    receipt_public_key = receipt_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    socket_root = (tmp_path / "socket").resolve()
    config_root = (tmp_path / "config").resolve()
    receipt_secret_root = (tmp_path / "receipt-secret").resolve()
    domain_secret_root = (tmp_path / "domain-secret").resolve()
    artifact_root = (tmp_path / "artifact-store").resolve()
    registry_root = (tmp_path / "authority-registry").resolve()
    for path, mode in (
        (socket_root, 0o750),
        (config_root, 0o700),
        (receipt_secret_root, 0o700),
        (domain_secret_root, 0o700),
    ):
        path.mkdir(mode=mode)
        path.chmod(mode)
    LocalArtifactStore(
        artifact_root,
        verifier_principal_id="principal:sea-artifact-verifier",
        object_store_id="store:sea-artifacts",
        max_object_bytes=1024**3,
    )
    filesystem_pin = _empty_registry(registry_root)
    terminal_public_key = _public_key("execution-authorization-terminal-verifier")
    terminal_pin = TerminalVerificationAuthorityPin(
        policy_sha256=_sha("execution-authorization-terminal-policy"),
        principal_id="principal:sea-terminal-verifier",
        key_id=qualification_key_id(terminal_public_key),
        public_key_ed25519_hex=terminal_public_key,
        valid_from=observed_at - timedelta(days=1),
        expires_at=observed_at + timedelta(days=1),
    )
    custody = QualificationPreAdmissionCustodyConfig(
        artifact_store_root=str(artifact_root),
        artifact_verifier_principal_id="principal:sea-artifact-verifier",
        artifact_object_store_id="store:sea-artifacts",
        artifact_max_object_bytes=1024**3,
        authority_registry_root=str(registry_root),
        authority_registry_filesystem_pin=filesystem_pin,
        pricing_authority_pin=_external_pin(
            PricingAuthorityPin,
            label="execution-authorization-pricing",
            principal=(
                bridge.authorization.message.qualification_bundle.cost_quote.quoted_by_principal_id
            ),
            observed_at=observed_at,
            policy_sha256=(
                bridge.authorization.message.qualification_bundle.cost_quote.pricing_policy_sha256
            ),
        ),
        source_budget_authority_pin=_external_pin(
            SourceBudgetAuthorityPin,
            label="execution-authorization-budget",
            principal=(
                bridge.authorization.message.qualification_bundle.budget_authorization.authorized_by_principal_id
            ),
            observed_at=observed_at,
        ),
        qualification_authority_pin=bridge.qualification.pin,
        terminal_verification_authority_pin=terminal_pin,
        input_resolver_principal_id="principal:sea-input-resolver",
        prepared_at=observed_at,
    )

    process_uid = os.geteuid()
    process_gid = os.getegid()
    pin = ControllerWorkerRPCServicePin(
        service_principal_id=binding.principal_id,
        service_manifest_sha256=binding.service_manifest_sha256,
        service_policy_sha256=binding.policy_sha256,
        operations=(ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(socket_root / "execution-authorization.sock"),
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(receipt_public_key.hex()),
        receipt_public_key_ed25519_hex=receipt_public_key.hex(),
        valid_from=observed_at - timedelta(minutes=1),
        expires_at=observed_at + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=8 * 1024**2,
        max_response_bytes=8 * 1024**2,
    )
    domain_key_path = (domain_secret_root / "execution-authorization.key").resolve()
    domain_key_path.write_bytes(EXECUTION_AUTHORITY_PRIVATE_KEY)
    domain_key_path.chmod(0o400)
    config = {
        "schema_name": "aletheia.execution_authorization_rpc_service_config",
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
        "kernel_reader": _kernel_reader(
            (tmp_path / "kernel-cas").resolve(), observed_at
        ).model_dump(mode="json"),
        "authority_binding": binding.model_dump(mode="json"),
        "qualification_custody": custody.model_dump(mode="json"),
        "authorization_catalog": catalog.model_dump(mode="json"),
        "issuer_implementation_source_path": str(implementation),
        "issuer_implementation_source_sha256": implementation_sha256,
        "execution_signing_key": {
            "path": str(domain_key_path),
            "file_sha256": hashlib.sha256(domain_key_path.read_bytes()).hexdigest(),
            "key_id": bridge.execution_pin.key_id,
            "owner_uid": process_uid,
            "owner_gid": process_gid,
            "file_mode": 0o400,
        },
        "prepared_at": observed_at.isoformat().replace("+00:00", "Z"),
        "direct_kernel_mutation_allowed": False,
        "execution_launch_allowed": False,
        "qualification_admission_allowed": False,
        "direct_observation_admission_allowed": False,
        "validator_signing_key_loaded": False,
        "admission_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "dynamic_template_mutation_allowed": False,
    }
    config_path = (config_root / "execution-authorization.json").resolve()
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
        composition_factory_module=("aletheia.research_controller_execution_authorization_runtime"),
        composition_factory_attribute="build_execution_authorization_rpc_service",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=observed_at,
    )
    return deployment, config, config_path, domain_key_path


def test_checked_in_execution_authorization_factory_is_operation_closed(tmp_path: Path) -> None:
    deployment, _config, config_path, _domain_key_path = _fixture(tmp_path)
    handlers = build_execution_authorization_rpc_service(
        deployment=deployment,
        configuration_bytes=config_path.read_bytes(),
    )

    assert handlers.operations == (ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,)
    handler = handlers.handler_for(ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION)
    with pytest.raises(TypeError, match="another payload"):
        handler(object())


def test_guarded_rpc_runtime_loads_execution_authorization_factory(tmp_path: Path) -> None:
    deployment, _config, _config_path, _domain_key_path = _fixture(tmp_path)
    runtime = build_controller_worker_rpc_server_runtime(
        deployment,
        clock=lambda: deployment.prepared_at,
    )

    assert runtime.deployment == deployment
    assert not Path(deployment.service_pin.socket_path).exists()


def test_execution_authorization_factory_rejects_duplicate_rebound_or_unsafe_key(
    tmp_path: Path,
) -> None:
    deployment, config, config_path, domain_key_path = _fixture(tmp_path)
    duplicate = config_path.read_bytes().replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )
    with pytest.raises(ValueError, match="config is invalid"):
        build_execution_authorization_rpc_service(
            deployment=deployment,
            configuration_bytes=duplicate,
        )

    config["execution_signing_key"]["key_id"] = _sha("rebound-key")
    with pytest.raises(ValueError, match="config is invalid"):
        build_execution_authorization_rpc_service(
            deployment=deployment,
            configuration_bytes=canonical_json_bytes(config),
        )

    domain_key_path.chmod(0o440)
    with pytest.raises(ControllerWorkerRPCProcessError, match="factory failed"):
        build_controller_worker_rpc_server_runtime(deployment)


def test_execution_authorization_runtime_rejects_factory_source_drift(tmp_path: Path) -> None:
    deployment, _config, _config_path, _domain_key_path = _fixture(tmp_path)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha("drifted-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
