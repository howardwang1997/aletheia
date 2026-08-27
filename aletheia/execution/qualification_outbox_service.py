"""Crash-replayable terminal-outbox spool for the qualification deployment.

The PR-4 database owns terminal authority.  This process has only the restricted outbox role: it
may read the two execution outboxes, durably mirror canonical envelopes into one private spool,
and perform the sole permitted legacy transition from ``pending`` to ``published``.  It cannot
allocate work, sign terminal evidence, enqueue Research Kernel tasks, or admit scientific state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from collections import Counter
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision, session_factory
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceHandlerSet,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    QualificationTerminalDeadlineExpiration,
)
from aletheia.execution.schemas import (
    ExecutionModel,
    ExecutionReceipt,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.execution.oci_runtime import host_parent_chain_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_OUTBOX_ID_PATTERN = r"^(?:xob|qto)_[0-9a-f]{64}$"
_MAX_ENVELOPE_BYTES = 4 * 1024 * 1024

_SELECT_LEGACY_OUTBOX = text(
    """
    SELECT outbox_id, receipt_sha256, execution_id, attempt_id, topic, delivery_key,
           payload_sha256, payload_json, status, publish_attempts, created_at, published_at
      FROM execution_outbox
     ORDER BY created_at, outbox_id
     LIMIT :limit
       FOR UPDATE
    """
)
_SELECT_QUALIFICATION_OUTBOX = text(
    """
    SELECT outbox_id, terminal_authority_kind, terminal_authority_sha256,
           accepted_terminal_submission_sha256, terminal_deadline_expiration_sha256,
           execution_id, attempt_id, topic, delivery_key, payload_sha256, payload_json,
           created_at
      FROM execution_qualification_terminal_outbox
     ORDER BY created_at, outbox_id
     LIMIT :limit
    """
)


class QualificationTerminalOutboxError(RuntimeError):
    """The database source, spool custody, or publication transition failed closed."""


class QualificationOutboxSpoolRootPinV1(ExecutionModel):
    """Exact pre-created private root owned only by the outbox process."""

    schema_name: Literal["aletheia.qualification_outbox_spool_root_pin"] = (
        "aletheia.qualification_outbox_spool_root_pin"
    )
    schema_version: Literal[1] = 1
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=1, le=2**31 - 1)
    owner_gid: int = Field(ge=1, le=2**31 - 1)
    mode: Literal[0o700] = 0o700
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _root_is_canonical(self) -> "QualificationOutboxSpoolRootPinV1":
        _absolute_path(self.path, label="qualification outbox spool root")
        return self


class QualificationTerminalOutboxServiceConfigV1(ExecutionModel):
    """Canonical process, database, spool, and bounded-scan configuration."""

    schema_name: Literal["aletheia.qualification_terminal_outbox_service_config"] = (
        "aletheia.qualification_terminal_outbox_service_config"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_config_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    postgresql_role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    spool_root: QualificationOutboxSpoolRootPinV1
    poll_milliseconds: int = Field(default=250, ge=50, le=60_000)
    maximum_source_rows_per_kind: int = Field(default=10_000, ge=1, le=1_000_000)
    prepared_at: AwareDatetime
    database_credentials_loaded: Literal[True] = True
    outbox_status_mutation_allowed: Literal[True] = True
    signing_private_key_loaded: Literal[False] = False
    execution_allocation_allowed: Literal[False] = False
    terminal_authority_mutation_allowed: Literal[False] = False
    durable_task_enqueue_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _config_is_utc(self) -> "QualificationTerminalOutboxServiceConfigV1":
        if self.prepared_at.utcoffset() != timedelta(0):
            raise ValueError("qualification terminal outbox preparation time must be UTC")
        return self


class QualificationTerminalSpoolEnvelopeV1(ExecutionModel):
    """Deterministic file envelope for one exact database outbox source."""

    schema_name: Literal["aletheia.qualification_terminal_spool_envelope"] = (
        "aletheia.qualification_terminal_spool_envelope"
    )
    schema_version: Literal[1] = 1
    source_kind: Literal["execution_terminal_v1", "qualification_terminal_v2"]
    outbox_id: str = Field(pattern=_OUTBOX_ID_PATTERN)
    terminal_authority_kind: Literal[
        "execution_receipt",
        "accepted_terminal_submission",
        "terminal_deadline_expiration",
    ]
    terminal_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    topic: Literal["execution.terminal.v1", "execution.qualification_terminal.v2"]
    delivery_key: str = Field(pattern=_IDENTITY_PATTERN)
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    payload: (
        ExecutionReceipt
        | AcceptedQualificationTerminalSubmission
        | QualificationTerminalDeadlineExpiration
    )
    source_created_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _source_is_exact(self) -> "QualificationTerminalSpoolEnvelopeV1":
        if (
            self.payload_sha256 != self.terminal_authority_sha256
            or canonical_sha256(self.payload) != self.payload_sha256
        ):
            raise ValueError("qualification terminal spool payload identity differs")
        if self.source_kind == "execution_terminal_v1":
            if (
                self.outbox_id
                != f"xob_{canonical_sha256({'receipt_sha256': self.terminal_authority_sha256})}"
                or self.terminal_authority_kind != "execution_receipt"
                or self.topic != "execution.terminal.v1"
                or self.delivery_key != f"execution:{self.execution_id}:{self.attempt_id}"
                or not isinstance(self.payload, ExecutionReceipt)
                or self.payload.execution_receipt_sha256 != self.terminal_authority_sha256
                or self.payload.intent.execution_id != self.execution_id
                or self.payload.intent.infrastructure_attempt.infrastructure_attempt_id
                != self.attempt_id
                or self.payload.verified_at > self.source_created_at
            ):
                raise ValueError("legacy execution terminal spool source is rebound")
        elif (
            self.outbox_id != f"qto_{self.terminal_authority_sha256}"
            or self.topic != "execution.qualification_terminal.v2"
            or self.delivery_key != f"execution-v2:{self.execution_id}:{self.attempt_id}"
            or self.payload.attempt_id != self.attempt_id
        ):
            raise ValueError("qualification-v2 terminal spool source is rebound")
        elif self.terminal_authority_kind == "accepted_terminal_submission":
            if (
                not isinstance(self.payload, AcceptedQualificationTerminalSubmission)
                or self.payload.accepted_terminal_submission_sha256
                != self.terminal_authority_sha256
                or self.payload.accepted_at > self.source_created_at
            ):
                raise ValueError("accepted terminal spool authority is rebound")
        elif (
            self.terminal_authority_kind != "terminal_deadline_expiration"
            or not isinstance(self.payload, QualificationTerminalDeadlineExpiration)
            or self.payload.terminal_deadline_expiration_sha256 != self.terminal_authority_sha256
            or self.payload.execution_id != self.execution_id
            or self.payload.expired_at > self.source_created_at
        ):
            raise ValueError("terminal-deadline spool authority is rebound")
        return self

    @property
    def envelope_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def filename(self) -> str:
        return f"{self.outbox_id}.json"


class QualificationTerminalOutboxTickReceipt(ExecutionModel):
    """Operational result of one bounded, replay-safe database/spool pass."""

    schema_name: Literal["aletheia.qualification_terminal_outbox_tick_receipt"] = (
        "aletheia.qualification_terminal_outbox_tick_receipt"
    )
    schema_version: Literal[1] = 1
    source_outbox_ids: tuple[str, ...]
    newly_spooled_outbox_ids: tuple[str, ...]
    replayed_spool_outbox_ids: tuple[str, ...]
    legacy_published_outbox_ids: tuple[str, ...]
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_canonical(self) -> "QualificationTerminalOutboxTickReceipt":
        for values in (
            self.source_outbox_ids,
            self.newly_spooled_outbox_ids,
            self.replayed_spool_outbox_ids,
            self.legacy_published_outbox_ids,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(
                    "qualification terminal outbox receipt identities are not canonical"
                )
        if set(self.newly_spooled_outbox_ids) & set(self.replayed_spool_outbox_ids):
            raise ValueError("a terminal outbox cannot be both newly spooled and replayed")
        if not (set(self.newly_spooled_outbox_ids) | set(self.replayed_spool_outbox_ids)) <= set(
            self.source_outbox_ids
        ) or not set(self.legacy_published_outbox_ids) <= set(self.source_outbox_ids):
            raise ValueError("qualification terminal outbox receipt escaped its source set")
        return self

    @property
    def work_performed(self) -> bool:
        return bool(self.newly_spooled_outbox_ids or self.legacy_published_outbox_ids)


def _absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or not path.is_absolute()
        or value != os.path.normpath(value)
        or value == "/"
    ):
        raise ValueError(f"{label} must be one canonical absolute path")
    return path


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate qualification terminal outbox config keys: {duplicates}")
    return dict(pairs)


def _load_config(configuration_bytes: bytes) -> QualificationTerminalOutboxServiceConfigV1:
    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=_unique_object)
        config = QualificationTerminalOutboxServiceConfigV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationTerminalOutboxError("qualification outbox config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise QualificationTerminalOutboxError("qualification outbox config is not canonical JSON")
    return config


def _bind_process(
    deployment: QualificationServiceProcessDeploymentV1,
    config: QualificationTerminalOutboxServiceConfigV1,
) -> QualificationServiceProcessDeploymentV1:
    try:
        process = QualificationServiceProcessDeploymentV1.model_validate(
            deployment.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationTerminalOutboxError(
            "qualification outbox process deployment is invalid"
        ) from exc
    spool = Path(config.spool_root.path)
    code = Path(process.reviewed_code_root)
    config_path = Path(process.composition_config_path)
    if (
        process.role is not QualificationServiceRole.OUTBOX
        or process.operation != "run"
        or process.worker_poll_milliseconds is not None
        or process.deployment_id != config.deployment_id
        or qualification_service_process_config_binding_sha256(process)
        != config.process_config_binding_sha256
        or (process.process_uid, process.process_gid)
        != (config.spool_root.owner_uid, config.spool_root.owner_gid)
        or spool == config_path
        or spool == code
        or spool in code.parents
        or code in spool.parents
        or spool in config_path.parents
        or config_path in spool.parents
    ):
        raise QualificationTerminalOutboxError(
            "qualification outbox config differs from its process deployment"
        )
    return process


def _verify_spool_root(pin: QualificationOutboxSpoolRootPinV1) -> Path:
    path = Path(pin.path)
    descriptor = -1
    try:
        if path.resolve(strict=True) != path:
            raise QualificationTerminalOutboxError("qualification outbox spool traverses a symlink")
        if host_parent_chain_sha256(path) != pin.parent_chain_sha256:
            raise QualificationTerminalOutboxError("qualification outbox parent chain changed")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        after = os.fstat(descriptor)
        parent_after = host_parent_chain_sha256(path)
    except (OSError, ValueError) as exc:
        raise QualificationTerminalOutboxError("qualification outbox spool is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    expected = (pin.device, pin.inode, pin.owner_uid, pin.owner_gid, pin.mode)
    observed = (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )
    if (
        not stat.S_ISDIR(before.st_mode)
        or observed != expected
        or (before.st_dev, before.st_ino, before.st_mode, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_ctime_ns)
        or parent_after != pin.parent_chain_sha256
    ):
        raise QualificationTerminalOutboxError("qualification outbox spool custody differs")
    return path


def _row_mapping(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise QualificationTerminalOutboxError("qualification outbox query returned another row")
    return row


def _legacy_envelope(row_value: object) -> tuple[QualificationTerminalSpoolEnvelopeV1, str, int]:
    row = _row_mapping(row_value)
    try:
        payload = ExecutionReceipt.model_validate(row["payload_json"])
        envelope = QualificationTerminalSpoolEnvelopeV1(
            source_kind="execution_terminal_v1",
            outbox_id=row["outbox_id"],
            terminal_authority_kind="execution_receipt",
            terminal_authority_sha256=row["receipt_sha256"],
            execution_id=row["execution_id"],
            attempt_id=row["attempt_id"],
            topic=row["topic"],
            delivery_key=row["delivery_key"],
            payload_sha256=row["payload_sha256"],
            payload=payload,
            source_created_at=row["created_at"],
        )
        status = row["status"]
        attempts = row["publish_attempts"]
        published_at = row["published_at"]
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationTerminalOutboxError("legacy execution outbox row is invalid") from exc
    if (
        status not in {"pending", "published"}
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 0
        or row["payload_json"] != payload.model_dump(mode="json")
        or (status == "pending" and published_at is not None)
        or (
            status == "published"
            and (attempts < 1 or published_at is None or published_at < envelope.source_created_at)
        )
    ):
        raise QualificationTerminalOutboxError("legacy execution outbox state is rebound")
    return envelope, status, attempts


def _qualification_envelope(row_value: object) -> QualificationTerminalSpoolEnvelopeV1:
    row = _row_mapping(row_value)
    try:
        kind = row["terminal_authority_kind"]
        if kind == "accepted_terminal_submission":
            payload: (
                AcceptedQualificationTerminalSubmission | QualificationTerminalDeadlineExpiration
            ) = AcceptedQualificationTerminalSubmission.model_validate(row["payload_json"])
            variant_is_exact = (
                row["accepted_terminal_submission_sha256"] == row["terminal_authority_sha256"]
                and row["terminal_deadline_expiration_sha256"] is None
            )
        elif kind == "terminal_deadline_expiration":
            payload = QualificationTerminalDeadlineExpiration.model_validate(row["payload_json"])
            variant_is_exact = (
                row["terminal_deadline_expiration_sha256"] == row["terminal_authority_sha256"]
                and row["accepted_terminal_submission_sha256"] is None
            )
        else:
            raise ValueError("unknown qualification terminal outbox kind")
        envelope = QualificationTerminalSpoolEnvelopeV1(
            source_kind="qualification_terminal_v2",
            outbox_id=row["outbox_id"],
            terminal_authority_kind=kind,
            terminal_authority_sha256=row["terminal_authority_sha256"],
            execution_id=row["execution_id"],
            attempt_id=row["attempt_id"],
            topic=row["topic"],
            delivery_key=row["delivery_key"],
            payload_sha256=row["payload_sha256"],
            payload=payload,
            source_created_at=row["created_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationTerminalOutboxError(
            "qualification-v2 terminal outbox row is invalid"
        ) from exc
    if not variant_is_exact or row["payload_json"] != payload.model_dump(mode="json"):
        raise QualificationTerminalOutboxError(
            "qualification-v2 terminal outbox variant is rebound"
        )
    return envelope


def qualification_terminal_spool_envelope_from_row(
    row_value: object,
) -> QualificationTerminalSpoolEnvelopeV1:
    """Rebuild one immutable v2 envelope from a read-only PostgreSQL row projection."""

    return _qualification_envelope(row_value)


def _spool_checkpoint(_phase: str, _path: Path) -> None:
    """Fault-injection seam; production deliberately performs no action."""


class QualificationTerminalOutboxSpool:
    """Private write-once hard-link spool with exact crash-residue recovery."""

    def __init__(self, pin: QualificationOutboxSpoolRootPinV1) -> None:
        self._pin = QualificationOutboxSpoolRootPinV1.model_validate(pin.model_dump(mode="python"))
        self.root = _verify_spool_root(self._pin)

    def revalidate(self) -> None:
        _verify_spool_root(self._pin)

    def acquire_service_lock(self) -> int:
        self.revalidate()
        try:
            root_fd = self._open_root()
            try:
                descriptor = os.open(
                    ".service.lock",
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_fd,
                )
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != self._pin.owner_uid
                    or metadata.st_gid != self._pin.owner_gid
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise QualificationTerminalOutboxError(
                        "qualification outbox service lock custody is unsafe"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fsync(root_fd)
                return descriptor
            finally:
                os.close(root_fd)
        except BlockingIOError as exc:
            raise QualificationTerminalOutboxError(
                "another qualification outbox service owns the spool"
            ) from exc
        except OSError as exc:
            raise QualificationTerminalOutboxError(
                "qualification outbox service lock is unavailable"
            ) from exc

    def retain(self, envelope: QualificationTerminalSpoolEnvelopeV1) -> bool:
        """Publish exact bytes once; return true only when this call creates the final link."""

        value = QualificationTerminalSpoolEnvelopeV1.model_validate(
            envelope.model_dump(mode="python")
        )
        payload = canonical_json_bytes(value)
        if len(payload) > _MAX_ENVELOPE_BYTES:
            raise QualificationTerminalOutboxError("qualification outbox envelope is oversized")
        final_name = value.filename
        pending_name = f".{value.outbox_id}.pending"
        root_fd = self._open_root()
        descriptor: int | None = None
        try:
            final = self._stat_optional(root_fd, final_name)
            pending = self._stat_optional(root_fd, pending_name)
            if final is not None:
                final_payload, final_stat = self._read_candidate(
                    root_fd,
                    final_name,
                    allowed_links=frozenset({1, 2}),
                    mode=0o400,
                )
                if final_payload != payload:
                    raise QualificationTerminalOutboxError(
                        "qualification outbox final file contains another envelope"
                    )
                if pending is None:
                    if final_stat.st_nlink != 1:
                        raise QualificationTerminalOutboxError(
                            "qualification outbox final file has an unsafe link count"
                        )
                    return False
                pending_payload, pending_stat = self._read_candidate(
                    root_fd,
                    pending_name,
                    allowed_links=frozenset({2}),
                    mode=0o400,
                )
                if (
                    pending_payload != payload
                    or final_stat.st_dev != pending_stat.st_dev
                    or final_stat.st_ino != pending_stat.st_ino
                    or final_stat.st_nlink != 2
                ):
                    raise QualificationTerminalOutboxError(
                        "qualification outbox final/pending residue is not one inode"
                    )
                os.unlink(pending_name, dir_fd=root_fd)
                os.fsync(root_fd)
                self._read_candidate(
                    root_fd,
                    final_name,
                    allowed_links=frozenset({1}),
                    mode=0o400,
                )
                return False

            if pending is not None and stat.S_IMODE(pending.st_mode) == 0o600:
                self._read_candidate(
                    root_fd,
                    pending_name,
                    allowed_links=frozenset({1}),
                    mode=0o600,
                )
                os.unlink(pending_name, dir_fd=root_fd)
                os.fsync(root_fd)
                pending = None
            if pending is not None:
                pending_payload, pending_stat = self._read_candidate(
                    root_fd,
                    pending_name,
                    allowed_links=frozenset({1}),
                    mode=0o400,
                )
                if pending_payload != payload or pending_stat.st_nlink != 1:
                    raise QualificationTerminalOutboxError(
                        "qualification outbox sealed pending file contains another envelope"
                    )
            else:
                try:
                    descriptor = os.open(
                        pending_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=root_fd,
                    )
                except OSError as exc:
                    raise QualificationTerminalOutboxError(
                        "qualification outbox pending file could not be created"
                    ) from exc
                _spool_checkpoint("pending-created", self.root / final_name)
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise QualificationTerminalOutboxError(
                            "qualification outbox envelope write made no progress"
                        )
                    view = view[written:]
                os.fsync(descriptor)
                _spool_checkpoint("pending-written", self.root / final_name)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.fsync(root_fd)
                _spool_checkpoint("pending-sealed", self.root / final_name)

            try:
                os.link(
                    pending_name,
                    final_name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise QualificationTerminalOutboxError(
                    "qualification outbox final link could not be published"
                ) from exc
            os.fsync(root_fd)
            _spool_checkpoint("final-linked", self.root / final_name)
            os.unlink(pending_name, dir_fd=root_fd)
            os.fsync(root_fd)
            final_payload, final_stat = self._read_candidate(
                root_fd,
                final_name,
                allowed_links=frozenset({1}),
                mode=0o400,
            )
            if final_payload != payload or final_stat.st_nlink != 1:
                raise QualificationTerminalOutboxError(
                    "qualification outbox final verification failed"
                )
            return True
        except OSError as exc:
            raise QualificationTerminalOutboxError(
                "qualification outbox durable publication failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(root_fd)

    def require_published(self, envelope: QualificationTerminalSpoolEnvelopeV1) -> None:
        payload = canonical_json_bytes(envelope)
        root_fd = self._open_root()
        try:
            if self._stat_optional(root_fd, f".{envelope.outbox_id}.pending") is not None:
                raise QualificationTerminalOutboxError(
                    "published legacy outbox retains an incomplete pending file"
                )
            observed, metadata = self._read_candidate(
                root_fd,
                envelope.filename,
                allowed_links=frozenset({1}),
                mode=0o400,
            )
            if observed != payload or metadata.st_nlink != 1:
                raise QualificationTerminalOutboxError(
                    "published legacy outbox lost its exact spool envelope"
                )
        finally:
            os.close(root_fd)

    def verify_inventory(self, envelopes: tuple[QualificationTerminalSpoolEnvelopeV1, ...]) -> None:
        expected = {item.filename for item in envelopes} | {".service.lock"}
        root_fd = self._open_root()
        try:
            observed = set(os.listdir(root_fd))
        except OSError as exc:
            os.close(root_fd)
            raise QualificationTerminalOutboxError(
                "qualification outbox spool inventory is unavailable"
            ) from exc
        os.close(root_fd)
        if observed != expected:
            raise QualificationTerminalOutboxError(
                "qualification outbox spool contains missing, pending, or foreign files"
            )

    def _open_root(self) -> int:
        self.revalidate()
        try:
            return os.open(
                self.root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise QualificationTerminalOutboxError(
                "qualification outbox spool cannot be opened safely"
            ) from exc

    @staticmethod
    def _stat_optional(root_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise QualificationTerminalOutboxError(
                "qualification outbox spool entry cannot be inspected"
            ) from exc

    def _read_candidate(
        self,
        root_fd: int,
        name: str,
        *,
        allowed_links: frozenset[int],
        mode: int,
    ) -> tuple[bytes, os.stat_result]:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise QualificationTerminalOutboxError(
                "qualification outbox spool candidate cannot be opened safely"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != self._pin.owner_uid
                or before.st_gid != self._pin.owner_gid
                or before.st_nlink not in allowed_links
                or stat.S_IMODE(before.st_mode) != mode
                or not 0 <= before.st_size <= _MAX_ENVELOPE_BYTES
            ):
                raise QualificationTerminalOutboxError(
                    "qualification outbox spool candidate custody is unsafe"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, _MAX_ENVELOPE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_ENVELOPE_BYTES:
                    raise QualificationTerminalOutboxError(
                        "qualification outbox spool candidate is oversized"
                    )
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
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
        ) or total != before.st_size:
            raise QualificationTerminalOutboxError(
                "qualification outbox spool candidate changed while read"
            )
        return payload, after


class QualificationTerminalOutboxService:
    """Bounded database mirror and durable polling loop for both execution outbox generations."""

    def __init__(
        self,
        *,
        config: QualificationTerminalOutboxServiceConfigV1,
        spool: QualificationTerminalOutboxSpool,
        sessions,
    ) -> None:
        self._config = QualificationTerminalOutboxServiceConfigV1.model_validate(
            config.model_dump(mode="python")
        )
        if not isinstance(spool, QualificationTerminalOutboxSpool):
            raise TypeError("qualification terminal outbox service requires the exact spool")
        if not callable(sessions):
            raise TypeError("qualification terminal outbox service requires a session factory")
        self._spool = spool
        self._sessions = sessions
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> QualificationTerminalOutboxTickReceipt:
        limit = self._config.maximum_source_rows_per_kind + 1
        try:
            with self._sessions() as session, session.begin():
                legacy_rows = (
                    session.execute(
                        _SELECT_LEGACY_OUTBOX,
                        {"limit": limit},
                    )
                    .mappings()
                    .all()
                )
                qualification_rows = (
                    session.execute(
                        _SELECT_QUALIFICATION_OUTBOX,
                        {"limit": limit},
                    )
                    .mappings()
                    .all()
                )
                if (
                    len(legacy_rows) > self._config.maximum_source_rows_per_kind
                    or len(qualification_rows) > self._config.maximum_source_rows_per_kind
                ):
                    raise QualificationTerminalOutboxError(
                        "qualification terminal outbox exceeds its bounded deployment scan"
                    )
                legacy = tuple(_legacy_envelope(item) for item in legacy_rows)
                qualification = tuple(_qualification_envelope(item) for item in qualification_rows)
                envelopes = tuple(item[0] for item in legacy) + qualification
                ids = tuple(item.outbox_id for item in envelopes)
                if len(ids) != len(set(ids)):
                    raise QualificationTerminalOutboxError(
                        "qualification terminal outbox sources are not globally canonical"
                    )
                newly_spooled: list[str] = []
                replayed: list[str] = []
                published: list[str] = []
                for envelope, status, attempts in legacy:
                    if status == "published":
                        self._spool.require_published(envelope)
                        replayed.append(envelope.outbox_id)
                        continue
                    if self._spool.retain(envelope):
                        newly_spooled.append(envelope.outbox_id)
                    else:
                        replayed.append(envelope.outbox_id)
                    result = session.execute(
                        text(
                            """
                            UPDATE execution_outbox
                               SET status = 'published',
                                   publish_attempts = publish_attempts + 1,
                                   published_at = clock_timestamp()
                             WHERE outbox_id = :outbox_id
                               AND status = 'pending'
                               AND publish_attempts = :publish_attempts
                               AND published_at IS NULL
                            RETURNING publish_attempts, published_at
                            """
                        ),
                        {
                            "outbox_id": envelope.outbox_id,
                            "publish_attempts": attempts,
                        },
                    ).one_or_none()
                    if (
                        result is None
                        or result[0] != attempts + 1
                        or result[1] < envelope.source_created_at
                    ):
                        raise QualificationTerminalOutboxError(
                            "legacy execution outbox publication CAS failed"
                        )
                    published.append(envelope.outbox_id)
                for envelope in qualification:
                    if self._spool.retain(envelope):
                        newly_spooled.append(envelope.outbox_id)
                    else:
                        replayed.append(envelope.outbox_id)
                self._spool.verify_inventory(envelopes)
                return QualificationTerminalOutboxTickReceipt(
                    source_outbox_ids=tuple(sorted(ids)),
                    newly_spooled_outbox_ids=tuple(sorted(newly_spooled)),
                    replayed_spool_outbox_ids=tuple(sorted(replayed)),
                    legacy_published_outbox_ids=tuple(sorted(published)),
                )
        except QualificationTerminalOutboxError:
            raise
        except (SQLAlchemyError, OSError, TypeError, ValueError) as exc:
            raise QualificationTerminalOutboxError(
                "qualification terminal outbox tick failed"
            ) from exc

    def run_forever(self) -> None:
        descriptor = self._spool.acquire_service_lock()
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self._config.poll_milliseconds / 1000)
        finally:
            os.close(descriptor)


def _verify_live_database_binding(config: QualificationTerminalOutboxServiceConfigV1) -> None:
    try:
        with session_factory()() as session:
            observed = session.execute(
                text(
                    "SELECT current_user, "
                    "(SELECT count(*) FROM alembic_version), "
                    "(SELECT min(version_num) FROM alembic_version)"
                )
            ).one()
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        raise QualificationTerminalOutboxError(
            "qualification outbox database identity is unavailable"
        ) from exc
    if tuple(observed) != (config.postgresql_role, 1, config.schema_revision):
        raise QualificationTerminalOutboxError(
            "qualification outbox PostgreSQL role or live schema differs from deployment"
        )


def compose_outbox_service(
    *,
    deployment: QualificationServiceProcessDeploymentV1,
    configuration_bytes: bytes,
) -> QualificationServiceHandlerSet:
    """Compose the exact non-root qualification terminal-outbox service."""

    config = _load_config(configuration_bytes)
    process = _bind_process(deployment, config)
    database_url_sha256 = hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
    if (
        config.database_url_sha256 != database_url_sha256
        or config.schema_revision != expected_schema_revision()
    ):
        raise QualificationTerminalOutboxError(
            "qualification outbox database differs from deployment"
        )
    _verify_live_database_binding(config)
    service = QualificationTerminalOutboxService(
        config=config,
        spool=QualificationTerminalOutboxSpool(config.spool_root),
        sessions=session_factory(),
    )

    def handler(*, poll_milliseconds: int | None) -> None:
        if poll_milliseconds is not None:
            raise QualificationTerminalOutboxError(
                "qualification outbox handler received a node-only poll interval"
            )
        service.run_forever()

    return QualificationServiceHandlerSet(
        role=process.role,
        operation=process.operation,
        handler=handler,
    )


__all__ = [
    "QualificationOutboxSpoolRootPinV1",
    "QualificationTerminalOutboxError",
    "QualificationTerminalOutboxService",
    "QualificationTerminalOutboxServiceConfigV1",
    "QualificationTerminalOutboxSpool",
    "QualificationTerminalOutboxTickReceipt",
    "QualificationTerminalSpoolEnvelopeV1",
    "qualification_terminal_spool_envelope_from_row",
    "compose_outbox_service",
]
