"""Concrete Linux deployment dependencies for the qualification-only OCI runtime.

The in-process OCI adapter deliberately delegates three facts that it cannot prove by
configuration alone:

* the digest-pinned Docker image contains the reviewed launch-gate bytes;
* the writable output bind has a kernel-enforced byte ceiling; and
* an independently supervised process will kill an overdue sandbox after the node agent dies.

This module supplies Linux implementations with deliberately narrow deployment prerequisites.
It does not contain a permissive development fallback.  Image verification needs a root-owned OCI
image layout in addition to Docker's local image metadata.  Output quota verification needs an
exclusive loop-backed filesystem whose block-device capacity is exactly the requested quota.
Deadline enforcement needs the root watchdog daemon below to be managed by the exact pinned
systemd unit.  Missing prerequisites fail closed.

The watchdog client never accepts an echoed challenge hash.  Both client and daemon independently
reconstruct the complete watchdog scope from the runtime's durable journals, and the daemon fsyncs
an append-only armed/retired/fired record before acknowledging a request.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.execution.oci_runtime import (
    DeploymentPinnedOCIPolicy,
    OCIConfiguration,
    OCIExecutionPlan,
    OCIProductionCapabilityError,
    OCIWatchdogCleanupQuiescence,
    _DeadlineWatchdogJournal,
    _LaunchGateAuthorizationJournal,
    _NeverStartedCleanupPending,
    host_parent_chain_sha256,
)
from aletheia.execution.runtime_v2_contracts import (
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    OutputQuotaProvisioningReceipt,
    PinnedOutputWorkspaceRoot,
    RuntimeControlAuthorityPin,
    RuntimePreparation,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_SYSTEMD_UNIT = re.compile(r"^aletheia-qualification-oci-watchdog(?:-[a-z0-9_.-]+)?\.service$")
_MAX_CONTROL_BYTES = 1024 * 1024
_MAX_GATE_BYTES = 32 * 1024 * 1024
_SECTOR_BYTES = 512
_OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_OCI_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
_OCI_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}


class OCIDeploymentDependencyError(OCIProductionCapabilityError):
    """One concrete Linux deployment dependency is absent or changed."""


class OCIImageAttestationError(OCIDeploymentDependencyError):
    """The immutable OCI layout or Docker image identity failed verification."""


class OCIOutputQuotaError(OCIDeploymentDependencyError):
    """The output bind lacks the exact kernel-enforced loop-device ceiling."""


class OCIWatchdogError(OCIDeploymentDependencyError):
    """The independent deadline watchdog failed closed."""


def _canonical_absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or str(path) != value
        or value == "/"
        or any(character in value for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError(f"{label} must be one canonical absolute child")
    return path


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextmanager
def _stable_regular_file(
    path: Path,
    *,
    label: str,
    owner_uid: int,
    maximum_bytes: int,
    allowed_modes: frozenset[int],
) -> Iterator[int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise OCIDeploymentDependencyError(f"{label} is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or before.st_size > maximum_bytes
        ):
            raise OCIDeploymentDependencyError(f"{label} custody metadata is unsafe")
        yield descriptor
        if _stat_identity(before) != _stat_identity(os.fstat(descriptor)):
            raise OCIDeploymentDependencyError(f"{label} changed while it was read")
    finally:
        os.close(descriptor)


def _read_bounded_file(
    path: Path,
    *,
    label: str,
    owner_uid: int,
    maximum_bytes: int,
    allowed_modes: frozenset[int] = frozenset({0o400, 0o440, 0o444}),
) -> bytes:
    with _stable_regular_file(
        path,
        label=label,
        owner_uid=owner_uid,
        maximum_bytes=maximum_bytes,
        allowed_modes=allowed_modes,
    ) as descriptor:
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise OCIDeploymentDependencyError(f"{label} exceeded its deployment bound")
        return bytes(payload)


def _durable_publish_checkpoint(phase: str, path: Path) -> None:
    """Unit-test fault boundary for power-loss-sensitive durable phases."""


def _read_publish_candidate(
    path: Path,
    *,
    owner_uid: int,
    allowed_link_counts: frozenset[int],
    error_type: type[OCIDeploymentDependencyError],
) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise error_type("durable publish candidate is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or before.st_gid != os.getegid()
            or before.st_nlink not in allowed_link_counts
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_size > _MAX_CONTROL_BYTES
        ):
            raise error_type("durable publish candidate custody is unsafe")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_CONTROL_BYTES:
                raise error_type("durable publish candidate exceeded its byte bound")
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise error_type("durable publish candidate changed while read")
        return bytes(payload), after
    finally:
        os.close(descriptor)


def _publish_root_record_once(
    path: Path,
    value: ExecutionModel,
    *,
    owner_uid: int,
    error_type: type[OCIDeploymentDependencyError],
) -> bytes:
    """Publish one immutable record and recover both legitimate hard-link crash residues.

    The caller must hold the root-service or generation lock for ``path.parent``.  The fixed
    ``.pending`` name is safe only under that lock and inside a root-custody directory.  We fsync
    the pending directory entry before linking, then fsync the final link before removing the
    pending name.  A restart can therefore distinguish three valid residues: an unsealed 0600
    writer-owned single-link inode (never evidence and always discarded), a sealed 0400 one-link
    pre-publish inode, or final+pending names for the same sealed two-link inode.
    """

    payload = canonical_json_bytes(value)
    pending = path.with_name(f".{path.name}.pending")
    try:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise error_type("durable publish directory is unavailable") from exc
    descriptor: int | None = None
    try:
        publish_payload = payload
        pending_exists = False
        final_exists = False
        try:
            os.stat(pending.name, dir_fd=parent_fd, follow_symlinks=False)
            pending_exists = True
        except FileNotFoundError:
            pass
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            final_exists = True
        except FileNotFoundError:
            pass

        if pending_exists and not final_exists:
            pending_metadata = os.stat(
                pending.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if stat.S_IMODE(pending_metadata.st_mode) == 0o600:
                # A crash can occur at any instruction after O_EXCL creates the fixed
                # pending name and before fchmod seals it 0400.  Such an inode is not
                # published evidence.  Recover only the exact writer-owned, single-link,
                # bounded inode that this writer could have left; discard it under the
                # caller's lock and rebuild from the typed value.  Bytes are deliberately
                # ignored: several otherwise immutable records contain a timestamp generated
                # before publication, so an unpublished residue need not prefix replay bytes.
                try:
                    descriptor = os.open(
                        pending.name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise error_type("incomplete durable pending record is unsafe") from exc
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != owner_uid
                    or before.st_gid != os.getegid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_CONTROL_BYTES
                ):
                    raise error_type("incomplete durable pending record custody is unsafe")
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                after = os.fstat(descriptor)
                if _stat_identity(before) != _stat_identity(after):
                    raise error_type("incomplete durable pending record changed while read")
                os.unlink(pending.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
                os.close(descriptor)
                descriptor = None
                pending_exists = False

        if pending_exists:
            pending_payload, pending_stat = _read_publish_candidate(
                pending,
                owner_uid=owner_uid,
                allowed_link_counts=frozenset({1, 2}),
                error_type=error_type,
            )
            try:
                pending_value = type(value).model_validate_json(pending_payload)
            except ValidationError as exc:
                raise error_type("sealed durable pending record failed validation") from exc
            if canonical_json_bytes(pending_value) != pending_payload:
                raise error_type("sealed durable pending record is not canonical")
            publish_payload = pending_payload
            if final_exists:
                final_payload, final_stat = _read_publish_candidate(
                    path,
                    owner_uid=owner_uid,
                    allowed_link_counts=frozenset({2}),
                    error_type=error_type,
                )
                if (
                    final_payload != pending_payload
                    or pending_stat.st_dev != final_stat.st_dev
                    or pending_stat.st_ino != final_stat.st_ino
                    or pending_stat.st_nlink != 2
                    or final_stat.st_nlink != 2
                ):
                    raise error_type("durable final/pending residue is not one linked inode")
                os.unlink(pending.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
                _read_publish_candidate(
                    path,
                    owner_uid=owner_uid,
                    allowed_link_counts=frozenset({1}),
                    error_type=error_type,
                )
                return final_payload
            if pending_stat.st_nlink != 1:
                raise error_type("pre-link durable pending residue has an unsafe link count")
        elif final_exists:
            final_payload, _ = _read_publish_candidate(
                path,
                owner_uid=owner_uid,
                allowed_link_counts=frozenset({1}),
                error_type=error_type,
            )
            try:
                final_value = type(value).model_validate_json(final_payload)
            except ValidationError as exc:
                raise error_type("durable final record failed validation") from exc
            if canonical_json_bytes(final_value) != final_payload:
                raise error_type("durable final record is not canonical")
            return final_payload
        else:
            try:
                descriptor = os.open(
                    pending.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise error_type("durable pending record could not be created") from exc
            _durable_publish_checkpoint("pending-created", path)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise error_type("durable record write made no progress")
                view = view[written:]
            # Make the complete data/size durable while the inode is still explicitly
            # unpublished (0600).  Only then seal it 0400.  Consequently every 0400 residue
            # that recovery treats as a committed decision is backed by a prior data fsync.
            os.fsync(descriptor)
            _durable_publish_checkpoint("pending-written-before-mode", path)
            os.fchmod(descriptor, 0o400)
            _durable_publish_checkpoint("pending-moded-before-final-fsync", path)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.fsync(parent_fd)
            _durable_publish_checkpoint("pending-fsynced", path)

        try:
            os.link(
                pending.name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise error_type("durable final link could not be published") from exc
        os.fsync(parent_fd)
        _durable_publish_checkpoint("final-linked-fsynced", path)
        os.unlink(pending.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        final_payload, final_stat = _read_publish_candidate(
            path,
            owner_uid=owner_uid,
            allowed_link_counts=frozenset({1}),
            error_type=error_type,
        )
        if final_payload != publish_payload or final_stat.st_nlink != 1:
            raise error_type("durable final record verification failed")
        return final_payload
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _recover_root_record_once(
    path: Path,
    model: type[ExecutionModel],
    *,
    owner_uid: int,
    error_type: type[OCIDeploymentDependencyError],
) -> ExecutionModel | None:
    """Recover a typed fixed-pending root record before reconstructing volatile fields.

    This is the read side of :func:`_publish_root_record_once`.  Callers must hold the same
    deployment/generation lock as the publisher.  A sealed 0400 pending inode is promoted and
    returned byte-for-byte; an unsealed 0600 writer residue is validated, discarded, and reported
    absent.  Recovering here—before loop formatting, cgroup killing, or timestamp generation—keeps
    every high-level operation idempotent rather than merely making its low-level publisher so.
    """

    pending = path.with_name(f".{path.name}.pending")
    try:
        pending_metadata = pending.lstat()
    except FileNotFoundError:
        pending_metadata = None
    except OSError as exc:
        raise error_type("durable pending record could not be inspected") from exc
    try:
        path.lstat()
        final_exists = True
    except FileNotFoundError:
        final_exists = False
    except OSError as exc:
        raise error_type("durable final record could not be inspected") from exc
    if pending_metadata is not None:
        mode = stat.S_IMODE(pending_metadata.st_mode)
        if mode == 0o600:
            if final_exists:
                raise error_type("unsealed durable pending record conflicts with final evidence")
            try:
                descriptor = os.open(
                    pending,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                raise error_type("unsealed durable pending record is unsafe") from exc
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != owner_uid
                    or before.st_gid != os.getegid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > _MAX_CONTROL_BYTES
                ):
                    raise error_type("unsealed durable pending record custody is unsafe")
                while os.read(descriptor, 64 * 1024):
                    pass
                after = os.fstat(descriptor)
                if _stat_identity(before) != _stat_identity(after):
                    raise error_type("unsealed durable pending record changed while read")
            finally:
                os.close(descriptor)
            try:
                parent_descriptor = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                raise error_type("durable publish directory is unavailable") from exc
            try:
                os.unlink(pending.name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
            return None
        if mode != 0o400:
            raise error_type("sealed durable pending record custody is unsafe")
        pending_payload, _ = _read_publish_candidate(
            pending,
            owner_uid=owner_uid,
            allowed_link_counts=frozenset({1, 2}),
            error_type=error_type,
        )
        try:
            pending_value = model.model_validate_json(pending_payload)
        except ValidationError as exc:
            raise error_type("sealed durable pending record failed validation") from exc
        if canonical_json_bytes(pending_value) != pending_payload:
            raise error_type("sealed durable pending record is not canonical")
        recovered_payload = _publish_root_record_once(
            path,
            pending_value,
            owner_uid=owner_uid,
            error_type=error_type,
        )
    elif final_exists:
        recovered_payload, _ = _read_publish_candidate(
            path,
            owner_uid=owner_uid,
            allowed_link_counts=frozenset({1}),
            error_type=error_type,
        )
    else:
        return None
    try:
        recovered = model.model_validate_json(recovered_payload)
    except ValidationError as exc:
        raise error_type("durable root record failed typed recovery") from exc
    if canonical_json_bytes(recovered) != recovered_payload:
        raise error_type("durable root record failed canonical recovery")
    return recovered


def _load_exact_model(path: Path, model: type[ExecutionModel], *, owner_uid: int) -> ExecutionModel:
    payload = _read_bounded_file(
        path,
        label=f"runtime journal {path.name}",
        owner_uid=owner_uid,
        maximum_bytes=_MAX_CONTROL_BYTES,
        allowed_modes=frozenset({0o400}),
    )
    try:
        value = model.model_validate_json(payload)
    except ValidationError as exc:
        raise OCIWatchdogError(f"runtime journal {path.name} failed closed validation") from exc
    if canonical_json_bytes(value) != payload:
        raise OCIWatchdogError(f"runtime journal {path.name} is not canonical")
    return value


class PinnedOCIImageLayout(ExecutionModel):
    """Root-owned OCI layout paired with the exact image loaded into Docker."""

    schema_name: Literal["aletheia.pinned_oci_image_layout"] = "aletheia.pinned_oci_image_layout"
    schema_version: Literal[1] = 1
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layout_root: str = Field(min_length=2, max_length=4096)
    layout_root_device: int = Field(ge=0)
    layout_root_inode: int = Field(ge=1)
    layout_root_mode: Literal[0o500, 0o550, 0o555] = 0o500
    layout_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_launch_gate_executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_launch_gate_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    maximum_blob_bytes: int = Field(default=8 * 1024**3, ge=1, le=64 * 1024**3)
    maximum_uncompressed_layer_bytes: int = Field(default=16 * 1024**3, ge=1, le=128 * 1024**3)
    qualification_only: Literal[True] = True

    @model_validator(mode="after")
    def _layout_is_canonical(self) -> "PinnedOCIImageLayout":
        _canonical_absolute_path(self.layout_root, label="OCI image layout")
        return self

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class _OCIImageLayoutAttestation(ExecutionModel):
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layout_pin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_blob_sha256s: tuple[str, ...]
    layer_diff_ids: tuple[str, ...]
    launch_gate_executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    launch_gate_mode: int = Field(ge=0, le=0o7777)
    docker_image_id: str
    docker_repo_digests: tuple[str, ...]
    docker_rootfs_diff_ids: tuple[str, ...]


class ImmutableOCIImageLaunchGateVerifier:
    """Verify the gate from OCI layer bytes and cross-check Docker's loaded identity.

    The OCI layout is the manifest/config/rootfs source of truth.  Docker inspection is a second
    binding: it must report the same config digest and uncompressed layer diff IDs.  Neither image
    tags nor labels are accepted as rootfs evidence.
    """

    def __init__(
        self,
        *,
        policy: DeploymentPinnedOCIPolicy,
        runtime_control_authority: RuntimeControlAuthorityPin,
        image_layout: PinnedOCIImageLayout,
    ) -> None:
        self._policy = DeploymentPinnedOCIPolicy.model_validate(policy.model_dump(mode="python"))
        self._authority = RuntimeControlAuthorityPin.model_validate(
            runtime_control_authority.model_dump(mode="python")
        )
        self._layout = PinnedOCIImageLayout.model_validate(image_layout.model_dump(mode="python"))
        if (
            self._layout.policy_sha256 != self._policy.policy_sha256
            or self._layout.reviewed_launch_gate_executable_sha256
            != self._policy.launch_gate_executable_sha256
            or self._layout.reviewed_launch_gate_protocol_sha256
            != self._policy.launch_gate_protocol_sha256
        ):
            raise ValueError("OCI image layout pin differs from the deployment policy")

    def verify_immutable_launch_gate(
        self,
        *,
        image_reference: str,
        image_manifest_sha256: str,
        image_config_sha256: str,
        launch_gate_path: str,
        launch_gate_executable_sha256: str,
        launch_gate_protocol_sha256: str,
        expected_evidence_sha256: str,
    ) -> str:
        supplied = (
            image_reference,
            image_manifest_sha256,
            image_config_sha256,
            launch_gate_path,
            launch_gate_executable_sha256,
            launch_gate_protocol_sha256,
        )
        pinned = (
            self._policy.image_reference,
            self._policy.image_manifest_sha256,
            self._policy.image_config_sha256,
            self._policy.launch_gate_path,
            self._policy.launch_gate_executable_sha256,
            self._policy.launch_gate_protocol_sha256,
        )
        if supplied != pinned:
            raise OCIImageAttestationError("launch-gate request differs from deployment pins")
        expected = self._expected_evidence_sha256()
        if expected_evidence_sha256 != expected:
            raise OCIImageAttestationError("launch-gate challenge differs from its full scope")
        self._require_linux_root_owned_layout()
        docker_inspection = self._docker_image_inspection()
        attestation = self._attest_layout(docker_inspection)
        if attestation.launch_gate_executable_sha256 != self._policy.launch_gate_executable_sha256:
            raise OCIImageAttestationError("OCI rootfs launch-gate bytes differ from policy")
        return expected

    def _expected_evidence_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.verified_immutable_oci_launch_gate.v2",
                "policy_sha256": self._policy.policy_sha256,
                "image_reference": self._policy.image_reference,
                "image_manifest_sha256": self._policy.image_manifest_sha256,
                "image_config_sha256": self._policy.image_config_sha256,
                "launch_gate_path": self._policy.launch_gate_path,
                "launch_gate_executable_sha256": (self._policy.launch_gate_executable_sha256),
                "launch_gate_protocol_sha256": self._policy.launch_gate_protocol_sha256,
                "runtime_control_authority": self._authority,
                "authorization_schema": "aletheia.runtime_launch_authorization.v2",
                "authorization_request_schema": (
                    "aletheia.runtime_launch_authorization_request.v2"
                ),
                "clock": "CLOCK_BOOTTIME",
                "checks_wall_and_suspend_aware_deadline_immediately_before_execve": True,
                "requires_exact_runtime_control_fence_and_token": True,
                "execves_only_pinned_workload_argv": True,
            }
        )

    def _require_linux_root_owned_layout(self) -> None:
        if sys.platform != "linux":
            raise OCIImageAttestationError("immutable OCI image verification is Linux-only")
        root = Path(self._layout.layout_root)
        try:
            metadata = root.lstat()
            parent_chain = host_parent_chain_sha256(root)
        except (OSError, ValueError) as exc:
            raise OCIImageAttestationError("OCI layout custody is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or root.is_symlink()
            or metadata.st_dev != self._layout.layout_root_device
            or metadata.st_ino != self._layout.layout_root_inode
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != self._layout.layout_root_mode
            or parent_chain != self._layout.layout_parent_chain_sha256
        ):
            raise OCIImageAttestationError("OCI layout differs from root-owned deployment pin")

    def _attest_layout(self, docker_inspection: Mapping[str, object]) -> _OCIImageLayoutAttestation:
        root = Path(self._layout.layout_root)
        layout_payload = self._read_layout_metadata(root / "oci-layout", "OCI layout marker")
        try:
            layout_document = json.loads(layout_payload)
        except json.JSONDecodeError as exc:
            raise OCIImageAttestationError("OCI layout marker is not JSON") from exc
        if layout_document != {"imageLayoutVersion": "1.0.0"}:
            raise OCIImageAttestationError("OCI layout version differs from 1.0.0")

        index_payload = self._read_layout_metadata(root / "index.json", "OCI image index")
        index = self._json_object(index_payload, label="OCI image index")
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list):
            raise OCIImageAttestationError("OCI image index omitted manifest descriptors")
        wanted_digest = f"sha256:{self._policy.image_manifest_sha256}"
        matches = [
            item
            for item in descriptors
            if isinstance(item, dict) and item.get("digest") == wanted_digest
        ]
        if len(matches) != 1:
            raise OCIImageAttestationError("OCI layout does not select one pinned manifest")
        descriptor = matches[0]
        if descriptor.get("mediaType") not in _OCI_MANIFEST_MEDIA_TYPES:
            raise OCIImageAttestationError("OCI index manifest media type is not supported")

        manifest_payload = self._read_blob(self._policy.image_manifest_sha256)
        if len(manifest_payload) != descriptor.get("size"):
            raise OCIImageAttestationError("OCI manifest size differs from its descriptor")
        manifest = self._json_object(manifest_payload, label="OCI image manifest")
        if (
            manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") not in _OCI_MANIFEST_MEDIA_TYPES
        ):
            raise OCIImageAttestationError("OCI manifest schema or media type is unsupported")
        config_descriptor = manifest.get("config")
        layers = manifest.get("layers")
        if not isinstance(config_descriptor, dict) or not isinstance(layers, list) or not layers:
            raise OCIImageAttestationError("OCI manifest omitted config or layers")
        if (
            config_descriptor.get("digest") != f"sha256:{self._policy.image_config_sha256}"
            or config_descriptor.get("mediaType") not in _OCI_CONFIG_MEDIA_TYPES
        ):
            raise OCIImageAttestationError("OCI manifest config differs from deployment pin")
        config_payload = self._read_blob(self._policy.image_config_sha256)
        if len(config_payload) != config_descriptor.get("size"):
            raise OCIImageAttestationError("OCI config size differs from manifest descriptor")
        config = self._json_object(config_payload, label="OCI image config")
        platform_os, platform_arch = self._policy.oci_platform.split("/", 1)
        if config.get("os") != platform_os or config.get("architecture") != platform_arch:
            raise OCIImageAttestationError("OCI config platform differs from deployment policy")
        rootfs = config.get("rootfs")
        if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
            raise OCIImageAttestationError("OCI config rootfs is not one layer chain")
        diff_ids = rootfs.get("diff_ids")
        if not isinstance(diff_ids, list) or not all(
            isinstance(item, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item)
            for item in diff_ids
        ):
            raise OCIImageAttestationError("OCI config diff IDs are not canonical SHA256s")
        if len(diff_ids) != len(layers):
            raise OCIImageAttestationError("OCI manifest layers and config diff IDs differ")

        gate_bytes: bytes | None = None
        gate_mode: int | None = None
        observed_diff_ids: list[str] = []
        layer_blob_sha256s: list[str] = []
        for position, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise OCIImageAttestationError("OCI layer descriptor is not typed")
            media_type = layer.get("mediaType")
            digest = layer.get("digest")
            size = layer.get("size")
            if (
                media_type not in _OCI_LAYER_MEDIA_TYPES
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise OCIImageAttestationError("OCI layer descriptor is unsupported")
            blob_sha256 = digest.removeprefix("sha256:")
            if self._blob_size(blob_sha256) != size:
                raise OCIImageAttestationError("OCI layer size differs from manifest descriptor")
            with self._uncompressed_layer(blob_sha256, media_type) as (layer_file, diff_id):
                if diff_id != diff_ids[position]:
                    raise OCIImageAttestationError("OCI layer diff ID differs from image config")
                gate_bytes, gate_mode = self._apply_gate_layer(
                    layer_file,
                    previous_bytes=gate_bytes,
                    previous_mode=gate_mode,
                )
            observed_diff_ids.append(diff_id)
            layer_blob_sha256s.append(blob_sha256)
        if gate_bytes is None or gate_mode is None:
            raise OCIImageAttestationError("OCI rootfs does not contain the pinned launch gate")
        gate_sha256 = hashlib.sha256(gate_bytes).hexdigest()
        if (
            len(gate_bytes) > _MAX_GATE_BYTES
            or gate_sha256 != self._policy.launch_gate_executable_sha256
            or not gate_mode & 0o111
            or gate_mode & 0o6022
        ):
            raise OCIImageAttestationError("OCI launch gate bytes or executable mode are unsafe")

        docker_image_id = docker_inspection.get("Id")
        repo_digests = docker_inspection.get("RepoDigests")
        docker_rootfs = docker_inspection.get("RootFS")
        if (
            docker_image_id != f"sha256:{self._policy.image_config_sha256}"
            or not isinstance(repo_digests, list)
            or not all(isinstance(item, str) for item in repo_digests)
            or not isinstance(docker_rootfs, dict)
            or docker_rootfs.get("Type") != "layers"
            or docker_rootfs.get("Layers") != diff_ids
            or docker_inspection.get("Os") != platform_os
            or docker_inspection.get("Architecture") != platform_arch
        ):
            raise OCIImageAttestationError("Docker image differs from OCI manifest/config chain")
        if "@sha256:" in self._policy.image_reference and (
            self._policy.image_reference not in repo_digests
        ):
            raise OCIImageAttestationError("Docker image lacks the pinned repository digest")
        return _OCIImageLayoutAttestation(
            policy_sha256=self._policy.policy_sha256,
            layout_pin_sha256=self._layout.pin_sha256,
            image_manifest_sha256=self._policy.image_manifest_sha256,
            image_config_sha256=self._policy.image_config_sha256,
            layer_blob_sha256s=tuple(layer_blob_sha256s),
            layer_diff_ids=tuple(observed_diff_ids),
            launch_gate_executable_sha256=gate_sha256,
            launch_gate_mode=gate_mode,
            docker_image_id=docker_image_id,
            docker_repo_digests=tuple(sorted(repo_digests)),
            docker_rootfs_diff_ids=tuple(diff_ids),
        )

    def _read_layout_metadata(self, path: Path, label: str) -> bytes:
        try:
            return _read_bounded_file(
                path,
                label=label,
                owner_uid=self._trusted_layout_owner_uid(),
                maximum_bytes=_MAX_CONTROL_BYTES,
            )
        except OCIDeploymentDependencyError as exc:
            raise OCIImageAttestationError(str(exc)) from exc

    def _blob_path(self, sha256: str) -> Path:
        if _SHA256.fullmatch(sha256) is None:
            raise OCIImageAttestationError("OCI blob digest is not canonical")
        return Path(self._layout.layout_root) / "blobs" / "sha256" / sha256

    def _blob_size(self, sha256: str) -> int:
        try:
            metadata = self._blob_path(sha256).lstat()
        except OSError as exc:
            raise OCIImageAttestationError("OCI blob is missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._trusted_layout_owner_uid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or metadata.st_size > self._layout.maximum_blob_bytes
        ):
            raise OCIImageAttestationError("OCI blob custody metadata is unsafe")
        return metadata.st_size

    def _read_blob(self, sha256: str) -> bytes:
        payload = _read_bounded_file(
            self._blob_path(sha256),
            label="OCI blob",
            owner_uid=self._trusted_layout_owner_uid(),
            maximum_bytes=self._layout.maximum_blob_bytes,
        )
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise OCIImageAttestationError("OCI blob bytes differ from descriptor digest")
        return payload

    @contextmanager
    def _uncompressed_layer(self, sha256: str, media_type: str) -> Iterator[tuple[object, str]]:
        path = self._blob_path(sha256)
        try:
            with _stable_regular_file(
                path,
                label="OCI layer blob",
                owner_uid=self._trusted_layout_owner_uid(),
                maximum_bytes=self._layout.maximum_blob_bytes,
                allowed_modes=frozenset({0o400, 0o440, 0o444}),
            ) as descriptor:
                compressed_hash = hashlib.sha256()
                with tempfile.TemporaryFile(mode="w+b") as compressed:
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        compressed_hash.update(chunk)
                        compressed.write(chunk)
                    if compressed_hash.hexdigest() != sha256:
                        raise OCIImageAttestationError(
                            "OCI layer bytes differ from descriptor digest"
                        )
                    compressed.seek(0)
                    source = (
                        gzip.GzipFile(fileobj=compressed, mode="rb")
                        if media_type.endswith("+gzip") or media_type.endswith(".gzip")
                        else compressed
                    )
                    with tempfile.TemporaryFile(mode="w+b") as uncompressed:
                        diff_hash = hashlib.sha256()
                        total = 0
                        try:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > self._layout.maximum_uncompressed_layer_bytes:
                                    raise OCIImageAttestationError(
                                        "OCI layer exceeds uncompressed deployment bound"
                                    )
                                diff_hash.update(chunk)
                                uncompressed.write(chunk)
                        except (OSError, EOFError) as exc:
                            raise OCIImageAttestationError(
                                "OCI layer decompression failed closed"
                            ) from exc
                        finally:
                            if source is not compressed:
                                source.close()
                        uncompressed.seek(0)
                        yield uncompressed, f"sha256:{diff_hash.hexdigest()}"
        except OCIDeploymentDependencyError as exc:
            raise OCIImageAttestationError(str(exc)) from exc

    @staticmethod
    def _trusted_layout_owner_uid() -> int:
        """Return the only production-trusted OCI-layout owner (patchable in unit tests)."""

        return 0

    def _apply_gate_layer(
        self,
        layer_file: object,
        *,
        previous_bytes: bytes | None,
        previous_mode: int | None,
    ) -> tuple[bytes | None, int | None]:
        target = PurePosixPath(self._policy.launch_gate_path.lstrip("/"))
        ancestors = tuple(target.parents)[:-1]
        target_parent = target.parent
        removed = False
        replacement: tuple[bytes, int] | None = None
        try:
            archive = tarfile.open(fileobj=layer_file, mode="r:")  # type: ignore[arg-type]
        except (tarfile.TarError, OSError) as exc:
            raise OCIImageAttestationError("OCI layer is not an uncompressed tar archive") from exc
        with archive:
            seen_paths: set[PurePosixPath] = set()
            for member in archive:
                path = self._safe_tar_path(member.name)
                if path in seen_paths:
                    raise OCIImageAttestationError("OCI layer contains duplicate tar paths")
                seen_paths.add(path)
                if path.name == ".wh..wh..opq" and (
                    path.parent == target_parent or path.parent in target_parent.parents
                ):
                    removed = True
                    continue
                if path.name.startswith(".wh."):
                    victim = path.parent / path.name.removeprefix(".wh.")
                    if victim == target or victim in target.parents:
                        removed = True
                    continue
                if path in ancestors and not member.isdir():
                    raise OCIImageAttestationError(
                        "OCI launch-gate ancestor was ever a symlink or non-directory"
                    )
                if path != target:
                    continue
                if not member.isfile() or member.issym() or member.islnk():
                    raise OCIImageAttestationError("OCI launch gate is not one regular file")
                if member.size > _MAX_GATE_BYTES:
                    raise OCIImageAttestationError("OCI launch gate exceeds its byte bound")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise OCIImageAttestationError("OCI launch gate bytes are unavailable")
                payload = extracted.read(_MAX_GATE_BYTES + 1)
                if len(payload) != member.size or len(payload) > _MAX_GATE_BYTES:
                    raise OCIImageAttestationError("OCI launch gate tar size is inconsistent")
                replacement = (payload, member.mode)
        if replacement is not None:
            return replacement
        if removed:
            return None, None
        return previous_bytes, previous_mode

    @staticmethod
    def _safe_tar_path(value: str) -> PurePosixPath:
        normalized = value.removeprefix("./")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise OCIImageAttestationError("OCI layer contains an unsafe tar path")
        return path

    @staticmethod
    def _json_object(payload: bytes, *, label: str) -> dict[str, object]:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCIImageAttestationError(f"{label} is not UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise OCIImageAttestationError(f"{label} is not one JSON object")
        return value

    def _docker_image_inspection(self) -> dict[str, object]:
        payload = self._run_pinned_docker(
            (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                self._policy.image_reference,
            )
        )
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OCIImageAttestationError("Docker image inspection is not JSON") from exc
        if not isinstance(value, dict):
            raise OCIImageAttestationError("Docker image inspection is not one object")
        return value

    def _run_pinned_docker(self, command: tuple[str, ...]) -> str:
        if sys.platform != "linux" or command[0] != self._policy.runtime_binary_path:
            raise OCIImageAttestationError("Docker inspection changed its pinned Linux binary")
        path = Path(self._policy.runtime_binary_path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIImageAttestationError("pinned Docker binary is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if (
                metadata.st_dev != self._policy.runtime_binary_device
                or metadata.st_ino != self._policy.runtime_binary_inode
                or metadata.st_uid != self._policy.runtime_binary_owner_uid
                or metadata.st_gid != self._policy.runtime_binary_owner_gid
                or stat.S_IMODE(metadata.st_mode) != self._policy.runtime_binary_mode
                or digest.hexdigest() != self._policy.runtime_binary_sha256
                or host_parent_chain_sha256(path) != self._policy.runtime_binary_parent_chain_sha256
            ):
                raise OCIImageAttestationError("Docker binary differs from deployment pin")
            completed = subprocess.run(
                command,
                executable=f"/proc/self/fd/{descriptor}",
                pass_fds=(descriptor,),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={},
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise OCIImageAttestationError("pinned Docker image inspection failed") from exc
        finally:
            os.close(descriptor)
        if completed.returncode != 0 or len(completed.stdout) > _MAX_CONTROL_BYTES:
            raise OCIImageAttestationError("Docker image inspection failed closed")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OCIImageAttestationError("Docker image inspection is not UTF-8") from exc


class LoopbackOutputQuotaController:
    """Prove that ``output_root`` is an exclusive, root-owned loop filesystem.

    A log limit, directory size observation, or userspace counter is intentionally insufficient.
    The loop block device uses the greatest sector-aligned capacity no larger than
    ``output_quota_bytes``, so even a compromised workload cannot write beyond the requested upper
    bound through the bind.
    """

    def __init__(
        self,
        *,
        policy: DeploymentPinnedOCIPolicy,
        journal_root: Path,
        backing_root: Path,
    ) -> None:
        self._policy = DeploymentPinnedOCIPolicy.model_validate(policy.model_dump(mode="python"))
        self._journal_root = _canonical_absolute_path(
            str(journal_root), label="OCI runtime journal root"
        )
        self._backing_root = _canonical_absolute_path(
            str(backing_root), label="loop quota backing root"
        )
        metadata = self._journal_root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or self._journal_root.is_symlink()
            or metadata.st_uid != self._policy.workload_uid
            or metadata.st_gid != self._policy.workload_gid
            or metadata.st_mode & 0o077
        ):
            raise OCIOutputQuotaError("OCI runtime journal root custody is unsafe")
        try:
            backing_metadata = self._backing_root.lstat()
        except OSError as exc:
            raise OCIOutputQuotaError("loop backing root is unavailable") from exc
        if (
            not stat.S_ISDIR(backing_metadata.st_mode)
            or self._backing_root.is_symlink()
            or backing_metadata.st_uid != 0
            or backing_metadata.st_gid != 0
            or stat.S_IMODE(backing_metadata.st_mode) != 0o700
        ):
            raise OCIOutputQuotaError("loop backing root custody is unsafe")

    def verify_enforced_quota(
        self,
        *,
        output_root: Path,
        output_quota_bytes: int,
        execution_id: str,
        infrastructure_attempt_id: str,
        runtime_id: str,
        expected_evidence_sha256: str,
    ) -> str:
        if sys.platform != "linux":
            raise OCIOutputQuotaError("loop-backed output quota verification is Linux-only")
        output_root = _canonical_absolute_path(str(output_root), label="output quota root")
        if output_root.is_symlink() or output_quota_bytes <= 0:
            raise OCIOutputQuotaError("output quota root or byte limit is unsafe")
        try:
            root_stat = output_root.lstat()
            parent_stat = output_root.parent.lstat()
        except OSError as exc:
            raise OCIOutputQuotaError("output quota mount identity is unavailable") from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != self._policy.workload_uid
            or root_stat.st_gid != self._policy.workload_gid
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or root_stat.st_dev == parent_stat.st_dev
        ):
            raise OCIOutputQuotaError("output root is not an exclusive owner-only mount")
        plan = self._read_plan(output_root, runtime_id)
        receipt = plan.output_quota_provisioning_receipt
        if (
            plan.execution_id != execution_id
            or plan.infrastructure_attempt_id != infrastructure_attempt_id
            or plan.output_quota_bytes != output_quota_bytes
            or receipt.output_root_device != root_stat.st_dev
            or receipt.output_root_inode != root_stat.st_ino
            or receipt.output_root_owner_uid != root_stat.st_uid
            or receipt.output_root_owner_gid != root_stat.st_gid
            or receipt.output_root_mode != stat.S_IMODE(root_stat.st_mode)
        ):
            raise OCIOutputQuotaError("output quota request differs from its durable receipt")
        expected = canonical_sha256(
            {
                "schema": "aletheia.host_output_project_quota_challenge.v2",
                "execution_id": execution_id,
                "infrastructure_attempt_id": infrastructure_attempt_id,
                "runtime_id": runtime_id,
                "enforced_placement_sha256": plan.enforced_placement_sha256,
                "output_root": str(output_root),
                "output_root_device": root_stat.st_dev,
                "output_root_inode": root_stat.st_ino,
                "output_root_owner_uid": root_stat.st_uid,
                "output_root_owner_gid": root_stat.st_gid,
                "output_quota_bytes": output_quota_bytes,
            }
        )
        # The runtime challenge includes enforced_placement_sha256 but the controller protocol
        # does not pass it.  The constructor-pinned journal root is therefore mandatory; returning
        # the supplied hash without resolving the durable plan would be an echo.
        if expected_evidence_sha256 != expected:
            raise OCIOutputQuotaError("output quota challenge differs from durable placement")
        mount = self._exact_mount(output_root, root_stat.st_dev)
        backing_identity = self._verify_loop_device(
            major=os.major(root_stat.st_dev),
            minor=os.minor(root_stat.st_dev),
            source=mount["source"],
            block_device_capacity_bytes=receipt.block_device_capacity_bytes,
        )
        filesystem_uuid_sha256 = self._filesystem_uuid_sha256(
            source=str(mount["source"]),
            major=os.major(root_stat.st_dev),
            minor=os.minor(root_stat.st_dev),
        )
        if (
            receipt.mount_id != mount["mount_id"]
            or receipt.mount_parent_id != mount["mount_parent_id"]
            or receipt.block_device_major != mount["major"]
            or receipt.block_device_minor != mount["minor"]
            or receipt.filesystem_type != mount["fstype"]
            or receipt.mount_options != tuple(sorted(mount["mount_options"]))
            or receipt.backing_file_identity_sha256 != backing_identity
            or receipt.filesystem_uuid_sha256 != filesystem_uuid_sha256
        ):
            raise OCIOutputQuotaError("live loop mount differs from provisioning receipt")
        filesystem = os.statvfs(output_root)
        if (
            filesystem.f_frsize <= 0
            or filesystem.f_blocks * filesystem.f_frsize > receipt.block_device_capacity_bytes
        ):
            raise OCIOutputQuotaError("mounted filesystem capacity exceeds output quota")
        if _stat_identity(root_stat) != _stat_identity(output_root.lstat()):
            raise OCIOutputQuotaError("output quota mount changed while verified")
        return expected

    def _read_plan(self, output_root: Path, runtime_id: str) -> OCIExecutionPlan:
        runtime_key = hashlib.sha256(
            b"ALETHEIA_QUALIFICATION_OCI_RUNTIME_V2\x00" + runtime_id.encode("utf-8")
        ).hexdigest()
        plan = _load_exact_model(
            self._journal_root / runtime_key / "plan.json",
            OCIExecutionPlan,
            owner_uid=self._policy.workload_uid,
        )
        assert isinstance(plan, OCIExecutionPlan)
        if plan.runtime_id != runtime_id or plan.output_root != str(output_root):
            raise OCIOutputQuotaError("output quota request differs from durable runtime plan")
        return plan

    def _exact_mount(self, output_root: Path, device: int) -> dict[str, object]:
        try:
            payload = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise OCIOutputQuotaError("Linux mountinfo is unavailable") from exc
        matches: list[dict[str, object]] = []
        for line in payload.splitlines():
            parsed = self._parse_mountinfo(line)
            if parsed["mountpoint"] == str(output_root):
                matches.append(parsed)
        if len(matches) != 1:
            raise OCIOutputQuotaError("output root is not one exact mountinfo entry")
        mount = matches[0]
        required_options = {"rw", "nosuid", "nodev", "noexec"}
        if (
            mount["major"] != os.major(device)
            or mount["minor"] != os.minor(device)
            or mount["fstype"] not in {"ext4", "xfs"}
            or re.fullmatch(r"/dev/loop[0-9]+", str(mount["source"])) is None
            or not required_options.issubset(mount["mount_options"])
            or "rw" not in mount["super_options"]
        ):
            raise OCIOutputQuotaError("output mount is not the required loop filesystem")
        return mount

    @staticmethod
    def _parse_mountinfo(line: str) -> dict[str, object]:
        left, separator, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not separator or len(left_fields) < 6 or len(right_fields) < 3:
            raise OCIOutputQuotaError("mountinfo contains an unparseable entry")
        try:
            major_text, minor_text = left_fields[2].split(":", 1)
            major, minor = int(major_text), int(minor_text)
        except ValueError as exc:
            raise OCIOutputQuotaError("mountinfo device identity is invalid") from exc
        return {
            "mount_id": int(left_fields[0]),
            "mount_parent_id": int(left_fields[1]),
            "major": major,
            "minor": minor,
            "mountpoint": LoopbackOutputQuotaController._unescape_mountinfo(left_fields[4]),
            "mount_options": frozenset(left_fields[5].split(",")),
            "fstype": right_fields[0],
            "source": LoopbackOutputQuotaController._unescape_mountinfo(right_fields[1]),
            "super_options": frozenset(right_fields[2].split(",")),
        }

    @staticmethod
    def _unescape_mountinfo(value: str) -> str:
        replacements = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}
        for encoded, decoded in replacements.items():
            value = value.replace(encoded, decoded)
        return value

    def _verify_loop_device(
        self,
        *,
        major: int,
        minor: int,
        source: object,
        block_device_capacity_bytes: int,
    ) -> str:
        sysfs = Path(f"/sys/dev/block/{major}:{minor}")
        try:
            resolved = sysfs.resolve(strict=True)
            size_text = (resolved / "size").read_text(encoding="ascii").strip()
            read_only = (resolved / "ro").read_text(encoding="ascii").strip()
            backing_text = (resolved / "loop" / "backing_file").read_text(encoding="utf-8").strip()
            autoclear = (resolved / "loop" / "autoclear").read_text(encoding="ascii").strip()
            sectors = int(size_text)
        except (OSError, UnicodeError, ValueError) as exc:
            raise OCIOutputQuotaError("loop device sysfs identity is unavailable") from exc
        if (
            resolved.name != str(source).removeprefix("/dev/")
            or sectors * _SECTOR_BYTES != block_device_capacity_bytes
            or read_only != "0"
            or autoclear != "0"
        ):
            raise OCIOutputQuotaError("loop block-device capacity or lifetime differs from quota")
        try:
            source_metadata = Path(str(source)).lstat()
        except OSError as exc:
            raise OCIOutputQuotaError("loop block-device node is unavailable") from exc
        if (
            not stat.S_ISBLK(source_metadata.st_mode)
            or os.major(source_metadata.st_rdev) != major
            or os.minor(source_metadata.st_rdev) != minor
            or source_metadata.st_uid != 0
            or source_metadata.st_gid != 0
            or source_metadata.st_mode & 0o022
        ):
            raise OCIOutputQuotaError("loop block-device node custody is unsafe")
        backing = Path("/") / self._unescape_mountinfo(backing_text).lstrip("/")
        try:
            backing = backing.resolve(strict=True)
            backing.relative_to(self._backing_root)
            metadata = backing.lstat()
            host_parent_chain_sha256(backing)
        except (OSError, ValueError) as exc:
            raise OCIOutputQuotaError(
                "loop backing file escaped its root-owned deployment root"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != block_device_capacity_bytes
        ):
            raise OCIOutputQuotaError("loop backing file custody or capacity is unsafe")
        return canonical_sha256(
            {
                "schema": "aletheia.loop_output_quota_backing_file_identity.v2",
                "path": str(backing),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "link_count": metadata.st_nlink,
                "size": metadata.st_size,
            }
        )

    @staticmethod
    def _filesystem_uuid_sha256(*, source: str, major: int, minor: int) -> str:
        source_path = Path(source)
        try:
            source_resolved = source_path.resolve(strict=True)
            metadata = source_resolved.lstat()
            candidates = tuple(Path("/dev/disk/by-uuid").iterdir())
        except OSError as exc:
            raise OCIOutputQuotaError("filesystem UUID identity is unavailable") from exc
        if (
            not stat.S_ISBLK(metadata.st_mode)
            or os.major(metadata.st_rdev) != major
            or os.minor(metadata.st_rdev) != minor
        ):
            raise OCIOutputQuotaError("filesystem UUID source differs from loop device")
        matches: list[str] = []
        for candidate in candidates:
            try:
                if not candidate.is_symlink() or candidate.resolve(strict=True) != source_resolved:
                    continue
            except OSError as exc:
                raise OCIOutputQuotaError("filesystem UUID link is unsafe") from exc
            if re.fullmatch(r"[0-9a-fA-F-]{8,64}", candidate.name) is None:
                raise OCIOutputQuotaError("filesystem UUID name is not canonical")
            matches.append(candidate.name.lower())
        if len(matches) != 1:
            raise OCIOutputQuotaError("loop filesystem lacks one exact UUID")
        return hashlib.sha256(matches[0].encode("ascii")).hexdigest()


class PinnedRootFile(ExecutionModel):
    """Exact immutable root-owned deployment file."""

    path: str = Field(min_length=2, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: Literal[0] = 0
    owner_gid: Literal[0] = 0
    mode: int = Field(ge=0, le=0o7777)
    parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _file_is_canonical(self) -> "PinnedRootFile":
        _canonical_absolute_path(self.path, label="root deployment file")
        if self.mode & 0o022:
            raise ValueError("root deployment file mode is writable outside root")
        return self


class PinnedRootExecutable(PinnedRootFile):
    """Exact root-owned executable used by one narrow privileged service."""

    @model_validator(mode="after")
    def _executable_is_canonical(self) -> "PinnedRootExecutable":
        if not self.mode & 0o111:
            raise ValueError("privileged deployment executable is not executable")
        return self


def _verify_root_file_pin(
    pin: PinnedRootFile,
    *,
    label: str,
    error_type: type[OCIDeploymentDependencyError],
) -> os.stat_result:
    path = Path(pin.path)
    try:
        if path.resolve(strict=True) != path:
            raise error_type(f"{label} path is not canonical")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise error_type(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != pin.device
            or metadata.st_ino != pin.inode
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != pin.mode
            or digest.hexdigest() != pin.sha256
            or host_parent_chain_sha256(path) != pin.parent_chain_sha256
        ):
            raise error_type(f"{label} differs from deployment pin")
        return metadata
    finally:
        os.close(descriptor)


def _verify_root_process_executable(
    pin: PinnedRootExecutable,
    *,
    error_type: type[OCIDeploymentDependencyError],
) -> None:
    pinned = _verify_root_file_pin(
        pin,
        label="root service executable",
        error_type=error_type,
    )
    try:
        process_path = Path("/proc/self/exe").resolve(strict=True)
        process_metadata = Path("/proc/self/exe").stat()
    except OSError as exc:
        raise error_type("root service process executable is unavailable") from exc
    if (
        process_path != Path(pin.path)
        or process_metadata.st_dev != pinned.st_dev
        or process_metadata.st_ino != pinned.st_ino
    ):
        raise error_type("root service process differs from deployment executable")


def _verify_pinned_directory(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    device: int,
    inode: int,
    parent_chain_sha256: str,
    label: str,
    error_type: type[OCIDeploymentDependencyError],
) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise error_type(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_dev != device
            or metadata.st_ino != inode
            or host_parent_chain_sha256(path) != parent_chain_sha256
        ):
            raise error_type(f"{label} differs from deployment pin")
    finally:
        os.close(descriptor)


def _in_exact_systemd_unit(cgroup_payload: str, unit_name: str) -> bool:
    matches: list[str] = []
    for line in cgroup_payload.splitlines():
        hierarchy, separator, remainder = line.partition(":")
        controllers, separator_two, cgroup_path = remainder.partition(":")
        if separator and separator_two and hierarchy == "0" and controllers == "":
            matches.append(cgroup_path)
    if len(matches) != 1:
        return False
    path = PurePosixPath(matches[0])
    return path.is_absolute() and path.name == unit_name and ".." not in path.parts


class LoopbackQuotaProvisionerDeploymentPin(ExecutionModel):
    """Closed root-service deployment for output loop-filesystem provisioning."""

    schema_name: Literal["aletheia.loopback_output_quota_provisioner_deployment"] = (
        "aletheia.loopback_output_quota_provisioner_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
    systemd_unit_name: str
    workspace_root: str = Field(min_length=2, max_length=4096)
    workspace_root_pin: PinnedOutputWorkspaceRoot
    backing_root: str = Field(min_length=2, max_length=4096)
    state_root: str = Field(min_length=2, max_length=4096)
    socket_path: str = Field(min_length=2, max_length=4096)
    allowed_client_uid: int = Field(ge=1, le=2**31 - 1)
    allowed_client_gid: int = Field(ge=1, le=2**31 - 1)
    provisioner_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provisioner_principal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
    filesystem_type: Literal["ext4"] = "ext4"
    minimum_loop_filesystem_bytes: int = Field(
        default=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        ge=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
        le=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    )
    systemd_unit: PinnedRootFile
    service_executable: PinnedRootExecutable
    losetup: PinnedRootExecutable
    mkfs: PinnedRootExecutable
    mount: PinnedRootExecutable
    service_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_module_device: int = Field(ge=0)
    service_module_inode: int = Field(ge=1)
    service_module_mode: Literal[0o400, 0o440, 0o444]
    service_module_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backing_root_device: int = Field(ge=0)
    backing_root_inode: int = Field(ge=1)
    backing_root_mode: Literal[0o700] = 0o700
    backing_root_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_root_device: int = Field(ge=0)
    state_root_inode: int = Field(ge=1)
    state_root_mode: Literal[0o700] = 0o700
    state_root_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    socket_parent_device: int = Field(ge=0)
    socket_parent_inode: int = Field(ge=1)
    socket_parent_mode: Literal[0o755] = 0o755
    socket_parent_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_only: Literal[True] = True

    @model_validator(mode="after")
    def _deployment_paths_are_separate(self) -> "LoopbackQuotaProvisionerDeploymentPin":
        if (
            re.fullmatch(
                r"aletheia-qualification-output-quota(?:-[a-z0-9_.-]+)?\.service",
                self.systemd_unit_name,
            )
            is None
        ):
            raise ValueError("quota provisioner systemd unit name is not deployment-scoped")
        if Path(self.systemd_unit.path).name != self.systemd_unit_name:
            raise ValueError("quota provisioner unit file does not match its unit name")
        if (
            self.workspace_root != self.workspace_root_pin.path
            or self.allowed_client_gid != self.workspace_root_pin.owner_gid
        ):
            raise ValueError("quota workspace path/principal differs from shared root pin")
        roots = tuple(
            _canonical_absolute_path(value, label="quota provisioner custody path")
            for value in (
                self.workspace_root,
                self.backing_root,
                self.state_root,
                str(Path(self.socket_path).parent),
            )
        )
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise ValueError("quota provisioner workspace/backing/state/socket roots overlap")
        _canonical_absolute_path(self.socket_path, label="quota provisioner socket")
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self)


class _QuotaProvisioningIntent(ExecutionModel):
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: str
    boot_id: str
    execution_id: str = Field(pattern=r"^exe_[0-9a-f]{32}$")
    infrastructure_attempt_id: str = Field(pattern=r"^iat_[0-9a-f]{32}$")
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_root: str
    output_quota_bytes: int = Field(ge=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES)
    block_device_capacity_bytes: int = Field(ge=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES)
    underlying_root_device: int = Field(ge=0)
    underlying_root_inode: int = Field(ge=1)
    backing_file: str
    filesystem_uuid: str
    created_at: AwareDatetime
    service_boot_id: str

    @model_validator(mode="after")
    def _capacity_is_a_safe_request_floor(self) -> "_QuotaProvisioningIntent":
        if (
            self.block_device_capacity_bytes % _SECTOR_BYTES
            or self.block_device_capacity_bytes > self.output_quota_bytes
        ):
            raise ValueError("quota intent capacity is not a sector-aligned request floor")
        return self

    @property
    def intent_record_sha256(self) -> str:
        return canonical_sha256(self)


class _QuotaLoopAttachment(ExecutionModel):
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_device: str
    backing_file_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attached_at: AwareDatetime

    @property
    def attachment_record_sha256(self) -> str:
        return canonical_sha256(self)


class _QuotaFilesystemFormatted(ExecutionModel):
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attachment_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filesystem_type: Literal["ext4"] = "ext4"
    filesystem_uuid: str
    filesystem_uuid_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formatted_at: AwareDatetime


def _attempt_workspace_key(attempt_id: str) -> str:
    if re.fullmatch(r"iat_[0-9a-f]{32}", attempt_id) is None:
        raise OCIOutputQuotaError("output quota attempt id is not canonical")
    return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()


def _loop_backing_identity(
    path: Path,
    *,
    expected_bytes: int,
    expected_owner_uid: int = 0,
    expected_owner_gid: int = 0,
) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OCIOutputQuotaError("loop backing file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_owner_uid
        or metadata.st_gid != expected_owner_gid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != expected_bytes
    ):
        raise OCIOutputQuotaError("loop backing file custody differs from provisioning intent")
    return canonical_sha256(
        {
            "schema": "aletheia.loop_output_quota_backing_file_identity.v2",
            "path": str(path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "owner_uid": metadata.st_uid,
            "owner_gid": metadata.st_gid,
            "mode": stat.S_IMODE(metadata.st_mode),
            "link_count": metadata.st_nlink,
            "size": metadata.st_size,
        }
    )


class LoopbackOutputQuotaProvisioningService:
    """Root/systemd service that owns loop setup, formatting, and mounting.

    The service accepts exactly one attempt-derived ``.../workspaces/<sha>/output`` path.  It
    publishes every irreversible phase before advancing, so a service restart either resumes the
    same generation or fails closed; it never provisions a replacement for an expected receipt.
    """

    def __init__(self, deployment: LoopbackQuotaProvisionerDeploymentPin) -> None:
        self._deployment = LoopbackQuotaProvisionerDeploymentPin.model_validate(
            deployment.model_dump(mode="python")
        )
        self._stop_event = threading.Event()

    def serve_forever(self) -> None:
        self._require_root_systemd_service()
        self._prepare_root(Path(self._deployment.backing_root), mode=0o700)
        self._prepare_root(Path(self._deployment.state_root), mode=0o700)
        lock_path = Path(self._deployment.state_root) / "service.lock"
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        server: socket.socket | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server = self._create_server_socket()
            server.settimeout(1)
            while not self._stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(2)
                    try:
                        self._serve_connection(connection)
                    except (OCIDeploymentDependencyError, OSError, TimeoutError, ValueError):
                        # A malformed authenticated request must not stop recovery for other mounts.
                        continue
        except BlockingIOError as exc:
            raise OCIOutputQuotaError("another quota service instance owns the deployment") from exc
        finally:
            if server is not None:
                server.close()
            try:
                Path(self._deployment.socket_path).unlink()
            except FileNotFoundError:
                pass
            os.close(lock_fd)

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _trusted_root_service_uid() -> int:
        """Production root uid (patchable only in unprivileged custody tests)."""

        return 0

    @contextmanager
    def _sealed_output_target(self, attempt_id: str) -> Iterator[int]:
        """Open the exact two-component target and remove peer-UID rename authority.

        The deployment workspace is an independently mounted, root-owned sticky directory.  The
        node UID may create a new attempt directory there, but after this service opens both fixed
        path components it transfers the attempt directory to root.  Sticky-parent semantics then
        prevent any process under the exclusively reserved node UID from renaming that attempt;
        mode 0710 retains traversal for the node while preventing child replacement.  All opens
        are one-component ``openat`` operations with ``O_NOFOLLOW``, which is equivalent to
        NO_SYMLINKS+BENEATH for this fixed two-level layout.
        """

        workspace = Path(self._deployment.workspace_root)
        workspace_descriptor: int | None = None
        attempt_descriptor: int | None = None
        output_descriptor: int | None = None
        try:
            workspace_descriptor = os.open(
                workspace,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            workspace_metadata = os.fstat(workspace_descriptor)
            mount = self._find_mount(workspace)
            if (
                not stat.S_ISDIR(workspace_metadata.st_mode)
                or workspace_metadata.st_uid != self._trusted_root_service_uid()
                or workspace_metadata.st_gid != self._deployment.allowed_client_gid
                or stat.S_IMODE(workspace_metadata.st_mode)
                != self._deployment.workspace_root_pin.mode
                or workspace_metadata.st_dev != self._deployment.workspace_root_pin.device
                or workspace_metadata.st_ino != self._deployment.workspace_root_pin.inode
                or mount is None
                or mount["mount_id"] != self._deployment.workspace_root_pin.mount_id
                or host_parent_chain_sha256(workspace)
                != self._deployment.workspace_root_pin.parent_chain_sha256
            ):
                raise OCIOutputQuotaError("quota workspace root differs from pinned mount custody")
            attempt_name = _attempt_workspace_key(attempt_id)
            attempt_descriptor = os.open(
                attempt_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=workspace_descriptor,
            )
            before_attempt = os.fstat(attempt_descriptor)
            output_descriptor = os.open(
                "output",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=attempt_descriptor,
            )
            if (
                before_attempt.st_uid == self._deployment.allowed_client_uid
                and before_attempt.st_gid == self._deployment.allowed_client_gid
                and stat.S_IMODE(before_attempt.st_mode) == 0o700
            ):
                os.fchown(
                    attempt_descriptor,
                    self._trusted_root_service_uid(),
                    self._deployment.allowed_client_gid,
                )
                _durable_publish_checkpoint(
                    "attempt-root-chowned-before-mode",
                    workspace / attempt_name,
                )
                os.fchmod(attempt_descriptor, 0o710)
                _durable_publish_checkpoint(
                    "attempt-root-moded-before-fsync",
                    workspace / attempt_name,
                )
            elif (
                before_attempt.st_uid == self._trusted_root_service_uid()
                and before_attempt.st_gid == self._deployment.allowed_client_gid
                and stat.S_IMODE(before_attempt.st_mode) == 0o700
            ):
                # Crash after fchown but before fchmod: only root can repair this exact phase.
                os.fchmod(attempt_descriptor, 0o710)
                _durable_publish_checkpoint(
                    "attempt-root-moded-before-fsync",
                    workspace / attempt_name,
                )
            elif (
                before_attempt.st_uid != self._trusted_root_service_uid()
                or before_attempt.st_gid != self._deployment.allowed_client_gid
                or stat.S_IMODE(before_attempt.st_mode) != 0o710
            ):
                raise OCIOutputQuotaError("attempt workspace directory custody is unsafe")
            # Also flush the already-final 0710 replay branch.  A previous process may have
            # crashed after fchmod returned but before either the inode or its parent dentry was
            # durable; observing the final metadata is not evidence that those writes reached
            # stable storage.
            os.fsync(attempt_descriptor)
            os.fsync(workspace_descriptor)
            reopened_attempt = os.open(
                attempt_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=workspace_descriptor,
            )
            try:
                current_attempt = os.fstat(reopened_attempt)
                sealed_attempt = os.fstat(attempt_descriptor)
                if (
                    current_attempt.st_dev != sealed_attempt.st_dev
                    or current_attempt.st_ino != sealed_attempt.st_ino
                    or current_attempt.st_uid != self._trusted_root_service_uid()
                    or current_attempt.st_gid != self._deployment.allowed_client_gid
                    or stat.S_IMODE(current_attempt.st_mode) != 0o710
                ):
                    raise OCIOutputQuotaError("attempt workspace changed while root sealed it")
                reopened_output = os.open(
                    "output",
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=reopened_attempt,
                )
                try:
                    original_output = os.fstat(output_descriptor)
                    current_output = os.fstat(reopened_output)
                    if (
                        original_output.st_dev != current_output.st_dev
                        or original_output.st_ino != current_output.st_ino
                    ):
                        raise OCIOutputQuotaError("output target changed while parent was sealed")
                finally:
                    os.close(reopened_output)
            finally:
                os.close(reopened_attempt)
            yield output_descriptor
        except OSError as exc:
            raise OCIOutputQuotaError("quota target lineage cannot be safely opened") from exc
        finally:
            if output_descriptor is not None:
                os.close(output_descriptor)
            if attempt_descriptor is not None:
                os.close(attempt_descriptor)
            if workspace_descriptor is not None:
                os.close(workspace_descriptor)

    def _validate_receipt_request(
        self,
        receipt: OutputQuotaProvisioningReceipt,
        request: Mapping[str, object],
    ) -> None:
        supplied = (
            self._required_text(request, "node_manifest_sha256"),
            self._required_text(request, "node_id"),
            self._required_text(request, "boot_id"),
            self._required_text(request, "execution_id"),
            self._required_text(request, "attempt_id"),
            self._required_text(request, "intent_sha256"),
            self._required_text(request, "output_root"),
            self._required_int(request, "output_quota_bytes"),
        )
        durable = (
            receipt.node_manifest_sha256,
            receipt.node_id,
            receipt.boot_id,
            receipt.execution_id,
            receipt.infrastructure_attempt_id,
            receipt.intent_sha256,
            receipt.output_root,
            receipt.output_quota_bytes,
        )
        if supplied != durable:
            raise OCIOutputQuotaError("quota receipt differs from closed service request")

    def ensure(self, request: Mapping[str, object]) -> OutputQuotaProvisioningReceipt:
        if set(request) != {
            "node_manifest_sha256",
            "node_id",
            "boot_id",
            "execution_id",
            "attempt_id",
            "intent_sha256",
            "output_root",
            "output_quota_bytes",
            "expected_receipt",
        }:
            raise OCIOutputQuotaError("quota service request shape is not closed")
        attempt_id = self._required_text(request, "attempt_id")
        expected_payload = request.get("expected_receipt")
        expected = (
            None
            if expected_payload == "none"
            else OutputQuotaProvisioningReceipt.model_validate(expected_payload)
        )
        with self._sealed_output_target(attempt_id) as output_descriptor:
            generation_root = self._generation_root(attempt_id)
            try:
                generation_root.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                pass
            else:
                _durable_publish_checkpoint(
                    "quota-generation-directory-created-before-parent-fsync",
                    generation_root,
                )
            self._require_root_directory(generation_root, mode=0o700)
            # Also fsync on replay: a previous process may have died after mkdir returned but
            # before the parent dentry was durable.  Existence alone is not durability evidence.
            self._fsync_directory(generation_root.parent)
            _durable_publish_checkpoint(
                "quota-generation-directory-fsynced",
                generation_root,
            )
            lock_fd = os.open(
                generation_root / "generation.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                final_path = generation_root / "receipt.json"
                recovered_receipt = self._recover_root_model(
                    final_path,
                    OutputQuotaProvisioningReceipt,
                )
                if recovered_receipt is not None:
                    receipt = recovered_receipt
                    assert isinstance(receipt, OutputQuotaProvisioningReceipt)
                    self._validate_receipt_request(receipt, request)
                    if expected is not None and receipt != expected:
                        raise OCIOutputQuotaError(
                            "expected quota receipt differs from root journal"
                        )
                    self._verify_live_receipt(receipt)
                    return receipt
                if expected is not None:
                    raise OCIOutputQuotaError(
                        "expected quota receipt has no durable root-service generation"
                    )
                intent = self._ensure_intent(
                    request,
                    generation_root=generation_root,
                    output_descriptor=output_descriptor,
                )
                backing_identity = self._ensure_backing_file(intent)
                attachment = self._ensure_loop_attachment(
                    intent,
                    backing_identity=backing_identity,
                    generation_root=generation_root,
                )
                formatted = self._ensure_formatted(
                    intent, attachment=attachment, generation_root=generation_root
                )
                receipt = self._ensure_mounted(
                    intent,
                    attachment=attachment,
                    formatted=formatted,
                    output_descriptor=output_descriptor,
                )
                _durable_publish_checkpoint(
                    "quota-mounted-before-receipt",
                    Path(receipt.output_root),
                )
                published_receipt = self._publish_root_model(final_path, receipt)
                assert isinstance(published_receipt, OutputQuotaProvisioningReceipt)
                self._validate_receipt_request(published_receipt, request)
                self._verify_live_receipt(published_receipt)
                return published_receipt
            finally:
                os.close(lock_fd)

    def _ensure_intent(
        self,
        request: Mapping[str, object],
        *,
        generation_root: Path,
        output_descriptor: int,
    ) -> _QuotaProvisioningIntent:
        attempt_id = self._required_text(request, "attempt_id")
        output_root = Path(self._required_text(request, "output_root"))
        expected_output = (
            Path(self._deployment.workspace_root) / _attempt_workspace_key(attempt_id) / "output"
        )
        quota = self._required_int(request, "output_quota_bytes")
        if quota < MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES:
            raise OCIOutputQuotaError("loop output quota is below the deployment filesystem floor")
        capacity = quota - (quota % _SECTOR_BYTES)
        path = generation_root / "intent.json"
        recovered_intent = self._recover_root_model(path, _QuotaProvisioningIntent)
        if recovered_intent is not None:
            stored = recovered_intent
            assert isinstance(stored, _QuotaProvisioningIntent)
            supplied_scope = (
                self._deployment.deployment_sha256,
                self._required_text(request, "node_manifest_sha256"),
                self._required_text(request, "node_id"),
                self._required_text(request, "boot_id"),
                self._required_text(request, "execution_id"),
                attempt_id,
                self._required_text(request, "intent_sha256"),
                str(output_root),
                quota,
                capacity,
                str(
                    Path(self._deployment.backing_root)
                    / f"{_attempt_workspace_key(attempt_id)}.img"
                ),
            )
            stored_scope = (
                stored.deployment_sha256,
                stored.node_manifest_sha256,
                stored.node_id,
                stored.boot_id,
                stored.execution_id,
                stored.infrastructure_attempt_id,
                stored.intent_sha256,
                stored.output_root,
                stored.output_quota_bytes,
                stored.block_device_capacity_bytes,
                stored.backing_file,
            )
            if supplied_scope != stored_scope:
                raise OCIOutputQuotaError("quota provisioning intent changed during recovery")
            mount = self._find_mount(output_root)
            observed = os.fstat(output_descriptor)
            if mount is None and (
                observed.st_dev != stored.underlying_root_device
                or observed.st_ino != stored.underlying_root_inode
            ):
                raise OCIOutputQuotaError("unmounted quota target differs from durable inode")
            return stored
        metadata = os.fstat(output_descriptor)
        if (
            output_root != expected_output
            or self._find_mount(output_root) is not None
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._deployment.allowed_client_uid
            or metadata.st_gid != self._deployment.allowed_client_gid
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or any(output_root.iterdir())
        ):
            raise OCIOutputQuotaError("quota service request escaped empty attempt workspace")
        filesystem_uuid = str(
            uuid.UUID(bytes=hashlib.sha256(attempt_id.encode("ascii")).digest()[:16])
        )
        expected_intent = _QuotaProvisioningIntent(
            deployment_sha256=self._deployment.deployment_sha256,
            node_manifest_sha256=self._required_text(request, "node_manifest_sha256"),
            node_id=self._required_text(request, "node_id"),
            boot_id=self._required_text(request, "boot_id"),
            execution_id=self._required_text(request, "execution_id"),
            infrastructure_attempt_id=attempt_id,
            intent_sha256=self._required_text(request, "intent_sha256"),
            output_root=str(output_root),
            output_quota_bytes=quota,
            block_device_capacity_bytes=capacity,
            underlying_root_device=metadata.st_dev,
            underlying_root_inode=metadata.st_ino,
            backing_file=str(
                Path(self._deployment.backing_root) / f"{_attempt_workspace_key(attempt_id)}.img"
            ),
            filesystem_uuid=filesystem_uuid,
            created_at=datetime.now(timezone.utc),
            service_boot_id=self._current_boot_id(),
        )
        published_intent = self._publish_root_model(path, expected_intent)
        assert isinstance(published_intent, _QuotaProvisioningIntent)
        if published_intent.model_dump(
            mode="python", exclude={"created_at", "service_boot_id"}
        ) != expected_intent.model_dump(mode="python", exclude={"created_at", "service_boot_id"}):
            raise OCIOutputQuotaError("published quota intent changed stable generation scope")
        return published_intent

    def _ensure_backing_file(self, intent: _QuotaProvisioningIntent) -> str:
        path = Path(intent.backing_file)
        if not path.exists():
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_directory(path.parent)
            _durable_publish_checkpoint("backing-created", path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise OCIOutputQuotaError("loop backing file disappeared during recovery") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._trusted_state_owner_uid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size not in (0, intent.block_device_capacity_bytes)
        ):
            raise OCIOutputQuotaError("partial loop backing file is not recoverable")
        descriptor = os.open(
            path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size != metadata.st_size
            ):
                raise OCIOutputQuotaError("loop backing inode changed before durability check")
            if metadata.st_size == 0:
                os.ftruncate(descriptor, intent.block_device_capacity_bytes)
                _durable_publish_checkpoint("backing-sized-before-fsync", path)
            # Always flush an already capacity-sized inode.  It can be the residue of a crash
            # after ftruncate returned but before the first file fsync completed.
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_directory(path.parent)
        return _loop_backing_identity(
            path,
            expected_bytes=intent.block_device_capacity_bytes,
            expected_owner_uid=self._trusted_state_owner_uid(),
            expected_owner_gid=os.getegid(),
        )

    def _ensure_loop_attachment(
        self,
        intent: _QuotaProvisioningIntent,
        *,
        backing_identity: str,
        generation_root: Path,
    ) -> _QuotaLoopAttachment:
        path = generation_root / "loop-attached.json"
        recovered_attachment = self._recover_root_model(path, _QuotaLoopAttachment)
        if recovered_attachment is not None:
            stored = recovered_attachment
            assert isinstance(stored, _QuotaLoopAttachment)
            if (
                stored.intent_record_sha256 != intent.intent_record_sha256
                or stored.backing_file_identity_sha256 != backing_identity
            ):
                raise OCIOutputQuotaError("loop attachment changed during recovery")
            self._verify_loop_association(
                loop_device=stored.loop_device,
                backing_file=Path(intent.backing_file),
                quota_bytes=intent.block_device_capacity_bytes,
            )
            return stored
        loop_device = self._find_existing_loop(Path(intent.backing_file))
        if loop_device is None:
            completed = self._run_pinned(
                self._deployment.losetup,
                ("--find", "--show", "--nooverlap", intent.backing_file),
            )
            if completed.stderr:
                raise OCIOutputQuotaError("losetup returned unexpected stderr")
            try:
                loop_device = completed.stdout.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise OCIOutputQuotaError("losetup response is not ASCII") from exc
        self._verify_loop_association(
            loop_device=loop_device,
            backing_file=Path(intent.backing_file),
            quota_bytes=intent.block_device_capacity_bytes,
        )
        record = _QuotaLoopAttachment(
            deployment_sha256=self._deployment.deployment_sha256,
            intent_record_sha256=intent.intent_record_sha256,
            loop_device=loop_device,
            backing_file_identity_sha256=backing_identity,
            attached_at=datetime.now(timezone.utc),
        )
        published = self._publish_root_model(path, record)
        assert isinstance(published, _QuotaLoopAttachment)
        if published.model_dump(mode="python", exclude={"attached_at"}) != record.model_dump(
            mode="python", exclude={"attached_at"}
        ):
            raise OCIOutputQuotaError("published loop attachment changed stable generation scope")
        return published

    def _ensure_formatted(
        self,
        intent: _QuotaProvisioningIntent,
        *,
        attachment: _QuotaLoopAttachment,
        generation_root: Path,
    ) -> _QuotaFilesystemFormatted:
        path = generation_root / "filesystem-formatted.json"
        recovered_formatted = self._recover_root_model(path, _QuotaFilesystemFormatted)
        if recovered_formatted is not None:
            stored = recovered_formatted
            assert isinstance(stored, _QuotaFilesystemFormatted)
            if (
                stored.attachment_record_sha256 != attachment.attachment_record_sha256
                or stored.filesystem_uuid != intent.filesystem_uuid
            ):
                raise OCIOutputQuotaError("formatted filesystem changed during recovery")
            return stored
        completed = self._run_pinned(
            self._deployment.mkfs,
            (
                "-F",
                "-U",
                intent.filesystem_uuid,
                "-m",
                "0",
                "-E",
                "lazy_itable_init=0,lazy_journal_init=0",
                attachment.loop_device,
            ),
        )
        if completed.stdout and len(completed.stdout) > _MAX_CONTROL_BYTES:
            raise OCIOutputQuotaError("mkfs response exceeded its bound")
        record = _QuotaFilesystemFormatted(
            deployment_sha256=self._deployment.deployment_sha256,
            attachment_record_sha256=attachment.attachment_record_sha256,
            filesystem_uuid=intent.filesystem_uuid,
            filesystem_uuid_sha256=hashlib.sha256(
                intent.filesystem_uuid.encode("ascii")
            ).hexdigest(),
            formatted_at=datetime.now(timezone.utc),
        )
        published = self._publish_root_model(path, record)
        assert isinstance(published, _QuotaFilesystemFormatted)
        if published.model_dump(mode="python", exclude={"formatted_at"}) != record.model_dump(
            mode="python", exclude={"formatted_at"}
        ):
            raise OCIOutputQuotaError("published filesystem record changed stable generation scope")
        return published

    def _ensure_mounted(
        self,
        intent: _QuotaProvisioningIntent,
        *,
        attachment: _QuotaLoopAttachment,
        formatted: _QuotaFilesystemFormatted,
        output_descriptor: int,
    ) -> OutputQuotaProvisioningReceipt:
        output_root = Path(intent.output_root)
        mount = self._find_mount(output_root)
        if mount is None:
            self._run_pinned(
                self._deployment.mount,
                (
                    "-t",
                    self._deployment.filesystem_type,
                    "-o",
                    "noatime,nodev,noexec,nosuid,rw",
                    attachment.loop_device,
                    f"/proc/self/fd/{output_descriptor}",
                ),
                extra_pass_fds=(output_descriptor,),
            )
            _durable_publish_checkpoint("quota-mount-command-returned", output_root)
            mount = self._find_mount(output_root)
        if mount is None or mount["source"] != attachment.loop_device:
            raise OCIOutputQuotaError("quota filesystem mount is absent or changed")
        self._validate_service_mount(mount)
        lost_found = output_root / "lost+found"
        if lost_found.exists():
            metadata = lost_found.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or any(lost_found.iterdir())
            ):
                raise OCIOutputQuotaError("fresh loop filesystem contains unexpected data")
            lost_found.rmdir()
        os.chown(
            output_root,
            self._deployment.allowed_client_uid,
            self._deployment.allowed_client_gid,
            follow_symlinks=False,
        )
        os.chmod(output_root, 0o700, follow_symlinks=False)
        self._fsync_directory(output_root)
        metadata = output_root.lstat()
        return OutputQuotaProvisioningReceipt(
            node_manifest_sha256=intent.node_manifest_sha256,
            node_id=intent.node_id,
            boot_id=intent.boot_id,
            execution_id=intent.execution_id,
            infrastructure_attempt_id=intent.infrastructure_attempt_id,
            intent_sha256=intent.intent_sha256,
            output_root=intent.output_root,
            output_quota_bytes=intent.output_quota_bytes,
            output_root_device=metadata.st_dev,
            output_root_inode=metadata.st_ino,
            output_root_owner_uid=metadata.st_uid,
            output_root_owner_gid=metadata.st_gid,
            mount_id=int(mount["mount_id"]),
            mount_parent_id=int(mount["mount_parent_id"]),
            block_device_major=int(mount["major"]),
            block_device_minor=int(mount["minor"]),
            block_device_capacity_bytes=intent.block_device_capacity_bytes,
            filesystem_type=self._deployment.filesystem_type,
            filesystem_uuid_sha256=formatted.filesystem_uuid_sha256,
            mount_options=tuple(sorted(mount["mount_options"])),
            backing_file_identity_sha256=attachment.backing_file_identity_sha256,
            provisioner_policy_sha256=self._deployment.provisioner_policy_sha256,
            provisioner_principal_id=self._deployment.provisioner_principal_id,
            provisioned_at=intent.created_at,
        )

    def _verify_live_receipt(self, receipt: OutputQuotaProvisioningReceipt) -> None:
        metadata = Path(receipt.output_root).lstat()
        mount = self._find_mount(Path(receipt.output_root))
        if mount is not None:
            self._validate_service_mount(mount)
        if (
            mount is None
            or receipt.output_root_device != metadata.st_dev
            or receipt.output_root_inode != metadata.st_ino
            or receipt.output_root_owner_uid != metadata.st_uid
            or receipt.output_root_owner_gid != metadata.st_gid
            or receipt.output_root_mode != stat.S_IMODE(metadata.st_mode)
            or receipt.mount_id != mount["mount_id"]
            or receipt.mount_parent_id != mount["mount_parent_id"]
            or receipt.block_device_major != mount["major"]
            or receipt.block_device_minor != mount["minor"]
            or receipt.filesystem_type != mount["fstype"]
            or receipt.mount_options != tuple(sorted(mount["mount_options"]))
        ):
            raise OCIOutputQuotaError("live provisioned quota mount differs from receipt")
        self._verify_loop_association(
            loop_device=str(mount["source"]),
            backing_file=Path(self._deployment.backing_root)
            / f"{_attempt_workspace_key(receipt.infrastructure_attempt_id)}.img",
            quota_bytes=receipt.block_device_capacity_bytes,
        )
        if (
            _loop_backing_identity(
                Path(self._deployment.backing_root)
                / f"{_attempt_workspace_key(receipt.infrastructure_attempt_id)}.img",
                expected_bytes=receipt.block_device_capacity_bytes,
                expected_owner_uid=self._trusted_state_owner_uid(),
                expected_owner_gid=os.getegid(),
            )
            != receipt.backing_file_identity_sha256
        ):
            raise OCIOutputQuotaError("live quota backing identity differs from receipt")
        filesystem_uuid_sha256 = LoopbackOutputQuotaController._filesystem_uuid_sha256(
            source=str(mount["source"]),
            major=int(mount["major"]),
            minor=int(mount["minor"]),
        )
        filesystem = os.statvfs(receipt.output_root)
        if (
            filesystem_uuid_sha256 != receipt.filesystem_uuid_sha256
            or filesystem.f_frsize <= 0
            or filesystem.f_blocks * filesystem.f_frsize > receipt.block_device_capacity_bytes
        ):
            raise OCIOutputQuotaError("live quota filesystem differs from durable ceiling")

    def _validate_service_mount(self, mount: Mapping[str, object]) -> None:
        options = mount.get("mount_options")
        super_options = mount.get("super_options")
        if (
            mount.get("fstype") != self._deployment.filesystem_type
            or re.fullmatch(r"/dev/loop[0-9]+", str(mount.get("source"))) is None
            or not isinstance(options, frozenset)
            or not {"rw", "nosuid", "nodev", "noexec", "noatime"}.issubset(options)
            or not isinstance(super_options, frozenset)
            or "rw" not in super_options
        ):
            raise OCIOutputQuotaError("quota mount flags or filesystem differ from deployment")

    def _find_existing_loop(self, backing_file: Path) -> str | None:
        matches: list[str] = []
        for loop_dir in sorted(Path("/sys/block").glob("loop[0-9]*")):
            try:
                encoded = (loop_dir / "loop" / "backing_file").read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            candidate = Path("/") / LoopbackOutputQuotaController._unescape_mountinfo(
                encoded
            ).lstrip("/")
            try:
                if candidate.resolve(strict=True) == backing_file.resolve(strict=True):
                    matches.append(f"/dev/{loop_dir.name}")
            except OSError as exc:
                raise OCIOutputQuotaError("loop association backing path is unsafe") from exc
        if len(matches) > 1:
            raise OCIOutputQuotaError("backing file has multiple loop associations")
        return matches[0] if matches else None

    @staticmethod
    def _verify_loop_association(*, loop_device: str, backing_file: Path, quota_bytes: int) -> None:
        if re.fullmatch(r"/dev/loop[0-9]+", loop_device) is None:
            raise OCIOutputQuotaError("loop association returned a non-loop device")
        name = Path(loop_device).name
        try:
            device = Path(loop_device).lstat()
            encoded = (
                Path(f"/sys/block/{name}/loop/backing_file").read_text(encoding="utf-8").strip()
            )
            sectors = int(Path(f"/sys/block/{name}/size").read_text(encoding="ascii").strip())
            autoclear = (
                Path(f"/sys/block/{name}/loop/autoclear").read_text(encoding="ascii").strip()
            )
            observed_backing = (
                Path("/") / LoopbackOutputQuotaController._unescape_mountinfo(encoded).lstrip("/")
            ).resolve(strict=True)
        except (OSError, UnicodeError, ValueError) as exc:
            raise OCIOutputQuotaError("loop association cannot be independently verified") from exc
        if (
            not stat.S_ISBLK(device.st_mode)
            or sectors * _SECTOR_BYTES != quota_bytes
            or autoclear != "0"
            or observed_backing != backing_file.resolve(strict=True)
        ):
            raise OCIOutputQuotaError("loop association differs from exact backing/quota")

    @staticmethod
    def _find_mount(output_root: Path) -> dict[str, object] | None:
        try:
            lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise OCIOutputQuotaError("mountinfo is unavailable to quota service") from exc
        matches = [
            parsed
            for line in lines
            if (parsed := LoopbackOutputQuotaController._parse_mountinfo(line))["mountpoint"]
            == str(output_root)
        ]
        if len(matches) > 1:
            raise OCIOutputQuotaError("output root has multiple mountinfo identities")
        return matches[0] if matches else None

    def _run_pinned(
        self,
        pin: PinnedRootExecutable,
        arguments: tuple[str, ...],
        *,
        extra_pass_fds: tuple[int, ...] = (),
    ) -> subprocess.CompletedProcess[bytes]:
        path = Path(pin.path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIOutputQuotaError("pinned privileged executable is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if (
                metadata.st_dev != pin.device
                or metadata.st_ino != pin.inode
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != pin.mode
                or digest.hexdigest() != pin.sha256
                or host_parent_chain_sha256(path) != pin.parent_chain_sha256
            ):
                raise OCIOutputQuotaError("privileged executable differs from deployment pin")
            completed = subprocess.run(
                (pin.path, *arguments),
                executable=f"/proc/self/fd/{descriptor}",
                pass_fds=(descriptor, *extra_pass_fds),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={},
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise OCIOutputQuotaError("pinned privileged command failed") from exc
        finally:
            os.close(descriptor)
        if (
            completed.returncode != 0
            or len(completed.stdout) > _MAX_CONTROL_BYTES
            or len(completed.stderr) > _MAX_CONTROL_BYTES
        ):
            raise OCIOutputQuotaError("pinned privileged command failed closed")
        return completed

    def _require_root_systemd_service(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
            raise OCIOutputQuotaError("quota provisioning service must run as root on Linux")
        try:
            pid_one = Path("/proc/1/comm").read_text(encoding="ascii").strip()
            cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii")
            status = Path("/proc/self/status").read_text(encoding="ascii")
            module = Path(__file__).resolve(strict=True)
            module_metadata = module.lstat()
            module_payload = module.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise OCIOutputQuotaError("quota service deployment identity is unavailable") from exc
        if (
            pid_one != "systemd"
            or re.fullmatch(r"[0-9a-f]{32}", os.environ.get("INVOCATION_ID", "")) is None
            or not _in_exact_systemd_unit(cgroup, self._deployment.systemd_unit_name)
            or re.search(r"^Uid:\s+0\s+0\s+0\s+0$", status, re.MULTILINE) is None
            or re.search(r"^Gid:\s+0\s+0\s+0\s+0$", status, re.MULTILINE) is None
            or module_metadata.st_uid != 0
            or module_metadata.st_gid != 0
            or module_metadata.st_dev != self._deployment.service_module_device
            or module_metadata.st_ino != self._deployment.service_module_inode
            or stat.S_IMODE(module_metadata.st_mode) != self._deployment.service_module_mode
            or hashlib.sha256(module_payload).hexdigest() != self._deployment.service_module_sha256
            or host_parent_chain_sha256(module)
            != self._deployment.service_module_parent_chain_sha256
        ):
            raise OCIOutputQuotaError("quota service is not its root-owned systemd deployment")
        _verify_root_file_pin(
            self._deployment.systemd_unit,
            label="quota systemd unit file",
            error_type=OCIOutputQuotaError,
        )
        _verify_root_process_executable(
            self._deployment.service_executable,
            error_type=OCIOutputQuotaError,
        )
        _verify_pinned_directory(
            Path(self._deployment.workspace_root),
            owner_uid=0,
            owner_gid=self._deployment.allowed_client_gid,
            mode=self._deployment.workspace_root_pin.mode,
            device=self._deployment.workspace_root_pin.device,
            inode=self._deployment.workspace_root_pin.inode,
            parent_chain_sha256=self._deployment.workspace_root_pin.parent_chain_sha256,
            label="quota workspace root",
            error_type=OCIOutputQuotaError,
        )
        for path, device, inode, mode, parent_chain, label in (
            (
                Path(self._deployment.backing_root),
                self._deployment.backing_root_device,
                self._deployment.backing_root_inode,
                self._deployment.backing_root_mode,
                self._deployment.backing_root_parent_chain_sha256,
                "quota backing root",
            ),
            (
                Path(self._deployment.state_root),
                self._deployment.state_root_device,
                self._deployment.state_root_inode,
                self._deployment.state_root_mode,
                self._deployment.state_root_parent_chain_sha256,
                "quota state root",
            ),
            (
                Path(self._deployment.socket_path).parent,
                self._deployment.socket_parent_device,
                self._deployment.socket_parent_inode,
                self._deployment.socket_parent_mode,
                self._deployment.socket_parent_parent_chain_sha256,
                "quota socket parent",
            ),
        ):
            _verify_pinned_directory(
                path,
                owner_uid=0,
                owner_gid=0,
                mode=mode,
                device=device,
                inode=inode,
                parent_chain_sha256=parent_chain,
                label=label,
                error_type=OCIOutputQuotaError,
            )

    def _create_server_socket(self) -> socket.socket:
        path = Path(self._deployment.socket_path)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != 0:
                raise OCIOutputQuotaError("quota service socket path contains an unsafe object")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chown(path, 0, self._deployment.allowed_client_gid)
        os.chmod(path, 0o660)
        server.listen(16)
        return server

    def _serve_connection(self, connection: socket.socket) -> None:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        peer_uid = int.from_bytes(credentials[4:8], sys.byteorder, signed=True)
        peer_gid = int.from_bytes(credentials[8:12], sys.byteorder, signed=True)
        if (
            peer_uid != self._deployment.allowed_client_uid
            or peer_gid != self._deployment.allowed_client_gid
        ):
            raise OCIOutputQuotaError("quota service client credentials differ from deployment")
        raw = self._receive_line(connection)
        request = json.loads(raw)
        if not isinstance(request, dict) or request.pop("operation", None) != "ensure":
            raise OCIOutputQuotaError("quota service request operation is not allowed")
        receipt = self.ensure(request)
        response = {
            "schema": "aletheia.loopback_output_quota_response.v1",
            "deployment_sha256": self._deployment.deployment_sha256,
            "service_pid": os.getpid(),
            "receipt": receipt.model_dump(mode="json"),
        }
        connection.sendall(canonical_json_bytes(response) + b"\n")

    @staticmethod
    def _receive_line(connection: socket.socket) -> bytes:
        payload = bytearray()
        while b"\n" not in payload:
            chunk = connection.recv(64 * 1024)
            if not chunk:
                raise OCIOutputQuotaError("quota service received an incomplete request")
            payload.extend(chunk)
            if len(payload) > _MAX_CONTROL_BYTES:
                raise OCIOutputQuotaError("quota service request exceeded its byte bound")
        raw, newline, residue = bytes(payload).partition(b"\n")
        if newline != b"\n" or residue:
            raise OCIOutputQuotaError("quota service request framing is not exact")
        return raw

    def _generation_root(self, attempt_id: str) -> Path:
        return Path(self._deployment.state_root) / _attempt_workspace_key(attempt_id)

    def _prepare_root(self, path: Path, *, mode: int) -> None:
        # These roots are deployment inputs pinned (including inode and parent chain) by
        # ``_require_root_systemd_service`` before this method is reachable.  Runtime creation
        # would both contradict that pin and introduce an unjournaled mkdir durability phase.
        self._require_root_directory(path, mode=mode)

    def _require_root_directory(self, path: Path, *, mode: int) -> None:
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != self._trusted_state_owner_uid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise OCIOutputQuotaError("quota service root directory custody is unsafe")

    def _publish_root_model(self, path: Path, value: ExecutionModel) -> ExecutionModel:
        payload = _publish_root_record_once(
            path,
            value,
            owner_uid=self._trusted_state_owner_uid(),
            error_type=OCIOutputQuotaError,
        )
        try:
            published = type(value).model_validate_json(payload)
        except ValidationError as exc:  # pragma: no cover - helper already validates
            raise OCIOutputQuotaError("published quota record failed typed reload") from exc
        if canonical_json_bytes(published) != payload:
            raise OCIOutputQuotaError("published quota record failed canonical reload")
        return published

    def _recover_root_model(
        self,
        path: Path,
        model: type[ExecutionModel],
    ) -> ExecutionModel | None:
        return _recover_root_record_once(
            path,
            model,
            owner_uid=self._trusted_state_owner_uid(),
            error_type=OCIOutputQuotaError,
        )

    @staticmethod
    def _trusted_state_owner_uid() -> int:
        """Return the production quota-journal owner (patchable in unit tests)."""

        return 0

    def _load_root_model(self, path: Path, model: type[ExecutionModel]) -> ExecutionModel:
        payload = _read_bounded_file(
            path,
            label="quota service durable record",
            owner_uid=self._trusted_state_owner_uid(),
            maximum_bytes=_MAX_CONTROL_BYTES,
            allowed_modes=frozenset({0o400}),
        )
        try:
            value = model.model_validate_json(payload)
        except ValidationError as exc:
            raise OCIOutputQuotaError("quota service durable record failed validation") from exc
        if canonical_json_bytes(value) != payload:
            raise OCIOutputQuotaError("quota service durable record is not canonical")
        return value

    @staticmethod
    def _required_text(request: Mapping[str, object], key: str) -> str:
        value = request.get(key)
        if not isinstance(value, str) or not value:
            raise OCIOutputQuotaError(f"quota service request {key} is not text")
        return value

    @staticmethod
    def _required_int(request: Mapping[str, object], key: str) -> int:
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OCIOutputQuotaError(f"quota service request {key} is not an integer")
        return value

    @staticmethod
    def _current_boot_id() -> str:
        return DurableDeadlineWatchdogService._current_boot_id()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class LoopbackOutputQuotaProvisionerClient:
    """Unprivileged NodeAgent port backed only by the pinned root service socket."""

    def __init__(self, deployment: LoopbackQuotaProvisionerDeploymentPin) -> None:
        self._deployment = LoopbackQuotaProvisionerDeploymentPin.model_validate(
            deployment.model_dump(mode="python")
        )

    @property
    def output_workspace_root_pin(self) -> PinnedOutputWorkspaceRoot:
        return self._deployment.workspace_root_pin

    @property
    def minimum_output_quota_bytes(self) -> int:
        return self._deployment.minimum_loop_filesystem_bytes

    def ensure_output_quota(
        self,
        *,
        node_manifest_sha256: str,
        node_id: str,
        boot_id: str,
        execution_id: str,
        attempt_id: str,
        intent_sha256: str,
        output_root: Path,
        output_quota_bytes: int,
        expected_receipt: OutputQuotaProvisioningReceipt | None,
    ) -> OutputQuotaProvisioningReceipt:
        request = {
            "operation": "ensure",
            "node_manifest_sha256": node_manifest_sha256,
            "node_id": node_id,
            "boot_id": boot_id,
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "intent_sha256": intent_sha256,
            "output_root": str(output_root),
            "output_quota_bytes": output_quota_bytes,
            "expected_receipt": (
                expected_receipt.model_dump(mode="json") if expected_receipt is not None else "none"
            ),
        }
        response, peer_pid = self._request(request)
        if (
            response.get("schema") != "aletheia.loopback_output_quota_response.v1"
            or response.get("deployment_sha256") != self._deployment.deployment_sha256
            or response.get("service_pid") != peer_pid
        ):
            raise OCIOutputQuotaError("quota service response differs from deployment peer")
        try:
            receipt = OutputQuotaProvisioningReceipt.model_validate(response["receipt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OCIOutputQuotaError("quota service response omitted typed receipt") from exc
        if expected_receipt is not None and receipt != expected_receipt:
            raise OCIOutputQuotaError("quota service rebound the expected durable receipt")
        if (
            receipt.node_manifest_sha256 != node_manifest_sha256
            or receipt.node_id != node_id
            or receipt.boot_id != boot_id
            or receipt.execution_id != execution_id
            or receipt.infrastructure_attempt_id != attempt_id
            or receipt.intent_sha256 != intent_sha256
            or receipt.output_root != str(output_root)
            or receipt.output_quota_bytes != output_quota_bytes
            or receipt.provisioner_policy_sha256 != self._deployment.provisioner_policy_sha256
            or receipt.provisioner_principal_id != self._deployment.provisioner_principal_id
        ):
            raise OCIOutputQuotaError("quota service receipt differs from exact node request")
        return receipt

    def _request(self, request: Mapping[str, object]) -> tuple[dict[str, object], int]:
        payload = canonical_json_bytes(dict(request)) + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(self._deployment.socket_path)
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid = int.from_bytes(credentials[0:4], sys.byteorder, signed=True)
            peer_uid = int.from_bytes(credentials[4:8], sys.byteorder, signed=True)
            peer_gid = int.from_bytes(credentials[8:12], sys.byteorder, signed=True)
            if peer_pid <= 0 or peer_uid != 0 or peer_gid != 0:
                raise OCIOutputQuotaError("quota service socket peer is not root")
            connection.sendall(payload)
            raw = LoopbackOutputQuotaProvisioningService._receive_line(connection)
        except (OSError, TimeoutError) as exc:
            raise OCIOutputQuotaError(
                "independent quota provisioning service is unavailable"
            ) from exc
        finally:
            connection.close()
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCIOutputQuotaError("quota service response is not JSON") from exc
        if not isinstance(response, dict):
            raise OCIOutputQuotaError("quota service response is not one object")
        return response, peer_pid


class SystemdWatchdogDeploymentPin(ExecutionModel):
    """Exact independently supervised watchdog service deployment."""

    schema_name: Literal["aletheia.systemd_oci_watchdog_deployment"] = (
        "aletheia.systemd_oci_watchdog_deployment"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    systemd_unit_name: str
    systemd_unit: PinnedRootFile
    service_executable: PinnedRootExecutable
    service_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_module_device: int = Field(ge=0)
    service_module_inode: int = Field(ge=1)
    service_module_mode: Literal[0o400, 0o440, 0o444]
    service_module_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_root: str = Field(min_length=2, max_length=4096)
    journal_root_device: int = Field(ge=0)
    journal_root_inode: int = Field(ge=1)
    journal_root_mode: Literal[0o700] = 0o700
    journal_root_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_root: str = Field(min_length=2, max_length=4096)
    state_root_device: int = Field(ge=0)
    state_root_inode: int = Field(ge=1)
    state_root_mode: Literal[0o700] = 0o700
    state_root_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    socket_path: str = Field(min_length=2, max_length=4096)
    socket_parent_device: int = Field(ge=0)
    socket_parent_inode: int = Field(ge=1)
    socket_parent_mode: Literal[0o755] = 0o755
    socket_parent_parent_chain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_client_uid: int = Field(ge=1, le=2**31 - 1)
    allowed_client_gid: int = Field(ge=1, le=2**31 - 1)
    maximum_active_jobs: int = Field(default=4096, ge=1, le=1_000_000)
    qualification_only: Literal[True] = True

    @model_validator(mode="after")
    def _paths_and_unit_are_closed(self) -> "SystemdWatchdogDeploymentPin":
        if _SYSTEMD_UNIT.fullmatch(self.systemd_unit_name) is None:
            raise ValueError("watchdog systemd unit name is not deployment-scoped")
        if Path(self.systemd_unit.path).name != self.systemd_unit_name:
            raise ValueError("watchdog unit file does not match its unit name")
        journal = _canonical_absolute_path(self.journal_root, label="watchdog journal root")
        state = _canonical_absolute_path(self.state_root, label="watchdog state root")
        socket_path = _canonical_absolute_path(self.socket_path, label="watchdog socket")
        paths = (journal, state, socket_path.parent)
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(paths)
            for right in paths[index + 1 :]
        ):
            raise ValueError("watchdog journal, state, and socket custody roots overlap")
        return self

    @property
    def deployment_sha256(self) -> str:
        return canonical_sha256(self)


class _WatchdogArmedRecord(ExecutionModel):
    schema_name: Literal["aletheia.systemd_oci_watchdog_armed"] = (
        "aletheia.systemd_oci_watchdog_armed"
    )
    schema_version: Literal[1] = 1
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preparation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boot_id: str
    runtime_id: str
    container_name: str
    engine_endpoint: Literal["unix:///var/run/docker.sock"]
    authorization_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_launch_authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pre_runtime_absence_epoch: int = Field(ge=0)
    hard_deadline: AwareDatetime
    hard_deadline_boottime_ns: int = Field(ge=0)
    expected_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_labels: tuple[tuple[str, str], ...]
    armed_at: AwareDatetime
    service_boot_id: str

    @property
    def job_sha256(self) -> str:
        # The daemon observation time is durable audit metadata, not job identity.  Excluding the
        # two service-local volatile fields lets the node independently reconstruct and validate
        # the exact response job hash from its immutable runtime journals.
        return canonical_sha256(
            self.model_dump(mode="json", exclude={"armed_at", "service_boot_id"})
        )


class _WatchdogTerminalRecord(ExecutionModel):
    schema_name: Literal["aletheia.systemd_oci_watchdog_terminal"] = (
        "aletheia.systemd_oci_watchdog_terminal"
    )
    schema_version: Literal[1] = 1
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    armed_job_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["retired", "fired"]
    retirement_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    container_was_running: bool | None = None
    cgroup_path: str | None = None
    cgroup_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cgroup_empty: bool | None = None
    completed_at: AwareDatetime
    service_boot_id: str

    @model_validator(mode="after")
    def _termination_proof_is_complete(self) -> "_WatchdogTerminalRecord":
        cgroup_fields = (self.cgroup_path, self.cgroup_identity_sha256, self.cgroup_empty)
        if self.status == "retired":
            if (
                self.retirement_evidence_sha256 is None
                or self.container_id is not None
                or self.container_was_running is not None
                or any(value is not None for value in cgroup_fields)
            ):
                raise ValueError("retired watchdog record is not retirement-only")
            return self
        if self.retirement_evidence_sha256 is not None:
            raise ValueError("fired watchdog cannot carry retirement evidence")
        if self.container_id is None:
            if self.container_was_running is not None or any(
                value is not None for value in cgroup_fields
            ):
                raise ValueError("absent-container watchdog proof is not exact")
            return self
        if self.container_was_running is False:
            if any(value is not None for value in cgroup_fields):
                raise ValueError("stopped-container watchdog proof carries cgroup evidence")
            return self
        if self.container_was_running is not True or (
            self.cgroup_path is None
            or self.cgroup_identity_sha256 is None
            or self.cgroup_empty is not True
        ):
            raise ValueError("fired watchdog lacks exact empty-cgroup proof")
        return self


class _WatchdogFiringIntent(ExecutionModel):
    """Durable pre-kill identity used to resume one exact overdue decision."""

    schema_name: Literal["aletheia.systemd_oci_watchdog_firing_intent"] = (
        "aletheia.systemd_oci_watchdog_firing_intent"
    )
    schema_version: Literal[1] = 1
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    armed_job_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    container_was_running: bool | None = None
    init_pid: int | None = Field(default=None, ge=2)
    cgroup_path: str | None = None
    cgroup_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    inspected_at: AwareDatetime
    service_boot_id: str

    @model_validator(mode="after")
    def _running_scope_is_complete(self) -> "_WatchdogFiringIntent":
        cgroup_fields = (self.init_pid, self.cgroup_path, self.cgroup_identity_sha256)
        if self.container_id is None:
            if self.container_was_running is not None or any(
                value is not None for value in cgroup_fields
            ):
                raise ValueError("absent-container firing intent is not exact")
            return self
        if self.container_was_running is False:
            if any(value is not None for value in cgroup_fields):
                raise ValueError("stopped-container firing intent carries cgroup identity")
            return self
        if self.container_was_running is not True or any(value is None for value in cgroup_fields):
            raise ValueError("running watchdog firing intent lacks exact cgroup identity")
        return self

    @property
    def firing_intent_sha256(self) -> str:
        return canonical_sha256(self)


class _WatchdogKillCompleted(ExecutionModel):
    schema_name: Literal["aletheia.systemd_oci_watchdog_kill_completed"] = (
        "aletheia.systemd_oci_watchdog_kill_completed"
    )
    schema_version: Literal[1] = 1
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    armed_job_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    firing_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    cgroup_path: str
    cgroup_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cgroup_empty: Literal[True] = True
    completed_at: AwareDatetime
    service_boot_id: str


class _WatchdogCleanupQuiescenceRecord(ExecutionModel):
    """Durably bind a fired pre-launch terminal to exact local cleanup authority.

    The terminal remains ``fired``.  This acknowledgement only proves that its absent or stopped
    decision is quiescent, so the node may complete exact absence or remove the same CREATED/PID0
    container without racing any later action by the watchdog.
    """

    schema_name: Literal["aletheia.systemd_oci_watchdog_cleanup_quiescence"] = (
        "aletheia.systemd_oci_watchdog_cleanup_quiescence"
    )
    schema_version: Literal[1] = 1
    deployment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    armed_job_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_pending_journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fired_terminal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["fired_absent", "fired_stopped"]
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acknowledged_at: AwareDatetime
    service_boot_id: str

    @model_validator(mode="after")
    def _decision_shape_is_exact(self) -> "_WatchdogCleanupQuiescenceRecord":
        if (self.decision == "fired_absent") != (self.container_id is None):
            raise ValueError("watchdog cleanup quiescence has an impossible container shape")
        return self

    @property
    def quiescence_record_sha256(self) -> str:
        return canonical_sha256(self)


class _WatchdogJournalScope:
    """Reconstruct watchdog challenges from exact runtime-owned journal bytes."""

    def __init__(
        self, *, policy: DeploymentPinnedOCIPolicy, deployment: SystemdWatchdogDeploymentPin
    ):
        self.policy = policy
        self.deployment = deployment

    def runtime_root(self, runtime_id: str) -> Path:
        key = hashlib.sha256(
            b"ALETHEIA_QUALIFICATION_OCI_RUNTIME_V2\x00" + runtime_id.encode("utf-8")
        ).hexdigest()
        return Path(self.deployment.journal_root) / key

    def arm_record(
        self,
        *,
        preparation_sha256: str,
        boot_id: str,
        runtime_id: str,
        container_name: str,
        engine_endpoint: str,
        authorization_request_sha256: str,
        runtime_launch_authorization_sha256: str,
        pre_runtime_absence_epoch: int,
        hard_deadline: datetime,
        hard_deadline_boottime_ns: int,
        expected_evidence_sha256: str,
        armed_at: datetime,
        service_boot_id: str,
    ) -> _WatchdogArmedRecord:
        root = self.runtime_root(runtime_id)
        plan = _load_exact_model(
            root / "plan.json", OCIExecutionPlan, owner_uid=self.deployment.allowed_client_uid
        )
        preparation = _load_exact_model(
            root / "preparation.json",
            RuntimePreparation,
            owner_uid=self.deployment.allowed_client_uid,
        )
        config = _load_exact_model(
            root / "oci-config.json",
            OCIConfiguration,
            owner_uid=self.deployment.allowed_client_uid,
        )
        gate = _load_exact_model(
            root / "control" / "launch-authorization.json",
            _LaunchGateAuthorizationJournal,
            owner_uid=self.deployment.allowed_client_uid,
        )
        assert isinstance(plan, OCIExecutionPlan)
        assert isinstance(preparation, RuntimePreparation)
        assert isinstance(config, OCIConfiguration)
        assert isinstance(gate, _LaunchGateAuthorizationJournal)
        derived_evidence = canonical_sha256(
            {
                "schema": "aletheia.crash_durable_oci_deadline_watchdog.v2",
                "preparation_sha256": preparation.preparation_sha256,
                "boot_id": plan.boot_id,
                "runtime_id": plan.runtime_id,
                "container_name": config.container_name,
                "engine_endpoint": self.policy.engine_endpoint,
                "authorization_request_sha256": gate.authorization_request_sha256,
                "runtime_launch_authorization_sha256": (gate.runtime_launch_authorization_sha256),
                "pre_runtime_absence_epoch": (gate.authorization_request.pre_runtime_absence_epoch),
                "hard_deadline": plan.deadline.isoformat(),
                "hard_deadline_boottime_ns": hard_deadline_boottime_ns,
                "enforced_placement_sha256": plan.enforced_placement_sha256,
                "fencing_epoch": preparation.fencing_epoch,
                "lease_token_sha256": preparation.lease_token_sha256,
                "required_action": "kill-cgroup-and-container-no-later-than-either-deadline",
                "survives_node_agent_process_crash": True,
            }
        )
        supplied = (
            preparation_sha256,
            boot_id,
            runtime_id,
            container_name,
            engine_endpoint,
            authorization_request_sha256,
            runtime_launch_authorization_sha256,
            pre_runtime_absence_epoch,
            hard_deadline,
            expected_evidence_sha256,
        )
        derived = (
            preparation.preparation_sha256,
            plan.boot_id,
            plan.runtime_id,
            config.container_name,
            self.policy.engine_endpoint,
            gate.authorization_request_sha256,
            gate.runtime_launch_authorization_sha256,
            gate.authorization_request.pre_runtime_absence_epoch,
            plan.deadline,
            derived_evidence,
        )
        if supplied != derived or gate.preparation_sha256 != preparation.preparation_sha256:
            raise OCIWatchdogError("watchdog arm differs from durable runtime scope")
        remaining = plan.deadline - preparation.prepared_at
        remaining_ns = (
            remaining.days * 86_400 * 1_000_000_000
            + remaining.seconds * 1_000_000_000
            + remaining.microseconds * 1_000
        )
        if (
            remaining_ns <= 0
            or preparation.prepared_monotonic_ns + remaining_ns != hard_deadline_boottime_ns
        ):
            raise OCIWatchdogError("watchdog boottime deadline differs from preparation")
        return _WatchdogArmedRecord(
            deployment_sha256=self.deployment.deployment_sha256,
            preparation_sha256=preparation_sha256,
            boot_id=boot_id,
            runtime_id=runtime_id,
            container_name=container_name,
            engine_endpoint="unix:///var/run/docker.sock",
            authorization_request_sha256=authorization_request_sha256,
            runtime_launch_authorization_sha256=runtime_launch_authorization_sha256,
            pre_runtime_absence_epoch=pre_runtime_absence_epoch,
            hard_deadline=hard_deadline,
            hard_deadline_boottime_ns=hard_deadline_boottime_ns,
            expected_evidence_sha256=expected_evidence_sha256,
            container_labels=tuple(config.labels),
            armed_at=armed_at,
            service_boot_id=service_boot_id,
        )

    def retirement_evidence(
        self,
        *,
        armed: _WatchdogArmedRecord,
        watchdog_journal_sha256: str,
        expected_evidence_sha256: str,
    ) -> str:
        root = self.runtime_root(armed.runtime_id)
        cleanup_epoch = armed.pre_runtime_absence_epoch + 1
        pending = _load_exact_model(
            root / "cleanup" / f"absence-{cleanup_epoch}-pending.json",
            _NeverStartedCleanupPending,
            owner_uid=self.deployment.allowed_client_uid,
        )
        watchdog = _load_exact_model(
            root / "deadline-watchdog.json",
            _DeadlineWatchdogJournal,
            owner_uid=self.deployment.allowed_client_uid,
        )
        preparation = _load_exact_model(
            root / "preparation.json",
            RuntimePreparation,
            owner_uid=self.deployment.allowed_client_uid,
        )
        assert isinstance(pending, _NeverStartedCleanupPending)
        assert isinstance(watchdog, _DeadlineWatchdogJournal)
        assert isinstance(preparation, RuntimePreparation)
        if (
            pending.deadline_watchdog != watchdog
            or watchdog.journal_sha256 != watchdog_journal_sha256
            or pending.watchdog_retirement_evidence_sha256 != expected_evidence_sha256
        ):
            raise OCIWatchdogError("watchdog retirement differs from cleanup pending scope")
        derived = canonical_sha256(
            {
                "schema": "aletheia.crash_durable_oci_watchdog_retirement.v2",
                "preparation_sha256": preparation.preparation_sha256,
                "runtime_id": preparation.runtime_id,
                "authorization_request_sha256": armed.authorization_request_sha256,
                "runtime_launch_authorization_sha256": (armed.runtime_launch_authorization_sha256),
                "pre_runtime_absence_epoch": armed.pre_runtime_absence_epoch,
                "cleanup_absence_epoch": cleanup_epoch,
                "watchdog_journal_sha256": watchdog_journal_sha256,
                "required_action": "retire-old-generation-before-replacement",
            }
        )
        if derived != expected_evidence_sha256:
            raise OCIWatchdogError("watchdog retirement challenge is not independently derived")
        return derived

    def fired_cleanup_quiescence(
        self,
        *,
        armed: _WatchdogArmedRecord,
        terminal: _WatchdogTerminalRecord,
    ) -> tuple[_NeverStartedCleanupPending, Literal["fired_absent", "fired_stopped"]]:
        """Bind one fired non-running terminal to the exact cleanup-pending generation."""

        root = self.runtime_root(armed.runtime_id)
        cleanup_epoch = armed.pre_runtime_absence_epoch + 1
        pending = _load_exact_model(
            root / "cleanup" / f"absence-{cleanup_epoch}-pending.json",
            _NeverStartedCleanupPending,
            owner_uid=self.deployment.allowed_client_uid,
        )
        assert isinstance(pending, _NeverStartedCleanupPending)
        if (
            terminal.status != "fired"
            or terminal.deployment_sha256 != self.deployment.deployment_sha256
            or terminal.armed_job_sha256 != armed.job_sha256
            or pending.deadline_watchdog is None
            or pending.deadline_watchdog.runtime_id != armed.runtime_id
            or pending.deadline_watchdog.authorization_request_sha256
            != armed.authorization_request_sha256
            or pending.deadline_watchdog.runtime_launch_authorization_sha256
            != armed.runtime_launch_authorization_sha256
            or pending.deadline_watchdog.pre_runtime_absence_epoch
            != armed.pre_runtime_absence_epoch
        ):
            raise OCIWatchdogError("fired watchdog differs from exact cleanup generation")
        if terminal.container_id is None:
            if (
                terminal.container_was_running is not None
                or pending.container_id is not None
                or pending.create_submission is not None
            ):
                raise OCIWatchdogError("fired-absent watchdog differs from exact absence cleanup")
            return pending, "fired_absent"
        if (
            terminal.container_was_running is not False
            or pending.container_id != terminal.container_id
            or pending.create_submission is None
            or pending.create_submission.phase != "create"
        ):
            raise OCIWatchdogError("fired-stopped watchdog differs from exact CREATED cleanup")
        return pending, "fired_stopped"


class DurableDeadlineWatchdogService:
    """Root service core; run :meth:`serve_forever` only from the pinned systemd unit."""

    def __init__(
        self,
        *,
        policy: DeploymentPinnedOCIPolicy,
        deployment: SystemdWatchdogDeploymentPin,
    ) -> None:
        self._policy = DeploymentPinnedOCIPolicy.model_validate(policy.model_dump(mode="python"))
        self._deployment = SystemdWatchdogDeploymentPin.model_validate(
            deployment.model_dump(mode="python")
        )
        if (
            self._deployment.policy_sha256 != self._policy.policy_sha256
            or self._deployment.allowed_client_uid != self._policy.workload_uid
            or self._deployment.allowed_client_gid != self._policy.workload_gid
        ):
            raise ValueError("watchdog deployment differs from OCI policy principal")
        self._scope = _WatchdogJournalScope(policy=self._policy, deployment=self._deployment)
        self._stop_event = threading.Event()
        # The pinned service unit permits one daemon process, while this lock serializes
        # deadline firing with an in-flight retirement inside that process.  Durable terminal
        # and firing-intent records are still the authority across daemon restarts.
        self._decision_lock = threading.RLock()

    def serve_forever(self) -> None:
        self._require_root_systemd_service()
        state_root = self._prepare_state_root()
        lock_fd = os.open(
            state_root / "service.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        server: socket.socket | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server = self._create_server_socket()
            server.settimeout(0.25)
            while not self._stop_event.is_set():
                self.recover_due_jobs()
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(2)
                    try:
                        self._serve_connection(connection)
                    except (OCIDeploymentDependencyError, OSError, TimeoutError, ValueError):
                        continue
        except BlockingIOError as exc:
            raise OCIWatchdogError("another watchdog service instance owns the deployment") from exc
        finally:
            if server is not None:
                server.close()
            try:
                Path(self._deployment.socket_path).unlink()
            except FileNotFoundError:
                pass
            os.close(lock_fd)

    def stop(self) -> None:
        self._stop_event.set()

    def arm(self, request: Mapping[str, object]) -> dict[str, object]:
        with self._decision_lock:
            return self._arm_locked(request)

    def _arm_locked(self, request: Mapping[str, object]) -> dict[str, object]:
        boot_id = self._current_boot_id()
        runtime_id = self._required_text(request, "runtime_id")
        authorization_request_sha256 = self._required_text(request, "authorization_request_sha256")
        path = self._armed_scope_path(runtime_id, authorization_request_sha256)
        existing_value = self._recover_watchdog_record(
            path,
            _WatchdogArmedRecord,
        )
        existing = existing_value if isinstance(existing_value, _WatchdogArmedRecord) else None
        if existing is not None:
            assert isinstance(existing, _WatchdogArmedRecord)
        now = existing.armed_at if existing is not None else datetime.now(timezone.utc)
        record = self._scope.arm_record(
            preparation_sha256=self._required_text(request, "preparation_sha256"),
            boot_id=self._required_text(request, "boot_id"),
            runtime_id=runtime_id,
            container_name=self._required_text(request, "container_name"),
            engine_endpoint=self._required_text(request, "engine_endpoint"),
            authorization_request_sha256=authorization_request_sha256,
            runtime_launch_authorization_sha256=self._required_text(
                request, "runtime_launch_authorization_sha256"
            ),
            pre_runtime_absence_epoch=self._required_int(request, "pre_runtime_absence_epoch"),
            hard_deadline=self._required_datetime(request, "hard_deadline"),
            hard_deadline_boottime_ns=self._required_int(request, "hard_deadline_boottime_ns"),
            expected_evidence_sha256=self._required_text(request, "expected_evidence_sha256"),
            armed_at=now,
            service_boot_id=(existing.service_boot_id if existing is not None else boot_id),
        )
        if record.boot_id != boot_id or self._deadline_reached(record, now=now):
            raise OCIWatchdogError("watchdog cannot arm after boot or hard deadline changed")
        if existing is not None:
            if existing != record:
                raise OCIWatchdogError("durable watchdog armed record changed stable scope")
            published = existing
        else:
            published = self._publish_once(path, record)
        assert isinstance(published, _WatchdogArmedRecord)
        if published.model_dump(mode="python", exclude={"armed_at"}) != record.model_dump(
            mode="python", exclude={"armed_at"}
        ):
            raise OCIWatchdogError("durable watchdog armed record changed stable scope")
        record = published
        observed = self._load_armed(record.runtime_id, record.authorization_request_sha256)
        if observed != record:
            raise OCIWatchdogError("durable watchdog armed record differs during replay")
        return self._response(
            operation="arm",
            evidence_sha256=record.expected_evidence_sha256,
            job_sha256=record.job_sha256,
        )

    def retire(self, request: Mapping[str, object]) -> dict[str, object]:
        with self._decision_lock:
            return self._retire_locked(request)

    def _retire_locked(self, request: Mapping[str, object]) -> dict[str, object]:
        runtime_id = self._required_text(request, "runtime_id")
        authorization_request_sha256 = self._required_text(request, "authorization_request_sha256")
        armed = self._load_armed(runtime_id, authorization_request_sha256)
        supplied = (
            self._required_text(request, "preparation_sha256"),
            runtime_id,
            self._required_text(request, "container_name"),
            authorization_request_sha256,
            self._required_text(request, "runtime_launch_authorization_sha256"),
            self._required_int(request, "pre_runtime_absence_epoch"),
        )
        expected_scope = (
            armed.preparation_sha256,
            armed.runtime_id,
            armed.container_name,
            armed.authorization_request_sha256,
            armed.runtime_launch_authorization_sha256,
            armed.pre_runtime_absence_epoch,
        )
        if supplied != expected_scope:
            raise OCIWatchdogError("watchdog retirement differs from armed generation")
        cleanup_evidence = self._scope.retirement_evidence(
            armed=armed,
            watchdog_journal_sha256=self._required_text(request, "watchdog_journal_sha256"),
            expected_evidence_sha256=self._required_text(request, "expected_evidence_sha256"),
        )
        terminal = self._recover_terminal_decision(armed)
        if terminal is None and self._deadline_reached(
            armed,
            now=datetime.now(timezone.utc),
        ):
            # The node caller holds the shared runtime-generation flock.  Reinspect only after its
            # exact cleanup pending journal exists, then freeze absent/CREATED as fired-quiescent;
            # a running generation is killed and remains ineligible for never-started cleanup.
            self._fire_locked(
                armed,
                now=datetime.now(timezone.utc),
                finalize_nonrunning=True,
            )
            terminal = self._recover_terminal_decision(armed)
            if terminal is None:
                raise OCIWatchdogError("overdue cleanup produced no terminal watchdog decision")
        if terminal is not None and terminal.status == "fired":
            pending, decision = self._scope.fired_cleanup_quiescence(
                armed=armed,
                terminal=terminal,
            )
            self._verify_cleanup_quiescent_container(
                armed=armed,
                terminal=terminal,
                pending=pending,
            )
            path = self._cleanup_quiescence_path(armed)
            recovered = self._recover_watchdog_record(
                path,
                _WatchdogCleanupQuiescenceRecord,
            )
            if recovered is None:
                expected_quiescence = _WatchdogCleanupQuiescenceRecord(
                    deployment_sha256=self._deployment.deployment_sha256,
                    armed_job_sha256=armed.job_sha256,
                    cleanup_pending_journal_sha256=pending.journal_sha256,
                    fired_terminal_sha256=canonical_sha256(terminal),
                    cleanup_evidence_sha256=cleanup_evidence,
                    decision=decision,
                    container_id=terminal.container_id,
                    acknowledged_at=datetime.now(timezone.utc),
                    service_boot_id=self._current_boot_id(),
                )
                recovered = self._publish_once(path, expected_quiescence)
            assert isinstance(recovered, _WatchdogCleanupQuiescenceRecord)
            stable_quiescence = (
                self._deployment.deployment_sha256,
                armed.job_sha256,
                pending.journal_sha256,
                canonical_sha256(terminal),
                cleanup_evidence,
                decision,
                terminal.container_id,
            )
            if (
                recovered.deployment_sha256,
                recovered.armed_job_sha256,
                recovered.cleanup_pending_journal_sha256,
                recovered.fired_terminal_sha256,
                recovered.cleanup_evidence_sha256,
                recovered.decision,
                recovered.container_id,
            ) != stable_quiescence:
                raise OCIWatchdogError("durable fired watchdog quiescence changed during replay")
            return self._response(
                operation="retire",
                evidence_sha256=cleanup_evidence,
                job_sha256=armed.job_sha256,
                terminal_decision=decision,
                cleanup_quiescence_record_sha256=(recovered.quiescence_record_sha256),
                cleanup_container_id=terminal.container_id,
            )
        firing_intent = self._recover_firing_intent(armed)
        if firing_intent is not None:
            raise OCIWatchdogError("watchdog firing already owns the terminal decision")
        retirement = cleanup_evidence
        if terminal is None:
            expected_terminal = _WatchdogTerminalRecord(
                deployment_sha256=self._deployment.deployment_sha256,
                armed_job_sha256=armed.job_sha256,
                status="retired",
                retirement_evidence_sha256=retirement,
                completed_at=datetime.now(timezone.utc),
                service_boot_id=self._current_boot_id(),
            )
            published = self._publish_once(self._terminal_path(armed), expected_terminal)
            assert isinstance(published, _WatchdogTerminalRecord)
            terminal = published
        if (
            terminal.status != "retired"
            or terminal.armed_job_sha256 != armed.job_sha256
            or terminal.retirement_evidence_sha256 != retirement
        ):
            raise OCIWatchdogError("durable watchdog retirement changed during replay")
        return self._response(
            operation="retire",
            evidence_sha256=retirement,
            job_sha256=armed.job_sha256,
            terminal_decision="retired",
        )

    def recover_due_jobs(self) -> int:
        return self._recover_due_jobs_locked()

    def _recover_due_jobs_locked(self) -> int:
        state_root = Path(self._deployment.state_root)
        if not state_root.exists():
            return 0
        armed_paths = sorted(state_root.glob("armed-*.json"))
        if len(armed_paths) > self._deployment.maximum_active_jobs:
            raise OCIWatchdogError("watchdog active job bound is exceeded")
        fired = 0
        now = datetime.now(timezone.utc)
        for path in armed_paths:
            armed = self._read_service_model(path, _WatchdogArmedRecord)
            assert isinstance(armed, _WatchdogArmedRecord)
            # Skip inactive jobs before any lock.  Each overdue scope then performs only
            # nonblocking mutation-lock arbitration, so one slow Docker operation cannot starve
            # another overdue running cgroup or the service socket.
            if self._recover_terminal_decision(armed) is not None or not self._deadline_reached(
                armed, now=now
            ):
                continue
            with self._decision_lock:
                current = self._load_armed(
                    armed.runtime_id,
                    armed.authorization_request_sha256,
                )
                if current != armed:
                    raise OCIWatchdogError("watchdog armed job changed during overdue recovery")
                if self._recover_terminal_decision(armed) is not None:
                    continue
                self._fire_locked(armed, now=now)
                fired += 1
        return fired

    def _fire(self, armed: _WatchdogArmedRecord, *, now: datetime) -> None:
        with self._decision_lock:
            self._fire_locked(armed, now=now)

    def _fire_locked(
        self,
        armed: _WatchdogArmedRecord,
        *,
        now: datetime,
        finalize_nonrunning: bool = False,
    ) -> None:
        existing_terminal = self._recover_terminal_decision(armed)
        if existing_terminal is not None:
            firing_intent = self._recover_firing_intent(armed)
            if existing_terminal.status == "retired" and firing_intent is not None:
                raise OCIWatchdogError("watchdog has conflicting durable terminal decisions")
            return
        firing_intent = self._ensure_firing_intent(armed, now=now)
        if firing_intent.container_was_running is not True:
            # Absence/CREATED is an observation, not permanent quiescence.  Only a fresh second
            # inspection while the narrow create/start mutation flock is demonstrably free can
            # finalize it; a busy lock leaves the job armed for the next service tick.
            with self._engine_mutation_generation_lock(armed) as acquired:
                if not acquired:
                    return
                firing_intent = self._ensure_firing_intent(armed, now=now)
                if firing_intent.container_was_running is not True and not finalize_nonrunning:
                    return
        kill_completed: _WatchdogKillCompleted | None = None
        if firing_intent.container_was_running is True:
            kill_completed = self._recover_kill_completed(armed, firing_intent)
            if kill_completed is None:
                assert firing_intent.container_id is not None
                assert firing_intent.init_pid is not None
                assert firing_intent.cgroup_path is not None
                assert firing_intent.cgroup_identity_sha256 is not None
                cgroup_path, cgroup_identity_sha256 = self._kill_exact_cgroup(
                    container_id=firing_intent.container_id,
                    init_pid=firing_intent.init_pid,
                    cgroup_path=firing_intent.cgroup_path,
                    expected_identity_sha256=firing_intent.cgroup_identity_sha256,
                )
                _durable_publish_checkpoint(
                    "watchdog-cgroup-killed-before-completed",
                    self._kill_completed_path(armed),
                )
                expected_kill = _WatchdogKillCompleted(
                    deployment_sha256=self._deployment.deployment_sha256,
                    armed_job_sha256=armed.job_sha256,
                    firing_intent_sha256=firing_intent.firing_intent_sha256,
                    container_id=firing_intent.container_id,
                    cgroup_path=cgroup_path,
                    cgroup_identity_sha256=cgroup_identity_sha256,
                    cgroup_empty=True,
                    completed_at=datetime.now(timezone.utc),
                    service_boot_id=self._current_boot_id(),
                )
                published_kill = self._publish_once(self._kill_completed_path(armed), expected_kill)
                assert isinstance(published_kill, _WatchdogKillCompleted)
                if published_kill.model_dump(
                    mode="python", exclude={"completed_at", "service_boot_id"}
                ) != expected_kill.model_dump(
                    mode="python", exclude={"completed_at", "service_boot_id"}
                ):
                    raise OCIWatchdogError("durable watchdog kill proof changed during replay")
                kill_completed = published_kill
        terminal = _WatchdogTerminalRecord(
            deployment_sha256=self._deployment.deployment_sha256,
            armed_job_sha256=armed.job_sha256,
            status="fired",
            container_id=firing_intent.container_id,
            container_was_running=firing_intent.container_was_running,
            cgroup_path=(kill_completed.cgroup_path if kill_completed is not None else None),
            cgroup_identity_sha256=(
                kill_completed.cgroup_identity_sha256 if kill_completed is not None else None
            ),
            cgroup_empty=(kill_completed.cgroup_empty if kill_completed is not None else None),
            completed_at=now,
            service_boot_id=self._current_boot_id(),
        )
        published = self._publish_once(self._terminal_path(armed), terminal)
        assert isinstance(published, _WatchdogTerminalRecord)
        if published.model_dump(
            mode="python", exclude={"completed_at", "service_boot_id"}
        ) != terminal.model_dump(mode="python", exclude={"completed_at", "service_boot_id"}):
            raise OCIWatchdogError("durable watchdog firing changed stable terminal proof")

    def _ensure_firing_intent(
        self,
        armed: _WatchdogArmedRecord,
        *,
        now: datetime,
    ) -> _WatchdogFiringIntent:
        existing = self._recover_firing_intent(armed)
        if existing is not None:
            if existing.container_was_running is not True:
                raise OCIWatchdogError("nonrunning watchdog intent cannot be permanent evidence")
            return existing
        inspection = self._inspect_container(armed.container_name)
        container_id: str | None = None
        was_running: bool | None = None
        cgroup_path: str | None = None
        cgroup_identity_sha256: str | None = None
        init_pid: int | None = None
        if inspection is not None:
            container_id = inspection.get("Id") if isinstance(inspection.get("Id"), str) else None
            config = inspection.get("Config")
            state = inspection.get("State")
            if (
                container_id is None
                or _CONTAINER_ID.fullmatch(container_id) is None
                or inspection.get("Name") != f"/{armed.container_name}"
                or not isinstance(config, dict)
                or config.get("Labels") != dict(armed.container_labels)
                or not isinstance(state, dict)
                or not isinstance(state.get("Running"), bool)
                or isinstance(state.get("Pid"), bool)
                or not isinstance(state.get("Pid"), int)
            ):
                raise OCIWatchdogError("overdue container differs from exact watchdog custody")
            was_running = state["Running"]
            if was_running:
                init_pid = state["Pid"]
                if init_pid <= 1:
                    raise OCIWatchdogError("running container has no safe init pid")
                cgroup_path, cgroup_identity_sha256 = self._resolve_cgroup_identity(
                    container_id=container_id,
                    init_pid=init_pid,
                )
        intent = _WatchdogFiringIntent(
            deployment_sha256=self._deployment.deployment_sha256,
            armed_job_sha256=armed.job_sha256,
            container_id=container_id,
            container_was_running=was_running,
            init_pid=init_pid,
            cgroup_path=cgroup_path,
            cgroup_identity_sha256=cgroup_identity_sha256,
            inspected_at=now,
            service_boot_id=self._current_boot_id(),
        )
        if intent.container_was_running is not True:
            return intent
        published = self._publish_once(self._firing_intent_path(armed), intent)
        assert isinstance(published, _WatchdogFiringIntent)
        if published.model_dump(
            mode="python", exclude={"inspected_at", "service_boot_id"}
        ) != intent.model_dump(mode="python", exclude={"inspected_at", "service_boot_id"}):
            raise OCIWatchdogError("durable watchdog firing intent changed during replay")
        return published

    def _inspect_container(self, identifier: str) -> dict[str, object] | None:
        completed = self._run_docker(("container", "inspect", identifier), allowed=(0, 1))
        if completed.returncode == 1:
            expected = f"Error: No such object: {identifier}\n".encode()
            if completed.stdout or completed.stderr != expected:
                raise OCIWatchdogError("Docker absence response is not exact")
            return None
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OCIWatchdogError("Docker watchdog inspection is not JSON") from exc
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise OCIWatchdogError("Docker watchdog inspection is not one object")
        return payload[0]

    @staticmethod
    def _container_cgroup_path(payload: str, *, container_id: str) -> PurePosixPath:
        matches: list[str] = []
        for line in payload.splitlines():
            hierarchy, separator, remainder = line.partition(":")
            controllers, separator_two, cgroup_path = remainder.partition(":")
            if separator and separator_two and hierarchy == "0" and controllers == "":
                matches.append(cgroup_path)
        if len(matches) != 1:
            raise OCIWatchdogError("container does not expose one unified cgroup-v2 path")
        cgroup = PurePosixPath(matches[0])
        if (
            not cgroup.is_absolute()
            or ".." in cgroup.parts
            or cgroup.name not in {container_id, f"docker-{container_id}.scope"}
        ):
            raise OCIWatchdogError("container cgroup path differs from exact Docker identity")
        return cgroup

    @staticmethod
    def _read_control_descriptor(descriptor: int) -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, 64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise OCIWatchdogError("cgroup control evidence exceeded its byte bound")
        return payload

    @contextmanager
    def _open_exact_cgroup(self, cgroup: PurePosixPath) -> Iterator[int]:
        root_descriptor: int | None = None
        current_descriptor: int | None = None
        try:
            root_descriptor = os.open(
                "/sys/fs/cgroup",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            current_descriptor = root_descriptor
            for component in cgroup.parts[1:]:
                opened = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_descriptor,
                )
                if current_descriptor != root_descriptor:
                    os.close(current_descriptor)
                current_descriptor = opened
            yield current_descriptor
        finally:
            if current_descriptor is not None and current_descriptor != root_descriptor:
                os.close(current_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    @staticmethod
    def _cgroup_identity_sha256(
        *,
        container_id: str,
        init_pid: int,
        cgroup: PurePosixPath,
        metadata: os.stat_result,
    ) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.watchdog_cgroup_v2_identity.v1",
                "container_id": container_id,
                "init_pid": init_pid,
                "path": str(cgroup),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
        )

    def _resolve_cgroup_identity(self, *, container_id: str, init_pid: int) -> tuple[str, str]:
        if init_pid <= 1:
            raise OCIWatchdogError("running container has no safe init pid")
        try:
            cgroup_payload = Path(f"/proc/{init_pid}/cgroup").read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise OCIWatchdogError("container init cgroup identity is unavailable") from exc
        cgroup = self._container_cgroup_path(cgroup_payload, container_id=container_id)
        try:
            with self._open_exact_cgroup(cgroup) as cgroup_descriptor:
                metadata = os.fstat(cgroup_descriptor)
                procs_descriptor = os.open(
                    "cgroup.procs",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=cgroup_descriptor,
                )
                try:
                    procs_before = self._read_control_descriptor(procs_descriptor).decode("ascii")
                finally:
                    os.close(procs_descriptor)
                try:
                    process_ids = {int(value) for value in procs_before.split()}
                except ValueError as exc:
                    raise OCIWatchdogError("container cgroup process list is invalid") from exc
                if init_pid not in process_ids:
                    raise OCIWatchdogError("container init pid escaped its exact cgroup")
                identity = self._cgroup_identity_sha256(
                    container_id=container_id,
                    init_pid=init_pid,
                    cgroup=cgroup,
                    metadata=metadata,
                )
                return str(cgroup), identity
        except (OSError, UnicodeError) as exc:
            raise OCIWatchdogError("watchdog cgroup-v2 identity failed closed") from exc

    def _verify_cleanup_quiescent_container(
        self,
        *,
        armed: _WatchdogArmedRecord,
        terminal: _WatchdogTerminalRecord,
        pending: _NeverStartedCleanupPending,
    ) -> None:
        inspection = self._inspect_container(armed.container_name)
        if terminal.container_id is None:
            if inspection is not None or pending.container_id is not None:
                raise OCIWatchdogError("fired-absent cleanup is no longer exactly absent")
            return
        state = inspection.get("State") if inspection is not None else None
        config = inspection.get("Config") if inspection is not None else None
        if (
            inspection is None
            or inspection.get("Id") != terminal.container_id
            or inspection.get("Name") != f"/{armed.container_name}"
            or not isinstance(config, dict)
            or config.get("Labels") != dict(armed.container_labels)
            or not isinstance(state, dict)
            or state.get("Status") != "created"
            or state.get("Running") is not False
            or state.get("Pid") != 0
            or isinstance(state.get("Pid"), bool)
            or pending.container_id != terminal.container_id
        ):
            raise OCIWatchdogError("fired-stopped cleanup is not exact CREATED/PID0 quiescence")

    @staticmethod
    def _inspection_proves_stopped(
        inspection: dict[str, object] | None,
        *,
        container_id: str,
    ) -> bool:
        if inspection is None:
            return True
        state = inspection.get("State")
        if inspection.get("Id") != container_id or not isinstance(state, dict):
            raise OCIWatchdogError("post-kill Docker identity changed")
        running = state.get("Running")
        if not isinstance(running, bool):
            raise OCIWatchdogError("post-kill Docker state is incomplete")
        return not running

    @staticmethod
    def _parse_cgroup_events(payload: str) -> dict[str, str]:
        events: dict[str, str] = {}
        for line in payload.splitlines():
            key, separator, value = line.partition(" ")
            if not separator or key in events:
                raise OCIWatchdogError("cgroup.events is not canonical")
            events[key] = value
        if events.get("populated") not in {"0", "1"}:
            raise OCIWatchdogError("cgroup.events lacks a canonical populated state")
        return events

    def _kill_exact_cgroup(
        self,
        *,
        container_id: str,
        init_pid: int,
        cgroup_path: str,
        expected_identity_sha256: str,
    ) -> tuple[str, str]:
        if init_pid <= 1:
            raise OCIWatchdogError("running container has no safe init pid")
        cgroup = PurePosixPath(cgroup_path)
        if (
            not cgroup.is_absolute()
            or ".." in cgroup.parts
            or cgroup.name not in {container_id, f"docker-{container_id}.scope"}
        ):
            raise OCIWatchdogError("durable watchdog cgroup path is unsafe")
        events_descriptor: int | None = None
        procs_descriptor: int | None = None
        kill_descriptor: int | None = None
        try:
            with self._open_exact_cgroup(cgroup) as cgroup_descriptor:
                metadata = os.fstat(cgroup_descriptor)
                identity = self._cgroup_identity_sha256(
                    container_id=container_id,
                    init_pid=init_pid,
                    cgroup=cgroup,
                    metadata=metadata,
                )
                if identity != expected_identity_sha256:
                    if self._inspection_proves_stopped(
                        self._inspect_container(container_id),
                        container_id=container_id,
                    ):
                        # Never signal a replacement inode.  A stopped/absent exact container
                        # plus loss of the old inode is the replay proof; retain the identity
                        # from the pre-kill durable intent.
                        return cgroup_path, expected_identity_sha256
                    raise OCIWatchdogError("durable watchdog cgroup identity changed before kill")
                events_descriptor = os.open(
                    "cgroup.events",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=cgroup_descriptor,
                )
                procs_descriptor = os.open(
                    "cgroup.procs",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=cgroup_descriptor,
                )
                events_before = self._parse_cgroup_events(
                    self._read_control_descriptor(events_descriptor).decode("ascii")
                )
                procs_before = self._read_control_descriptor(procs_descriptor).decode("ascii")
                try:
                    process_ids = {int(value) for value in procs_before.split()}
                except ValueError as exc:
                    raise OCIWatchdogError("container cgroup process list is invalid") from exc
                stopped_before = self._inspection_proves_stopped(
                    self._inspect_container(container_id),
                    container_id=container_id,
                )
                already_empty = (
                    events_before.get("populated") == "0" and not process_ids and stopped_before
                )
                if not already_empty:
                    if init_pid not in process_ids and not stopped_before:
                        raise OCIWatchdogError("container init pid escaped its exact cgroup")
                    kill_descriptor = os.open(
                        "cgroup.kill",
                        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=cgroup_descriptor,
                    )
                    if os.write(kill_descriptor, b"1") != 1:
                        raise OCIWatchdogError("cgroup.kill write made no progress")
                    deadline = time.monotonic() + 10
                    while True:
                        events = self._parse_cgroup_events(
                            self._read_control_descriptor(events_descriptor).decode("ascii")
                        )
                        procs_payload = self._read_control_descriptor(procs_descriptor).decode(
                            "ascii"
                        )
                        stopped = self._inspection_proves_stopped(
                            self._inspect_container(container_id),
                            container_id=container_id,
                        )
                        if events.get("populated") == "0" and not procs_payload.strip() and stopped:
                            break
                        if time.monotonic() >= deadline:
                            raise OCIWatchdogError("watchdog could not prove exact cgroup empty")
                        time.sleep(0.05)
                after_metadata = os.fstat(cgroup_descriptor)
                if (
                    metadata.st_dev != after_metadata.st_dev
                    or metadata.st_ino != after_metadata.st_ino
                    or metadata.st_mode != after_metadata.st_mode
                    or metadata.st_uid != after_metadata.st_uid
                    or metadata.st_gid != after_metadata.st_gid
                ):
                    raise OCIWatchdogError("container cgroup changed while watchdog killed it")
                return str(cgroup), identity
        except FileNotFoundError as exc:
            # A prior successful cgroup.kill can be followed by cgroup removal before the
            # completion record is published.  The old exact identity remains in the durable
            # firing intent; disappearance is accepted only with an absent/stopped full id.
            if self._inspection_proves_stopped(
                self._inspect_container(container_id),
                container_id=container_id,
            ):
                return cgroup_path, expected_identity_sha256
            raise OCIWatchdogError("watchdog cgroup disappeared while container still ran") from exc
        except (OSError, UnicodeError) as exc:
            raise OCIWatchdogError("watchdog cgroup-v2 kill failed closed") from exc
        finally:
            for descriptor in (kill_descriptor, procs_descriptor, events_descriptor):
                if descriptor is not None:
                    os.close(descriptor)

    def _run_docker(
        self, arguments: tuple[str, ...], *, allowed: tuple[int, ...]
    ) -> subprocess.CompletedProcess[bytes]:
        path = Path(self._policy.runtime_binary_path)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIWatchdogError("pinned watchdog Docker binary is unavailable") from exc
        command = (
            self._policy.runtime_binary_path,
            "--host",
            self._policy.engine_endpoint,
            *arguments,
        )
        try:
            metadata = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if (
                metadata.st_dev != self._policy.runtime_binary_device
                or metadata.st_ino != self._policy.runtime_binary_inode
                or metadata.st_uid != self._policy.runtime_binary_owner_uid
                or metadata.st_gid != self._policy.runtime_binary_owner_gid
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != self._policy.runtime_binary_mode
                or not metadata.st_mode & 0o111
                or metadata.st_mode & 0o022
                or digest.hexdigest() != self._policy.runtime_binary_sha256
                or host_parent_chain_sha256(path) != self._policy.runtime_binary_parent_chain_sha256
            ):
                raise OCIWatchdogError("watchdog Docker binary differs from root deployment pin")
            completed = subprocess.run(
                command,
                executable=f"/proc/self/fd/{descriptor}",
                pass_fds=(descriptor,),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={},
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OCIWatchdogError("watchdog Docker command failed") from exc
        finally:
            os.close(descriptor)
        if (
            completed.returncode not in allowed
            or len(completed.stdout) > _MAX_CONTROL_BYTES
            or len(completed.stderr) > _MAX_CONTROL_BYTES
        ):
            raise OCIWatchdogError("watchdog Docker command failed closed")
        return completed

    def _deadline_reached(self, armed: _WatchdogArmedRecord, *, now: datetime) -> bool:
        if now >= armed.hard_deadline:
            return True
        if armed.boot_id != self._current_boot_id():
            return False
        if not hasattr(time, "CLOCK_BOOTTIME"):
            raise OCIWatchdogError("watchdog lacks Linux CLOCK_BOOTTIME")
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME) >= armed.hard_deadline_boottime_ns

    def _require_root_systemd_service(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
            raise OCIWatchdogError("watchdog daemon must run as root on Linux")
        try:
            pid_one = Path("/proc/1/comm").read_text(encoding="ascii").strip()
            cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii")
            status = Path("/proc/self/status").read_text(encoding="ascii")
            module = Path(__file__).resolve(strict=True)
            module_metadata = module.lstat()
            module_sha256 = hashlib.sha256(module.read_bytes()).hexdigest()
        except (OSError, UnicodeError) as exc:
            raise OCIWatchdogError("watchdog cannot prove systemd supervision") from exc
        invocation_id = os.environ.get("INVOCATION_ID", "")
        if (
            pid_one != "systemd"
            or re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None
            or not _in_exact_systemd_unit(cgroup, self._deployment.systemd_unit_name)
            or re.search(r"^Uid:\s+0\s+0\s+0\s+0$", status, re.MULTILINE) is None
            or re.search(r"^Gid:\s+0\s+0\s+0\s+0$", status, re.MULTILINE) is None
            or module_metadata.st_uid != 0
            or module_metadata.st_gid != 0
            or module_metadata.st_dev != self._deployment.service_module_device
            or module_metadata.st_ino != self._deployment.service_module_inode
            or stat.S_IMODE(module_metadata.st_mode) != self._deployment.service_module_mode
            or module_sha256 != self._deployment.service_module_sha256
            or host_parent_chain_sha256(module)
            != self._deployment.service_module_parent_chain_sha256
        ):
            raise OCIWatchdogError("watchdog is not running in its pinned systemd service")
        _verify_root_file_pin(
            self._deployment.systemd_unit,
            label="watchdog systemd unit file",
            error_type=OCIWatchdogError,
        )
        _verify_root_process_executable(
            self._deployment.service_executable,
            error_type=OCIWatchdogError,
        )
        self._verify_deployment_roots()

    def _verify_deployment_roots(self) -> None:
        _verify_pinned_directory(
            Path(self._deployment.journal_root),
            owner_uid=self._deployment.allowed_client_uid,
            owner_gid=self._deployment.allowed_client_gid,
            mode=self._deployment.journal_root_mode,
            device=self._deployment.journal_root_device,
            inode=self._deployment.journal_root_inode,
            parent_chain_sha256=self._deployment.journal_root_parent_chain_sha256,
            label="watchdog runtime journal root",
            error_type=OCIWatchdogError,
        )
        for path, device, inode, mode, parent_chain, label in (
            (
                Path(self._deployment.state_root),
                self._deployment.state_root_device,
                self._deployment.state_root_inode,
                self._deployment.state_root_mode,
                self._deployment.state_root_parent_chain_sha256,
                "watchdog state root",
            ),
            (
                Path(self._deployment.socket_path).parent,
                self._deployment.socket_parent_device,
                self._deployment.socket_parent_inode,
                self._deployment.socket_parent_mode,
                self._deployment.socket_parent_parent_chain_sha256,
                "watchdog socket parent",
            ),
        ):
            _verify_pinned_directory(
                path,
                owner_uid=0,
                owner_gid=0,
                mode=mode,
                device=device,
                inode=inode,
                parent_chain_sha256=parent_chain,
                label=label,
                error_type=OCIWatchdogError,
            )

    def _prepare_state_root(self) -> Path:
        root = Path(self._deployment.state_root)
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or root.is_symlink()
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OCIWatchdogError("watchdog state root custody is unsafe")
        return root

    def _create_server_socket(self) -> socket.socket:
        path = Path(self._deployment.socket_path)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != 0:
                raise OCIWatchdogError("watchdog socket path contains an unsafe object")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chown(path, 0, self._deployment.allowed_client_gid)
        os.chmod(path, 0o660)
        server.listen(16)
        return server

    def _serve_connection(self, connection: socket.socket) -> None:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        peer_pid = int.from_bytes(credentials[0:4], sys.byteorder, signed=True)
        peer_uid = int.from_bytes(credentials[4:8], sys.byteorder, signed=True)
        peer_gid = int.from_bytes(credentials[8:12], sys.byteorder, signed=True)
        if (
            peer_pid <= 0
            or peer_uid != self._deployment.allowed_client_uid
            or peer_gid != self._deployment.allowed_client_gid
        ):
            raise OCIWatchdogError("watchdog client peer credentials differ from deployment")
        payload = bytearray()
        while b"\n" not in payload:
            chunk = connection.recv(64 * 1024)
            if not chunk:
                raise OCIWatchdogError("watchdog client closed an incomplete request")
            payload.extend(chunk)
            if len(payload) > _MAX_CONTROL_BYTES:
                raise OCIWatchdogError("watchdog request exceeded its byte bound")
        raw, newline, residue = bytes(payload).partition(b"\n")
        if newline != b"\n" or residue:
            raise OCIWatchdogError("watchdog request framing is not exact")
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCIWatchdogError("watchdog request is not JSON") from exc
        if not isinstance(request, dict):
            raise OCIWatchdogError("watchdog request is not one object")
        operation = request.pop("operation", None)
        if operation == "arm":
            response = self.arm(request)
        elif operation == "retire":
            response = self.retire(request)
        elif operation == "health" and not request:
            response = self._response(operation="health", evidence_sha256=None, job_sha256=None)
        else:
            raise OCIWatchdogError("watchdog operation is not allowed")
        connection.sendall(canonical_json_bytes(response) + b"\n")

    def _response(
        self,
        *,
        operation: str,
        evidence_sha256: str | None,
        job_sha256: str | None,
        terminal_decision: str | None = None,
        cleanup_quiescence_record_sha256: str | None = None,
        cleanup_container_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "schema": "aletheia.systemd_oci_watchdog_response.v1",
            "operation": operation,
            "deployment_sha256": self._deployment.deployment_sha256,
            "service_pid": os.getpid(),
            "service_boot_id": self._current_boot_id(),
            "managed_by_systemd": True,
            "evidence_sha256": evidence_sha256,
            "job_sha256": job_sha256,
            "terminal_decision": terminal_decision,
            "cleanup_quiescence_record_sha256": cleanup_quiescence_record_sha256,
            "cleanup_container_id": cleanup_container_id,
        }

    def _armed_path(self, record: _WatchdogArmedRecord) -> Path:
        return self._armed_scope_path(record.runtime_id, record.authorization_request_sha256)

    def _armed_scope_path(self, runtime_id: str, authorization_request_sha256: str) -> Path:
        key = canonical_sha256(
            {
                "runtime_id": runtime_id,
                "authorization_request_sha256": authorization_request_sha256,
            }
        )
        return Path(self._deployment.state_root) / f"armed-{key}.json"

    def _terminal_path(self, armed: _WatchdogArmedRecord) -> Path:
        return self._armed_path(armed).with_name(
            self._armed_path(armed).name.replace("armed-", "terminal-", 1)
        )

    def _firing_intent_path(self, armed: _WatchdogArmedRecord) -> Path:
        return self._armed_path(armed).with_name(
            self._armed_path(armed).name.replace("armed-", "firing-", 1)
        )

    def _kill_completed_path(self, armed: _WatchdogArmedRecord) -> Path:
        return self._armed_path(armed).with_name(
            self._armed_path(armed).name.replace("armed-", "killed-", 1)
        )

    def _cleanup_quiescence_path(self, armed: _WatchdogArmedRecord) -> Path:
        return self._armed_path(armed).with_name(
            self._armed_path(armed).name.replace("armed-", "quiescence-", 1)
        )

    @contextmanager
    def _engine_mutation_generation_lock(self, armed: _WatchdogArmedRecord) -> Iterator[bool]:
        """Try to join the node's narrow create/start serialization domain without blocking."""

        runtime_root = self._scope.runtime_root(armed.runtime_id)
        lock_path = runtime_root / "engine-mutation.lock"
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise OCIWatchdogError("watchdog engine mutation lock is unavailable") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != self._deployment.allowed_client_uid
                or before.st_gid != self._deployment.allowed_client_gid
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise OCIWatchdogError("watchdog engine mutation lock custody is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            after = os.fstat(descriptor)
            if _stat_identity(before) != _stat_identity(after):
                raise OCIWatchdogError("watchdog engine mutation lock changed while acquired")
            yield True
        except OSError as exc:
            raise OCIWatchdogError("watchdog engine mutation lock failed") from exc
        finally:
            os.close(descriptor)

    def _load_armed(
        self, runtime_id: str, authorization_request_sha256: str
    ) -> _WatchdogArmedRecord:
        value = self._read_service_model(
            self._armed_scope_path(runtime_id, authorization_request_sha256),
            _WatchdogArmedRecord,
        )
        assert isinstance(value, _WatchdogArmedRecord)
        return value

    def _load_terminal(self, armed: _WatchdogArmedRecord) -> _WatchdogTerminalRecord | None:
        path = self._terminal_path(armed)
        if not path.exists():
            return None
        value = self._read_service_model(path, _WatchdogTerminalRecord)
        assert isinstance(value, _WatchdogTerminalRecord)
        if value.armed_job_sha256 != armed.job_sha256:
            raise OCIWatchdogError("watchdog terminal record differs from armed job")
        return value

    def _recover_terminal_decision(
        self, armed: _WatchdogArmedRecord
    ) -> _WatchdogTerminalRecord | None:
        value = self._recover_watchdog_record(
            self._terminal_path(armed),
            _WatchdogTerminalRecord,
        )
        if value is None:
            return None
        assert isinstance(value, _WatchdogTerminalRecord)
        if (
            value.deployment_sha256 != self._deployment.deployment_sha256
            or value.armed_job_sha256 != armed.job_sha256
        ):
            raise OCIWatchdogError("watchdog terminal record differs from armed job")
        return value

    def _recover_firing_intent(self, armed: _WatchdogArmedRecord) -> _WatchdogFiringIntent | None:
        value = self._recover_watchdog_record(
            self._firing_intent_path(armed),
            _WatchdogFiringIntent,
        )
        if value is None:
            return None
        assert isinstance(value, _WatchdogFiringIntent)
        if (
            value.deployment_sha256 != self._deployment.deployment_sha256
            or value.armed_job_sha256 != armed.job_sha256
        ):
            raise OCIWatchdogError("watchdog firing intent differs from armed job")
        return value

    def _recover_kill_completed(
        self,
        armed: _WatchdogArmedRecord,
        firing_intent: _WatchdogFiringIntent,
    ) -> _WatchdogKillCompleted | None:
        value = self._recover_watchdog_record(
            self._kill_completed_path(armed),
            _WatchdogKillCompleted,
        )
        if value is None:
            return None
        assert isinstance(value, _WatchdogKillCompleted)
        expected = (
            self._deployment.deployment_sha256,
            armed.job_sha256,
            firing_intent.firing_intent_sha256,
            firing_intent.container_id,
            firing_intent.cgroup_path,
            firing_intent.cgroup_identity_sha256,
            True,
        )
        observed = (
            value.deployment_sha256,
            value.armed_job_sha256,
            value.firing_intent_sha256,
            value.container_id,
            value.cgroup_path,
            value.cgroup_identity_sha256,
            value.cgroup_empty,
        )
        if observed != expected:
            raise OCIWatchdogError("watchdog kill proof differs from firing intent")
        return value

    def _recover_watchdog_record(
        self,
        path: Path,
        model: type[ExecutionModel],
    ) -> ExecutionModel | None:
        """Recover a typed sealed record before choosing a new irreversible operation.

        A sealed 0400 pending inode is a committed durable decision and is promoted even if its
        timestamp differs from a freshly reconstructed request.  An unsealed 0600 inode is never
        evidence; only the exact single-link root-service residue is discarded.  This distinction
        gives a deterministic deadline race: a sealed retirement wins before any kill, while an
        unsealed retirement has not committed and an overdue firing may take ownership.
        """
        return _recover_root_record_once(
            path,
            model,
            owner_uid=self._trusted_state_owner_uid(),
            error_type=OCIWatchdogError,
        )

    def _publish_once(self, path: Path, value: ExecutionModel) -> ExecutionModel:
        payload = _publish_root_record_once(
            path,
            value,
            owner_uid=self._trusted_state_owner_uid(),
            error_type=OCIWatchdogError,
        )
        try:
            published = type(value).model_validate_json(payload)
        except ValidationError as exc:  # pragma: no cover - helper already validates
            raise OCIWatchdogError("published watchdog record failed typed reload") from exc
        if canonical_json_bytes(published) != payload:
            raise OCIWatchdogError("published watchdog record failed canonical reload")
        return published

    def _read_service_model(self, path: Path, model: type[ExecutionModel]) -> ExecutionModel:
        payload = _read_bounded_file(
            path,
            label="watchdog durable record",
            owner_uid=self._trusted_state_owner_uid(),
            maximum_bytes=_MAX_CONTROL_BYTES,
            allowed_modes=frozenset({0o400}),
        )
        try:
            value = model.model_validate_json(payload)
        except ValidationError as exc:
            raise OCIWatchdogError("watchdog durable record failed validation") from exc
        if canonical_json_bytes(value) != payload:
            raise OCIWatchdogError("watchdog durable record is not canonical")
        return value

    @staticmethod
    def _trusted_state_owner_uid() -> int:
        """Return the production watchdog-store owner (patchable in unit tests)."""

        return 0

    @staticmethod
    def _required_text(request: Mapping[str, object], key: str) -> str:
        value = request.get(key)
        if not isinstance(value, str) or not value:
            raise OCIWatchdogError(f"watchdog request {key} is not text")
        return value

    @staticmethod
    def _required_int(request: Mapping[str, object], key: str) -> int:
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OCIWatchdogError(f"watchdog request {key} is not a nonnegative integer")
        return value

    @staticmethod
    def _required_datetime(request: Mapping[str, object], key: str) -> datetime:
        value = request.get(key)
        if not isinstance(value, str):
            raise OCIWatchdogError(f"watchdog request {key} is not a timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise OCIWatchdogError(f"watchdog request {key} is not ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise OCIWatchdogError(f"watchdog request {key} is not UTC")
        return parsed

    @staticmethod
    def _current_boot_id() -> str:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise OCIWatchdogError("Linux boot identity is unavailable") from exc
        if re.fullmatch(r"[0-9a-f-]{36}", value) is None:
            raise OCIWatchdogError("Linux boot identity is not canonical")
        return value


class SystemdDeadlineWatchdogController:
    """Node-side client for the independent root/systemd watchdog service."""

    def __init__(
        self,
        *,
        policy: DeploymentPinnedOCIPolicy,
        deployment: SystemdWatchdogDeploymentPin,
    ) -> None:
        self._policy = DeploymentPinnedOCIPolicy.model_validate(policy.model_dump(mode="python"))
        self._deployment = SystemdWatchdogDeploymentPin.model_validate(
            deployment.model_dump(mode="python")
        )
        if self._deployment.policy_sha256 != self._policy.policy_sha256:
            raise ValueError("watchdog client deployment differs from OCI policy")
        self._scope = _WatchdogJournalScope(policy=self._policy, deployment=self._deployment)

    def arm_and_verify_deadline_watchdog(
        self,
        *,
        preparation_sha256: str,
        boot_id: str,
        runtime_id: str,
        container_name: str,
        engine_endpoint: str,
        authorization_request_sha256: str,
        runtime_launch_authorization_sha256: str,
        pre_runtime_absence_epoch: int,
        hard_deadline: datetime,
        hard_deadline_boottime_ns: int,
        expected_evidence_sha256: str,
    ) -> str:
        # Local reconstruction closes the legacy protocol's otherwise tempting echo path.  The
        # daemon repeats this reconstruction under root before its fsynced acknowledgement.
        expected_job = self._scope.arm_record(
            preparation_sha256=preparation_sha256,
            boot_id=boot_id,
            runtime_id=runtime_id,
            container_name=container_name,
            engine_endpoint=engine_endpoint,
            authorization_request_sha256=authorization_request_sha256,
            runtime_launch_authorization_sha256=runtime_launch_authorization_sha256,
            pre_runtime_absence_epoch=pre_runtime_absence_epoch,
            hard_deadline=hard_deadline,
            hard_deadline_boottime_ns=hard_deadline_boottime_ns,
            expected_evidence_sha256=expected_evidence_sha256,
            armed_at=datetime.now(timezone.utc),
            service_boot_id=boot_id,
        )
        response = self._request(
            {
                "operation": "arm",
                "preparation_sha256": preparation_sha256,
                "boot_id": boot_id,
                "runtime_id": runtime_id,
                "container_name": container_name,
                "engine_endpoint": engine_endpoint,
                "authorization_request_sha256": authorization_request_sha256,
                "runtime_launch_authorization_sha256": (runtime_launch_authorization_sha256),
                "pre_runtime_absence_epoch": pre_runtime_absence_epoch,
                "hard_deadline": hard_deadline.isoformat(),
                "hard_deadline_boottime_ns": hard_deadline_boottime_ns,
                "expected_evidence_sha256": expected_evidence_sha256,
            }
        )
        self._validate_response(
            response,
            operation="arm",
            evidence=expected_evidence_sha256,
            expected_job_sha256=expected_job.job_sha256,
        )
        return expected_evidence_sha256

    def retire_and_verify_deadline_watchdog(
        self,
        *,
        preparation_sha256: str,
        runtime_id: str,
        container_name: str,
        authorization_request_sha256: str,
        runtime_launch_authorization_sha256: str,
        pre_runtime_absence_epoch: int,
        watchdog_journal_sha256: str,
        expected_evidence_sha256: str,
    ) -> OCIWatchdogCleanupQuiescence:
        armed = self._load_local_armed_scope(runtime_id, authorization_request_sha256)
        derived = self._scope.retirement_evidence(
            armed=armed,
            watchdog_journal_sha256=watchdog_journal_sha256,
            expected_evidence_sha256=expected_evidence_sha256,
        )
        response = self._request(
            {
                "operation": "retire",
                "preparation_sha256": preparation_sha256,
                "runtime_id": runtime_id,
                "container_name": container_name,
                "authorization_request_sha256": authorization_request_sha256,
                "runtime_launch_authorization_sha256": (runtime_launch_authorization_sha256),
                "pre_runtime_absence_epoch": pre_runtime_absence_epoch,
                "watchdog_journal_sha256": watchdog_journal_sha256,
                "expected_evidence_sha256": expected_evidence_sha256,
            }
        )
        self._validate_response(
            response,
            operation="retire",
            evidence=derived,
            expected_job_sha256=armed.job_sha256,
        )
        try:
            return OCIWatchdogCleanupQuiescence(
                cleanup_evidence_sha256=derived,
                decision=response["terminal_decision"],
                service_quiescence_record_sha256=(response["cleanup_quiescence_record_sha256"]),
                container_id=response["cleanup_container_id"],
            )
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise OCIWatchdogError("watchdog cleanup response has no typed quiescence") from exc

    def _load_local_armed_scope(
        self, runtime_id: str, authorization_request_sha256: str
    ) -> _WatchdogArmedRecord:
        # The client does not read the root service store.  Reconstruct the stable armed facts from
        # the runtime journal; timestamps are irrelevant to retirement evidence.
        root = self._scope.runtime_root(runtime_id)
        watchdog = _load_exact_model(
            root / "deadline-watchdog.json",
            _DeadlineWatchdogJournal,
            owner_uid=self._deployment.allowed_client_uid,
        )
        config = _load_exact_model(
            root / "oci-config.json",
            OCIConfiguration,
            owner_uid=self._deployment.allowed_client_uid,
        )
        preparation = _load_exact_model(
            root / "preparation.json",
            RuntimePreparation,
            owner_uid=self._deployment.allowed_client_uid,
        )
        assert isinstance(watchdog, _DeadlineWatchdogJournal)
        assert isinstance(config, OCIConfiguration)
        assert isinstance(preparation, RuntimePreparation)
        return _WatchdogArmedRecord(
            deployment_sha256=self._deployment.deployment_sha256,
            preparation_sha256=watchdog.preparation_sha256,
            boot_id=preparation.boot_id,
            runtime_id=runtime_id,
            container_name=watchdog.container_name,
            engine_endpoint="unix:///var/run/docker.sock",
            authorization_request_sha256=authorization_request_sha256,
            runtime_launch_authorization_sha256=(watchdog.runtime_launch_authorization_sha256),
            pre_runtime_absence_epoch=watchdog.pre_runtime_absence_epoch,
            hard_deadline=watchdog.hard_deadline,
            hard_deadline_boottime_ns=watchdog.hard_deadline_boottime_ns,
            expected_evidence_sha256=watchdog.watchdog_evidence_sha256,
            container_labels=tuple(config.labels),
            armed_at=watchdog.hard_deadline,
            service_boot_id="historical",
        )

    def _request(self, request: Mapping[str, object]) -> dict[str, object]:
        payload = canonical_json_bytes(dict(request)) + b"\n"
        if len(payload) > _MAX_CONTROL_BYTES:
            raise OCIWatchdogError("watchdog client request exceeds its byte bound")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(self._deployment.socket_path)
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid = int.from_bytes(credentials[0:4], sys.byteorder, signed=True)
            peer_uid = int.from_bytes(credentials[4:8], sys.byteorder, signed=True)
            peer_gid = int.from_bytes(credentials[8:12], sys.byteorder, signed=True)
            if peer_pid <= 0 or peer_uid != 0 or peer_gid != 0:
                raise OCIWatchdogError("watchdog socket peer is not the root service")
            connection.sendall(payload)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    raise OCIWatchdogError("watchdog service closed an incomplete response")
                response.extend(chunk)
                if len(response) > _MAX_CONTROL_BYTES:
                    raise OCIWatchdogError("watchdog response exceeds its byte bound")
        except (OSError, TimeoutError) as exc:
            raise OCIWatchdogError("independent watchdog service is unavailable") from exc
        finally:
            connection.close()
        raw, newline, residue = bytes(response).partition(b"\n")
        if newline != b"\n" or residue:
            raise OCIWatchdogError("watchdog response framing is not exact")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OCIWatchdogError("watchdog response is not JSON") from exc
        if not isinstance(value, dict) or value.get("service_pid") != peer_pid:
            raise OCIWatchdogError("watchdog response differs from socket peer identity")
        return value

    def _validate_response(
        self,
        response: Mapping[str, object],
        *,
        operation: str,
        evidence: str,
        expected_job_sha256: str,
    ) -> None:
        terminal_decision = response.get("terminal_decision")
        quiescence_sha256 = response.get("cleanup_quiescence_record_sha256")
        cleanup_container_id = response.get("cleanup_container_id")
        retirement_shape_is_valid = (
            operation != "retire"
            and terminal_decision is None
            and quiescence_sha256 is None
            and cleanup_container_id is None
        ) or (
            operation == "retire"
            and (
                (
                    terminal_decision == "retired"
                    and quiescence_sha256 is None
                    and cleanup_container_id is None
                )
                or (
                    terminal_decision == "fired_absent"
                    and isinstance(quiescence_sha256, str)
                    and _SHA256.fullmatch(quiescence_sha256) is not None
                    and cleanup_container_id is None
                )
                or (
                    terminal_decision == "fired_stopped"
                    and isinstance(quiescence_sha256, str)
                    and _SHA256.fullmatch(quiescence_sha256) is not None
                    and isinstance(cleanup_container_id, str)
                    and _CONTAINER_ID.fullmatch(cleanup_container_id) is not None
                )
            )
        )
        if (
            set(response)
            != {
                "schema",
                "operation",
                "deployment_sha256",
                "service_pid",
                "service_boot_id",
                "managed_by_systemd",
                "evidence_sha256",
                "job_sha256",
                "terminal_decision",
                "cleanup_quiescence_record_sha256",
                "cleanup_container_id",
            }
            or response.get("schema") != "aletheia.systemd_oci_watchdog_response.v1"
            or response.get("operation") != operation
            or response.get("deployment_sha256") != self._deployment.deployment_sha256
            or response.get("managed_by_systemd") is not True
            or response.get("evidence_sha256") != evidence
            or response.get("job_sha256") != expected_job_sha256
            or not retirement_shape_is_valid
        ):
            raise OCIWatchdogError("watchdog service attestation differs from deployment scope")


__all__ = [
    "DurableDeadlineWatchdogService",
    "ImmutableOCIImageLaunchGateVerifier",
    "LoopbackOutputQuotaProvisionerClient",
    "LoopbackOutputQuotaProvisioningService",
    "LoopbackOutputQuotaController",
    "LoopbackQuotaProvisionerDeploymentPin",
    "OCIDeploymentDependencyError",
    "OCIImageAttestationError",
    "OCIOutputQuotaError",
    "OCIWatchdogError",
    "PinnedOCIImageLayout",
    "PinnedRootExecutable",
    "PinnedRootFile",
    "SystemdDeadlineWatchdogController",
    "SystemdWatchdogDeploymentPin",
]
