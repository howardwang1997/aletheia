"""Guarded-loader factory for the frozen-template protocol-compilation RPC service."""

from __future__ import annotations


def build_protocol_compilation_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``COMPILE_PROTOCOL`` without a model, signer, or execution port."""

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
    from aletheia.research_controller import protocol_template_provider as provider_module
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ControllerTickRPCPayload,
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        ControllerWorkerRPCServiceBlocked,
    )
    from aletheia.research_controller.protocol_compilation_step import (
        DurableProtocolCompilationService,
        ProtocolCompilationPolicyPin,
        ProtocolCompilationUnavailable,
    )
    from aletheia.research_controller.protocol_template_provider import (
        FrozenProtocolTemplateProvider,
        FrozenProtocolTemplateProviderPolicyPin,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class ProtocolCompilationRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.protocol_compilation_rpc_service_config"] = (
            "aletheia.protocol_compilation_rpc_service_config"
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
        compilation_policy: ProtocolCompilationPolicyPin
        provider_policy: FrozenProtocolTemplateProviderPolicyPin
        provider_implementation_source_path: str
        provider_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        direct_scientific_authority: Literal[False] = False
        kernel_signing_key_loaded: Literal[False] = False
        observation_signing_key_loaded: Literal[False] = False
        execution_access_allowed: Literal[False] = False
        generic_model_callback_allowed: Literal[False] = False
        dynamic_template_mutation_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_closed(self):
            binding = self.authority_binding
            provider = self.provider_policy
            source = Path(self.provider_implementation_source_path)
            if (
                binding.role is not ControllerStepAuthorityRole.PROTOCOL_COMPILATION
                or binding.key_id is not None
                or not binding.externally_deployed
                or binding.policy_sha256 != self.compilation_policy.policy_sha256
                or provider.compilation_policy_sha256 != self.compilation_policy.policy_sha256
                or provider.provider_implementation_sha256
                != self.provider_implementation_source_sha256
                or provider.prepared_by_principal_id
                not in self.compilation_policy.allowed_protocol_author_principal_ids
                or not source.is_absolute()
                or self.provider_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("protocol compilation RPC authority or policies are not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate protocol compilation RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_source_bytes(path: Path, expected_sha256: str) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError("protocol provider source traverses a symlink")
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
                    raise ValueError("protocol provider source is not bounded regular data")
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError("protocol provider source ended unexpectedly")
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
                    raise ValueError("protocol provider source changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError("protocol provider source is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("protocol provider source differs from its byte pin")
        return payload

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = ProtocolCompilationRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("protocol compilation RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("protocol compilation RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    if (
        pin.operations != (ControllerWorkerRPCOperation.COMPILE_PROTOCOL,)
        or pin.authority_binding_sha256s != (binding.binding_sha256,)
        or pin.service_principal_id != binding.principal_id
        or pin.service_manifest_sha256 != binding.service_manifest_sha256
        or pin.service_policy_sha256 != binding.policy_sha256
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ValueError("protocol compilation RPC config differs from deployment or database")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.provider_implementation_source_path)
    expected_module_path = Path(provider_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("protocol provider implementation escaped reviewed source") from exc
    before = fresh_source_bytes(
        implementation_path,
        config.provider_implementation_source_sha256,
    )
    if implementation_path != expected_module_path:
        raise ValueError("protocol provider implementation resolved another module")

    cas_path = Path(config.kernel_reader.cas_root)
    try:
        if cas_path.resolve(strict=True) != cas_path or cas_path.is_symlink():
            raise ValueError("protocol compilation Kernel CAS traverses a symlink")
        cas_metadata = cas_path.lstat()
    except OSError as exc:
        raise ValueError("protocol compilation Kernel CAS is unavailable") from exc
    if (
        not stat.S_ISDIR(cas_metadata.st_mode)
        or cas_metadata.st_uid != config.kernel_reader.cas_owner_uid
        or cas_metadata.st_gid != config.kernel_reader.cas_group_gid
        or cas_metadata.st_dev != config.kernel_reader.cas_device_id
        or cas_metadata.st_ino != config.kernel_reader.cas_inode
        or stat.S_IMODE(cas_metadata.st_mode) != config.kernel_reader.cas_directory_mode
    ):
        raise ValueError("protocol compilation Kernel CAS differs from its custody pin")
    archive = FilesystemResearchArchive(
        cas_path,
        max_object_bytes=config.kernel_reader.max_object_bytes,
        read_only=True,
    )
    kernel_store = ResearchKernelStore(
        trust_root=config.kernel_reader.trust_root,
        archive=archive,
    )
    provider = FrozenProtocolTemplateProvider(
        policy=config.provider_policy,
        compilation_policy=config.compilation_policy,
        implementation_sha256=config.provider_implementation_source_sha256,
    )
    service = DurableProtocolCompilationService(
        kernel_store=kernel_store,
        object_archive=archive,
        provider=provider,
        preparation_verifier=provider,
        compilation_policy=config.compilation_policy,
        authority_binding=binding,
    )

    def compile_protocol(payload):
        if type(payload) is not ControllerTickRPCPayload:
            raise TypeError("protocol compilation RPC handler received another payload type")
        try:
            return service.compile_and_register(
                wakeup=payload.wakeup,
                projection=payload.projection,
                plan=payload.plan,
            )
        except ProtocolCompilationUnavailable as exc:
            raise ControllerWorkerRPCServiceBlocked(exc.blocker_codes) from exc

    after = fresh_source_bytes(
        implementation_path,
        config.provider_implementation_source_sha256,
    )
    if before != after:
        raise ValueError("protocol provider implementation changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.COMPILE_PROTOCOL,
                handler=compile_protocol,
            ),
        ),
    )


__all__ = ["build_protocol_compilation_rpc_service"]
