"""Byte-pinned, keyless Linux runtime for one given-protocol ARL-1 campaign.

The runtime is intentionally one-shot.  A deployment manifest pins the canonical request,
configuration, implementation bytes, public authority bindings, Unix RPC endpoints, PostgreSQL
schema, Research Kernel CAS, terminal lineage custody, and write-once evidence archive.  The
process can coordinate already-signed work, but cannot design research or load any authority key.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.arl1_campaign import (
    ARL1IndependentValidationCoordinator,
    ARL1PrimaryAdmissionCoordinator,
    ARL1ProtocolCampaignPending,
    ARL1ProtocolCampaignRequestV1,
    ARL1ProtocolCampaignRunReceiptV1,
    ARL1ProtocolCampaignService,
)
from aletheia.arl1_verifier import LocalARL1EvidenceArchive
from aletheia.config import get_settings
from aletheia.db import expected_schema_revision, session_factory
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.terminal_runtime import (
    QualificationTerminalReaderConfig,
    compose_qualification_run_lineage_reader,
)
from aletheia.observations.adapters import PostgreSQLRawRunCustodyVerificationAdapter
from aletheia.observations.scientific_bridge import VerifiedExecutionAuthorityProjection
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    ControllerWorkerRPCTransport,
    RPCAtomicObservationAdmission,
    RPCDatabaseObservationBridge,
    RPCIndependentObservationAdmission,
    RPCIndependentObservationValidator,
    RPCRawRunEnvelopeSource,
    RPCScientificExecutionRegistrar,
    RawRunEnvelopePending,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller.worker_composition import ResearchKernelReadOnlyConfig
from aletheia.research_kernel.schemas import KernelModel, canonical_json_bytes, canonical_sha256
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore
from aletheia.schema_migrations import require_schema_exact

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_MAX_PINNED_FILE_BYTES = 16 * 1024 * 1024


class ARL1RuntimeError(RuntimeError):
    """A deployment, host, source, authority, or runtime result failed closed."""


class _PendingAwareRawRunSource:
    """Translate only a signed terminal-pending source result into campaign flow control."""

    def __init__(self, source: RPCRawRunEnvelopeSource) -> None:
        self._source = source

    def load_raw_run(self, *, quest_id: str, action_sha256: str, scientific_slot_id: str):
        try:
            return self._source.load_raw_run(
                quest_id=quest_id,
                action_sha256=action_sha256,
                scientific_slot_id=scientific_slot_id,
            )
        except RawRunEnvelopePending as exc:
            raise ARL1ProtocolCampaignPending(
                scientific_slot_id=scientific_slot_id,
                pending_code=exc.pending_code,
                retry_after_milliseconds=exc.retry_after_milliseconds,
            ) from exc


def _operations(
    *values: ControllerWorkerRPCOperation,
) -> tuple[ControllerWorkerRPCOperation, ...]:
    return tuple(sorted(values, key=lambda item: item.value))


_SERVICE_OPERATIONS = {
    "execution_registration": _operations(ControllerWorkerRPCOperation.REGISTER_EXECUTION_CAMPAIGN),
    "raw_run_source": _operations(ControllerWorkerRPCOperation.LOAD_RAW_RUN),
    "database_observation": _operations(
        ControllerWorkerRPCOperation.COMMIT_VALIDATION,
        ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
    ),
    "independent_validation": _operations(
        ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
        ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
    ),
    "independent_admission": _operations(ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION),
    "atomic_admission": _operations(ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE),
}

_SERVICE_ROLES = {
    "execution_registration": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "raw_run_source": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "database_observation": (ControllerStepAuthorityRole.DATABASE_ATTESTATION,),
    "independent_validation": (ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,),
    "independent_admission": (ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,),
    "atomic_admission": tuple(
        sorted(
            (
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                ControllerStepAuthorityRole.KERNEL_COMMAND,
            ),
            key=lambda item: item.value,
        )
    ),
}

_PRIMARY_ROLES = {
    "database_observation": ControllerStepAuthorityRole.DATABASE_ATTESTATION,
    "independent_validation": ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    "independent_admission": ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
}

_REQUIRED_ROLES = tuple(
    sorted(
        {
            ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
            ControllerStepAuthorityRole.DATABASE_ATTESTATION,
            ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
            ControllerStepAuthorityRole.KERNEL_COMMAND,
        },
        key=lambda item: item.value,
    )
)


def _canonical_absolute_path(value: str, *, label: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not candidate.is_absolute()
        or str(candidate) != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be one canonical absolute non-root path")
    return candidate


class ARL1CampaignRPCServiceSetV1(KernelModel):
    schema_name: Literal["aletheia.arl1_campaign_rpc_service_set"] = (
        "aletheia.arl1_campaign_rpc_service_set"
    )
    schema_version: Literal[1] = 1
    execution_registration: ControllerWorkerRPCServicePin
    raw_run_source: ControllerWorkerRPCServicePin
    database_observation: ControllerWorkerRPCServicePin
    independent_validation: ControllerWorkerRPCServicePin
    independent_admission: ControllerWorkerRPCServicePin
    atomic_admission: ControllerWorkerRPCServicePin

    @property
    def named_pins(self) -> tuple[tuple[str, ControllerWorkerRPCServicePin], ...]:
        return tuple((name, getattr(self, name)) for name in _SERVICE_OPERATIONS)

    @model_validator(mode="after")
    def _ports_are_minimal_and_disjoint(self) -> "ARL1CampaignRPCServiceSetV1":
        for name, pin in self.named_pins:
            if pin.operations != _SERVICE_OPERATIONS[name]:
                raise ValueError(f"ARL-1 RPC service {name} has another operation set")
        pins = tuple(pin for _name, pin in self.named_pins)
        operations = tuple(operation for pin in pins for operation in pin.operations)
        if len(operations) != len(set(operations)):
            raise ValueError("ARL-1 RPC operations overlap between services")
        for label, values in (
            ("service ids", tuple(pin.service_id for pin in pins)),
            ("receipt keys", tuple(pin.receipt_key_id for pin in pins)),
            ("socket paths", tuple(pin.socket_path for pin in pins)),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"ARL-1 RPC {label} must be pairwise distinct")
        return self

    @property
    def service_set_sha256(self) -> str:
        return canonical_sha256(self)


class ARL1EvidenceArchiveRuntimeConfigV1(KernelModel):
    schema_name: Literal["aletheia.arl1_evidence_archive_runtime_config"] = (
        "aletheia.arl1_evidence_archive_runtime_config"
    )
    schema_version: Literal[1] = 1
    root: str
    owner_uid: int = Field(ge=0)
    group_gid: int = Field(ge=0)
    device_id: int = Field(ge=0)
    inode: int = Field(gt=0)
    directory_mode: Literal[0o700, 0o750]
    object_mode: Literal[0o400, 0o440]
    max_object_bytes: int = Field(ge=1, le=64 * 1024**2)
    write_once: Literal[True] = True
    deletion_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _archive_is_traversable_when_shared(self) -> "ARL1EvidenceArchiveRuntimeConfigV1":
        _canonical_absolute_path(self.root, label="ARL-1 evidence archive root")
        if self.object_mode == 0o440 and self.directory_mode != 0o750:
            raise ValueError("group-readable ARL-1 evidence requires 0750 archive directories")
        return self


class ARL1CampaignRuntimeConfigV1(KernelModel):
    """Public/keyless dependencies reachable by the campaign process."""

    schema_name: Literal["aletheia.arl1_campaign_runtime_config"] = (
        "aletheia.arl1_campaign_runtime_config"
    )
    schema_version: Literal[1] = 1
    configuration_id: str | None = Field(default=None, pattern=r"^arl1c_[0-9a-f]{32}$")
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    controller_id: str = Field(pattern=r"^rctl_[0-9a-f]{32}$")
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    authority_bindings: tuple[ControllerStepAuthorityBinding, ...] = Field(
        min_length=5,
        max_length=5,
    )
    rpc_services: ARL1CampaignRPCServiceSetV1
    kernel_reader: ResearchKernelReadOnlyConfig
    qualification_reader: QualificationTerminalReaderConfig
    allocator_authority: VerifiedExecutionAuthorityProjection
    artifact_authority: VerifiedExecutionAuthorityProjection
    evidence_archive: ARL1EvidenceArchiveRuntimeConfigV1
    campaign_implementation_source_path: str
    campaign_implementation_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    verifier_implementation_source_path: str
    verifier_implementation_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    private_signing_key_loaded: Literal[False] = False
    autonomous_research_design_allowed: Literal[False] = False
    generic_callback_allowed: Literal[False] = False
    direct_database_mutation_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authority_and_custody_are_closed(self) -> "ARL1CampaignRuntimeConfigV1":
        bindings = self.authority_bindings
        if tuple(item.role for item in bindings) != _REQUIRED_ROLES:
            raise ValueError("ARL-1 runtime authority bindings are not exhaustive and canonical")
        if not all(item.externally_deployed for item in bindings):
            raise ValueError("ARL-1 runtime requires externally deployed signed authorities")
        by_role = {item.role: item for item in bindings}
        if len(by_role) != len(bindings):
            raise ValueError("ARL-1 runtime repeats an authority role")
        services = self.rpc_services
        for name, pin in services.named_pins:
            expected = tuple(sorted(by_role[role].binding_sha256 for role in _SERVICE_ROLES[name]))
            if pin.authority_binding_sha256s != expected:
                raise ValueError(f"ARL-1 RPC service {name} changed its authority closure")
            if not pin.valid_from <= self.prepared_at < pin.expires_at:
                raise ValueError("ARL-1 RPC receipt key is not active at preparation")
            if (
                pin.peer_uid == self.process_uid
                or pin.peer_gid != self.process_gid
                or pin.socket_owner_uid != pin.peer_uid
                or pin.socket_group_gid != self.process_gid
                or pin.socket_mode != 0o660
            ):
                raise ValueError(
                    "ARL-1 RPC server peer must be UID-separated and share the campaign socket GID"
                )
            primary = _PRIMARY_ROLES.get(name)
            if primary is not None:
                binding = by_role[primary]
                if (
                    pin.service_principal_id != binding.principal_id
                    or pin.service_manifest_sha256 != binding.service_manifest_sha256
                    or pin.service_policy_sha256 != binding.policy_sha256
                ):
                    raise ValueError(f"ARL-1 RPC service {name} changed its primary authority")
        execution = by_role[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION]
        for name in ("execution_registration", "raw_run_source"):
            if getattr(services, name).service_principal_id == execution.principal_id:
                raise ValueError("ARL-1 read/registration service overlaps execution signer")
        authority_principals = {item.principal_id for item in bindings}
        terminal_principals = set(self.qualification_reader.authority_principal_ids)
        service_principals = {pin.service_principal_id for _name, pin in services.named_pins}
        kernel_principals = {
            item.principal_id for item in self.kernel_reader.trust_root.commissioning_keys
        }
        if (
            self.process_principal_id
            in authority_principals | terminal_principals | service_principals | kernel_principals
            or self.qualification_reader.prepared_at != self.prepared_at
            or self.allocator_authority.principal_id
            != self.qualification_reader.allocator_principal_id
            or self.artifact_authority.principal_id
            != self.qualification_reader.artifact_verifier_principal_id
        ):
            raise ValueError("ARL-1 process or custody authority separation differs")
        self.kernel_reader.require_effective_read_only(
            process_uid=self.process_uid,
            process_gid=self.process_gid,
        )
        campaign_source = _canonical_absolute_path(
            self.campaign_implementation_source_path,
            label="ARL-1 campaign implementation",
        )
        verifier_source = _canonical_absolute_path(
            self.verifier_implementation_source_path,
            label="ARL-1 verifier implementation",
        )
        roots = (
            Path(self.evidence_archive.root),
            Path(self.kernel_reader.cas_root),
            Path(self.qualification_reader.artifact_store_root),
            Path(self.qualification_reader.authority_registry_root),
        )
        if campaign_source == verifier_source:
            raise ValueError("ARL-1 campaign and verifier implementation pins must be distinct")
        for index, first in enumerate(roots):
            for second in roots[index + 1 :]:
                if first == second or first in second.parents or second in first.parents:
                    raise ValueError("ARL-1 runtime custody roots overlap")
        expected = f"arl1c_{self.configuration_sha256[:32]}"
        if self.configuration_id is not None and self.configuration_id != expected:
            raise ValueError("ARL-1 runtime configuration id differs from its contents")
        object.__setattr__(self, "configuration_id", expected)
        return self

    @property
    def configuration_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"configuration_id"}))


class ARL1CampaignRuntimeDeploymentV1(KernelModel):
    """Externally pinned one-shot invocation; creation alone does not execute work."""

    schema_name: Literal["aletheia.arl1_campaign_runtime_deployment"] = (
        "aletheia.arl1_campaign_runtime_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str | None = Field(default=None, pattern=r"^arl1d_[0-9a-f]{32}$")
    configuration_path: str
    configuration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_path: str
    request_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    prepared_at: AwareDatetime
    linux_required: Literal[True] = True
    explicit_apply_required: Literal[True] = True
    acknowledgement: Literal["RUN_ARL1_PROTOCOL_CAMPAIGN"] = "RUN_ARL1_PROTOCOL_CAMPAIGN"
    private_signing_key_loaded: Literal[False] = False
    autonomous_research_design_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_paths_are_distinct(self) -> "ARL1CampaignRuntimeDeploymentV1":
        config = _canonical_absolute_path(
            self.configuration_path,
            label="ARL-1 runtime configuration",
        )
        request = _canonical_absolute_path(
            self.request_path,
            label="ARL-1 campaign request",
        )
        if config == request:
            raise ValueError("ARL-1 runtime configuration and request must be distinct")
        expected = f"arl1d_{self.deployment_sha256[:32]}"
        if self.deployment_id is not None and self.deployment_id != expected:
            raise ValueError("ARL-1 runtime deployment id differs from its contents")
        object.__setattr__(self, "deployment_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"deployment_id"}))


def _fresh_pinned_bytes(
    path_value: str | Path,
    expected_sha256: str,
    *,
    label: str,
) -> bytes:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise ARL1RuntimeError(f"{label} traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or not 0 < before.st_size <= _MAX_PINNED_FILE_BYTES
            ):
                raise ARL1RuntimeError(f"{label} has unsafe file custody")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise ARL1RuntimeError(f"{label} ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ARL1RuntimeError:
        raise
    except OSError as exc:
        raise ARL1RuntimeError(f"{label} is unavailable") from exc
    payload = b"".join(chunks)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ARL1RuntimeError(f"{label} changed or differs from its byte pin")
    return payload


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate ARL-1 runtime JSON keys: {duplicates}")
    return dict(pairs)


def _load_canonical_model(payload: bytes, model_type, *, label: str):
    import json

    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
        value = model_type.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ARL1RuntimeError(f"{label} is invalid") from exc
    if canonical_json_bytes(value) != payload:
        raise ARL1RuntimeError(f"{label} is not canonical JSON")
    return value


def load_arl1_campaign_runtime_deployment(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> ARL1CampaignRuntimeDeploymentV1:
    payload = _fresh_pinned_bytes(
        path,
        expected_file_sha256,
        label="ARL-1 runtime deployment manifest",
    )
    return _load_canonical_model(
        payload,
        ARL1CampaignRuntimeDeploymentV1,
        label="ARL-1 runtime deployment manifest",
    )


def load_arl1_campaign_runtime_inputs(
    deployment: ARL1CampaignRuntimeDeploymentV1,
) -> tuple[ARL1CampaignRuntimeConfigV1, ARL1ProtocolCampaignRequestV1]:
    deployment = ARL1CampaignRuntimeDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    config = _load_canonical_model(
        _fresh_pinned_bytes(
            deployment.configuration_path,
            deployment.configuration_file_sha256,
            label="ARL-1 runtime configuration",
        ),
        ARL1CampaignRuntimeConfigV1,
        label="ARL-1 runtime configuration",
    )
    request = _load_canonical_model(
        _fresh_pinned_bytes(
            deployment.request_path,
            deployment.request_file_sha256,
            label="ARL-1 campaign request",
        ),
        ARL1ProtocolCampaignRequestV1,
        label="ARL-1 campaign request",
    )
    if (
        config.configuration_sha256 != deployment.configuration_sha256
        or request.request_sha256 != deployment.request_sha256
        or config.process_principal_id != deployment.process_principal_id
        or config.process_uid != deployment.process_uid
        or config.process_gid != deployment.process_gid
        or config.prepared_at != deployment.prepared_at
    ):
        raise ARL1RuntimeError("ARL-1 runtime inputs differ from the deployment manifest")
    return config, request


def _require_directory(config: ARL1EvidenceArchiveRuntimeConfigV1) -> None:
    try:
        metadata = os.lstat(config.root)
    except OSError as exc:
        raise ARL1RuntimeError("ARL-1 evidence archive is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != config.owner_uid
        or metadata.st_gid != config.group_gid
        or metadata.st_dev != config.device_id
        or metadata.st_ino != config.inode
        or stat.S_IMODE(metadata.st_mode) != config.directory_mode
    ):
        raise ARL1RuntimeError("ARL-1 evidence archive differs from its inode custody pin")


def _compose_kernel_store(config: ResearchKernelReadOnlyConfig) -> ResearchKernelStore:
    path = Path(config.cas_root)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ARL1RuntimeError("ARL-1 Research Kernel CAS is unavailable") from exc
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != config.cas_owner_uid
        or metadata.st_gid != config.cas_group_gid
        or metadata.st_dev != config.cas_device_id
        or metadata.st_ino != config.cas_inode
        or stat.S_IMODE(metadata.st_mode) != config.cas_directory_mode
    ):
        raise ARL1RuntimeError("ARL-1 Research Kernel CAS custody differs")
    return ResearchKernelStore(
        trust_root=config.trust_root,
        archive=FilesystemResearchArchive(
            path,
            max_object_bytes=config.max_object_bytes,
            read_only=True,
        ),
    )


def compose_arl1_campaign_service(
    config: ARL1CampaignRuntimeConfigV1,
    *,
    transport: ControllerWorkerRPCTransport | None = None,
    raw_run_custody: object | None = None,
    kernel_store: object | None = None,
    archive: LocalARL1EvidenceArchive | None = None,
    clock: Callable | None = None,
) -> ARL1ProtocolCampaignService:
    """Compose the exact keyless service; optional ports support hermetic contract tests only."""

    config = ARL1CampaignRuntimeConfigV1.model_validate(config.model_dump(mode="python"))
    if (
        config.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or config.schema_revision != expected_schema_revision()
    ):
        raise ARL1RuntimeError("ARL-1 runtime database identity differs")
    campaign_path = Path(config.campaign_implementation_source_path)
    verifier_path = Path(config.verifier_implementation_source_path)
    expected_campaign_path = Path(__file__).with_name("arl1_campaign.py").resolve(strict=True)
    expected_verifier_path = Path(__file__).with_name("arl1_verifier.py").resolve(strict=True)
    if campaign_path != expected_campaign_path or verifier_path != expected_verifier_path:
        raise ARL1RuntimeError("ARL-1 runtime implementation paths resolve another module")
    campaign_before = _fresh_pinned_bytes(
        campaign_path,
        config.campaign_implementation_source_sha256,
        label="ARL-1 campaign implementation",
    )
    verifier_before = _fresh_pinned_bytes(
        verifier_path,
        config.verifier_implementation_source_sha256,
        label="ARL-1 verifier implementation",
    )
    bindings = {item.role: item for item in config.authority_bindings}

    def client(name: str) -> ControllerWorkerRPCClient:
        return ControllerWorkerRPCClient(
            pin=getattr(config.rpc_services, name),
            controller_id=config.controller_id,
            controller_manifest_sha256=config.controller_manifest_sha256,
            worker_process_principal_id=config.process_principal_id,
            transport=transport,
            clock=clock,
        )

    registrar = RPCScientificExecutionRegistrar(
        client("execution_registration"),
        bindings[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION],
    )
    raw_runs = _PendingAwareRawRunSource(
        RPCRawRunEnvelopeSource(
            client("raw_run_source"),
            bindings[ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION],
        )
    )
    database = RPCDatabaseObservationBridge(
        client("database_observation"),
        bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
    )
    validator = RPCIndependentObservationValidator(
        client("independent_validation"),
        bindings[ControllerStepAuthorityRole.INDEPENDENT_VALIDATION],
    )
    admission = RPCIndependentObservationAdmission(
        client("independent_admission"),
        bindings[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION],
    )
    atomic = RPCAtomicObservationAdmission(
        client("atomic_admission"),
        database_binding=bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION],
        admission_binding=bindings[ControllerStepAuthorityRole.INDEPENDENT_ADMISSION],
        kernel_binding=bindings[ControllerStepAuthorityRole.KERNEL_COMMAND],
    )
    if raw_run_custody is None:
        reader = config.qualification_reader
        raw_run_custody = PostgreSQLRawRunCustodyVerificationAdapter(
            execution_lineage=compose_qualification_run_lineage_reader(reader),
            artifact_store=LocalArtifactStore(
                Path(reader.artifact_store_root),
                verifier_principal_id=reader.artifact_verifier_principal_id,
                object_store_id=reader.artifact_object_store_id,
                max_object_bytes=reader.artifact_max_object_bytes,
                read_only=True,
            ),
            sea_sessions=session_factory(),
            allocator_authority=config.allocator_authority,
            artifact_authority=config.artifact_authority,
        )
    if kernel_store is None:
        kernel_store = _compose_kernel_store(config.kernel_reader)
    if archive is None:
        _require_directory(config.evidence_archive)
        archive = LocalARL1EvidenceArchive(
            Path(config.evidence_archive.root),
            expected_owner_uid=config.evidence_archive.owner_uid,
            expected_owner_gid=config.evidence_archive.group_gid,
            object_mode=config.evidence_archive.object_mode,
            directory_mode=config.evidence_archive.directory_mode,
            max_object_bytes=config.evidence_archive.max_object_bytes,
        )
    service = ARL1ProtocolCampaignService(
        registrar=registrar,
        raw_run_source=raw_runs,
        raw_run_custody=raw_run_custody,
        validation=ARL1IndependentValidationCoordinator(
            database=database,
            validator=validator,
        ),
        admission=ARL1PrimaryAdmissionCoordinator(
            database=database,
            admission=admission,
            coordinator=atomic,
        ),
        kernel_store=kernel_store,
        archive=archive,
    )
    if campaign_before != _fresh_pinned_bytes(
        campaign_path,
        config.campaign_implementation_source_sha256,
        label="ARL-1 campaign implementation",
    ) or verifier_before != _fresh_pinned_bytes(
        verifier_path,
        config.verifier_implementation_source_sha256,
        label="ARL-1 verifier implementation",
    ):
        raise ARL1RuntimeError("ARL-1 runtime implementation changed during composition")
    return service


def execute_arl1_campaign_deployment(
    deployment: ARL1CampaignRuntimeDeploymentV1,
    *,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> ARL1ProtocolCampaignRunReceiptV1:
    """Execute on the exact Linux identity, waiting only for signed terminal-pending results."""

    if sys.platform != "linux":
        raise ARL1RuntimeError("ARL-1 protocol campaign execution requires Linux")
    if os.geteuid() != deployment.process_uid or os.getegid() != deployment.process_gid:
        raise ARL1RuntimeError("ARL-1 runtime process identity differs from deployment")
    require_schema_exact()
    config, request = load_arl1_campaign_runtime_inputs(deployment)
    runtime_clock = clock or (lambda: datetime.now(timezone.utc))
    runtime_sleeper = sleeper or time.sleep
    service = compose_arl1_campaign_service(config, clock=runtime_clock)
    deadline = request.authorizations[0].message.observation_admission_deadline
    while True:
        try:
            return service.execute(request)
        except ARL1ProtocolCampaignPending as exc:
            observed_at = runtime_clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(
                None
            ):
                raise ARL1RuntimeError("ARL-1 runtime clock is not timezone-aware UTC") from exc
            remaining_seconds = (deadline - observed_at).total_seconds()
            if remaining_seconds <= 0:
                raise ARL1RuntimeError(
                    "ARL-1 terminal material remained pending through the admission deadline"
                ) from exc
            runtime_sleeper(min(exc.retry_after_milliseconds / 1_000, remaining_seconds))


__all__ = [
    "ARL1CampaignRPCServiceSetV1",
    "ARL1CampaignRuntimeConfigV1",
    "ARL1CampaignRuntimeDeploymentV1",
    "ARL1EvidenceArchiveRuntimeConfigV1",
    "ARL1RuntimeError",
    "compose_arl1_campaign_service",
    "execute_arl1_campaign_deployment",
    "load_arl1_campaign_runtime_deployment",
    "load_arl1_campaign_runtime_inputs",
]
