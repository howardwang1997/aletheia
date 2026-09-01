"""Guarded-loader factory for atomic observation admission and Kernel incorporation."""

from __future__ import annotations


def build_atomic_admission_rpc_service(*, deployment, configuration_bytes):
    """Compose exactly ``COMMIT_AND_INCORPORATE`` with DB and ordinary Kernel keys."""

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
    from aletheia.db import expected_schema_revision, session_factory
    from aletheia.execution.artifact_store import LocalArtifactStore
    from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
    from aletheia.execution.terminal_runtime import (
        QualificationTerminalReaderConfig,
        compose_qualification_run_lineage_reader,
    )
    from aletheia.observations import coordinator as coordinator_module
    from aletheia.observations import kernel_authority as kernel_authority_module
    from aletheia.observations.adapters import (
        PostgreSQLRawRunCustodyVerificationAdapter,
        PostgreSQLResearchActionAuthorityAdapter,
    )
    from aletheia.observations.coordinator import (
        ObservationAdmissionVerificationContext,
        PostgreSQLAtomicObservationAdmissionCoordinator,
    )
    from aletheia.observations.f9_v2_validation import (
        WriteOnceF9V2ValidationCampaignArchive,
    )
    from aletheia.observations.kernel_authority import (
        ExactObservationKernelAuthority,
        ObservationKernelPolicyAssignment,
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
        AdmissionCommitRPCPayload,
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_kernel.policy import (
        ResearchAuthorizationRole,
        ResearchAuthorizationTrustRootV1,
        verify_research_authorization_policy,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class DomainSigningKeyPin(BaseModel):
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
                raise ValueError("atomic admission signing key path is not canonical")
            return self

    class WritableResearchKernelConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        trust_root: ResearchAuthorizationTrustRootV1
        cas_root: str
        cas_owner_uid: int = Field(ge=0, le=2**31 - 1)
        cas_group_gid: int = Field(ge=0, le=2**31 - 1)
        cas_device_id: int = Field(ge=0)
        cas_inode: int = Field(gt=0)
        cas_directory_mode: Literal[0o700, 0o750] = 0o700
        max_object_bytes: int = Field(ge=1, le=1024**3)
        read_only: Literal[False] = False
        snapshot_archive_write_allowed: Literal[True] = True
        arbitrary_object_admission_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _root_is_canonical(self):
            candidate = Path(self.cas_root)
            if (
                not candidate.is_absolute()
                or self.cas_root != os.path.normpath(candidate)
                or self.cas_root == "/"
            ):
                raise ValueError("atomic admission Kernel CAS root is not canonical")
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
                raise ValueError("atomic admission validation archive root is not closed")
            return self

    class AtomicAdmissionRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.atomic_admission_rpc_service_config"] = (
            "aletheia.atomic_admission_rpc_service_config"
        )
        schema_version: Literal[1] = 1
        controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
        controller_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        worker_process_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
        service_id: str = Field(pattern=r"^rpcs_[0-9a-f]{32}$")
        service_pin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        database_url_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
        kernel: WritableResearchKernelConfig
        kernel_policy_assignments: tuple[ObservationKernelPolicyAssignment, ...] = Field(
            min_length=1,
            max_length=1,
        )
        authority_bindings: tuple[ControllerStepAuthorityBinding, ...] = Field(
            min_length=3,
            max_length=3,
        )
        database_authority_pin: ObservationDatabaseAuthorityPin
        execution_authority_pin: ScientificBridgeAuthorityPin
        validator_authority_pin: ScientificBridgeAuthorityPin
        admission_authority_pin: ScientificBridgeAuthorityPin
        qualification_reader: QualificationTerminalReaderConfig
        artifact_verification_authority: VerifiedExecutionAuthorityProjection
        validation_archive: F9V2ValidationArchiveReadConfig
        database_signing_key: DomainSigningKeyPin
        kernel_signing_key: DomainSigningKeyPin
        coordinator_source_path: str
        coordinator_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        kernel_authority_source_path: str
        kernel_authority_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        database_signing_key_loaded: Literal[True] = True
        kernel_signing_key_loaded: Literal[True] = True
        admission_signing_key_loaded: Literal[False] = False
        execution_signing_key_loaded: Literal[False] = False
        validator_signing_key_loaded: Literal[False] = False
        admission_row_and_kernel_commit_atomic: Literal[True] = True
        independent_decision_required: Literal[True] = True
        arbitrary_kernel_event_allowed: Literal[False] = False
        campaign_publication_allowed: Literal[False] = False
        execution_mutation_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_atomic_and_closed(self):
            bindings = self.authority_bindings
            if bindings != tuple(sorted(bindings, key=lambda item: item.binding_sha256)):
                raise ValueError("atomic admission authority bindings are not canonical")
            by_role = {item.role: item for item in bindings}
            if set(by_role) != {
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                ControllerStepAuthorityRole.KERNEL_COMMAND,
            } or not all(item.externally_deployed for item in bindings):
                raise ValueError("atomic admission authority partition is not exhaustive")
            database = self.database_authority_pin
            execution = self.execution_authority_pin
            validator = self.validator_authority_pin
            admission = self.admission_authority_pin
            database_binding = by_role[ControllerStepAuthorityRole.DATABASE_ATTESTATION]
            admission_binding = by_role[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION]
            kernel_binding = by_role[ControllerStepAuthorityRole.KERNEL_COMMAND]
            if (
                (
                    database_binding.principal_id,
                    database_binding.key_id,
                    database_binding.policy_sha256,
                )
                != (database.principal_id, database.key_id, database.policy_sha256)
                or (
                    admission_binding.principal_id,
                    admission_binding.key_id,
                    admission_binding.policy_sha256,
                )
                != (admission.principal_id, admission.key_id, admission.policy_sha256)
                or execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
                or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
                or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
                or self.database_signing_key.key_id != database.key_id
                or self.kernel_signing_key.key_id != kernel_binding.key_id
            ):
                raise ValueError("atomic admission authority binding changed identity")
            assignments = self.kernel_policy_assignments
            if assignments != tuple(sorted(assignments, key=lambda item: item.quest_id)) or len(
                {item.quest_id for item in assignments}
            ) != len(assignments):
                raise ValueError("atomic admission Kernel assignments are not canonical")
            kernel_public_keys = set()
            kernel_principals = set()
            for assignment in assignments:
                verify_research_authorization_policy(
                    policy=assignment.authorization_policy,
                    trust_root=self.kernel.trust_root,
                )
                key = assignment.authorization_policy.key(self.kernel_signing_key.key_id)
                if key.role is not ResearchAuthorizationRole.ORDINARY or not key.active_at(
                    self.prepared_at
                ):
                    raise ValueError("atomic admission Kernel assignment lacks a live ordinary key")
                kernel_public_keys.add(key.public_key_ed25519_hex)
                kernel_principals.add(key.principal_id)
            if (
                kernel_principals != {kernel_binding.principal_id}
                or len(kernel_public_keys) != 1
                or kernel_binding.policy_sha256 != assignments[0].authorization_policy.policy_sha256
                or not all(
                    self.kernel.trust_root.frozen_at
                    <= item.authorization_policy.frozen_at
                    <= item.authorization_policy.certified_at
                    <= self.prepared_at
                    for item in assignments
                )
            ):
                raise ValueError("atomic admission Kernel assignments changed signer or time")
            reader = self.qualification_reader
            bridge_principals = (
                database.principal_id,
                execution.principal_id,
                validator.principal_id,
                admission.principal_id,
            )
            bridge_keys = (database.key_id, execution.key_id, validator.key_id, admission.key_id)
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
            trust_keys = tuple(self.kernel.trust_root.commissioning_keys)
            trust_principals = {item.principal_id for item in trust_keys}
            trust_key_ids = {item.key_id for item in trust_keys}
            non_kernel_operational_principals = (
                set(bridge_principals)
                | set(reader.authority_principal_ids)
                | {self.artifact_verification_authority.principal_id}
            )
            non_kernel_operational_key_ids = (
                set(bridge_keys) | set(reader_keys) | {self.artifact_verification_authority.key_id}
            )
            all_operational_principals = non_kernel_operational_principals | {
                kernel_binding.principal_id
            }
            all_operational_key_ids = non_kernel_operational_key_ids | {kernel_binding.key_id}
            manifests = {
                self.controller_manifest_sha256,
                self.validation_archive.validator_manifest_sha256,
                *(item.service_manifest_sha256 for item in bindings),
                *(item.manifest.manifest_sha256 for item in reader.node_authorities),
            }
            source_paths = (
                Path(self.coordinator_source_path),
                Path(self.kernel_authority_source_path),
            )
            if (
                reader.prepared_at != self.prepared_at
                or not all(
                    pin.active_at(self.prepared_at)
                    for pin in (database, execution, validator, admission)
                )
                or not all(pin.active_at(self.prepared_at) for pin in active_reader_pins)
                or not all(
                    item.manifest.key_valid_from
                    <= self.prepared_at
                    < min(
                        item.manifest.key_expires_at,
                        item.manifest.key_revoked_at or item.manifest.key_expires_at,
                    )
                    for item in reader.node_authorities
                )
                or len(set(bridge_principals)) != len(bridge_principals)
                or len(set(bridge_keys)) != len(bridge_keys)
                or len(set(bridge_policies)) != len(bridge_policies)
                or set(bridge_principals) & set(reader.authority_principal_ids)
                or set(bridge_keys) & set(reader_keys)
                or set(bridge_policies) & set(reader_policies)
                or kernel_binding.policy_sha256 in set(bridge_policies) | set(reader_policies)
                or kernel_binding.principal_id in non_kernel_operational_principals
                or kernel_binding.key_id in non_kernel_operational_key_ids
                or all_operational_principals & trust_principals
                or all_operational_key_ids & trust_key_ids
                or self.artifact_verification_authority.principal_id
                != reader.artifact_verifier_principal_id
                or self.worker_process_principal_id in all_operational_principals | trust_principals
                or any(
                    not path.is_absolute() or str(path) != os.path.normpath(path)
                    for path in source_paths
                )
                or self.database_signing_key.path == self.kernel_signing_key.path
                or self.database_signing_key.file_sha256 == self.kernel_signing_key.file_sha256
                or len(manifests) != 2 + len(bindings) + len(reader.node_authorities)
            ):
                raise ValueError("atomic admission authority or source custody is not isolated")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate atomic admission config keys: {duplicates}")
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

    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=unique_object)
        config = AtomicAdmissionRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("atomic admission RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("atomic admission RPC config is not canonical JSON")

    pin = deployment.service_pin
    bindings = config.authority_bindings
    by_role = {item.role: item for item in bindings}
    reader = config.qualification_reader
    database = config.database_authority_pin
    kernel_binding = by_role[ControllerStepAuthorityRole.KERNEL_COMMAND]
    policy_keys = tuple(
        item.authorization_policy.key(config.kernel_signing_key.key_id)
        for item in config.kernel_policy_assignments
    )
    domain_public_keys = {
        database.public_key_ed25519_hex,
        config.execution_authority_pin.public_key_ed25519_hex,
        config.validator_authority_pin.public_key_ed25519_hex,
        config.admission_authority_pin.public_key_ed25519_hex,
        *(item.public_key_ed25519_hex for item in policy_keys),
        reader.pricing_authority_pin.public_key_ed25519_hex,
        reader.source_budget_authority_pin.public_key_ed25519_hex,
        reader.qualification_authority_pin.public_key_ed25519_hex,
        reader.terminal_verification_authority_pin.public_key_ed25519_hex,
        reader.runtime_control_authority_pin.public_key_ed25519_hex,
        *(item.manifest.node_signing_public_key_ed25519_hex for item in reader.node_authorities),
        *(item.enrollment_authority_pin.public_key_ed25519_hex for item in reader.node_authorities),
        *(item.assignment_transport_pin.public_key_x25519_hex for item in reader.node_authorities),
        *(item.public_key_ed25519_hex for item in config.kernel.trust_root.commissioning_keys),
    }
    domain_key_ids = {
        database.key_id,
        config.execution_authority_pin.key_id,
        config.validator_authority_pin.key_id,
        config.admission_authority_pin.key_id,
        kernel_binding.key_id,
        reader.pricing_authority_pin.key_id,
        reader.source_budget_authority_pin.key_id,
        reader.qualification_authority_pin.key_id,
        reader.terminal_verification_authority_pin.key_id,
        reader.runtime_control_authority_pin.key_id,
        config.artifact_verification_authority.key_id,
        *(item.manifest.node_signing_key_id for item in reader.node_authorities),
        *(item.enrollment_authority_pin.key_id for item in reader.node_authorities),
        *(item.assignment_transport_pin.transport_key_id for item in reader.node_authorities),
        *(item.key_id for item in config.kernel.trust_root.commissioning_keys),
    }
    domain_principals = set(reader.authority_principal_ids) | {
        database.principal_id,
        config.execution_authority_pin.principal_id,
        config.validator_authority_pin.principal_id,
        config.admission_authority_pin.principal_id,
        config.artifact_verification_authority.principal_id,
        kernel_binding.principal_id,
        *(item.principal_id for item in config.kernel.trust_root.commissioning_keys),
    }
    domain_policies = {
        database.policy_sha256,
        config.execution_authority_pin.policy_sha256,
        config.validator_authority_pin.policy_sha256,
        config.admission_authority_pin.policy_sha256,
        kernel_binding.policy_sha256,
        reader.pricing_authority_pin.policy_sha256,
        reader.source_budget_authority_pin.policy_sha256,
        reader.qualification_authority_pin.policy_sha256,
        reader.terminal_verification_authority_pin.policy_sha256,
        reader.runtime_control_authority_pin.policy_sha256,
        config.artifact_verification_authority.policy_sha256,
        *(item.manifest.sandbox_policy_sha256 for item in reader.node_authorities),
        *(item.enrollment_authority_pin.policy_sha256 for item in reader.node_authorities),
        *(
            item.assignment_transport_pin.transport_policy_sha256
            for item in reader.node_authorities
        ),
    }
    if (
        pin.operations != (ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,)
        or pin.authority_binding_sha256s != tuple(item.binding_sha256 for item in bindings)
        or config.controller_id != deployment.controller_id
        or config.controller_manifest_sha256 != deployment.controller_manifest_sha256
        or config.worker_process_principal_id != deployment.worker_process_principal_id
        or config.service_id != pin.service_id
        or config.service_pin_sha256 != pin.pin_sha256
        or config.prepared_at != deployment.prepared_at
        or config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
        or pin.service_principal_id in domain_principals
        or pin.service_principal_id == config.worker_process_principal_id
        or pin.service_policy_sha256 in domain_policies
        or pin.service_manifest_sha256
        in {
            config.controller_manifest_sha256,
            config.validation_archive.validator_manifest_sha256,
            *(item.service_manifest_sha256 for item in bindings),
            *(item.manifest.manifest_sha256 for item in reader.node_authorities),
        }
        or pin.receipt_key_id in domain_key_ids
        or pin.receipt_public_key_ed25519_hex in domain_public_keys
        or config.database_signing_key.file_sha256 == deployment.receipt_private_key_sha256
        or config.kernel_signing_key.file_sha256 == deployment.receipt_private_key_sha256
        or config.database_signing_key.owner_uid != deployment.process_uid
        or config.database_signing_key.group_gid != deployment.process_gid
        or config.kernel_signing_key.owner_uid != deployment.process_uid
        or config.kernel_signing_key.group_gid != deployment.process_gid
        or config.kernel.cas_owner_uid != deployment.process_uid
        or config.kernel.cas_group_gid != deployment.process_gid
    ):
        raise ValueError("atomic admission config differs from deployment or authority")

    reviewed_root = Path(deployment.reviewed_code_root)
    coordinator_path = Path(config.coordinator_source_path)
    authority_path = Path(config.kernel_authority_source_path)
    database_key_path = Path(config.database_signing_key.path)
    kernel_key_path = Path(config.kernel_signing_key.path)
    receipt_key_path = Path(deployment.receipt_private_key_path)
    cas_path = Path(config.kernel.cas_root)
    validation_archive_path = Path(config.validation_archive.root)
    expected_coordinator_path = Path(coordinator_module.__file__).resolve(strict=True)
    expected_authority_path = Path(kernel_authority_module.__file__).resolve(strict=True)
    for path, expected, label in (
        (coordinator_path, expected_coordinator_path, "atomic admission coordinator"),
        (authority_path, expected_authority_path, "observation Kernel authority"),
    ):
        try:
            path.relative_to(reviewed_root)
        except ValueError as exc:
            raise ValueError(f"{label} escaped reviewed source") from exc
        if path != expected:
            raise ValueError(f"{label} resolved another module")
    custody_roots = (
        Path(reader.artifact_store_root),
        Path(reader.authority_registry_root),
        cas_path,
        validation_archive_path,
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        receipt_key_path.parent,
        database_key_path.parent,
        kernel_key_path.parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("atomic admission custody roots overlap")

    before_coordinator = fresh_regular_bytes(
        coordinator_path,
        expected_sha256=config.coordinator_source_sha256,
        label="atomic admission coordinator implementation",
    )
    before_authority = fresh_regular_bytes(
        authority_path,
        expected_sha256=config.kernel_authority_source_sha256,
        label="observation Kernel authority implementation",
    )
    database_private_key = fresh_regular_bytes(
        database_key_path,
        expected_sha256=config.database_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.database_signing_key.owner_uid,
            config.database_signing_key.group_gid,
        ),
        expected_mode=config.database_signing_key.file_mode,
        label="atomic admission database signing key",
    )
    kernel_private_key = fresh_regular_bytes(
        kernel_key_path,
        expected_sha256=config.kernel_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.kernel_signing_key.owner_uid,
            config.kernel_signing_key.group_gid,
        ),
        expected_mode=config.kernel_signing_key.file_mode,
        label="atomic admission Kernel signing key",
    )
    database_public = (
        Ed25519PrivateKey.from_private_bytes(database_private_key)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        .hex()
    )
    kernel_public = (
        Ed25519PrivateKey.from_private_bytes(kernel_private_key)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        .hex()
    )
    if (
        database_public != database.public_key_ed25519_hex
        or scientific_bridge_key_id(database_public) != config.database_signing_key.key_id
        or kernel_public != policy_keys[0].public_key_ed25519_hex
        or any(item.public_key_ed25519_hex != kernel_public for item in policy_keys)
    ):
        raise ValueError("atomic admission private keys differ from their public assignments")

    exact_directory(
        cas_path,
        label="atomic admission writable Kernel CAS",
        owner=(config.kernel.cas_owner_uid, config.kernel.cas_group_gid),
        device=config.kernel.cas_device_id,
        inode=config.kernel.cas_inode,
        mode=config.kernel.cas_directory_mode,
    )
    exact_directory(
        validation_archive_path,
        label="atomic admission F9-v2 archive",
        owner=(config.validation_archive.owner_uid, config.validation_archive.group_gid),
        device=config.validation_archive.device_id,
        inode=config.validation_archive.inode,
        mode=config.validation_archive.directory_mode,
    )

    kernel_store = ResearchKernelStore(
        trust_root=config.kernel.trust_root,
        archive=FilesystemResearchArchive(
            cas_path,
            max_object_bytes=config.kernel.max_object_bytes,
            read_only=False,
            directory_mode=config.kernel.cas_directory_mode,
            object_mode=0o440 if config.kernel.cas_directory_mode == 0o750 else 0o400,
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
        validation_archive_path,
        validator_manifest_sha256=config.validation_archive.validator_manifest_sha256,
        validator_authority_pin=config.validator_authority_pin,
        read_only=True,
    )
    kernel_authority = ExactObservationKernelAuthority(
        trust_root=config.kernel.trust_root,
        assignments=config.kernel_policy_assignments,
        authorization_key_id=config.kernel_signing_key.key_id,
        private_key=kernel_private_key,
    )
    coordinator = PostgreSQLAtomicObservationAdmissionCoordinator(
        kernel_store=kernel_store,
        kernel_authority=kernel_authority,
        verification=ObservationAdmissionVerificationContext(
            qualification_authority=QualificationAuthorityVerifier(
                reader.qualification_authority_pin
            ),
            action_authority=action_authority,
            qualification_custody=raw_run_custody,
            raw_run_custody=raw_run_custody,
            validation_campaign_custody=validation_archive,
            execution_authority_pin=config.execution_authority_pin,
            validator_authority_pin=config.validator_authority_pin,
            admission_authority_pin=config.admission_authority_pin,
            database_authority_pin=database,
            database_private_key=database_private_key,
        ),
        controller_principal_id=config.worker_process_principal_id,
    )

    def commit_and_incorporate(payload):
        if type(payload) is not AdmissionCommitRPCPayload:
            raise TypeError("atomic admission RPC handler received another payload type")
        return coordinator.commit_and_incorporate(payload.decision)

    after_coordinator = fresh_regular_bytes(
        coordinator_path,
        expected_sha256=config.coordinator_source_sha256,
        label="atomic admission coordinator implementation",
    )
    after_authority = fresh_regular_bytes(
        authority_path,
        expected_sha256=config.kernel_authority_source_sha256,
        label="observation Kernel authority implementation",
    )
    after_database_key = fresh_regular_bytes(
        database_key_path,
        expected_sha256=config.database_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.database_signing_key.owner_uid,
            config.database_signing_key.group_gid,
        ),
        expected_mode=config.database_signing_key.file_mode,
        label="atomic admission database signing key",
    )
    after_kernel_key = fresh_regular_bytes(
        kernel_key_path,
        expected_sha256=config.kernel_signing_key.file_sha256,
        expected_size=32,
        expected_owner=(
            config.kernel_signing_key.owner_uid,
            config.kernel_signing_key.group_gid,
        ),
        expected_mode=config.kernel_signing_key.file_mode,
        label="atomic admission Kernel signing key",
    )
    if (
        before_coordinator != after_coordinator
        or before_authority != after_authority
        or database_private_key != after_database_key
        or kernel_private_key != after_kernel_key
    ):
        raise ValueError("atomic admission implementation or signing keys changed")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,
                handler=commit_and_incorporate,
            ),
        ),
    )


__all__ = ["build_atomic_admission_rpc_service"]
