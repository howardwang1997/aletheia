"""Guarded production composition for ARL-1 evidence verification and qualification.

The source verifier, qualification signer, and receipt auditor are separate one-shot processes.
They share only deployment-pinned public replay material.  The source verifier can load exactly one
source-verification key, the qualification signer can load exactly one qualification key, and the
auditor loads no private key.  Every operation freshly replays PostgreSQL, filesystem CAS, F9-v2,
Research Kernel, compiler, target-campaign, and ARL-0 evidence through the concrete verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select

from aletheia import arl1 as arl1_contract_module
from aletheia import arl1_verifier as arl1_verifier_module
from aletheia.arl1 import (
    ARL0GateKind,
    ARL1EvidenceBundleV1,
    ARL1EvidenceVerifierPinV1,
    ARL1QualificationReceiptV1,
    ARL1QualificationTrustAnchorV1,
    issue_arl1_qualification,
    verify_arl1_qualification,
)
from aletheia.arl1_runtime import (
    ARL1CampaignRuntimeConfigV1,
    ARL1EvidenceArchiveRuntimeConfigV1,
)
from aletheia.arl1_verifier import (
    ARL0GateCommandPinV1,
    ARL1EvidenceBundleSourceV1,
    LocalARL1EvidenceArchive,
    PostgreSQLARL1EvidenceVerifier,
    SubprocessARL0GateReplayPort,
    prepare_arl1_evidence_bundle,
)
from aletheia.config import get_settings
from aletheia.db import expected_schema_revision, session_factory
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
from aletheia.execution.terminal_runtime import compose_qualification_run_lineage_reader
from aletheia.observations.adapters import (
    CommittedValidationSourceVerificationContext,
    PostgreSQLCommittedObservationValidationSource,
    PostgreSQLRawRunCustodyVerificationAdapter,
    PostgreSQLResearchActionAuthorityAdapter,
)
from aletheia.observations.f9_v2_validation import WriteOnceF9V2ValidationCampaignArchive
from aletheia.observations.scientific_bridge import (
    ObservationDatabaseAuthorityPin,
    ScientificBridgeAuthorityPin,
    ScientificBridgeRole,
    VerifiedExecutionAuthorityProjection,
)
from aletheia.research_controller.step_executor import ControllerStepAuthorityRole
from aletheia.research_controller.worker_composition import (
    require_effective_read_only_directory,
)
from aletheia.research_kernel.policy import ed25519_key_id, ed25519_public_key_hex
from aletheia.research_kernel.schemas import KernelModel, canonical_json_bytes, canonical_sha256
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore
from aletheia.schema_migrations import require_schema_exact

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_MAX_PINNED_FILE_BYTES = 64 * 1024**2


class ARL1QualificationRuntimeError(RuntimeError):
    """A qualification runtime input, custody boundary, or authority failed closed."""


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


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} must be timezone-aware UTC")


def _database_time() -> datetime:
    try:
        with session_factory()() as session:
            observed_at = session.scalar(select(func.clock_timestamp()))
    except Exception as exc:  # noqa: BLE001 - PostgreSQL time is an authority boundary
        raise ARL1QualificationRuntimeError("ARL-1 database clock failed closed") from exc
    if not isinstance(observed_at, datetime):
        raise ARL1QualificationRuntimeError("ARL-1 database clock returned another type")
    try:
        _require_utc(observed_at, label="ARL-1 database clock")
    except ValueError as exc:
        raise ARL1QualificationRuntimeError("ARL-1 database clock is not UTC") from exc
    return observed_at


def _bounded_clock(
    clock: Callable[[], datetime],
    *,
    not_before: datetime,
    deadline: datetime,
    label: str,
) -> Callable[[], datetime]:
    def read() -> datetime:
        try:
            observed_at = clock()
            _require_utc(observed_at, label=f"{label} clock")
        except ARL1QualificationRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - clock is an authority boundary
            raise ARL1QualificationRuntimeError(f"{label} clock failed closed") from exc
        if not not_before <= observed_at < deadline:
            raise ARL1QualificationRuntimeError(f"{label} is outside its approved time window")
        return observed_at

    return read


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate ARL-1 qualification runtime JSON keys: {duplicates}")
    return dict(pairs)


def _fresh_regular_bytes(
    path_value: str | Path,
    expected_sha256: str,
    *,
    label: str,
    maximum_bytes: int = _MAX_PINNED_FILE_BYTES,
) -> bytes:
    path = Path(path_value)
    try:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise ARL1QualificationRuntimeError(f"{label} traverses a symlink")
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
                or not 0 < before.st_size <= maximum_bytes
            ):
                raise ARL1QualificationRuntimeError(f"{label} has unsafe file custody")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    raise ARL1QualificationRuntimeError(f"{label} ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ARL1QualificationRuntimeError(f"{label} exceeded its observed size")
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ARL1QualificationRuntimeError:
        raise
    except OSError as exc:
        raise ARL1QualificationRuntimeError(f"{label} is unavailable") from exc
    payload = b"".join(chunks)
    if (
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
    ) or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ARL1QualificationRuntimeError(f"{label} changed or differs from its byte pin")
    return payload


def _load_canonical_model(payload: bytes, model_type, *, label: str):
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
        value = model_type.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ARL1QualificationRuntimeError(f"{label} is invalid") from exc
    if canonical_json_bytes(value) != payload:
        raise ARL1QualificationRuntimeError(f"{label} is not canonical JSON")
    return value


def _exact_directory(
    path_value: str,
    *,
    owner_uid: int,
    group_gid: int,
    device_id: int,
    inode: int,
    directory_mode: int,
    label: str,
) -> Path:
    path = Path(path_value)
    try:
        metadata = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ARL1QualificationRuntimeError(f"{label} is unavailable") from exc
    if (
        resolved != path
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != group_gid
        or metadata.st_dev != device_id
        or metadata.st_ino != inode
        or stat.S_IMODE(metadata.st_mode) != directory_mode
    ):
        raise ARL1QualificationRuntimeError(f"{label} differs from its inode custody pin")
    return path


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


class ARL1PrivateSigningKeyPinV1(KernelModel):
    """One exact raw Ed25519 private key file owned by the invoking process identity."""

    schema_name: Literal["aletheia.arl1_private_signing_key_pin"] = (
        "aletheia.arl1_private_signing_key_pin"
    )
    schema_version: Literal[1] = 1
    path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    owner_uid: int = Field(ge=0)
    owner_gid: int = Field(ge=0)
    file_mode: Literal[0o400] = 0o400

    @model_validator(mode="after")
    def _key_path_is_canonical(self) -> "ARL1PrivateSigningKeyPinV1":
        _canonical_absolute_path(self.path, label="ARL-1 signing key")
        return self


class ARL1F9V2ArchiveReadConfigV1(KernelModel):
    schema_name: Literal["aletheia.arl1_f9_v2_archive_read_config"] = (
        "aletheia.arl1_f9_v2_archive_read_config"
    )
    schema_version: Literal[1] = 1
    root: str
    owner_uid: int = Field(ge=0)
    group_gid: int = Field(ge=0)
    device_id: int = Field(ge=0)
    inode: int = Field(gt=0)
    directory_mode: Literal[0o700, 0o750] = 0o700
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_authority_pin: ScientificBridgeAuthorityPin
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def _archive_is_closed_and_canonical(self) -> "ARL1F9V2ArchiveReadConfigV1":
        _canonical_absolute_path(self.root, label="ARL-1 F9-v2 archive")
        if (
            self.directory_mode & 0o022
            or self.validator_authority_pin.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
        ):
            raise ValueError("ARL-1 F9-v2 archive custody or validator role is invalid")
        return self


class ARL1EvidenceVerifierRuntimeConfigV1(KernelModel):
    """All public material used by source verification, qualification, and later audit."""

    schema_name: Literal["aletheia.arl1_evidence_verifier_runtime_config"] = (
        "aletheia.arl1_evidence_verifier_runtime_config"
    )
    schema_version: Literal[1] = 1
    configuration_id: str | None = Field(default=None, pattern=r"^arl1vc_[0-9a-f]{32}$")
    campaign_runtime: ARL1CampaignRuntimeConfigV1
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin
    database_authority_pin: ObservationDatabaseAuthorityPin
    validation_archive: ARL1F9V2ArchiveReadConfigV1
    arl0_gate_command_pins: tuple[ARL0GateCommandPinV1, ...] = Field(
        min_length=len(ARL0GateKind),
        max_length=len(ARL0GateKind),
    )
    trusted_verifier_pins: tuple[ARL1EvidenceVerifierPinV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    qualification_contract_source_path: str
    qualification_contract_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    verifier_implementation_source_path: str
    verifier_implementation_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_implementation_source_path: str
    runtime_implementation_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    private_source_verifier_key_loaded: Literal[False] = False
    private_qualification_key_loaded: Literal[False] = False
    database_mutation_allowed: Literal[False] = False
    kernel_mutation_allowed: Literal[False] = False
    scientific_admission_allowed: Literal[False] = False
    autonomous_research_design_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _authority_and_custody_are_closed(self) -> "ARL1EvidenceVerifierRuntimeConfigV1":
        campaign = self.campaign_runtime
        _require_utc(self.prepared_at, label="ARL-1 verifier config prepared_at")
        if self.prepared_at < campaign.prepared_at:
            raise ValueError("ARL-1 verifier config predates the campaign runtime")
        expected_gate_order = tuple(ARL0GateKind)
        if tuple(item.gate_kind for item in self.arl0_gate_command_pins) != expected_gate_order:
            raise ValueError("ARL-1 verifier requires every ARL-0 gate command in canonical order")
        if self.trusted_verifier_pins != tuple(
            sorted(self.trusted_verifier_pins, key=lambda item: item.pin_sha256)
        ) or len({item.pin_sha256 for item in self.trusted_verifier_pins}) != len(
            self.trusted_verifier_pins
        ):
            raise ValueError("ARL-1 verifier trust pins are noncanonical")
        bridge_pins = (
            self.execution_authority_pin,
            self.validator_authority_pin,
            self.admission_authority_pin,
        )
        if tuple(item.role for item in bridge_pins) != (
            ScientificBridgeRole.EXECUTION_AUTHORIZER,
            ScientificBridgeRole.OBSERVATION_VALIDATOR,
            ScientificBridgeRole.OBSERVATION_ADMITTER,
        ) or not all(item.active_at(self.prepared_at) for item in bridge_pins):
            raise ValueError("ARL-1 verifier bridge authorities are not exact or active")
        if not self.database_authority_pin.active_at(self.prepared_at) or not all(
            item.active_at(self.prepared_at) for item in self.trusted_verifier_pins
        ):
            raise ValueError("ARL-1 database or source-verifier authority is not active")
        bindings = {item.role: item for item in campaign.authority_bindings}
        for role, pin in (
            (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION, self.execution_authority_pin),
            (ControllerStepAuthorityRole.INDEPENDENT_VALIDATION, self.validator_authority_pin),
            (ControllerStepAuthorityRole.INDEPENDENT_ADMISSION, self.admission_authority_pin),
        ):
            binding = bindings[role]
            if (
                binding.principal_id != pin.principal_id
                or binding.key_id != pin.key_id
                or binding.policy_sha256 != pin.policy_sha256
            ):
                raise ValueError("ARL-1 verifier bridge pin differs from campaign authority")
        database_binding = bindings[ControllerStepAuthorityRole.DATABASE_ATTESTATION]
        if (
            database_binding.principal_id != self.database_authority_pin.principal_id
            or database_binding.key_id != self.database_authority_pin.key_id
            or database_binding.policy_sha256 != self.database_authority_pin.policy_sha256
            or self.validation_archive.validator_authority_pin != self.validator_authority_pin
            or self.validation_archive.validator_manifest_sha256
            != bindings[ControllerStepAuthorityRole.INDEPENDENT_VALIDATION].service_manifest_sha256
        ):
            raise ValueError("ARL-1 verifier database or F9 authority differs from campaign")
        evaluated_principals = {
            *(item.principal_id for item in bridge_pins),
            self.database_authority_pin.principal_id,
            *campaign.qualification_reader.authority_principal_ids,
            *(item.principal_id for item in campaign.kernel_reader.trust_root.commissioning_keys),
            *(pin.service_principal_id for _name, pin in campaign.rpc_services.named_pins),
            campaign.process_principal_id,
        }
        evaluated_keys = {
            *(item.key_id for item in bridge_pins),
            self.database_authority_pin.key_id,
            campaign.qualification_reader.qualification_authority_pin.key_id,
            campaign.qualification_reader.pricing_authority_pin.key_id,
            campaign.qualification_reader.source_budget_authority_pin.key_id,
            campaign.qualification_reader.terminal_verification_authority_pin.key_id,
            campaign.qualification_reader.runtime_control_authority_pin.key_id,
            *(item.key_id for item in campaign.kernel_reader.trust_root.commissioning_keys),
        }
        evaluated_policies = {
            *(item.policy_sha256 for item in bridge_pins),
            self.database_authority_pin.policy_sha256,
            campaign.qualification_reader.qualification_authority_pin.policy_sha256,
            campaign.qualification_reader.pricing_authority_pin.policy_sha256,
            campaign.qualification_reader.source_budget_authority_pin.policy_sha256,
            campaign.qualification_reader.terminal_verification_authority_pin.policy_sha256,
            campaign.qualification_reader.runtime_control_authority_pin.policy_sha256,
        }
        if (
            {item.principal_id for item in self.trusted_verifier_pins} & evaluated_principals
            or {item.key_id for item in self.trusted_verifier_pins} & evaluated_keys
            or {item.verification_policy_sha256 for item in self.trusted_verifier_pins}
            & evaluated_policies
        ):
            raise ValueError("ARL-1 source verifier overlaps an evaluated runtime authority")
        contract_path = _canonical_absolute_path(
            self.qualification_contract_source_path,
            label="ARL-1 qualification contract source",
        )
        verifier_path = _canonical_absolute_path(
            self.verifier_implementation_source_path,
            label="ARL-1 verifier implementation source",
        )
        runtime_path = _canonical_absolute_path(
            self.runtime_implementation_source_path,
            label="ARL-1 qualification runtime source",
        )
        if len({contract_path, verifier_path, runtime_path}) != 3:
            raise ValueError("ARL-1 qualification implementation pins must be distinct")
        if (
            campaign.verifier_implementation_source_path != self.verifier_implementation_source_path
            or campaign.verifier_implementation_source_sha256
            != self.verifier_implementation_source_sha256
        ):
            raise ValueError("ARL-1 verifier code pin differs from the campaign runtime")
        roots = (
            Path(campaign.evidence_archive.root),
            Path(campaign.kernel_reader.cas_root),
            Path(campaign.qualification_reader.artifact_store_root),
            Path(campaign.qualification_reader.authority_registry_root),
            Path(self.validation_archive.root),
        )
        for index, first in enumerate(roots):
            for second in roots[index + 1 :]:
                if _overlap(first, second):
                    raise ValueError("ARL-1 verifier custody roots overlap")
        expected = f"arl1vc_{self.configuration_sha256[:32]}"
        if self.configuration_id is not None and self.configuration_id != expected:
            raise ValueError("ARL-1 verifier configuration id differs from its contents")
        object.__setattr__(self, "configuration_id", expected)
        return self

    @property
    def configuration_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"configuration_id"}))


class ARL1SourceVerificationDeploymentV1(KernelModel):
    schema_name: Literal["aletheia.arl1_source_verification_deployment"] = (
        "aletheia.arl1_source_verification_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str | None = Field(default=None, pattern=r"^arl1sd_[0-9a-f]{32}$")
    configuration_path: str
    configuration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_path: str
    source_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_verifier_signing_key: ARL1PrivateSigningKeyPinV1
    signing_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    approved_at: AwareDatetime
    linux_required: Literal[True] = True
    explicit_apply_required: Literal[True] = True
    acknowledgement: Literal["PREPARE_ARL1_EVIDENCE_BUNDLE"] = "PREPARE_ARL1_EVIDENCE_BUNDLE"
    private_qualification_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_is_closed(self) -> "ARL1SourceVerificationDeploymentV1":
        _require_utc(self.approved_at, label="ARL-1 source-verification approval")
        paths = (
            _canonical_absolute_path(self.configuration_path, label="ARL-1 verifier config"),
            _canonical_absolute_path(self.source_path, label="ARL-1 evidence source"),
            Path(self.source_verifier_signing_key.path),
        )
        if len(set(paths)) != len(paths):
            raise ValueError("ARL-1 source-verification inputs overlap")
        if (
            self.source_verifier_signing_key.owner_uid != self.process_uid
            or self.source_verifier_signing_key.owner_gid != self.process_gid
        ):
            raise ValueError("ARL-1 source-verifier key owner differs from process identity")
        expected = f"arl1sd_{self.deployment_sha256[:32]}"
        if self.deployment_id is not None and self.deployment_id != expected:
            raise ValueError("ARL-1 source-verification deployment id differs")
        object.__setattr__(self, "deployment_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"deployment_id"}))


class ARL1QualificationIssuanceDeploymentV1(KernelModel):
    schema_name: Literal["aletheia.arl1_qualification_issuance_deployment"] = (
        "aletheia.arl1_qualification_issuance_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str | None = Field(default=None, pattern=r"^arl1qd_[0-9a-f]{32}$")
    configuration_path: str
    configuration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_path: str
    bundle_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    trust_anchor_path: str
    trust_anchor_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    trust_anchor_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_signing_key: ARL1PrivateSigningKeyPinV1
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    issuance_not_before: AwareDatetime
    issuance_deadline: AwareDatetime
    receipt_validity_seconds: int = Field(ge=60, le=31_536_000)
    linux_required: Literal[True] = True
    explicit_apply_required: Literal[True] = True
    acknowledgement: Literal["ISSUE_ARL1_QUALIFICATION"] = "ISSUE_ARL1_QUALIFICATION"
    private_source_verifier_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_is_closed(self) -> "ARL1QualificationIssuanceDeploymentV1":
        _require_utc(self.issuance_not_before, label="ARL-1 issuance window start")
        _require_utc(self.issuance_deadline, label="ARL-1 issuance deadline")
        paths = (
            _canonical_absolute_path(self.configuration_path, label="ARL-1 verifier config"),
            _canonical_absolute_path(self.bundle_path, label="ARL-1 evidence bundle"),
            _canonical_absolute_path(self.trust_anchor_path, label="ARL-1 trust anchor"),
            Path(self.qualification_signing_key.path),
        )
        if (
            len(set(paths)) != len(paths)
            or not self.issuance_not_before < self.issuance_deadline
            or self.issuance_deadline - self.issuance_not_before > timedelta(hours=24)
        ):
            raise ValueError("ARL-1 qualification inputs overlap or issuance window is invalid")
        if (
            self.qualification_signing_key.owner_uid != self.process_uid
            or self.qualification_signing_key.owner_gid != self.process_gid
        ):
            raise ValueError("ARL-1 qualification key owner differs from process identity")
        expected = f"arl1qd_{self.deployment_sha256[:32]}"
        if self.deployment_id is not None and self.deployment_id != expected:
            raise ValueError("ARL-1 qualification deployment id differs")
        object.__setattr__(self, "deployment_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"deployment_id"}))


class ARL1QualificationVerificationDeploymentV1(KernelModel):
    schema_name: Literal["aletheia.arl1_qualification_verification_deployment"] = (
        "aletheia.arl1_qualification_verification_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str | None = Field(default=None, pattern=r"^arl1vd_[0-9a-f]{32}$")
    configuration_path: str
    configuration_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_path: str
    bundle_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    trust_anchor_path: str
    trust_anchor_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    trust_anchor_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipt_path: str
    receipt_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_uid: int = Field(ge=0)
    process_gid: int = Field(ge=0)
    verification_not_before: AwareDatetime
    verification_deadline: AwareDatetime
    linux_required: Literal[True] = True
    explicit_verification_required: Literal[True] = True
    acknowledgement: Literal["VERIFY_ARL1_QUALIFICATION"] = "VERIFY_ARL1_QUALIFICATION"
    private_source_verifier_key_loaded: Literal[False] = False
    private_qualification_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _deployment_is_closed(self) -> "ARL1QualificationVerificationDeploymentV1":
        _require_utc(self.verification_not_before, label="ARL-1 verification window start")
        _require_utc(self.verification_deadline, label="ARL-1 verification deadline")
        paths = tuple(
            _canonical_absolute_path(value, label="ARL-1 verification input")
            for value in (
                self.configuration_path,
                self.bundle_path,
                self.trust_anchor_path,
                self.receipt_path,
            )
        )
        if (
            len(set(paths)) != len(paths)
            or not self.verification_not_before < self.verification_deadline
            or self.verification_deadline - self.verification_not_before > timedelta(hours=24)
        ):
            raise ValueError("ARL-1 verification inputs overlap or time window is invalid")
        expected = f"arl1vd_{self.deployment_sha256[:32]}"
        if self.deployment_id is not None and self.deployment_id != expected:
            raise ValueError("ARL-1 verification deployment id differs")
        object.__setattr__(self, "deployment_id", expected)
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"deployment_id"}))


_Deployment = (
    ARL1SourceVerificationDeploymentV1
    | ARL1QualificationIssuanceDeploymentV1
    | ARL1QualificationVerificationDeploymentV1
)


def load_arl1_qualification_runtime_deployment(
    path: str | Path,
    *,
    expected_file_sha256: str,
    model_type: type[_Deployment],
) -> _Deployment:
    payload = _fresh_regular_bytes(
        path,
        expected_file_sha256,
        label="ARL-1 qualification deployment manifest",
    )
    return _load_canonical_model(
        payload,
        model_type,
        label="ARL-1 qualification deployment manifest",
    )


def _load_config(deployment: _Deployment) -> tuple[ARL1EvidenceVerifierRuntimeConfigV1, bytes]:
    payload = _fresh_regular_bytes(
        deployment.configuration_path,
        deployment.configuration_file_sha256,
        label="ARL-1 evidence-verifier runtime config",
    )
    config = _load_canonical_model(
        payload,
        ARL1EvidenceVerifierRuntimeConfigV1,
        label="ARL-1 evidence-verifier runtime config",
    )
    if config.configuration_sha256 != deployment.configuration_sha256:
        raise ARL1QualificationRuntimeError("ARL-1 verifier config differs from deployment")
    return config, payload


def _load_model_path(path: str, digest: str, model_type, *, label: str):
    payload = _fresh_regular_bytes(path, digest, label=label)
    return _load_canonical_model(payload, model_type, label=label), payload


def _load_private_key(pin: ARL1PrivateSigningKeyPinV1, *, label: str) -> bytes:
    path = Path(pin.path)
    try:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise ARL1QualificationRuntimeError(f"{label} traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            payload = os.read(descriptor, 33)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ARL1QualificationRuntimeError:
        raise
    except OSError as exc:
        raise ARL1QualificationRuntimeError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != pin.owner_uid
        or before.st_gid != pin.owner_gid
        or stat.S_IMODE(before.st_mode) != pin.file_mode
        or len(payload) != 32
        or hashlib.sha256(payload).hexdigest() != pin.file_sha256
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or ed25519_key_id(ed25519_public_key_hex(payload)) != pin.key_id
    ):
        raise ARL1QualificationRuntimeError(f"{label} differs from its custody or public-key pin")
    return payload


def _compose_kernel_store(config) -> ResearchKernelStore:
    root = _exact_directory(
        config.cas_root,
        owner_uid=config.cas_owner_uid,
        group_gid=config.cas_group_gid,
        device_id=config.cas_device_id,
        inode=config.cas_inode,
        directory_mode=config.cas_directory_mode,
        label="ARL-1 verifier Research Kernel CAS",
    )
    return ResearchKernelStore(
        trust_root=config.trust_root,
        archive=FilesystemResearchArchive(
            root,
            max_object_bytes=config.max_object_bytes,
            read_only=True,
        ),
    )


def compose_arl1_evidence_verifier(
    config: ARL1EvidenceVerifierRuntimeConfigV1,
    *,
    source_signing_private_key: bytes | None = None,
    source_signing_pin_sha256: str | None = None,
    clock: Callable[[], datetime] = _database_time,
) -> PostgreSQLARL1EvidenceVerifier:
    """Compose the concrete verifier; optional signing material can only sign source receipts."""

    config = ARL1EvidenceVerifierRuntimeConfigV1.model_validate(config.model_dump(mode="python"))
    campaign = config.campaign_runtime
    campaign.kernel_reader.require_effective_read_only(
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
    )
    require_effective_read_only_directory(
        owner_uid=config.validation_archive.owner_uid,
        group_gid=config.validation_archive.group_gid,
        directory_mode=config.validation_archive.directory_mode,
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        label="ARL-1 F9-v2 validation archive",
    )
    if (
        campaign.database_url_sha256
        != hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
        or campaign.schema_revision != expected_schema_revision()
    ):
        raise ARL1QualificationRuntimeError("ARL-1 verifier database identity differs")
    require_schema_exact()
    expected_paths = (
        (
            Path(arl1_contract_module.__file__).resolve(strict=True),
            Path(config.qualification_contract_source_path),
            config.qualification_contract_source_sha256,
            "ARL-1 qualification contract",
        ),
        (
            Path(arl1_verifier_module.__file__).resolve(strict=True),
            Path(config.verifier_implementation_source_path),
            config.verifier_implementation_source_sha256,
            "ARL-1 evidence verifier",
        ),
        (
            Path(__file__).resolve(strict=True),
            Path(config.runtime_implementation_source_path),
            config.runtime_implementation_source_sha256,
            "ARL-1 qualification runtime",
        ),
    )
    before: list[bytes] = []
    for actual, configured, digest, label in expected_paths:
        if actual != configured:
            raise ARL1QualificationRuntimeError(f"{label} path resolves another module")
        before.append(_fresh_regular_bytes(configured, digest, label=label))
    archive_config: ARL1EvidenceArchiveRuntimeConfigV1 = campaign.evidence_archive
    archive_root = _exact_directory(
        archive_config.root,
        owner_uid=archive_config.owner_uid,
        group_gid=archive_config.group_gid,
        device_id=archive_config.device_id,
        inode=archive_config.inode,
        directory_mode=archive_config.directory_mode,
        label="ARL-1 evidence archive",
    )
    f9 = config.validation_archive
    f9_root = _exact_directory(
        f9.root,
        owner_uid=f9.owner_uid,
        group_gid=f9.group_gid,
        device_id=f9.device_id,
        inode=f9.inode,
        directory_mode=f9.directory_mode,
        label="ARL-1 F9-v2 validation archive",
    )
    kernel_store = _compose_kernel_store(campaign.kernel_reader)
    action_authority = PostgreSQLResearchActionAuthorityAdapter(kernel_store)
    reader = campaign.qualification_reader
    sessions = session_factory()
    raw_run_custody = PostgreSQLRawRunCustodyVerificationAdapter(
        execution_lineage=compose_qualification_run_lineage_reader(reader),
        artifact_store=LocalArtifactStore(
            Path(reader.artifact_store_root),
            verifier_principal_id=reader.artifact_verifier_principal_id,
            object_store_id=reader.artifact_object_store_id,
            max_object_bytes=reader.artifact_max_object_bytes,
            read_only=True,
        ),
        sea_sessions=sessions,
        # The raw-run lineage attributes allocation to the cost quote's
        # quoter, so the custody projection is the pricing authority pin —
        # the same derivation the five RPC service runtimes and the campaign
        # runner use (the generation-l unsatisfiable glued field is gone).
        allocator_authority=VerifiedExecutionAuthorityProjection(
            principal_id=reader.pricing_authority_pin.principal_id,
            key_id=reader.pricing_authority_pin.key_id,
            policy_sha256=reader.pricing_authority_pin.policy_sha256,
        ),
        artifact_authority=campaign.artifact_authority,
    )
    validation_campaign = WriteOnceF9V2ValidationCampaignArchive(
        f9_root,
        validator_manifest_sha256=f9.validator_manifest_sha256,
        validator_authority_pin=f9.validator_authority_pin,
        read_only=True,
    )
    observation_verification = CommittedValidationSourceVerificationContext(
        qualification_authority=QualificationAuthorityVerifier(reader.qualification_authority_pin),
        action_authority=action_authority,
        qualification_custody=raw_run_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign,
        execution_authority_pin=config.execution_authority_pin,
        validator_authority_pin=config.validator_authority_pin,
        admission_authority_pin=config.admission_authority_pin,
        database_authority_pin=config.database_authority_pin,
    )
    verifier = PostgreSQLARL1EvidenceVerifier(
        archive=LocalARL1EvidenceArchive(
            archive_root,
            read_only=True,
            expected_owner_uid=archive_config.owner_uid,
            expected_owner_gid=archive_config.group_gid,
            object_mode=archive_config.object_mode,
            directory_mode=archive_config.directory_mode,
            max_object_bytes=archive_config.max_object_bytes,
        ),
        gate_replayer=SubprocessARL0GateReplayPort(
            config.arl0_gate_command_pins,
            clock=clock,
        ),
        sessions=sessions,
        action_authority=action_authority,
        raw_run_custody=raw_run_custody,
        committed_validation_source=PostgreSQLCommittedObservationValidationSource(
            sessions=sessions,
            verification=observation_verification,
        ),
        observation_verification=observation_verification,
        kernel_store=kernel_store,
        trusted_verifier_pins=config.trusted_verifier_pins,
        signing_private_key=source_signing_private_key,
        signing_pin_sha256=source_signing_pin_sha256,
        clock=clock,
    )
    for old, (_actual, configured, digest, label) in zip(before, expected_paths, strict=True):
        if old != _fresh_regular_bytes(configured, digest, label=label):
            raise ARL1QualificationRuntimeError(f"{label} changed during composition")
    return verifier


def _require_host(process_uid: int, process_gid: int) -> None:
    if sys.platform != "linux":
        raise ARL1QualificationRuntimeError("ARL-1 qualification operations require Linux")
    if os.geteuid() != process_uid or os.getegid() != process_gid:
        raise ARL1QualificationRuntimeError("ARL-1 qualification process identity differs")


def _assert_process_separation(
    config: ARL1EvidenceVerifierRuntimeConfigV1, principal_id: str
) -> None:
    campaign = config.campaign_runtime
    evaluated = {
        campaign.process_principal_id,
        *campaign.qualification_reader.authority_principal_ids,
        *(item.principal_id for item in config.campaign_runtime.authority_bindings),
        *(item.principal_id for item in campaign.kernel_reader.trust_root.commissioning_keys),
        *(pin.service_principal_id for _name, pin in campaign.rpc_services.named_pins),
    }
    if principal_id in evaluated:
        raise ARL1QualificationRuntimeError(
            "ARL-1 qualification process overlaps an evaluated authority"
        )


def _assert_secret_path_separation(
    config: ARL1EvidenceVerifierRuntimeConfigV1,
    key_path_value: str,
) -> None:
    key_path = Path(key_path_value)
    campaign = config.campaign_runtime
    public_roots = (
        Path(campaign.evidence_archive.root),
        Path(campaign.kernel_reader.cas_root),
        Path(campaign.qualification_reader.artifact_store_root),
        Path(campaign.qualification_reader.authority_registry_root),
        Path(config.validation_archive.root),
    )
    public_files = (
        Path(config.qualification_contract_source_path),
        Path(config.verifier_implementation_source_path),
        Path(config.runtime_implementation_source_path),
    )
    if any(_overlap(key_path, path) for path in (*public_roots, *public_files)):
        raise ARL1QualificationRuntimeError(
            "ARL-1 signing key overlaps public evidence or implementation custody"
        )


def prepare_arl1_evidence_bundle_deployment(
    deployment: ARL1SourceVerificationDeploymentV1,
    *,
    clock: Callable[[], datetime] = _database_time,
) -> ARL1EvidenceBundleV1:
    """Replay and sign source evidence without loading the qualification signing key."""

    deployment = ARL1SourceVerificationDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    _require_host(deployment.process_uid, deployment.process_gid)
    require_schema_exact()
    config, config_bytes = _load_config(deployment)
    _assert_process_separation(config, deployment.process_principal_id)
    _assert_secret_path_separation(config, deployment.source_verifier_signing_key.path)
    source, source_bytes = _load_model_path(
        deployment.source_path,
        deployment.source_file_sha256,
        ARL1EvidenceBundleSourceV1,
        label="ARL-1 evidence-bundle source",
    )
    source_sha256 = canonical_sha256(source)
    pin = next(
        (
            item
            for item in config.trusted_verifier_pins
            if item.pin_sha256 == deployment.signing_pin_sha256
        ),
        None,
    )
    if (
        source_sha256 != deployment.source_sha256
        or source.policy.policy_sha256 != deployment.expected_policy_sha256
        or pin is None
        or pin.principal_id != deployment.process_principal_id
        or pin.key_id != deployment.source_verifier_signing_key.key_id
        or not pin.active_at(deployment.approved_at)
        or deployment.approved_at
        < max(
            source.arl0_integrity.completed_at,
            source.target_campaign_receipt.completed_at,
            *(item.report.reported_at for item in source.protocol_campaigns),
        )
    ):
        raise ARL1QualificationRuntimeError(
            "ARL-1 evidence source, policy, verifier, or chronology differs from deployment"
        )
    key = _load_private_key(
        deployment.source_verifier_signing_key,
        label="ARL-1 source-verifier signing key",
    )
    verifier = compose_arl1_evidence_verifier(
        config,
        source_signing_private_key=key,
        source_signing_pin_sha256=deployment.signing_pin_sha256,
        clock=clock,
    )
    bundle = prepare_arl1_evidence_bundle(source, source_verifier=verifier)
    if bundle.policy.policy_sha256 != deployment.expected_policy_sha256:
        raise ARL1QualificationRuntimeError("ARL-1 prepared bundle changed its trusted policy")
    if config_bytes != _fresh_regular_bytes(
        deployment.configuration_path,
        deployment.configuration_file_sha256,
        label="ARL-1 evidence-verifier runtime config",
    ) or source_bytes != _fresh_regular_bytes(
        deployment.source_path,
        deployment.source_file_sha256,
        label="ARL-1 evidence-bundle source",
    ):
        raise ARL1QualificationRuntimeError(
            "ARL-1 source-verification inputs changed during replay"
        )
    if key != _load_private_key(
        deployment.source_verifier_signing_key,
        label="ARL-1 source-verifier signing key",
    ):
        raise ARL1QualificationRuntimeError("ARL-1 source-verifier key changed during replay")
    return bundle


def issue_arl1_qualification_deployment(
    deployment: ARL1QualificationIssuanceDeploymentV1,
    *,
    clock: Callable[[], datetime] = _database_time,
) -> ARL1QualificationReceiptV1:
    """Freshly replay one prepared bundle and sign only its bounded ARL-1 claim."""

    deployment = ARL1QualificationIssuanceDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    _require_host(deployment.process_uid, deployment.process_gid)
    require_schema_exact()
    config, config_bytes = _load_config(deployment)
    _assert_process_separation(config, deployment.process_principal_id)
    _assert_secret_path_separation(config, deployment.qualification_signing_key.path)
    bounded_clock = _bounded_clock(
        clock,
        not_before=deployment.issuance_not_before,
        deadline=deployment.issuance_deadline,
        label="ARL-1 qualification issuance",
    )
    bounded_clock()
    bundle, bundle_bytes = _load_model_path(
        deployment.bundle_path,
        deployment.bundle_file_sha256,
        ARL1EvidenceBundleV1,
        label="ARL-1 evidence bundle",
    )
    anchor, anchor_bytes = _load_model_path(
        deployment.trust_anchor_path,
        deployment.trust_anchor_file_sha256,
        ARL1QualificationTrustAnchorV1,
        label="ARL-1 out-of-band trust anchor",
    )
    if (
        bundle.bundle_sha256 != deployment.bundle_sha256
        or bundle.policy.policy_sha256 != deployment.expected_policy_sha256
        or anchor.anchor_sha256 != deployment.trust_anchor_sha256
        or anchor.policy_sha256 != deployment.expected_policy_sha256
        or anchor.qualification_authority_principal_id != deployment.process_principal_id
        or anchor.qualification_authority_key_id != deployment.qualification_signing_key.key_id
        or tuple(item.pin_sha256 for item in config.trusted_verifier_pins)
        != anchor.evidence_verifier_pin_sha256s
    ):
        raise ARL1QualificationRuntimeError(
            "ARL-1 bundle, trust anchor, verifier set, or signer differs from deployment"
        )
    key = _load_private_key(
        deployment.qualification_signing_key,
        label="ARL-1 qualification signing key",
    )
    verifier = compose_arl1_evidence_verifier(config, clock=bounded_clock)
    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        trust_anchor=anchor,
        qualification_private_key=key,
        receipt_validity_seconds=deployment.receipt_validity_seconds,
        clock=bounded_clock,
    )
    completed_at = bounded_clock()
    if not receipt.message.qualified_at <= completed_at < receipt.message.expires_at:
        raise ARL1QualificationRuntimeError(
            "ARL-1 qualification was not active when signing completed"
        )
    for path, digest, before, label in (
        (
            deployment.configuration_path,
            deployment.configuration_file_sha256,
            config_bytes,
            "ARL-1 evidence-verifier runtime config",
        ),
        (
            deployment.bundle_path,
            deployment.bundle_file_sha256,
            bundle_bytes,
            "ARL-1 evidence bundle",
        ),
        (
            deployment.trust_anchor_path,
            deployment.trust_anchor_file_sha256,
            anchor_bytes,
            "ARL-1 out-of-band trust anchor",
        ),
    ):
        if before != _fresh_regular_bytes(path, digest, label=label):
            raise ARL1QualificationRuntimeError(f"{label} changed during qualification")
    if key != _load_private_key(
        deployment.qualification_signing_key,
        label="ARL-1 qualification signing key",
    ):
        raise ARL1QualificationRuntimeError("ARL-1 qualification key changed during signing")
    return receipt


def verify_arl1_qualification_deployment(
    deployment: ARL1QualificationVerificationDeploymentV1,
    *,
    clock: Callable[[], datetime] = _database_time,
) -> ARL1QualificationReceiptV1:
    """Restart from empty memory and verify the complete qualification without a private key."""

    deployment = ARL1QualificationVerificationDeploymentV1.model_validate(
        deployment.model_dump(mode="python")
    )
    _require_host(deployment.process_uid, deployment.process_gid)
    require_schema_exact()
    config, config_bytes = _load_config(deployment)
    _assert_process_separation(config, deployment.process_principal_id)
    bounded_clock = _bounded_clock(
        clock,
        not_before=deployment.verification_not_before,
        deadline=deployment.verification_deadline,
        label="ARL-1 qualification verification",
    )
    bounded_clock()
    bundle, bundle_bytes = _load_model_path(
        deployment.bundle_path,
        deployment.bundle_file_sha256,
        ARL1EvidenceBundleV1,
        label="ARL-1 evidence bundle",
    )
    anchor, anchor_bytes = _load_model_path(
        deployment.trust_anchor_path,
        deployment.trust_anchor_file_sha256,
        ARL1QualificationTrustAnchorV1,
        label="ARL-1 out-of-band trust anchor",
    )
    receipt, receipt_bytes = _load_model_path(
        deployment.receipt_path,
        deployment.receipt_file_sha256,
        ARL1QualificationReceiptV1,
        label="ARL-1 qualification receipt",
    )
    if (
        bundle.bundle_sha256 != deployment.bundle_sha256
        or bundle.policy.policy_sha256 != deployment.expected_policy_sha256
        or anchor.anchor_sha256 != deployment.trust_anchor_sha256
        or anchor.policy_sha256 != deployment.expected_policy_sha256
        or receipt.receipt_sha256 != deployment.receipt_sha256
        or tuple(item.pin_sha256 for item in config.trusted_verifier_pins)
        != anchor.evidence_verifier_pin_sha256s
        or deployment.process_principal_id
        in {
            anchor.qualification_authority_principal_id,
            *(item.principal_id for item in config.trusted_verifier_pins),
        }
    ):
        raise ARL1QualificationRuntimeError(
            "ARL-1 verification bundle, anchor, receipt, verifier set, or auditor separation differs"
        )
    verified = verify_arl1_qualification(
        bundle,
        receipt,
        source_verifier=compose_arl1_evidence_verifier(config, clock=bounded_clock),
        trust_anchor=anchor,
        clock=bounded_clock,
    )
    completed_at = bounded_clock()
    if not receipt.message.qualified_at <= completed_at < receipt.message.expires_at:
        raise ARL1QualificationRuntimeError(
            "ARL-1 receipt was not active when fresh verification completed"
        )
    for path, digest, before, label in (
        (
            deployment.configuration_path,
            deployment.configuration_file_sha256,
            config_bytes,
            "ARL-1 evidence-verifier runtime config",
        ),
        (
            deployment.bundle_path,
            deployment.bundle_file_sha256,
            bundle_bytes,
            "ARL-1 evidence bundle",
        ),
        (
            deployment.trust_anchor_path,
            deployment.trust_anchor_file_sha256,
            anchor_bytes,
            "ARL-1 out-of-band trust anchor",
        ),
        (
            deployment.receipt_path,
            deployment.receipt_file_sha256,
            receipt_bytes,
            "ARL-1 qualification receipt",
        ),
    ):
        if before != _fresh_regular_bytes(path, digest, label=label):
            raise ARL1QualificationRuntimeError(f"{label} changed during verification")
    return verified


__all__ = [
    "ARL1EvidenceVerifierRuntimeConfigV1",
    "ARL1F9V2ArchiveReadConfigV1",
    "ARL1PrivateSigningKeyPinV1",
    "ARL1QualificationIssuanceDeploymentV1",
    "ARL1QualificationRuntimeError",
    "ARL1QualificationVerificationDeploymentV1",
    "ARL1SourceVerificationDeploymentV1",
    "compose_arl1_evidence_verifier",
    "issue_arl1_qualification_deployment",
    "load_arl1_qualification_runtime_deployment",
    "prepare_arl1_evidence_bundle_deployment",
    "verify_arl1_qualification_deployment",
]
