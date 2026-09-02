"""Target-local commissioning for one attempt-scoped pre-runtime cleanup key/config."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.db import expected_schema_revision
from aletheia.execution.oci_runtime import host_parent_chain_sha256
from aletheia.execution.qualification_node_service import (
    AttemptScopedPreRuntimeCleanupServiceConfigV1,
    QualificationNodePrivateKeyPinV1,
    QualificationNodeServiceConfigV1,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_v2_contracts import (
    AttemptScopedPreRuntimeCleanupAuthorityPin,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ATTEMPT_PATTERN = r"^iat_[0-9a-f]{32}$"
_MAX_CONFIG_BYTES = 16 * 1024 * 1024


class PreRuntimeCleanupCommissioningError(RuntimeError):
    """Source/config/key custody or exact-retry validation failed closed."""


class PreRuntimeCleanupCommissioningRequestV1(ExecutionModel):
    """Frozen non-secret inputs for one target-local key and recovery config."""

    schema_name: Literal["aletheia.pre_runtime_cleanup_commissioning_request"] = (
        "aletheia.pre_runtime_cleanup_commissioning_request"
    )
    schema_version: Literal[1] = 1
    source_node_config_path: str
    source_node_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    target_cleanup_key_path: str
    target_cleanup_config_path: str
    principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_PATTERN)
    runtime_preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    configured_at: AwareDatetime
    root_commissioning_required: Literal[True] = True
    target_local_key_generation_required: Literal[True] = True
    private_key_export_allowed: Literal[False] = False
    execution_launch_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _paths_and_window_are_closed(self) -> "PreRuntimeCleanupCommissioningRequestV1":
        source = _absolute(self.source_node_config_path, label="source node config")
        key = _absolute(self.target_cleanup_key_path, label="cleanup key")
        config = _absolute(self.target_cleanup_config_path, label="cleanup config")
        if (
            source in {key, config}
            or key == config
            or not self.valid_from <= self.configured_at < self.expires_at
            or self.expires_at <= self.valid_from
            or self.expires_at - self.valid_from > timedelta(hours=1)
        ):
            raise ValueError(
                "cleanup commissioning paths or authority window differ from one direct scope"
            )
        return self


class PreRuntimeCleanupCommissioningReceiptV1(ExecutionModel):
    """Non-secret exact-retry evidence; private bytes and their file hash are omitted."""

    schema_name: Literal["aletheia.pre_runtime_cleanup_commissioning_receipt"] = (
        "aletheia.pre_runtime_cleanup_commissioning_receipt"
    )
    schema_version: Literal[1] = 1
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_PATTERN)
    cleanup_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_key_id: str = Field(pattern=_SHA256_PATTERN)
    cleanup_config_path: str
    cleanup_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str
    key_published: Literal[True] = True
    config_published: Literal[True] = True
    commissioned_at: AwareDatetime
    private_key_exported: Literal[False] = False
    execution_launch_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_derived(self) -> "PreRuntimeCleanupCommissioningReceiptV1":
        _absolute(self.cleanup_config_path, label="cleanup config receipt")
        return self


def _absolute(value: str, *, label: str) -> Path:
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


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    duplicates = [key for key, count in Counter(key for key, _value in pairs).items() if count > 1]
    if duplicates:
        raise ValueError("duplicate source node config keys")
    return dict(pairs)


def _read_regular(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_mode: int | None = None,
    expected_bytes: int | None = None,
    allowed_link_counts: frozenset[int] = frozenset({1}),
) -> bytes:
    descriptor = -1
    try:
        if path.resolve(strict=True) != path:
            raise PreRuntimeCleanupCommissioningError("commissioning input traverses a symlink")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in allowed_link_counts
            or (expected_uid is not None and before.st_uid != expected_uid)
            or (expected_gid is not None and before.st_gid != expected_gid)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
            or (expected_bytes is not None and before.st_size != expected_bytes)
            or before.st_size > _MAX_CONFIG_BYTES
        ):
            raise PreRuntimeCleanupCommissioningError(
                "commissioning input custody differs from its exact pin"
            )
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise PreRuntimeCleanupCommissioningError("commissioning input cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or (expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256)
    ):
        raise PreRuntimeCleanupCommissioningError("commissioning input changed while read")
    return payload


def _publish_exclusive(
    path: Path,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> bool:
    """Durably publish exact bytes without overwriting and recover closed crash residues."""

    pending = path.with_name(f".{path.name}.pending")
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PreRuntimeCleanupCommissioningError(
            "commissioning output directory is unavailable"
        ) from exc
    descriptor = -1
    try:
        try:
            pending_metadata = os.stat(
                pending.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pending_metadata = None
        try:
            final_metadata = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            final_metadata = None

        if pending_metadata is not None and stat.S_IMODE(pending_metadata.st_mode) == 0o600:
            try:
                descriptor = os.open(
                    pending.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_descriptor,
                )
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_uid not in {os.geteuid(), uid}
                    or before.st_gid not in {os.getegid(), gid}
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_CONFIG_BYTES
                ):
                    raise PreRuntimeCleanupCommissioningError(
                        "unsealed commissioning pending custody is unsafe"
                    )
                while os.read(descriptor, 65_536):
                    pass
                after = os.fstat(descriptor)
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
                ):
                    raise PreRuntimeCleanupCommissioningError(
                        "unsealed commissioning pending changed while read"
                    )
                os.close(descriptor)
                descriptor = -1
                os.unlink(pending.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                pending_metadata = None
            except OSError as exc:
                raise PreRuntimeCleanupCommissioningError(
                    "unsealed commissioning pending cannot be recovered"
                ) from exc

        if pending_metadata is not None:
            pending_payload = _read_regular(
                pending,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_uid=uid,
                expected_gid=gid,
                expected_mode=mode,
                expected_bytes=len(payload),
                allowed_link_counts=frozenset({1, 2}),
            )
            if pending_payload != payload:
                raise PreRuntimeCleanupCommissioningError(
                    "commissioning sealed pending changed bytes"
                )
            pending_metadata = pending.stat(follow_symlinks=False)
            if final_metadata is not None:
                final_payload = _read_regular(
                    path,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_uid=uid,
                    expected_gid=gid,
                    expected_mode=mode,
                    expected_bytes=len(payload),
                    allowed_link_counts=frozenset({2}),
                )
                final_metadata = path.stat(follow_symlinks=False)
                if (
                    final_payload != payload
                    or pending_metadata.st_dev != final_metadata.st_dev
                    or pending_metadata.st_ino != final_metadata.st_ino
                    or pending_metadata.st_nlink != 2
                    or final_metadata.st_nlink != 2
                ):
                    raise PreRuntimeCleanupCommissioningError(
                        "commissioning final/pending residue is not one sealed inode"
                    )
                os.unlink(pending.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                _read_regular(
                    path,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_uid=uid,
                    expected_gid=gid,
                    expected_mode=mode,
                    expected_bytes=len(payload),
                )
                return False
            if pending_metadata.st_nlink != 1:
                raise PreRuntimeCleanupCommissioningError(
                    "commissioning pending has an unsafe pre-publication link count"
                )
        elif final_metadata is not None:
            existing = _read_regular(
                path,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_uid=uid,
                expected_gid=gid,
                expected_mode=mode,
                expected_bytes=len(payload),
            )
            if existing != payload:
                raise PreRuntimeCleanupCommissioningError("commissioning exact retry changed bytes")
            return False
        else:
            try:
                descriptor = os.open(
                    pending.name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short commissioning write")
                    view = view[written:]
                os.fsync(descriptor)
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.fsync(parent_descriptor)
            except OSError as exc:
                raise PreRuntimeCleanupCommissioningError(
                    "commissioning pending could not be durably sealed"
                ) from exc

        created = True
        try:
            os.link(
                pending.name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            created = False
            existing = _read_regular(
                path,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_uid=uid,
                expected_gid=gid,
                expected_mode=mode,
                expected_bytes=len(payload),
                allowed_link_counts=frozenset({1, 2}),
            )
            if existing != payload:
                raise PreRuntimeCleanupCommissioningError(
                    "commissioning final raced with different bytes"
                )
        except OSError as exc:
            raise PreRuntimeCleanupCommissioningError(
                "commissioning final link could not be published"
            ) from exc
        os.fsync(parent_descriptor)
        try:
            os.unlink(pending.name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.fsync(parent_descriptor)
        final = _read_regular(
            path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_uid=uid,
            expected_gid=gid,
            expected_mode=mode,
            expected_bytes=len(payload),
        )
        if final != payload:
            raise PreRuntimeCleanupCommissioningError(
                "commissioning final publication changed bytes"
            )
        return created
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _load_or_create_private_key(path: Path, *, uid: int, gid: int) -> tuple[bytes, bool]:
    pending = path.with_name(f".{path.name}.pending")
    if os.path.lexists(path):
        key_bytes = _read_regular(
            path,
            expected_sha256=None,
            expected_uid=uid,
            expected_gid=gid,
            expected_mode=0o400,
            expected_bytes=32,
            allowed_link_counts=frozenset({1, 2}),
        )
    elif os.path.lexists(pending) and stat.S_IMODE(pending.lstat().st_mode) == 0o400:
        key_bytes = _read_regular(
            pending,
            expected_sha256=None,
            expected_uid=uid,
            expected_gid=gid,
            expected_mode=0o400,
            expected_bytes=32,
            allowed_link_counts=frozenset({1, 2}),
        )
    else:
        key_bytes = Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    created = _publish_exclusive(path, key_bytes, uid=uid, gid=gid, mode=0o400)
    return key_bytes, created


def _load_source(
    request: PreRuntimeCleanupCommissioningRequestV1,
) -> QualificationNodeServiceConfigV1:
    source_path = Path(request.source_node_config_path)
    payload = _read_regular(
        source_path,
        expected_sha256=request.source_node_config_sha256,
    )
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
        source = QualificationNodeServiceConfigV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise PreRuntimeCleanupCommissioningError("source node config is invalid") from exc
    if canonical_json_bytes(source) != payload:
        raise PreRuntimeCleanupCommissioningError("source node config is not canonical JSON")
    metadata = source_path.stat()
    node_identity = (source.node_signing_key.owner_uid, source.node_signing_key.owner_gid)
    observed_identity = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    if observed_identity not in {
        (*node_identity, 0o400),
        (0, node_identity[1], 0o440),
    }:
        raise PreRuntimeCleanupCommissioningError(
            "source node config owner or mode differs from deployment custody"
        )
    if (
        _read_regular(
            source_path,
            expected_sha256=request.source_node_config_sha256,
            expected_uid=metadata.st_uid,
            expected_gid=metadata.st_gid,
            expected_mode=stat.S_IMODE(metadata.st_mode),
        )
        != payload
    ):
        raise PreRuntimeCleanupCommissioningError("source node config changed during validation")
    return QualificationNodeServiceConfigV1.model_validate(
        source.model_copy(update={"schema_revision": expected_schema_revision()}).model_dump(
            mode="python"
        )
    )


def commission_pre_runtime_cleanup(
    request: PreRuntimeCleanupCommissioningRequestV1,
) -> PreRuntimeCleanupCommissioningReceiptV1:
    """Generate or exact-replay one target-only key and canonical recovery config."""

    request = PreRuntimeCleanupCommissioningRequestV1.model_validate(
        request.model_dump(mode="python")
    )
    if os.geteuid() != 0:
        raise PreRuntimeCleanupCommissioningError("cleanup commissioning requires root")
    source = _load_source(request)
    key_path = Path(request.target_cleanup_key_path)
    config_path = Path(request.target_cleanup_config_path)
    node_uid = source.node_signing_key.owner_uid
    node_gid = source.node_signing_key.owner_gid
    if (
        key_path.parent != Path(source.node_signing_key.path).parent
        or config_path.parent != Path(request.source_node_config_path).parent
        or key_path.parent.resolve(strict=True) != key_path.parent
        or config_path.parent.resolve(strict=True) != config_path.parent
    ):
        raise PreRuntimeCleanupCommissioningError(
            "cleanup key or config escaped its existing deployment custody root"
        )
    key_bytes, _key_created = _load_or_create_private_key(
        key_path,
        uid=node_uid,
        gid=node_gid,
    )
    public_key = (
        Ed25519PrivateKey.from_private_bytes(key_bytes)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    key_id = qualification_key_id(public_key)
    key_pin = QualificationNodePrivateKeyPinV1(
        role="pre_runtime_cleanup_recovery",
        algorithm="ed25519",
        path=str(key_path),
        file_sha256=hashlib.sha256(key_bytes).hexdigest(),
        key_id=key_id,
        owner_uid=node_uid,
        owner_gid=node_gid,
        parent_chain_sha256=host_parent_chain_sha256(key_path),
    )
    authority = AttemptScopedPreRuntimeCleanupAuthorityPin(
        policy_sha256=request.policy_sha256,
        principal_id=request.principal_id,
        key_id=key_id,
        public_key_ed25519_hex=public_key,
        source_node_id=source.node_authority.manifest.node_id,
        source_node_manifest_sha256=source.node_authority.manifest.manifest_sha256,
        infrastructure_attempt_id=request.infrastructure_attempt_id,
        runtime_preparation_sha256=request.runtime_preparation_sha256,
        runtime_launch_authorization_sha256=request.runtime_launch_authorization_sha256,
        cleanup_absence_epoch=request.cleanup_absence_epoch,
        watchdog_deployment_sha256=source.watchdog_deployment.deployment_sha256,
        valid_from=request.valid_from,
        expires_at=request.expires_at,
    )
    config = AttemptScopedPreRuntimeCleanupServiceConfigV1(
        source_node_service_config=source,
        source_node_service_config_sha256=canonical_sha256(source),
        cleanup_authority_pin=authority,
        cleanup_signing_key=key_pin,
        configured_at=request.configured_at,
    )
    config_payload = canonical_json_bytes(config)
    _config_created = _publish_exclusive(
        config_path,
        config_payload,
        uid=0,
        gid=node_gid,
        mode=0o440,
    )
    return PreRuntimeCleanupCommissioningReceiptV1(
        request_sha256=canonical_sha256(request),
        infrastructure_attempt_id=request.infrastructure_attempt_id,
        cleanup_authority_sha256=authority.authority_sha256,
        cleanup_key_id=key_id,
        cleanup_config_path=str(config_path),
        cleanup_config_sha256=hashlib.sha256(config_payload).hexdigest(),
        schema_revision=source.schema_revision,
        commissioned_at=request.configured_at,
    )


__all__ = [
    "PreRuntimeCleanupCommissioningError",
    "PreRuntimeCleanupCommissioningReceiptV1",
    "PreRuntimeCleanupCommissioningRequestV1",
    "commission_pre_runtime_cleanup",
]
