"""Guarded-loader factory for the independent graph-scoped F9-v2 validator RPC service."""

from __future__ import annotations


def build_f9_v2_validation_rpc_service(*, deployment, configuration_bytes):
    """Compose the two validation operations with one isolated validator signing key."""

    import hashlib
    import json
    import os
    import stat
    from collections import Counter
    from datetime import datetime, timezone
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
    from aletheia.observations import f9_v2_assessor as assessor_module
    from aletheia.observations import f9_v2_validation as validation_module
    from aletheia.observations.adapters import (
        PostgreSQLRawRunCustodyVerificationAdapter,
        PostgreSQLResearchActionAuthorityAdapter,
    )
    from aletheia.observations.f9_v2_assessor import (
        ExactContentF9V2ObservationAssessor,
        FrozenF9V2ExactContentAssessmentCatalog,
    )
    from aletheia.observations.f9_v2_validation import (
        F9V2BridgeVerificationContext,
        F9V2IndependentValidationService,
        WriteOnceF9V2ValidationCampaignArchive,
    )
    from aletheia.observations.scientific_bridge import (
        ObservationDatabaseAuthorityPin,
        ScientificBridgeAuthorityPin,
        ScientificBridgeRole,
        VerifiedExecutionAuthorityProjection,
        scientific_bridge_key_id,
    )
    from aletheia.research_controller.external_rpc import (
        ControllerWorkerRPCOperation,
        ValidationCampaignResult,
    )
    from aletheia.research_controller.external_rpc_server import (
        ControllerWorkerRPCHandlerBinding,
        ControllerWorkerRPCHandlerSet,
        RawRunRPCPayload,
        ValidationReceiptIssuanceRPCPayload,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    class F9V2ValidatorSigningKeyPin(BaseModel):
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
                not candidate.is_absolute()
                or self.path != os.path.normpath(candidate)
                or self.path == "/"
            ):
                raise ValueError("F9-v2 validator signing key path is not canonical")
            return self

    class F9V2ValidationArchiveWriteConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        root: str
        owner_uid: int = Field(ge=1, le=2**31 - 1)
        group_gid: int = Field(ge=1, le=2**31 - 1)
        device_id: int = Field(ge=0)
        inode: int = Field(gt=0)
        directory_mode: Literal[0o700, 0o750] = 0o700
        validator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        read_only: Literal[False] = False
        campaign_publication_allowed: Literal[True] = True

        @model_validator(mode="after")
        def _root_is_canonical(self):
            candidate = Path(self.root)
            if (
                not candidate.is_absolute()
                or self.root != os.path.normpath(candidate)
                or self.root == "/"
            ):
                raise ValueError("F9-v2 validation archive write root is not canonical")
            return self

    class IndependentF9V2ValidationRPCConfig(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        schema_name: Literal["aletheia.independent_f9_v2_validation_rpc_service_config"] = (
            "aletheia.independent_f9_v2_validation_rpc_service_config"
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
        validator_authority_pin: ScientificBridgeAuthorityPin
        validator_signing_key: F9V2ValidatorSigningKeyPin
        execution_authority_pin: ScientificBridgeAuthorityPin
        admission_authority_pin: ScientificBridgeAuthorityPin
        database_authority_pin: ObservationDatabaseAuthorityPin
        qualification_reader: QualificationTerminalReaderConfig
        artifact_verification_authority: VerifiedExecutionAuthorityProjection
        validation_archive: F9V2ValidationArchiveWriteConfig
        assessment_catalog: FrozenF9V2ExactContentAssessmentCatalog
        assessor_implementation_source_path: str
        assessor_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        service_implementation_source_path: str
        service_implementation_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        prepared_at: AwareDatetime
        validator_signing_key_loaded: Literal[True] = True
        database_signing_key_loaded: Literal[False] = False
        admission_signing_key_loaded: Literal[False] = False
        execution_signing_key_loaded: Literal[False] = False
        kernel_signing_key_loaded: Literal[False] = False
        database_mutation_allowed: Literal[False] = False
        execution_mutation_allowed: Literal[False] = False
        artifact_mutation_allowed: Literal[False] = False
        campaign_publication_allowed: Literal[True] = True
        direct_observation_admission_allowed: Literal[False] = False
        direct_kernel_mutation_allowed: Literal[False] = False
        generic_model_callback_allowed: Literal[False] = False

        @model_validator(mode="after")
        def _authority_is_closed(self):
            binding = self.authority_binding
            validator = self.validator_authority_pin
            execution = self.execution_authority_pin
            admission = self.admission_authority_pin
            database = self.database_authority_pin
            reader = self.qualification_reader
            assessor_source = Path(self.assessor_implementation_source_path)
            service_source = Path(self.service_implementation_source_path)
            bridge_principals = (
                validator.principal_id,
                execution.principal_id,
                admission.principal_id,
                database.principal_id,
            )
            bridge_keys = (
                validator.key_id,
                execution.key_id,
                admission.key_id,
                database.key_id,
            )
            bridge_policies = (
                validator.policy_sha256,
                execution.policy_sha256,
                admission.policy_sha256,
                database.policy_sha256,
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
            nodes_active = all(
                item.manifest.key_valid_from
                <= self.prepared_at
                < min(
                    item.manifest.key_expires_at,
                    item.manifest.key_revoked_at or item.manifest.key_expires_at,
                )
                for item in reader.node_authorities
            )
            template_manifests = {
                item.validator_manifest_sha256 for item in self.assessment_catalog.templates
            }
            if (
                binding.role is not ControllerStepAuthorityRole.INDEPENDENT_VALIDATION
                or not binding.externally_deployed
                or (binding.principal_id, binding.key_id, binding.policy_sha256)
                != (validator.principal_id, validator.key_id, validator.policy_sha256)
                or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
                or execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
                or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
                or self.validator_signing_key.key_id != validator.key_id
                or self.validation_archive.validator_manifest_sha256
                != binding.service_manifest_sha256
                or template_manifests != {binding.service_manifest_sha256}
                or self.assessment_catalog.assessor_implementation_sha256
                != self.assessor_implementation_source_sha256
                or reader.prepared_at != self.prepared_at
                or not all(
                    pin.active_at(self.prepared_at)
                    for pin in (validator, execution, admission, database)
                )
                or not all(pin.active_at(self.prepared_at) for pin in active_reader_pins)
                or not nodes_active
                or len(set(bridge_principals)) != len(bridge_principals)
                or len(set(bridge_keys)) != len(bridge_keys)
                or len(set(bridge_policies)) != len(bridge_policies)
                or set(bridge_principals) & set(reader.authority_principal_ids)
                or set(bridge_keys) & set(reader_keys)
                or set(bridge_policies) & set(reader_policies)
                or self.artifact_verification_authority.principal_id
                != reader.artifact_verifier_principal_id
                or self.artifact_verification_authority.principal_id in set(bridge_principals)
                or self.artifact_verification_authority.key_id
                in set(bridge_keys) | set(reader_keys)
                or self.artifact_verification_authority.policy_sha256
                in set(bridge_policies) | set(reader_policies)
                or self.worker_process_principal_id
                in set(bridge_principals) | set(reader.authority_principal_ids)
                or not assessor_source.is_absolute()
                or self.assessor_implementation_source_path != os.path.normpath(assessor_source)
                or not service_source.is_absolute()
                or self.service_implementation_source_path != os.path.normpath(service_source)
            ):
                raise ValueError("independent F9-v2 validation RPC authority is not closed")
            return self

    def unique_object(pairs):
        duplicates = sorted(
            key for key, count in Counter(key for key, _value in pairs).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate independent F9-v2 validation config keys: {duplicates}")
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
        config = IndependentF9V2ValidationRPCConfig.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("independent F9-v2 validation RPC config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise ValueError("independent F9-v2 validation RPC config is not canonical JSON")

    pin = deployment.service_pin
    binding = config.authority_binding
    validator = config.validator_authority_pin
    reader = config.qualification_reader
    expected_operations = tuple(
        sorted(
            (
                ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
                ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
            ),
            key=lambda item: item.value,
        )
    )
    kernel_keys = tuple(config.kernel_reader.trust_root.commissioning_keys)
    signed_public_keys = {
        validator.public_key_ed25519_hex,
        config.execution_authority_pin.public_key_ed25519_hex,
        config.admission_authority_pin.public_key_ed25519_hex,
        config.database_authority_pin.public_key_ed25519_hex,
        reader.pricing_authority_pin.public_key_ed25519_hex,
        reader.source_budget_authority_pin.public_key_ed25519_hex,
        reader.qualification_authority_pin.public_key_ed25519_hex,
        reader.terminal_verification_authority_pin.public_key_ed25519_hex,
        reader.runtime_control_authority_pin.public_key_ed25519_hex,
        *(item.manifest.node_signing_public_key_ed25519_hex for item in reader.node_authorities),
        *(item.enrollment_authority_pin.public_key_ed25519_hex for item in reader.node_authorities),
        *(item.public_key_ed25519_hex for item in kernel_keys),
    }
    all_key_ids = {
        validator.key_id,
        config.execution_authority_pin.key_id,
        config.admission_authority_pin.key_id,
        config.database_authority_pin.key_id,
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
    all_principals = set(reader.authority_principal_ids) | {
        validator.principal_id,
        config.execution_authority_pin.principal_id,
        config.admission_authority_pin.principal_id,
        config.database_authority_pin.principal_id,
        *(item.principal_id for item in kernel_keys),
    }
    key_pin = config.validator_signing_key
    if (
        pin.operations != expected_operations
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
        or config.worker_process_principal_id in all_principals
        or validator.principal_id in {item.principal_id for item in kernel_keys}
        or validator.key_id in {item.key_id for item in kernel_keys}
        or pin.receipt_key_id in all_key_ids
        or pin.receipt_public_key_ed25519_hex in signed_public_keys
        or key_pin.file_sha256 == deployment.receipt_private_key_sha256
        or key_pin.owner_uid != deployment.process_uid
        or key_pin.owner_gid != deployment.process_gid
        or config.validation_archive.owner_uid != deployment.process_uid
        or config.validation_archive.group_gid != deployment.process_gid
    ):
        raise ValueError("independent F9-v2 validation config differs from deployment or authority")

    reviewed_root = Path(deployment.reviewed_code_root)
    service_path = Path(config.service_implementation_source_path)
    assessor_path = Path(config.assessor_implementation_source_path)
    key_path = Path(key_pin.path)
    receipt_key_path = Path(deployment.receipt_private_key_path)
    cas_path = Path(config.kernel_reader.cas_root)
    validation_archive_path = Path(config.validation_archive.root)
    expected_service_path = Path(validation_module.__file__).resolve(strict=True)
    expected_assessor_path = Path(assessor_module.__file__).resolve(strict=True)
    for path, label in (
        (service_path, "F9-v2 validation service"),
        (assessor_path, "F9-v2 validation assessor"),
    ):
        try:
            path.relative_to(reviewed_root)
        except ValueError as exc:
            raise ValueError(f"{label} escaped reviewed source") from exc
    if service_path != expected_service_path or assessor_path != expected_assessor_path:
        raise ValueError("independent F9-v2 validation implementation resolved another module")
    custody_roots = (
        Path(reader.artifact_store_root),
        Path(reader.authority_registry_root),
        cas_path,
        validation_archive_path,
        reviewed_root,
        Path(deployment.socket_parent_path),
        Path(deployment.composition_config_path).parent,
        receipt_key_path.parent,
        key_path.parent,
    )
    for index, first in enumerate(custody_roots):
        for second in custody_roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("independent F9-v2 validation custody roots overlap")

    before_service = fresh_regular_bytes(
        service_path,
        expected_sha256=config.service_implementation_source_sha256,
        label="F9-v2 validation service implementation",
    )
    before_assessor = fresh_regular_bytes(
        assessor_path,
        expected_sha256=config.assessor_implementation_source_sha256,
        label="F9-v2 validation assessor implementation",
    )
    private_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.owner_gid),
        expected_mode=key_pin.file_mode,
        label="F9-v2 validator signing key",
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
        public_hex != validator.public_key_ed25519_hex
        or scientific_bridge_key_id(public_hex) != key_pin.key_id
    ):
        raise ValueError("F9-v2 validator private key differs from its public pin")

    exact_directory(
        cas_path,
        label="F9-v2 validator Kernel CAS",
        owner=(config.kernel_reader.cas_owner_uid, config.kernel_reader.cas_group_gid),
        device=config.kernel_reader.cas_device_id,
        inode=config.kernel_reader.cas_inode,
        mode=config.kernel_reader.cas_directory_mode,
    )
    exact_directory(
        validation_archive_path,
        label="F9-v2 validation campaign archive",
        owner=(config.validation_archive.owner_uid, config.validation_archive.group_gid),
        device=config.validation_archive.device_id,
        inode=config.validation_archive.inode,
        mode=config.validation_archive.directory_mode,
    )

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
    campaign_archive = WriteOnceF9V2ValidationCampaignArchive(
        validation_archive_path,
        validator_manifest_sha256=config.validation_archive.validator_manifest_sha256,
        validator_authority_pin=validator,
    )
    assessor = ExactContentF9V2ObservationAssessor(
        catalog=config.assessment_catalog,
        implementation_sha256=config.assessor_implementation_source_sha256,
    )
    service = F9V2IndependentValidationService(
        archive=campaign_archive,
        assessor=assessor,
        verification=F9V2BridgeVerificationContext(
            qualification_authority=QualificationAuthorityVerifier(
                reader.qualification_authority_pin
            ),
            action_authority=action_authority,
            qualification_custody=raw_run_custody,
            raw_run_custody=raw_run_custody,
            execution_authority_pin=config.execution_authority_pin,
            validator_authority_pin=validator,
            admission_authority_pin=config.admission_authority_pin,
            database_authority_pin=config.database_authority_pin,
        ),
        validator_private_key=private_key,
        clock=lambda: datetime.now(timezone.utc),
    )

    def prepare_validation_campaign(payload):
        if type(payload) is not RawRunRPCPayload:
            raise TypeError("F9-v2 validation RPC handler received another payload type")
        return ValidationCampaignResult(
            validation_campaign_sha256=service.prepare_validation_campaign(raw_run=payload.raw_run)
        )

    def issue_validation_receipt(payload):
        if type(payload) is not ValidationReceiptIssuanceRPCPayload:
            raise TypeError("F9-v2 validation RPC handler received another payload type")
        return service.issue_validation_receipt(
            raw_run=payload.raw_run,
            validation_campaign_sha256=payload.validation_campaign_sha256,
            issuance_challenge=payload.issuance_challenge,
        )

    after_service = fresh_regular_bytes(
        service_path,
        expected_sha256=config.service_implementation_source_sha256,
        label="F9-v2 validation service implementation",
    )
    after_assessor = fresh_regular_bytes(
        assessor_path,
        expected_sha256=config.assessor_implementation_source_sha256,
        label="F9-v2 validation assessor implementation",
    )
    final_key = fresh_regular_bytes(
        key_path,
        expected_sha256=key_pin.file_sha256,
        expected_size=32,
        expected_owner=(key_pin.owner_uid, key_pin.owner_gid),
        expected_mode=key_pin.file_mode,
        label="F9-v2 validator signing key",
    )
    if (
        before_service != after_service
        or before_assessor != after_assessor
        or private_key != final_key
    ):
        raise ValueError("F9-v2 validation implementation or signing key changed")
    return ControllerWorkerRPCHandlerSet(
        operations=pin.operations,
        bindings=(
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
                handler=issue_validation_receipt,
            ),
            ControllerWorkerRPCHandlerBinding(
                operation=ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
                handler=prepare_validation_campaign,
            ),
        ),
    )


__all__ = ["build_f9_v2_validation_rpc_service"]
