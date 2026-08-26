"""Guarded-loader factory for the scientific execution-authorization RPC service."""

from __future__ import annotations


def build_execution_authorization_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``ISSUE_EXECUTION_AUTHORIZATION`` with one domain signing key."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from pathlib import Path
    from typing import Literal

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision
    from aletheia.execution.qualification_custody import (
        QualificationPreAdmissionCustodyConfig,
        compose_qualification_pre_admission_verification,
    )
    from aletheia.research_controller import execution_authorization_service as issuer_module
    from aletheia.research_controller.execution_authorization_service import (
        FrozenScientificExecutionAuthorizationCatalog,
        FrozenScientificExecutionAuthorizationIssuer,
        PostgreSQLScientificExecutionAuthorizationSource,
    )
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        ControllerTickRPCPayload,
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class ExecutionAuthorizationSigningKeyPin(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        path: str
        file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
        owner_uid: int = Field(ge=1, le=2**31 - 1)
        owner_gid: int = Field(ge=1, le=2**31 - 1)
        file_mode: Literal[0o400] = 0o400

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
                raise ValueError("execution authorization key path must be canonical and absolute")
            return self

    class ExecutionAuthorizationRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.execution_authorization_rpc_service_config"] = (
            "aletheia.execution_authorization_rpc_service_config"
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
        qualification_custody: QualificationPreAdmissionCustodyConfig
        authorization_catalog: FrozenScientificExecutionAuthorizationCatalog
        issuer_implementation_source_path: str
        issuer_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        execution_signing_key: ExecutionAuthorizationSigningKeyPin
        prepared_at: AwareDatetime
        direct_kernel_mutation_allowed: Literal[False] = False
        execution_launch_allowed: Literal[False] = False
        qualification_admission_allowed: Literal[False] = False
        direct_observation_admission_allowed: Literal[False] = False
        validator_signing_key_loaded: Literal[False] = False
        admission_signing_key_loaded: Literal[False] = False
        kernel_signing_key_loaded: Literal[False] = False
        dynamic_template_mutation_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_closed(self):
            binding = self.authority_binding
            catalog = self.authorization_catalog
            execution = catalog.execution_authority_pin
            source = Path(self.issuer_implementation_source_path)
            custody = self.qualification_custody
            principals = (
                execution.principal_id,
                catalog.validator_authority_pin.principal_id,
                catalog.admission_authority_pin.principal_id,
                custody.qualification_authority_pin.principal_id,
                custody.pricing_authority_pin.principal_id,
                custody.source_budget_authority_pin.principal_id,
                custody.terminal_verification_authority_pin.principal_id,
                custody.artifact_verifier_principal_id,
                custody.input_resolver_principal_id,
            )
            keys = (
                execution.key_id,
                catalog.validator_authority_pin.key_id,
                catalog.admission_authority_pin.key_id,
                custody.qualification_authority_pin.key_id,
                custody.pricing_authority_pin.key_id,
                custody.source_budget_authority_pin.key_id,
                custody.terminal_verification_authority_pin.key_id,
            )
            policies = (
                execution.policy_sha256,
                catalog.validator_authority_pin.policy_sha256,
                catalog.admission_authority_pin.policy_sha256,
                custody.qualification_authority_pin.policy_sha256,
                custody.pricing_authority_pin.policy_sha256,
                custody.source_budget_authority_pin.policy_sha256,
                custody.terminal_verification_authority_pin.policy_sha256,
            )
            auxiliary_principals = {
                custody.pricing_authority_pin.principal_id,
                custody.source_budget_authority_pin.principal_id,
                custody.terminal_verification_authority_pin.principal_id,
                custody.artifact_verifier_principal_id,
                custody.input_resolver_principal_id,
            }
            proposal_role_overlap = any(
                auxiliary_principals
                & {
                    item.action_protocol_binding.action.proposed_by_principal_id,
                    item.action_protocol_binding.action_authorized_event.principal_id,
                }
                for item in catalog.templates
            )
            template_authority_rebound = any(
                (
                    item.qualification_bundle.cost_quote.quoted_by_principal_id
                    != custody.pricing_authority_pin.principal_id
                    or item.qualification_bundle.cost_quote.pricing_policy_sha256
                    != custody.pricing_authority_pin.policy_sha256
                    or item.qualification_bundle.budget_authorization.authorized_by_principal_id
                    != custody.source_budget_authority_pin.principal_id
                    or item.qualification_grant.message.authorized_by_principal_id
                    != custody.qualification_authority_pin.principal_id
                    or item.qualification_grant.message.authorization_key_id
                    != custody.qualification_authority_pin.key_id
                    or item.qualification_grant.message.qualification_authority_policy_sha256
                    != custody.qualification_authority_pin.policy_sha256
                )
                for item in catalog.templates
            )
            if (
                binding.role is not ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION
                or not binding.externally_deployed
                or binding.principal_id != execution.principal_id
                or binding.key_id != execution.key_id
                or binding.policy_sha256 != execution.policy_sha256
                or self.execution_signing_key.key_id != execution.key_id
                or catalog.qualification_authority_pin
                != self.qualification_custody.qualification_authority_pin
                or catalog.issuer_implementation_sha256 != self.issuer_implementation_source_sha256
                or custody.prepared_at != self.prepared_at
                or self.worker_process_principal_id in set(principals)
                or len(set(principals)) != len(principals)
                or len(set(keys)) != len(keys)
                or len(set(policies)) != len(policies)
                or proposal_role_overlap
                or template_authority_rebound
                or not source.is_absolute()
                or self.issuer_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("execution authorization RPC authority is not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate execution authorization RPC config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(
        path: Path,
        *,
        expected_sha256: str,
        expected_size: int | None = None,
        expected_owner: tuple[int, int] | None = None,
        expected_mode: int | None = None,
        label: str,
    ) -> bytes:
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
                    or (expected_size is not None and before.st_size != expected_size)
                    or (expected_size is None and not 0 < before.st_size <= 4 * 1024 * 1024)
                    or (
                        expected_owner is not None
                        and (before.st_uid, before.st_gid) != expected_owner
                    )
                    or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
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
        config = ExecutionAuthorizationRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution authorization RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("execution authorization RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    key_pin = config.execution_signing_key
    if (
        pin.operations != (ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,)
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
        or key_pin.owner_uid != deployment.process_uid
        or key_pin.owner_gid != deployment.process_gid
        or key_pin.key_id != binding.key_id
        or pin.receipt_key_id
        in {
            config.authorization_catalog.execution_authority_pin.key_id,
            config.authorization_catalog.validator_authority_pin.key_id,
            config.authorization_catalog.admission_authority_pin.key_id,
            config.qualification_custody.qualification_authority_pin.key_id,
            config.qualification_custody.pricing_authority_pin.key_id,
            config.qualification_custody.source_budget_authority_pin.key_id,
            config.qualification_custody.terminal_verification_authority_pin.key_id,
        }
        or key_pin.file_sha256 == deployment.receipt_private_key_sha256
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ValueError("execution authorization RPC config differs from deployment or database")

    reviewed_root = Path(deployment.reviewed_code_root)
    implementation_path = Path(config.issuer_implementation_source_path)
    key_path = Path(key_pin.path)
    receipt_key_path = Path(deployment.receipt_private_key_path)
    cas_path = Path(config.kernel_reader.cas_root)
    expected_module_path = Path(issuer_module.__file__).resolve(strict=True)
    try:
        implementation_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("execution authorization implementation escaped reviewed source") from exc
    if implementation_path != expected_module_path:
        raise ValueError("execution authorization implementation resolved another module")
    for first, second in (
        (key_path, receipt_key_path),
        (key_path, reviewed_root),
        (key_path, cas_path),
        (key_path, Path(config.qualification_custody.artifact_store_root)),
        (key_path, Path(config.qualification_custody.authority_registry_root)),
        (key_path, Path(deployment.socket_parent_path)),
        (key_path, Path(deployment.composition_config_path)),
    ):
        if first == second or first in second.parents or second in first.parents:
            raise ValueError("execution authorization key overlaps another custody root")
    custody_roots = (
        Path(config.qualification_custody.artifact_store_root),
        Path(config.qualification_custody.authority_registry_root),
        cas_path,
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        receipt_key_path.parent,
        key_path.parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("execution authorization custody roots overlap")

    before = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.issuer_implementation_source_sha256,
        label="execution authorization implementation",
    )
    private_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.owner_gid),
        expected_mode=key_pin.file_mode,
        label="execution authorization private key",
    )
    public_key_hex = (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    if (
        public_key_hex
        != config.authorization_catalog.execution_authority_pin.public_key_ed25519_hex
    ):
        raise ValueError("execution authorization private key differs from its public pin")

    cas_path = Path(config.kernel_reader.cas_root)
    try:
        if cas_path.resolve(strict=True) != cas_path or cas_path.is_symlink():
            raise ValueError("execution authorization Kernel CAS traverses a symlink")
        cas_metadata = cas_path.lstat()
    except OSError as exc:
        raise ValueError("execution authorization Kernel CAS is unavailable") from exc
    if (
        not stat.S_ISDIR(cas_metadata.st_mode)
        or cas_metadata.st_uid != config.kernel_reader.cas_owner_uid
        or cas_metadata.st_gid != config.kernel_reader.cas_group_gid
        or cas_metadata.st_dev != config.kernel_reader.cas_device_id
        or cas_metadata.st_ino != config.kernel_reader.cas_inode
        or stat.S_IMODE(cas_metadata.st_mode) != config.kernel_reader.cas_directory_mode
    ):
        raise ValueError("execution authorization Kernel CAS differs from its custody pin")
    archive = FilesystemResearchArchive(
        cas_path,
        max_object_bytes=config.kernel_reader.max_object_bytes,
        read_only=True,
    )
    kernel_store = ResearchKernelStore(
        trust_root=config.kernel_reader.trust_root,
        archive=archive,
    )
    qualification = compose_qualification_pre_admission_verification(config.qualification_custody)
    source = PostgreSQLScientificExecutionAuthorizationSource(kernel_store=kernel_store)
    service = FrozenScientificExecutionAuthorizationIssuer(
        source=source,
        qualification_authority=qualification.authority,
        qualification_custody=qualification.custody,
        catalog=config.authorization_catalog,
        authority_binding=binding,
        private_key=private_key,
        implementation_sha256=config.issuer_implementation_source_sha256,
    )

    def issue_execution_authorization(payload):
        if type(payload) is not ControllerTickRPCPayload:
            raise TypeError("execution authorization RPC handler received another payload type")
        return service.issue_scientific_execution_authorization(
            wakeup=payload.wakeup,
            projection=payload.projection,
            plan=payload.plan,
        )

    after = fresh_regular_bytes(
        implementation_path,
        expected_sha256=config.issuer_implementation_source_sha256,
        label="execution authorization implementation",
    )
    if before != after:
        raise ValueError("execution authorization implementation changed during composition")
    final_private_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.owner_gid),
        expected_mode=key_pin.file_mode,
        label="execution authorization private key",
    )
    if private_key != final_private_key:
        raise ValueError("execution authorization private key changed during composition")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,
                handler=issue_execution_authorization,
            ),
        ),
    )


__all__ = ["build_execution_authorization_rpc_service"]
