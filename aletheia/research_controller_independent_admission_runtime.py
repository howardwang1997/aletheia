"""Guarded-loader factory for the isolated independent-observation-admission signer."""

from __future__ import annotations


def build_independent_admission_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``ISSUE_ADMISSION_DECISION`` with only the admitter key."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from datetime import datetime, timedelta
    from pathlib import Path
    from typing import Literal

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
    from sqlalchemy import func, select

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision, session_factory
    from aletheia.execution.artifact_store import LocalArtifactStore
    from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
    from aletheia.execution.terminal_runtime import (
        QualificationTerminalReaderConfig,
        compose_qualification_run_lineage_reader,
    )
    from aletheia.observations import admission_service as admission_module
    from aletheia.observations.adapters import (
        PostgreSQLRawRunCustodyVerificationAdapter,
        PostgreSQLResearchActionAuthorityAdapter,
    )
    from aletheia.observations.admission_service import (
        IndependentAdmissionVerificationContext,
        IndependentObservationAdmissionService,
    )
    from aletheia.observations.f9_v2_validation import (
        WriteOnceF9V2ValidationCampaignArchive,
    )
    from aletheia.observations.scientific_bridge import (
        ObservationDatabaseAuthorityPin,
        ScientificBridgeAuthorityPin,
        ScientificBridgeRole,
        VerifiedExecutionAuthorityProjection,
        scientific_bridge_key_id,
    )
    from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
    from aletheia.research_controller.external_rpc_server import (
        AdmissionDecisionIssuanceRPCPayload,
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

    class AdmissionSigningKeyPin(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        path: str
        file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
        owner_uid: int = Field(ge=0, le=2**31 - 1)
        group_gid: int = Field(ge=0, le=2**31 - 1)
        file_mode: Literal[0o400] = 0o400

        @model_validator(mode="after")
        def _path_is_canonical(self):
            candidate = Path(self.path)
            if (
                not candidate.is_absolute()
                or self.path != os.path.normpath(candidate)
                or self.path == "/"
            ):
                raise ValueError("admission signing key path is not canonical")
            return self

    class F9V2ValidationArchiveReadConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        root: str
        owner_uid: int = Field(ge=0, le=2**31 - 1)
        group_gid: int = Field(ge=0, le=2**31 - 1)
        device_id: int = Field(ge=0)
        inode: int = Field(gt=0)
        directory_mode: int = Field(ge=0, le=0o777)
        validator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        read_only: Literal[True] = True
        campaign_publication_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _root_is_canonical_and_readable(self):
            candidate = Path(self.root)
            readable = any(self.directory_mode & mask == mask for mask in (0o500, 0o050, 0o005))
            if (
                not candidate.is_absolute()
                or self.root != os.path.normpath(candidate)
                or self.root == "/"
                or self.directory_mode & 0o022
                or not readable
            ):
                raise ValueError("admission validation archive read root is not closed")
            return self

    class IndependentAdmissionRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.independent_admission_rpc_service_config"] = (
            "aletheia.independent_admission_rpc_service_config"
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
        database_authority_pin: ObservationDatabaseAuthorityPin
        execution_authority_pin: ScientificBridgeAuthorityPin
        validator_authority_pin: ScientificBridgeAuthorityPin
        admission_authority_pin: ScientificBridgeAuthorityPin
        qualification_reader: QualificationTerminalReaderConfig
        artifact_verification_authority: VerifiedExecutionAuthorityProjection
        validation_archive: F9V2ValidationArchiveReadConfig
        admission_signing_key: AdmissionSigningKeyPin
        service_implementation_source_path: str
        service_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        admission_signing_key_loaded: Literal[True] = True
        database_signing_key_loaded: Literal[False] = False
        execution_signing_key_loaded: Literal[False] = False
        validator_signing_key_loaded: Literal[False] = False
        kernel_signing_key_loaded: Literal[False] = False
        database_mutation_allowed: Literal[False] = False
        execution_mutation_allowed: Literal[False] = False
        campaign_publication_allowed: Literal[False] = False
        validation_receipt_issuance_allowed: Literal[False] = False
        scientific_slot_commit_allowed: Literal[False] = False
        direct_kernel_mutation_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_isolated_and_closed(self):
            binding = self.authority_binding
            database = self.database_authority_pin
            execution = self.execution_authority_pin
            validator = self.validator_authority_pin
            admission = self.admission_authority_pin
            reader = self.qualification_reader
            source = Path(self.service_implementation_source_path)
            bridge_principals = (
                database.principal_id,
                execution.principal_id,
                validator.principal_id,
                admission.principal_id,
            )
            bridge_keys = (
                database.key_id,
                execution.key_id,
                validator.key_id,
                admission.key_id,
            )
            bridge_policies = (
                database.policy_sha256,
                execution.policy_sha256,
                validator.policy_sha256,
                admission.policy_sha256,
            )
            reader_keys = (
                reader.pricing_authority_pin.key_id,
                reader.source_budget_authority_pin.key_id,
                reader.qualification_authority_pin.key_id,
                reader.terminal_verification_authority_pin.key_id,
                reader.runtime_control_authority_pin.key_id,
                *(item.manifest.node_signing_key_id for item in reader.node_authorities),
                *(item.enrollment_authority_pin.key_id for item in reader.node_authorities),
                *(
                    item.assignment_transport_pin.transport_key_id
                    for item in reader.node_authorities
                ),
            )
            reader_policies = (
                reader.pricing_authority_pin.policy_sha256,
                reader.source_budget_authority_pin.policy_sha256,
                reader.qualification_authority_pin.policy_sha256,
                reader.terminal_verification_authority_pin.policy_sha256,
                reader.runtime_control_authority_pin.policy_sha256,
                *(item.manifest.sandbox_policy_sha256 for item in reader.node_authorities),
                *(item.enrollment_authority_pin.policy_sha256 for item in reader.node_authorities),
                *(
                    item.assignment_transport_pin.transport_policy_sha256
                    for item in reader.node_authorities
                ),
            )
            active_reader_pins = (
                reader.pricing_authority_pin,
                reader.source_budget_authority_pin,
                reader.qualification_authority_pin,
                reader.terminal_verification_authority_pin,
                reader.runtime_control_authority_pin,
                *(item.enrollment_authority_pin for item in reader.node_authorities),
                *(item.assignment_transport_pin for item in reader.node_authorities),
            )
            kernel_keys = tuple(self.kernel_reader.trust_root.commissioning_keys)
            kernel_principals = {item.principal_id for item in kernel_keys}
            kernel_key_ids = {item.key_id for item in kernel_keys}
            non_kernel_principals = (
                set(bridge_principals)
                | set(reader.authority_principal_ids)
                | {self.artifact_verification_authority.principal_id}
            )
            non_kernel_key_ids = (
                set(bridge_keys) | set(reader_keys) | {self.artifact_verification_authority.key_id}
            )
            nodes_active = all(
                item.manifest.key_valid_from
                <= self.prepared_at
                < min(
                    item.manifest.key_expires_at,
                    item.manifest.key_revoked_at or item.manifest.key_expires_at,
                )
                for item in reader.node_authorities
            )
            if (
                binding.role is not ControllerStepAuthorityRole.INDEPENDENT_ADMISSION
                or not binding.externally_deployed
                or (binding.principal_id, binding.key_id, binding.policy_sha256)
                != (admission.principal_id, admission.key_id, admission.policy_sha256)
                or binding.service_manifest_sha256 == self.controller_manifest_sha256
                or binding.service_manifest_sha256
                == self.validation_archive.validator_manifest_sha256
                or binding.service_manifest_sha256
                in {item.manifest.manifest_sha256 for item in reader.node_authorities}
                or execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
                or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
                or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
            ):
                raise ValueError("independent admission authority binding is not closed")
            if (
                self.admission_signing_key.key_id != admission.key_id
                or reader.prepared_at != self.prepared_at
                or not all(
                    pin.active_at(self.prepared_at)
                    for pin in (database, execution, validator, admission)
                )
                or not all(pin.active_at(self.prepared_at) for pin in active_reader_pins)
                or not nodes_active
                or len(set(bridge_principals)) != len(bridge_principals)
                or len(set(bridge_keys)) != len(bridge_keys)
                or len(set(bridge_policies)) != len(bridge_policies)
                or set(bridge_principals) & set(reader.authority_principal_ids)
                or set(bridge_keys) & set(reader_keys)
                or set(bridge_policies) & set(reader_policies)
                or non_kernel_principals & kernel_principals
                or non_kernel_key_ids & kernel_key_ids
                or self.artifact_verification_authority.principal_id
                != reader.artifact_verifier_principal_id
                or self.artifact_verification_authority.principal_id in set(bridge_principals)
                or self.artifact_verification_authority.key_id
                in set(bridge_keys) | set(reader_keys)
                or self.artifact_verification_authority.policy_sha256
                in set(bridge_policies) | set(reader_policies)
                or self.worker_process_principal_id
                in set(bridge_principals) | set(reader.authority_principal_ids)
                or not source.is_absolute()
                or self.service_implementation_source_path != os.path.normpath(source)
            ):
                raise ValueError("independent admission authority is not isolated")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate independent admission config keys: {duplicates}")
        return dict(pairs)

    def fresh_regular_bytes(
        path: Path,
        *,
        expected_sha256: str,
        label: str,
        expected_size: int | None = None,
        expected_owner: tuple[int, int] | None = None,
        expected_mode: int | None = None,
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
                    or not 0 < before.st_size <= 4 * 1024 * 1024
                    or (expected_size is not None and before.st_size != expected_size)
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

    def exact_directory(path: Path, *, label: str, owner, device, inode, mode) -> None:
        try:
            if path.resolve(strict=True) != path or path.is_symlink():
                raise ValueError(f"{label} traverses a symlink")
            metadata = path.lstat()
        except OSError as exc:
            raise ValueError(f"{label} is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid) != owner
            or metadata.st_dev != device
            or metadata.st_ino != inode
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise ValueError(f"{label} differs from its custody pin")

    def database_time() -> datetime:
        with session_factory()() as session:
            observed = session.scalar(select(func.clock_timestamp()))
        if (
            not isinstance(observed, datetime)
            or observed.tzinfo is None
            or observed.utcoffset() != timedelta(0)
        ):
            raise ValueError("PostgreSQL did not provide UTC admission decision time")
        return observed

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = IndependentAdmissionRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("independent admission RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("independent admission RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    reader = config.qualification_reader
    admission = config.admission_authority_pin
    kernel_keys = tuple(config.kernel_reader.trust_root.commissioning_keys)
    domain_public_keys = {
        config.database_authority_pin.public_key_ed25519_hex,
        config.execution_authority_pin.public_key_ed25519_hex,
        config.validator_authority_pin.public_key_ed25519_hex,
        admission.public_key_ed25519_hex,
        reader.pricing_authority_pin.public_key_ed25519_hex,
        reader.source_budget_authority_pin.public_key_ed25519_hex,
        reader.qualification_authority_pin.public_key_ed25519_hex,
        reader.terminal_verification_authority_pin.public_key_ed25519_hex,
        reader.runtime_control_authority_pin.public_key_ed25519_hex,
        *(item.manifest.node_signing_public_key_ed25519_hex for item in reader.node_authorities),
        *(item.enrollment_authority_pin.public_key_ed25519_hex for item in reader.node_authorities),
        *(item.assignment_transport_pin.public_key_x25519_hex for item in reader.node_authorities),
        *(item.public_key_ed25519_hex for item in kernel_keys),
    }
    domain_key_ids = {
        config.database_authority_pin.key_id,
        config.execution_authority_pin.key_id,
        config.validator_authority_pin.key_id,
        admission.key_id,
        reader.pricing_authority_pin.key_id,
        reader.source_budget_authority_pin.key_id,
        reader.qualification_authority_pin.key_id,
        reader.terminal_verification_authority_pin.key_id,
        reader.runtime_control_authority_pin.key_id,
        config.artifact_verification_authority.key_id,
        *(item.manifest.node_signing_key_id for item in reader.node_authorities),
        *(item.enrollment_authority_pin.key_id for item in reader.node_authorities),
        *(item.assignment_transport_pin.transport_key_id for item in reader.node_authorities),
        *(item.key_id for item in kernel_keys),
    }
    domain_principals = set(reader.authority_principal_ids) | {
        config.database_authority_pin.principal_id,
        config.execution_authority_pin.principal_id,
        config.validator_authority_pin.principal_id,
        admission.principal_id,
        *(item.principal_id for item in kernel_keys),
    }
    domain_policies = {
        config.database_authority_pin.policy_sha256,
        config.execution_authority_pin.policy_sha256,
        config.validator_authority_pin.policy_sha256,
        admission.policy_sha256,
        config.artifact_verification_authority.policy_sha256,
        reader.pricing_authority_pin.policy_sha256,
        reader.source_budget_authority_pin.policy_sha256,
        reader.qualification_authority_pin.policy_sha256,
        reader.terminal_verification_authority_pin.policy_sha256,
        reader.runtime_control_authority_pin.policy_sha256,
        *(item.manifest.sandbox_policy_sha256 for item in reader.node_authorities),
        *(item.enrollment_authority_pin.policy_sha256 for item in reader.node_authorities),
        *(
            item.assignment_transport_pin.transport_policy_sha256
            for item in reader.node_authorities
        ),
    }
    key_pin = config.admission_signing_key
    if (
        pin.operations != (ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,)
        or pin.authority_binding_sha256s != (binding.binding_sha256,)
        or (pin.service_principal_id, pin.service_manifest_sha256, pin.service_policy_sha256)
        != (binding.principal_id, binding.service_manifest_sha256, binding.policy_sha256)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
        or config.worker_process_principal_id in domain_principals
        or admission.principal_id in {item.principal_id for item in kernel_keys}
        or admission.key_id in {item.key_id for item in kernel_keys}
        or pin.receipt_key_id in domain_key_ids
        or pin.receipt_public_key_ed25519_hex in domain_public_keys
        or key_pin.file_sha256 == deployment.receipt_private_key_sha256
        or key_pin.owner_uid != deployment.process_uid
        or key_pin.group_gid != deployment.process_gid
        or pin.service_policy_sha256 not in domain_policies
    ):
        raise ValueError("independent admission config differs from deployment or authority")

    reviewed_root = Path(deployment.reviewed_code_root)
    service_path = Path(config.service_implementation_source_path)
    key_path = Path(key_pin.path)
    receipt_key_path = Path(deployment.receipt_private_key_path)
    cas_path = Path(config.kernel_reader.cas_root)
    archive_path = Path(config.validation_archive.root)
    expected_service_path = Path(admission_module.__file__).resolve(strict=True)
    try:
        service_path.relative_to(reviewed_root)
    except ValueError as exc:
        raise ValueError("independent admission implementation escaped reviewed source") from exc
    if service_path != expected_service_path:
        raise ValueError("independent admission implementation resolved another module")
    custody_roots = (
        Path(reader.artifact_store_root),
        Path(reader.authority_registry_root),
        cas_path,
        archive_path,
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        receipt_key_path.parent,
        key_path.parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("independent admission custody roots overlap")

    before_service = fresh_regular_bytes(
        service_path,
        expected_sha256=config.service_implementation_source_sha256,
        label="independent admission service implementation",
    )
    private_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.group_gid),
        expected_mode=key_pin.file_mode,
        label="independent admission signing key",
    )
    public_hex = (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    if (
        public_hex != admission.public_key_ed25519_hex
        or scientific_bridge_key_id(public_hex) != key_pin.key_id
    ):
        raise ValueError("independent admission private key differs from its public pin")

    exact_directory(
        cas_path,
        label="independent admission Kernel CAS",
        owner=(config.kernel_reader.cas_owner_uid, config.kernel_reader.cas_group_gid),
        device=config.kernel_reader.cas_device_id,
        inode=config.kernel_reader.cas_inode,
        mode=config.kernel_reader.cas_directory_mode,
    )
    exact_directory(
        archive_path,
        label="independent admission F9-v2 archive",
        owner=(config.validation_archive.owner_uid, config.validation_archive.group_gid),
        device=config.validation_archive.device_id,
        inode=config.validation_archive.inode,
        mode=config.validation_archive.directory_mode,
    )

    kernel_store = ResearchKernelStore(
        trust_root=config.kernel_reader.trust_root,
        archive=FilesystemResearchArchive(
            cas_path,
            max_object_bytes=config.kernel_reader.max_object_bytes,
            read_only=True,
        ),
    )
    action_authority = PostgreSQLResearchActionAuthorityAdapter(kernel_store)
    lineage = compose_qualification_run_lineage_reader(reader)
    artifact_store = LocalArtifactStore(
        Path(reader.artifact_store_root),
        verifier_principal_id=reader.artifact_verifier_principal_id,
        object_store_id=reader.artifact_object_store_id,
        max_object_bytes=reader.artifact_max_object_bytes,
        read_only=True,
    )
    raw_run_custody = PostgreSQLRawRunCustodyVerificationAdapter(
        execution_lineage=lineage,
        artifact_store=artifact_store,
        sea_sessions=session_factory(),
        allocator_authority=VerifiedExecutionAuthorityProjection(
            principal_id=reader.pricing_authority_pin.principal_id,
            key_id=reader.pricing_authority_pin.key_id,
            policy_sha256=reader.pricing_authority_pin.policy_sha256,
        ),
        artifact_authority=config.artifact_verification_authority,
    )
    validation_archive = WriteOnceF9V2ValidationCampaignArchive(
        archive_path,
        validator_manifest_sha256=config.validation_archive.validator_manifest_sha256,
        validator_authority_pin=config.validator_authority_pin,
        read_only=True,
    )
    service = IndependentObservationAdmissionService(
        verification=IndependentAdmissionVerificationContext(
            qualification_authority=QualificationAuthorityVerifier(
                reader.qualification_authority_pin
            ),
            action_authority=action_authority,
            qualification_custody=raw_run_custody,
            raw_run_custody=raw_run_custody,
            validation_campaign_custody=validation_archive,
            execution_authority_pin=config.execution_authority_pin,
            validator_authority_pin=config.validator_authority_pin,
            admission_authority_pin=admission,
            database_authority_pin=config.database_authority_pin,
        ),
        admission_private_key=private_key,
        clock=database_time,
    )

    def issue_admission_decision(payload):
        if type(payload) is not AdmissionDecisionIssuanceRPCPayload:
            raise TypeError("independent admission RPC handler received another payload type")
        return service.issue_admission_decision(
            committed_validation=payload.committed_validation,
            issuance_challenge=payload.issuance_challenge,
        )

    after_service = fresh_regular_bytes(
        service_path,
        expected_sha256=config.service_implementation_source_sha256,
        label="independent admission service implementation",
    )
    after_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.group_gid),
        expected_mode=key_pin.file_mode,
        label="independent admission signing key",
    )
    if before_service != after_service or private_key != after_key:
        raise ValueError("independent admission implementation or signing key changed")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,
                handler=issue_admission_decision,
            ),
        ),
    )


__all__ = ["build_independent_admission_rpc_service"]
