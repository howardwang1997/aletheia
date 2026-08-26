from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision
from aletheia.execution.terminal_runtime import QualificationTerminalReaderConfig
from aletheia.research_controller.contracts import ControllerStep
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.service import ResearchControllerService
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAdapterSetManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    DedicatedControllerStepExecutor,
)
from aletheia.research_controller.worker_composition import (
    ControllerWorkerCompositionError,
    ControllerWorkerRPCServiceSet,
    ResearchKernelReadOnlyConfig,
    ResearchControllerWorkerRuntimeConfig,
    compose_research_controller_worker_service,
    controller_step_adapter_source_sha256,
    controller_step_rpc_configuration_sha256,
    load_research_controller_worker_runtime_config,
)
from aletheia.research_controller_runtime import (
    ResearchControllerRuntimeDeployment,
    ResearchControllerRuntimeRole,
    build_research_controller_runtime,
)
from aletheia.research_kernel.schemas import canonical_json_bytes
from aletheia.research_kernel.policy import (
    ResearchAuthorizationTrustKey,
    ResearchAuthorizationTrustRootV1,
    ed25519_key_id,
)

_CONTROLLER_FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(_CONTROLLER_FIXTURES))
from test_terminal_runtime import _config as _terminal_runtime_config  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _rpc_public_key(label: str) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _kernel_reader(tmp_path: Path, prepared_at) -> ResearchKernelReadOnlyConfig:
    cas_root = (tmp_path / "research-kernel-cas").resolve()
    cas_root.mkdir(mode=0o550)
    cas_root.chmod(0o550)
    metadata = cas_root.stat()
    public_key = _rpc_public_key("kernel-root")
    trust_key = ResearchAuthorizationTrustKey(
        key_id=ed25519_key_id(public_key),
        principal_id="principal.kernel.root",
        public_key_ed25519_hex=public_key,
        valid_from=prepared_at - timedelta(days=1),
        expires_at=prepared_at + timedelta(days=1),
    )
    return ResearchKernelReadOnlyConfig(
        trust_root=ResearchAuthorizationTrustRootV1(
            trust_root_id=f"rat_{_sha('worker-runtime-root')[:32]}",
            frozen_at=prepared_at,
            commissioning_keys=(trust_key,),
        ),
        cas_root=str(cas_root),
        cas_owner_uid=metadata.st_uid,
        cas_group_gid=metadata.st_gid,
        cas_device_id=metadata.st_dev,
        cas_inode=metadata.st_ino,
        cas_directory_mode=stat.S_IMODE(metadata.st_mode),
        max_object_bytes=1024**2,
    )


_STEP_ROLES = {
    ControllerStep.PROPOSE_ACTION: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.COMPILE_PROTOCOL: (ControllerStepAuthorityRole.PROTOCOL_COMPILATION,),
    ControllerStep.PROPOSE_REDESIGN: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.REGISTER_EXECUTION: (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    ControllerStep.COMMIT_VALIDATION: tuple(
        sorted(
            (
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            ),
            key=lambda item: item.value,
        )
    ),
    ControllerStep.COMMIT_ADMISSION: tuple(
        sorted(
            (
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                ControllerStepAuthorityRole.KERNEL_COMMAND,
            ),
            key=lambda item: item.value,
        )
    ),
    ControllerStep.DERIVE_CONTINUATION: (ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,),
    ControllerStep.PROPOSE_FOLLOWUP: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
}

_SIGNED_ROLES = frozenset(
    {
        ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    }
)

_SERVICE_OPERATIONS = {
    "action_proposal": (ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,),
    "protocol_compilation": (ControllerWorkerRPCOperation.COMPILE_PROTOCOL,),
    "execution_authorization": (ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,),
    "execution_registration": (ControllerWorkerRPCOperation.REGISTER_EXECUTION,),
    "raw_run_source": (ControllerWorkerRPCOperation.LOAD_RAW_RUN,),
    "database_observation": (
        ControllerWorkerRPCOperation.COMMIT_VALIDATION,
        ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
    ),
    "independent_validation": (
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
        ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
    ),
    "committed_validation_source": (ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION,),
    "independent_admission": (ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,),
    "atomic_admission": (ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,),
    "continuation_assessment": (ControllerWorkerRPCOperation.DERIVE_CONTINUATION,),
}

_SERVICE_ROLES = {
    "action_proposal": (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    "protocol_compilation": (ControllerStepAuthorityRole.PROTOCOL_COMPILATION,),
    "execution_authorization": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "execution_registration": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "raw_run_source": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "database_observation": (ControllerStepAuthorityRole.DATABASE_ATTESTATION,),
    "independent_validation": (ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,),
    "committed_validation_source": (
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    ),
    "independent_admission": (ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,),
    "atomic_admission": (
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    ),
    "continuation_assessment": (ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,),
}

_PRIMARY_ROLES = {
    "action_proposal": ControllerStepAuthorityRole.ACTION_PROPOSAL,
    "protocol_compilation": ControllerStepAuthorityRole.PROTOCOL_COMPILATION,
    "execution_authorization": ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
    "database_observation": ControllerStepAuthorityRole.DATABASE_ATTESTATION,
    "independent_validation": ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    "independent_admission": ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
    "continuation_assessment": ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,
}


class _KernelStore:
    def audit_in_session(self, _session, _quest_id):  # pragma: no cover - composition only
        raise AssertionError("composition must not audit before a worker tick")


class _TerminalReader:
    def load_verified_qualification_terminal_source(self, **_kwargs):  # pragma: no cover
        raise AssertionError("composition must not read terminal state")

    def load_qualification_terminal_outbox_in_session(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("composition must not read terminal state")


def _bindings() -> dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding]:
    return {
        role: ControllerStepAuthorityBinding(
            role=role,
            principal_id=f"principal.authority.{role.value}",
            key_id=f"key.authority.{role.value}" if role in _SIGNED_ROLES else None,
            policy_sha256=_sha(f"{role.value}:policy"),
            service_manifest_sha256=_sha(f"{role.value}:manifest"),
            externally_deployed=True,
        )
        for role in ControllerStepAuthorityRole
    }


def _service_set(
    *,
    tmp_path: Path,
    prepared_at,
    bindings: dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding],
) -> ControllerWorkerRPCServiceSet:
    pins: dict[str, ControllerWorkerRPCServicePin] = {}
    for name, operations in _SERVICE_OPERATIONS.items():
        primary_role = _PRIMARY_ROLES.get(name)
        public_key = _rpc_public_key(f"{name}:rpc-receipt")
        pins[name] = ControllerWorkerRPCServicePin(
            service_principal_id=(
                bindings[primary_role].principal_id
                if primary_role is not None
                else f"principal.rpc.{name}"
            ),
            service_manifest_sha256=(
                bindings[primary_role].service_manifest_sha256
                if primary_role is not None
                else _sha(f"{name}:service-manifest")
            ),
            service_policy_sha256=(
                bindings[primary_role].policy_sha256
                if primary_role is not None
                else _sha(f"{name}:service-policy")
            ),
            operations=operations,
            authority_binding_sha256s=tuple(
                sorted(bindings[role].binding_sha256 for role in _SERVICE_ROLES[name])
            ),
            socket_path=str((tmp_path / f"{name}.sock").resolve()),
            socket_owner_uid=os.getuid(),
            socket_group_gid=os.getgid(),
            socket_mode=0o660,
            peer_uid=os.getuid(),
            peer_gid=os.getgid(),
            receipt_key_id=controller_worker_rpc_key_id(public_key),
            receipt_public_key_ed25519_hex=public_key,
            valid_from=prepared_at - timedelta(days=1),
            expires_at=prepared_at + timedelta(days=1),
            connect_timeout_seconds=2.0,
            max_request_bytes=1024**2,
            max_response_bytes=1024**2,
        )
    return ControllerWorkerRPCServiceSet.model_validate(pins)


def _worker_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    wrong_source_step: ControllerStep | None = None,
) -> tuple[ResearchControllerWorkerRuntimeConfig, object]:
    terminal_config, controller_manifest = _terminal_runtime_config(monkeypatch, tmp_path)
    reader_payload = terminal_config.model_dump(
        mode="python",
        include=set(QualificationTerminalReaderConfig.model_fields),
    )
    reader_payload["schema_name"] = "aletheia.qualification_terminal_reader_config"
    reader_payload["schema_version"] = 1
    reader = QualificationTerminalReaderConfig.model_validate(reader_payload)
    bindings = _bindings()
    services = _service_set(
        tmp_path=tmp_path,
        prepared_at=controller_manifest.prepared_at,
        bindings=bindings,
    )
    adapters = tuple(
        ControllerStepAdapterManifest(
            step=step,
            adapter_code_sha256=(
                _sha("wrong-adapter-source")
                if step is wrong_source_step
                else controller_step_adapter_source_sha256(step)
            ),
            adapter_config_sha256=controller_step_rpc_configuration_sha256(step, services),
            authorities=tuple(bindings[role] for role in _STEP_ROLES[step]),
            prepared_at=controller_manifest.prepared_at,
        )
        for step in sorted(_STEP_ROLES, key=lambda item: item.value)
    )
    process_principal = "principal.controller.worker"
    adapter_set = ControllerStepAdapterSetManifest(
        controller_id=controller_manifest.controller_id,
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        worker_manifest_sha256=controller_manifest.worker_manifest_sha256,
        worker_process_principal_id=process_principal,
        adapters=adapters,
        prepared_at=controller_manifest.prepared_at,
    )
    return (
        ResearchControllerWorkerRuntimeConfig(
            role="worker",
            process_principal_id=process_principal,
            controller_id=controller_manifest.controller_id,
            controller_manifest_sha256=controller_manifest.manifest_sha256,
            database_url_sha256=hashlib.sha256(get_settings().database_url.encode()).hexdigest(),
            schema_revision=expected_schema_revision(),
            adapter_set_manifest=adapter_set,
            rpc_services=services,
            kernel_reader=_kernel_reader(tmp_path, controller_manifest.prepared_at),
            terminal_reader=reader,
            prepared_at=controller_manifest.prepared_at,
        ),
        controller_manifest,
    )


def test_complete_worker_composes_all_eight_adapters_without_opening_rpc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _worker_config(monkeypatch, tmp_path)
    service = compose_research_controller_worker_service(
        config=config,
        controller_manifest=controller_manifest,
        reviewed_code_root=Path(__file__).resolve().parents[2],
        kernel_store=_KernelStore(),
        terminal_outbox=_TerminalReader(),
    )

    assert type(service) is ResearchControllerService
    assert type(service._executor) is DedicatedControllerStepExecutor
    assert frozenset(service._executor._adapters) == frozenset(_STEP_ROLES)
    assert len(service._executor._adapters) == 8
    assert config.private_signing_key_loaded_in_worker is False
    assert config.generic_step_callback_allowed is False
    assert all(
        pin.private_key_loaded_in_worker is False for _name, pin in config.rpc_services.named_pins
    )


def test_worker_composition_fresh_hashes_each_adapter_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _worker_config(
        monkeypatch,
        tmp_path,
        wrong_source_step=ControllerStep.DERIVE_CONTINUATION,
    )
    with pytest.raises(ControllerWorkerCompositionError, match="derive_continuation"):
        compose_research_controller_worker_service(
            config=config,
            controller_manifest=controller_manifest,
            reviewed_code_root=Path(__file__).resolve().parents[2],
            kernel_store=_KernelStore(),
            terminal_outbox=_TerminalReader(),
        )


def test_worker_config_rejects_endpoint_rebinding_and_duplicate_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _controller_manifest = _worker_config(monkeypatch, tmp_path)
    payload = config.model_dump(mode="python", exclude={"configuration_id"})
    payload["rpc_services"]["execution_authorization"]["service_principal_id"] = "principal.rebound"
    payload["rpc_services"]["execution_authorization"].pop("service_id", None)
    with pytest.raises(ValidationError, match="adapter config hash"):
        ResearchControllerWorkerRuntimeConfig.model_validate(payload)

    overlap = config.model_dump(mode="python", exclude={"configuration_id"})
    overlap["kernel_reader"]["trust_root"]["commissioning_keys"][0]["principal_id"] = (
        config.rpc_services.action_proposal.service_principal_id
    )
    with pytest.raises(ValidationError, match="principals overlap"):
        ResearchControllerWorkerRuntimeConfig.model_validate(overlap)

    encoded = canonical_json_bytes(config)
    duplicate = encoded.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)
    with pytest.raises(ControllerWorkerCompositionError, match="config is invalid"):
        load_research_controller_worker_runtime_config(duplicate)


def test_guarded_runtime_loader_accepts_complete_checked_in_worker_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, controller_manifest = _worker_config(monkeypatch, tmp_path)
    root = Path(__file__).resolve().parents[2]
    factory = (root / "aletheia/research_controller_worker_runtime.py").resolve()
    controller_path = (tmp_path / "controller.json").resolve()
    controller_path.write_bytes(canonical_json_bytes(controller_manifest))
    config_path = (tmp_path / "worker-runtime.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    deployment = ResearchControllerRuntimeDeployment(
        role=ResearchControllerRuntimeRole.WORKER,
        controller_manifest_path=str(controller_path),
        controller_manifest_file_sha256=hashlib.sha256(controller_path.read_bytes()).hexdigest(),
        controller_manifest_sha256=controller_manifest.manifest_sha256,
        reviewed_code_root=str(root),
        composition_factory_module="aletheia.research_controller_worker_runtime",
        composition_factory_attribute="build_worker_runtime",
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        process_principal_id=config.process_principal_id,
        prepared_at=config.prepared_at,
    )

    runtime = build_research_controller_runtime(deployment)

    assert runtime.deployment == deployment
    assert callable(runtime._component.run_once)
    assert runtime._queue.principal == config.process_principal_id
    assert json.loads(config_path.read_bytes())["private_signing_key_loaded_in_worker"] is False
