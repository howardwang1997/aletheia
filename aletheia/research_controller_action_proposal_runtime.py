"""Guarded-loader factory for the conservative action-proposal RPC service."""

from __future__ import annotations


def build_action_proposal_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``MATERIALIZE_ACTION_PROPOSAL`` without any signing authority."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from datetime import datetime, timezone
    from pathlib import Path
    from typing import Literal

    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision
    from aletheia.research_controller import action_proposal_provider as provider_module
    from aletheia.research_controller.action_proposal_provider import (
        DeterministicActionProposalPolicyPin,
        DeterministicActionProposalProvider,
    )
    from aletheia.research_controller.action_proposal_service import (
        ActionProposalMaterializationService,
        PostgreSQLActionProposalContextSource,
        WriteOnceActionProposalSpool,
    )
    from aletheia.research_controller.action_proposals import ActionProposalBlocked
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ControllerTickRPCPayload,
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        ControllerWorkerRPCServiceBlocked,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class ActionProposalSpoolRootPin(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        path: str
        owner_uid: int = Field(ge=1, le=2**31 - 1)
        owner_gid: int = Field(ge=1, le=2**31 - 1)
        device_id: int = Field(ge=0)
        inode: int = Field(ge=1)
        directory_mode: Literal[0o700] = 0o700

        @model_validator(mode="after")
        def _path_is_canonical(self):
            candidate = Path(self.path)
            if (
                not self.path
                or "\x00" in self.path
                or "\n" in self.path
                or "\r" in self.path
                or not candidate.is_absolute()
                or self.path != os.path.normpath(self.path)
                or self.path == "/"
            ):
                raise ValueError("action proposal spool root must be canonical and absolute")
            return self

    class ActionProposalRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.action_proposal_rpc_service_config"] = (
            "aletheia.action_proposal_rpc_service_config"
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
        proposal_policy: DeterministicActionProposalPolicyPin
        provider_implementation_source_path: str
        provider_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        submission_spool_root: ActionProposalSpoolRootPin
        prepared_at: AwareDatetime
        direct_scientific_authority: Literal[False] = False
        kernel_signing_key_loaded: Literal[False] = False
        observation_signing_key_loaded: Literal[False] = False
        execution_access_allowed: Literal[False] = False
        generic_model_callback_allowed: Literal[False] = False
        cost_or_risk_authority_loaded: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_closed(self):
            binding = self.authority_binding
            policy = self.proposal_policy
            source = Path(self.provider_implementation_source_path)
            if (
                binding.role is not ControllerStepAuthorityRole.ACTION_PROPOSAL
                or binding.key_id is not None
                or not binding.externally_deployed
                or binding.policy_sha256 != policy.policy_sha256
                or policy.provider_implementation_sha256
                != self.provider_implementation_source_sha256
                or policy.provider_principal_id != binding.principal_id
                or not source.is_absolute()
                or self.provider_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("action proposal RPC authority or implementation is not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate action proposal RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_source_bytes(path: Path, expected_sha256: str) -> bytes:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError("action proposal implementation source traverses a symlink")
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
                    raise ValueError(
                        "action proposal implementation source is not bounded regular data"
                    )
                chunks = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ValueError("action proposal implementation source ended unexpectedly")
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
                    raise ValueError("action proposal implementation source changed while read")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ValueError("action proposal implementation source is unavailable") from exc
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("action proposal implementation differs from its byte pin")
        return payload

    def verify_directory(pin: ActionProposalSpoolRootPin) -> None:
        candidate = Path(pin.path)
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("action proposal spool root is unavailable") from exc
        if (
            resolved != candidate
            or candidate.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
            )
            != (
                pin.owner_uid,
                pin.owner_gid,
                pin.device_id,
                pin.inode,
                pin.directory_mode,
            )
        ):
            raise ValueError("action proposal spool root differs from its custody pin")

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = ActionProposalRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("action proposal RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("action proposal RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    if (
        pin.operations != (ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,)
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
        or config.submission_spool_root.owner_uid != deployment.process_uid
        or config.submission_spool_root.owner_gid != deployment.process_gid
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ValueError("action proposal RPC config differs from its deployment or database")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.provider_implementation_source_path)
    spool_path = Path(config.submission_spool_root.path)
    cas_path = Path(config.kernel_reader.cas_root)
    expected_module_path = Path(provider_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("action proposal implementation escaped reviewed source") from exc
    for first, second in (
        (spool_path, reviewed_root),
        (spool_path, cas_path),
        (spool_path, Path(deployment.socket_parent_path)),
        (spool_path, Path(deployment.receipt_private_key_path).parent),
    ):
        if first == second or first in second.parents or second in first.parents:
            raise ValueError("action proposal writable spool overlaps another custody root")
    before = fresh_source_bytes(
        implementation_path,
        config.provider_implementation_source_sha256,
    )
    if implementation_path != expected_module_path:
        raise ValueError("action proposal implementation path resolved another module")

    try:
        if cas_path.resolve(strict=True) != cas_path or cas_path.is_symlink():
            raise ValueError("action proposal Kernel CAS traverses a symlink")
        cas_metadata = cas_path.lstat()
    except OSError as exc:
        raise ValueError("action proposal Kernel CAS is unavailable") from exc
    if (
        not stat.S_ISDIR(cas_metadata.st_mode)
        or cas_metadata.st_uid != config.kernel_reader.cas_owner_uid
        or cas_metadata.st_gid != config.kernel_reader.cas_group_gid
        or cas_metadata.st_dev != config.kernel_reader.cas_device_id
        or cas_metadata.st_ino != config.kernel_reader.cas_inode
        or stat.S_IMODE(cas_metadata.st_mode) != config.kernel_reader.cas_directory_mode
    ):
        raise ValueError("action proposal Kernel CAS differs from its custody pin")
    archive = FilesystemResearchArchive(
        cas_path,
        max_object_bytes=config.kernel_reader.max_object_bytes,
        read_only=True,
    )
    kernel_store = ResearchKernelStore(
        trust_root=config.kernel_reader.trust_root,
        archive=archive,
    )
    verify_directory(config.submission_spool_root)
    spool = WriteOnceActionProposalSpool(
        Path(config.submission_spool_root.path),
        authority_binding=binding,
        owner_uid=config.submission_spool_root.owner_uid,
        owner_gid=config.submission_spool_root.owner_gid,
        device_id=config.submission_spool_root.device_id,
        inode=config.submission_spool_root.inode,
        directory_mode=config.submission_spool_root.directory_mode,
    )
    provider = DeterministicActionProposalProvider(
        policy=config.proposal_policy,
        implementation_sha256=config.provider_implementation_source_sha256,
        principal_id=binding.principal_id,
    )
    service = ActionProposalMaterializationService(
        context_source=PostgreSQLActionProposalContextSource(
            kernel_store=kernel_store,
            object_archive=archive,
        ),
        provider=provider,
        draft_verifier=provider,
        submissions=spool,
        clock=lambda: datetime.now(timezone.utc),
    )

    def materialize_action_proposal(payload):
        if type(payload) is not ControllerTickRPCPayload:
            raise TypeError("action proposal RPC handler received another payload type")
        try:
            return service.materialize_and_submit(
                wakeup=payload.wakeup,
                projection=payload.projection,
                plan=payload.plan,
            )
        except ActionProposalBlocked as exc:
            raise ControllerWorkerRPCServiceBlocked(exc.blocker_codes) from exc

    verify_directory(config.submission_spool_root)
    after = fresh_source_bytes(
        implementation_path,
        config.provider_implementation_source_sha256,
    )
    if before != after:
        raise ValueError("action proposal implementation changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,
                handler=materialize_action_proposal,
            ),
        ),
    )


__all__ = ["build_action_proposal_rpc_service"]
