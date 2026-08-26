"""Guarded-loader factory for the atomic scientific execution-registration RPC service."""

from __future__ import annotations


def build_execution_registration_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``REGISTER_EXECUTION`` without loading a domain signing key."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from pathlib import Path
    from typing import Literal

    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision
    from aletheia.execution.registration_custody import (
        QualificationExecutionRegistrationConfig,
        compose_qualification_execution_registration,
    )
    from aletheia.observations import execution_registration as registration_module
    from aletheia.observations.adapters import PostgreSQLResearchActionAuthorityAdapter
    from aletheia.observations.execution_registration import (
        PostgreSQLAtomicScientificExecutionRegistrar,
        ScientificExecutionRegistrationVerificationContext,
    )
    from aletheia.observations.scientific_bridge import (
        ScientificBridgeAuthorityPin,
        ScientificBridgeRole,
    )
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        ScientificExecutionRegistrationRPCPayload,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class ExecutionRegistrationRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.execution_registration_rpc_service_config"] = (
            "aletheia.execution_registration_rpc_service_config"
        )
        schema_version: Literal[1] = 1
        controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
        controller_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        worker_process_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
        service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
        service_pin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        database_url_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
        kernel_reader: ResearchKernelReadOnlyConfig
        authority_binding: ControllerStepAuthorityBinding
        execution_authority_pin: ScientificBridgeAuthorityPin
        validator_authority_pin: ScientificBridgeAuthorityPin
        admission_authority_pin: ScientificBridgeAuthorityPin
        qualification_registration: QualificationExecutionRegistrationConfig
        registrar_implementation_source_path: str
        registrar_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        private_domain_signing_key_loaded: Literal[False] = False
        runtime_control_signing_key_loaded: Literal[False] = False
        execution_launch_allowed: Literal[False] = False
        node_registry_mutation_allowed: Literal[False] = False
        terminal_commit_allowed: Literal[False] = False
        direct_kernel_mutation_allowed: Literal[False] = False
        direct_observation_admission_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_closed(self):
            binding = self.authority_binding
            execution = self.execution_authority_pin
            validator = self.validator_authority_pin
            admission = self.admission_authority_pin
            registration = self.qualification_registration
            source = Path(self.registrar_implementation_source_path)
            bridge_principals = (
                execution.principal_id,
                validator.principal_id,
                admission.principal_id,
            )
            bridge_keys = (execution.key_id, validator.key_id, admission.key_id)
            bridge_policies = (
                execution.policy_sha256,
                validator.policy_sha256,
                admission.policy_sha256,
            )
            kernel_principals = tuple(
                key.principal_id for key in self.kernel_reader.trust_root.commissioning_keys
            )
            kernel_keys = tuple(
                key.key_id for key in self.kernel_reader.trust_root.commissioning_keys
            )
            if (
                binding.role is not ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION
                or not binding.externally_deployed
                or execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
                or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
                or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
                or binding.principal_id != execution.principal_id
                or binding.key_id != execution.key_id
                or binding.policy_sha256 != execution.policy_sha256
                or self.prepared_at != registration.prepared_at
                or not all(
                    pin.active_at(self.prepared_at) for pin in (execution, validator, admission)
                )
                or len(set(bridge_principals)) != len(bridge_principals)
                or len(set(bridge_keys)) != len(bridge_keys)
                or len(set(bridge_policies)) != len(bridge_policies)
                or set(bridge_principals) & set(registration.authority_principal_ids)
                or set(bridge_keys) & set(registration.authority_key_ids)
                or set(bridge_policies) & set(registration.authority_policy_sha256s)
                or set(kernel_principals)
                & (set(bridge_principals) | set(registration.authority_principal_ids))
                or set(kernel_keys) & (set(bridge_keys) | set(registration.authority_key_ids))
                or self.worker_process_principal_id
                in (
                    set(bridge_principals)
                    | set(registration.authority_principal_ids)
                    | set(kernel_principals)
                )
                or not source.is_absolute()
                or self.registrar_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("execution registration RPC authority is not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate execution registration RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(path: Path, *, expected_sha256: str, label: str) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError(f"{label} traverses a symlink")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or not 0 < before.st_size <= 4 * 1024 * 1024
                ):
                    raise ValueError(f"{label} has unsafe file custody")
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError(f"{label} ended unexpectedly")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if os.read(descriptor, 1) or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(f"{label} changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(f"{label} differs from its byte pin")
        return payload

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = ExecutionRegistrationRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution registration RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("execution registration RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    registration = config.qualification_registration
    signed_public_keys = {
        config.execution_authority_pin.public_key_ed25519_hex,
        config.validator_authority_pin.public_key_ed25519_hex,
        config.admission_authority_pin.public_key_ed25519_hex,
        registration.qualification_custody.pricing_authority_pin.public_key_ed25519_hex,
        registration.qualification_custody.source_budget_authority_pin.public_key_ed25519_hex,
        registration.qualification_custody.qualification_authority_pin.public_key_ed25519_hex,
        registration.qualification_custody.terminal_verification_authority_pin.public_key_ed25519_hex,
        registration.runtime_control_authority_pin.public_key_ed25519_hex,
        *(
            item.manifest.node_signing_public_key_ed25519_hex
            for item in registration.node_authorities
        ),
        *(
            item.enrollment_authority_pin.public_key_ed25519_hex
            for item in registration.node_authorities
        ),
        *(key.public_key_ed25519_hex for key in config.kernel_reader.trust_root.commissioning_keys),
    }
    transport_public_keys = {
        item.assignment_transport_pin.public_key_x25519_hex
        for item in registration.node_authorities
    }
    all_authority_principals = (
        set(registration.authority_principal_ids)
        | {
            config.execution_authority_pin.principal_id,
            config.validator_authority_pin.principal_id,
            config.admission_authority_pin.principal_id,
        }
        | {key.principal_id for key in config.kernel_reader.trust_root.commissioning_keys}
    )
    if (
        pin.operations != (ControllerWorkerRPCOperation.REGISTER_EXECUTION,)
        or pin.authority_binding_sha256s != (binding.binding_sha256,)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
        or pin.service_principal_id in all_authority_principals
        or pin.service_principal_id == config.worker_process_principal_id
        or pin.service_policy_sha256
        in {
            config.execution_authority_pin.policy_sha256,
            config.validator_authority_pin.policy_sha256,
            config.admission_authority_pin.policy_sha256,
            registration.qualification_custody.pricing_authority_pin.policy_sha256,
            registration.qualification_custody.source_budget_authority_pin.policy_sha256,
            registration.qualification_custody.qualification_authority_pin.policy_sha256,
            registration.qualification_custody.terminal_verification_authority_pin.policy_sha256,
            registration.runtime_control_authority_pin.policy_sha256,
            *registration.authority_policy_sha256s,
        }
        or pin.receipt_public_key_ed25519_hex in signed_public_keys | transport_public_keys
    ):
        raise ValueError("execution registration RPC config differs from deployment or authority")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.registrar_implementation_source_path)
    cas_path = Path(config.kernel_reader.cas_root)
    expected_module_path = Path(registration_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("execution registration implementation escaped reviewed source") from exc
    if implementation_path != expected_module_path:
        raise ValueError("execution registration implementation resolved another module")
    custody_roots = (
        Path(registration.qualification_custody.artifact_store_root),
        Path(registration.qualification_custody.authority_registry_root),
        cas_path,
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        Path(deployment.receipt_private_key_path).parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("execution registration custody roots overlap")

    before = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.registrar_implementation_source_sha256,
        label="execution registration implementation",
    )
    try:
        if cas_path.resolve(strict=True) != cas_path or cas_path.is_symlink():
            raise ValueError("execution registration Kernel CAS traverses a symlink")
        cas_metadata = cas_path.lstat()
    except OSError as exc:
        raise ValueError("execution registration Kernel CAS is unavailable") from exc
    if (
        not stat.S_ISDIR(cas_metadata.st_mode)
        or cas_metadata.st_uid != config.kernel_reader.cas_owner_uid
        or cas_metadata.st_gid != config.kernel_reader.cas_group_gid
        or cas_metadata.st_dev != config.kernel_reader.cas_device_id
        or cas_metadata.st_ino != config.kernel_reader.cas_inode
        or stat.S_IMODE(cas_metadata.st_mode) != config.kernel_reader.cas_directory_mode
    ):
        raise ValueError("execution registration Kernel CAS differs from its custody pin")
    archive = FilesystemResearchArchive(
        cas_path,
        max_object_bytes=config.kernel_reader.max_object_bytes,
        read_only=True,
    )
    kernel_store = ResearchKernelStore(
        trust_root=config.kernel_reader.trust_root,
        archive=archive,
    )
    action_authority = PostgreSQLResearchActionAuthorityAdapter(kernel_store)
    qualification = compose_qualification_execution_registration(registration)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=ScientificExecutionRegistrationVerificationContext(
            qualification_authority=qualification.qualification_authority,
            current_action_authority=action_authority,
            qualification_custody=qualification.qualification_custody,
            execution_authority_pin=config.execution_authority_pin,
            validator_authority_pin=config.validator_authority_pin,
            admission_authority_pin=config.admission_authority_pin,
        ),
        allocator=qualification.allocator,
    )

    def register_execution(payload):
        if type(payload) is not ScientificExecutionRegistrationRPCPayload:
            raise TypeError("execution registration RPC handler received another payload type")
        return registrar.register_and_reserve(payload.authorization)

    after = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.registrar_implementation_source_sha256,
        label="execution registration implementation",
    )
    if before != after:
        raise ValueError("execution registration implementation changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.REGISTER_EXECUTION,
                handler=register_execution,
            ),
        ),
    )


__all__ = ["build_execution_registration_rpc_service"]
