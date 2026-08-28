"""Independent, read-only Linux observer for one commissioned qualification deployment.

The observer consumes only canonical commissioning/installation evidence and live host state.  It
does not install, enable, repair, reserve, execute, or admit anything.  A root-owned Ed25519 key
separate from every runtime authority signs the resulting closed deployment observation.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.oci_deployment import (
    ImmutableOCIImageLaunchGateVerifier,
    PinnedRootExecutable,
    PinnedRootFile,
    host_parent_chain_sha256,
)
from aletheia.execution.qualification_deployment import (
    ObservedNativeDependency,
    ObservedNativeDependencyClosure,
    POSTGRESQL_DANGEROUS_BUILTIN_ROLES,
    PostgreSQLRestrictedRoleObservation,
    QualificationDeploymentObserverPin,
    QualificationDeploymentSpecV1,
    QualificationExpectedRootExecutable,
    QualificationLinuxDeploymentObservation,
    QualificationObservedCustodyRoot,
    QualificationObservedRootCodeTree,
    QualificationReviewedCodeTree,
    QualificationSystemdServiceIdentityObservation,
    RenderedSystemdUnit,
    SignedQualificationLinuxDeploymentObservation,
    expected_qualification_systemd_service_identities,
    render_postgresql_acl,
    render_systemd_units,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_v2_contracts import PinnedOutputWorkspaceRoot
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256
from aletheia.qualification_authority_commissioning import (
    LinuxQualificationAuthorityCommissioningHost,
    QualificationAuthorityCommissioningReceiptV1,
    QualificationAuthorityCommissioningRequestV1,
    build_qualification_authority_commissioning_plan,
    qualification_postgresql_commissioning_intent,
    verify_qualification_authority_commissioning_receipt,
)
from aletheia.qualification_installer import (
    QualificationInstallationReceiptV1,
    verify_qualification_installation_receipt,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_MAX_COMMAND_BYTES = 4 * 1024 * 1024
_OBSERVER_SIGNATURE_DOMAIN = b"aletheia.qualification-deployment-observation.v1\x00"
_LINUX_CAPABILITY_NAMES = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_KILL",
    "CAP_SETGID",
    "CAP_SETUID",
    "CAP_SETPCAP",
    "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_ADMIN",
    "CAP_NET_RAW",
    "CAP_IPC_LOCK",
    "CAP_IPC_OWNER",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE",
    "CAP_SYS_PACCT",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_TIME",
    "CAP_SYS_TTY_CONFIG",
    "CAP_MKNOD",
    "CAP_LEASE",
    "CAP_AUDIT_WRITE",
    "CAP_AUDIT_CONTROL",
    "CAP_SETFCAP",
    "CAP_MAC_OVERRIDE",
    "CAP_MAC_ADMIN",
    "CAP_SYSLOG",
    "CAP_WAKE_ALARM",
    "CAP_BLOCK_SUSPEND",
    "CAP_AUDIT_READ",
    "CAP_PERFMON",
    "CAP_BPF",
    "CAP_CHECKPOINT_RESTORE",
)


class QualificationObserverError(RuntimeError):
    """The observer config, live host, database, or signature failed closed."""


class QualificationObserverPrivateKeyPinV1(ExecutionModel):
    """Root-only observer signing-key source; key bytes never enter canonical evidence."""

    schema_name: Literal["aletheia.qualification_observer_private_key_pin"] = (
        "aletheia.qualification_observer_private_key_pin"
    )
    schema_version: Literal[1] = 1
    path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    owner_uid: Literal[0] = 0
    owner_gid: Literal[0] = 0
    mode: Literal[0o400] = 0o400

    @model_validator(mode="after")
    def _path_is_canonical(self) -> "QualificationObserverPrivateKeyPinV1":
        path = Path(self.path)
        if (
            not path.is_absolute()
            or str(path) != os.path.normpath(self.path)
            or self.path == "/"
            or any(character in self.path for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("observer private-key path must be canonical and absolute")
        return self


class QualificationDockerSecurityProjectionV1(ExecutionModel):
    """Stable, exact rootful-Docker security projection pinned before activation."""

    schema_name: Literal["aletheia.qualification_docker_security_projection"] = (
        "aletheia.qualification_docker_security_projection"
    )
    schema_version: Literal[1] = 1
    server_version: str = Field(min_length=1, max_length=128)
    kernel_version: str = Field(min_length=1, max_length=256)
    operating_system: str = Field(min_length=1, max_length=256)
    os_type: Literal["linux"] = "linux"
    architecture: str = Field(pattern=r"^[a-z0-9_]+$")
    cgroup_driver: Literal["systemd"] = "systemd"
    cgroup_version: Literal[2] = 2
    storage_driver: str = Field(min_length=1, max_length=128)
    docker_root_dir: str
    docker_root_device: int = Field(ge=0)
    docker_root_inode: int = Field(ge=1)
    docker_root_owner_uid: Literal[0] = 0
    docker_root_owner_gid: Literal[0] = 0
    docker_root_mode: int = Field(ge=0, le=0o7777)
    docker_root_parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    security_options: tuple[str, ...] = Field(min_length=2)
    rootless: Literal[False] = False

    @model_validator(mode="after")
    def _projection_is_canonical(self) -> "QualificationDockerSecurityProjectionV1":
        root = Path(self.docker_root_dir)
        if (
            not root.is_absolute()
            or str(root) != os.path.normpath(self.docker_root_dir)
            or root == Path("/")
            or self.security_options != tuple(sorted(set(self.security_options)))
            or not any("seccomp" in item.lower() for item in self.security_options)
            or not any("apparmor" in item.lower() for item in self.security_options)
            or any("rootless" in item.lower() for item in self.security_options)
            or self.docker_root_mode & 0o022
        ):
            raise ValueError("Docker security projection is not rootful, canonical, and confined")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


def qualification_authority_bundle_sha256(
    request: QualificationAuthorityCommissioningRequestV1,
    receipt: QualificationAuthorityCommissioningReceiptV1,
) -> str:
    """Validate commissioning and return its externally reviewed authority-bundle pin.

    The v1 deployment spec deliberately carries an opaque external review digest.  Its config and
    key inodes do not exist until after bootstrap, so folding those live identities back into the
    original spec would be a self-referential deployment hash.  The concrete observer instead
    freshly verifies every commissioned artifact before it reports this frozen pin.
    """

    verify_qualification_authority_commissioning_receipt(request, receipt)
    return request.installation_request.deployment_spec.authority_bundle_sha256


class QualificationLinuxObserverConfigV1(ExecutionModel):
    """Closed independent observer inputs; database URL and private bytes stay external."""

    schema_name: Literal["aletheia.qualification_linux_observer_config"] = (
        "aletheia.qualification_linux_observer_config"
    )
    schema_version: Literal[1] = 1
    config_id: str | None = Field(default=None, pattern=r"^qoc_[0-9a-f]{32}$")
    commissioning_request: QualificationAuthorityCommissioningRequestV1
    commissioning_receipt: QualificationAuthorityCommissioningReceiptV1
    installation_receipt: QualificationInstallationReceiptV1
    observer_pin: QualificationDeploymentObserverPin
    observer_private_key: QualificationObserverPrivateKeyPinV1
    timedatectl_executable: QualificationExpectedRootExecutable
    docker_security_projection: QualificationDockerSecurityProjectionV1
    admin_database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _config_is_closed(self) -> "QualificationLinuxObserverConfigV1":
        request = self.commissioning_request
        installation = request.installation_request
        try:
            verify_qualification_authority_commissioning_receipt(
                request,
                self.commissioning_receipt,
            )
            verify_qualification_installation_receipt(
                installation,
                self.installation_receipt,
            )
        except Exception as exc:
            raise ValueError("observer prerequisite receipt chain is invalid") from exc
        if (
            self.installation_receipt.completed_at < self.commissioning_receipt.completed_at
            or self.prepared_at < self.installation_receipt.completed_at
            or self.prepared_at.utcoffset() != timedelta(0)
            or self.admin_database_url_sha256 != request.admin_database_url_sha256
            or self.observer_pin.principal_id
            in {
                request.node_config.node_authority.manifest.node_id,
                request.node_config.allocator_principal_id,
                request.node_config.input_materializer_principal_id,
                request.quota_config.quota_deployment.provisioner_principal_id,
            }
            or self.observer_private_key.key_id != self.observer_pin.key_id
            or not self.observer_pin.active_at(self.prepared_at)
        ):
            raise ValueError("observer identity, chronology, or derived deployment pins differ")
        expected_id = f"qoc_{self.identity_sha256[:32]}"
        if self.config_id is not None and self.config_id != expected_id:
            raise ValueError("observer config id is not derived")
        object.__setattr__(self, "config_id", expected_id)
        return self

    @property
    def identity_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"config_id"}))

    @property
    def config_sha256(self) -> str:
        return canonical_sha256(self)


def _read_exact_file(
    path: Path,
    *,
    expected_sha256: str | None = None,
    maximum_bytes: int = _MAX_FILE_BYTES,
) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise QualificationObserverError(f"observer file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise QualificationObserverError(f"observer file custody is unsafe: {path}")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise QualificationObserverError(f"observer file is oversized: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
        )

    digest = hashlib.sha256(payload).hexdigest()
    if (
        identity(before) != identity(after)
        or before.st_nlink != 1
        or (expected_sha256 is not None and digest != expected_sha256)
    ):
        raise QualificationObserverError(f"observer file changed or differs: {path}")
    return bytes(payload), after


def _hash_reviewed_tree_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_length: int,
    expected_owner_uid: int,
    expected_owner_gid: int,
    expected_mode: int,
) -> tuple[str, os.stat_result]:
    """Stream-hash one exhaustive tree entry under exact immutable custody.

    Control/config files remain subject to ``_MAX_FILE_BYTES``.  A reviewed Python environment may
    legitimately contain larger native objects (for example ``libpython``), so tree entries use
    their individually frozen byte lengths as the bound and are never accumulated in memory.
    """

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise QualificationObserverError(f"reviewed tree file is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_owner_uid
            or before.st_gid != expected_owner_gid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size != expected_byte_length
        ):
            raise QualificationObserverError(f"reviewed tree file custody differs: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > expected_byte_length:
                raise QualificationObserverError(f"reviewed tree file grew while read: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    resolved = digest.hexdigest()
    if (
        identity(before) != identity(after)
        or total != expected_byte_length
        or resolved != expected_sha256
    ):
        raise QualificationObserverError(f"reviewed tree file changed or differs: {path}")
    return resolved, after


def _pinned_root_file(path: str, *, expected_sha256: str | None = None) -> PinnedRootFile:
    candidate = Path(path)
    payload, metadata = _read_exact_file(candidate, expected_sha256=expected_sha256)
    try:
        parent_sha256 = host_parent_chain_sha256(candidate)
    except (OSError, ValueError) as exc:
        raise QualificationObserverError(f"observer file parent custody differs: {path}") from exc
    return PinnedRootFile(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        parent_chain_sha256=parent_sha256,
    )


def _pinned_root_executable(
    expected: QualificationExpectedRootExecutable,
) -> PinnedRootExecutable:
    digest, metadata, parent_chain_sha256 = _stream_exact_executable(
        Path(expected.path),
        expected_sha256=expected.reviewed_sha256,
        expected_owner_uid=expected.expected_owner_uid,
        expected_owner_gid=expected.expected_owner_gid,
        expected_mode=expected.expected_mode,
    )
    return PinnedRootExecutable(
        path=expected.path,
        sha256=digest,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        parent_chain_sha256=parent_chain_sha256,
    )


def _stream_exact_executable(
    path: Path,
    *,
    expected_sha256: str,
    expected_owner_uid: int,
    expected_owner_gid: int,
    expected_mode: int,
    maximum_bytes: int = _MAX_EXECUTABLE_BYTES,
) -> tuple[str, os.stat_result, str]:
    """Hash one large executable without weakening the bounded control-file reader."""

    try:
        parent_before = host_parent_chain_sha256(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, ValueError) as exc:
        raise QualificationObserverError(f"observer executable is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum_bytes
            or before.st_uid != expected_owner_uid
            or before.st_gid != expected_owner_gid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not before.st_mode & 0o111
        ):
            raise QualificationObserverError(f"observer executable custody is unsafe: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise QualificationObserverError(f"observer executable is oversized: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
        parent_after = host_parent_chain_sha256(path)
    except (OSError, ValueError) as exc:
        raise QualificationObserverError(f"observer executable changed: {path}") from exc

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    resolved = digest.hexdigest()
    if (
        total != before.st_size
        or identity(before) != identity(after)
        or identity(after) != identity(path_after)
        or parent_before != parent_after
        or resolved != expected_sha256
    ):
        raise QualificationObserverError(f"observer executable changed or differs: {path}")
    return resolved, after, parent_after


def _run_pinned(
    expected: QualificationExpectedRootExecutable,
    arguments: Sequence[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    pin = _pinned_root_executable(expected)
    descriptor = os.open(
        pin.path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        completed = subprocess.run(
            (pin.path, *arguments),
            executable=f"/proc/self/fd/{descriptor}",
            pass_fds=(descriptor,),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise QualificationObserverError(f"pinned command failed: {pin.path}") from exc
    finally:
        os.close(descriptor)
    if (
        completed.returncode != 0
        or len(completed.stdout.encode()) > _MAX_COMMAND_BYTES
        or len(completed.stderr.encode()) > _MAX_COMMAND_BYTES
    ):
        raise QualificationObserverError(f"pinned command returned an invalid result: {pin.path}")
    return completed


def _mountinfo(path: str, *, pid: int | Literal["self"] = "self") -> Mapping[str, object]:
    marker = str(Path(path))
    try:
        lines = Path(f"/proc/{pid}/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QualificationObserverError("Linux mountinfo is unavailable") from exc
    matches: list[dict[str, object]] = []
    for line in lines:
        left, separator, right = line.partition(" - ")
        if not separator:
            raise QualificationObserverError("Linux mountinfo is malformed")
        fields = left.split()
        right_fields = right.split()
        if len(fields) < 6 or len(right_fields) < 3:
            raise QualificationObserverError("Linux mountinfo entry is malformed")
        mountpoint = fields[4].replace("\\040", " ").replace("\\011", "\t")
        if mountpoint != marker:
            continue
        major, colon, minor = fields[2].partition(":")
        if colon != ":":
            raise QualificationObserverError("Linux mountinfo device is malformed")
        matches.append(
            {
                "mount_id": int(fields[0]),
                "parent_id": int(fields[1]),
                "major": int(major),
                "minor": int(minor),
                "mountpoint": mountpoint,
                "options": tuple(sorted(set(fields[5].split(",")))),
                "optional": tuple(fields[6:]),
                "filesystem_type": right_fields[0],
                "source": right_fields[1],
                "super_options": tuple(sorted(set(right_fields[2].split(",")))),
            }
        )
    if len(matches) != 1:
        raise QualificationObserverError(f"path is not one exact mount: {path}")
    return matches[0]


def _read_process_status(pid: int) -> dict[str, str]:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise QualificationObserverError(f"service process {pid} is unavailable") from exc
    result: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            if key in result:
                raise QualificationObserverError("process status contains duplicate fields")
            result[key] = value.strip()
    return result


def _process_nul_values(pid: int, name: Literal["cmdline", "environ"]) -> tuple[str, ...]:
    try:
        payload = Path(f"/proc/{pid}/{name}").read_bytes()
        values = payload.split(b"\x00")
        if not values or values[-1] != b"":
            raise QualificationObserverError(f"service process {name} framing is incomplete")
        return tuple(value.decode("utf-8", errors="strict") for value in values[:-1])
    except (OSError, UnicodeError) as exc:
        raise QualificationObserverError(f"service process {name} is unavailable") from exc


def linux_capability_names_from_hex(value: str) -> tuple[str, ...]:
    """Decode the kernel capability bitmap used by signed live-process observations."""

    try:
        bits = int(value, 16)
    except ValueError as exc:
        raise QualificationObserverError("service capability bitmap is invalid") from exc
    if bits >> len(_LINUX_CAPABILITY_NAMES):
        raise QualificationObserverError("service capability bitmap has unknown bits")
    return tuple(
        sorted(name for number, name in enumerate(_LINUX_CAPABILITY_NAMES) if bits & (1 << number))
    )


class LinuxQualificationDeploymentObserver:
    """Concrete root/Linux observer backed by pinned commands, /proc, files, and PostgreSQL."""

    def __init__(self, config: QualificationLinuxObserverConfigV1) -> None:
        self.config = QualificationLinuxObserverConfigV1.model_validate(
            config.model_dump(mode="python")
        )
        self.request = self.config.commissioning_request
        self.spec = self.request.installation_request.deployment_spec
        self._systemctl = self.request.installation_request.systemctl_executable
        self._authority_host = LinuxQualificationAuthorityCommissioningHost(self.request)

    def _require_environment(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0:
            raise QualificationObserverError("deployment observation requires Linux root:root")
        try:
            pid_one = Path("/proc/1/comm").read_text(encoding="ascii").strip()
            cgroup_type = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise QualificationObserverError("Linux host identity is unavailable") from exc
        if (
            pid_one != "systemd"
            or not cgroup_type
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                boot_id,
            )
            is None
        ):
            raise QualificationObserverError("host is not real systemd/cgroup-v2 Linux")

    def _verify_live_artifacts(self) -> None:
        commissioning_plan = build_qualification_authority_commissioning_plan(self.request)
        for artifact, completion in zip(
            commissioning_plan.artifacts,
            self.config.commissioning_receipt.artifact_completions,
            strict=True,
        ):
            if self._authority_host.observe_artifact(artifact) != completion.installed_file:
                raise QualificationObserverError(
                    f"commissioned artifact changed after receipt: {artifact.artifact_key}"
                )
        installation_plan = verify_qualification_installation_receipt(
            self.request.installation_request,
            self.config.installation_receipt,
        )
        for artifact, completion in zip(
            installation_plan.artifacts,
            self.config.installation_receipt.artifact_completions,
            strict=True,
        ):
            observed = _pinned_root_file(
                artifact.target_path,
                expected_sha256=artifact.content_sha256,
            )
            installed = completion.installed_file
            if (
                observed.path != installed.path
                or observed.sha256 != installed.content_sha256
                or observed.device != installed.device
                or observed.inode != installed.inode
                or observed.owner_uid != installed.owner_uid
                or observed.owner_gid != installed.owner_gid
                or observed.mode != installed.mode
            ):
                raise QualificationObserverError(
                    f"installed artifact changed after receipt: {artifact.target_path}"
                )

    def _systemd_show(self, unit_name: str) -> dict[str, str]:
        properties = (
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "FragmentPath",
            "DropInPaths",
            "NeedDaemonReload",
            "MainPID",
            "User",
            "Group",
            "SupplementaryGroups",
            "NoNewPrivileges",
            "PrivateMounts",
            "WorkingDirectory",
        )
        completed = _run_pinned(
            self._systemctl,
            ("show", unit_name, *(f"--property={item}" for item in properties)),
        )
        result: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in result:
                raise QualificationObserverError("systemd show output is not canonical")
            result[key] = value
        if set(result) != set(properties):
            raise QualificationObserverError("systemd show omitted an effective property")
        return result

    def _service_processes(
        self,
    ) -> tuple[tuple[QualificationSystemdServiceIdentityObservation, ...], dict[str, int]]:
        spec = self.spec
        expected = {
            item.unit_name: item for item in expected_qualification_systemd_service_identities(spec)
        }
        pids: dict[str, int] = {}
        observed_identities: list[QualificationSystemdServiceIdentityObservation] = []
        for unit_name, identity in expected.items():
            show = self._systemd_show(unit_name)
            fragment = _pinned_root_file(
                show["FragmentPath"],
                expected_sha256=identity.loaded_fragment_sha256,
            )
            expected_user = "root" if identity.effective_uid == 0 else str(identity.effective_uid)
            expected_group = "root" if identity.effective_gid == 0 else str(identity.effective_gid)
            supplied_groups = tuple(show["SupplementaryGroups"].split())
            groups_shape_is_valid = (
                not supplied_groups
                if not identity.supplementary_gids
                else len(supplied_groups) == len(identity.supplementary_gids)
            )
            if (
                show["LoadState"] != "loaded"
                or show["ActiveState"] != "active"
                or show["UnitFileState"] != "enabled"
                or show["FragmentPath"] != identity.fragment_path
                or fragment.path != identity.fragment_path
                or show["DropInPaths"]
                or show["NeedDaemonReload"] != "no"
                or show["User"] not in {expected_user, str(identity.effective_uid)}
                or show["Group"] not in {expected_group, str(identity.effective_gid)}
                or not groups_shape_is_valid
                or show["NoNewPrivileges"] != "yes"
                or show["PrivateMounts"] != ("yes" if identity.private_mounts else "no")
                or show["WorkingDirectory"] != identity.working_directory
            ):
                raise QualificationObserverError(f"systemd effective identity differs: {unit_name}")
            try:
                pid = int(show["MainPID"])
            except ValueError as exc:
                raise QualificationObserverError("systemd MainPID is not numeric") from exc
            if unit_name == spec.workspace_unit_name:
                if show["SubState"] != "exited" or pid != 0:
                    raise QualificationObserverError("workspace one-shot is not active/exited")
                observed_identities.append(identity)
                continue
            if pid <= 1 or show["SubState"] != "running":
                raise QualificationObserverError(f"systemd service is not running: {unit_name}")
            status = _read_process_status(pid)
            uid_values = tuple(int(value) for value in status.get("Uid", "").split())
            gid_values = tuple(int(value) for value in status.get("Gid", "").split())
            groups = tuple(sorted(int(value) for value in status.get("Groups", "").split()))
            expected_capabilities = identity.effective_capabilities
            capabilities = tuple(
                linux_capability_names_from_hex(status.get(field, ""))
                for field in ("CapEff", "CapPrm", "CapBnd", "CapAmb")
            )
            cmdline = _process_nul_values(pid, "cmdline")
            environment_values = _process_nul_values(pid, "environ")
            environment: dict[str, str] = {}
            for assignment in environment_values:
                name, separator, value = assignment.partition("=")
                if not separator or not name or name in environment:
                    raise QualificationObserverError(
                        f"live process environment is ambiguous: {unit_name}"
                    )
                environment[name] = value
            expected_environment = {
                name: value
                for assignment in identity.effective_environment
                for name, separator, value in (assignment.partition("="),)
                if separator
            }
            try:
                executable = os.stat(f"/proc/{pid}/exe")
                expected_executable = Path(identity.python_executable_path).stat()
                working_directory = os.readlink(f"/proc/{pid}/cwd")
            except OSError as exc:
                raise QualificationObserverError(
                    f"live process executable or cwd is unavailable: {unit_name}"
                ) from exc
            if (
                uid_values != (identity.effective_uid,) * 4
                or gid_values != (identity.effective_gid,) * 4
                or groups != tuple(sorted({identity.effective_gid, *identity.supplementary_gids}))
                or status.get("NoNewPrivs") != "1"
                or any(value != expected_capabilities for value in capabilities)
                or cmdline != identity.exec_start_argvs[0]
                or any(
                    environment.get(name) != value for name, value in expected_environment.items()
                )
                or any(name in environment for name in identity.unset_environment_names)
                or (executable.st_dev, executable.st_ino)
                != (expected_executable.st_dev, expected_executable.st_ino)
                or working_directory != identity.working_directory
            ):
                raise QualificationObserverError(f"live process identity differs: {unit_name}")
            pids[unit_name] = pid
            observed_identities.append(identity)
        return (
            tuple(sorted(observed_identities, key=lambda item: item.unit_name)),
            pids,
        )

    def _docker_projection(self) -> tuple[QualificationDockerSecurityProjectionV1, int]:
        policy = self.request.node_config.oci_policy
        docker = QualificationExpectedRootExecutable(
            path=policy.runtime_binary_path,
            reviewed_sha256=policy.runtime_binary_sha256,
            expected_owner_uid=policy.runtime_binary_owner_uid,
            expected_owner_gid=policy.runtime_binary_owner_gid,
            expected_mode=policy.runtime_binary_mode,
        )
        completed = _run_pinned(
            docker,
            ("--host", policy.engine_endpoint, "info", "--format", "{{json .}}"),
            timeout=60,
        )
        try:
            value = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise QualificationObserverError("Docker info is not one JSON object") from exc
        if not isinstance(value, dict):
            raise QualificationObserverError("Docker info is not one JSON object")
        docker_show = self._systemd_show("docker.service")
        try:
            daemon_pid = int(docker_show["MainPID"])
            status = _read_process_status(daemon_pid)
            root = Path(str(value["DockerRootDir"]))
            root_stat = root.lstat()
            security_options = tuple(sorted(set(str(item) for item in value["SecurityOptions"])))
            cgroup_version = int(value["CgroupVersion"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise QualificationObserverError("Docker security projection is incomplete") from exc
        uid = tuple(int(item) for item in status.get("Uid", "").split())
        gid = tuple(int(item) for item in status.get("Gid", "").split())
        projection = QualificationDockerSecurityProjectionV1(
            server_version=str(value["ServerVersion"]),
            kernel_version=str(value["KernelVersion"]),
            operating_system=str(value["OperatingSystem"]),
            architecture=str(value["Architecture"]),
            cgroup_driver=str(value["CgroupDriver"]),
            cgroup_version=cgroup_version,
            storage_driver=str(value["Driver"]),
            docker_root_dir=str(root),
            docker_root_device=root_stat.st_dev,
            docker_root_inode=root_stat.st_ino,
            docker_root_owner_uid=root_stat.st_uid,
            docker_root_owner_gid=root_stat.st_gid,
            docker_root_mode=stat.S_IMODE(root_stat.st_mode),
            docker_root_parent_chain_sha256=host_parent_chain_sha256(root),
            security_options=security_options,
        )
        if uid != (0, 0, 0, 0) or gid != (0, 0, 0, 0):
            raise QualificationObserverError("Docker daemon is not root:root")
        return projection, daemon_pid

    def _observed_tree(
        self,
        reviewed: QualificationReviewedCodeTree,
    ) -> QualificationObservedRootCodeTree:
        root = Path(reviewed.root_path)
        try:
            metadata = root.lstat()
            parent_sha256 = host_parent_chain_sha256(root)
        except (OSError, ValueError) as exc:
            raise QualificationObserverError("reviewed tree root is unavailable") from exc
        if (
            root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != reviewed.expected_root_owner_uid
            or metadata.st_gid != reviewed.expected_root_owner_gid
            or stat.S_IMODE(metadata.st_mode) != reviewed.expected_root_mode
        ):
            raise QualificationObserverError("reviewed tree root custody differs")

        expected_directories = {item.relative_path: item for item in reviewed.directories}
        expected_files = {item.relative_path: item for item in reviewed.entries}
        directories: list[tuple[str, int]] = []
        files: list[tuple[str, str, int, int]] = []
        for current, names, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            names.sort()
            filenames.sort()
            for name in names:
                path = current_path / name
                relative = str(path.relative_to(root))
                expected = expected_directories.get(relative)
                try:
                    observed = path.lstat()
                except OSError as exc:
                    raise QualificationObserverError(
                        "reviewed tree directory is unavailable"
                    ) from exc
                if (
                    expected is None
                    or path.is_symlink()
                    or not stat.S_ISDIR(observed.st_mode)
                    or observed.st_uid != expected.expected_owner_uid
                    or observed.st_gid != expected.expected_owner_gid
                    or stat.S_IMODE(observed.st_mode) != expected.expected_mode
                ):
                    raise QualificationObserverError("reviewed tree directory custody differs")
                directories.append((relative, stat.S_IMODE(observed.st_mode)))
            for name in filenames:
                path = current_path / name
                relative = str(path.relative_to(root))
                expected = expected_files.get(relative)
                if expected is None:
                    raise QualificationObserverError("reviewed tree contains an unknown file")
                digest, observed = _hash_reviewed_tree_file(
                    path,
                    expected_sha256=expected.reviewed_sha256,
                    expected_byte_length=expected.byte_length,
                    expected_owner_uid=expected.expected_owner_uid,
                    expected_owner_gid=expected.expected_owner_gid,
                    expected_mode=expected.expected_mode,
                )
                files.append(
                    (
                        relative,
                        digest,
                        observed.st_size,
                        stat.S_IMODE(observed.st_mode),
                    )
                )
        expected_directory_projection = tuple(
            (item.relative_path, item.expected_mode) for item in reviewed.directories
        )
        observed_directories = tuple(sorted(directories))
        expected_file_projection = tuple(
            (
                item.relative_path,
                item.reviewed_sha256,
                item.byte_length,
                item.expected_mode,
            )
            for item in reviewed.entries
        )
        if (
            observed_directories != expected_directory_projection
            or tuple(sorted(files)) != expected_file_projection
        ):
            raise QualificationObserverError("live reviewed tree differs from exhaustive manifest")
        return QualificationObservedRootCodeTree(
            path=reviewed.root_path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            mode=stat.S_IMODE(metadata.st_mode),
            parent_chain_sha256=parent_sha256,
            tree_manifest_sha256=reviewed.manifest_sha256,
            directory_count=len(directories),
            regular_file_count=len(files),
            total_regular_file_bytes=sum(item[2] for item in files),
        )

    def _custody_roots(self) -> tuple[QualificationObservedCustodyRoot, ...]:
        spec = self.spec
        policies = {
            "artifact_store": (spec.artifact_store_root, spec.node_uid, spec.node_gid, 0o700),
            "authority_registry": (spec.authority_registry_root, 0, 0, 0o555),
            "input_materialization_journal": (
                spec.input_materialization_journal_root,
                spec.node_uid,
                spec.node_gid,
                0o700,
            ),
            "node_state": (spec.node_state_root, spec.node_uid, spec.node_gid, 0o700),
            "outbox_spool": (spec.outbox_spool_root, spec.outbox_uid, spec.outbox_gid, 0o700),
            "workspace_source": (spec.workspace_source_root, 0, spec.node_gid, 0o1730),
        }
        result: list[QualificationObservedCustodyRoot] = []
        for purpose, (path_value, uid, gid, mode) in policies.items():
            path = Path(path_value)
            try:
                observed = path.lstat()
                parent_sha256 = host_parent_chain_sha256(path)
            except (OSError, ValueError) as exc:
                raise QualificationObserverError(f"custody root is unavailable: {purpose}") from exc
            if (
                path.is_symlink()
                or not stat.S_ISDIR(observed.st_mode)
                or (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode))
                != (uid, gid, mode)
            ):
                raise QualificationObserverError(f"custody root differs: {purpose}")
            result.append(
                QualificationObservedCustodyRoot(
                    purpose=purpose,
                    path=path_value,
                    device=observed.st_dev,
                    inode=observed.st_ino,
                    owner_uid=observed.st_uid,
                    owner_gid=observed.st_gid,
                    mode=stat.S_IMODE(observed.st_mode),
                    parent_chain_sha256=parent_sha256,
                )
            )
        return tuple(result)

    def _external_native_paths(self, pids: Mapping[str, int]) -> tuple[str, ...]:
        roots = (Path(self.spec.code_root), Path(self.spec.reviewed_python_environment.root_path))
        external: set[str] = set()
        for pid in pids.values():
            try:
                lines = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise QualificationObserverError("service native map is unavailable") from exc
            for line in lines:
                fields = line.split(maxsplit=5)
                if len(fields) != 6 or not fields[5].startswith("/"):
                    continue
                value = fields[5].removesuffix(" (deleted)")
                candidate = Path(value)
                if not any(root == candidate or root in candidate.parents for root in roots):
                    external.add(value)
        return tuple(sorted(external))

    def _native_closures(self) -> tuple[ObservedNativeDependencyClosure, ...]:
        result: list[ObservedNativeDependencyClosure] = []
        for reviewed in self.spec.reviewed_privileged_tool_native_closures:
            executable = _pinned_root_executable(reviewed.executable)
            interpreter = _pinned_root_file(
                reviewed.elf_interpreter.path,
                expected_sha256=reviewed.elf_interpreter.reviewed_sha256,
            )
            dependencies = tuple(
                ObservedNativeDependency(
                    soname=item.soname,
                    file=_pinned_root_file(
                        item.file.path,
                        expected_sha256=item.file.reviewed_sha256,
                    ),
                    needed_sonames=item.needed_sonames,
                )
                for item in reviewed.dependencies
            )
            result.append(
                ObservedNativeDependencyClosure(
                    executable=executable,
                    elf_interpreter=interpreter,
                    executable_needed_sonames=reviewed.executable_needed_sonames,
                    dependencies=dependencies,
                    exhaustive=True,
                    external_native_dependency_paths=(),
                )
            )
        return tuple(result)

    def _postgresql_projection(self) -> dict[str, object]:
        intent = qualification_postgresql_commissioning_intent(self.request)
        host_before = datetime.now(timezone.utc)
        live = self._authority_host.observe_postgresql_deployment_projection(intent)
        host_after = datetime.now(timezone.utc)
        state = live.commissioned_state
        catalog = live.execution_catalog
        clock_healthy = (
            host_before - timedelta(seconds=1)
            <= live.database_time
            <= host_after + timedelta(seconds=1)
        )
        spec = self.spec
        owner_state = next(
            item for item in state.roles if item.role_name == spec.postgresql_owner_role
        )
        roles = tuple(
            PostgreSQLRestrictedRoleObservation(
                role_name=item.role_name,
                can_login=item.can_login,
                is_superuser=item.superuser,
                can_create_database=item.create_database,
                can_create_role=item.create_role,
                inherits_roles=item.inherit,
                can_replicate=item.replication,
                bypasses_row_security=item.bypass_rls,
                member_of_owner_role=(spec.postgresql_owner_role in item.direct_memberships),
                owns_execution_objects=any(
                    owner.owner_role == item.role_name for owner in catalog.object_owners
                ),
                can_create_in_schema=False,
                can_create_temporary_tables=False,
                can_delete_execution_rows=False,
                can_truncate_execution_tables=False,
                can_execute_ddl=False,
                can_mutate_triggers_or_functions=False,
                direct_role_memberships=item.direct_memberships,
                transitive_role_memberships=item.direct_memberships,
                role_members=item.direct_members,
                dangerous_builtin_role_memberships=tuple(
                    sorted(set(item.direct_memberships) & set(POSTGRESQL_DANGEROUS_BUILTIN_ROLES))
                ),
                table_privileges_sha256=item.target_privileges_sha256,
            )
            for item in state.roles
            if item.role_name in {spec.postgresql_allocator_role, spec.postgresql_outbox_role}
        )
        expected_routine_keys = {
            (item.routine_kind, item.routine_name, item.identity_argument_types)
            for item in spec.expected_postgresql_routines
        }
        unexpected_routines = tuple(
            item
            for item in catalog.routines
            if (item.routine_kind, item.routine_name, item.identity_argument_types)
            not in expected_routine_keys
        )
        return {
            "schema_revision": state.schema_revision,
            "postgresql_server_identity_sha256": state.server_identity.identity_sha256,
            "postgresql_acl_sha256": state.acl_sha256,
            "postgresql_clock_healthy": clock_healthy,
            "postgresql_roles": roles,
            "postgresql_owner_role_inherits": owner_state.inherit,
            "postgresql_owner_direct_role_memberships": owner_state.direct_memberships,
            "postgresql_owner_transitive_role_memberships": owner_state.direct_memberships,
            "postgresql_owner_dangerous_builtin_role_memberships": tuple(
                sorted(
                    set(owner_state.direct_memberships) & set(POSTGRESQL_DANGEROUS_BUILTIN_ROLES)
                )
            ),
            "postgresql_owner_role_members": owner_state.direct_members,
            "postgresql_unexpected_database_grants": (),
            "postgresql_unexpected_schema_grants": (),
            "postgresql_unexpected_table_grants": (),
            "postgresql_unexpected_column_grants": (),
            "postgresql_unexpected_sequence_grants": (),
            "postgresql_unexpected_routine_execute_grants": (),
            "postgresql_unexpected_grant_options": (),
            "postgresql_unexpected_execution_routines": unexpected_routines,
            "postgresql_routines": catalog.routines,
            "postgresql_triggers": catalog.triggers,
            "postgresql_sequences": catalog.sequences,
            "postgresql_non_execution_public_routine_owners": (
                live.non_execution_public_routine_owners
            ),
            "postgresql_non_execution_public_routine_owner_projection_exhaustive": True,
            "postgresql_execution_object_owners": catalog.object_owners,
        }

    def _observe_live(self) -> QualificationLinuxDeploymentObservation:
        self._require_environment()
        started_at = datetime.now(timezone.utc)
        self._verify_live_artifacts()
        systemd_identities, pids = self._service_processes()
        docker, docker_pid = self._docker_projection()
        if docker != self.config.docker_security_projection:
            raise QualificationObserverError("live Docker security projection drifted")
        spec = self.spec
        timedate = _run_pinned(
            self.config.timedatectl_executable,
            ("show", "--property=NTPSynchronized", "--value"),
        ).stdout.strip()
        if timedate != "yes":
            raise QualificationObserverError("host clock is not synchronized")
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            pid_one_namespace = os.readlink("/proc/1/ns/mnt")
            quota_namespace = os.readlink(f"/proc/{pids[spec.quota_unit_name]}/ns/mnt")
            node_namespace = os.readlink(f"/proc/{pids[spec.node_unit_name]}/ns/mnt")
            docker_namespace = os.readlink(f"/proc/{docker_pid}/ns/mnt")
        except (KeyError, OSError, UnicodeError) as exc:
            raise QualificationObserverError("mount namespace identities are unavailable") from exc
        workspace_path = Path(spec.output_workspace_root)
        workspace_stat = workspace_path.lstat()
        workspace_mount = _mountinfo(spec.output_workspace_root)
        workspace_pin = PinnedOutputWorkspaceRoot(
            path=spec.output_workspace_root,
            device=workspace_stat.st_dev,
            inode=workspace_stat.st_ino,
            mount_id=int(workspace_mount["mount_id"]),
            owner_gid=workspace_stat.st_gid,
            parent_chain_sha256=host_parent_chain_sha256(workspace_path),
        )
        shared_visible = any(
            str(item).startswith("shared:") for item in workspace_mount["optional"]
        ) and all(
            _mountinfo(spec.output_workspace_root, pid=pid)["mount_id"]
            == workspace_mount["mount_id"]
            for pid in (pids[spec.quota_unit_name], pids[spec.node_unit_name], docker_pid)
        )
        node_config = self.request.node_config
        verifier = ImmutableOCIImageLaunchGateVerifier(
            policy=node_config.oci_policy,
            runtime_control_authority=node_config.runtime_control_authority_pin,
            image_layout=node_config.image_layout,
        )
        expected_gate_evidence = verifier._expected_evidence_sha256()  # noqa: SLF001
        verifier.verify_immutable_launch_gate(
            image_reference=node_config.oci_policy.image_reference,
            image_manifest_sha256=spec.image_manifest_sha256,
            image_config_sha256=spec.image_config_sha256,
            launch_gate_path=node_config.oci_policy.launch_gate_path,
            launch_gate_executable_sha256=spec.launch_gate_executable_sha256,
            launch_gate_protocol_sha256=spec.launch_gate_protocol_sha256,
            expected_evidence_sha256=expected_gate_evidence,
        )
        entrypoints = tuple(
            sorted(
                (
                    _pinned_root_file(pin.path, expected_sha256=pin.reviewed_sha256)
                    for pin in (
                        spec.expected_workspace_runner,
                        spec.expected_quota_runner,
                        spec.expected_watchdog_runner,
                        spec.expected_node_runner,
                        spec.expected_outbox_runner,
                    )
                ),
                key=lambda item: item.path,
            )
        )
        units = render_systemd_units(spec)
        unit_files = tuple(
            sorted(
                (
                    _pinned_root_file(item.path, expected_sha256=item.content_sha256)
                    for item in units
                ),
                key=lambda item: item.path,
            )
        )
        service_modules = tuple(
            sorted(
                {
                    pin.path: _pinned_root_file(
                        pin.path,
                        expected_sha256=pin.reviewed_sha256,
                    )
                    for pin in (
                        spec.expected_quota_service_module,
                        spec.expected_watchdog_service_module,
                    )
                }.values(),
                key=lambda item: item.path,
            )
        )
        values: dict[str, object] = {
            "deployment_id": spec.deployment_id,
            "node_id": spec.node_id,
            "node_manifest_sha256": spec.node_manifest_sha256,
            "platform": "linux",
            "cpu_architecture": platform.machine().lower(),
            "oci_platform": node_config.oci_policy.oci_platform,
            "kernel_release": platform.release(),
            "boot_id": boot_id,
            "pid_one_comm": "systemd",
            "cgroup_version": 2,
            "docker_cgroup_driver": docker.cgroup_driver,
            # The deployment spec carries the independently reviewed, opaque Docker policy pin.
            # The typed live projection is separately frozen in this observer config and freshly
            # compared above; replacing the external pin with that post-installation digest would
            # make old deployment requests unverifiable and introduce a self-referential input.
            "docker_security_projection_sha256": spec.docker_security_projection_sha256,
            "pid_one_mount_namespace": pid_one_namespace,
            "quota_mount_namespace": quota_namespace,
            "node_mount_namespace": node_namespace,
            "docker_mount_namespace": docker_namespace,
            "shared_output_mount_visible": shared_visible,
            "host_clock_synchronized": True,
            "custody_roots": self._custody_roots(),
            "python_executable": _pinned_root_executable(spec.expected_python_executable),
            "python_environment_root": self._observed_tree(spec.reviewed_python_environment),
            "python_import_paths": spec.expected_python_import_paths,
            "python_external_loaded_native_object_paths": self._external_native_paths(pids),
            "entrypoint_files": entrypoints,
            "code_root": self._observed_tree(spec.reviewed_code_tree),
            "deployment_manifest_file": _pinned_root_file(
                spec.deployment_manifest_path,
                expected_sha256=spec.deployment_manifest_sha256,
            ),
            "systemd_unit_files": unit_files,
            "service_module_files": service_modules,
            "privileged_tool_native_closures": self._native_closures(),
            "systemd_service_identities": systemd_identities,
            "seccomp_profile": _pinned_root_file(
                spec.seccomp_profile_path,
                expected_sha256=spec.seccomp_profile_sha256,
            ),
            "apparmor_profile": _pinned_root_file(
                spec.apparmor_profile_path,
                expected_sha256=spec.apparmor_profile_sha256,
            ),
            "loaded_apparmor_profile_name": spec.apparmor_profile_name,
            "apparmor_profile_enforcing": self._apparmor_enforcing(spec.apparmor_profile_name),
            "agent_implementation_sha256": spec.agent_implementation_sha256,
            "authority_bundle_sha256": qualification_authority_bundle_sha256(
                self.request,
                self.config.commissioning_receipt,
            ),
            "output_workspace_root": workspace_pin,
            "oci_image_layout": node_config.image_layout,
            "loaded_image_manifest_sha256": spec.image_manifest_sha256,
            "loaded_image_config_sha256": spec.image_config_sha256,
            "quota_deployment": self.request.quota_config.quota_deployment,
            "watchdog_deployment": self.request.watchdog_config.watchdog_deployment,
            "quota_service_systemd_verified": True,
            "watchdog_service_systemd_verified": True,
            **self._postgresql_projection(),
            "observation_started_at": started_at,
            "observed_at": datetime.now(timezone.utc),
        }
        observation = QualificationLinuxDeploymentObservation(**values)
        if observation.observed_at - observation.observation_started_at > timedelta(
            seconds=spec.maximum_observation_duration_seconds
        ):
            raise QualificationObserverError("live deployment observation exceeded its time bound")
        return observation

    @staticmethod
    def _apparmor_enforcing(profile_name: str) -> bool:
        try:
            profiles = Path("/sys/kernel/security/apparmor/profiles").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise QualificationObserverError("AppArmor profiles are unavailable") from exc
        matches = [line for line in profiles.splitlines() if line.partition(" ")[0] == profile_name]
        return len(matches) == 1 and matches[0].endswith(" (enforce)")

    def _load_private_key(self) -> bytes:
        pin = self.config.observer_private_key
        payload, metadata = _read_exact_file(
            Path(pin.path),
            expected_sha256=pin.file_sha256,
            maximum_bytes=32,
        )
        if (
            len(payload) != 32
            or metadata.st_uid != pin.owner_uid
            or metadata.st_gid != pin.owner_gid
            or stat.S_IMODE(metadata.st_mode) != pin.mode
        ):
            raise QualificationObserverError("observer private-key custody differs")
        try:
            public_hex = (
                Ed25519PrivateKey.from_private_bytes(payload)
                .public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except ValueError as exc:
            raise QualificationObserverError("observer private key is invalid") from exc
        if qualification_key_id(public_hex) != pin.key_id:
            raise QualificationObserverError("observer private key differs from external pin")
        return payload

    def observe(
        self,
        *,
        spec: QualificationDeploymentSpecV1,
        rendered_units: tuple[RenderedSystemdUnit, ...],
        postgresql_acl: bytes,
    ) -> SignedQualificationLinuxDeploymentObservation:
        """Freshly observe and sign one exact deployment without mutating it."""

        frozen_spec = QualificationDeploymentSpecV1.model_validate(spec.model_dump(mode="python"))
        if (
            frozen_spec != self.spec
            or rendered_units != render_systemd_units(self.spec)
            or postgresql_acl != render_postgresql_acl(self.spec)
        ):
            raise QualificationObserverError("observer call differs from its frozen config")
        observation = self._observe_live()
        pin = self.config.observer_pin
        expires_at = min(
            observation.observed_at + timedelta(seconds=self.spec.observation_ttl_seconds),
            pin.active_until,
        )
        unsigned = SignedQualificationLinuxDeploymentObservation(
            observation=observation,
            spec_sha256=self.spec.spec_sha256,
            rendered_systemd_units_sha256=canonical_sha256(rendered_units),
            rendered_postgresql_acl_sha256=hashlib.sha256(postgresql_acl).hexdigest(),
            observer_policy_sha256=pin.policy_sha256,
            observer_principal_id=pin.principal_id,
            observer_key_id=pin.key_id,
            signed_at=observation.observed_at,
            expires_at=expires_at,
            signature_ed25519_hex="0" * 128,
        )
        private_key = bytearray(self._load_private_key())
        try:
            signature = Ed25519PrivateKey.from_private_bytes(bytes(private_key)).sign(
                _OBSERVER_SIGNATURE_DOMAIN
                + canonical_json_bytes(
                    unsigned.model_dump(mode="json", exclude={"signature_ed25519_hex"})
                )
            )
        finally:
            for index in range(len(private_key)):
                private_key[index] = 0
        return unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()})


__all__ = [
    "LinuxQualificationDeploymentObserver",
    "QualificationDockerSecurityProjectionV1",
    "QualificationLinuxObserverConfigV1",
    "QualificationObserverError",
    "QualificationObserverPrivateKeyPinV1",
    "linux_capability_names_from_hex",
    "qualification_authority_bundle_sha256",
]
