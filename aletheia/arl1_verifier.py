"""Production evidence custody and independent source replay for ARL-1.

The pure contracts in :mod:`aletheia.arl1` deliberately cannot read a database or filesystem.
This module is the operational verifier: it freshly reopens every archived source, replays the
native compiler and scientific authority chain, checks the PostgreSQL rows and Kernel ledger, and
only then accepts or issues the independent source-verification signature.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aletheia.arl1 import (
    ARL0GateEvidenceV1,
    ARL0GateKind,
    ARL0IntegrityEvidenceV1,
    ARL1ArchiveManifestKind,
    ARL1EvidenceArchiveEntryV1,
    ARL1EvidenceArchiveManifestV1,
    ARL1EvidenceBundleV1,
    ARL1EvidenceVerifierPinV1,
    ARL1ProtocolCampaignEvidenceV1,
    ARL1QualificationError,
    ARL1QualificationPolicyV1,
    ARL1SourceVerificationReceiptV1,
    ARL1VerificationSubjectKind,
    issue_arl1_source_verification_receipt,
    verify_arl1_source_verification_receipt,
)
from aletheia.observations.adapters import (
    CommittedValidationSourceVerificationContext,
)
from aletheia.observations.scientific_bridge import (
    CommittedObservationAdmission,
    ScientificExecutionAuthorization,
    VerifiedRawRunCustodyProjection,
    verify_committed_observation_admission,
)
from aletheia.observations.store import (
    ObservationAdmissionWrite,
    ScientificExecutionAuthorizationWrite,
    get_observation_admission_by_slot,
    get_scientific_execution_authorization_by_slot,
)
from aletheia.protocols.compiler import verify_compilation
from aletheia.qualification_campaign import (
    QualificationTargetCampaignReceiptV1,
    QualificationTargetCampaignRequestV1,
    verify_qualification_target_campaign_receipt,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import KernelModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MAX_ARCHIVE_OBJECT_BYTES = 64 * 1024**2


class ARL1SourceVerificationError(ARL1QualificationError):
    """Archive custody or production authority replay failed closed."""


@dataclass(frozen=True)
class FreshARL1ArchiveObject:
    entry: ARL1EvidenceArchiveEntryV1
    payload: bytes


class LocalARL1EvidenceArchive:
    """Small write-once JSON/evidence CAS with fresh, no-follow reads.

    Files are stored as ``objects/sha256/<prefix>/<digest>``.  A separate principal can mount the
    root read-only and construct this facade with ``read_only=True``.  Every read checks type,
    link count, owner/mode pins, length and SHA-256 before returning bytes.
    """

    def __init__(
        self,
        root: Path,
        *,
        read_only: bool = False,
        expected_owner_uid: int | None = None,
        expected_owner_gid: int | None = None,
        object_mode: int = 0o400,
        directory_mode: int = 0o700,
        max_object_bytes: int = _MAX_ARCHIVE_OBJECT_BYTES,
    ) -> None:
        if object_mode not in {0o400, 0o440}:
            raise ValueError("ARL-1 archive object mode must be 0400 or 0440")
        if directory_mode not in {0o700, 0o750}:
            raise ValueError("ARL-1 archive directory mode must be 0700 or 0750")
        if object_mode == 0o440 and directory_mode != 0o750:
            raise ValueError("group-readable ARL-1 objects require traversable 0750 directories")
        if not 1 <= max_object_bytes <= _MAX_ARCHIVE_OBJECT_BYTES:
            raise ValueError("ARL-1 archive object bound is invalid")
        candidate = Path(root)
        if candidate.is_symlink():
            raise ARL1SourceVerificationError("ARL-1 archive root cannot be a symlink")
        root_existed = candidate.exists()
        if read_only:
            if not candidate.is_dir():
                raise ARL1SourceVerificationError("read-only ARL-1 archive root is absent")
        else:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not root_existed:
                candidate.chmod(directory_mode)
        self.root = candidate.resolve(strict=True)
        self.read_only = read_only
        self.expected_owner_uid = expected_owner_uid
        self.expected_owner_gid = expected_owner_gid
        self.object_mode = object_mode
        self.directory_mode = directory_mode
        self.max_object_bytes = max_object_bytes
        descriptor = self._open_root()
        try:
            child = self._open_directory(
                descriptor,
                ("objects", "sha256"),
                create=not read_only,
            )
            os.close(child)
        finally:
            os.close(descriptor)

    def _open_root(self) -> int:
        try:
            descriptor = os.open(
                self.root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise ARL1SourceVerificationError("ARL-1 archive root became unsafe") from exc
        metadata = os.fstat(descriptor)
        if not self._directory_metadata_is_exact(metadata):
            os.close(descriptor)
            raise ARL1SourceVerificationError("ARL-1 archive root custody is unsafe")
        return descriptor

    def _directory_metadata_is_exact(self, metadata: os.stat_result) -> bool:
        return bool(
            stat.S_ISDIR(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == self.directory_mode
            and (self.expected_owner_uid is None or metadata.st_uid == self.expected_owner_uid)
            and (self.expected_owner_gid is None or metadata.st_gid == self.expected_owner_gid)
        )

    def _open_directory(
        self,
        root: int,
        components: tuple[str, ...],
        *,
        create: bool,
    ) -> int:
        descriptor = os.dup(root)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in components:
                created = False
                if create:
                    try:
                        os.mkdir(component, mode=self.directory_mode, dir_fd=descriptor)
                        os.fsync(descriptor)
                        created = True
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                if created:
                    os.fchmod(child, self.directory_mode)
                metadata = os.fstat(child)
                if not self._directory_metadata_is_exact(metadata):
                    os.close(child)
                    raise ARL1SourceVerificationError("ARL-1 archive directory custody is unsafe")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _prefix_directory(self, digest: str, *, create: bool) -> int:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ARL1SourceVerificationError("ARL-1 archive digest is invalid")
        root = self._open_root()
        try:
            return self._open_directory(
                root,
                ("objects", "sha256", digest[:2]),
                create=create,
            )
        finally:
            os.close(root)

    def _validate_file(self, metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != self.object_mode
            or metadata.st_size < 1
            or metadata.st_size > self.max_object_bytes
            or (self.expected_owner_uid is not None and metadata.st_uid != self.expected_owner_uid)
            or (self.expected_owner_gid is not None and metadata.st_gid != self.expected_owner_gid)
        ):
            raise ARL1SourceVerificationError("ARL-1 archive object custody differs")

    def publish_bytes(
        self,
        *,
        object_kind: str,
        payload: bytes,
        canonical_json: bool,
    ) -> ARL1EvidenceArchiveEntryV1:
        if self.read_only:
            raise ARL1SourceVerificationError("read-only ARL-1 archive cannot publish")
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= self.max_object_bytes:
            raise ARL1SourceVerificationError("ARL-1 archive payload size is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        directory = self._prefix_directory(digest, create=True)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            try:
                descriptor = os.open(digest, flags, self.object_mode, dir_fd=directory)
            except FileExistsError:
                existing = self.load_bytes(digest)
                if existing != payload:
                    raise ARL1SourceVerificationError(
                        "ARL-1 archive identity collided with different bytes"
                    )
            else:
                try:
                    os.fchmod(descriptor, self.object_mode)
                    view = memoryview(payload)
                    offset = 0
                    while offset < len(view):
                        written = os.write(descriptor, view[offset:])
                        if written <= 0:  # pragma: no cover - regular writes progress or raise
                            raise OSError("archive write made no progress")
                        offset += written
                    os.fsync(descriptor)
                    self._validate_file(os.fstat(descriptor))
                finally:
                    os.close(descriptor)
                os.fsync(directory)
        except ARL1SourceVerificationError:
            raise
        except OSError as exc:
            raise ARL1SourceVerificationError("ARL-1 archive publish failed") from exc
        finally:
            os.close(directory)
        return ARL1EvidenceArchiveEntryV1(
            object_kind=object_kind,
            object_sha256=digest,
            byte_length=len(payload),
            canonical_json=canonical_json,
        )

    def publish_model(
        self,
        *,
        object_kind: str,
        value: BaseModel,
    ) -> ARL1EvidenceArchiveEntryV1:
        return self.publish_bytes(
            object_kind=object_kind,
            payload=canonical_json_bytes(value),
            canonical_json=True,
        )

    def publish_manifest(
        self,
        manifest: ARL1EvidenceArchiveManifestV1,
    ) -> str:
        entry = self.publish_model(object_kind="archive_manifest", value=manifest)
        if entry.object_sha256 != manifest.manifest_sha256:
            raise ARL1SourceVerificationError("ARL-1 archive manifest identity differs")
        return entry.object_sha256

    def load_bytes(self, object_sha256: str) -> bytes:
        directory = self._prefix_directory(object_sha256, create=False)
        try:
            descriptor = os.open(
                object_sha256,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            try:
                before = os.fstat(descriptor)
                self._validate_file(before)
                chunks: list[bytes] = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(1024**2, remaining))
                    if not chunk:
                        raise ARL1SourceVerificationError(
                            "ARL-1 archive object was truncated during read"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                after = os.fstat(descriptor)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) or hashlib.sha256(payload).hexdigest() != object_sha256:
                    raise ARL1SourceVerificationError(
                        "ARL-1 archive object changed or failed fresh rehash"
                    )
                return payload
            finally:
                os.close(descriptor)
        except ARL1SourceVerificationError:
            raise
        except OSError as exc:
            raise ARL1SourceVerificationError("ARL-1 archive object is missing or unsafe") from exc
        finally:
            os.close(directory)

    def load_entry(self, entry: ARL1EvidenceArchiveEntryV1) -> FreshARL1ArchiveObject:
        payload = self.load_bytes(entry.object_sha256)
        if len(payload) != entry.byte_length:
            raise ARL1SourceVerificationError("ARL-1 archive entry byte length differs")
        return FreshARL1ArchiveObject(entry=entry, payload=payload)

    def load_model(self, object_sha256: str, model_type: type[BaseModel]) -> BaseModel:
        payload = self.load_bytes(object_sha256)
        try:
            value = model_type.model_validate_json(payload)
        except (TypeError, ValueError) as exc:
            raise ARL1SourceVerificationError("ARL-1 archived model is invalid") from exc
        if payload != canonical_json_bytes(value):
            raise ARL1SourceVerificationError("ARL-1 archived model is not canonical JSON")
        return value


class ARL0GateReplayProjectionV1(KernelModel):
    schema_name: Literal["aletheia.arl0_gate_replay_projection"] = (
        "aletheia.arl0_gate_replay_projection"
    )
    schema_version: Literal[1] = 1
    gate_kind: ARL0GateKind
    gate_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluated_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    verification_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    replayed_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    replayed_at: AwareDatetime
    passed: Literal[True] = True
    synthetic_evidence: Literal[False] = False


class ARL0GateReplayPort(Protocol):
    def replay_gate(
        self,
        *,
        gate: ARL0GateEvidenceV1,
        evidence_artifact: bytes,
        verification_receipt: bytes,
        observed_at: datetime,
    ) -> ARL0GateReplayProjectionV1: ...


class ARL0GateCommandResultV1(KernelModel):
    """Deterministic stdout contract emitted by one frozen ARL-0 gate command."""

    schema_name: Literal["aletheia.arl0_gate_command_result"] = "aletheia.arl0_gate_command_result"
    schema_version: Literal[1] = 1
    gate_kind: ARL0GateKind
    evaluated_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    checks: tuple[str, ...] = Field(min_length=1, max_length=256)
    blockers: tuple[str, ...] = ()
    passed: Literal[True] = True
    synthetic_evidence: Literal[False] = False

    @model_validator(mode="after")
    def _result_is_canonical(self) -> "ARL0GateCommandResultV1":
        if self.checks != tuple(sorted(set(self.checks))) or self.blockers:
            raise ValueError("ARL-0 gate command result is noncanonical or blocked")
        return self


class ARL0PinnedInputV1(KernelModel):
    absolute_path: str
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _path_is_absolute(self) -> "ARL0PinnedInputV1":
        path = Path(self.absolute_path)
        if not path.is_absolute() or str(path) != os.path.normpath(self.absolute_path):
            raise ValueError("ARL-0 pinned input path must be canonical and absolute")
        return self


class ARL0GateCommandPinV1(KernelModel):
    """Out-of-band command identity used to independently replay one integrity gate."""

    schema_name: Literal["aletheia.arl0_gate_command_pin"] = "aletheia.arl0_gate_command_pin"
    schema_version: Literal[1] = 1
    gate_kind: ARL0GateKind
    evaluated_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    executable_path: str
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    arguments: tuple[str, ...] = Field(max_length=128)
    working_directory: str
    pinned_inputs: tuple[ARL0PinnedInputV1, ...] = Field(max_length=1024)
    environment: tuple[tuple[str, str], ...] = Field(max_length=128)
    replay_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    timeout_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def _command_is_closed(self) -> "ARL0GateCommandPinV1":
        executable = Path(self.executable_path)
        working = Path(self.working_directory)
        if (
            not executable.is_absolute()
            or str(executable) != os.path.normpath(self.executable_path)
            or not working.is_absolute()
            or str(working) != os.path.normpath(self.working_directory)
            or any("\x00" in item for item in self.arguments)
            or self.pinned_inputs
            != tuple(sorted(self.pinned_inputs, key=lambda item: item.absolute_path))
            or len({item.absolute_path for item in self.pinned_inputs}) != len(self.pinned_inputs)
            or self.environment != tuple(sorted(self.environment))
            or len({key for key, _value in self.environment}) != len(self.environment)
            or any(
                not key
                or not key.replace("_", "A").isalnum()
                or not key[0].isalpha()
                or "\x00" in value
                for key, value in self.environment
            )
        ):
            raise ValueError("ARL-0 gate command pin is noncanonical")
        return self

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class ARL0GateCommandReceiptV1(KernelModel):
    schema_name: Literal["aletheia.arl0_gate_command_receipt"] = (
        "aletheia.arl0_gate_command_receipt"
    )
    schema_version: Literal[1] = 1
    gate_kind: ARL0GateKind
    evaluated_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    stderr_sha256: str = Field(pattern=_SHA256_PATTERN)
    verified_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    completed_at: AwareDatetime
    return_code: Literal[0] = 0
    passed: Literal[True] = True
    synthetic_evidence: Literal[False] = False


class SubprocessARL0GateReplayPort:
    """Run exact, digest-pinned gate commands without a shell or inherited environment."""

    def __init__(
        self,
        pins: tuple[ARL0GateCommandPinV1, ...],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        gate_order = {kind: index for index, kind in enumerate(ARL0GateKind)}
        if (
            not pins
            or pins != tuple(sorted(pins, key=lambda item: gate_order[item.gate_kind]))
            or len({item.gate_kind for item in pins}) != len(pins)
        ):
            raise ValueError("ARL-0 command pins must be nonempty, unique, and canonical")
        self.pins = pins
        self._by_gate = {item.gate_kind: item for item in pins}
        self._clock = clock

    @staticmethod
    def _read_pinned_file(path_value: str, expected_sha256: str) -> bytes:
        path = Path(path_value)
        try:
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
                    or before.st_size < 1
                    or before.st_size > _MAX_ARCHIVE_OBJECT_BYTES
                ):
                    raise ARL1SourceVerificationError("ARL-0 pinned file custody differs")
                payload = b""
                while len(payload) < before.st_size:
                    chunk = os.read(descriptor, min(1024**2, before.st_size - len(payload)))
                    if not chunk:
                        raise ARL1SourceVerificationError("ARL-0 pinned file was truncated")
                    payload += chunk
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except ARL1SourceVerificationError:
            raise
        except OSError as exc:
            raise ARL1SourceVerificationError("ARL-0 pinned file is missing or unsafe") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ARL1SourceVerificationError("ARL-0 pinned file failed fresh rehash")
        return payload

    def _execute(self, pin: ARL0GateCommandPinV1) -> tuple[bytes, bytes]:
        self._read_pinned_file(pin.executable_path, pin.executable_sha256)
        for item in pin.pinned_inputs:
            self._read_pinned_file(item.absolute_path, item.content_sha256)
        working = Path(pin.working_directory)
        try:
            working_stat = working.stat(follow_symlinks=False)
        except OSError as exc:
            raise ARL1SourceVerificationError("ARL-0 working directory is unavailable") from exc
        if (
            not stat.S_ISDIR(working_stat.st_mode)
            or working.is_symlink()
            or working_stat.st_mode & 0o022
        ):
            raise ARL1SourceVerificationError("ARL-0 working directory custody differs")
        try:
            completed = subprocess.run(  # noqa: S603 - executable and argv are digest pinned
                (pin.executable_path, *pin.arguments),
                cwd=pin.working_directory,
                env=dict(pin.environment),
                check=False,
                capture_output=True,
                timeout=pin.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ARL1SourceVerificationError("ARL-0 gate command failed to execute") from exc
        if (
            completed.returncode != 0
            or len(completed.stdout) > 1024**2
            or len(completed.stderr) > 1024**2
        ):
            raise ARL1SourceVerificationError("ARL-0 gate command failed or exceeded output bounds")
        self._read_pinned_file(pin.executable_path, pin.executable_sha256)
        for item in pin.pinned_inputs:
            self._read_pinned_file(item.absolute_path, item.content_sha256)
        return completed.stdout, completed.stderr

    @staticmethod
    def _parse_canonical(payload: bytes, model_type: type[BaseModel]) -> BaseModel:
        try:
            value = model_type.model_validate_json(payload)
        except (TypeError, ValueError) as exc:
            raise ARL1SourceVerificationError("ARL-0 command evidence is invalid") from exc
        if payload != canonical_json_bytes(value):
            raise ARL1SourceVerificationError("ARL-0 command evidence is not canonical JSON")
        return value

    def replay_gate(
        self,
        *,
        gate: ARL0GateEvidenceV1,
        evidence_artifact: bytes,
        verification_receipt: bytes,
        observed_at: datetime,
    ) -> ARL0GateReplayProjectionV1:
        result = self._parse_canonical(evidence_artifact, ARL0GateCommandResultV1)
        receipt = self._parse_canonical(
            verification_receipt,
            ARL0GateCommandReceiptV1,
        )
        if not isinstance(result, ARL0GateCommandResultV1) or not isinstance(
            receipt, ARL0GateCommandReceiptV1
        ):
            raise ARL1SourceVerificationError("ARL-0 command evidence type differs")
        pin = self._by_gate.get(gate.gate_kind)
        if (
            pin is None
            or pin.evaluated_scope_sha256 != gate.evaluated_scope_sha256
            or receipt.gate_kind is not gate.gate_kind
            or receipt.evaluated_scope_sha256 != gate.evaluated_scope_sha256
            or receipt.command_pin_sha256 != pin.pin_sha256
            or receipt.result_sha256 != gate.evidence_artifact_sha256
            or receipt.verified_by_principal_id != gate.verified_by_principal_id
            or result.gate_kind is not gate.gate_kind
            or result.evaluated_scope_sha256 != gate.evaluated_scope_sha256
            or receipt.completed_at != gate.verified_at
            or receipt.completed_at > observed_at
        ):
            raise ARL1SourceVerificationError("ARL-0 command receipt rebound its gate or pin")
        stdout, stderr = self._execute(pin)
        if (
            stdout != evidence_artifact
            or hashlib.sha256(stderr).hexdigest() != receipt.stderr_sha256
        ):
            raise ARL1SourceVerificationError("fresh ARL-0 gate output differs from evidence")
        return ARL0GateReplayProjectionV1(
            gate_kind=gate.gate_kind,
            gate_evidence_sha256=gate.evidence_sha256,
            evaluated_scope_sha256=gate.evaluated_scope_sha256,
            evidence_artifact_sha256=gate.evidence_artifact_sha256,
            verification_receipt_sha256=gate.verification_receipt_sha256,
            replayed_by_principal_id=gate.verified_by_principal_id,
            replayed_at=observed_at,
        )

    def capture_gate(
        self,
        gate_kind: ARL0GateKind,
    ) -> tuple[ARL0GateEvidenceV1, bytes, bytes]:
        """Execute once and return the exact artifact/receipt bytes to retain before replay."""

        pin = self._by_gate.get(gate_kind)
        if pin is None:
            raise ARL1SourceVerificationError("ARL-0 gate has no command pin")
        completed_at = self._clock()
        if completed_at.tzinfo is None or completed_at.utcoffset() != timedelta(0):
            raise ARL1SourceVerificationError("ARL-0 gate clock must return UTC")
        stdout, stderr = self._execute(pin)
        result = self._parse_canonical(stdout, ARL0GateCommandResultV1)
        if (
            not isinstance(result, ARL0GateCommandResultV1)
            or result.gate_kind is not gate_kind
            or result.evaluated_scope_sha256 != pin.evaluated_scope_sha256
        ):
            raise ARL1SourceVerificationError("ARL-0 gate command returned another scope")
        receipt = ARL0GateCommandReceiptV1(
            gate_kind=gate_kind,
            evaluated_scope_sha256=pin.evaluated_scope_sha256,
            command_pin_sha256=pin.pin_sha256,
            result_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            verified_by_principal_id=pin.replay_principal_id,
            completed_at=completed_at,
        )
        receipt_bytes = canonical_json_bytes(receipt)
        gate = ARL0GateEvidenceV1(
            gate_kind=gate_kind,
            evaluated_scope_sha256=pin.evaluated_scope_sha256,
            evidence_artifact_sha256=hashlib.sha256(stdout).hexdigest(),
            verification_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            verified_by_principal_id=pin.replay_principal_id,
            verified_at=completed_at,
        )
        return gate, stdout, receipt_bytes


class ResearchActionAuthorityPort(Protocol):
    def verify_action_protocol_binding(self, *, binding: object, observed_at: datetime) -> str: ...


class RawRunCustodyPort(Protocol):
    def verify_raw_run_custody(
        self, *, raw_run: object, observed_at: datetime
    ) -> VerifiedRawRunCustodyProjection: ...


class CommittedValidationSourcePort(Protocol):
    def load_committed_validation(
        self, *, quest_id: str, action_sha256: str, scientific_slot_id: str
    ) -> object: ...


class ResearchKernelAuditPort(Protocol):
    def audit(self, quest_id: str, *, expected_scope_binding: object | None = None) -> object: ...


@dataclass(frozen=True)
class _ExpectedArchiveObject:
    object_sha256: str
    canonical_json: bool
    payload: bytes | None


def _typed(value: BaseModel) -> _ExpectedArchiveObject:
    payload = canonical_json_bytes(value)
    return _ExpectedArchiveObject(
        object_sha256=hashlib.sha256(payload).hexdigest(),
        canonical_json=True,
        payload=payload,
    )


def _raw(object_sha256: str) -> _ExpectedArchiveObject:
    return _ExpectedArchiveObject(
        object_sha256=object_sha256,
        canonical_json=False,
        payload=None,
    )


def _arl0_scope_sha256(evidence: ARL0IntegrityEvidenceV1) -> str:
    return canonical_sha256(
        {
            "source_tree_sha256": evidence.source_tree_sha256,
            "environment_lock_sha256": evidence.environment_lock_sha256,
            "schema_revision": evidence.schema_revision,
            "database_schema_verification_receipt_sha256": (
                evidence.database_schema_verification_receipt_sha256
            ),
            "gate_evidence_sha256s": tuple(item.evidence_sha256 for item in evidence.gates),
        }
    )


def _campaign_scope_sha256(evidence: ARL1ProtocolCampaignEvidenceV1) -> str:
    binding = evidence.replicate_executions[0].authorization.message.action_protocol_binding
    return canonical_sha256(
        {
            "quest_id": binding.action.quest_id,
            "action_sha256": binding.action.object_sha256,
            "protocol_sha256": evidence.compilation_request.protocol.protocol_sha256,
            "work_order_node_id": evidence.work_order_node_id,
        }
    )


def _arl0_expected(evidence: ARL0IntegrityEvidenceV1) -> dict[str, _ExpectedArchiveObject]:
    expected: dict[str, _ExpectedArchiveObject] = {
        "arl0:source_tree": _raw(evidence.source_tree_sha256),
        "arl0:environment_lock": _raw(evidence.environment_lock_sha256),
        "arl0:database_schema_verification_receipt": _raw(
            evidence.database_schema_verification_receipt_sha256
        ),
    }
    for gate in evidence.gates:
        suffix = gate.gate_kind.value
        expected[f"arl0:gate_evidence:{suffix}"] = _typed(gate)
        expected[f"arl0:gate_artifact:{suffix}"] = _raw(gate.evidence_artifact_sha256)
        expected[f"arl0:gate_verification_receipt:{suffix}"] = _raw(
            gate.verification_receipt_sha256
        )
    return expected


def _campaign_expected(
    evidence: ARL1ProtocolCampaignEvidenceV1,
) -> dict[str, _ExpectedArchiveObject]:
    expected: dict[str, _ExpectedArchiveObject] = {
        "campaign:compilation_request": _typed(evidence.compilation_request),
        "campaign:compilation_result": _typed(evidence.compilation_result),
        "campaign:registration": _typed(evidence.campaign_registration),
        "campaign:all_attempts_manifest": _typed(evidence.all_attempts_manifest),
        "campaign:reproduction_receipt": _typed(evidence.reproduction_receipt),
        "campaign:committed_admission": _typed(evidence.committed_admission),
        "campaign:incorporation_event": _typed(evidence.incorporation_event),
    }
    for replicate in evidence.replicate_executions:
        suffix = f"slot-{replicate.slot_index:03d}"
        expected[f"campaign:replicate:{suffix}"] = _typed(replicate)
        expected[f"campaign:authorization:{suffix}"] = _typed(replicate.authorization)
        expected[f"campaign:registration_receipt:{suffix}"] = _typed(replicate.registration_receipt)
        expected[f"campaign:raw_run:{suffix}"] = _typed(replicate.raw_run)
        expected[f"campaign:raw_run_custody:{suffix}"] = _typed(replicate.raw_run_custody)
        expected[f"campaign:committed_validation:{suffix}"] = _typed(replicate.committed_validation)
    return expected


def _bundle_expected(
    *,
    policy: ARL1QualificationPolicyV1,
    arl0_integrity: ARL0IntegrityEvidenceV1,
    target_campaign_request: QualificationTargetCampaignRequestV1,
    target_campaign_receipt: QualificationTargetCampaignReceiptV1,
    protocol_campaigns: tuple[ARL1ProtocolCampaignEvidenceV1, ...],
) -> dict[str, _ExpectedArchiveObject]:
    expected = {
        "bundle:policy": _typed(policy),
        "bundle:arl0_integrity": _typed(arl0_integrity),
        "bundle:arl0_archive_manifest": _typed_sha(arl0_integrity.evidence_archive_manifest_sha256),
        "bundle:target_campaign_request": _typed(target_campaign_request),
        "bundle:target_campaign_receipt": _typed(target_campaign_receipt),
    }
    for index, campaign in enumerate(protocol_campaigns, start=1):
        suffix = f"campaign-{index:03d}"
        expected[f"bundle:protocol_campaign:{suffix}"] = _typed(campaign)
        expected[f"bundle:protocol_archive_manifest:{suffix}"] = _typed_sha(
            campaign.source_evidence_archive_manifest_sha256
        )
    return expected


def _typed_sha(object_sha256: str) -> _ExpectedArchiveObject:
    return _ExpectedArchiveObject(
        object_sha256=object_sha256,
        canonical_json=True,
        payload=None,
    )


def _retain_expected(
    archive: LocalARL1EvidenceArchive,
    expected: Mapping[str, _ExpectedArchiveObject],
    *,
    raw_objects: Mapping[str, bytes] | None = None,
) -> tuple[ARL1EvidenceArchiveEntryV1, ...]:
    supplied = dict(raw_objects or {})
    entries: list[ARL1EvidenceArchiveEntryV1] = []
    for object_kind, item in sorted(expected.items()):
        payload = (
            item.payload if item.payload is not None else supplied.pop(item.object_sha256, None)
        )
        if payload is None:
            payload = archive.load_bytes(item.object_sha256)
            entry = ARL1EvidenceArchiveEntryV1(
                object_kind=object_kind,
                object_sha256=item.object_sha256,
                byte_length=len(payload),
                canonical_json=item.canonical_json,
            )
        else:
            entry = archive.publish_bytes(
                object_kind=object_kind,
                payload=payload,
                canonical_json=item.canonical_json,
            )
        if entry.object_sha256 != item.object_sha256:
            raise ARL1SourceVerificationError("ARL-1 retained source differs from its contract")
        entries.append(entry)
    if supplied:
        raise ARL1SourceVerificationError("unreferenced bytes were supplied to ARL-1 archive")
    return tuple(entries)


def retain_arl0_evidence_archive(
    archive: LocalARL1EvidenceArchive,
    evidence: ARL0IntegrityEvidenceV1,
    *,
    raw_objects: Mapping[str, bytes],
    retained_at: datetime,
) -> ARL1EvidenceArchiveManifestV1:
    manifest = build_arl0_evidence_archive_manifest(
        evidence,
        raw_objects=raw_objects,
        retained_at=retained_at,
    )
    entries = _retain_expected(archive, _arl0_expected(evidence), raw_objects=raw_objects)
    if entries != manifest.entries:
        raise ARL1SourceVerificationError("ARL-0 archive publication changed its manifest")
    if manifest.manifest_sha256 != evidence.evidence_archive_manifest_sha256:
        raise ARL1SourceVerificationError(
            "ARL-0 evidence must bind the exact archive manifest before publication"
        )
    archive.publish_manifest(manifest)
    return manifest


def build_arl0_evidence_archive_manifest(
    evidence: ARL0IntegrityEvidenceV1,
    *,
    raw_objects: Mapping[str, bytes],
    retained_at: datetime,
) -> ARL1EvidenceArchiveManifestV1:
    expected = _arl0_expected(evidence)
    required_raw = {item.object_sha256 for item in expected.values() if item.payload is None}
    if set(raw_objects) != required_raw or any(
        hashlib.sha256(raw_objects[digest]).hexdigest() != digest for digest in required_raw
    ):
        raise ARL1SourceVerificationError("ARL-0 raw archive inputs are incomplete or rebound")
    return ARL1EvidenceArchiveManifestV1(
        manifest_kind=ARL1ArchiveManifestKind.ARL0_INTEGRITY,
        scope_sha256=_arl0_scope_sha256(evidence),
        entries=tuple(
            ARL1EvidenceArchiveEntryV1(
                object_kind=kind,
                object_sha256=item.object_sha256,
                byte_length=(
                    len(item.payload)
                    if item.payload is not None
                    else len(raw_objects[item.object_sha256])
                ),
                canonical_json=item.canonical_json,
            )
            for kind, item in sorted(_arl0_expected(evidence).items())
        ),
        retained_at=retained_at,
    )


def retain_protocol_campaign_archive(
    archive: LocalARL1EvidenceArchive,
    evidence: ARL1ProtocolCampaignEvidenceV1,
    *,
    retained_at: datetime,
) -> ARL1EvidenceArchiveManifestV1:
    manifest = build_protocol_campaign_archive_manifest(
        evidence,
        retained_at=retained_at,
    )
    entries = _retain_expected(archive, _campaign_expected(evidence))
    if entries != manifest.entries:
        raise ARL1SourceVerificationError("protocol archive publication changed its manifest")
    if manifest.manifest_sha256 != evidence.source_evidence_archive_manifest_sha256:
        raise ARL1SourceVerificationError(
            "protocol campaign must bind the exact archive manifest before publication"
        )
    archive.publish_manifest(manifest)
    return manifest


def build_protocol_campaign_archive_manifest(
    evidence: ARL1ProtocolCampaignEvidenceV1,
    *,
    retained_at: datetime,
) -> ARL1EvidenceArchiveManifestV1:
    expected = _campaign_expected(evidence)
    if any(item.payload is None for item in expected.values()):  # pragma: no cover - closed list
        raise ARL1SourceVerificationError("protocol archive contains an untyped source")
    return ARL1EvidenceArchiveManifestV1(
        manifest_kind=ARL1ArchiveManifestKind.PROTOCOL_CAMPAIGN,
        scope_sha256=_campaign_scope_sha256(evidence),
        entries=tuple(
            ARL1EvidenceArchiveEntryV1(
                object_kind=kind,
                object_sha256=item.object_sha256,
                byte_length=len(item.payload or b""),
                canonical_json=item.canonical_json,
            )
            for kind, item in sorted(expected.items())
        ),
        retained_at=retained_at,
    )


def retain_bundle_evidence_archive(
    archive: LocalARL1EvidenceArchive,
    *,
    policy: ARL1QualificationPolicyV1,
    arl0_integrity: ARL0IntegrityEvidenceV1,
    target_campaign_request: QualificationTargetCampaignRequestV1,
    target_campaign_receipt: QualificationTargetCampaignReceiptV1,
    protocol_campaigns: tuple[ARL1ProtocolCampaignEvidenceV1, ...],
    retained_at: datetime,
) -> ARL1EvidenceArchiveManifestV1:
    manifest = ARL1EvidenceArchiveManifestV1(
        manifest_kind=ARL1ArchiveManifestKind.EVIDENCE_BUNDLE,
        scope_sha256=policy.policy_sha256,
        entries=_retain_expected(
            archive,
            _bundle_expected(
                policy=policy,
                arl0_integrity=arl0_integrity,
                target_campaign_request=target_campaign_request,
                target_campaign_receipt=target_campaign_receipt,
                protocol_campaigns=protocol_campaigns,
            ),
        ),
        retained_at=retained_at,
    )
    archive.publish_manifest(manifest)
    return manifest


def _verify_manifest(
    archive: LocalARL1EvidenceArchive,
    *,
    manifest_sha256: str,
    manifest_kind: ARL1ArchiveManifestKind,
    scope_sha256: str,
    expected: Mapping[str, _ExpectedArchiveObject],
    observed_at: datetime,
) -> tuple[ARL1EvidenceArchiveManifestV1, dict[str, bytes]]:
    manifest = archive.load_model(manifest_sha256, ARL1EvidenceArchiveManifestV1)
    if not isinstance(manifest, ARL1EvidenceArchiveManifestV1):  # pragma: no cover - typed loader
        raise ARL1SourceVerificationError("ARL-1 archive returned another manifest type")
    if (
        manifest.manifest_sha256 != manifest_sha256
        or manifest.manifest_kind is not manifest_kind
        or manifest.scope_sha256 != scope_sha256
        or manifest.retained_at > observed_at
        or set(expected) != {item.object_kind for item in manifest.entries}
    ):
        raise ARL1SourceVerificationError("ARL-1 archive manifest scope or coverage differs")
    payloads: dict[str, bytes] = {}
    for entry in manifest.entries:
        item = expected[entry.object_kind]
        if (
            entry.object_sha256 != item.object_sha256
            or entry.canonical_json is not item.canonical_json
        ):
            raise ARL1SourceVerificationError("ARL-1 archive entry identity differs")
        fresh = archive.load_entry(entry).payload
        if item.payload is not None and fresh != item.payload:
            raise ARL1SourceVerificationError("ARL-1 canonical source bytes differ")
        payloads[entry.object_kind] = fresh
    return manifest, payloads


class ARL1EvidenceBundleSourceV1(KernelModel):
    """Evidence before independent receipts are issued; avoids circular self-attestation."""

    schema_name: Literal["aletheia.arl1_evidence_bundle_source"] = (
        "aletheia.arl1_evidence_bundle_source"
    )
    schema_version: Literal[1] = 1
    policy: ARL1QualificationPolicyV1
    arl0_integrity: ARL0IntegrityEvidenceV1
    target_campaign_request: QualificationTargetCampaignRequestV1
    target_campaign_receipt: QualificationTargetCampaignReceiptV1
    protocol_campaigns: tuple[ARL1ProtocolCampaignEvidenceV1, ...] = Field(
        min_length=1, max_length=100
    )
    evidence_archive_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _source_is_ordered(self) -> "ARL1EvidenceBundleSourceV1":
        if tuple(item.campaign_id for item in self.protocol_campaigns) != tuple(
            sorted(item.campaign_id for item in self.protocol_campaigns)
        ):
            raise ValueError("ARL-1 source campaigns are not canonical")
        return self


class PostgreSQLARL1EvidenceVerifier:
    """Independent verifier over archive bytes, PostgreSQL receipts and Kernel replay."""

    def __init__(
        self,
        *,
        archive: LocalARL1EvidenceArchive,
        gate_replayer: ARL0GateReplayPort,
        sessions: sessionmaker[Session],
        action_authority: ResearchActionAuthorityPort,
        raw_run_custody: RawRunCustodyPort,
        committed_validation_source: CommittedValidationSourcePort,
        observation_verification: CommittedValidationSourceVerificationContext,
        kernel_store: ResearchKernelAuditPort,
        trusted_verifier_pins: tuple[ARL1EvidenceVerifierPinV1, ...],
        signing_private_key: bytes | None = None,
        signing_pin_sha256: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if (
            not trusted_verifier_pins
            or trusted_verifier_pins
            != tuple(sorted(trusted_verifier_pins, key=lambda item: item.pin_sha256))
            or len({item.pin_sha256 for item in trusted_verifier_pins})
            != len(trusted_verifier_pins)
        ):
            raise ValueError("trusted ARL-1 verifier pins must be nonempty and canonical")
        self.archive = archive
        self.gate_replayer = gate_replayer
        self.sessions = sessions
        self.action_authority = action_authority
        self.raw_run_custody = raw_run_custody
        self.committed_validation_source = committed_validation_source
        self.observation_verification = observation_verification
        self.kernel_store = kernel_store
        self.trusted_verifier_pins = trusted_verifier_pins
        self._pins = {item.pin_sha256: item for item in trusted_verifier_pins}
        self._signing_private_key = signing_private_key
        self._signing_pin = self._pins.get(signing_pin_sha256 or "")
        self._clock = clock
        if (signing_private_key is None) != (self._signing_pin is None):
            raise ValueError("ARL-1 verifier signing key and exact pin must be configured together")

    def _now(self) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
            raise ARL1SourceVerificationError("ARL-1 verifier clock must return UTC")
        return observed_at

    def _trusted_policy_pin(
        self,
        policy: ARL1QualificationPolicyV1,
        receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1EvidenceVerifierPinV1:
        matches = tuple(
            item
            for item in policy.evidence_verifier_pins
            if item.principal_id == receipt.verified_by_principal_id
            and item.key_id == receipt.verification_key_id
            and item.verification_policy_sha256 == receipt.verification_policy_sha256
        )
        if len(matches) != 1 or matches[0].pin_sha256 not in self._pins:
            raise ARL1SourceVerificationError(
                "ARL-1 receipt signer is not in the verifier's out-of-band trust set"
            )
        if self._pins[matches[0].pin_sha256] != matches[0]:
            raise ARL1SourceVerificationError("ARL-1 verifier pin bytes differ from policy")
        return matches[0]

    def _verify_retained(
        self,
        *,
        receipt: ARL1SourceVerificationReceiptV1,
        policy: ARL1QualificationPolicyV1,
        subject_kind: ARL1VerificationSubjectKind,
        subject_sha256: str,
        observed_at: datetime,
    ) -> ARL1SourceVerificationReceiptV1:
        if receipt.subject_kind is not subject_kind or receipt.subject_sha256 != subject_sha256:
            raise ARL1SourceVerificationError("ARL-1 retained receipt rebound its subject")
        pin = self._trusted_policy_pin(policy, receipt)
        return verify_arl1_source_verification_receipt(
            receipt,
            verifier_pin=pin,
            observed_at=observed_at,
        )

    def _issue(
        self,
        *,
        policy: ARL1QualificationPolicyV1,
        subject_kind: ARL1VerificationSubjectKind,
        subject_sha256: str,
        verified_at: datetime,
    ) -> ARL1SourceVerificationReceiptV1:
        pin = self._signing_pin
        private_key = self._signing_private_key
        if pin is None or private_key is None or pin not in policy.evidence_verifier_pins:
            raise ARL1SourceVerificationError(
                "ARL-1 source verifier lacks an exact policy-approved signing authority"
            )
        return issue_arl1_source_verification_receipt(
            subject_kind=subject_kind,
            subject_sha256=subject_sha256,
            verifier_pin=pin,
            verifier_private_key=private_key,
            verified_at=verified_at,
        )

    def _verify_arl0_sources(
        self,
        evidence: ARL0IntegrityEvidenceV1,
        *,
        observed_at: datetime,
    ) -> None:
        _manifest, payloads = _verify_manifest(
            self.archive,
            manifest_sha256=evidence.evidence_archive_manifest_sha256,
            manifest_kind=ARL1ArchiveManifestKind.ARL0_INTEGRITY,
            scope_sha256=_arl0_scope_sha256(evidence),
            expected=_arl0_expected(evidence),
            observed_at=observed_at,
        )
        for gate in evidence.gates:
            suffix = gate.gate_kind.value
            projection = self.gate_replayer.replay_gate(
                gate=gate,
                evidence_artifact=payloads[f"arl0:gate_artifact:{suffix}"],
                verification_receipt=payloads[f"arl0:gate_verification_receipt:{suffix}"],
                observed_at=observed_at,
            )
            expected = ARL0GateReplayProjectionV1(
                gate_kind=gate.gate_kind,
                gate_evidence_sha256=gate.evidence_sha256,
                evaluated_scope_sha256=gate.evaluated_scope_sha256,
                evidence_artifact_sha256=gate.evidence_artifact_sha256,
                verification_receipt_sha256=gate.verification_receipt_sha256,
                replayed_by_principal_id=gate.verified_by_principal_id,
                replayed_at=observed_at,
            )
            if projection != expected:
                raise ARL1SourceVerificationError("ARL-0 gate replay returned another result")

    @staticmethod
    def _stable_custody(
        projection: VerifiedRawRunCustodyProjection,
    ) -> dict[str, object]:
        return projection.model_dump(mode="python", exclude={"verified_at"})

    def _verify_protocol_sources(
        self,
        evidence: ARL1ProtocolCampaignEvidenceV1,
        *,
        observed_at: datetime,
    ) -> ARL1EvidenceArchiveManifestV1:
        manifest, _payloads = _verify_manifest(
            self.archive,
            manifest_sha256=evidence.source_evidence_archive_manifest_sha256,
            manifest_kind=ARL1ArchiveManifestKind.PROTOCOL_CAMPAIGN,
            scope_sha256=_campaign_scope_sha256(evidence),
            expected=_campaign_expected(evidence),
            observed_at=observed_at,
        )
        verify_compilation(evidence.compilation_request, evidence.compilation_result)
        for replicate in evidence.replicate_executions:
            authorization = ScientificExecutionAuthorization.model_validate(
                replicate.authorization.model_dump(mode="python")
            )
            binding = authorization.message.action_protocol_binding
            if (
                self.action_authority.verify_action_protocol_binding(
                    binding=binding,
                    observed_at=observed_at,
                )
                != binding.binding_sha256
            ):
                raise ARL1SourceVerificationError("ARL-1 action authority returned another binding")
            with self.sessions() as session:
                row = get_scientific_execution_authorization_by_slot(
                    session,
                    quest_id=binding.action.quest_id,
                    scientific_slot_id=authorization.message.scientific_slot_id,
                )
            if row is None or row != ScientificExecutionAuthorizationWrite.from_contract(
                authorization,
                registered_at=row.registered_at,
            ):
                raise ARL1SourceVerificationError("ARL-1 SEA registration row differs")
            fresh_custody = self.raw_run_custody.verify_raw_run_custody(
                raw_run=replicate.raw_run,
                observed_at=observed_at,
            )
            if self._stable_custody(fresh_custody) != self._stable_custody(
                replicate.raw_run_custody
            ):
                raise ARL1SourceVerificationError("ARL-1 raw-run custody changed")
            committed = self.committed_validation_source.load_committed_validation(
                quest_id=binding.action.quest_id,
                action_sha256=binding.action.object_sha256,
                scientific_slot_id=replicate.scientific_slot_id,
            )
            if committed != replicate.committed_validation:
                raise ARL1SourceVerificationError("ARL-1 committed validation source changed")
        self._verify_primary_admission(evidence=evidence, observed_at=observed_at)
        return manifest

    def _verify_primary_admission(
        self,
        *,
        evidence: ARL1ProtocolCampaignEvidenceV1,
        observed_at: datetime,
    ) -> None:
        primary = next(
            item
            for item in evidence.replicate_executions
            if item.scientific_slot_id == evidence.scientific_slot_id
        )
        binding = primary.authorization.message.action_protocol_binding
        committed = CommittedObservationAdmission.model_validate(
            evidence.committed_admission.model_dump(mode="python")
        )
        context = self.observation_verification
        verify_committed_observation_admission(
            committed_admission=committed,
            qualification_authority=context.qualification_authority,
            action_authority=context.action_authority,
            qualification_custody=context.qualification_custody,
            raw_run_custody=context.raw_run_custody,
            validation_campaign_custody=context.validation_campaign_custody,
            execution_authority_pin=context.execution_authority_pin,
            validator_authority_pin=context.validator_authority_pin,
            admission_authority_pin=context.admission_authority_pin,
            database_authority_pin=context.database_authority_pin,
            observed_at=observed_at,
        )
        with self.sessions() as session:
            row = get_observation_admission_by_slot(
                session,
                quest_id=binding.action.quest_id,
                scientific_slot_id=evidence.scientific_slot_id,
            )
        expected = ObservationAdmissionWrite.from_contract(
            committed,
            quest_id=binding.action.quest_id,
            incorporated_event_sequence=evidence.incorporation_event.sequence,
            incorporated_event_sha256=evidence.incorporation_event.event_sha256,
            incorporated_event_type=evidence.incorporation_event.event_type.value,
        )
        if row is None or row != expected:
            raise ARL1SourceVerificationError("ARL-1 admission row differs from its Kernel event")
        audit = self.kernel_store.audit(
            binding.action.quest_id,
            expected_scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        )
        matching_events = tuple(
            event for event in audit.events if event == evidence.incorporation_event
        )
        matching_actions = tuple(
            action
            for action in audit.state.actions
            if action.action_ref == binding.action.object_ref
        )
        if (
            len(matching_events) != 1
            or len(matching_actions) != 1
            or matching_actions[0].lifecycle is not ActionLifecycle.APPLIED
            or matching_actions[0].decided_event_sha256 != evidence.incorporation_event.event_sha256
            or matching_actions[0].observation_evidence_ref
            != evidence.incorporation_event.payload.evidence_ref
        ):
            raise ARL1SourceVerificationError(
                "ARL-1 Kernel audit lacks the exact admitted observation"
            )

    def _verify_bundle_archive_sources(
        self,
        source: ARL1EvidenceBundleSourceV1,
        *,
        observed_at: datetime,
    ) -> ARL1EvidenceArchiveManifestV1:
        manifest, _payloads = _verify_manifest(
            self.archive,
            manifest_sha256=source.evidence_archive_manifest_sha256,
            manifest_kind=ARL1ArchiveManifestKind.EVIDENCE_BUNDLE,
            scope_sha256=source.policy.policy_sha256,
            expected=_bundle_expected(
                policy=source.policy,
                arl0_integrity=source.arl0_integrity,
                target_campaign_request=source.target_campaign_request,
                target_campaign_receipt=source.target_campaign_receipt,
                protocol_campaigns=source.protocol_campaigns,
            ),
            observed_at=observed_at,
        )
        return manifest

    def verify_arl0_integrity(
        self,
        *,
        evidence: ARL0IntegrityEvidenceV1,
        policy: ARL1QualificationPolicyV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1:
        observed_at = self._now()
        self._verify_arl0_sources(evidence, observed_at=observed_at)
        manifest = self.archive.load_model(
            evidence.evidence_archive_manifest_sha256,
            ARL1EvidenceArchiveManifestV1,
        )
        if (
            not isinstance(manifest, ARL1EvidenceArchiveManifestV1)
            or retained_receipt.verified_at < manifest.retained_at
        ):
            raise ARL1SourceVerificationError("ARL-0 receipt predates retained archive bytes")
        return self._verify_retained(
            receipt=retained_receipt,
            policy=policy,
            subject_kind=ARL1VerificationSubjectKind.ARL0_INTEGRITY,
            subject_sha256=evidence.integrity_sha256,
            observed_at=observed_at,
        )

    def verify_protocol_campaign(
        self,
        *,
        evidence: ARL1ProtocolCampaignEvidenceV1,
        policy: ARL1QualificationPolicyV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1:
        observed_at = self._now()
        manifest = self._verify_protocol_sources(evidence, observed_at=observed_at)
        if retained_receipt.verified_at < manifest.retained_at:
            raise ARL1SourceVerificationError(
                "ARL-1 protocol receipt predates retained archive bytes"
            )
        return self._verify_retained(
            receipt=retained_receipt,
            policy=policy,
            subject_kind=ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
            subject_sha256=evidence.campaign_sha256,
            observed_at=observed_at,
        )

    def verify_evidence_archive(
        self,
        *,
        bundle: ARL1EvidenceBundleV1,
        retained_receipt: ARL1SourceVerificationReceiptV1,
    ) -> ARL1SourceVerificationReceiptV1:
        observed_at = self._now()
        source = ARL1EvidenceBundleSourceV1(
            policy=bundle.policy,
            arl0_integrity=bundle.arl0_integrity,
            target_campaign_request=bundle.target_campaign_request,
            target_campaign_receipt=bundle.target_campaign_receipt,
            protocol_campaigns=bundle.protocol_campaigns,
            evidence_archive_manifest_sha256=bundle.evidence_archive_manifest_sha256,
        )
        manifest = self._verify_bundle_archive_sources(source, observed_at=observed_at)
        if retained_receipt.verified_at < manifest.retained_at:
            raise ARL1SourceVerificationError(
                "ARL-1 archive receipt predates retained archive bytes"
            )
        return self._verify_retained(
            receipt=retained_receipt,
            policy=bundle.policy,
            subject_kind=ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
            subject_sha256=bundle.evidence_archive_manifest_sha256,
            observed_at=observed_at,
        )

    def issue_source_receipts(
        self,
        source: ARL1EvidenceBundleSourceV1,
    ) -> tuple[ARL1SourceVerificationReceiptV1, ...]:
        """Freshly replay all sources, then sign their exact identities at one trusted time."""

        observed_at = self._now()
        verify_qualification_target_campaign_receipt(
            source.target_campaign_request,
            source.target_campaign_receipt,
        )
        self._verify_arl0_sources(source.arl0_integrity, observed_at=observed_at)
        for campaign in source.protocol_campaigns:
            self._verify_protocol_sources(campaign, observed_at=observed_at)
        self._verify_bundle_archive_sources(source, observed_at=observed_at)
        receipts = [
            self._issue(
                policy=source.policy,
                subject_kind=ARL1VerificationSubjectKind.ARL0_INTEGRITY,
                subject_sha256=source.arl0_integrity.integrity_sha256,
                verified_at=observed_at,
            ),
            self._issue(
                policy=source.policy,
                subject_kind=ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
                subject_sha256=source.evidence_archive_manifest_sha256,
                verified_at=observed_at,
            ),
        ]
        receipts.extend(
            self._issue(
                policy=source.policy,
                subject_kind=ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
                subject_sha256=campaign.campaign_sha256,
                verified_at=observed_at,
            )
            for campaign in source.protocol_campaigns
        )
        return tuple(
            sorted(receipts, key=lambda item: (item.subject_kind.value, item.subject_sha256))
        )


def prepare_arl1_evidence_bundle(
    source: ARL1EvidenceBundleSourceV1,
    *,
    source_verifier: PostgreSQLARL1EvidenceVerifier,
) -> ARL1EvidenceBundleV1:
    receipts = source_verifier.issue_source_receipts(source)
    prepared_at = max(
        source.arl0_integrity.completed_at,
        source.target_campaign_receipt.completed_at,
        *(item.report.reported_at for item in source.protocol_campaigns),
        *(item.verified_at for item in receipts),
    )
    return ARL1EvidenceBundleV1(
        policy=source.policy,
        arl0_integrity=source.arl0_integrity,
        target_campaign_request=source.target_campaign_request,
        target_campaign_receipt=source.target_campaign_receipt,
        protocol_campaigns=source.protocol_campaigns,
        source_verification_receipts=receipts,
        evidence_archive_manifest_sha256=source.evidence_archive_manifest_sha256,
        prepared_at=prepared_at,
    )


__all__ = [
    "ARL0GateCommandPinV1",
    "ARL0GateCommandReceiptV1",
    "ARL0GateCommandResultV1",
    "ARL0GateReplayPort",
    "ARL0GateReplayProjectionV1",
    "ARL0PinnedInputV1",
    "ARL1EvidenceBundleSourceV1",
    "ARL1SourceVerificationError",
    "FreshARL1ArchiveObject",
    "LocalARL1EvidenceArchive",
    "PostgreSQLARL1EvidenceVerifier",
    "SubprocessARL0GateReplayPort",
    "build_arl0_evidence_archive_manifest",
    "build_protocol_campaign_archive_manifest",
    "prepare_arl1_evidence_bundle",
    "retain_arl0_evidence_archive",
    "retain_bundle_evidence_archive",
    "retain_protocol_campaign_archive",
]
