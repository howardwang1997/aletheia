"""Deployment-pinned OCI runtime substrate for local engineering qualification.

This adapter does not reuse the legacy compute or Docker backends.  Preparation writes inert
metadata only.  Container creation is confined to :meth:`ensure_started`, is disabled unless the
host proves Linux, cgroup-v2, the pinned runtime binary, and the pinned seccomp profile, and never
turns a Darwin command/spec test into production qualification.

The runtime-control sidecar is mounted read-only into the container.  Fence rotation is a
single-lock, crash-idempotent transition: pending journal fsync, optional device-controller CAS,
exact sidecar replacement and directory fsync, then completed evidence fsync.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from aletheia.execution.node_agent import (
    PinnedEnvironmentVariable,
    PinnedLaunchSpec,
    RuntimeLaunchRequest,
)
from aletheia.execution.qualification_launch_gate import (
    QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256,
)
from aletheia.execution.runtime_contracts import (
    NodeRuntimeIdentity,
    QualificationVerificationError,
    RuntimeInspectionState,
)
from aletheia.execution.runtime_v2_contracts import (
    InputMaterializationReceipt,
    OutputQuotaProvisioningReceipt,
    PreRuntimeAbsenceReceipt,
    RuntimeControlAuthorityPin,
    RuntimeControlAuthorityVerifier,
    RuntimeFenceRebindEvidence,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimeLaunchEvidence,
    RuntimePreparation,
    validate_runtime_fence_rebind_evidence,
    verify_runtime_launch_authorization,
    verify_runtime_launch_authorization_historical,
    verify_runtime_launch_authorization_ticket_historical,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_IMAGE_REFERENCE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64})$"
)
_MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_ENGINE_OUTPUT_LIMIT = 8 * 1024 * 1024


def _durable_runtime_checkpoint(phase: str, path: Path) -> None:
    """Unit-test fault boundary for runtime directory/publication durability."""


def _strict_json_value(payload: bytes | str, *, label: str) -> object:
    """Decode one bounded, duplicate-free standard JSON value."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} repeats a JSON key")
            value[key] = item
        return value

    def reject_constant(value: str) -> object:
        raise ValueError(f"{label} contains non-standard JSON constant {value}")

    if len(payload) > _MAX_JOURNAL_BYTES:
        raise ValueError(f"{label} exceeds the bounded JSON limit")
    try:
        return json.loads(
            payload,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not UTF-8 JSON") from exc


_DOCKER_MASKED_PATHS = (
    "/proc/acpi",
    "/proc/asound",
    "/proc/interrupts",
    "/proc/kcore",
    "/proc/keys",
    "/proc/latency_stats",
    "/proc/sched_debug",
    "/proc/scsi",
    "/proc/timer_list",
    "/proc/timer_stats",
    "/sys/devices/virtual/powercap",
    "/sys/firmware",
)
_DOCKER_READONLY_PATHS = (
    "/proc/bus",
    "/proc/fs",
    "/proc/irq",
    "/proc/sys",
    "/proc/sysrq-trigger",
)


def host_parent_chain_sha256(path: Path) -> str:
    """Hash the exact non-symlink custody chain containing one deployment-pinned file."""

    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate) != str(path):
        raise ValueError("pinned host file path must be canonical and absolute")
    parents = tuple(reversed(candidate.parents))
    identities: list[dict[str, object]] = []
    for parent in parents:
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise ValueError("pinned host file parent chain is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink() or metadata.st_mode & 0o022:
            raise ValueError("pinned host file parent chain is writable or not a directory")
        identities.append(
            {
                "path": str(parent),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        )
    return canonical_sha256(
        {
            "schema": "aletheia.host_file_parent_chain_identity.v2",
            "parents": tuple(identities),
        }
    )


class OCIRuntimeError(RuntimeError):
    """Base error for the fail-closed local OCI adapter."""


class OCIPolicyRejected(OCIRuntimeError):
    """A request differs from the exact deployment policy."""


class OCIJournalError(OCIRuntimeError):
    """Runtime journal custody or crash replay is ambiguous."""


class OCIProductionCapabilityError(OCIRuntimeError):
    """The host cannot prove every capability needed for real OCI launch."""


class OCIEngineError(OCIRuntimeError):
    """The pinned OCI engine failed or returned non-exact evidence."""


class OCIRuntimeClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...

    def boottime_ns(self) -> int: ...


class SystemOCIRuntimeClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        # All boot-scoped runtime evidence shares the node's suspend-aware clock domain.
        return self.boottime_ns()

    def boottime_ns(self) -> int:
        if sys.platform != "linux" or not hasattr(time, "CLOCK_BOOTTIME"):
            raise OCIProductionCapabilityError(
                "production launch requires suspend-aware Linux CLOCK_BOOTTIME"
            )
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


class OCIDeviceFenceController(Protocol):
    """Deployment adapter for an actual device broker or revocable device cgroup."""

    def apply_initial_fence(
        self,
        *,
        preparation_sha256: str,
        devices: tuple["OCIDeviceBinding", ...],
        expected_evidence_sha256: str,
    ) -> str:
        """Idempotently enforce identity, epoch, access, and requested device memory."""
        ...

    def expected_rebind_evidence_sha256(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        devices: tuple["OCIDeviceBinding", ...],
    ) -> str: ...

    def apply_rebind(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        devices: tuple["OCIDeviceBinding", ...],
        expected_evidence_sha256: str,
    ) -> str: ...


class OCIOutputQuotaController(Protocol):
    """Deployment project-quota adapter for the host output bind."""

    def verify_enforced_quota(
        self,
        *,
        output_root: Path,
        output_quota_bytes: int,
        execution_id: str,
        infrastructure_attempt_id: str,
        runtime_id: str,
        expected_evidence_sha256: str,
    ) -> str: ...


class OCILaunchGateVerifier(Protocol):
    """Deployment rootfs verifier for the immutable in-container deadline gate.

    This substrate intentionally ships no launch-gate binary.  A production composition must
    provide a verifier that independently opens the digest-pinned image rootfs, hashes the gate
    bytes at the pinned path, and attests the exact signed-ticket/CLOCK_BOOTTIME/execve protocol.
    Merely echoing policy metadata is not a production implementation of this protocol.
    """

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
    ) -> str: ...


class OCIWatchdogCleanupQuiescence(ExecutionModel):
    """Typed root-service acknowledgement for one exact never-started cleanup.

    ``retired`` means the watchdog committed a retirement terminal.  The fired variants preserve
    the immutable fired terminal and bind the root service's separate durable quiescence record.
    """

    schema_name: Literal["aletheia.oci_watchdog_cleanup_quiescence"] = (
        "aletheia.oci_watchdog_cleanup_quiescence"
    )
    schema_version: Literal[1] = 1
    cleanup_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision: Literal["retired", "fired_absent", "fired_stopped"]
    service_quiescence_record_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _decision_union_is_closed(self) -> "OCIWatchdogCleanupQuiescence":
        if (self.decision == "retired") != (self.service_quiescence_record_sha256 is None):
            raise ValueError("watchdog cleanup quiescence decision has an impossible record hash")
        if self.decision == "fired_stopped":
            if self.container_id is None:
                raise ValueError("fired-stopped watchdog cleanup omitted its container id")
        elif self.container_id is not None:
            raise ValueError("watchdog cleanup decision carries an impossible container id")
        return self

    @property
    def quiescence_sha256(self) -> str:
        return canonical_sha256(self)


class OCIDeadlineWatchdogController(Protocol):
    """Deployment daemon that durably kills the exact sandbox by UTC/BOOTTIME deadline."""

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
    ) -> str: ...

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
    ) -> OCIWatchdogCleanupQuiescence: ...


class OCIDeviceBinding(ExecutionModel):
    """Deployment-resolved physical device and its current allocator fence."""

    schema_name: Literal["aletheia.oci_device_binding"] = "aletheia.oci_device_binding"
    schema_version: Literal[2] = 2
    device_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    hardware_uuid: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    host_device_path: str = Field(min_length=1, max_length=1024)
    container_device_path: str = Field(min_length=1, max_length=1024)
    requested_memory_bytes: int = Field(ge=0)
    fencing_epoch: int = Field(ge=1)
    device_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    access: Literal["rwm"] = "rwm"

    @model_validator(mode="after")
    def _paths_are_exact_devices(self) -> "OCIDeviceBinding":
        for value, label in (
            (self.host_device_path, "host device"),
            (self.container_device_path, "container device"),
        ):
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or value == "/dev"
                or not value.startswith("/dev/")
                or any(character in value for character in (",", "\x00", "\n", "\r"))
            ):
                raise ValueError(f"{label} path must be one canonical /dev child")
        return self


class OCIDevicePathPin(ExecutionModel):
    """Static deployment mapping; allocator supplies only identity, memory, and current fence."""

    device_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    hardware_uuid: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    host_device_path: str = Field(min_length=1, max_length=1024)
    container_device_path: str = Field(min_length=1, max_length=1024)
    device_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    access: Literal["rwm"] = "rwm"

    @model_validator(mode="after")
    def _paths_are_exact_devices(self) -> "OCIDevicePathPin":
        OCIDeviceBinding(
            device_id=self.device_id,
            hardware_uuid=self.hardware_uuid,
            host_device_path=self.host_device_path,
            container_device_path=self.container_device_path,
            requested_memory_bytes=0,
            fencing_epoch=1,
            device_policy_sha256=self.device_policy_sha256,
            access=self.access,
        )
        return self


class DeploymentPinnedOCIPolicy(ExecutionModel):
    """Closed deployment policy; no tags, host env, generic mounts, or weaker isolation knobs."""

    schema_name: Literal["aletheia.deployment_pinned_oci_policy"] = (
        "aletheia.deployment_pinned_oci_policy"
    )
    schema_version: Literal[2] = 2
    policy_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    runtime_engine: Literal["docker"] = "docker"
    low_level_runtime: str = Field(default="runc", pattern=_SYMBOLIC_ID_PATTERN)
    runtime_binary_path: str = Field(min_length=1, max_length=1024)
    runtime_binary_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_binary_device: int = Field(ge=0)
    runtime_binary_inode: int = Field(ge=1)
    runtime_binary_owner_uid: int = Field(ge=0)
    runtime_binary_owner_gid: int = Field(ge=0)
    runtime_binary_mode: int = Field(ge=0, le=0o7777)
    runtime_binary_parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_endpoint: Literal["unix:///var/run/docker.sock"] = "unix:///var/run/docker.sock"
    inspect_absence_stderr_template: Literal[
        "Error: No such object: {container_name}\n",
        "error: no such object: {container_name}\n",
    ] = "Error: No such object: {container_name}\n"
    image_reference: str = Field(min_length=1, max_length=1024)
    image_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_platform: str = Field(pattern=r"^linux/[a-z0-9_]+$")
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment_sha256: str = Field(pattern=_SHA256_PATTERN)
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_path: str = Field(min_length=1, max_length=1024)
    launch_gate_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    seccomp_profile_path: str = Field(min_length=1, max_length=1024)
    seccomp_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    seccomp_profile_device: int = Field(ge=0)
    seccomp_profile_inode: int = Field(ge=1)
    seccomp_profile_owner_uid: int = Field(ge=0)
    seccomp_profile_owner_gid: int = Field(ge=0)
    seccomp_profile_mode: int = Field(ge=0, le=0o7777)
    seccomp_profile_parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    apparmor_profile: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    workload_uid: int = Field(ge=1, le=2**31 - 1)
    workload_gid: int = Field(ge=1, le=2**31 - 1)
    input_mount_target: str = "/opt/aletheia/input"
    output_mount_target: str = "/opt/aletheia/output"
    scratch_mount_target: str = "/opt/aletheia/scratch"
    control_mount_target: str = "/run/aletheia-control"
    launch_authorization_control_path: str = "/run/aletheia-control/launch-authorization.json"
    runtime_control_path: str = "/run/aletheia-control/current.json"
    working_directory: str = "/opt/aletheia/output"
    image_environment: tuple[PinnedEnvironmentVariable, ...] = ()
    pids_limit: int = Field(default=256, ge=1, le=1_048_576)
    cpu_period_microseconds: int = Field(default=100_000, ge=1_000, le=1_000_000)
    stop_timeout_seconds: int = Field(default=10, ge=1, le=300)
    masked_paths: tuple[str, ...] = _DOCKER_MASKED_PATHS
    readonly_paths: tuple[str, ...] = _DOCKER_READONLY_PATHS
    network_mode: Literal["none"] = "none"
    pull_policy: Literal["never"] = "never"
    privileged: Literal[False] = False
    inherit_host_environment: Literal[False] = False
    allow_extra_mounts: Literal[False] = False
    read_only_root_filesystem: Literal[True] = True
    input_mount_read_only: Literal[True] = True
    control_mount_read_only: Literal[True] = True
    output_mount_only_writable: Literal[True] = True
    cap_drop_all: Literal[True] = True
    no_new_privileges: Literal[True] = True
    host_pid_namespace: Literal[False] = False
    host_ipc_namespace: Literal[False] = False
    host_network_namespace: Literal[False] = False
    docker_socket_mounted: Literal[False] = False
    database_credentials_mounted: Literal[False] = False
    artifact_store_credentials_mounted: Literal[False] = False
    node_signing_key_mounted: Literal[False] = False
    cgroup_v2_required: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _policy_is_immutable_and_closed(self) -> "DeploymentPinnedOCIPolicy":
        binary = Path(self.runtime_binary_path)
        seccomp = Path(self.seccomp_profile_path)
        launch_gate = Path(self.launch_gate_path)
        if not binary.is_absolute() or str(binary) != self.runtime_binary_path:
            raise ValueError("OCI runtime binary must be one canonical absolute path")
        if not seccomp.is_absolute() or str(seccomp) != self.seccomp_profile_path:
            raise ValueError("OCI seccomp profile must be one canonical absolute path")
        if not launch_gate.is_absolute() or str(launch_gate) != self.launch_gate_path:
            raise ValueError("OCI launch gate must be one canonical absolute image path")
        if _IMAGE_REFERENCE.fullmatch(self.image_reference) is None:
            raise ValueError("OCI image must use an immutable sha256 digest reference")
        image_digest = self.image_reference.rsplit("sha256:", 1)[1]
        expected = (
            self.image_config_sha256
            if self.image_reference.startswith("sha256:")
            else self.image_manifest_sha256
        )
        if image_digest != expected:
            raise ValueError("OCI image reference differs from its pinned digest identity")
        if self.apparmor_profile.lower() == "unconfined":
            raise ValueError("OCI AppArmor policy cannot be unconfined")
        if self.launch_gate_protocol_sha256 != QUALIFICATION_LAUNCH_GATE_PROTOCOL_SHA256:
            raise ValueError("OCI policy differs from the repository launch-gate protocol")
        targets = (
            self.input_mount_target,
            self.output_mount_target,
            self.scratch_mount_target,
            self.control_mount_target,
        )
        for value in targets + (
            self.working_directory,
            self.launch_authorization_control_path,
            self.runtime_control_path,
        ):
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or value == "/"
                or any(character in value for character in (",", "\x00", "\n", "\r"))
            ):
                raise ValueError("OCI container paths must be canonical absolute children")
        if len(set(targets)) != len(targets):
            raise ValueError("OCI fixed mounts must use distinct targets")
        target_paths = tuple(Path(item) for item in targets)
        if any(
            left in right.parents or right in left.parents
            for index, left in enumerate(target_paths)
            for right in target_paths[index + 1 :]
        ):
            raise ValueError("OCI fixed mount targets must not overlap")
        output = Path(self.output_mount_target)
        workdir = Path(self.working_directory)
        if workdir != output and output not in workdir.parents:
            raise ValueError("OCI working directory must remain inside the output mount")
        control = Path(self.control_mount_target)
        if (
            control not in Path(self.launch_authorization_control_path).parents
            or control not in Path(self.runtime_control_path).parents
            or launch_gate in target_paths
            or any(target in launch_gate.parents for target in target_paths)
        ):
            raise ValueError("OCI launch gate/control paths differ from fixed isolation paths")
        names = tuple(item.name for item in self.image_environment)
        if names != tuple(sorted(set(names))):
            raise ValueError("pinned image environment must be unique and canonical")
        if (
            self.masked_paths != _DOCKER_MASKED_PATHS
            or self.readonly_paths != _DOCKER_READONLY_PATHS
        ):
            raise ValueError("OCI system path isolation differs from the pinned closed policy")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class OCIExecutionPlan(ExecutionModel):
    """Exact internal projection of a runtime request plus immutable OCI policy binding."""

    schema_name: Literal["aletheia.oci_execution_plan"] = "aletheia.oci_execution_plan"
    schema_version: Literal[2] = 2
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_ID_PATTERN)
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    runtime_engine: Literal["docker"] = "docker"
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_spec: PinnedLaunchSpec
    launch_spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_materialization_receipt: InputMaterializationReceipt
    input_materialization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_quota_provisioning_receipt: OutputQuotaProvisioningReceipt
    output_quota_provisioning_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_resource_ids: tuple[str, ...]
    cpu_cores: int = Field(ge=1, le=65_536)
    memory_bytes: int = Field(ge=1)
    scratch_bytes: int = Field(ge=0)
    output_quota_bytes: int = Field(ge=1)
    exclusive: bool
    device_bindings: tuple[OCIDeviceBinding, ...] = ()
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    input_root: str = Field(min_length=1, max_length=4096)
    output_root: str = Field(min_length=1, max_length=4096)
    deadline: AwareDatetime
    network_mode: Literal["none"] = "none"
    privileged: Literal[False] = False
    inherit_host_environment: Literal[False] = False
    extra_mounts: tuple[()] = ()
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _plan_is_canonical_and_fenced(self) -> "OCIExecutionPlan":
        if self.launch_spec_sha256 != self.launch_spec.launch_spec_sha256:
            raise ValueError("OCI plan changed its exact launch specification")
        if (
            self.input_materialization_receipt_sha256
            != self.input_materialization_receipt.materialization_receipt_sha256
            or self.input_materialization_receipt.intent_sha256 != self.intent_sha256
            or self.input_materialization_receipt.execution_id != self.execution_id
            or self.input_materialization_receipt.infrastructure_attempt_id
            != self.infrastructure_attempt_id
        ):
            raise ValueError("OCI plan changed its typed input materialization receipt")
        quota_receipt = self.output_quota_provisioning_receipt
        if (
            self.output_quota_provisioning_receipt_sha256
            != quota_receipt.provisioning_receipt_sha256
            or quota_receipt.node_manifest_sha256 != self.node_manifest_sha256
            or quota_receipt.node_id != self.node_id
            or quota_receipt.boot_id != self.boot_id
            or quota_receipt.execution_id != self.execution_id
            or quota_receipt.infrastructure_attempt_id != self.infrastructure_attempt_id
            or quota_receipt.intent_sha256 != self.intent_sha256
            or quota_receipt.output_root != self.output_root
            or quota_receipt.output_quota_bytes != self.output_quota_bytes
        ):
            raise ValueError("OCI plan changed its typed output quota provisioning receipt")
        if self.runtime_engine != self.launch_spec.runtime_engine:
            raise ValueError("OCI plan runtime engine differs from its launch specification")
        if self.selected_resource_ids != tuple(sorted(set(self.selected_resource_ids))):
            raise ValueError("OCI selected resources must be unique and canonical")
        keys = tuple((item.device_id, item.hardware_uuid) for item in self.device_bindings)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("OCI device bindings must be unique and canonical")
        if any(item.fencing_epoch != self.fencing_epoch for item in self.device_bindings):
            raise ValueError("OCI device bindings must carry the allocator fence")
        for value, label in ((self.input_root, "input"), (self.output_root, "output")):
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or any(character in value for character in (",", "\x00", "\n", "\r"))
            ):
                raise ValueError(f"OCI {label} root must be one canonical absolute path")
        try:
            output_metadata = Path(self.output_root).lstat()
        except OSError as exc:
            raise ValueError("OCI output quota mount identity is unavailable") from exc
        if (
            Path(self.output_root).is_symlink()
            or quota_receipt.output_root_device != output_metadata.st_dev
            or quota_receipt.output_root_inode != output_metadata.st_ino
            or quota_receipt.output_root_owner_uid != output_metadata.st_uid
            or quota_receipt.output_root_owner_gid != output_metadata.st_gid
            or quota_receipt.output_root_mode != stat.S_IMODE(output_metadata.st_mode)
        ):
            raise ValueError("OCI output root differs from its post-mount quota receipt")
        left, right = Path(self.input_root), Path(self.output_root)
        if left == right or left in right.parents or right in left.parents:
            raise ValueError("OCI input and output roots must not overlap")
        return self

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self)

    @classmethod
    def from_runtime_launch_request(
        cls,
        *,
        request: RuntimeLaunchRequest,
        policy: DeploymentPinnedOCIPolicy,
        device_bindings: tuple[OCIDeviceBinding, ...] = (),
        node_manifest_sha256: str | None = None,
        input_materialization_receipt_sha256: str | None = None,
    ) -> "OCIExecutionPlan":
        """Project the old in-process dataclass without granting it image or mount authority."""

        receipt = request.input_materialization_receipt
        if (
            receipt.intent_sha256 != request.intent_sha256
            or receipt.execution_id != request.execution_id
            or receipt.infrastructure_attempt_id != request.attempt_id
        ):
            raise ValueError("runtime input receipt differs from the exact launch request")
        effective_manifest = node_manifest_sha256 or request.node_manifest_sha256
        effective_receipt = (
            input_materialization_receipt_sha256 or receipt.materialization_receipt_sha256
        )
        if (
            effective_manifest != request.node_manifest_sha256
            or effective_receipt != receipt.materialization_receipt_sha256
        ):
            raise ValueError("runtime projection override differs from typed node request")
        return cls(
            node_manifest_sha256=effective_manifest,
            node_id=request.node_id,
            boot_id=request.boot_id,
            execution_id=request.execution_id,
            infrastructure_attempt_id=request.attempt_id,
            intent_sha256=request.intent_sha256,
            runtime_id=request.runtime_id,
            runtime_engine=policy.runtime_engine,
            policy_sha256=policy.policy_sha256,
            runtime_request_sha256=request.runtime_request_sha256,
            launch_spec=request.spec,
            launch_spec_sha256=request.spec.launch_spec_sha256,
            enforced_placement_sha256=request.enforced_placement_sha256,
            input_materialization_receipt=receipt,
            input_materialization_receipt_sha256=effective_receipt,
            output_quota_provisioning_receipt=(request.output_quota_provisioning_receipt),
            output_quota_provisioning_receipt_sha256=(
                request.output_quota_provisioning_receipt.provisioning_receipt_sha256
            ),
            node_inventory_sha256=request.node_inventory_sha256,
            resource_lease_sha256=request.resource_lease_sha256,
            selected_resource_ids=request.selected_resource_ids,
            cpu_cores=request.cpu_cores,
            memory_bytes=request.memory_bytes,
            scratch_bytes=request.scratch_bytes,
            output_quota_bytes=request.output_quota_bytes,
            exclusive=request.exclusive,
            device_bindings=device_bindings,
            fencing_epoch=request.fencing_epoch,
            lease_token_sha256=request.lease_token_sha256,
            input_root=str(request.input_root.resolve(strict=True)),
            output_root=str(request.output_root.resolve(strict=True)),
            deadline=request.deadline,
        )


class OCIMountSpec(ExecutionModel):
    purpose: Literal["input", "output", "runtime_control"]
    source: str
    destination: str
    read_only: bool
    propagation: Literal["rprivate"] = "rprivate"


class OCIConfiguration(ExecutionModel):
    """Pure deterministic OCI/Docker configuration; it carries no start observation."""

    schema_name: Literal["aletheia.qualification_oci_configuration"] = (
        "aletheia.qualification_oci_configuration"
    )
    schema_version: Literal[2] = 2
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    low_level_runtime: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    container_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    image_reference: str
    image_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_platform: str
    entrypoint: str
    arguments: tuple[str, ...]
    launch_gate_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_authorization_control_path: str
    runtime_control_path: str
    runtime_control_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_control_key_id: str = Field(pattern=_SHA256_PATTERN)
    runtime_control_public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    workload_argv: tuple[str, ...]
    environment: tuple[PinnedEnvironmentVariable, ...]
    image_environment: tuple[PinnedEnvironmentVariable, ...]
    workload_uid: int
    workload_gid: int
    working_directory: str
    mounts: tuple[OCIMountSpec, ...]
    devices: tuple[OCIDeviceBinding, ...]
    labels: tuple[tuple[str, str], ...]
    cpu_period_microseconds: int
    cpu_quota_microseconds: int
    memory_bytes: int
    memory_swap_bytes: int
    scratch_bytes: int
    output_quota_bytes: int
    pids_limit: int
    stop_timeout_seconds: int
    masked_paths: tuple[str, ...]
    readonly_paths: tuple[str, ...]
    seccomp_profile_path: str
    seccomp_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    apparmor_profile: str
    network_mode: Literal["none"] = "none"
    pull_policy: Literal["never"] = "never"
    privileged: Literal[False] = False
    inherit_host_environment: Literal[False] = False
    extra_mounts: tuple[()] = ()
    read_only_root_filesystem: Literal[True] = True
    cap_drop_all: Literal[True] = True
    no_new_privileges: Literal[True] = True
    cgroup_namespace_mode: Literal["private"] = "private"
    healthcheck_disabled: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @property
    def oci_config_sha256(self) -> str:
        return canonical_sha256(self)


class OCIProductionCapability(ExecutionModel):
    """Fresh exact host evidence required before the first engine mutation."""

    schema_name: Literal["aletheia.oci_production_capability"] = (
        "aletheia.oci_production_capability"
    )
    schema_version: Literal[2] = 2
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    boot_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    operating_system: Literal["linux"] = "linux"
    cgroup_version: Literal[2] = 2
    cgroup_controllers: tuple[Literal["cpu", "memory", "pids"], ...]
    cgroup_mount_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_binary_sha256: str = Field(pattern=_SHA256_PATTERN)
    seccomp_profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_info_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_quota_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_attestation_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)
    production_qualified: Literal[True] = True
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _controllers_are_exact(self) -> "OCIProductionCapability":
        if self.cgroup_controllers != ("cpu", "memory", "pids"):
            raise ValueError("OCI production capability requires exact cpu/memory/pids controllers")
        return self

    @property
    def capability_sha256(self) -> str:
        return canonical_sha256(self)


class _OCIEngineTerminalObservation(ExecutionModel):
    """Adapter-owned wait/reinspect result; callers cannot submit this as evidence."""

    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = Field(ge=-255, le=255)
    ended_at: AwareDatetime
    ended_monotonic_ns: int = Field(ge=0)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)
    engine_event_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    observer_principal_id: Literal["principal:local-oci-runtime-v2"] = (
        "principal:local-oci-runtime-v2"
    )

    @model_validator(mode="after")
    def _event_is_ordered(self) -> "_OCIEngineTerminalObservation":
        if self.observed_at < self.ended_at or self.observed_monotonic_ns < self.ended_monotonic_ns:
            raise ValueError("OCI terminal event observation predates process termination")
        return self


class _PreparationIntent(ExecutionModel):
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_at: AwareDatetime
    prepared_monotonic_ns: int = Field(ge=0)


class _LaunchPending(ExecutionModel):
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    pending_at: AwareDatetime
    pending_boottime_ns: int = Field(ge=0)


class _EngineMutationSubmission(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    phase: Literal["create", "start"]
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    submitted_at: AwareDatetime
    submitted_boottime_ns: int = Field(ge=0)

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _LaunchGateAuthorizationJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request: RuntimeLaunchAuthorizationRequest
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization: RuntimeLaunchAuthorization
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_control_authority: RuntimeControlAuthorityPin
    launch_gate_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    published_at: AwareDatetime
    published_boottime_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _gate_authority_is_exact(self) -> "_LaunchGateAuthorizationJournal":
        if (
            self.authorization_request_sha256 != self.authorization_request.request_sha256
            or self.runtime_launch_authorization_sha256 != self.authorization.authorization_sha256
            or self.authorization.authorization_request_sha256
            != self.authorization_request.request_sha256
            or self.authorization.runtime_control_policy_sha256
            != self.runtime_control_authority.policy_sha256
            or self.authorization.authorization_key_id != self.runtime_control_authority.key_id
        ):
            raise ValueError("OCI launch gate journal changed signed authority scope")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _RuntimeControlJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    sequence: int = Field(ge=0)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    device_fences: tuple[tuple[str, str, int], ...]
    device_fence_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    previous_runtime_control_journal_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @property
    def control_journal_sha256(self) -> str:
        return canonical_sha256(self)


class _DeadlineWatchdogJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    container_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    pre_runtime_absence_epoch: int = Field(ge=0)
    hard_deadline: AwareDatetime
    hard_deadline_boottime_ns: int = Field(ge=0)
    watchdog_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _PrelaunchAbsenceJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_runtime_locator_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    statement: Literal["prepare-wrote-metadata-only"] = "prepare-wrote-metadata-only"

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _NeverStartedCleanupPending(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    oci_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    authorization_request: RuntimeLaunchAuthorizationRequest
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization: RuntimeLaunchAuthorization
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_pending: _LaunchPending | None = None
    launch_gate_authorization: _LaunchGateAuthorizationJournal | None = None
    production_capability: OCIProductionCapability | None = None
    deadline_watchdog: _DeadlineWatchdogJournal | None = None
    create_submission: _EngineMutationSubmission | None = None
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    initial_engine_inspection_sha256: str = Field(pattern=_SHA256_PATTERN)
    watchdog_retirement_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    pending_at: AwareDatetime
    pending_boottime_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _cleanup_scope_is_exact(self) -> "_NeverStartedCleanupPending":
        if (
            self.cleanup_absence_epoch != self.authorization_request.pre_runtime_absence_epoch + 1
            or self.authorization_request_sha256 != self.authorization_request.request_sha256
            or self.runtime_launch_authorization_sha256 != self.authorization.authorization_sha256
            or self.authorization.authorization_request_sha256
            != self.authorization_request.request_sha256
            or (self.deadline_watchdog is None)
            != (self.watchdog_retirement_evidence_sha256 is None)
        ):
            raise ValueError("OCI never-started cleanup changed its exact launch generation")
        if self.launch_pending is None:
            if any(
                phase is not None
                for phase in (
                    self.launch_gate_authorization,
                    self.production_capability,
                    self.deadline_watchdog,
                    self.create_submission,
                    self.container_id,
                )
            ):
                raise ValueError("OCI cleanup without a launch journal has a later launch phase")
        elif (
            self.launch_pending.authorization_request_sha256
            != self.authorization_request.request_sha256
            or self.launch_pending.runtime_launch_authorization_sha256
            != self.authorization.authorization_sha256
            or self.launch_pending.runtime_request_sha256 != self.runtime_request_sha256
            or self.launch_pending.oci_config_sha256 != self.oci_config_sha256
        ):
            raise ValueError("OCI never-started cleanup changed its exact launch generation")
        if self.container_id is not None and (
            self.launch_gate_authorization is None
            or self.production_capability is None
            or self.deadline_watchdog is None
            or self.create_submission is None
            or self.create_submission.phase != "create"
        ):
            raise ValueError("created OCI cleanup lacks every durable pre-mutation phase")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _WatchdogCleanupQuiescenceJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    expected_cleanup_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    acknowledgement: OCIWatchdogCleanupQuiescence
    acknowledgement_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _acknowledgement_is_exact(self) -> "_WatchdogCleanupQuiescenceJournal":
        if (
            self.acknowledgement_sha256 != self.acknowledgement.quiescence_sha256
            or self.expected_cleanup_evidence_sha256 != self.acknowledgement.cleanup_evidence_sha256
        ):
            raise ValueError("watchdog cleanup quiescence journal changed acknowledgement")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _NeverStartedCleanupCompleted(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    pending_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    deleted_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    watchdog_retirement_evidence_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    watchdog_cleanup_quiescence_journal_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    exact_engine_absence_sha256: str = Field(pattern=_SHA256_PATTERN)
    completed_at: AwareDatetime
    completed_boottime_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _watchdog_acknowledgement_is_paired(self) -> "_NeverStartedCleanupCompleted":
        if (self.watchdog_retirement_evidence_sha256 is None) != (
            self.watchdog_cleanup_quiescence_journal_sha256 is None
        ):
            raise ValueError("completed cleanup lacks its typed watchdog quiescence journal")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _LaunchGenerationRetirementPending(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    cleanup_completed_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    pre_runtime_absence_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    replacement_authorization_request_sha256: str = Field(pattern=_SHA256_PATTERN)
    replacement_runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    pending_at: AwareDatetime
    pending_boottime_ns: int = Field(ge=0)

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _LaunchGenerationRetirementCompleted(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_absence_epoch: int = Field(ge=1)
    pending_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    retired_at: AwareDatetime
    retired_boottime_ns: int = Field(ge=0)

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _FenceRebindPending(ExecutionModel):
    request: RuntimeFenceRebindRequest
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposed_control: _RuntimeControlJournal
    proposed_control_sha256: str = Field(pattern=_SHA256_PATTERN)
    device_fence_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    rebound_at: AwareDatetime
    rebound_monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _pending_is_exact(self) -> "_FenceRebindPending":
        if (
            self.request_sha256 != self.request.request_sha256
            or self.proposed_control_sha256 != self.proposed_control.control_journal_sha256
            or self.device_fence_evidence_sha256
            != self.proposed_control.device_fence_evidence_sha256
        ):
            raise ValueError("pending OCI fence journal changed its exact request or control state")
        return self


class _EngineLaunchJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    production_capability_sha256: str = Field(pattern=_SHA256_PATTERN)
    launch_gate_authorization_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    deadline_watchdog_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    create_submission_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    start_submission_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_inspection_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    pid: int = Field(ge=1)
    proc_start_ticks: int = Field(ge=0)
    pid_namespace_device: int = Field(ge=0)
    pid_namespace_inode: int = Field(ge=1)
    proc_cgroup_sha256: str = Field(pattern=_SHA256_PATTERN)
    cgroup_limits_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    workload_argv_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    started_monotonic_lower_bound_ns: int = Field(ge=0)
    started_monotonic_upper_bound_exclusive_ns: int = Field(ge=1)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _start_tick_interval_is_nonempty(self) -> "_EngineLaunchJournal":
        if self.started_monotonic_upper_bound_exclusive_ns <= self.started_monotonic_lower_bound_ns:
            raise ValueError("OCI process start tick interval is empty")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _EngineRecoveryObservationJournal(ExecutionModel):
    """Fresh live observation of one immutable engine-start journal.

    The engine start identity remains the original append-only journal.  This second journal is
    deliberately observation-only: it allows a node to sign a recovered launch more than the
    bounded receipt-signing lag after the original crash, but only after the adapter has reopened
    and rehashed the same PID, executable, argv, namespace and cgroup state.
    """

    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_launch_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_launch_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    container_inspection_sha256: str = Field(pattern=_SHA256_PATTERN)
    sandbox_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    process_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    started_monotonic_lower_bound_ns: int = Field(ge=0)
    started_monotonic_upper_bound_exclusive_ns: int = Field(ge=1)
    observed_at: AwareDatetime
    observed_monotonic_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _observation_is_ordered(self) -> "_EngineRecoveryObservationJournal":
        if (
            self.started_monotonic_upper_bound_exclusive_ns <= self.started_monotonic_lower_bound_ns
            or self.observed_at < self.started_at
            or self.observed_monotonic_ns < self.started_monotonic_upper_bound_exclusive_ns
        ):
            raise ValueError("OCI recovery observation is not ordered after engine start")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class _EngineTerminalJournal(ExecutionModel):
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_launch_journal_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation: _OCIEngineTerminalObservation

    @model_validator(mode="after")
    def _terminal_scope_is_exact(self) -> "_EngineTerminalJournal":
        if self.runtime_identity_sha256 != self.observation.runtime_identity_sha256:
            raise ValueError("OCI terminal journal changed its runtime identity")
        return self

    @property
    def journal_sha256(self) -> str:
        return canonical_sha256(self)


class LocalQualificationOCIRuntime:
    """Journaled Docker/OCI adapter implementing the v2 runtime evidence boundary."""

    def __init__(
        self,
        *,
        policy: DeploymentPinnedOCIPolicy,
        journal_root: Path,
        clock: OCIRuntimeClock | None = None,
        device_fence_controller: OCIDeviceFenceController | None = None,
        device_path_pins: tuple[OCIDevicePathPin, ...] = (),
        runtime_control_authority: RuntimeControlAuthorityVerifier | None = None,
        output_quota_controller: OCIOutputQuotaController | None = None,
        launch_gate_verifier: OCILaunchGateVerifier | None = None,
        deadline_watchdog_controller: OCIDeadlineWatchdogController | None = None,
    ) -> None:
        self._policy = DeploymentPinnedOCIPolicy.model_validate(policy.model_dump(mode="python"))
        self._journal_root = self._prepare_private_root(journal_root)
        self._seccomp_copy_path = (
            self._journal_root / "policy" / f"seccomp-{self._policy.seccomp_profile_sha256}.json"
        )
        self._clock = clock or SystemOCIRuntimeClock()
        self._device_fence_controller = device_fence_controller
        self._runtime_control_authority = runtime_control_authority
        self._output_quota_controller = output_quota_controller
        self._launch_gate_verifier = launch_gate_verifier
        self._deadline_watchdog_controller = deadline_watchdog_controller
        validated_pins = tuple(
            OCIDevicePathPin.model_validate(item.model_dump(mode="python"))
            for item in device_path_pins
        )
        keys = tuple((item.device_id, item.hardware_uuid) for item in validated_pins)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("OCI device path pins must be unique and canonical")
        self._device_path_pins = dict(zip(keys, validated_pins, strict=True))

    @property
    def policy(self) -> DeploymentPinnedOCIPolicy:
        return self._policy

    def build_oci_configuration(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
    ) -> OCIConfiguration:
        """Build the same closed OCI configuration on Linux or Darwin without launching."""

        request = self._coerce_request(request)
        control_root = self._runtime_path(request) / "control"
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "runtime_id": request.runtime_id,
                    "runtime_request_sha256": request.runtime_request_sha256,
                    "policy_sha256": self._policy.policy_sha256,
                }
            )
        ).hexdigest()
        labels = tuple(
            sorted(
                {
                    "aletheia.execution_id": request.execution_id,
                    "aletheia.infrastructure_attempt_id": request.infrastructure_attempt_id,
                    "aletheia.intent_sha256": request.intent_sha256,
                    "aletheia.node_manifest_sha256": request.node_manifest_sha256,
                    "aletheia.runtime_id": request.runtime_id,
                    "aletheia.runtime_request_sha256": request.runtime_request_sha256,
                }.items()
            )
        )
        mounts = tuple(
            sorted(
                (
                    OCIMountSpec(
                        purpose="input",
                        source=request.input_root,
                        destination=self._policy.input_mount_target,
                        read_only=True,
                    ),
                    OCIMountSpec(
                        purpose="output",
                        source=request.output_root,
                        destination=self._policy.output_mount_target,
                        read_only=False,
                    ),
                    OCIMountSpec(
                        purpose="runtime_control",
                        source=str(control_root),
                        destination=self._policy.control_mount_target,
                        read_only=True,
                    ),
                ),
                key=lambda item: item.purpose,
            )
        )
        spec = request.launch_spec
        if self._runtime_control_authority is None:
            raise OCIPolicyRejected(
                "OCI configuration requires a deployment-pinned runtime-control authority"
            )
        authority_pin = self._runtime_control_authority.pin
        gate_arguments = (
            "--authorization",
            self._policy.launch_authorization_control_path,
            "--runtime-control",
            self._policy.runtime_control_path,
            "--authority-policy-sha256",
            authority_pin.policy_sha256,
            "--authority-key-id",
            authority_pin.key_id,
            "--authority-public-key-ed25519-hex",
            authority_pin.public_key_ed25519_hex,
            "--launch-gate-protocol-sha256",
            self._policy.launch_gate_protocol_sha256,
            "--workload-executable-sha256",
            self._policy.executable_sha256,
            "--clock",
            "CLOCK_BOOTTIME",
            "--",
            *spec.argv,
        )
        return OCIConfiguration(
            policy_sha256=self._policy.policy_sha256,
            runtime_request_sha256=request.runtime_request_sha256,
            runtime_id=request.runtime_id,
            low_level_runtime=self._policy.low_level_runtime,
            container_name=f"aletheia-q-{digest[:48]}",
            image_reference=self._policy.image_reference,
            image_manifest_sha256=self._policy.image_manifest_sha256,
            image_config_sha256=self._policy.image_config_sha256,
            oci_platform=self._policy.oci_platform,
            entrypoint=self._policy.launch_gate_path,
            arguments=gate_arguments,
            launch_gate_executable_sha256=(self._policy.launch_gate_executable_sha256),
            launch_gate_protocol_sha256=self._policy.launch_gate_protocol_sha256,
            launch_authorization_control_path=(self._policy.launch_authorization_control_path),
            runtime_control_path=self._policy.runtime_control_path,
            runtime_control_policy_sha256=authority_pin.policy_sha256,
            runtime_control_key_id=authority_pin.key_id,
            runtime_control_public_key_ed25519_hex=(authority_pin.public_key_ed25519_hex),
            workload_argv=spec.argv,
            environment=spec.environment,
            image_environment=self._policy.image_environment,
            workload_uid=self._policy.workload_uid,
            workload_gid=self._policy.workload_gid,
            working_directory=self._policy.working_directory,
            mounts=mounts,
            devices=request.device_bindings,
            labels=labels,
            cpu_period_microseconds=self._policy.cpu_period_microseconds,
            cpu_quota_microseconds=(request.cpu_cores * self._policy.cpu_period_microseconds),
            memory_bytes=request.memory_bytes,
            memory_swap_bytes=request.memory_bytes,
            scratch_bytes=request.scratch_bytes,
            output_quota_bytes=request.output_quota_bytes,
            pids_limit=self._policy.pids_limit,
            stop_timeout_seconds=self._policy.stop_timeout_seconds,
            masked_paths=self._policy.masked_paths,
            readonly_paths=self._policy.readonly_paths,
            seccomp_profile_path=str(self._seccomp_copy_path),
            seccomp_profile_sha256=self._policy.seccomp_profile_sha256,
            apparmor_profile=self._policy.apparmor_profile,
        )

    def build_create_command(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
    ) -> tuple[str, ...]:
        """Return argv only; no shell and no ambient environment are involved."""

        config = self.build_oci_configuration(request=request)
        command: list[str] = [
            self._policy.runtime_binary_path,
            "--host",
            self._policy.engine_endpoint,
            "create",
            "--name",
            config.container_name,
            "--pull",
            "never",
            "--platform",
            config.oci_platform,
            "--network",
            "none",
            "--ipc",
            "none",
            "--cgroupns",
            config.cgroup_namespace_mode,
            "--read-only",
            "--user",
            f"{config.workload_uid}:{config.workload_gid}",
            "--workdir",
            config.working_directory,
            "--entrypoint",
            config.entrypoint,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={config.seccomp_profile_path}",
            "--security-opt",
            f"apparmor={config.apparmor_profile}",
            "--pids-limit",
            str(config.pids_limit),
            "--cpu-period",
            str(config.cpu_period_microseconds),
            "--cpu-quota",
            str(config.cpu_quota_microseconds),
            "--memory",
            str(config.memory_bytes),
            "--memory-swap",
            str(config.memory_swap_bytes),
            "--restart",
            "no",
            "--stop-timeout",
            str(config.stop_timeout_seconds),
            "--log-driver",
            "none",
            "--runtime",
            config.low_level_runtime,
            "--no-healthcheck",
        ]
        if config.scratch_bytes:
            command.extend(
                (
                    "--tmpfs",
                    (
                        f"{self._policy.scratch_mount_target}:rw,noexec,nosuid,nodev,"
                        f"size={config.scratch_bytes},mode=0700"
                    ),
                )
            )
        for mount in config.mounts:
            value = (
                f"type=bind,src={mount.source},dst={mount.destination},"
                f"bind-propagation={mount.propagation}"
            )
            if mount.read_only:
                value += ",readonly"
            command.extend(("--mount", value))
        for device in config.devices:
            command.extend(
                (
                    "--device",
                    f"{device.host_device_path}:{device.container_device_path}:{device.access}",
                )
            )
        for name, value in config.labels:
            command.extend(("--label", f"{name}={value}"))
        for item in config.environment:
            command.extend(("--env", f"{item.name}={item.value}"))
        command.append(config.image_reference)
        command.extend(config.arguments)
        return tuple(command)

    def prepare(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
    ) -> RuntimePreparation:
        """Durably prepare metadata; this method never invokes or contacts the OCI engine."""

        request = self._coerce_request(request)
        runtime_root = self._ensure_runtime_directories(request)
        config = self.build_oci_configuration(request=request)
        self._validate_workspace(request)
        with self._runtime_lock(runtime_root):
            stored_plan = self._load_model(
                runtime_root / "plan.json", OCIExecutionPlan, optional=True
            )
            if stored_plan is not None and stored_plan != request:
                raise OCIJournalError("runtime id is already bound to another OCI request")
            self._publish_model(runtime_root / "plan.json", request)
            stored_config = self._load_model(
                runtime_root / "oci-config.json", OCIConfiguration, optional=True
            )
            if stored_config is not None and stored_config != config:
                raise OCIJournalError("runtime OCI configuration changed during replay")
            self._publish_model(runtime_root / "oci-config.json", config)
            preparation_intent = self._load_model(
                runtime_root / "prepare-intent.json",
                _PreparationIntent,
                optional=True,
            )
            if preparation_intent is None:
                preparation_intent = _PreparationIntent(
                    runtime_request_sha256=request.runtime_request_sha256,
                    oci_config_sha256=config.oci_config_sha256,
                    prepared_at=self._utc_now(),
                    prepared_monotonic_ns=self._clock.monotonic_ns(),
                )
                self._publish_model(runtime_root / "prepare-intent.json", preparation_intent)
            elif (
                preparation_intent.runtime_request_sha256 != request.runtime_request_sha256
                or preparation_intent.oci_config_sha256 != config.oci_config_sha256
            ):
                raise OCIJournalError("runtime preparation intent changed during crash replay")
            locator_sha256 = canonical_sha256(
                {
                    "schema": "aletheia.prepared_oci_runtime_locator.v2",
                    "runtime_engine": self._policy.runtime_engine,
                    "engine_endpoint": self._policy.engine_endpoint,
                    "container_name": config.container_name,
                    "policy_sha256": self._policy.policy_sha256,
                }
            )
            expected = RuntimePreparation(
                node_manifest_sha256=request.node_manifest_sha256,
                node_id=request.node_id,
                boot_id=request.boot_id,
                execution_id=request.execution_id,
                infrastructure_attempt_id=request.infrastructure_attempt_id,
                intent_sha256=request.intent_sha256,
                runtime_id=request.runtime_id,
                runtime_engine=request.runtime_engine,
                launch_spec_sha256=request.launch_spec_sha256,
                workload_executable_sha256=request.launch_spec.executable_sha256,
                workload_argv=request.launch_spec.argv,
                runtime_request_sha256=request.runtime_request_sha256,
                enforced_placement_sha256=request.enforced_placement_sha256,
                input_materialization_receipt_sha256=(request.input_materialization_receipt_sha256),
                output_quota_provisioning_receipt_sha256=(
                    request.output_quota_provisioning_receipt_sha256
                ),
                fencing_epoch=request.fencing_epoch,
                lease_token_sha256=request.lease_token_sha256,
                prepared_runtime_locator_sha256=locator_sha256,
                oci_config_sha256=config.oci_config_sha256,
                prepared_at=preparation_intent.prepared_at,
                prepared_monotonic_ns=preparation_intent.prepared_monotonic_ns,
            )
            stored = self._load_model(
                runtime_root / "preparation.json", RuntimePreparation, optional=True
            )
            if stored is not None and stored != expected:
                raise OCIJournalError("runtime preparation bytes changed during replay")
            self._publish_model(runtime_root / "preparation.json", expected)
            self._ensure_initial_control(runtime_root, request=request, preparation=expected)
            absence = _PrelaunchAbsenceJournal(
                preparation_sha256=expected.preparation_sha256,
                prepared_runtime_locator_sha256=expected.prepared_runtime_locator_sha256,
                runtime_request_sha256=request.runtime_request_sha256,
            )
            self._publish_model(runtime_root / "prelaunch-absence.json", absence)
            return expected

    def inspect(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity | None,
    ) -> RuntimeInspectionEvidence:
        """Inspect durable state; missing runtime without exact absence/terminal proof is UNKNOWN."""

        request, preparation, runtime_root = self._validate_prepared(request, preparation)
        with self._runtime_lock(runtime_root):
            control = self._current_control(runtime_root)
            self._validate_current_request(request, preparation, control)
            launch = self._load_model(
                runtime_root / "launch-evidence.json", RuntimeLaunchEvidence, optional=True
            )
            pending_launch = (runtime_root / "launch-pending.json").exists()
            terminal = self._load_model(
                runtime_root / "engine-terminal.json", _EngineTerminalJournal, optional=True
            )
            inspected_at = self._utc_now()
            inspected_monotonic_ns = self._clock.monotonic_ns()
            if launch is None and not pending_launch:
                if identity is not None:
                    raise OCIJournalError(
                        "caller supplied an identity for exact never-started state"
                    )
                absence = self._load_required(
                    runtime_root / "prelaunch-absence.json", _PrelaunchAbsenceJournal
                )
                return self._inspection(
                    state=RuntimeInspectionState.ABSENT,
                    preparation=preparation,
                    control=control,
                    inspected_at=inspected_at,
                    inspected_monotonic_ns=inspected_monotonic_ns,
                    identity=None,
                    prelaunch_absence_journal_sha256=absence.journal_sha256,
                    prelaunch_absence_epoch=1,
                )
            if launch is None:
                return self._inspection(
                    state=RuntimeInspectionState.UNKNOWN,
                    preparation=preparation,
                    control=control,
                    inspected_at=inspected_at,
                    inspected_monotonic_ns=inspected_monotonic_ns,
                    identity=None,
                )
            self._validate_launch_evidence(launch, preparation)
            runtime_identity = launch.runtime_identity
            if identity is not None and identity != runtime_identity:
                raise OCIJournalError("runtime inspection identity differs from launch evidence")
            if terminal is not None:
                self._validate_terminal_journal(
                    runtime_root=runtime_root,
                    preparation=preparation,
                    launch=launch,
                    terminal=terminal,
                )
                observation = terminal.observation
                return self._inspection(
                    state=RuntimeInspectionState.TERMINATED,
                    preparation=preparation,
                    control=control,
                    inspected_at=max(inspected_at, observation.observed_at),
                    inspected_monotonic_ns=max(
                        inspected_monotonic_ns,
                        observation.observed_monotonic_ns,
                    ),
                    identity=runtime_identity,
                    terminal=terminal,
                )
            try:
                engine_inspection = self._inspect_container(
                    request=request,
                    preparation=preparation,
                    expected_identity=runtime_identity,
                )
            except OCIProductionCapabilityError:
                engine_inspection = None
            engine_state = engine_inspection.get("State") if engine_inspection is not None else None
            if isinstance(engine_state, dict) and engine_state.get("Running"):
                return self._inspection(
                    state=RuntimeInspectionState.RUNNING,
                    preparation=preparation,
                    control=control,
                    inspected_at=inspected_at,
                    inspected_monotonic_ns=inspected_monotonic_ns,
                    identity=runtime_identity,
                )
            if isinstance(engine_state, dict) and engine_state.get("Status") in {
                "dead",
                "exited",
            }:
                terminal = self._capture_terminal_from_engine(
                    runtime_root=runtime_root,
                    preparation=preparation,
                    launch=launch,
                    first_inspection=engine_inspection,
                )
                self._publish_model(runtime_root / "engine-terminal.json", terminal)
                return self._inspection(
                    state=RuntimeInspectionState.TERMINATED,
                    preparation=preparation,
                    control=control,
                    inspected_at=max(inspected_at, terminal.observation.observed_at),
                    inspected_monotonic_ns=max(
                        inspected_monotonic_ns,
                        terminal.observation.observed_monotonic_ns,
                    ),
                    identity=runtime_identity,
                    terminal=terminal,
                )
            return self._inspection(
                state=RuntimeInspectionState.UNKNOWN,
                preparation=preparation,
                control=control,
                inspected_at=inspected_at,
                inspected_monotonic_ns=inspected_monotonic_ns,
                identity=runtime_identity,
            )

    def rebind_fence(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity,
    ) -> RuntimeFenceRebindEvidence:
        """Crash-idempotently rotate one exact runtime/device fence under a singleton lock."""

        request = RuntimeFenceRebindRequest.model_validate(request.model_dump(mode="python"))
        preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        identity = NodeRuntimeIdentity.model_validate(identity.model_dump(mode="python"))
        runtime_root = self._runtime_path_for_id(preparation.runtime_id)
        self._validate_identity(preparation, identity)
        with self._runtime_lock(runtime_root):
            stored_preparation = self._load_required(
                runtime_root / "preparation.json", RuntimePreparation
            )
            if stored_preparation != preparation:
                raise OCIJournalError("fence rebind preparation differs from durable bytes")
            plan = self._load_required(runtime_root / "plan.json", OCIExecutionPlan)
            current = self._current_control(runtime_root)
            completed_path = (
                runtime_root / "rebind" / f"{request.rebind_sequence:08d}.completed.json"
            )
            pending_path = runtime_root / "rebind" / f"{request.rebind_sequence:08d}.pending.json"
            completed = self._load_model(completed_path, RuntimeFenceRebindEvidence, optional=True)
            if completed is not None:
                pending = self._load_required(pending_path, _FenceRebindPending)
                if pending.request != request:
                    raise OCIJournalError("completed fence sequence is bound to another request")
                validate_runtime_fence_rebind_evidence(request=request, evidence=completed)
                if current.control_journal_sha256 != completed.new_runtime_control_journal_sha256:
                    raise OCIJournalError("completed fence evidence differs from current sidecar")
                return completed
            pending = self._load_model(pending_path, _FenceRebindPending, optional=True)
            if pending is None:
                if (
                    request.preparation_sha256 != preparation.preparation_sha256
                    or request.runtime_identity_sha256 != identity.runtime_identity_sha256
                    or request.previous_fencing_epoch != current.fencing_epoch
                    or request.previous_lease_token_sha256 != current.lease_token_sha256
                    or request.rebind_sequence != current.sequence + 1
                    or request.expected_runtime_control_journal_sha256
                    != current.control_journal_sha256
                ):
                    raise OCIJournalError("runtime fence request is not an exact next-sidecar CAS")
                device_evidence = self._expected_device_rebind(request, plan.device_bindings)
                proposed = _RuntimeControlJournal(
                    preparation_sha256=preparation.preparation_sha256,
                    runtime_identity_sha256=identity.runtime_identity_sha256,
                    sequence=request.rebind_sequence,
                    fencing_epoch=request.new_fencing_epoch,
                    lease_token_sha256=request.new_lease_token_sha256,
                    enforced_placement_sha256=preparation.enforced_placement_sha256,
                    device_fences=tuple(
                        (item.device_id, item.hardware_uuid, request.new_fencing_epoch)
                        for item in plan.device_bindings
                    ),
                    device_fence_evidence_sha256=device_evidence,
                    previous_runtime_control_journal_sha256=(current.control_journal_sha256),
                )
                pending = _FenceRebindPending(
                    request=request,
                    request_sha256=request.request_sha256,
                    proposed_control=proposed,
                    proposed_control_sha256=proposed.control_journal_sha256,
                    device_fence_evidence_sha256=device_evidence,
                    rebound_at=self._utc_now(),
                    rebound_monotonic_ns=self._clock.monotonic_ns(),
                )
                self._publish_model(pending_path, pending)
            elif pending.request != request:
                raise OCIJournalError("fence sequence already has a different pending request")

            proposed = pending.proposed_control
            if current.control_journal_sha256 == request.expected_runtime_control_journal_sha256:
                self._apply_device_rebind(
                    request=request,
                    devices=plan.device_bindings,
                    expected_sha256=pending.device_fence_evidence_sha256,
                )
                self._replace_model(runtime_root / "control" / "current.json", proposed)
                current = proposed
            elif current != proposed:
                raise OCIJournalError("runtime control sidecar changed outside the exact CAS")
            evidence = RuntimeFenceRebindEvidence(
                request_sha256=request.request_sha256,
                preparation_sha256=preparation.preparation_sha256,
                runtime_identity_sha256=identity.runtime_identity_sha256,
                previous_fencing_epoch=request.previous_fencing_epoch,
                previous_lease_token_sha256=request.previous_lease_token_sha256,
                new_fencing_epoch=request.new_fencing_epoch,
                new_lease_token_sha256=request.new_lease_token_sha256,
                rebind_sequence=request.rebind_sequence,
                previous_runtime_control_journal_sha256=(
                    request.expected_runtime_control_journal_sha256
                ),
                new_runtime_control_journal_sha256=current.control_journal_sha256,
                rebind_evidence_sha256=canonical_sha256(
                    {
                        "schema": "aletheia.oci_runtime_fence_transition_evidence.v2",
                        "request_sha256": request.request_sha256,
                        "pending_journal_sha256": canonical_sha256(pending),
                        "device_fence_evidence_sha256": (pending.device_fence_evidence_sha256),
                        "new_runtime_control_journal_sha256": (current.control_journal_sha256),
                    }
                ),
                rebound_at=pending.rebound_at,
                rebound_monotonic_ns=pending.rebound_monotonic_ns,
            )
            validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
            self._publish_model(completed_path, evidence)
            return evidence

    def ensure_started(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None = None,
    ) -> RuntimeLaunchEvidence:
        """Create/start once, with a fresh DB ticket checked at each engine mutation."""

        request, preparation, runtime_root = self._validate_prepared(request, preparation)
        config = self._load_required(runtime_root / "oci-config.json", OCIConfiguration)
        authorization_request, authorization = self._validate_launch_authorization_history(
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        authorization_sha256 = authorization.authorization_sha256

        # A completed launch is a non-mutating crash replay.  Its original signed ticket may have
        # expired, but the exact pending/engine/evidence chain must still bind that ticket.
        with self._runtime_lock(runtime_root):
            self._activate_authorized_launch_generation_locked(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                pre_runtime_absence_receipt=pre_runtime_absence_receipt,
            )
            self._assert_launch_generation_not_under_cleanup(
                runtime_root=runtime_root,
                authorization_request=authorization_request,
            )
            control = self._current_control(runtime_root)
            self._validate_current_request(request, preparation, control)
            existing = self._load_model(
                runtime_root / "launch-evidence.json", RuntimeLaunchEvidence, optional=True
            )
            if existing is not None:
                self._validate_persisted_launch(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    evidence=existing,
                    authorization_request_sha256=authorization_request.request_sha256,
                    authorization_sha256=authorization_sha256,
                    authorization_request=authorization_request,
                    authorization=authorization,
                )
                return existing
            engine_journal = self._load_model(
                runtime_root / "engine-launch.json", _EngineLaunchJournal, optional=True
            )
            if engine_journal is not None:
                capability = self._load_required(
                    runtime_root / "production-capability.json",
                    OCIProductionCapability,
                )
                evidence = self._complete_launch_evidence_from_journal(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    engine_journal=engine_journal,
                    capability=capability,
                    authorization_request_sha256=(authorization_request.request_sha256),
                    authorization_sha256=authorization_sha256,
                    authorization_request=authorization_request,
                    authorization=authorization,
                )
                return evidence

        if request.device_bindings:
            raise OCIProductionCapabilityError(
                "this qualification runtime cut is CPU-only; device launch is fail-closed"
            )
        capability = self.probe_production_capability(request=request)
        if capability.boot_id != request.boot_id:
            raise OCIProductionCapabilityError("host boot changed after runtime preparation")
        recovered = self._recover_unjournaled_running_launch(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
            observed_capability=capability,
        )
        if recovered is not None:
            return recovered
        # Recheck immediately after every potentially slow production-capability probe.  The same
        # guard runs from inside _run_engine after binary/seccomp revalidation, immediately before
        # each create/start subprocess mutation.
        self._verify_launch_authorization_fresh(
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )

        initial_device_evidence_sha256: str | None = None

        def mutation_guard() -> None:
            # Controller establishment is itself authority-sensitive.  Bracket it with fresh
            # ticket checks, then leave the second check as the final pre-subprocess operation.
            self._verify_launch_authorization_fresh(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            if request.device_bindings:
                if initial_device_evidence_sha256 is None:
                    raise OCIProductionCapabilityError(
                        "initial device-fence evidence is not durably prepared"
                    )
                self._apply_initial_device_fence(
                    preparation=preparation,
                    devices=request.device_bindings,
                    expected_sha256=initial_device_evidence_sha256,
                )
            self._ensure_deadline_watchdog(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                config=config,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._validate_materialized_input_tree(request)
            self._validate_empty_output_tree(request)
            observed_quota_evidence = self._verify_output_quota(request)
            if observed_quota_evidence != capability.output_quota_evidence_sha256:
                raise OCIProductionCapabilityError(
                    "output project quota changed after production capability probe"
                )
            self._output_mount_generation_sha256(request)
            # The daemon consumes this pathname, so rehash the owner-only immutable copy after
            # every other slow preflight and before the final ticket check/subprocess boundary.
            self._verify_seccomp_copy()
            self._verify_launch_authorization_fresh(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )

        def final_mutation_guard() -> None:
            self._verify_launch_authorization_fresh(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._output_mount_generation_sha256(request)

        def assert_generation_or_terminate(container_id: str) -> None:
            try:
                self._output_mount_generation_sha256(request)
            except OCIProductionCapabilityError as generation_error:
                try:
                    self._terminate_after_mount_generation_failure(container_id)
                except OCIRuntimeError as cleanup_error:
                    raise OCIEngineError(
                        "output mount generation changed and emergency termination failed"
                    ) from cleanup_error
                raise generation_error

        with self._runtime_lock(runtime_root):
            self._activate_authorized_launch_generation_locked(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                pre_runtime_absence_receipt=pre_runtime_absence_receipt,
            )
            self._assert_launch_generation_not_under_cleanup(
                runtime_root=runtime_root,
                authorization_request=authorization_request,
            )
            control = self._current_control(runtime_root)
            self._validate_current_request(request, preparation, control)
            initial_device_evidence_sha256 = control.device_fence_evidence_sha256
            existing = self._load_model(
                runtime_root / "launch-evidence.json", RuntimeLaunchEvidence, optional=True
            )
            if existing is not None:
                self._validate_persisted_launch(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    evidence=existing,
                    authorization_request_sha256=authorization_request.request_sha256,
                    authorization_sha256=authorization_sha256,
                    authorization_request=authorization_request,
                    authorization=authorization,
                )
                return existing
            pending_path = runtime_root / "launch-pending.json"
            pending = self._load_model(pending_path, _LaunchPending, optional=True)
            if pending is None:
                pending = _LaunchPending(
                    runtime_request_sha256=request.runtime_request_sha256,
                    oci_config_sha256=config.oci_config_sha256,
                    authorization_request_sha256=authorization_request.request_sha256,
                    runtime_launch_authorization_sha256=authorization_sha256,
                    pending_at=self._utc_now(),
                    pending_boottime_ns=self._boottime_ns(),
                )
                self._publish_model(pending_path, pending)
            elif (
                pending.runtime_request_sha256 != request.runtime_request_sha256
                or pending.oci_config_sha256 != config.oci_config_sha256
                or pending.authorization_request_sha256 != authorization_request.request_sha256
                or pending.runtime_launch_authorization_sha256 != authorization_sha256
            ):
                raise OCIJournalError("OCI launch pending journal changed scope")

            gate_authorization = self._ensure_launch_gate_authorization(
                runtime_root=runtime_root,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )

            stored_capability = self._load_model(
                runtime_root / "production-capability.json",
                OCIProductionCapability,
                optional=True,
            )
            if stored_capability is None:
                self._publish_model(runtime_root / "production-capability.json", capability)
            else:
                self._validate_capability_replay(
                    stored=stored_capability,
                    observed=capability,
                    request=request,
                )
                capability = stored_capability

            engine_journal = self._load_model(
                runtime_root / "engine-launch.json", _EngineLaunchJournal, optional=True
            )
            if engine_journal is not None:
                return self._complete_launch_evidence_from_journal(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    engine_journal=engine_journal,
                    capability=capability,
                    authorization_request_sha256=(authorization_request.request_sha256),
                    authorization_sha256=authorization_sha256,
                    authorization_request=authorization_request,
                    authorization=authorization,
                )

            self._verify_launch_authorization_fresh(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._ensure_deadline_watchdog(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                config=config,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._verify_launch_authorization_fresh(
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            create_command = self.build_create_command(request=request)
            start_command = (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "start",
                config.container_name,
            )
            create_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="create",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=create_command,
            )
            start_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="start",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=start_command,
            )
            inspection = self._engine_inspect(config.container_name, optional=True)
            if inspection is None:
                if create_submission is not None or start_submission is not None:
                    raise OCIJournalError(
                        "submitted OCI mutation has no exact completed engine state"
                    )
                created_id = self._run_engine(
                    create_command,
                    mutation_guard=mutation_guard,
                    submission_marker=lambda: self._publish_engine_submission(
                        runtime_root=runtime_root,
                        phase="create",
                        preparation=preparation,
                        authorization_request=authorization_request,
                        authorization=authorization,
                        command=create_command,
                    ),
                    final_mutation_guard=final_mutation_guard,
                    mutation_runtime_root=runtime_root,
                ).strip()
                if re.fullmatch(r"[0-9a-f]{64}", created_id) is None:
                    raise OCIEngineError("OCI create did not return one exact container id")
                assert_generation_or_terminate(created_id)
                inspection = self._engine_inspect(config.container_name, optional=False)
                if inspection.get("Id") != created_id:
                    raise OCIEngineError("OCI create identity differs from engine inspection")
                create_submission = self._load_engine_submission(
                    runtime_root=runtime_root,
                    phase="create",
                    preparation=preparation,
                    authorization_request=authorization_request,
                    authorization=authorization,
                    command=create_command,
                )
            self._validate_engine_configuration(inspection, config=config)
            state = inspection.get("State")
            if not isinstance(state, dict):
                raise OCIEngineError("OCI inspection omitted typed process state")
            if not state.get("Running"):
                if state.get("Status") != "created":
                    raise OCIJournalError(
                        "pending OCI launch is neither safely startable nor durably evidenced"
                    )
                if create_submission is None or start_submission is not None:
                    raise OCIJournalError(
                        "OCI CREATED state lacks an exact quiescent create submission"
                    )
                self._run_engine(
                    start_command,
                    mutation_guard=mutation_guard,
                    submission_marker=lambda: self._publish_engine_submission(
                        runtime_root=runtime_root,
                        phase="start",
                        preparation=preparation,
                        authorization_request=authorization_request,
                        authorization=authorization,
                        command=start_command,
                    ),
                    final_mutation_guard=final_mutation_guard,
                    mutation_runtime_root=runtime_root,
                )
                container_id = inspection.get("Id")
                if (
                    not isinstance(container_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
                ):
                    raise OCIEngineError("OCI CREATED state omitted exact container id")
                assert_generation_or_terminate(container_id)
                start_submission = self._load_engine_submission(
                    runtime_root=runtime_root,
                    phase="start",
                    preparation=preparation,
                    authorization_request=authorization_request,
                    authorization=authorization,
                    command=start_command,
                )
                inspection = self._engine_inspect(config.container_name, optional=False)
                self._validate_engine_configuration(inspection, config=config)
                state = inspection.get("State")
            if not isinstance(state, dict) or not state.get("Running"):
                # A process that starts and exits before exact PID/start evidence is captured is
                # intentionally UNKNOWN, never a fabricated launch receipt.
                raise OCIEngineError(
                    "OCI process did not remain running long enough for exact launch evidence"
                )
            if create_submission is None or start_submission is None:
                raise OCIJournalError("running OCI process lacks exact create/start submissions")
            running_container_id = inspection.get("Id")
            if (
                not isinstance(running_container_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", running_container_id) is None
            ):
                raise OCIEngineError("running OCI process omitted exact container id")
            assert_generation_or_terminate(running_container_id)
            watchdog = self._load_required(
                runtime_root / "deadline-watchdog.json",
                _DeadlineWatchdogJournal,
            )
            journal = self._launch_journal(
                request=request,
                preparation=preparation,
                inspection=inspection,
                authorization_sha256=authorization_sha256,
                production_capability_sha256=capability.capability_sha256,
                launch_gate_authorization_journal_sha256=(gate_authorization.journal_sha256),
                deadline_watchdog_journal_sha256=watchdog.journal_sha256,
                create_submission_journal_sha256=create_submission.journal_sha256,
                start_submission_journal_sha256=start_submission.journal_sha256,
            )
            self._verify_actual_launch_window(
                journal=journal,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._publish_model(runtime_root / "engine-launch.json", journal)
            evidence = self._compose_launch_evidence(
                request=request,
                preparation=preparation,
                config=config,
                journal=journal,
                capability=capability,
            )
            self._validate_launch_evidence(
                evidence,
                preparation,
                authorization_sha256=authorization_sha256,
            )
            self._publish_model(runtime_root / "launch-evidence.json", evidence)
            return evidence

    def recover_started(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None = None,
    ) -> RuntimeLaunchEvidence | None:
        """Recover exact live evidence without ever reaching an engine create/start command."""

        request, preparation, runtime_root = self._validate_prepared(request, preparation)
        config = self._load_required(runtime_root / "oci-config.json", OCIConfiguration)
        authorization_request, authorization = self._validate_launch_authorization_history(
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        self._activate_authorized_launch_generation(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            pre_runtime_absence_receipt=pre_runtime_absence_receipt,
        )
        if request.device_bindings:
            raise OCIProductionCapabilityError(
                "this qualification runtime cut is CPU-only; device recovery is fail-closed"
            )
        capability = self.probe_production_capability(request=request)
        if capability.boot_id != request.boot_id:
            raise OCIProductionCapabilityError("host boot changed after runtime preparation")
        return self._recover_unjournaled_running_launch(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
            observed_capability=capability,
        )

    def cleanup_never_started(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> RuntimeInspectionEvidence:
        """Remove only exact CREATED/PID0 state with no submitted start mutation."""

        request, preparation, runtime_root = self._validate_prepared(request, preparation)
        config = self._load_required(runtime_root / "oci-config.json", OCIConfiguration)
        authorization_request, authorization = self._validate_launch_authorization_history(
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        if request.device_bindings:
            raise OCIProductionCapabilityError(
                "this qualification runtime cut is CPU-only; device cleanup is fail-closed"
            )
        capability = self.probe_production_capability(request=request)
        if capability.boot_id != request.boot_id:
            raise OCIProductionCapabilityError("host boot changed during never-started cleanup")
        cleanup_epoch = authorization_request.pre_runtime_absence_epoch + 1
        pending_path, completed_path = self._cleanup_paths(runtime_root, cleanup_epoch)
        create_command = self.build_create_command(request=request)
        start_command = (
            self._policy.runtime_binary_path,
            "--host",
            self._policy.engine_endpoint,
            "start",
            config.container_name,
        )
        with self._runtime_lock(runtime_root):
            control = self._current_control(runtime_root)
            self._validate_current_request(request, preparation, control)
            if (
                self._load_model(
                    runtime_root / "launch-evidence.json",
                    RuntimeLaunchEvidence,
                    optional=True,
                )
                is not None
                or self._load_model(
                    runtime_root / "engine-launch.json",
                    _EngineLaunchJournal,
                    optional=True,
                )
                is not None
            ):
                # A quick exit can leave exact engine-start evidence before the node has persisted
                # its launch receipt.  It is definitely not never-started, but this cleanup port
                # must return UNKNOWN so the node retains the hold instead of treating an expected
                # reconciliation outcome as an adapter failure.
                return self._inspection(
                    state=RuntimeInspectionState.UNKNOWN,
                    preparation=preparation,
                    control=control,
                    inspected_at=self._utc_now(),
                    inspected_monotonic_ns=self._clock.monotonic_ns(),
                    identity=None,
                )
            launch_pending = self._load_model(
                runtime_root / "launch-pending.json",
                _LaunchPending,
                optional=True,
            )
            if launch_pending is not None and (
                launch_pending.runtime_request_sha256 != request.runtime_request_sha256
                or launch_pending.oci_config_sha256 != config.oci_config_sha256
                or launch_pending.authorization_request_sha256
                != authorization_request.request_sha256
                or launch_pending.runtime_launch_authorization_sha256
                != authorization.authorization_sha256
            ):
                raise OCIJournalError("OCI cleanup differs from active launch pending authority")
            gate = self._load_model(
                runtime_root / "control" / "launch-authorization.json",
                _LaunchGateAuthorizationJournal,
                optional=True,
            )
            stored_capability = self._load_model(
                runtime_root / "production-capability.json",
                OCIProductionCapability,
                optional=True,
            )
            watchdog = self._load_model(
                runtime_root / "deadline-watchdog.json",
                _DeadlineWatchdogJournal,
                optional=True,
            )
            create_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="create",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=create_command,
            )
            start_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="start",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=start_command,
            )
            if start_submission is not None:
                return self._inspection(
                    state=RuntimeInspectionState.UNKNOWN,
                    preparation=preparation,
                    control=control,
                    inspected_at=self._utc_now(),
                    inspected_monotonic_ns=self._clock.monotonic_ns(),
                    identity=None,
                )
            if gate is not None and (
                gate.preparation_sha256 != preparation.preparation_sha256
                or gate.authorization_request != authorization_request
                or gate.authorization != authorization
                or self._runtime_control_authority is None
                or gate.runtime_control_authority != self._runtime_control_authority.pin
            ):
                raise OCIJournalError("OCI cleanup launch-gate phase changed exact authority")
            if stored_capability is not None:
                self._validate_capability_replay(
                    stored=stored_capability,
                    observed=capability,
                    request=request,
                )
            if watchdog is not None:
                watchdog = self._validate_deadline_watchdog_journal(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    authorization_request=authorization_request,
                    authorization=authorization,
                )
            if (
                (
                    launch_pending is None
                    and any(
                        phase is not None
                        for phase in (
                            gate,
                            stored_capability,
                            watchdog,
                            create_submission,
                            start_submission,
                        )
                    )
                )
                or (stored_capability is not None and gate is None)
                or (watchdog is not None and (gate is None or stored_capability is None))
                or (
                    create_submission is not None
                    and (gate is None or stored_capability is None or watchdog is None)
                )
            ):
                raise OCIJournalError("OCI cleanup phase ordering is incomplete or impossible")

            pending = self._load_model(
                pending_path,
                _NeverStartedCleanupPending,
                optional=True,
            )
            completed = self._load_model(
                completed_path,
                _NeverStartedCleanupCompleted,
                optional=True,
            )
            inspection = self._engine_inspect(config.container_name, optional=True)
            if pending is None:
                container_id: str | None = None
                if inspection is None:
                    if create_submission is not None:
                        return self._inspection(
                            state=RuntimeInspectionState.UNKNOWN,
                            preparation=preparation,
                            control=control,
                            inspected_at=self._utc_now(),
                            inspected_monotonic_ns=self._clock.monotonic_ns(),
                            identity=None,
                        )
                    initial_inspection_sha256 = self._exact_engine_absence_sha256(
                        config.container_name
                    )
                else:
                    if create_submission is None:
                        return self._inspection(
                            state=RuntimeInspectionState.UNKNOWN,
                            preparation=preparation,
                            control=control,
                            inspected_at=self._utc_now(),
                            inspected_monotonic_ns=self._clock.monotonic_ns(),
                            identity=None,
                        )
                    try:
                        container_id = self._created_never_started_container_id(
                            inspection,
                            config=config,
                        )
                    except OCIEngineError:
                        return self._inspection(
                            state=RuntimeInspectionState.UNKNOWN,
                            preparation=preparation,
                            control=control,
                            inspected_at=self._utc_now(),
                            inspected_monotonic_ns=self._clock.monotonic_ns(),
                            identity=None,
                        )
                    initial_inspection_sha256 = canonical_sha256(inspection)
                retirement_evidence = (
                    self._expected_watchdog_retirement_evidence_sha256(
                        preparation=preparation,
                        authorization_request=authorization_request,
                        authorization=authorization,
                        watchdog=watchdog,
                        cleanup_absence_epoch=cleanup_epoch,
                    )
                    if watchdog is not None
                    else None
                )
                pending = _NeverStartedCleanupPending(
                    preparation_sha256=preparation.preparation_sha256,
                    runtime_request_sha256=request.runtime_request_sha256,
                    oci_config_sha256=config.oci_config_sha256,
                    cleanup_absence_epoch=cleanup_epoch,
                    authorization_request=authorization_request,
                    authorization_request_sha256=authorization_request.request_sha256,
                    authorization=authorization,
                    runtime_launch_authorization_sha256=authorization.authorization_sha256,
                    launch_pending=launch_pending,
                    launch_gate_authorization=gate,
                    production_capability=stored_capability,
                    deadline_watchdog=watchdog,
                    create_submission=create_submission,
                    container_id=container_id,
                    initial_engine_inspection_sha256=initial_inspection_sha256,
                    watchdog_retirement_evidence_sha256=retirement_evidence,
                    pending_at=self._utc_now(),
                    pending_boottime_ns=self._boottime_ns(),
                )
                self._publish_model(pending_path, pending)
            elif (
                pending.preparation_sha256 != preparation.preparation_sha256
                or pending.runtime_request_sha256 != request.runtime_request_sha256
                or pending.oci_config_sha256 != config.oci_config_sha256
                or pending.cleanup_absence_epoch != cleanup_epoch
                or pending.authorization_request != authorization_request
                or pending.authorization != authorization
                or pending.launch_pending != launch_pending
                or pending.launch_gate_authorization != gate
                or pending.production_capability != stored_capability
                or pending.deadline_watchdog != watchdog
                or pending.create_submission != create_submission
            ):
                raise OCIJournalError("OCI cleanup pending journal changed during replay")

            quiescence_path = (
                runtime_root / "cleanup" / f"absence-{cleanup_epoch}-watchdog-quiescence.json"
            )
            quiescence = self._load_model(
                quiescence_path,
                _WatchdogCleanupQuiescenceJournal,
                optional=True,
            )
            if pending.deadline_watchdog is not None:
                assert pending.watchdog_retirement_evidence_sha256 is not None
                if quiescence is None:
                    acknowledgement = self._retire_deadline_watchdog(
                        preparation=preparation,
                        authorization_request=authorization_request,
                        authorization=authorization,
                        watchdog=pending.deadline_watchdog,
                        cleanup_absence_epoch=cleanup_epoch,
                        expected_evidence_sha256=(pending.watchdog_retirement_evidence_sha256),
                    )
                    if (
                        acknowledgement.decision == "fired_absent"
                        and (
                            pending.container_id is not None
                            or pending.create_submission is not None
                        )
                    ) or (
                        acknowledgement.decision == "fired_stopped"
                        and (
                            pending.container_id is None
                            or pending.create_submission is None
                            or acknowledgement.container_id != pending.container_id
                        )
                    ):
                        raise OCIProductionCapabilityError(
                            "watchdog fired quiescence differs from never-started cleanup scope"
                        )
                    quiescence = _WatchdogCleanupQuiescenceJournal(
                        preparation_sha256=preparation.preparation_sha256,
                        cleanup_absence_epoch=cleanup_epoch,
                        expected_cleanup_evidence_sha256=(
                            pending.watchdog_retirement_evidence_sha256
                        ),
                        acknowledgement=acknowledgement,
                        acknowledgement_sha256=acknowledgement.quiescence_sha256,
                    )
                    self._publish_model(quiescence_path, quiescence)
                elif (
                    quiescence.preparation_sha256 != preparation.preparation_sha256
                    or quiescence.cleanup_absence_epoch != cleanup_epoch
                    or quiescence.expected_cleanup_evidence_sha256
                    != pending.watchdog_retirement_evidence_sha256
                    or (
                        quiescence.acknowledgement.decision == "fired_absent"
                        and (
                            pending.container_id is not None
                            or pending.create_submission is not None
                        )
                    )
                    or (
                        quiescence.acknowledgement.decision == "fired_stopped"
                        and (
                            pending.container_id is None
                            or pending.create_submission is None
                            or quiescence.acknowledgement.container_id != pending.container_id
                        )
                    )
                ):
                    raise OCIJournalError("watchdog cleanup quiescence changed during exact replay")
            elif quiescence is not None:
                raise OCIJournalError("watchdog cleanup quiescence exists without an armed job")
            if completed is None and pending.container_id is not None:
                if inspection is None:
                    pass
                else:
                    try:
                        observed_id = self._created_never_started_container_id(
                            inspection,
                            config=config,
                        )
                    except OCIEngineError:
                        return self._inspection(
                            state=RuntimeInspectionState.UNKNOWN,
                            preparation=preparation,
                            control=control,
                            inspected_at=self._utc_now(),
                            inspected_monotonic_ns=self._clock.monotonic_ns(),
                            identity=None,
                        )
                    if observed_id != pending.container_id:
                        raise OCIJournalError("OCI cleanup container identity changed")
                    self._remove_created_container(observed_id)
            final_inspection = self._engine_inspect(config.container_name, optional=True)
            if final_inspection is not None:
                return self._inspection(
                    state=RuntimeInspectionState.UNKNOWN,
                    preparation=preparation,
                    control=control,
                    inspected_at=self._utc_now(),
                    inspected_monotonic_ns=self._clock.monotonic_ns(),
                    identity=None,
                )
            exact_absence_sha256 = self._exact_engine_absence_sha256(config.container_name)
            if completed is None:
                completed = _NeverStartedCleanupCompleted(
                    preparation_sha256=preparation.preparation_sha256,
                    cleanup_absence_epoch=cleanup_epoch,
                    pending_journal_sha256=pending.journal_sha256,
                    authorization_request_sha256=authorization_request.request_sha256,
                    runtime_launch_authorization_sha256=authorization.authorization_sha256,
                    deleted_container_id=pending.container_id,
                    watchdog_retirement_evidence_sha256=(
                        pending.watchdog_retirement_evidence_sha256
                    ),
                    watchdog_cleanup_quiescence_journal_sha256=(
                        quiescence.journal_sha256 if quiescence is not None else None
                    ),
                    exact_engine_absence_sha256=exact_absence_sha256,
                    completed_at=self._utc_now(),
                    completed_boottime_ns=self._boottime_ns(),
                )
                self._publish_model(completed_path, completed)
            elif (
                completed.preparation_sha256 != preparation.preparation_sha256
                or completed.cleanup_absence_epoch != cleanup_epoch
                or completed.pending_journal_sha256 != pending.journal_sha256
                or completed.authorization_request_sha256 != authorization_request.request_sha256
                or completed.runtime_launch_authorization_sha256
                != authorization.authorization_sha256
                or completed.deleted_container_id != pending.container_id
                or completed.watchdog_retirement_evidence_sha256
                != pending.watchdog_retirement_evidence_sha256
                or completed.watchdog_cleanup_quiescence_journal_sha256
                != (quiescence.journal_sha256 if quiescence is not None else None)
                or completed.exact_engine_absence_sha256 != exact_absence_sha256
            ):
                raise OCIJournalError("completed OCI cleanup changed during exact replay")
            return self._inspection(
                state=RuntimeInspectionState.ABSENT,
                preparation=preparation,
                control=control,
                inspected_at=self._utc_now(),
                inspected_monotonic_ns=self._clock.monotonic_ns(),
                identity=None,
                prelaunch_absence_journal_sha256=completed.journal_sha256,
                prelaunch_absence_epoch=cleanup_epoch,
                prelaunch_authorization_request_sha256=(authorization_request.request_sha256),
                prelaunch_authorization_sha256=authorization.authorization_sha256,
            )

    def _recover_unjournaled_running_launch(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        observed_capability: OCIProductionCapability,
    ) -> RuntimeLaunchEvidence | None:
        """Recover only an exact live process whose actual start fell inside its old ticket."""

        authority = self._runtime_control_authority
        if authority is None:  # pragma: no cover - validated on public entry
            raise OCIProductionCapabilityError(
                "OCI launch recovery requires a pinned runtime-control authority"
            )
        with self._runtime_lock(runtime_root):
            self._assert_launch_generation_not_under_cleanup(
                runtime_root=runtime_root,
                authorization_request=authorization_request,
            )
            control = self._current_control(runtime_root)
            self._validate_current_request(request, preparation, control)
            existing_journal = self._load_model(
                runtime_root / "engine-launch.json",
                _EngineLaunchJournal,
                optional=True,
            )
            if existing_journal is not None:
                stored_capability = self._load_required(
                    runtime_root / "production-capability.json",
                    OCIProductionCapability,
                )
                self._validate_capability_replay(
                    stored=stored_capability,
                    observed=observed_capability,
                    request=request,
                )
                existing_evidence = self._load_model(
                    runtime_root / "launch-evidence.json",
                    RuntimeLaunchEvidence,
                    optional=True,
                )
                if existing_evidence is not None:
                    self._validate_persisted_launch(
                        runtime_root=runtime_root,
                        request=request,
                        preparation=preparation,
                        config=config,
                        evidence=existing_evidence,
                        authorization_request_sha256=authorization_request.request_sha256,
                        authorization_sha256=authorization.authorization_sha256,
                        authorization_request=authorization_request,
                        authorization=authorization,
                    )
                return self._complete_launch_evidence_from_journal(
                    runtime_root=runtime_root,
                    request=request,
                    preparation=preparation,
                    config=config,
                    engine_journal=existing_journal,
                    capability=stored_capability,
                    authorization_request_sha256=authorization_request.request_sha256,
                    authorization_sha256=authorization.authorization_sha256,
                    authorization_request=authorization_request,
                    authorization=authorization,
                    publish_canonical=False,
                    return_none_when_not_running=True,
                )
            pending = self._load_model(
                runtime_root / "launch-pending.json",
                _LaunchPending,
                optional=True,
            )
            if pending is None:
                return None
            if (
                pending.runtime_request_sha256 != request.runtime_request_sha256
                or pending.oci_config_sha256 != config.oci_config_sha256
                or pending.authorization_request_sha256 != authorization_request.request_sha256
                or pending.runtime_launch_authorization_sha256 != authorization.authorization_sha256
            ):
                raise OCIJournalError("OCI launch recovery differs from durable pending authority")
            stored_capability = self._load_model(
                runtime_root / "production-capability.json",
                OCIProductionCapability,
                optional=True,
            )
            gate = self._load_model(
                runtime_root / "control" / "launch-authorization.json",
                _LaunchGateAuthorizationJournal,
                optional=True,
            )
            watchdog = self._load_model(
                runtime_root / "deadline-watchdog.json",
                _DeadlineWatchdogJournal,
                optional=True,
            )
            # No engine mutation is reachable until all three phase records exist.  A crash before
            # that point remains an ordinary fresh-ticket retry, not a historical recovery.
            if stored_capability is None or gate is None or watchdog is None:
                return None
            self._validate_capability_replay(
                stored=stored_capability,
                observed=observed_capability,
                request=request,
            )
            if (
                gate.preparation_sha256 != preparation.preparation_sha256
                or gate.authorization_request != authorization_request
                or gate.authorization != authorization
                or gate.runtime_control_authority != authority.pin
                or gate.launch_gate_executable_sha256 != self._policy.launch_gate_executable_sha256
                or gate.launch_gate_protocol_sha256 != self._policy.launch_gate_protocol_sha256
            ):
                raise OCIJournalError("OCI launch recovery changed its exact gate authority")
            watchdog = self._ensure_deadline_watchdog(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                config=config,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            create_command = self.build_create_command(request=request)
            start_command = (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "start",
                config.container_name,
            )
            create_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="create",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=create_command,
            )
            start_submission = self._load_engine_submission(
                runtime_root=runtime_root,
                phase="start",
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=start_command,
            )
            inspection = self._engine_inspect(config.container_name, optional=True)
            if inspection is None:
                return None
            self._validate_engine_configuration(inspection, config=config)
            state = inspection.get("State")
            if not isinstance(state, dict):
                raise OCIEngineError("OCI recovery inspection omitted typed process state")
            if state.get("Running") is not True:
                return None
            if create_submission is None or start_submission is None:
                raise OCIJournalError(
                    "historical running recovery lacks exact create/start submissions"
                )
            journal = self._launch_journal(
                request=request,
                preparation=preparation,
                inspection=inspection,
                authorization_sha256=authorization.authorization_sha256,
                production_capability_sha256=stored_capability.capability_sha256,
                launch_gate_authorization_journal_sha256=gate.journal_sha256,
                deadline_watchdog_journal_sha256=watchdog.journal_sha256,
                create_submission_journal_sha256=create_submission.journal_sha256,
                start_submission_journal_sha256=start_submission.journal_sha256,
            )
            self._verify_actual_launch_window(
                journal=journal,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
            )
            self._publish_model(runtime_root / "engine-launch.json", journal)
            evidence = self._compose_launch_evidence(
                request=request,
                preparation=preparation,
                config=config,
                journal=journal,
                capability=stored_capability,
            )
            self._validate_launch_evidence(
                evidence,
                preparation,
                authorization_sha256=authorization.authorization_sha256,
            )
            self._publish_model(runtime_root / "launch-evidence.json", evidence)
            return evidence

    def _validate_launch_authorization_history(
        self,
        *,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> tuple[RuntimeLaunchAuthorizationRequest, RuntimeLaunchAuthorization]:
        if self._runtime_control_authority is None:
            raise OCIProductionCapabilityError(
                "OCI launch requires a deployment-pinned runtime-control authority"
            )
        try:
            authorization_request = RuntimeLaunchAuthorizationRequest.model_validate(
                authorization_request.model_dump(mode="python")
            )
            authorization = RuntimeLaunchAuthorization.model_validate(
                authorization.model_dump(mode="python")
            )
            # Re-evaluate the complete scope and historical signature without treating an expired
            # ticket as fresh launch authority.  Actual start-window proof is separate and is
            # required before publishing recovered engine evidence.
            verify_runtime_launch_authorization_ticket_historical(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
            )
        except (
            AttributeError,
            TypeError,
            ValidationError,
            ValueError,
            QualificationVerificationError,
        ) as exc:
            raise OCIProductionCapabilityError(
                "runtime launch authorization is not an exact historical ticket"
            ) from exc
        return authorization_request, authorization

    def _verify_actual_launch_window(
        self,
        *,
        journal: _EngineLaunchJournal,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> None:
        authority = self._runtime_control_authority
        if authority is None:  # pragma: no cover - validated at public entry
            raise OCIProductionCapabilityError(
                "OCI launch evidence requires a pinned runtime-control authority"
            )
        try:
            verify_runtime_launch_authorization_historical(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=authority,
                started_at=journal.started_at,
                started_monotonic_lower_bound_ns=(journal.started_monotonic_lower_bound_ns),
                started_monotonic_upper_bound_exclusive_ns=(
                    journal.started_monotonic_upper_bound_exclusive_ns
                ),
            )
        except (QualificationVerificationError, ValidationError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "actual OCI start falls outside its signed launch ticket"
            ) from exc

    @staticmethod
    def _engine_submission_path(runtime_root: Path, phase: Literal["create", "start"]) -> Path:
        return runtime_root / f"engine-{phase}-submitted.json"

    def _load_engine_submission(
        self,
        *,
        runtime_root: Path,
        phase: Literal["create", "start"],
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        command: tuple[str, ...],
    ) -> _EngineMutationSubmission | None:
        observed = self._load_model(
            self._engine_submission_path(runtime_root, phase),
            _EngineMutationSubmission,
            optional=True,
        )
        if observed is not None and (
            observed.preparation_sha256 != preparation.preparation_sha256
            or observed.authorization_request_sha256 != authorization_request.request_sha256
            or observed.runtime_launch_authorization_sha256 != authorization.authorization_sha256
            or observed.phase != phase
            or observed.command_sha256
            != canonical_sha256(
                {
                    "schema": "aletheia.oci_engine_mutation_command.v2",
                    "phase": phase,
                    "command": command,
                }
            )
        ):
            raise OCIJournalError("OCI engine submission marker changed exact mutation scope")
        return observed

    def _publish_engine_submission(
        self,
        *,
        runtime_root: Path,
        phase: Literal["create", "start"],
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        command: tuple[str, ...],
    ) -> None:
        if (
            self._load_engine_submission(
                runtime_root=runtime_root,
                phase=phase,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                command=command,
            )
            is not None
        ):
            raise OCIJournalError("OCI engine mutation was already durably submitted")
        marker = _EngineMutationSubmission(
            preparation_sha256=preparation.preparation_sha256,
            authorization_request_sha256=authorization_request.request_sha256,
            runtime_launch_authorization_sha256=authorization.authorization_sha256,
            phase=phase,
            command_sha256=canonical_sha256(
                {
                    "schema": "aletheia.oci_engine_mutation_command.v2",
                    "phase": phase,
                    "command": command,
                }
            ),
            submitted_at=self._utc_now(),
            submitted_boottime_ns=self._boottime_ns(),
        )
        self._publish_model(self._engine_submission_path(runtime_root, phase), marker)

    def _required_engine_submissions(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        config: OCIConfiguration,
    ) -> tuple[_EngineMutationSubmission, _EngineMutationSubmission]:
        create_command = self.build_create_command(request=request)
        start_command = (
            self._policy.runtime_binary_path,
            "--host",
            self._policy.engine_endpoint,
            "start",
            config.container_name,
        )
        create = self._load_engine_submission(
            runtime_root=runtime_root,
            phase="create",
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            command=create_command,
        )
        start = self._load_engine_submission(
            runtime_root=runtime_root,
            phase="start",
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            command=start_command,
        )
        if create is None or start is None:
            raise OCIJournalError("OCI engine launch lacks exact create/start submissions")
        return create, start

    @staticmethod
    def _cleanup_paths(runtime_root: Path, absence_epoch: int) -> tuple[Path, Path]:
        if absence_epoch < 1:
            raise OCIJournalError("OCI cleanup absence epoch must be positive")
        base = runtime_root / "cleanup" / f"absence-{absence_epoch}"
        return base.with_name(f"{base.name}-pending.json"), base.with_name(
            f"{base.name}-completed.json"
        )

    def _load_completed_cleanup(
        self,
        *,
        runtime_root: Path,
        absence_epoch: int,
    ) -> tuple[_NeverStartedCleanupPending, _NeverStartedCleanupCompleted] | None:
        pending_path, completed_path = self._cleanup_paths(runtime_root, absence_epoch)
        completed = self._load_model(
            completed_path,
            _NeverStartedCleanupCompleted,
            optional=True,
        )
        if completed is None:
            return None
        pending = self._load_required(pending_path, _NeverStartedCleanupPending)
        if (
            pending.cleanup_absence_epoch != absence_epoch
            or completed.preparation_sha256 != pending.preparation_sha256
            or completed.cleanup_absence_epoch != absence_epoch
            or completed.pending_journal_sha256 != pending.journal_sha256
            or completed.authorization_request_sha256 != pending.authorization_request_sha256
            or completed.runtime_launch_authorization_sha256
            != pending.runtime_launch_authorization_sha256
            or completed.deleted_container_id != pending.container_id
            or completed.watchdog_retirement_evidence_sha256
            != pending.watchdog_retirement_evidence_sha256
        ):
            raise OCIJournalError("completed OCI cleanup changed its exact pending generation")
        return pending, completed

    def _assert_launch_generation_not_under_cleanup(
        self,
        *,
        runtime_root: Path,
        authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> None:
        pending_path, completed_path = self._cleanup_paths(
            runtime_root,
            authorization_request.pre_runtime_absence_epoch + 1,
        )
        if (
            self._read_blob(pending_path, optional=True) is not None
            or self._read_blob(completed_path, optional=True) is not None
        ):
            raise OCIJournalError("OCI launch generation is already under never-started cleanup")

    def _activate_authorized_launch_generation(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> None:
        with self._runtime_lock(runtime_root):
            self._activate_authorized_launch_generation_locked(
                runtime_root=runtime_root,
                request=request,
                preparation=preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                pre_runtime_absence_receipt=pre_runtime_absence_receipt,
            )

    def _activate_authorized_launch_generation_locked(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> None:
        epoch = authorization_request.pre_runtime_absence_epoch
        if epoch == 0:
            if pre_runtime_absence_receipt is not None:
                raise OCIJournalError("initial OCI launch cannot consume an absence receipt")
            pending_path, completed_path = self._cleanup_paths(runtime_root, 1)
            if (
                self._read_blob(pending_path, optional=True) is not None
                or self._read_blob(completed_path, optional=True) is not None
            ):
                raise OCIJournalError("cleaned OCI launch generation cannot be restarted")
            return
        if pre_runtime_absence_receipt is None:
            raise OCIJournalError("replacement OCI launch omitted its full absence receipt")
        try:
            receipt = PreRuntimeAbsenceReceipt.model_validate(
                pre_runtime_absence_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise OCIJournalError(
                "replacement OCI absence receipt failed closed validation"
            ) from exc
        evidence = receipt.absence_evidence
        if (
            authorization_request.pre_runtime_absence_receipt_sha256
            != receipt.absence_receipt_sha256
            or receipt.preparation != preparation
            or receipt.preparation_sha256 != preparation.preparation_sha256
            or receipt.node_manifest_sha256 != preparation.node_manifest_sha256
            or not receipt.signed_at
            <= authorization_request.requested_at
            <= authorization.issued_at
            < authorization.expires_at
            <= receipt.expires_at
            or evidence.inspected_monotonic_ns > authorization_request.requested_monotonic_ns
            or evidence.prelaunch_absence_epoch != epoch
            or evidence.preparation_sha256 != preparation.preparation_sha256
            or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
            or evidence.input_materialization_receipt_sha256
            != preparation.input_materialization_receipt_sha256
            or evidence.runtime_control_journal_sha256
            != self._current_control(runtime_root).control_journal_sha256
        ):
            raise OCIJournalError("replacement OCI launch differs from signed local absence")
        next_cleanup_pending, next_cleanup_completed = self._cleanup_paths(
            runtime_root,
            epoch + 1,
        )
        if (
            self._read_blob(next_cleanup_pending, optional=True) is not None
            or self._read_blob(next_cleanup_completed, optional=True) is not None
        ):
            raise OCIJournalError("replacement OCI generation is already under cleanup")
        if evidence.prelaunch_authorization_request_sha256 is None:
            absence = self._load_required(
                runtime_root / "prelaunch-absence.json",
                _PrelaunchAbsenceJournal,
            )
            if (
                epoch != 1
                or evidence.prelaunch_authorization_sha256 is not None
                or evidence.prelaunch_absence_journal_sha256 != absence.journal_sha256
                or absence.preparation_sha256 != preparation.preparation_sha256
            ):
                raise OCIJournalError("initial OCI absence receipt changed inert preparation")
            return
        cleaned = self._load_completed_cleanup(
            runtime_root=runtime_root,
            absence_epoch=epoch,
        )
        if cleaned is None:
            raise OCIJournalError("replacement OCI launch lacks local completed cleanup")
        pending, completed = cleaned
        if (
            evidence.prelaunch_absence_journal_sha256 != completed.journal_sha256
            or evidence.prelaunch_authorization_request_sha256
            != pending.authorization_request_sha256
            or evidence.prelaunch_authorization_sha256
            != pending.runtime_launch_authorization_sha256
            or pending.preparation_sha256 != preparation.preparation_sha256
            or pending.runtime_request_sha256 != request.runtime_request_sha256
        ):
            raise OCIJournalError("replacement OCI receipt differs from completed cleanup lineage")
        self._retire_cleaned_launch_generation_locked(
            runtime_root=runtime_root,
            preparation=preparation,
            receipt=receipt,
            authorization_request=authorization_request,
            authorization=authorization,
            cleanup_pending=pending,
            cleanup_completed=completed,
        )

    def _retire_cleaned_launch_generation_locked(
        self,
        *,
        runtime_root: Path,
        preparation: RuntimePreparation,
        receipt: PreRuntimeAbsenceReceipt,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        cleanup_pending: _NeverStartedCleanupPending,
        cleanup_completed: _NeverStartedCleanupCompleted,
    ) -> None:
        epoch = cleanup_completed.cleanup_absence_epoch
        pending_path = runtime_root / "retired-launch" / f"absence-{epoch}-pending.json"
        completed_path = runtime_root / "retired-launch" / f"absence-{epoch}-completed.json"
        expected_pending = _LaunchGenerationRetirementPending(
            preparation_sha256=preparation.preparation_sha256,
            cleanup_absence_epoch=epoch,
            cleanup_completed_journal_sha256=cleanup_completed.journal_sha256,
            pre_runtime_absence_receipt_sha256=receipt.absence_receipt_sha256,
            replacement_authorization_request_sha256=authorization_request.request_sha256,
            replacement_runtime_launch_authorization_sha256=authorization.authorization_sha256,
            pending_at=authorization.issued_at,
            pending_boottime_ns=authorization_request.requested_monotonic_ns,
        )
        completed = self._load_model(
            completed_path,
            _LaunchGenerationRetirementCompleted,
            optional=True,
        )
        if completed is not None:
            stored_pending = self._load_required(
                pending_path,
                _LaunchGenerationRetirementPending,
            )
            if (
                stored_pending != expected_pending
                or completed.pending_journal_sha256 != stored_pending.journal_sha256
                or completed.preparation_sha256 != preparation.preparation_sha256
                or completed.cleanup_absence_epoch != epoch
            ):
                raise OCIJournalError("OCI retired launch generation changed during replay")
            return
        self._publish_model(pending_path, expected_pending)
        phase_files: tuple[tuple[Path, ExecutionModel | None], ...] = (
            (runtime_root / "launch-pending.json", cleanup_pending.launch_pending),
            (
                runtime_root / "control" / "launch-authorization.json",
                cleanup_pending.launch_gate_authorization,
            ),
            (
                runtime_root / "production-capability.json",
                cleanup_pending.production_capability,
            ),
            (
                runtime_root / "deadline-watchdog.json",
                cleanup_pending.deadline_watchdog,
            ),
            (
                self._engine_submission_path(runtime_root, "create"),
                cleanup_pending.create_submission,
            ),
            (self._engine_submission_path(runtime_root, "start"), None),
        )
        for path, expected in phase_files:
            observed = self._read_blob(path, optional=True)
            if observed is not None:
                if expected is None or observed != canonical_json_bytes(expected):
                    raise OCIJournalError("active OCI launch phase differs from cleaned generation")
                try:
                    path.unlink()
                    self._fsync_directory(path.parent)
                except OSError as exc:
                    raise OCIJournalError("cleaned OCI launch phase could not be retired") from exc
        retired = _LaunchGenerationRetirementCompleted(
            preparation_sha256=preparation.preparation_sha256,
            cleanup_absence_epoch=epoch,
            pending_journal_sha256=expected_pending.journal_sha256,
            retired_at=self._utc_now(),
            retired_boottime_ns=self._boottime_ns(),
        )
        self._publish_model(completed_path, retired)

    def _ensure_launch_gate_authorization(
        self,
        *,
        runtime_root: Path,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> _LaunchGateAuthorizationJournal:
        if self._runtime_control_authority is None:  # pragma: no cover - validated on entry
            raise OCIProductionCapabilityError(
                "OCI launch gate requires a pinned runtime-control authority"
            )
        path = runtime_root / "control" / "launch-authorization.json"
        existing = self._load_model(path, _LaunchGateAuthorizationJournal, optional=True)
        if existing is None:
            existing = _LaunchGateAuthorizationJournal(
                preparation_sha256=preparation.preparation_sha256,
                authorization_request=authorization_request,
                authorization_request_sha256=authorization_request.request_sha256,
                authorization=authorization,
                runtime_launch_authorization_sha256=authorization.authorization_sha256,
                runtime_control_authority=self._runtime_control_authority.pin,
                launch_gate_executable_sha256=(self._policy.launch_gate_executable_sha256),
                launch_gate_protocol_sha256=self._policy.launch_gate_protocol_sha256,
                published_at=self._utc_now(),
                published_boottime_ns=self._boottime_ns(),
            )
            self._publish_model(path, existing)
        if (
            existing.preparation_sha256 != preparation.preparation_sha256
            or existing.authorization_request != authorization_request
            or existing.authorization != authorization
            or existing.runtime_control_authority != self._runtime_control_authority.pin
            or existing.launch_gate_executable_sha256 != self._policy.launch_gate_executable_sha256
            or existing.launch_gate_protocol_sha256 != self._policy.launch_gate_protocol_sha256
        ):
            raise OCIJournalError("OCI launch gate is bound to another signed ticket")
        return existing

    def _verify_launch_authorization_fresh(
        self,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> None:
        if self._runtime_control_authority is None:  # pragma: no cover - checked on entry
            raise OCIProductionCapabilityError(
                "OCI launch requires a deployment-pinned runtime-control authority"
            )
        observed_at = self._utc_now()
        self._require_request_deadline(request, observed_at=observed_at)
        observed_boottime_ns = self._boottime_ns()
        try:
            verify_runtime_launch_authorization(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
                observed_at=observed_at,
                observed_monotonic_ns=observed_boottime_ns,
            )
        except (QualificationVerificationError, ValidationError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "runtime launch authorization is expired, delayed, or out of scope"
            ) from exc

    def _boottime_ns(self) -> int:
        try:
            value = self._clock.boottime_ns()
        except OCIProductionCapabilityError:
            raise
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "production launch requires suspend-aware Linux CLOCK_BOOTTIME"
            ) from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OCIProductionCapabilityError(
                "production launch returned an invalid suspend-aware BOOTTIME"
            )
        return value

    def _require_request_deadline(
        self,
        request: OCIExecutionPlan,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if (observed_at or self._utc_now()) >= request.deadline:
            raise OCIProductionCapabilityError(
                "OCI launch mutation is outside the exact runtime hard deadline"
            )

    def probe_production_capability(
        self,
        *,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
    ) -> OCIProductionCapability:
        """Prove exact Linux/cgroup-v2/engine inputs; Darwin always fails here."""

        request = self._coerce_request(request)
        if sys.platform != "linux":
            raise OCIProductionCapabilityError(
                "real OCI qualification launch requires a Linux host"
            )
        cgroup_controllers_path = Path("/sys/fs/cgroup/cgroup.controllers")
        mountinfo_path = Path("/proc/self/mountinfo")
        boot_id_path = Path("/proc/sys/kernel/random/boot_id")
        try:
            mountinfo = mountinfo_path.read_bytes()
            controllers = tuple(sorted(cgroup_controllers_path.read_text().split()))
            boot_id = boot_id_path.read_text().strip()
        except OSError as exc:
            raise OCIProductionCapabilityError(
                "Linux host does not expose exact cgroup-v2 capability"
            ) from exc
        if b" - cgroup2 " not in mountinfo or not {"cpu", "memory", "pids"}.issubset(controllers):
            raise OCIProductionCapabilityError(
                "Linux host lacks unified cpu/memory/pids cgroup-v2 controllers"
            )
        if boot_id != request.boot_id:
            raise OCIProductionCapabilityError("runtime request boot id differs from Linux boot")
        with self._pinned_runtime_binary() as descriptor:
            del descriptor
        self._ensure_seccomp_copy()
        self._verify_seccomp_copy()
        binary_hash = self._policy.runtime_binary_sha256
        seccomp_hash = self._policy.seccomp_profile_sha256
        info_payload = self._run_engine(
            (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "info",
                "--format",
                "{{json .}}",
            )
        )
        try:
            info = json.loads(info_payload)
        except json.JSONDecodeError as exc:
            raise OCIProductionCapabilityError("OCI engine info is not canonical JSON") from exc
        if (
            not isinstance(info, dict)
            or str(info.get("CgroupVersion")) != "2"
            or info.get("OSType") != "linux"
        ):
            raise OCIProductionCapabilityError(
                "OCI engine is not operating on the exact Linux cgroup-v2 substrate"
            )
        engine_security_projection = self._engine_security_projection(info)
        observed_at = self._utc_now()
        output_quota_evidence_sha256 = self._verify_output_quota(request)
        launch_gate_attestation_sha256 = self._verify_launch_gate()
        return OCIProductionCapability(
            node_id=request.node_id,
            boot_id=request.boot_id,
            cgroup_controllers=("cpu", "memory", "pids"),
            cgroup_mount_sha256=hashlib.sha256(mountinfo).hexdigest(),
            runtime_binary_sha256=binary_hash,
            seccomp_profile_sha256=seccomp_hash,
            engine_info_sha256=canonical_sha256(engine_security_projection),
            output_quota_evidence_sha256=output_quota_evidence_sha256,
            launch_gate_attestation_sha256=launch_gate_attestation_sha256,
            observed_at=observed_at,
            observed_monotonic_ns=self._clock.monotonic_ns(),
        )

    def _engine_security_projection(self, info: dict[str, object]) -> dict[str, object]:
        security_options = info.get("SecurityOptions")
        runtimes = info.get("Runtimes")
        required_strings = {
            name: info.get(name)
            for name in (
                "Architecture",
                "CgroupDriver",
                "DockerRootDir",
                "Driver",
                "ID",
                "KernelVersion",
                "Name",
                "OperatingSystem",
                "ServerVersion",
            )
        }
        if (
            not isinstance(security_options, list)
            or not all(isinstance(item, str) for item in security_options)
            or not isinstance(runtimes, dict)
            or self._policy.low_level_runtime not in runtimes
            or any(not isinstance(value, str) or not value for value in required_strings.values())
            or info.get("OSType") != "linux"
            or str(info.get("CgroupVersion")) != "2"
            or info.get("CgroupDriver") not in {"cgroupfs", "systemd"}
            or not Path(str(info.get("DockerRootDir"))).is_absolute()
            or not any(item.startswith("name=seccomp") for item in security_options)
            or "name=apparmor" not in security_options
            or "name=cgroupns" not in security_options
        ):
            raise OCIProductionCapabilityError(
                "OCI daemon lacks exact seccomp/AppArmor/cgroup namespace capability"
            )
        return {
            "schema": "aletheia.oci_daemon_security_projection.v2",
            **required_strings,
            "OSType": "linux",
            "CgroupVersion": "2",
            "DefaultRuntime": info.get("DefaultRuntime"),
            "selected_low_level_runtime": {
                "name": self._policy.low_level_runtime,
                "configuration": runtimes[self._policy.low_level_runtime],
            },
            "SecurityOptions": tuple(sorted(security_options)),
        }

    def _validate_request(self, request: OCIExecutionPlan) -> OCIExecutionPlan:
        try:
            request = OCIExecutionPlan.model_validate(request.model_dump(mode="python"))
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise OCIPolicyRejected("OCI request failed closed-model validation") from exc
        spec = request.launch_spec
        policy = self._policy
        if (
            request.policy_sha256 != policy.policy_sha256
            or request.runtime_engine != policy.runtime_engine
            or request.launch_spec_sha256 != policy.launch_spec_sha256
            or spec.capability_manifest_sha256 != policy.capability_manifest_sha256
            or spec.command_sha256 != policy.command_sha256
            or spec.environment_sha256 != policy.environment_sha256
            or spec.executable_sha256 != policy.executable_sha256
            or spec.network_policy.value != "none"
            or not spec.direct_exec_only
            or spec.inherit_host_environment
            or spec.privileged
            or not spec.read_only_root_filesystem
            or not spec.input_mount_read_only
            or not spec.output_mount_only_writable
            or spec.docker_socket_mounted
            or spec.database_credentials_mounted
            or spec.artifact_store_credentials_mounted
            or spec.node_signing_key_mounted
        ):
            raise OCIPolicyRejected("OCI request differs from exact deployment launch policy")
        return request

    def _coerce_request(
        self,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
    ) -> OCIExecutionPlan:
        if isinstance(request, OCIExecutionPlan):
            return self._validate_request(request)
        if not isinstance(request, RuntimeLaunchRequest):
            raise OCIPolicyRejected("OCI adapter accepts only the typed runtime request")
        resolved_devices: list[OCIDeviceBinding] = []
        for lease in request.device_leases:
            pin = self._device_path_pins.get((lease.device_id, lease.hardware_uuid))
            if pin is None:
                raise OCIPolicyRejected(
                    "allocator device lease has no exact deployment device-path pin"
                )
            resolved_devices.append(
                OCIDeviceBinding(
                    device_id=lease.device_id,
                    hardware_uuid=lease.hardware_uuid,
                    host_device_path=pin.host_device_path,
                    container_device_path=pin.container_device_path,
                    requested_memory_bytes=lease.requested_memory_bytes,
                    fencing_epoch=lease.fencing_epoch,
                    device_policy_sha256=pin.device_policy_sha256,
                    access=pin.access,
                )
            )
        try:
            projected = OCIExecutionPlan.from_runtime_launch_request(
                request=request,
                policy=self._policy,
                device_bindings=tuple(resolved_devices),
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise OCIPolicyRejected(
                "node runtime request cannot be exactly projected to OCI"
            ) from exc
        return self._validate_request(projected)

    def _validate_prepared(
        self,
        request: OCIExecutionPlan | RuntimeLaunchRequest,
        preparation: RuntimePreparation,
    ) -> tuple[OCIExecutionPlan, RuntimePreparation, Path]:
        request = self._coerce_request(request)
        try:
            preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise OCIJournalError("runtime preparation failed closed-model validation") from exc
        runtime_root = self._runtime_path(request)
        expected = self._load_required(runtime_root / "preparation.json", RuntimePreparation)
        initial_plan = self._load_required(runtime_root / "plan.json", OCIExecutionPlan)
        if expected != preparation or (
            preparation.node_manifest_sha256 != initial_plan.node_manifest_sha256
            or preparation.node_id != initial_plan.node_id
            or preparation.boot_id != initial_plan.boot_id
            or preparation.execution_id != initial_plan.execution_id
            or preparation.infrastructure_attempt_id != initial_plan.infrastructure_attempt_id
            or preparation.intent_sha256 != initial_plan.intent_sha256
            or preparation.runtime_id != initial_plan.runtime_id
            or preparation.runtime_engine != initial_plan.runtime_engine
            or preparation.launch_spec_sha256 != initial_plan.launch_spec_sha256
            or preparation.workload_executable_sha256 != initial_plan.launch_spec.executable_sha256
            or preparation.workload_argv != initial_plan.launch_spec.argv
            or preparation.runtime_request_sha256 != initial_plan.runtime_request_sha256
            or preparation.enforced_placement_sha256 != initial_plan.enforced_placement_sha256
            or preparation.input_materialization_receipt_sha256
            != initial_plan.input_materialization_receipt_sha256
            or preparation.output_quota_provisioning_receipt_sha256
            != initial_plan.output_quota_provisioning_receipt_sha256
            or preparation.fencing_epoch != initial_plan.fencing_epoch
            or preparation.lease_token_sha256 != initial_plan.lease_token_sha256
            or self._stable_plan_scope(request) != self._stable_plan_scope(initial_plan)
        ):
            raise OCIJournalError("runtime preparation differs from exact OCI request")
        return request, preparation, runtime_root

    @staticmethod
    def _stable_plan_scope(request: OCIExecutionPlan) -> dict[str, object]:
        payload = request.model_dump(
            mode="json",
            exclude={
                "runtime_request_sha256",
                "fencing_epoch",
                "lease_token_sha256",
                "device_bindings",
            },
        )
        payload["devices"] = tuple(
            item.model_dump(mode="json", exclude={"fencing_epoch"})
            for item in request.device_bindings
        )
        return payload

    @staticmethod
    def _validate_current_request(
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        control: _RuntimeControlJournal,
    ) -> None:
        expected_devices = tuple(
            (item.device_id, item.hardware_uuid, item.fencing_epoch)
            for item in request.device_bindings
        )
        if (
            control.preparation_sha256 != preparation.preparation_sha256
            or control.enforced_placement_sha256 != preparation.enforced_placement_sha256
            or request.enforced_placement_sha256 != preparation.enforced_placement_sha256
            or control.fencing_epoch != request.fencing_epoch
            or control.lease_token_sha256 != request.lease_token_sha256
            or control.device_fences != expected_devices
        ):
            raise OCIJournalError(
                "incoming runtime request differs from current durable fence control"
            )

    @staticmethod
    def _validate_identity(
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity,
    ) -> None:
        if (
            identity.node_id != preparation.node_id
            or identity.boot_id != preparation.boot_id
            or identity.execution_id != preparation.execution_id
            or identity.infrastructure_attempt_id != preparation.infrastructure_attempt_id
            or identity.runtime_id != preparation.runtime_id
            or identity.runtime_engine != preparation.runtime_engine
            or identity.launch_spec_sha256 != preparation.launch_spec_sha256
            or identity.started_at < preparation.prepared_at
            or identity.started_monotonic_ns < preparation.prepared_monotonic_ns
        ):
            raise OCIJournalError("runtime identity differs from inert preparation")

    @classmethod
    def _validate_launch_evidence(
        cls,
        evidence: RuntimeLaunchEvidence,
        preparation: RuntimePreparation,
        *,
        authorization_sha256: str | None = None,
    ) -> None:
        cls._validate_identity(preparation, evidence.runtime_identity)
        if (
            evidence.preparation_sha256 != preparation.preparation_sha256
            or evidence.runtime_identity_sha256 != evidence.runtime_identity.runtime_identity_sha256
            or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
            or evidence.input_materialization_receipt_sha256
            != preparation.input_materialization_receipt_sha256
            or evidence.enforced_fencing_epoch != preparation.fencing_epoch
            or evidence.enforced_lease_token_sha256 != preparation.lease_token_sha256
            or (
                authorization_sha256 is not None
                and evidence.runtime_launch_authorization_sha256 != authorization_sha256
            )
        ):
            raise OCIJournalError("runtime launch evidence differs from exact preparation")

    def _validate_persisted_launch(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        evidence: RuntimeLaunchEvidence,
        authorization_request_sha256: str,
        authorization_sha256: str,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> None:
        self._validate_launch_evidence(
            evidence,
            preparation,
            authorization_sha256=authorization_sha256,
        )
        pending = self._load_required(runtime_root / "launch-pending.json", _LaunchPending)
        capability = self._load_required(
            runtime_root / "production-capability.json", OCIProductionCapability
        )
        gate_authorization = self._load_required(
            runtime_root / "control" / "launch-authorization.json",
            _LaunchGateAuthorizationJournal,
        )
        watchdog = self._ensure_deadline_watchdog(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        journal = self._load_required(runtime_root / "engine-launch.json", _EngineLaunchJournal)
        create_submission, start_submission = self._required_engine_submissions(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            config=config,
        )
        self._validate_capability_scope(capability, request)
        if (
            pending.runtime_request_sha256 != request.runtime_request_sha256
            or pending.oci_config_sha256 != config.oci_config_sha256
            or pending.authorization_request_sha256 != authorization_request_sha256
            or pending.runtime_launch_authorization_sha256 != authorization_sha256
            or journal.preparation_sha256 != preparation.preparation_sha256
            or journal.runtime_launch_authorization_sha256 != authorization_sha256
            or journal.production_capability_sha256 != capability.capability_sha256
            or journal.launch_gate_authorization_journal_sha256 != gate_authorization.journal_sha256
            or journal.deadline_watchdog_journal_sha256 != watchdog.journal_sha256
            or journal.create_submission_journal_sha256 != create_submission.journal_sha256
            or journal.start_submission_journal_sha256 != start_submission.journal_sha256
            or gate_authorization.preparation_sha256 != preparation.preparation_sha256
            or gate_authorization.authorization_request_sha256 != authorization_request_sha256
            or gate_authorization.runtime_launch_authorization_sha256 != authorization_sha256
            or journal.journal_sha256 != evidence.engine_launch_journal_sha256
            or journal.sandbox_instance_sha256 != evidence.runtime_identity.sandbox_instance_sha256
            or journal.process_identity_sha256 != evidence.runtime_identity.process_identity_sha256
            or journal.process_identity_sha256
            != self._expected_process_identity_sha256(request, journal)
            or journal.workload_executable_sha256 != self._policy.executable_sha256
            or journal.workload_argv_sha256 != self._expected_workload_argv_sha256(request)
        ):
            raise OCIJournalError(
                "persisted OCI launch differs from its exact ticket or engine journal"
            )
        self._verify_actual_launch_window(
            journal=journal,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )

    def _validate_capability_scope(
        self,
        capability: OCIProductionCapability,
        request: OCIExecutionPlan,
    ) -> None:
        if (
            capability.node_id != request.node_id
            or capability.boot_id != request.boot_id
            or capability.runtime_binary_sha256 != self._policy.runtime_binary_sha256
            or capability.seccomp_profile_sha256 != self._policy.seccomp_profile_sha256
            or capability.output_quota_evidence_sha256
            != self._expected_output_quota_evidence_sha256(request)
            or capability.launch_gate_attestation_sha256
            != self._expected_launch_gate_attestation_sha256()
        ):
            raise OCIJournalError("production capability differs from the exact host/request scope")

    def _validate_capability_replay(
        self,
        *,
        stored: OCIProductionCapability,
        observed: OCIProductionCapability,
        request: OCIExecutionPlan,
    ) -> None:
        self._validate_capability_scope(stored, request)
        self._validate_capability_scope(observed, request)
        stable_exclusions = {"observed_at", "observed_monotonic_ns"}
        if stored.model_dump(mode="json", exclude=stable_exclusions) != observed.model_dump(
            mode="json", exclude=stable_exclusions
        ):
            raise OCIProductionCapabilityError(
                "production capability changed during exact launch replay"
            )

    def _compose_launch_evidence(
        self,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        journal: _EngineLaunchJournal,
        capability: OCIProductionCapability,
        recovery_observation: _EngineRecoveryObservationJournal | None = None,
    ) -> RuntimeLaunchEvidence:
        if recovery_observation is not None and (
            recovery_observation.preparation_sha256 != preparation.preparation_sha256
            or recovery_observation.engine_launch_journal_sha256 != journal.journal_sha256
            or recovery_observation.runtime_launch_authorization_sha256
            != journal.runtime_launch_authorization_sha256
            or recovery_observation.sandbox_instance_sha256 != journal.sandbox_instance_sha256
            or recovery_observation.process_identity_sha256 != journal.process_identity_sha256
            or recovery_observation.started_at != journal.started_at
            or recovery_observation.started_monotonic_lower_bound_ns
            != journal.started_monotonic_lower_bound_ns
            or recovery_observation.started_monotonic_upper_bound_exclusive_ns
            != journal.started_monotonic_upper_bound_exclusive_ns
        ):
            raise OCIJournalError(
                "fresh OCI recovery observation differs from immutable engine start"
            )
        observed_at = (
            recovery_observation.observed_at
            if recovery_observation is not None
            else journal.observed_at
        )
        observed_monotonic_ns = (
            recovery_observation.observed_monotonic_ns
            if recovery_observation is not None
            else journal.observed_monotonic_ns
        )
        identity = NodeRuntimeIdentity(
            node_id=request.node_id,
            boot_id=request.boot_id,
            execution_id=request.execution_id,
            infrastructure_attempt_id=request.infrastructure_attempt_id,
            runtime_id=request.runtime_id,
            runtime_engine=request.runtime_engine,
            launch_spec_sha256=request.launch_spec_sha256,
            sandbox_instance_sha256=journal.sandbox_instance_sha256,
            process_identity_sha256=journal.process_identity_sha256,
            started_at=journal.started_at,
            started_monotonic_ns=journal.started_monotonic_lower_bound_ns,
        )
        return RuntimeLaunchEvidence(
            preparation_sha256=preparation.preparation_sha256,
            runtime_launch_authorization_sha256=(journal.runtime_launch_authorization_sha256),
            runtime_identity=identity,
            runtime_identity_sha256=identity.runtime_identity_sha256,
            engine_start_monotonic_lower_bound_ns=(journal.started_monotonic_lower_bound_ns),
            engine_start_monotonic_upper_bound_exclusive_ns=(
                journal.started_monotonic_upper_bound_exclusive_ns
            ),
            enforced_placement_sha256=preparation.enforced_placement_sha256,
            input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
            enforced_fencing_epoch=preparation.fencing_epoch,
            enforced_lease_token_sha256=preparation.lease_token_sha256,
            engine_launch_journal_sha256=journal.journal_sha256,
            launch_evidence_sha256=canonical_sha256(
                {
                    "schema": "aletheia.oci_engine_launch_observation.v2",
                    "journal_sha256": journal.journal_sha256,
                    "runtime_launch_authorization_sha256": (
                        journal.runtime_launch_authorization_sha256
                    ),
                    "production_capability_sha256": capability.capability_sha256,
                    "oci_config_sha256": config.oci_config_sha256,
                    "recovery_observation_journal_sha256": (
                        recovery_observation.journal_sha256
                        if recovery_observation is not None
                        else None
                    ),
                }
            ),
            observed_at=observed_at,
            observed_monotonic_ns=observed_monotonic_ns,
        )

    @staticmethod
    def _expected_workload_argv_sha256(request: OCIExecutionPlan) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.linux_process_argv.v2",
                "argv": request.launch_spec.argv,
            }
        )

    @classmethod
    def _expected_process_identity_sha256(
        cls,
        request: OCIExecutionPlan,
        journal: _EngineLaunchJournal,
    ) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.linux_oci_process_identity.v2",
                "boot_id": request.boot_id,
                "container_id": journal.container_id,
                "pid": journal.pid,
                "proc_start_ticks": journal.proc_start_ticks,
                "pid_namespace_device": journal.pid_namespace_device,
                "pid_namespace_inode": journal.pid_namespace_inode,
                "proc_cgroup_sha256": journal.proc_cgroup_sha256,
                "cgroup_limits_sha256": journal.cgroup_limits_sha256,
                "workload_executable_sha256": (journal.workload_executable_sha256),
                "workload_argv_sha256": journal.workload_argv_sha256,
                "started_at": journal.started_at.isoformat(),
                "started_monotonic_lower_bound_ns": (journal.started_monotonic_lower_bound_ns),
                "started_monotonic_upper_bound_exclusive_ns": (
                    journal.started_monotonic_upper_bound_exclusive_ns
                ),
            }
        )

    def _fresh_recovery_observation(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        engine_journal: _EngineLaunchJournal,
    ) -> _EngineRecoveryObservationJournal | None:
        """Reopen an exact live runtime and bind a fresh receipt-signable observation."""

        inspection = self._engine_inspect(config.container_name, optional=True)
        if inspection is None:
            return None
        self._validate_engine_configuration(inspection, config=config)
        state = inspection.get("State")
        if not isinstance(state, dict):
            raise OCIEngineError("OCI recovery inspection omitted typed process state")
        if state.get("Running") is not True:
            return None
        candidate = self._launch_journal(
            request=request,
            preparation=preparation,
            inspection=inspection,
            authorization_sha256=engine_journal.runtime_launch_authorization_sha256,
            production_capability_sha256=engine_journal.production_capability_sha256,
            launch_gate_authorization_journal_sha256=(
                engine_journal.launch_gate_authorization_journal_sha256
            ),
            deadline_watchdog_journal_sha256=(engine_journal.deadline_watchdog_journal_sha256),
            create_submission_journal_sha256=(engine_journal.create_submission_journal_sha256),
            start_submission_journal_sha256=(engine_journal.start_submission_journal_sha256),
        )
        observation_only_fields = {
            "container_inspection_sha256",
            "observed_at",
            "observed_monotonic_ns",
        }
        if candidate.model_dump(
            mode="json", exclude=observation_only_fields
        ) != engine_journal.model_dump(mode="json", exclude=observation_only_fields):
            raise OCIJournalError(
                "live OCI recovery differs from immutable PID/workload/cgroup identity"
            )
        observation = _EngineRecoveryObservationJournal(
            preparation_sha256=preparation.preparation_sha256,
            engine_launch_journal_sha256=engine_journal.journal_sha256,
            runtime_launch_authorization_sha256=(
                engine_journal.runtime_launch_authorization_sha256
            ),
            container_inspection_sha256=candidate.container_inspection_sha256,
            sandbox_instance_sha256=candidate.sandbox_instance_sha256,
            process_identity_sha256=candidate.process_identity_sha256,
            started_at=candidate.started_at,
            started_monotonic_lower_bound_ns=(candidate.started_monotonic_lower_bound_ns),
            started_monotonic_upper_bound_exclusive_ns=(
                candidate.started_monotonic_upper_bound_exclusive_ns
            ),
            observed_at=candidate.observed_at,
            observed_monotonic_ns=candidate.observed_monotonic_ns,
        )
        self._publish_model(
            runtime_root / "launch-recovery" / f"observation-{observation.journal_sha256}.json",
            observation,
        )
        return observation

    def _complete_launch_evidence_from_journal(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        engine_journal: _EngineLaunchJournal,
        capability: OCIProductionCapability,
        authorization_request_sha256: str,
        authorization_sha256: str,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        publish_canonical: bool = True,
        return_none_when_not_running: bool = False,
    ) -> RuntimeLaunchEvidence | None:
        pending = self._load_required(runtime_root / "launch-pending.json", _LaunchPending)
        gate_authorization = self._load_required(
            runtime_root / "control" / "launch-authorization.json",
            _LaunchGateAuthorizationJournal,
        )
        watchdog = self._ensure_deadline_watchdog(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        self._validate_capability_scope(capability, request)
        create_submission, start_submission = self._required_engine_submissions(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            config=config,
        )
        expected_sandbox = canonical_sha256(
            {
                "schema": "aletheia.oci_sandbox_instance_identity.v2",
                "container_id": engine_journal.container_id,
                "image_config_sha256": self._policy.image_config_sha256,
                "oci_config_sha256": preparation.oci_config_sha256,
                "runtime_request_sha256": preparation.runtime_request_sha256,
            }
        )
        if (
            pending.runtime_request_sha256 != request.runtime_request_sha256
            or pending.oci_config_sha256 != config.oci_config_sha256
            or pending.authorization_request_sha256 != authorization_request_sha256
            or pending.runtime_launch_authorization_sha256 != authorization_sha256
            or engine_journal.preparation_sha256 != preparation.preparation_sha256
            or engine_journal.runtime_launch_authorization_sha256 != authorization_sha256
            or engine_journal.production_capability_sha256 != capability.capability_sha256
            or gate_authorization.preparation_sha256 != preparation.preparation_sha256
            or gate_authorization.authorization_request_sha256 != authorization_request_sha256
            or gate_authorization.runtime_launch_authorization_sha256 != authorization_sha256
            or gate_authorization.launch_gate_executable_sha256
            != self._policy.launch_gate_executable_sha256
            or gate_authorization.launch_gate_protocol_sha256
            != self._policy.launch_gate_protocol_sha256
            or engine_journal.launch_gate_authorization_journal_sha256
            != gate_authorization.journal_sha256
            or engine_journal.deadline_watchdog_journal_sha256 != watchdog.journal_sha256
            or engine_journal.create_submission_journal_sha256 != create_submission.journal_sha256
            or engine_journal.start_submission_journal_sha256 != start_submission.journal_sha256
            or engine_journal.sandbox_instance_sha256 != expected_sandbox
            or engine_journal.process_identity_sha256
            != self._expected_process_identity_sha256(request, engine_journal)
            or engine_journal.workload_executable_sha256 != self._policy.executable_sha256
            or engine_journal.workload_argv_sha256 != self._expected_workload_argv_sha256(request)
        ):
            raise OCIJournalError(
                "engine launch journal differs from its durable ticket/capability phase"
            )
        self._verify_actual_launch_window(
            journal=engine_journal,
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        recovery_observation = self._fresh_recovery_observation(
            runtime_root=runtime_root,
            request=request,
            preparation=preparation,
            config=config,
            engine_journal=engine_journal,
        )
        if recovery_observation is None:
            if return_none_when_not_running:
                return None
            raise OCIEngineError(
                "immutable OCI engine start is no longer available for fresh recovery"
            )
        evidence = self._compose_launch_evidence(
            request=request,
            preparation=preparation,
            config=config,
            journal=engine_journal,
            capability=capability,
            recovery_observation=recovery_observation,
        )
        self._validate_launch_evidence(
            evidence,
            preparation,
            authorization_sha256=authorization_sha256,
        )
        self._publish_model(
            runtime_root / "launch-recovery" / f"evidence-{evidence.evidence_sha256}.json",
            evidence,
        )
        if publish_canonical:
            self._publish_model(runtime_root / "launch-evidence.json", evidence)
        return evidence

    def _ensure_initial_control(
        self,
        runtime_root: Path,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
    ) -> _RuntimeControlJournal:
        initial_device_evidence = canonical_sha256(
            {
                "schema": "aletheia.oci_initial_device_fence_binding.v2",
                "preparation_sha256": preparation.preparation_sha256,
                "devices": request.device_bindings,
            }
        )
        expected = _RuntimeControlJournal(
            preparation_sha256=preparation.preparation_sha256,
            sequence=0,
            fencing_epoch=request.fencing_epoch,
            lease_token_sha256=request.lease_token_sha256,
            enforced_placement_sha256=request.enforced_placement_sha256,
            device_fences=tuple(
                (item.device_id, item.hardware_uuid, item.fencing_epoch)
                for item in request.device_bindings
            ),
            device_fence_evidence_sha256=initial_device_evidence,
        )
        path = runtime_root / "control" / "current.json"
        current = self._load_model(path, _RuntimeControlJournal, optional=True)
        if current is not None and current != expected:
            raise OCIJournalError("initial runtime control journal changed before launch")
        self._publish_model(path, expected)
        return expected

    def _current_control(self, runtime_root: Path) -> _RuntimeControlJournal:
        return self._load_required(
            runtime_root / "control" / "current.json", _RuntimeControlJournal
        )

    def _expected_device_rebind(
        self,
        request: RuntimeFenceRebindRequest,
        devices: tuple[OCIDeviceBinding, ...],
    ) -> str:
        if not devices:
            return canonical_sha256(
                {
                    "schema": "aletheia.oci_empty_device_fence_rebind.v2",
                    "request_sha256": request.request_sha256,
                }
            )
        if self._device_fence_controller is None:
            raise OCIProductionCapabilityError(
                "device fence rotation requires a deployment controller"
            )
        evidence = self._device_fence_controller.expected_rebind_evidence_sha256(
            request=request,
            devices=devices,
        )
        if re.fullmatch(_SHA256_PATTERN, evidence) is None:
            raise OCIProductionCapabilityError(
                "device controller returned a non-SHA256 expected evidence identity"
            )
        return evidence

    def _apply_initial_device_fence(
        self,
        *,
        preparation: RuntimePreparation,
        devices: tuple[OCIDeviceBinding, ...],
        expected_sha256: str,
    ) -> None:
        if not devices:
            return
        if self._device_fence_controller is None:
            raise OCIProductionCapabilityError(
                "initial device fencing requires a deployment controller"
            )
        try:
            observed = self._device_fence_controller.apply_initial_fence(
                preparation_sha256=preparation.preparation_sha256,
                devices=devices,
                expected_evidence_sha256=expected_sha256,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "device controller cannot enforce the initial fence and memory binding"
            ) from exc
        if observed != expected_sha256:
            raise OCIProductionCapabilityError(
                "device controller did not establish the exact initial fence"
            )

    def _apply_device_rebind(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        devices: tuple[OCIDeviceBinding, ...],
        expected_sha256: str,
    ) -> None:
        if not devices:
            return
        assert self._device_fence_controller is not None
        observed = self._device_fence_controller.apply_rebind(
            request=request,
            devices=devices,
            expected_evidence_sha256=expected_sha256,
        )
        if observed != expected_sha256:
            raise OCIProductionCapabilityError(
                "device fence controller did not apply the exact pending CAS"
            )

    def _inspection(
        self,
        *,
        state: RuntimeInspectionState,
        preparation: RuntimePreparation,
        control: _RuntimeControlJournal,
        inspected_at: datetime,
        inspected_monotonic_ns: int,
        identity: NodeRuntimeIdentity | None,
        prelaunch_absence_journal_sha256: str | None = None,
        prelaunch_absence_epoch: int | None = None,
        prelaunch_authorization_request_sha256: str | None = None,
        prelaunch_authorization_sha256: str | None = None,
        terminal: _EngineTerminalJournal | None = None,
    ) -> RuntimeInspectionEvidence:
        exit_code = ended_at = ended_monotonic_ns = terminal_journal_sha256 = None
        if terminal is not None:
            exit_code = terminal.observation.exit_code
            ended_at = terminal.observation.ended_at
            ended_monotonic_ns = terminal.observation.ended_monotonic_ns
            terminal_journal_sha256 = terminal.journal_sha256
        raw_evidence_sha256 = canonical_sha256(
            {
                "schema": "aletheia.oci_runtime_inspection_observation.v2",
                "state": state.value,
                "preparation_sha256": preparation.preparation_sha256,
                "runtime_identity_sha256": (
                    identity.runtime_identity_sha256 if identity is not None else None
                ),
                "runtime_control_journal_sha256": control.control_journal_sha256,
                "prelaunch_absence_journal_sha256": prelaunch_absence_journal_sha256,
                "prelaunch_absence_epoch": prelaunch_absence_epoch,
                "prelaunch_authorization_request_sha256": (prelaunch_authorization_request_sha256),
                "prelaunch_authorization_sha256": prelaunch_authorization_sha256,
                "engine_terminal_journal_sha256": terminal_journal_sha256,
                "inspected_at": inspected_at.isoformat(),
                "inspected_monotonic_ns": inspected_monotonic_ns,
            }
        )
        return RuntimeInspectionEvidence(
            state=state,
            preparation_sha256=preparation.preparation_sha256,
            runtime_identity=identity,
            runtime_identity_sha256=(
                identity.runtime_identity_sha256 if identity is not None else None
            ),
            enforced_placement_sha256=control.enforced_placement_sha256,
            input_materialization_receipt_sha256=(preparation.input_materialization_receipt_sha256),
            enforced_fencing_epoch=control.fencing_epoch,
            enforced_lease_token_sha256=control.lease_token_sha256,
            inspection_evidence_sha256=raw_evidence_sha256,
            runtime_control_journal_sha256=control.control_journal_sha256,
            prelaunch_absence_journal_sha256=prelaunch_absence_journal_sha256,
            prelaunch_absence_epoch=prelaunch_absence_epoch,
            prelaunch_authorization_request_sha256=(prelaunch_authorization_request_sha256),
            prelaunch_authorization_sha256=prelaunch_authorization_sha256,
            engine_terminal_journal_sha256=terminal_journal_sha256,
            inspected_at=inspected_at,
            inspected_monotonic_ns=inspected_monotonic_ns,
            exit_code=exit_code,
            ended_at=ended_at,
            ended_monotonic_ns=ended_monotonic_ns,
        )

    def _launch_journal(
        self,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        inspection: dict[str, object],
        authorization_sha256: str,
        production_capability_sha256: str,
        launch_gate_authorization_journal_sha256: str,
        deadline_watchdog_journal_sha256: str,
        create_submission_journal_sha256: str,
        start_submission_journal_sha256: str,
    ) -> _EngineLaunchJournal:
        container_id = inspection.get("Id")
        state = inspection.get("State")
        if not isinstance(container_id, str) or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise OCIEngineError("OCI inspection returned an invalid container id")
        if not isinstance(state, dict) or not state.get("Running"):
            raise OCIEngineError("OCI process is not running during launch evidence capture")
        pid = state.get("Pid")
        started_value = state.get("StartedAt")
        if not isinstance(pid, int) or pid < 1 or not isinstance(started_value, str):
            raise OCIEngineError("OCI inspection omitted exact PID/start evidence")
        started_at = self._parse_engine_timestamp(started_value)
        proc_stat_path = Path(f"/proc/{pid}/stat")
        proc_namespace_path = Path(f"/proc/{pid}/ns/pid")
        proc_cgroup_path = Path(f"/proc/{pid}/cgroup")
        proc_cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            proc_stat = proc_stat_path.read_text()
            proc_namespace = proc_namespace_path.stat()
            proc_cgroup = proc_cgroup_path.read_bytes()
            proc_cmdline = proc_cmdline_path.read_bytes()
            clock_ticks = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError) as exc:
            raise OCIEngineError("OCI PID disappeared before exact identity capture") from exc
        # A command name may contain spaces and parentheses; fields begin after the final ') '.
        suffix = proc_stat.rsplit(") ", 1)
        if len(suffix) != 2:
            raise OCIEngineError("Linux process stat is malformed")
        fields = suffix[1].split()
        try:
            # proc(5) field 22; suffix field zero is original field 3.
            start_ticks = int(fields[19])
        except (IndexError, ValueError) as exc:
            raise OCIEngineError("Linux process stat lacks a start-time identity") from exc
        expected_cmdline = (
            b"\x00".join(os.fsencode(item) for item in request.launch_spec.argv) + b"\x00"
        )
        if proc_cmdline != expected_cmdline:
            raise OCIEngineError("OCI launch gate has not execved the exact pinned workload argv")
        workload_executable_sha256 = self._rehash_proc_executable(pid)
        if workload_executable_sha256 != self._policy.executable_sha256:
            raise OCIEngineError("OCI launch gate has not execved the pinned workload executable")
        try:
            final_proc_stat = proc_stat_path.read_text()
        except OSError as exc:
            raise OCIEngineError(
                "OCI workload changed while exact process evidence was captured"
            ) from exc
        final_suffix = final_proc_stat.rsplit(") ", 1)
        try:
            final_start_ticks = int(final_suffix[1].split()[19])
        except (IndexError, ValueError) as exc:
            raise OCIEngineError("Linux process stat changed during identity capture") from exc
        if final_start_ticks != start_ticks:
            raise OCIEngineError("OCI PID was reused during exact identity capture")
        workload_argv_sha256 = canonical_sha256(
            {
                "schema": "aletheia.linux_process_argv.v2",
                "argv": request.launch_spec.argv,
            }
        )
        cgroup_limits_sha256 = self._verify_cgroup_v2_enforcement(
            request=request,
            container_id=container_id,
            proc_cgroup=proc_cgroup,
        )
        started_monotonic_lower_bound_ns = start_ticks * 1_000_000_000 // int(clock_ticks)
        started_monotonic_upper_bound_exclusive_ns = (
            (start_ticks + 1) * 1_000_000_000 + int(clock_ticks) - 1
        ) // int(clock_ticks)
        observed_at = self._utc_now()
        observed_monotonic_ns = self._clock.monotonic_ns()
        if (
            observed_at < started_at
            or observed_monotonic_ns < started_monotonic_upper_bound_exclusive_ns
            or started_at < preparation.prepared_at
            or started_monotonic_lower_bound_ns < preparation.prepared_monotonic_ns
        ):
            raise OCIEngineError("OCI start identity is out of preparation/observation order")
        inspection_sha256 = canonical_sha256(inspection)
        sandbox_sha256 = canonical_sha256(
            {
                "schema": "aletheia.oci_sandbox_instance_identity.v2",
                "container_id": container_id,
                "image_config_sha256": self._policy.image_config_sha256,
                "oci_config_sha256": preparation.oci_config_sha256,
                "runtime_request_sha256": preparation.runtime_request_sha256,
            }
        )
        process_sha256 = canonical_sha256(
            {
                "schema": "aletheia.linux_oci_process_identity.v2",
                "boot_id": request.boot_id,
                "container_id": container_id,
                "pid": pid,
                "proc_start_ticks": start_ticks,
                "pid_namespace_device": proc_namespace.st_dev,
                "pid_namespace_inode": proc_namespace.st_ino,
                "proc_cgroup_sha256": hashlib.sha256(proc_cgroup).hexdigest(),
                "cgroup_limits_sha256": cgroup_limits_sha256,
                "workload_executable_sha256": workload_executable_sha256,
                "workload_argv_sha256": workload_argv_sha256,
                "started_at": started_at.isoformat(),
                "started_monotonic_lower_bound_ns": started_monotonic_lower_bound_ns,
                "started_monotonic_upper_bound_exclusive_ns": (
                    started_monotonic_upper_bound_exclusive_ns
                ),
            }
        )
        return _EngineLaunchJournal(
            preparation_sha256=preparation.preparation_sha256,
            runtime_launch_authorization_sha256=authorization_sha256,
            production_capability_sha256=production_capability_sha256,
            launch_gate_authorization_journal_sha256=(launch_gate_authorization_journal_sha256),
            deadline_watchdog_journal_sha256=deadline_watchdog_journal_sha256,
            create_submission_journal_sha256=create_submission_journal_sha256,
            start_submission_journal_sha256=start_submission_journal_sha256,
            container_id=container_id,
            container_inspection_sha256=inspection_sha256,
            sandbox_instance_sha256=sandbox_sha256,
            process_identity_sha256=process_sha256,
            pid=pid,
            proc_start_ticks=start_ticks,
            pid_namespace_device=proc_namespace.st_dev,
            pid_namespace_inode=proc_namespace.st_ino,
            proc_cgroup_sha256=hashlib.sha256(proc_cgroup).hexdigest(),
            cgroup_limits_sha256=cgroup_limits_sha256,
            workload_executable_sha256=workload_executable_sha256,
            workload_argv_sha256=workload_argv_sha256,
            started_at=started_at,
            started_monotonic_lower_bound_ns=started_monotonic_lower_bound_ns,
            started_monotonic_upper_bound_exclusive_ns=(started_monotonic_upper_bound_exclusive_ns),
            observed_at=observed_at,
            observed_monotonic_ns=observed_monotonic_ns,
        )

    @staticmethod
    def _rehash_proc_executable(pid: int) -> str:
        path = Path(f"/proc/{pid}/exe")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise OCIEngineError("OCI workload executable is unavailable for exact rehash") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OCIEngineError("OCI workload executable is not a regular file")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if LocalQualificationOCIRuntime._stat_identity(before) != (
                LocalQualificationOCIRuntime._stat_identity(after)
            ):
                raise OCIEngineError("OCI workload executable changed while rehashed")
            return digest.hexdigest()
        finally:
            os.close(descriptor)

    def _verify_cgroup_v2_enforcement(
        self,
        *,
        request: OCIExecutionPlan,
        container_id: str,
        proc_cgroup: bytes,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> str:
        """Reopen the live leaf cgroup and attest actual cpu/memory/swap/pids limits."""

        try:
            decoded = proc_cgroup.decode("ascii")
        except UnicodeDecodeError as exc:
            raise OCIEngineError("OCI process cgroup membership is not canonical ASCII") from exc
        lines = decoded.rstrip("\n").splitlines()
        if (
            len(lines) != 1
            or not lines[0].startswith("0::/")
            or decoded not in {lines[0], f"{lines[0]}\n"}
        ):
            raise OCIEngineError("OCI process does not have one exact cgroup-v2 membership")
        components = tuple(lines[0][4:].split("/"))
        if (
            not components
            or any(
                not component
                or component in {".", ".."}
                or re.fullmatch(r"[A-Za-z0-9_.:@-]+", component) is None
                for component in components
            )
            or container_id not in "/".join(components)
        ):
            raise OCIEngineError("OCI process cgroup path is unsafe or belongs to another sandbox")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptors: list[int] = []
        try:
            current = os.open(cgroup_root, directory_flags)
            descriptors.append(current)
            for component in components:
                current = os.open(component, directory_flags, dir_fd=current)
                descriptors.append(current)
            leaf = os.fstat(current)
            values: dict[str, str] = {}
            identities: dict[str, tuple[int, ...]] = {}
            for name in ("cpu.max", "memory.max", "memory.swap.max", "pids.max"):
                value, identity = self._read_cgroup_control(current, name)
                values[name] = value
                identities[name] = identity
        except OSError as exc:
            raise OCIEngineError("OCI live cgroup-v2 controls are unavailable or unsafe") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        expected_cpu = (
            str(request.cpu_cores * self._policy.cpu_period_microseconds),
            str(self._policy.cpu_period_microseconds),
        )
        if (
            tuple(values["cpu.max"].split(" ")) != expected_cpu
            or values["memory.max"] != str(request.memory_bytes)
            or values["memory.swap.max"] != "0"
            or values["pids.max"] != str(self._policy.pids_limit)
        ):
            raise OCIEngineError("OCI live cgroup-v2 limits differ from exact placement")
        return canonical_sha256(
            {
                "schema": "aletheia.live_oci_cgroup_v2_enforcement.v2",
                "container_id": container_id,
                "cgroup_path": "/" + "/".join(components),
                "leaf_device": leaf.st_dev,
                "leaf_inode": leaf.st_ino,
                "values": values,
                "control_file_identities": identities,
            }
        )

    @staticmethod
    def _read_cgroup_control(
        directory_descriptor: int,
        name: str,
    ) -> tuple[str, tuple[int, ...]]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_descriptor,
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OCIEngineError("OCI cgroup-v2 control is not one regular kernel file")
            payload = os.read(descriptor, 257)
            if len(payload) > 256 or os.read(descriptor, 1):
                raise OCIEngineError("OCI cgroup-v2 control exceeded its exact bound")
            after = os.fstat(descriptor)
            if LocalQualificationOCIRuntime._stat_identity(before) != (
                LocalQualificationOCIRuntime._stat_identity(after)
            ):
                raise OCIEngineError("OCI cgroup-v2 control changed while read")
            try:
                decoded = payload.decode("ascii")
            except UnicodeDecodeError as exc:
                raise OCIEngineError("OCI cgroup-v2 control is not canonical ASCII") from exc
            if not decoded.endswith("\n") or "\n" in decoded[:-1] or not decoded[:-1]:
                raise OCIEngineError("OCI cgroup-v2 control is not one canonical value")
            return decoded[:-1], LocalQualificationOCIRuntime._stat_identity(after)
        except OSError as exc:
            raise OCIEngineError("OCI cgroup-v2 control could not be reopened") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validate_terminal_journal(
        self,
        *,
        runtime_root: Path,
        preparation: RuntimePreparation,
        launch: RuntimeLaunchEvidence,
        terminal: _EngineTerminalJournal,
    ) -> None:
        engine_launch = self._load_required(
            runtime_root / "engine-launch.json", _EngineLaunchJournal
        )
        observation = terminal.observation
        if (
            terminal.preparation_sha256 != preparation.preparation_sha256
            or terminal.runtime_identity_sha256 != launch.runtime_identity_sha256
            or terminal.engine_launch_journal_sha256 != launch.engine_launch_journal_sha256
            or engine_launch.journal_sha256 != launch.engine_launch_journal_sha256
            or engine_launch.preparation_sha256 != preparation.preparation_sha256
            or engine_launch.runtime_launch_authorization_sha256
            != launch.runtime_launch_authorization_sha256
            or engine_launch.sandbox_instance_sha256
            != launch.runtime_identity.sandbox_instance_sha256
            or engine_launch.process_identity_sha256
            != launch.runtime_identity.process_identity_sha256
            or observation.container_id != engine_launch.container_id
            or observation.ended_at < engine_launch.started_at
            or observation.ended_monotonic_ns
            < engine_launch.started_monotonic_upper_bound_exclusive_ns
        ):
            raise OCIJournalError(
                "OCI terminal journal differs from exact launch or process identity"
            )

    def _capture_terminal_from_engine(
        self,
        *,
        runtime_root: Path,
        preparation: RuntimePreparation,
        launch: RuntimeLaunchEvidence,
        first_inspection: dict[str, object],
    ) -> _EngineTerminalJournal:
        """Compose terminal evidence only from pinned wait plus independent reinspection."""

        engine_launch = self._load_required(
            runtime_root / "engine-launch.json", _EngineLaunchJournal
        )
        if (
            engine_launch.journal_sha256 != launch.engine_launch_journal_sha256
            or engine_launch.preparation_sha256 != preparation.preparation_sha256
            or engine_launch.runtime_launch_authorization_sha256
            != launch.runtime_launch_authorization_sha256
            or engine_launch.sandbox_instance_sha256
            != launch.runtime_identity.sandbox_instance_sha256
            or engine_launch.process_identity_sha256
            != launch.runtime_identity.process_identity_sha256
        ):
            raise OCIJournalError("OCI terminal capture lacks its exact launch journal")
        first_state = first_inspection.get("State")
        if (
            first_inspection.get("Id") != engine_launch.container_id
            or not isinstance(first_state, dict)
            or first_state.get("Running") is not False
            or first_state.get("Status") not in {"dead", "exited"}
            or first_state.get("Pid") != 0
        ):
            raise OCIEngineError("OCI terminal capture began from a live or different container")
        first_exit_code = first_state.get("ExitCode")
        first_finished_at = first_state.get("FinishedAt")
        if (
            isinstance(first_exit_code, bool)
            or not isinstance(first_exit_code, int)
            or not 0 <= first_exit_code <= 255
            or not isinstance(first_finished_at, str)
        ):
            raise OCIEngineError("OCI terminal inspection omitted exact exit evidence")

        config = self._load_required(runtime_root / "oci-config.json", OCIConfiguration)
        wait_payload = self._run_engine(
            (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "wait",
                config.container_name,
            )
        )
        if re.fullmatch(r"[0-9]{1,3}\n?", wait_payload) is None:
            raise OCIEngineError("OCI wait returned a non-canonical exit status")
        waited_exit_code = int(wait_payload.strip())
        second_inspection = self._engine_inspect(config.container_name, optional=False)
        assert second_inspection is not None  # optional=False is exact by contract
        self._validate_engine_configuration(second_inspection, config=config)
        second_state = second_inspection.get("State")
        if (
            second_inspection.get("Id") != engine_launch.container_id
            or not isinstance(second_state, dict)
            or second_state.get("Running") is not False
            or second_state.get("Status") not in {"dead", "exited"}
            or second_state.get("Pid") != 0
            or second_state.get("ExitCode") != first_exit_code
            or second_state.get("FinishedAt") != first_finished_at
            or waited_exit_code != first_exit_code
        ):
            raise OCIEngineError(
                "OCI terminal wait/reinspection observed a live or different process"
            )
        self._require_original_process_absent(engine_launch)
        ended_at = self._parse_engine_timestamp(first_finished_at)
        observed_at = self._utc_now()
        observed_monotonic_ns = self._clock.monotonic_ns()
        if ended_at < engine_launch.started_at or ended_at > observed_at:
            raise OCIEngineError("OCI terminal timestamp is outside the exact observation")
        event_sha256 = canonical_sha256(
            {
                "schema": "aletheia.adapter_owned_oci_terminal_wait.v2",
                "engine_launch_journal_sha256": engine_launch.journal_sha256,
                "container_id": engine_launch.container_id,
                "wait_stdout_sha256": hashlib.sha256(wait_payload.encode()).hexdigest(),
                "first_inspection_sha256": canonical_sha256(first_inspection),
                "second_inspection_sha256": canonical_sha256(second_inspection),
                "original_pid": engine_launch.pid,
                "original_proc_start_ticks": engine_launch.proc_start_ticks,
                "original_process_absent_at_monotonic_ns": observed_monotonic_ns,
            }
        )
        observation = _OCIEngineTerminalObservation(
            runtime_identity_sha256=launch.runtime_identity_sha256,
            container_id=engine_launch.container_id,
            exit_code=first_exit_code,
            ended_at=ended_at,
            # The engine supplies a wall-clock finish timestamp, not a trustworthy host monotonic
            # instant.  Use the first exact wait+PID-absence confirmation as the conservative
            # monotonic end bound rather than fabricating an earlier value.
            ended_monotonic_ns=observed_monotonic_ns,
            observed_at=observed_at,
            observed_monotonic_ns=observed_monotonic_ns,
            engine_event_journal_sha256=event_sha256,
        )
        terminal = _EngineTerminalJournal(
            preparation_sha256=preparation.preparation_sha256,
            runtime_identity_sha256=launch.runtime_identity_sha256,
            engine_launch_journal_sha256=engine_launch.journal_sha256,
            observation=observation,
        )
        self._validate_terminal_journal(
            runtime_root=runtime_root,
            preparation=preparation,
            launch=launch,
            terminal=terminal,
        )
        return terminal

    @staticmethod
    def _require_original_process_absent(engine_launch: _EngineLaunchJournal) -> None:
        try:
            proc_stat = Path(f"/proc/{engine_launch.pid}/stat").read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise OCIEngineError(
                "OCI terminal capture could not prove original PID absence"
            ) from exc
        suffix = proc_stat.rsplit(") ", 1)
        if len(suffix) != 2:
            raise OCIEngineError("OCI terminal PID stat is malformed")
        fields = suffix[1].split()
        try:
            observed_start_ticks = int(fields[19])
        except (IndexError, ValueError) as exc:
            raise OCIEngineError("OCI terminal PID stat lacks start identity") from exc
        if observed_start_ticks == engine_launch.proc_start_ticks:
            raise OCIEngineError(
                "OCI engine reported termination while the original process is still present"
            )

    def _inspect_container(
        self,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        expected_identity: NodeRuntimeIdentity,
    ) -> dict[str, object] | None:
        capability = self.probe_production_capability(request=request)
        if capability.boot_id != preparation.boot_id:
            raise OCIProductionCapabilityError("runtime inspection crossed a host reboot")
        config = self._load_required(
            self._runtime_path(request) / "oci-config.json", OCIConfiguration
        )
        inspection = self._engine_inspect(config.container_name, optional=True)
        if inspection is None:
            return None
        self._validate_engine_configuration(inspection, config=config)
        container_id = inspection.get("Id")
        expected_sandbox = canonical_sha256(
            {
                "schema": "aletheia.oci_sandbox_instance_identity.v2",
                "container_id": container_id,
                "image_config_sha256": self._policy.image_config_sha256,
                "oci_config_sha256": preparation.oci_config_sha256,
                "runtime_request_sha256": preparation.runtime_request_sha256,
            }
        )
        if expected_sandbox != expected_identity.sandbox_instance_sha256:
            raise OCIEngineError("OCI inspection resolved a different sandbox instance")
        state = inspection.get("State")
        if not isinstance(state, dict):
            raise OCIEngineError("OCI inspection omitted process state")
        return inspection

    def _exact_engine_absence_sha256(self, name: str) -> str:
        stdout = b"[]\n"
        stderr = self._policy.inspect_absence_stderr_template.format(container_name=name).encode(
            "utf-8"
        )
        return canonical_sha256(
            {
                "schema": "aletheia.oci_exact_cli_absence.v2",
                "runtime_binary_sha256": self._policy.runtime_binary_sha256,
                "engine_endpoint": self._policy.engine_endpoint,
                "container_name": name,
                "returncode": 1,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            }
        )

    def _engine_inspect(self, name: str, *, optional: bool) -> dict[str, object] | None:
        completed = self._invoke_engine(
            (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "inspect",
                name,
            ),
            allowed_exit_codes=(0, 1) if optional else (0,),
        )
        if completed.returncode == 1:
            expected_stderr = self._policy.inspect_absence_stderr_template.format(
                container_name=name
            ).encode("utf-8")
            if completed.stdout != b"[]\n" or completed.stderr != expected_stderr:
                raise OCIEngineError(
                    "OCI inspect failure is not the deployment-pinned absence response"
                )
            return None
        if completed.stderr:
            raise OCIEngineError("OCI inspect success emitted unexpected stderr")
        try:
            payload = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OCIEngineError("OCI inspect response is not UTF-8") from exc
        if not payload.strip():
            raise OCIEngineError("OCI inspect response is empty")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OCIEngineError("OCI inspect response is not JSON") from exc
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise OCIEngineError("OCI inspect response must contain exactly one container")
        return decoded[0]

    def _created_never_started_container_id(
        self,
        inspection: dict[str, object],
        *,
        config: OCIConfiguration,
    ) -> str:
        self._validate_engine_configuration(inspection, config=config)
        container_id = inspection.get("Id")
        state = inspection.get("State")
        allowed_state_fields = {
            "Status",
            "Running",
            "Paused",
            "Restarting",
            "OOMKilled",
            "Dead",
            "Pid",
            "ExitCode",
            "Error",
            "StartedAt",
            "FinishedAt",
        }
        if (
            not isinstance(container_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or not isinstance(state, dict)
            or set(state) != allowed_state_fields
            or state.get("Status") != "created"
            or state.get("Running") is not False
            or state.get("Paused") is not False
            or state.get("Restarting") is not False
            or state.get("OOMKilled") is not False
            or state.get("Dead") is not False
            or state.get("Pid") != 0
            or isinstance(state.get("Pid"), bool)
            or state.get("ExitCode") != 0
            or isinstance(state.get("ExitCode"), bool)
            or state.get("Error") != ""
            or inspection.get("RestartCount") != 0
            or isinstance(inspection.get("RestartCount"), bool)
        ):
            raise OCIEngineError("OCI cleanup state is not exact CREATED/PID0 never-started")
        started_at = state.get("StartedAt")
        finished_at = state.get("FinishedAt")
        if not isinstance(started_at, str) or not isinstance(finished_at, str):
            raise OCIEngineError("OCI CREATED state omitted zero start/finish timestamps")
        zero = datetime(1, 1, 1, tzinfo=timezone.utc)
        if (
            self._parse_engine_timestamp(started_at) != zero
            or self._parse_engine_timestamp(finished_at) != zero
        ):
            raise OCIEngineError("OCI CREATED state has historical start/finish evidence")
        return container_id

    def _remove_created_container(self, container_id: str) -> None:
        command = (
            self._policy.runtime_binary_path,
            "--host",
            self._policy.engine_endpoint,
            "rm",
            container_id,
        )
        completed = self._invoke_engine(command)
        if completed.stdout != f"{container_id}\n".encode() or completed.stderr:
            raise OCIEngineError("OCI remove response changed exact created container identity")

    def _validate_engine_configuration(
        self,
        inspection: dict[str, object],
        *,
        config: OCIConfiguration,
    ) -> None:
        container_config = inspection.get("Config")
        host_config = inspection.get("HostConfig")
        mounts = inspection.get("Mounts")
        image_id = inspection.get("Image")
        if (
            not isinstance(container_config, dict)
            or not isinstance(host_config, dict)
            or not isinstance(mounts, list)
        ):
            raise OCIEngineError("OCI inspection lacks config, host config, or mounts")
        # Docker Engine has exposed two closed identities through ContainerInspect.Image:
        # older releases return the image config digest while Docker 29 returns the
        # manifest descriptor digest.  Both identities are independently frozen in the
        # deployment policy; accepting anything outside that exact pair remains fail-closed.
        frozen_image_ids = {
            f"sha256:{config.image_config_sha256}",
            f"sha256:{config.image_manifest_sha256}",
        }
        if image_id not in frozen_image_ids:
            raise OCIEngineError("OCI container resolved outside the frozen image digests")
        expected_environment = {item.name: item.value for item in config.image_environment}
        expected_environment.update({item.name: item.value for item in config.environment})
        observed_environment: dict[str, str] = {}
        raw_environment = container_config.get("Env") or []
        if not isinstance(raw_environment, list):
            raise OCIEngineError("OCI environment inspection is not typed")
        for item in raw_environment:
            if not isinstance(item, str) or "=" not in item:
                raise OCIEngineError("OCI environment inspection contains an invalid entry")
            name, value = item.split("=", 1)
            if name in observed_environment:
                raise OCIEngineError("OCI environment inspection repeats a variable")
            observed_environment[name] = value
        expected_mounts = {
            (item.source, item.destination, item.read_only, item.propagation)
            for item in config.mounts
        }
        observed_mounts: set[tuple[str, str, bool, str]] = set()
        for item in mounts:
            if not isinstance(item, dict) or item.get("Type") != "bind":
                raise OCIEngineError("OCI runtime created an undeclared non-bind mount")
            observed_mounts.add(
                (
                    str(item.get("Source")),
                    str(item.get("Destination")),
                    not bool(item.get("RW")),
                    str(item.get("Propagation")),
                )
            )
        raw_host_mounts = host_config.get("Mounts")
        if not isinstance(raw_host_mounts, list):
            raise OCIEngineError("OCI HostConfig omitted exact mount projection")
        observed_host_mounts: set[tuple[str, str, bool, str]] = set()
        for item in raw_host_mounts:
            if not isinstance(item, dict) or item.get("Type") != "bind":
                raise OCIEngineError("OCI HostConfig contains an undeclared mount")
            bind_options = item.get("BindOptions")
            if not isinstance(bind_options, dict):
                raise OCIEngineError("OCI bind mount omitted propagation options")
            observed_host_mounts.add(
                (
                    str(item.get("Source")),
                    str(item.get("Target")),
                    bool(item.get("ReadOnly")),
                    str(bind_options.get("Propagation")),
                )
            )
        raw_devices = host_config.get("Devices") or []
        if not isinstance(raw_devices, list):
            raise OCIEngineError("OCI device inspection is not typed")
        observed_devices: set[tuple[str, str, str]] = set()
        for item in raw_devices:
            if not isinstance(item, dict) or set(item) != {
                "CgroupPermissions",
                "PathInContainer",
                "PathOnHost",
            }:
                raise OCIEngineError("OCI engine returned a non-exact device mapping")
            observed_devices.add(
                (
                    str(item.get("PathOnHost")),
                    str(item.get("PathInContainer")),
                    str(item.get("CgroupPermissions")),
                )
            )
        expected_devices = {
            (item.host_device_path, item.container_device_path, item.access)
            for item in config.devices
        }
        raw_security_options = host_config.get("SecurityOpt")
        if not isinstance(raw_security_options, list) or not all(
            isinstance(item, str) for item in raw_security_options
        ):
            raise OCIEngineError("OCI security options are absent or untyped")
        normalized_security_options = self._normalize_engine_security_options(
            raw_security_options,
            config=config,
        )
        expected_security_options = {
            "no-new-privileges=true",
            f"seccomp={config.seccomp_profile_path}",
            f"apparmor={config.apparmor_profile}",
        }
        raw_tmpfs = host_config.get("Tmpfs") or {}
        if not isinstance(raw_tmpfs, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_tmpfs.items()
        ):
            raise OCIEngineError("OCI tmpfs inspection is not typed")
        expected_tmpfs: dict[str, frozenset[str]] = {}
        if config.scratch_bytes:
            expected_tmpfs[self._policy.scratch_mount_target] = frozenset(
                {
                    "rw",
                    "noexec",
                    "nosuid",
                    "nodev",
                    f"size={config.scratch_bytes}",
                    "mode=0700",
                }
            )
        observed_tmpfs = {
            key: frozenset(item for item in value.split(",") if item)
            for key, value in raw_tmpfs.items()
        }
        expected_labels = dict(config.labels)
        empty_host_fields = (
            "Binds",
            "CapAdd",
            "CgroupParent",
            "CpusetCpus",
            "CpusetMems",
            "DeviceCgroupRules",
            "DeviceRequests",
            "Dns",
            "DnsOptions",
            "DnsSearch",
            "ExtraHosts",
            "GroupAdd",
            "Links",
            "PortBindings",
            "StorageOpt",
            "Sysctls",
            "Ulimits",
            "VolumesFrom",
        )
        zero_host_fields = (
            "BlkioWeight",
            "CpuCount",
            "CpuPercent",
            "CpuShares",
            "IOMaximumBandwidth",
            "IOMaximumIOps",
            "KernelMemory",
            "KernelMemoryTCP",
            "MemoryReservation",
            "NanoCpus",
        )
        has_undeclared_host_field = any(
            host_config.get(name) not in (None, "", [], {}) for name in empty_host_fields
        )
        has_nonzero_host_field = any(
            host_config.get(name) not in (None, 0) for name in zero_host_fields
        )
        restart_policy = host_config.get("RestartPolicy")
        log_config = host_config.get("LogConfig")
        healthcheck = container_config.get("Healthcheck")
        if (
            container_config.get("Image") != config.image_reference
            or container_config.get("Entrypoint") != [config.entrypoint]
            or tuple(container_config.get("Cmd") or ()) != config.arguments
            or container_config.get("User") != f"{config.workload_uid}:{config.workload_gid}"
            or container_config.get("WorkingDir") != config.working_directory
            or container_config.get("Labels") != expected_labels
            or container_config.get("OpenStdin") not in (None, False)
            or container_config.get("StdinOnce") not in (None, False)
            or container_config.get("Tty") not in (None, False)
            or healthcheck != {"Test": ["NONE"]}
            or observed_environment != expected_environment
            or observed_mounts != expected_mounts
            or len(mounts) != len(expected_mounts)
            or observed_host_mounts != expected_mounts
            or len(raw_host_mounts) != len(expected_mounts)
            or observed_devices != expected_devices
            or len(raw_devices) != len(expected_devices)
            or set(normalized_security_options) != expected_security_options
            or len(normalized_security_options) != len(expected_security_options)
            or observed_tmpfs != expected_tmpfs
            or inspection.get("AppArmorProfile") != config.apparmor_profile
            or host_config.get("NetworkMode") != "none"
            or host_config.get("IpcMode") != "none"
            or host_config.get("PidMode") not in (None, "")
            or host_config.get("UTSMode") not in (None, "")
            or host_config.get("UsernsMode") not in (None, "")
            or host_config.get("CgroupnsMode") != config.cgroup_namespace_mode
            or host_config.get("Runtime") != config.low_level_runtime
            or tuple(host_config.get("MaskedPaths") or ()) != config.masked_paths
            or tuple(host_config.get("ReadonlyPaths") or ()) != config.readonly_paths
            or not host_config.get("ReadonlyRootfs")
            or bool(host_config.get("Privileged"))
            or tuple(host_config.get("CapDrop") or ()) != ("ALL",)
            or has_undeclared_host_field
            or has_nonzero_host_field
            or host_config.get("Isolation") not in (None, "", "default")
            or host_config.get("ShmSize") not in (None, 64 * 1024 * 1024)
            or host_config.get("MemorySwappiness") not in (None, -1)
            or host_config.get("AutoRemove") not in (None, False)
            or host_config.get("PublishAllPorts") not in (None, False)
            or host_config.get("Init") not in (None, False)
            or host_config.get("OomKillDisable") not in (None, False)
            or restart_policy != {"Name": "no", "MaximumRetryCount": 0}
            or log_config != {"Type": "none", "Config": {}}
            or int(host_config.get("Memory") or 0) != config.memory_bytes
            or int(host_config.get("MemorySwap") or 0) != config.memory_swap_bytes
            or int(host_config.get("CpuPeriod") or 0) != config.cpu_period_microseconds
            or int(host_config.get("CpuQuota") or 0) != config.cpu_quota_microseconds
            or int(host_config.get("PidsLimit") or 0) != config.pids_limit
        ):
            raise OCIEngineError("OCI engine enforcement differs from deterministic config")

    def _normalize_engine_security_options(
        self,
        raw_security_options: list[object],
        *,
        config: OCIConfiguration,
    ) -> tuple[str, ...]:
        """Normalize only independently verified Docker inspection variants.

        Docker 29 expands a seccomp file option into inline compact JSON in ContainerInspect.
        Older engines preserve the exact file path.  The inline form is equivalent only when it
        is duplicate-free standard JSON and canonicalizes to the freshly rehashed, runtime-owned
        copy of the deployment-pinned profile.
        """

        expected_seccomp = f"seccomp={config.seccomp_profile_path}"
        normalized: list[str] = []
        for raw_item in raw_security_options:
            if not isinstance(raw_item, str):
                raise OCIEngineError("OCI security options contain a non-string value")
            item = "no-new-privileges=true" if raw_item == "no-new-privileges:true" else raw_item
            if item.startswith("seccomp=") and item != expected_seccomp:
                inline_payload = item.removeprefix("seccomp=")
                try:
                    frozen_payload = self._load_verified_seccomp_copy()
                    frozen = _strict_json_value(
                        frozen_payload,
                        label="deployment-pinned seccomp profile",
                    )
                    observed = _strict_json_value(
                        inline_payload,
                        label="Docker inline seccomp profile",
                    )
                except (OCIProductionCapabilityError, ValueError) as exc:
                    raise OCIEngineError(
                        "OCI inline seccomp projection could not be independently verified"
                    ) from exc
                if canonical_json_bytes(observed) == canonical_json_bytes(frozen):
                    item = expected_seccomp
            normalized.append(item)
        return tuple(normalized)

    def _run_engine(
        self,
        command: tuple[str, ...],
        *,
        allowed_exit_codes: tuple[int, ...] = (0,),
        mutation_guard: Callable[[], None] | None = None,
        submission_marker: Callable[[], None] | None = None,
        final_mutation_guard: Callable[[], None] | None = None,
        mutation_runtime_root: Path | None = None,
    ) -> str:
        completed = self._invoke_engine(
            command,
            allowed_exit_codes=allowed_exit_codes,
            mutation_guard=mutation_guard,
            submission_marker=submission_marker,
            final_mutation_guard=final_mutation_guard,
            mutation_runtime_root=mutation_runtime_root,
        )
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OCIEngineError("OCI engine response is not UTF-8") from exc

    def _invoke_engine(
        self,
        command: tuple[str, ...],
        *,
        allowed_exit_codes: tuple[int, ...] = (0,),
        mutation_guard: Callable[[], None] | None = None,
        submission_marker: Callable[[], None] | None = None,
        final_mutation_guard: Callable[[], None] | None = None,
        mutation_runtime_root: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if not command or command[0] != self._policy.runtime_binary_path:
            raise OCIEngineError("OCI engine command changed its deployment-pinned executable")
        if sys.platform != "linux":
            raise OCIProductionCapabilityError("OCI engine invocation is Linux-only")
        if (submission_marker is None) != (mutation_runtime_root is None):
            raise OCIEngineError("OCI mutation lock scope differs from submission journal")
        self._verify_seccomp_copy()
        try:
            mutation_lock = (
                self._engine_mutation_lock(mutation_runtime_root)
                if mutation_runtime_root is not None
                else nullcontext()
            )
            with mutation_lock, self._pinned_runtime_binary() as binary_descriptor:
                executable = f"/proc/self/fd/{binary_descriptor}"
                if mutation_guard is not None:
                    # This is intentionally the final operation before process creation.  The
                    # executable descriptor and immutable seccomp copy are already revalidated.
                    mutation_guard()
                if submission_marker is not None:
                    # A crash after this fsynced marker is conservatively treated as a submitted
                    # daemon mutation until exact running evidence or CREATED/PID0 proof exists.
                    submission_marker()
                if final_mutation_guard is not None:
                    # Marker fsync may cross the short ticket window, so clocks/signature are read
                    # once more as the final in-process operation before subprocess creation.
                    final_mutation_guard()
                completed = subprocess.run(
                    command,
                    executable=executable,
                    pass_fds=(binary_descriptor,),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd="/",
                    env={},
                    timeout=60,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OCIEngineError("pinned OCI engine invocation failed") from exc
        if (
            len(completed.stdout) > _ENGINE_OUTPUT_LIMIT
            or len(completed.stderr) > _ENGINE_OUTPUT_LIMIT
        ):
            raise OCIEngineError("OCI engine response exceeded its bounded journal limit")
        if completed.returncode not in allowed_exit_codes:
            raise OCIEngineError(f"pinned OCI engine returned exit code {completed.returncode}")
        return completed

    @staticmethod
    def _parse_engine_timestamp(value: str) -> datetime:
        # Docker commonly emits RFC3339Nano, while datetime accepts at most six fractional digits.
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z",
            value,
        )
        if match is None:
            raise OCIEngineError("OCI engine timestamp is not canonical UTC RFC3339")
        fraction = (match.group(2) or "")[:6].ljust(6, "0")
        normalized = f"{match.group(1)}.{fraction}+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:  # pragma: no cover - regex already constrains shape
            raise OCIEngineError("OCI engine timestamp is invalid") from exc

    @contextmanager
    def _pinned_file_descriptor(
        self,
        *,
        path: Path,
        label: str,
        expected_sha256: str,
        expected_device: int,
        expected_inode: int,
        expected_owner_uid: int,
        expected_owner_gid: int,
        expected_mode: int,
        expected_parent_chain_sha256: str,
        executable: bool,
    ) -> Iterator[int]:
        try:
            parent_before = host_parent_chain_sha256(path)
        except ValueError as exc:
            raise OCIProductionCapabilityError(f"{label} parent custody is unsafe") from exc
        if parent_before != expected_parent_chain_sha256:
            raise OCIProductionCapabilityError(f"{label} parent chain differs from deployment pin")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIProductionCapabilityError(f"{label} is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_dev != expected_device
                or before.st_ino != expected_inode
                or before.st_uid != expected_owner_uid
                or before.st_gid != expected_owner_gid
                or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_mode & 0o022
                or (executable and not before.st_mode & 0o111)
            ):
                raise OCIProductionCapabilityError(f"{label} differs from pinned inode custody")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                LocalQualificationOCIRuntime._stat_identity(before)
                != (LocalQualificationOCIRuntime._stat_identity(after))
                or digest.hexdigest() != expected_sha256
            ):
                raise OCIProductionCapabilityError(f"{label} changed while it was hashed")
            try:
                parent_after = host_parent_chain_sha256(path)
            except ValueError as exc:
                raise OCIProductionCapabilityError(
                    f"{label} parent custody changed while verified"
                ) from exc
            if parent_after != parent_before:
                raise OCIProductionCapabilityError(f"{label} parent custody changed while verified")
            os.lseek(descriptor, 0, os.SEEK_SET)
            yield descriptor
            if LocalQualificationOCIRuntime._stat_identity(after) != (
                LocalQualificationOCIRuntime._stat_identity(os.fstat(descriptor))
            ):
                raise OCIProductionCapabilityError(f"{label} changed while in use")
        finally:
            os.close(descriptor)

    def _pinned_runtime_binary(self) -> Iterator[int]:
        return self._pinned_file_descriptor(
            path=Path(self._policy.runtime_binary_path),
            label="OCI runtime binary",
            expected_sha256=self._policy.runtime_binary_sha256,
            expected_device=self._policy.runtime_binary_device,
            expected_inode=self._policy.runtime_binary_inode,
            expected_owner_uid=self._policy.runtime_binary_owner_uid,
            expected_owner_gid=self._policy.runtime_binary_owner_gid,
            expected_mode=self._policy.runtime_binary_mode,
            expected_parent_chain_sha256=(self._policy.runtime_binary_parent_chain_sha256),
            executable=True,
        )

    def _pinned_seccomp_source(self) -> Iterator[int]:
        return self._pinned_file_descriptor(
            path=Path(self._policy.seccomp_profile_path),
            label="OCI seccomp profile",
            expected_sha256=self._policy.seccomp_profile_sha256,
            expected_device=self._policy.seccomp_profile_device,
            expected_inode=self._policy.seccomp_profile_inode,
            expected_owner_uid=self._policy.seccomp_profile_owner_uid,
            expected_owner_gid=self._policy.seccomp_profile_owner_gid,
            expected_mode=self._policy.seccomp_profile_mode,
            expected_parent_chain_sha256=(self._policy.seccomp_profile_parent_chain_sha256),
            executable=False,
        )

    def _ensure_seccomp_copy(self) -> None:
        with self._policy_lock():
            self._ensure_seccomp_copy_locked()

    def _ensure_seccomp_copy_locked(self) -> None:
        policy_root = self._seccomp_copy_path.parent
        try:
            policy_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise OCIProductionCapabilityError(
                "runtime-owned seccomp directory could not be created"
            ) from exc
        else:
            _durable_runtime_checkpoint(
                "seccomp-directory-created-before-parent-fsync",
                policy_root,
            )
        metadata = policy_root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o500, 0o700}
        ):
            raise OCIProductionCapabilityError("runtime-owned seccomp directory is unsafe")
        # Replay must flush an existing-but-not-yet-durable dentry left by a crash after mkdir.
        self._fsync_directory(policy_root.parent)
        if stat.S_IMODE(metadata.st_mode) == 0o500:
            # Recovery needs directory write permission.  The singleton policy lock is already
            # held; a crash here leaves the owner-only 0700 state that this method accepts and
            # deterministically reseals on the next call.
            os.chmod(policy_root, 0o700, follow_symlinks=False)
            self._fsync_directory(policy_root.parent)
        self._recover_publish_temps(self._seccomp_copy_path, suffix="tmp")
        if not self._seccomp_copy_path.exists():
            with self._pinned_seccomp_source() as descriptor:
                payload = bytearray()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
            self._publish_blob(self._seccomp_copy_path, bytes(payload))
        self._verify_seccomp_copy()
        if stat.S_IMODE(policy_root.lstat().st_mode) != 0o500:
            os.chmod(policy_root, 0o500, follow_symlinks=False)
            self._fsync_directory(policy_root)
        self._verify_seccomp_copy()

    def _load_verified_seccomp_copy(self) -> bytes:
        try:
            descriptor = os.open(
                self._seccomp_copy_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise OCIProductionCapabilityError(
                "runtime-owned immutable seccomp copy is missing"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
            ):
                raise OCIProductionCapabilityError("runtime-owned seccomp copy custody is unsafe")
            digest = hashlib.sha256()
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                if len(payload) + len(chunk) > _MAX_JOURNAL_BYTES:
                    raise OCIProductionCapabilityError(
                        "runtime-owned seccomp copy exceeds its bounded size"
                    )
                payload.extend(chunk)
                digest.update(chunk)
            if digest.hexdigest() != self._policy.seccomp_profile_sha256 or self._stat_identity(
                before
            ) != self._stat_identity(os.fstat(descriptor)):
                raise OCIProductionCapabilityError(
                    "runtime-owned seccomp copy differs from deployment policy"
                )
            return bytes(payload)
        finally:
            os.close(descriptor)

    def _verify_seccomp_copy(self) -> None:
        self._load_verified_seccomp_copy()

    def _expected_launch_gate_attestation_sha256(self) -> str:
        authority = self._runtime_control_authority
        if authority is None:
            raise OCIProductionCapabilityError(
                "in-container launch gate requires a pinned runtime-control authority"
            )
        return canonical_sha256(
            {
                "schema": "aletheia.verified_immutable_oci_launch_gate.v2",
                "policy_sha256": self._policy.policy_sha256,
                "image_reference": self._policy.image_reference,
                "image_manifest_sha256": self._policy.image_manifest_sha256,
                "image_config_sha256": self._policy.image_config_sha256,
                "launch_gate_path": self._policy.launch_gate_path,
                "launch_gate_executable_sha256": (self._policy.launch_gate_executable_sha256),
                "launch_gate_protocol_sha256": (self._policy.launch_gate_protocol_sha256),
                "runtime_control_authority": authority.pin,
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

    def _verify_launch_gate(self) -> str:
        if self._launch_gate_verifier is None:
            raise OCIProductionCapabilityError(
                "real OCI launch requires a deployment immutable-launch-gate verifier"
            )
        expected = self._expected_launch_gate_attestation_sha256()
        try:
            observed = self._launch_gate_verifier.verify_immutable_launch_gate(
                image_reference=self._policy.image_reference,
                image_manifest_sha256=self._policy.image_manifest_sha256,
                image_config_sha256=self._policy.image_config_sha256,
                launch_gate_path=self._policy.launch_gate_path,
                launch_gate_executable_sha256=(self._policy.launch_gate_executable_sha256),
                launch_gate_protocol_sha256=(self._policy.launch_gate_protocol_sha256),
                expected_evidence_sha256=expected,
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "immutable in-container launch gate could not be independently verified"
            ) from exc
        if observed != expected:
            raise OCIProductionCapabilityError(
                "immutable in-container launch gate differs from deployment policy"
            )
        return observed

    def _deadline_watchdog_journal(
        self,
        *,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> _DeadlineWatchdogJournal:
        remaining = request.deadline - preparation.prepared_at
        remaining_ns = (
            remaining.days * 86_400 * 1_000_000_000
            + remaining.seconds * 1_000_000_000
            + remaining.microseconds * 1_000
        )
        if remaining_ns <= 0:
            raise OCIProductionCapabilityError(
                "runtime hard deadline does not outlive inert preparation"
            )
        hard_deadline_boottime_ns = preparation.prepared_monotonic_ns + remaining_ns
        expected_evidence_sha256 = canonical_sha256(
            {
                "schema": "aletheia.crash_durable_oci_deadline_watchdog.v2",
                "preparation_sha256": preparation.preparation_sha256,
                "boot_id": request.boot_id,
                "runtime_id": request.runtime_id,
                "container_name": config.container_name,
                "engine_endpoint": self._policy.engine_endpoint,
                "authorization_request_sha256": authorization_request.request_sha256,
                "runtime_launch_authorization_sha256": authorization.authorization_sha256,
                "pre_runtime_absence_epoch": authorization_request.pre_runtime_absence_epoch,
                "hard_deadline": request.deadline.isoformat(),
                "hard_deadline_boottime_ns": hard_deadline_boottime_ns,
                "enforced_placement_sha256": request.enforced_placement_sha256,
                "fencing_epoch": preparation.fencing_epoch,
                "lease_token_sha256": preparation.lease_token_sha256,
                "required_action": "kill-cgroup-and-container-no-later-than-either-deadline",
                "survives_node_agent_process_crash": True,
            }
        )
        return _DeadlineWatchdogJournal(
            preparation_sha256=preparation.preparation_sha256,
            runtime_id=request.runtime_id,
            container_name=config.container_name,
            authorization_request_sha256=authorization_request.request_sha256,
            runtime_launch_authorization_sha256=authorization.authorization_sha256,
            pre_runtime_absence_epoch=authorization_request.pre_runtime_absence_epoch,
            hard_deadline=request.deadline,
            hard_deadline_boottime_ns=hard_deadline_boottime_ns,
            watchdog_evidence_sha256=expected_evidence_sha256,
        )

    def _ensure_deadline_watchdog(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> _DeadlineWatchdogJournal:
        if self._deadline_watchdog_controller is None:
            raise OCIProductionCapabilityError(
                "real OCI launch requires a crash-durable deadline-watchdog controller"
            )
        expected = self._deadline_watchdog_journal(
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        try:
            observed = self._deadline_watchdog_controller.arm_and_verify_deadline_watchdog(
                preparation_sha256=preparation.preparation_sha256,
                boot_id=request.boot_id,
                runtime_id=request.runtime_id,
                container_name=config.container_name,
                engine_endpoint=self._policy.engine_endpoint,
                authorization_request_sha256=authorization_request.request_sha256,
                runtime_launch_authorization_sha256=authorization.authorization_sha256,
                pre_runtime_absence_epoch=authorization_request.pre_runtime_absence_epoch,
                hard_deadline=expected.hard_deadline,
                hard_deadline_boottime_ns=expected.hard_deadline_boottime_ns,
                expected_evidence_sha256=expected.watchdog_evidence_sha256,
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "runtime deadline watchdog could not be durably armed"
            ) from exc
        if observed != expected.watchdog_evidence_sha256:
            raise OCIProductionCapabilityError(
                "runtime deadline watchdog differs from exact hard deadline"
            )
        path = runtime_root / "deadline-watchdog.json"
        stored = self._load_model(path, _DeadlineWatchdogJournal, optional=True)
        if stored is not None and stored != expected:
            raise OCIJournalError("runtime deadline watchdog journal changed during replay")
        self._publish_model(path, expected)
        return expected

    def _validate_deadline_watchdog_journal(
        self,
        *,
        runtime_root: Path,
        request: OCIExecutionPlan,
        preparation: RuntimePreparation,
        config: OCIConfiguration,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> _DeadlineWatchdogJournal:
        observed = self._load_required(
            runtime_root / "deadline-watchdog.json",
            _DeadlineWatchdogJournal,
        )
        expected = self._deadline_watchdog_journal(
            request=request,
            preparation=preparation,
            config=config,
            authorization_request=authorization_request,
            authorization=authorization,
        )
        if observed != expected:
            raise OCIJournalError("runtime deadline watchdog differs from exact preparation")
        return observed

    @staticmethod
    def _expected_watchdog_retirement_evidence_sha256(
        *,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        watchdog: _DeadlineWatchdogJournal,
        cleanup_absence_epoch: int,
    ) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.crash_durable_oci_watchdog_retirement.v2",
                "preparation_sha256": preparation.preparation_sha256,
                "runtime_id": preparation.runtime_id,
                "authorization_request_sha256": authorization_request.request_sha256,
                "runtime_launch_authorization_sha256": authorization.authorization_sha256,
                "pre_runtime_absence_epoch": authorization_request.pre_runtime_absence_epoch,
                "cleanup_absence_epoch": cleanup_absence_epoch,
                "watchdog_journal_sha256": watchdog.journal_sha256,
                "required_action": "retire-old-generation-before-replacement",
            }
        )

    def _retire_deadline_watchdog(
        self,
        *,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        watchdog: _DeadlineWatchdogJournal,
        cleanup_absence_epoch: int,
        expected_evidence_sha256: str,
    ) -> OCIWatchdogCleanupQuiescence:
        controller = self._deadline_watchdog_controller
        if controller is None:
            raise OCIProductionCapabilityError(
                "never-started cleanup requires a watchdog retirement controller"
            )
        expected = self._expected_watchdog_retirement_evidence_sha256(
            preparation=preparation,
            authorization_request=authorization_request,
            authorization=authorization,
            watchdog=watchdog,
            cleanup_absence_epoch=cleanup_absence_epoch,
        )
        if expected != expected_evidence_sha256:
            raise OCIJournalError("watchdog retirement differs from cleanup pending journal")
        try:
            observed = controller.retire_and_verify_deadline_watchdog(
                preparation_sha256=preparation.preparation_sha256,
                runtime_id=preparation.runtime_id,
                container_name=watchdog.container_name,
                authorization_request_sha256=authorization_request.request_sha256,
                runtime_launch_authorization_sha256=authorization.authorization_sha256,
                pre_runtime_absence_epoch=authorization_request.pre_runtime_absence_epoch,
                watchdog_journal_sha256=watchdog.journal_sha256,
                expected_evidence_sha256=expected,
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "old runtime deadline watchdog could not be durably retired"
            ) from exc
        try:
            observed = OCIWatchdogCleanupQuiescence.model_validate(
                observed.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "old runtime watchdog returned no typed cleanup quiescence"
            ) from exc
        if observed.cleanup_evidence_sha256 != expected:
            raise OCIProductionCapabilityError(
                "old runtime deadline watchdog retirement evidence changed"
            )
        return observed

    @staticmethod
    def _expected_output_quota_evidence_sha256(request: OCIExecutionPlan) -> str:
        output_root = Path(request.output_root)
        try:
            metadata = output_root.lstat()
        except OSError as exc:
            raise OCIProductionCapabilityError("output quota root identity is unavailable") from exc
        return canonical_sha256(
            {
                "schema": "aletheia.host_output_project_quota_challenge.v2",
                "execution_id": request.execution_id,
                "infrastructure_attempt_id": request.infrastructure_attempt_id,
                "runtime_id": request.runtime_id,
                "enforced_placement_sha256": request.enforced_placement_sha256,
                "output_root": request.output_root,
                "output_root_device": metadata.st_dev,
                "output_root_inode": metadata.st_ino,
                "output_root_owner_uid": metadata.st_uid,
                "output_root_owner_gid": metadata.st_gid,
                "output_quota_bytes": request.output_quota_bytes,
            }
        )

    def _verify_output_quota(self, request: OCIExecutionPlan) -> str:
        if self._output_quota_controller is None:
            raise OCIProductionCapabilityError(
                "real OCI launch requires a deployment project-quota controller"
            )
        expected = self._expected_output_quota_evidence_sha256(request)
        try:
            observed = self._output_quota_controller.verify_enforced_quota(
                output_root=Path(request.output_root),
                output_quota_bytes=request.output_quota_bytes,
                execution_id=request.execution_id,
                infrastructure_attempt_id=request.infrastructure_attempt_id,
                runtime_id=request.runtime_id,
                expected_evidence_sha256=expected,
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise OCIProductionCapabilityError(
                "output project quota could not be independently verified"
            ) from exc
        if observed != expected:
            raise OCIProductionCapabilityError(
                "output project quota differs from the exact placement"
            )
        return observed

    @staticmethod
    def _output_mount_generation_sha256(request: OCIExecutionPlan) -> str:
        """Reopen the exact post-provision mount generation at a daemon mutation boundary."""

        receipt = request.output_quota_provisioning_receipt
        output_root = Path(request.output_root)
        try:
            metadata = output_root.lstat()
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise OCIProductionCapabilityError("output mount generation is unavailable") from exc
        matches: list[dict[str, object]] = []
        for line in mountinfo.splitlines():
            left, separator, right = line.partition(" - ")
            left_fields = left.split()
            right_fields = right.split()
            if not separator or len(left_fields) < 6 or len(right_fields) < 3:
                raise OCIProductionCapabilityError("mountinfo contains an invalid entry")
            mountpoint = left_fields[4]
            for encoded, decoded in (
                ("\\040", " "),
                ("\\011", "\t"),
                ("\\012", "\n"),
                ("\\134", "\\"),
            ):
                mountpoint = mountpoint.replace(encoded, decoded)
            if mountpoint != request.output_root:
                continue
            try:
                major_text, minor_text = left_fields[2].split(":", 1)
                matches.append(
                    {
                        "mount_id": int(left_fields[0]),
                        "mount_parent_id": int(left_fields[1]),
                        "major": int(major_text),
                        "minor": int(minor_text),
                        "mount_options": tuple(sorted(left_fields[5].split(","))),
                        "filesystem_type": right_fields[0],
                        "source": right_fields[1],
                    }
                )
            except ValueError as exc:
                raise OCIProductionCapabilityError(
                    "output mount generation has invalid numeric identity"
                ) from exc
        if len(matches) != 1:
            raise OCIProductionCapabilityError("output root is not one exact mount generation")
        mount = matches[0]
        if (
            output_root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != receipt.output_root_device
            or metadata.st_ino != receipt.output_root_inode
            or metadata.st_uid != receipt.output_root_owner_uid
            or metadata.st_gid != receipt.output_root_owner_gid
            or stat.S_IMODE(metadata.st_mode) != receipt.output_root_mode
            or mount["mount_id"] != receipt.mount_id
            or mount["mount_parent_id"] != receipt.mount_parent_id
            or mount["major"] != receipt.block_device_major
            or mount["minor"] != receipt.block_device_minor
            or mount["filesystem_type"] != receipt.filesystem_type
            or mount["mount_options"] != receipt.mount_options
            or re.fullmatch(r"/dev/loop[0-9]+", str(mount["source"])) is None
        ):
            raise OCIProductionCapabilityError(
                "output mount generation differs from provisioning receipt"
            )
        return canonical_sha256(
            {
                "schema": "aletheia.output_mount_generation_boundary.v1",
                "provisioning_receipt_sha256": receipt.provisioning_receipt_sha256,
                "output_root_device": metadata.st_dev,
                "output_root_inode": metadata.st_ino,
                **mount,
            }
        )

    def _terminate_after_mount_generation_failure(self, container_id: str) -> None:
        inspection = self._engine_inspect(container_id, optional=True)
        if inspection is None:
            return
        state = inspection.get("State")
        if not isinstance(state, dict) or not isinstance(state.get("Running"), bool):
            raise OCIEngineError("unsafe container lacks typed state during emergency cleanup")
        if state["Running"]:
            command = (
                self._policy.runtime_binary_path,
                "--host",
                self._policy.engine_endpoint,
                "kill",
                "--signal",
                "KILL",
                container_id,
            )
            completed = self._invoke_engine(command)
            if completed.stdout != f"{container_id}\n".encode() or completed.stderr:
                raise OCIEngineError("emergency kill changed exact container identity")
            after = self._engine_inspect(container_id, optional=False)
            after_state = after.get("State")
            if not isinstance(after_state, dict) or after_state.get("Running") is not False:
                raise OCIEngineError("emergency kill did not stop the unsafe container")
        elif state.get("Status") == "created" and state.get("Pid") == 0:
            self._remove_created_container(container_id)
        else:
            raise OCIEngineError("unsafe container is not safely killable or removable")

    def _validate_workspace(self, request: OCIExecutionPlan) -> None:
        input_root = self._validate_directory(
            Path(request.input_root), label="OCI input root", modes=(0o500,)
        )
        output_root = self._validate_directory(
            Path(request.output_root), label="OCI output root", modes=(0o700,)
        )
        runtime_root = self._runtime_path(request)
        roots = (input_root, output_root, runtime_root, self._journal_root)
        for index, left in enumerate(roots):
            for right in roots[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    if {left, right} == {runtime_root, self._journal_root}:
                        continue
                    raise OCIPolicyRejected("OCI workspace and runtime journal roots overlap")
        if self._policy.workload_uid != os.geteuid() or self._policy.workload_gid != os.getegid():
            raise OCIPolicyRejected(
                "owner-only workspace requires workload uid/gid equal to the node principal"
            )
        self._validate_materialized_input_tree(request)
        self._validate_empty_output_tree(request)

    @classmethod
    def _validate_materialized_input_tree(cls, request: OCIExecutionPlan) -> None:
        root = Path(request.input_root)
        receipt = request.input_materialization_receipt
        files: dict[str, os.stat_result] = {}
        directories: dict[str, os.stat_result] = {}
        for current_root, directory_names, file_names in os.walk(root, topdown=True):
            current = Path(current_root)
            for name in directory_names:
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o500
                ):
                    raise OCIPolicyRejected("OCI input tree contains an unsafe directory")
                directories[relative] = metadata
            for name in file_names:
                path = current / name
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if not stat.S_ISREG(metadata.st_mode):
                    raise OCIPolicyRejected("OCI input tree contains a non-regular file")
                files[relative] = metadata
        expected_files = {item.relative_path: item for item in receipt.entries}
        expected_directories: set[str] = set()
        for relative_path in expected_files:
            parts = Path(*relative_path.split("/")).parts[:-1]
            for index in range(1, len(parts) + 1):
                expected_directories.add("/".join(parts[:index]))
        if set(files) != set(expected_files) or set(directories) != expected_directories:
            raise OCIPolicyRejected("OCI input tree differs from its typed exact receipt")
        observed_entries = []
        for relative_path, entry in expected_files.items():
            path = root / Path(*relative_path.split("/"))
            metadata = cls._rehash_materialized_input(
                path,
                expected_sha256=entry.content_sha256,
                expected_bytes=entry.content_bytes,
            )
            file_identity = canonical_sha256(
                {
                    "schema": "aletheia.local_staged_input_file_identity.v2",
                    "relative_path": relative_path,
                    "content_sha256": entry.content_sha256,
                    "content_bytes": entry.content_bytes,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "owner_uid": metadata.st_uid,
                    "owner_gid": metadata.st_gid,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "link_count": metadata.st_nlink,
                    "modified_ns": metadata.st_mtime_ns,
                    "changed_ns": metadata.st_ctime_ns,
                }
            )
            if file_identity != entry.staged_file_identity_sha256:
                raise OCIPolicyRejected("OCI input file identity changed after materialization")
            observed_entries.append(entry)
        root_metadata = root.lstat()
        root_identity = canonical_sha256(
            {
                "schema": "aletheia.local_staged_input_root_identity.v2",
                "resolved_path_sha256": hashlib.sha256(os.fsencode(root)).hexdigest(),
                "device": root_metadata.st_dev,
                "inode": root_metadata.st_ino,
                "owner_uid": root_metadata.st_uid,
                "owner_gid": root_metadata.st_gid,
                "mode": stat.S_IMODE(root_metadata.st_mode),
                "link_count": root_metadata.st_nlink,
                "modified_ns": root_metadata.st_mtime_ns,
                "changed_ns": root_metadata.st_ctime_ns,
                "entries": tuple(
                    {
                        "input_port_id": item.input_port_id,
                        "relative_path": item.relative_path,
                        "staged_file_identity_sha256": (item.staged_file_identity_sha256),
                    }
                    for item in observed_entries
                ),
            }
        )
        if root_identity != receipt.staged_root_identity_sha256:
            raise OCIPolicyRejected("OCI input root identity changed after materialization")

    @classmethod
    def _rehash_materialized_input(
        cls,
        path: Path,
        *,
        expected_sha256: str,
        expected_bytes: int,
    ) -> os.stat_result:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise OCIPolicyRejected("OCI materialized input is missing or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
            ):
                raise OCIPolicyRejected("OCI materialized input custody is unsafe")
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_bytes:
                    raise OCIPolicyRejected("OCI materialized input exceeded its receipt")
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                cls._stat_identity(before) != cls._stat_identity(after)
                or size != expected_bytes
                or digest.hexdigest() != expected_sha256
            ):
                raise OCIPolicyRejected("OCI materialized input differs from its receipt")
            return after
        finally:
            os.close(descriptor)

    @classmethod
    def _validate_empty_output_tree(cls, request: OCIExecutionPlan) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                request.output_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_gid != os.getegid()
                or stat.S_IMODE(before.st_mode) != 0o700
            ):
                raise OCIPolicyRejected("OCI output root custody metadata is unsafe")
            if os.listdir(descriptor):
                raise OCIPolicyRejected("OCI output root is not empty before workload launch")
            after = os.fstat(descriptor)
            if cls._stat_identity(before) != cls._stat_identity(after):
                raise OCIPolicyRejected("OCI output root changed while it was inspected")
        except OSError as exc:
            raise OCIPolicyRejected("OCI output root could not be inspected") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _validate_directory(path: Path, *, label: str, modes: tuple[int, ...]) -> Path:
        if path.is_symlink():
            raise OCIPolicyRejected(f"{label} cannot be a symlink")
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.lstat()
        except OSError as exc:
            raise OCIPolicyRejected(f"{label} is missing") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in modes
        ):
            raise OCIPolicyRejected(f"{label} custody metadata is unsafe")
        return resolved

    @staticmethod
    def _prepare_private_root(path: Path) -> Path:
        candidate = Path(path)
        if candidate.is_symlink():
            raise ValueError("OCI runtime journal root cannot be a symlink")
        try:
            candidate.mkdir(parents=False, exist_ok=False, mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError("OCI runtime journal parent must be pre-provisioned") from exc
        else:
            _durable_runtime_checkpoint(
                "journal-root-created-before-parent-fsync",
                candidate,
            )
        root = candidate.resolve(strict=True)
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("OCI runtime journal must be an owner-controlled private directory")
        LocalQualificationOCIRuntime._fsync_directory(root.parent)
        return root

    @staticmethod
    def _runtime_key(runtime_id: str) -> str:
        return hashlib.sha256(
            b"ALETHEIA_QUALIFICATION_OCI_RUNTIME_V2\x00" + runtime_id.encode("utf-8")
        ).hexdigest()

    def _runtime_path_for_id(self, runtime_id: str) -> Path:
        return self._journal_root / self._runtime_key(runtime_id)

    def _runtime_path(self, request: OCIExecutionPlan) -> Path:
        return self._runtime_path_for_id(request.runtime_id)

    def _ensure_runtime_directories(self, request: OCIExecutionPlan) -> Path:
        runtime_root = self._runtime_path(request)
        for path in (
            runtime_root,
            runtime_root / "control",
            runtime_root / "rebind",
            runtime_root / "cleanup",
            runtime_root / "retired-launch",
            runtime_root / "launch-recovery",
        ):
            if path.is_symlink():
                raise OCIJournalError("OCI runtime journal path cannot be a symlink")
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                _durable_runtime_checkpoint(
                    "runtime-directory-created-before-parent-fsync",
                    path,
                )
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise OCIJournalError("OCI runtime journal directory custody is unsafe")
            # Always flush the parent on replay; the directory may exist only because a prior
            # process died after mkdir returned and before the dentry reached stable storage.
            self._fsync_directory(path.parent)
        lock_path = runtime_root / "engine-mutation.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OCIJournalError("OCI engine mutation lock custody is unsafe")
            os.fsync(descriptor)
            self._fsync_directory(runtime_root)
        except OSError as exc:
            raise OCIJournalError("OCI engine mutation lock is unavailable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return runtime_root

    @contextmanager
    def _runtime_lock(self, runtime_root: Path) -> Iterator[None]:
        lock_path = runtime_root / "runtime.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OCIJournalError("OCI runtime singleton lock custody is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._recover_runtime_scope_temps(runtime_root)
            yield
        except OSError as exc:
            raise OCIJournalError("OCI runtime singleton lock failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _engine_mutation_lock(self, runtime_root: Path) -> Iterator[None]:
        lock_path = runtime_root / "engine-mutation.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OCIJournalError("OCI engine mutation lock custody is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise OCIJournalError("OCI engine mutation lock failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _policy_lock(self) -> Iterator[None]:
        lock_path = self._journal_root / "policy.lock"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OCIJournalError("OCI policy singleton lock custody is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise OCIJournalError("OCI policy singleton lock failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _publish_model(self, path: Path, value: ExecutionModel) -> None:
        payload = canonical_json_bytes(value)
        self._publish_blob(path, payload)

    def _publish_blob(self, path: Path, payload: bytes) -> None:
        self._recover_publish_temps(path, suffix="tmp")
        existing = self._read_blob(path, optional=True)
        if existing is not None:
            if existing != payload:
                raise OCIJournalError(f"OCI journal {path.name} is already bound to other bytes")
            return
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                observed = self._read_blob(path, optional=False)
                if observed != payload:
                    raise OCIJournalError(f"OCI journal {path.name} raced with different bytes")
            os.unlink(temporary, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise OCIJournalError(f"OCI journal {path.name} could not be published") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)

    def _replace_model(self, path: Path, value: ExecutionModel) -> None:
        self._recover_publish_temps(path, suffix="cas")
        payload = canonical_json_bytes(value)
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        temporary = f".{path.name}.{secrets.token_hex(16)}.cas"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        except OSError as exc:
            raise OCIJournalError("OCI runtime control sidecar CAS failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)

    def _recover_publish_temps(self, path: Path, *, suffix: Literal["cas", "tmp"]) -> None:
        """Recover one journal's bounded temp namespace while its caller holds a scope lock."""

        pattern = re.compile(rf"^\.{re.escape(path.name)}\.([0-9a-f]{{32}})\.{suffix}$")
        try:
            candidates = tuple(
                item
                for item in path.parent.iterdir()
                if item.name.startswith(f".{path.name}.") and item.name.endswith(f".{suffix}")
            )
        except OSError as exc:
            raise OCIJournalError(f"OCI journal {path.name} temp namespace is unsafe") from exc
        for temporary in candidates:
            if pattern.fullmatch(temporary.name) is None:
                raise OCIJournalError(f"OCI journal {path.name} has an unrecognized temp residue")
            try:
                metadata = temporary.lstat()
            except OSError as exc:
                raise OCIJournalError(f"OCI journal {path.name} temp residue is unsafe") from exc
            expected_modes = {0o400, 0o600}
            expected_links = {1, 2} if suffix == "tmp" else {1}
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink not in expected_links
                or stat.S_IMODE(metadata.st_mode) not in expected_modes
                or metadata.st_size > _MAX_JOURNAL_BYTES
            ):
                raise OCIJournalError(f"OCI journal {path.name} temp residue custody is unsafe")
            if metadata.st_nlink == 2:
                try:
                    final_metadata = path.lstat()
                except OSError as exc:
                    raise OCIJournalError(
                        f"OCI journal {path.name} interrupted hardlink lost its final name"
                    ) from exc
                if (
                    self._stat_identity(metadata) != self._stat_identity(final_metadata)
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    raise OCIJournalError(
                        f"OCI journal {path.name} interrupted hardlink changed identity"
                    )
            try:
                temporary.unlink()
                self._fsync_directory(path.parent)
            except OSError as exc:
                raise OCIJournalError(
                    f"OCI journal {path.name} temp residue could not be removed"
                ) from exc

    def _recover_runtime_scope_temps(self, runtime_root: Path) -> None:
        pattern = re.compile(r"^\.(.+)\.([0-9a-f]{32})\.(tmp|cas)$")
        try:
            residues = tuple(
                path for path in runtime_root.rglob(".*") if path.name.endswith((".tmp", ".cas"))
            )
        except OSError as exc:
            raise OCIJournalError("OCI runtime temp namespace could not be scanned") from exc
        for residue in residues:
            match = pattern.fullmatch(residue.name)
            if match is None or "/" in match.group(1):
                raise OCIJournalError("OCI runtime contains an unrecognized temp residue")
            suffix: Literal["cas", "tmp"] = "tmp" if match.group(3) == "tmp" else "cas"
            self._recover_publish_temps(
                residue.parent / match.group(1),
                suffix=suffix,
            )

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:  # pragma: no cover - regular-file writes progress or raise
                raise OCIJournalError("OCI journal write made no progress")
            offset += written

    @staticmethod
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

    @classmethod
    def _read_blob(cls, path: Path, *, optional: bool) -> bytes | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            if optional:
                return None
            raise OCIJournalError(f"required OCI journal {path.name} is missing") from None
        except OSError as exc:
            raise OCIJournalError(f"OCI journal {path.name} is unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size > _MAX_JOURNAL_BYTES
            ):
                raise OCIJournalError(f"OCI journal {path.name} custody metadata is unsafe")
            payload = bytearray()
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(descriptor)
            if cls._stat_identity(before) != cls._stat_identity(after):
                raise OCIJournalError(f"OCI journal {path.name} changed while read")
            return bytes(payload)
        finally:
            os.close(descriptor)

    @classmethod
    def _load_model(cls, path: Path, model: type, *, optional: bool):  # type: ignore[no-untyped-def]
        payload = cls._read_blob(path, optional=optional)
        if payload is None:
            return None
        try:
            value = model.model_validate_json(payload)
        except ValidationError as exc:
            raise OCIJournalError(f"OCI journal {path.name} failed closed validation") from exc
        if canonical_json_bytes(value) != payload:
            raise OCIJournalError(f"OCI journal {path.name} is not canonical")
        return value

    @classmethod
    def _load_required(cls, path: Path, model: type):  # type: ignore[no-untyped-def]
        value = cls._load_model(path, model, optional=False)
        assert value is not None
        return value

    def _utc_now(self) -> datetime:
        value = self._clock.now()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise OCIJournalError("OCI runtime clock must return timezone-aware UTC")
        return value

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


# Descriptive aliases for callers that prefer the shorter adapter name.
LocalOCIRuntime = LocalQualificationOCIRuntime
OCIQualificationRuntime = LocalQualificationOCIRuntime
QualificationOCIRuntime = LocalQualificationOCIRuntime


__all__ = [
    "DeploymentPinnedOCIPolicy",
    "OCIDeadlineWatchdogController",
    "OCIDeviceBinding",
    "OCIDeviceFenceController",
    "OCIDevicePathPin",
    "OCIEngineError",
    "OCIExecutionPlan",
    "OCIJournalError",
    "OCILaunchGateVerifier",
    "OCIMountSpec",
    "OCIOutputQuotaController",
    "OCIPolicyRejected",
    "OCIProductionCapability",
    "OCIProductionCapabilityError",
    "OCIWatchdogCleanupQuiescence",
    "OCIQualificationRuntime",
    "OCIConfiguration",
    "OCIRuntimeClock",
    "OCIRuntimeError",
    "LocalOCIRuntime",
    "LocalQualificationOCIRuntime",
    "QualificationOCIRuntime",
    "SystemOCIRuntimeClock",
]
