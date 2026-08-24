"""Qualification-only, pull-based local execution node agent.

This module intentionally stops at the engineering boundary.  It can run only a deployment-
pinned, replay-safe :class:`WorkOrderNode` in a networkless sandbox and can emit node-signed
runtime/output evidence.  It cannot admit scientific evidence, invent a command, read execution
database tables, or turn a legacy queue message into execution authority.

This PR-4a slice is a qualification facade and fault harness, not a composed production runtime.
Potential PostgreSQL-allocator and container-runtime adapters are represented only by the narrow
protocols below.  Keeping them local is deliberate: a future runtime receives a closed
``RuntimeLaunchRequest`` rather than caller-controlled argv, and this module never imports the
allocator's private persistence records.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReason,
    AttemptAdoptionReceipt,
    EngineeringQualificationGrant,
    NodeExecutionReceipt,
    NodeRuntimeIdentity,
    QualificationAuthorityVerifier,
    QualificationVerificationError,
    RuntimeInspectionReceipt,
    RuntimeInspectionState,
    WorkerNodeAuthorityVerifier,
    issue_attempt_adoption_receipt,
    issue_node_execution_receipt,
    issue_runtime_inspection_receipt,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ExecutionEffectClass,
    ExecutionIntent,
    ExecutionModel,
    ExecutionRetryMode,
    NetworkPolicy,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.protocols.schemas import WorkOrderNode

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_EXECUTION_ID_PATTERN = r"^exe_[0-9a-f]{32}$"
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")
_SHELL_EXECUTABLES = frozenset(
    {
        "ash",
        "bash",
        "busybox",
        "csh",
        "dash",
        "fish",
        "env",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "su",
        "sudo",
        "tcsh",
        "zsh",
        "xargs",
    }
)
_SENSITIVE_ENV_FRAGMENTS = (
    "CREDENTIAL",
    "DATABASE",
    "DB_PASSWORD",
    "DOCKER_",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SIGNING_KEY",
    "TOKEN",
)
_SENSITIVE_ENV_PREFIXES = (
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "MYSQL_",
    "PG",
    "POSTGRES_",
    "REDIS_",
)
_STATE_SCHEMA_KEYS = frozenset(
    {
        "schema_name",
        "schema_version",
        "attempt_id",
        "execution_id",
        "intent_sha256",
        "node_id",
        "boot_id",
        "launch_spec_sha256",
        "launch_committed",
        "running_confirmed",
        "phase",
        "fencing_epoch",
        "lease_token_sha256",
        "runtime_identity",
        "inspection_sequence",
        "adoption_sequence",
    }
)


class NodeAgentError(RuntimeError):
    """Base class for fail-closed node-agent decisions."""


class AssignmentRejected(NodeAgentError):
    """An assignment differs from its qualification, WorkOrder, node, or launch pin."""


class LocalStateError(NodeAgentError):
    """Node-local state, token, lock, or workspace custody is unsafe."""


class NodeLeaseRejected(NodeAgentError):
    """Allocator adapter signal that the supplied raw token or fence is no longer current."""


class RuntimeRejected(NodeAgentError):
    """A runtime returned identity or inspection evidence outside the pinned request."""


class OutputCollectionRejected(NodeAgentError):
    """A stopped runtime's output tree could not be quarantined exactly."""


class PinnedEnvironmentVariable(ExecutionModel):
    """One constructor-pinned, non-secret workload environment variable."""

    name: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=16_384)

    @model_validator(mode="after")
    def _environment_is_public_and_canonical(self) -> "PinnedEnvironmentVariable":
        if _ENVIRONMENT_NAME.fullmatch(self.name) is None:
            raise ValueError("workload environment variable name is not canonical")
        upper = self.name.upper()
        if (
            upper == "DOCKER_HOST"
            or upper.startswith(_SENSITIVE_ENV_PREFIXES)
            or any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS)
        ):
            raise ValueError("workload environment cannot contain credentials or control sockets")
        if "\x00" in self.value or "\n" in self.value or "\r" in self.value:
            raise ValueError("workload environment value contains a control delimiter")
        return self


class PinnedArtifactPath(ExecutionModel):
    """Exact declared artifact key to canonical output-relative path binding."""

    artifact_key: str = Field(min_length=1, max_length=192)
    relative_path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _path_is_relative_and_canonical(self) -> "PinnedArtifactPath":
        components = self.relative_path.split("/")
        if (
            self.relative_path.startswith("/")
            or "\\" in self.relative_path
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise ValueError("pinned artifact path must be a canonical relative path")
        return self


class PinnedLaunchSpec(ExecutionModel):
    """Deployment-pinned direct-exec sandbox specification.

    All isolation booleans are literals so deserializing a weaker launch specification fails at
    the type boundary.  There is no generic mounts field and no way to request a shell string.
    """

    schema_name: Literal["aletheia.pinned_qualification_launch_spec"] = (
        "aletheia.pinned_qualification_launch_spec"
    )
    schema_version: Literal[1] = 1
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    environment_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_engine: str = Field(min_length=1, max_length=128)
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    environment: tuple[PinnedEnvironmentVariable, ...] = ()
    artifact_paths: tuple[PinnedArtifactPath, ...] = Field(min_length=1, max_length=128)
    network_policy: Literal[NetworkPolicy.NONE] = NetworkPolicy.NONE
    direct_exec_only: Literal[True] = True
    inherit_host_environment: Literal[False] = False
    read_only_root_filesystem: Literal[True] = True
    input_mount_read_only: Literal[True] = True
    output_mount_only_writable: Literal[True] = True
    privileged: Literal[False] = False
    database_credentials_mounted: Literal[False] = False
    artifact_store_credentials_mounted: Literal[False] = False
    node_signing_key_mounted: Literal[False] = False
    docker_socket_mounted: Literal[False] = False

    @model_validator(mode="after")
    def _launch_is_closed_and_direct(self) -> "PinnedLaunchSpec":
        executable = self.argv[0]
        if (
            not executable.startswith("/")
            or executable.endswith("/")
            or os.path.basename(executable).lower() in _SHELL_EXECUTABLES
        ):
            raise ValueError("qualification workload requires a non-shell absolute executable")
        if any(
            not argument or "\x00" in argument or "\n" in argument or "\r" in argument
            for argument in self.argv
        ):
            raise ValueError("qualification workload argv must be nonempty direct-exec tokens")
        environment_names = tuple(item.name for item in self.environment)
        if environment_names != tuple(sorted(set(environment_names))):
            raise ValueError("pinned workload environment must be unique and canonical")
        artifact_keys = tuple(item.artifact_key for item in self.artifact_paths)
        artifact_paths = tuple(item.relative_path for item in self.artifact_paths)
        if artifact_keys != tuple(sorted(set(artifact_keys))):
            raise ValueError("pinned artifact keys must be unique and canonical")
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("pinned artifacts cannot share one output path")
        return self

    @property
    def launch_spec_sha256(self) -> str:
        return canonical_sha256(self)


class PinnedLaunchRegistry:
    """Immutable exact-match registry populated only by deployment construction."""

    def __init__(self, specs: tuple[PinnedLaunchSpec, ...]) -> None:
        if not specs:
            raise ValueError("qualification launch registry must be nonempty")
        validated = tuple(
            PinnedLaunchSpec.model_validate(item.model_dump(mode="python")) for item in specs
        )
        keys = tuple(self._key(item) for item in validated)
        if len(set(keys)) != len(keys):
            raise ValueError("qualification launch registry repeats an exact binding")
        self._specs = dict(zip(keys, validated, strict=True))

    @staticmethod
    def _key(spec: PinnedLaunchSpec) -> tuple[str, str, str]:
        return (
            spec.command_sha256,
            spec.environment_sha256,
            spec.capability_manifest_sha256,
        )

    def resolve(self, node: WorkOrderNode) -> PinnedLaunchSpec | None:
        return self._specs.get(
            (node.command_sha256, node.environment_sha256, node.capability_manifest_sha256)
        )


@dataclass(frozen=True)
class ReservedDeviceBinding:
    """Exact accelerator lease projected into the runtime isolation request."""

    device_id: str
    hardware_uuid: str
    fencing_epoch: int
    requested_memory_bytes: int
    state: Literal["held"] = "held"


@dataclass(frozen=True)
class NodeReservation:
    """Minimum immutable allocator snapshot needed by a node; contains no raw token."""

    execution_id: str
    attempt_id: str
    intent_sha256: str
    admission_sha256: str
    grant_sha256: str
    node_id: str
    node_inventory_sha256: str
    resource_lease_sha256: str
    selected_resource_ids: tuple[str, ...]
    cpu_cores: int
    memory_bytes: int
    scratch_bytes: int
    exclusive: bool
    device_leases: tuple[ReservedDeviceBinding, ...]
    status: str
    fencing_epoch: int
    lease_token_sha256: str
    lease_expires_at: datetime
    hard_deadline: datetime


@dataclass(frozen=True)
class QualificationAssignment:
    """Allocator-authenticated pull result for one already-admitted qualification attempt."""

    intent: ExecutionIntent
    work_order_node: WorkOrderNode
    qualification_grant: EngineeringQualificationGrant
    reservation: NodeReservation
    lease_token: str | None = None


@dataclass(frozen=True)
class RuntimeLabel:
    name: str
    value: str


@dataclass(frozen=True)
class RuntimeLaunchRequest:
    """Only value a runtime adapter may accept; it has no arbitrary command/mount escape hatch."""

    spec: PinnedLaunchSpec
    node_id: str
    boot_id: str
    execution_id: str
    attempt_id: str
    intent_sha256: str
    node_inventory_sha256: str
    resource_lease_sha256: str
    selected_resource_ids: tuple[str, ...]
    cpu_cores: int
    memory_bytes: int
    scratch_bytes: int
    exclusive: bool
    device_leases: tuple[ReservedDeviceBinding, ...]
    fencing_epoch: int
    lease_token_sha256: str
    runtime_id: str
    labels: tuple[RuntimeLabel, ...]
    input_root: Path
    output_root: Path
    output_quota_bytes: int
    deadline: datetime

    @property
    def enforced_placement_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.qualification_runtime_placement.v1",
                "node_id": self.node_id,
                "boot_id": self.boot_id,
                "execution_id": self.execution_id,
                "attempt_id": self.attempt_id,
                "intent_sha256": self.intent_sha256,
                "node_inventory_sha256": self.node_inventory_sha256,
                "resource_lease_sha256": self.resource_lease_sha256,
                "selected_resource_ids": self.selected_resource_ids,
                "cpu_cores": self.cpu_cores,
                "memory_bytes": self.memory_bytes,
                "scratch_bytes": self.scratch_bytes,
                "exclusive": self.exclusive,
                "device_leases": tuple(
                    {
                        "device_id": item.device_id,
                        "hardware_uuid": item.hardware_uuid,
                        "fencing_epoch": item.fencing_epoch,
                        "requested_memory_bytes": item.requested_memory_bytes,
                        "state": item.state,
                    }
                    for item in self.device_leases
                ),
                "fencing_epoch": self.fencing_epoch,
                "lease_token_sha256": self.lease_token_sha256,
                "output_quota_bytes": self.output_quota_bytes,
                "deadline": self.deadline.isoformat(),
            }
        )


class RuntimeObservation(ExecutionModel):
    """Runtime-adapter evidence which the node independently signs after exact checks."""

    state: RuntimeInspectionState
    runtime_identity: NodeRuntimeIdentity
    enforced_placement_sha256: str = Field(pattern=_SHA256_PATTERN)
    enforced_fencing_epoch: int = Field(ge=1)
    inspection_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    inspected_at: AwareDatetime
    inspected_monotonic_ns: int = Field(ge=0)
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    ended_at: AwareDatetime | None = None
    ended_monotonic_ns: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _observation_is_typed(self) -> "RuntimeObservation":
        terminal_values = (self.exit_code, self.ended_at, self.ended_monotonic_ns)
        if self.state is RuntimeInspectionState.TERMINATED:
            if any(item is None for item in terminal_values):
                raise ValueError("terminated runtime observation requires exact exit evidence")
        elif any(item is not None for item in terminal_values):
            raise ValueError("only a terminated runtime observation may carry exit evidence")
        if (
            self.inspected_at < self.runtime_identity.started_at
            or self.inspected_monotonic_ns < self.runtime_identity.started_monotonic_ns
        ):
            raise ValueError("runtime inspection predates its exact runtime identity")
        if self.ended_at is not None and (
            self.ended_at < self.runtime_identity.started_at
            or self.ended_at > self.inspected_at
            or self.ended_monotonic_ns is None
            or self.ended_monotonic_ns < self.runtime_identity.started_monotonic_ns
            or self.ended_monotonic_ns > self.inspected_monotonic_ns
        ):
            raise ValueError("runtime exit evidence is out of order")
        return self


class QualificationRuntimePort(Protocol):
    """Non-production facade for a deterministic network-none qualification runtime.

    Methods never accept free-form argv or mounts.  A concrete adapter must remain disabled until
    it can independently enforce and attest every projected resource/device binding and provide a
    crash-idempotent runtime fence-rebind operation for adoption.  That composition operation is
    intentionally not claimed by this PR-4a facade or its fake fault harness.
    """

    # ``prepare`` may create only durable engine metadata.  It must not create or start the
    # sandbox/process; ``ensure_started`` is the sole launch operation after allocator authority.
    def prepare(self, *, request: RuntimeLaunchRequest) -> NodeRuntimeIdentity: ...

    def inspect(
        self, *, request: RuntimeLaunchRequest, identity: NodeRuntimeIdentity
    ) -> RuntimeObservation: ...

    def ensure_started(
        self, *, request: RuntimeLaunchRequest, identity: NodeRuntimeIdentity
    ) -> None: ...


class QualificationInputMaterializerPort(Protocol):
    """Copy only already-verified input objects into the attempt-scoped staging directory."""

    def ensure_verified_inputs(self, *, intent: ExecutionIntent, destination: Path) -> str:
        """Idempotently materialize/reverify exact bytes and return a bound SHA-256 receipt."""
        ...


class ArtifactQuarantinePort(Protocol):
    """Exact output quarantine; concrete stores independently reject undeclared tree entries."""

    def quarantine_outputs(
        self,
        *,
        intent: ExecutionIntent,
        output_root: Path,
        artifact_paths: Mapping[str, str],
        produced_at: datetime,
        allow_partial: bool = False,
    ) -> ArtifactManifest: ...


class NodeAllocatorPort(Protocol):
    """Fenced allocator facade.  Every authority mutation carries raw token plus epoch."""

    def pull_qualification_assignment(
        self, *, node_id: str, node_manifest_sha256: str
    ) -> QualificationAssignment | None: ...

    def start_attempt(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_identity: NodeRuntimeIdentity,
    ) -> NodeReservation: ...

    def mark_running(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> NodeReservation: ...

    def heartbeat(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> NodeReservation: ...

    def retain_reconciliation(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
        reason: str,
    ) -> NodeReservation: ...

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
    ) -> NodeReservation: ...

    def mark_terminated(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
    ) -> NodeReservation: ...

    def mark_verifying(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_execution_receipt: NodeExecutionReceipt,
        disposition: "NodeTerminalDisposition",
    ) -> NodeReservation: ...


class NodeClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class SystemNodeClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class AttemptPhase(str, Enum):
    PREPARED = "prepared"
    START_AUTHORIZED = "start_authorized"
    LAUNCH_COMMITTED = "launch_committed"
    RUNNING = "running"
    TERMINATED = "terminated"
    VERIFYING = "verifying"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True)
class _AttemptState:
    attempt_id: str
    execution_id: str
    intent_sha256: str
    node_id: str
    boot_id: str
    launch_spec_sha256: str
    launch_committed: bool
    running_confirmed: bool
    phase: AttemptPhase
    fencing_epoch: int
    lease_token_sha256: str
    runtime_identity: NodeRuntimeIdentity
    inspection_sequence: int = 0
    adoption_sequence: int = 0

    def payload(self) -> dict[str, object]:
        return {
            "schema_name": "aletheia.qualification_node_attempt_state",
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "execution_id": self.execution_id,
            "intent_sha256": self.intent_sha256,
            "node_id": self.node_id,
            "boot_id": self.boot_id,
            "launch_spec_sha256": self.launch_spec_sha256,
            "launch_committed": self.launch_committed,
            "running_confirmed": self.running_confirmed,
            "phase": self.phase.value,
            "fencing_epoch": self.fencing_epoch,
            "lease_token_sha256": self.lease_token_sha256,
            "runtime_identity": self.runtime_identity.model_dump(mode="json"),
            "inspection_sequence": self.inspection_sequence,
            "adoption_sequence": self.adoption_sequence,
        }

    @classmethod
    def parse(cls, payload: object) -> "_AttemptState":
        if not isinstance(payload, dict) or frozenset(payload) != _STATE_SCHEMA_KEYS:
            raise LocalStateError("node attempt state is not a closed schema")
        if (
            payload.get("schema_name") != "aletheia.qualification_node_attempt_state"
            or payload.get("schema_version") != 1
        ):
            raise LocalStateError("node attempt state schema identity is unsupported")
        try:
            if (
                type(payload["launch_committed"]) is not bool
                or type(payload["running_confirmed"]) is not bool
            ):
                raise ValueError("attempt state phase flags must be booleans")
            state = cls(
                attempt_id=str(payload["attempt_id"]),
                execution_id=str(payload["execution_id"]),
                intent_sha256=str(payload["intent_sha256"]),
                node_id=str(payload["node_id"]),
                boot_id=str(payload["boot_id"]),
                launch_spec_sha256=str(payload["launch_spec_sha256"]),
                launch_committed=bool(payload["launch_committed"]),
                running_confirmed=bool(payload["running_confirmed"]),
                phase=AttemptPhase(str(payload["phase"])),
                fencing_epoch=int(payload["fencing_epoch"]),
                lease_token_sha256=str(payload["lease_token_sha256"]),
                runtime_identity=NodeRuntimeIdentity.model_validate(payload["runtime_identity"]),
                inspection_sequence=int(payload["inspection_sequence"]),
                adoption_sequence=int(payload["adoption_sequence"]),
            )
            if (
                re.fullmatch(_ATTEMPT_ID_PATTERN, state.attempt_id) is None
                or re.fullmatch(_EXECUTION_ID_PATTERN, state.execution_id) is None
                or re.fullmatch(_SHA256_PATTERN, state.intent_sha256) is None
                or re.fullmatch(_SHA256_PATTERN, state.launch_spec_sha256) is None
                or re.fullmatch(_SHA256_PATTERN, state.lease_token_sha256) is None
                or _SAFE_LABEL.fullmatch(state.node_id) is None
                or _SAFE_LABEL.fullmatch(state.boot_id) is None
                or state.fencing_epoch < 1
                or state.inspection_sequence < 0
                or state.adoption_sequence < 0
                or (state.running_confirmed and not state.launch_committed)
                or (
                    state.phase in {AttemptPhase.PREPARED, AttemptPhase.START_AUTHORIZED}
                    and (state.launch_committed or state.running_confirmed)
                )
                or (
                    state.phase is AttemptPhase.LAUNCH_COMMITTED
                    and (not state.launch_committed or state.running_confirmed)
                )
                or (
                    state.phase is AttemptPhase.RUNNING
                    and (not state.launch_committed or not state.running_confirmed)
                )
                or (
                    state.phase in {AttemptPhase.TERMINATED, AttemptPhase.VERIFYING}
                    and not state.launch_committed
                )
            ):
                raise ValueError("attempt state phase or identity invariant is invalid")
            return state
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("node attempt state failed closed validation") from exc


@dataclass(frozen=True)
class _LockEvidence:
    attempt_id: str
    evidence_sha256: str
    checked_monotonic_ns: int
    device: int
    inode: int
    descriptor: int = field(repr=False, compare=False)

    def revalidate(self, *, monotonic_ns: int) -> "_LockEvidence":
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = os.fstat(self.descriptor)
        except (BlockingIOError, OSError) as exc:
            raise LocalStateError("node singleton lock ownership was lost") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_dev != self.device
            or metadata.st_ino != self.inode
            or monotonic_ns < self.checked_monotonic_ns
        ):
            raise LocalStateError("node singleton lock identity changed during adoption")
        return _LockEvidence(
            attempt_id=self.attempt_id,
            evidence_sha256=canonical_sha256(
                {
                    "schema": "aletheia.node_singleton_lock_revalidation.v1",
                    "attempt_id": self.attempt_id,
                    "acquisition_evidence_sha256": self.evidence_sha256,
                    "acquired_monotonic_ns": self.checked_monotonic_ns,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "nlink": metadata.st_nlink,
                    "revalidated_monotonic_ns": monotonic_ns,
                }
            ),
            checked_monotonic_ns=monotonic_ns,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            descriptor=self.descriptor,
        )


class NodeLocalStateStore:
    """Crash-durable 0600 attempt/token state plus one per-attempt nonblocking flock."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if candidate.is_symlink():
            raise LocalStateError("node state root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(candidate, 0o700)
        self.root = candidate.resolve(strict=True)
        root_descriptor = self._open_directory(self.root)
        try:
            for name in (
                "adoptions",
                "attempts",
                "tokens",
                "locks",
                "preparations",
                "results",
                "terminations",
                "workspaces",
            ):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
                    os.fsync(root_descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_descriptor,
                )
                try:
                    metadata = os.fstat(child)
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o700
                    ):
                        raise LocalStateError("node state directory must have mode 0700")
                finally:
                    os.close(child)
        finally:
            os.close(root_descriptor)

    @staticmethod
    def _open_directory(path: Path) -> int:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
            os.close(descriptor)
            raise LocalStateError("node state directory must be private mode 0700")
        return descriptor

    @staticmethod
    def _key(attempt_id: str) -> str:
        if re.fullmatch(_ATTEMPT_ID_PATTERN, attempt_id) is None:
            raise LocalStateError("node state attempt id is invalid")
        return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_private_file(descriptor: int, *, label: str) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise LocalStateError(f"{label} must be a regular nlink=1 mode-0600 file")

    def _child_directory(self, name: str) -> int:
        root = self._open_directory(self.root)
        try:
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root,
            )
        finally:
            os.close(root)
        metadata = os.fstat(child)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            os.close(child)
            raise LocalStateError("node state child directory is unsafe")
        return child

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:  # pragma: no cover - regular files progress or raise
                raise LocalStateError("node state write made no progress")
            offset += written

    def _atomic_write(self, directory: str, name: str, payload: bytes) -> None:
        parent = self._child_directory(directory)
        temporary = f".{name}.{secrets.token_hex(16)}.tmp"
        descriptor: int | None = None
        try:
            try:
                existing = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                try:
                    self._validate_private_file(existing, label="existing node state")
                finally:
                    os.close(existing)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            self._validate_private_file(descriptor, label="temporary node state")
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            published = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                self._validate_private_file(published, label="published node state")
            finally:
                os.close(published)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def _read(self, directory: str, name: str, *, optional: bool) -> bytes | None:
        parent = self._child_directory(directory)
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                if optional:
                    return None
                raise LocalStateError("required node-local custody file is missing") from None
            try:
                self._validate_private_file(descriptor, label="node-local custody file")
                before = os.fstat(descriptor)
                if before.st_size > 1024 * 1024:
                    raise LocalStateError("node-local custody file exceeds its bound")
                payload = bytearray()
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
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
                    raise LocalStateError("node-local custody file changed while read")
                return bytes(payload)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def save_state(self, state: _AttemptState) -> None:
        self._atomic_write(
            "attempts", f"{self._key(state.attempt_id)}.json", canonical_json_bytes(state.payload())
        )

    def load_state(self, attempt_id: str) -> _AttemptState | None:
        payload = self._read("attempts", f"{self._key(attempt_id)}.json", optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalStateError("node attempt state is not canonical JSON") from exc
        state = _AttemptState.parse(decoded)
        if canonical_json_bytes(state.payload()) != payload:
            raise LocalStateError("node attempt state bytes are not canonical")
        return state

    def save_preparation_scope(
        self, *, attempt_id: str, intent_sha256: str, launch_spec_sha256: str
    ) -> None:
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_preparation_scope",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "intent_sha256": intent_sha256,
                "launch_spec_sha256": launch_spec_sha256,
            }
        )
        name = f"{self._key(attempt_id)}.scope.json"
        existing = self._read("preparations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("preparation scope is rebound to different authority")
            return
        self._atomic_write("preparations", name, payload)

    def save_input_materialization(self, *, attempt_id: str, materialization_sha256: str) -> None:
        if re.fullmatch(_SHA256_PATTERN, materialization_sha256) is None:
            raise LocalStateError("input materialization identity is not SHA-256")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_input_materialization",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "materialization_sha256": materialization_sha256,
            }
        )
        name = f"{self._key(attempt_id)}.input.json"
        existing = self._read("preparations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("input materialization changed across recovery")
            return
        self._atomic_write("preparations", name, payload)

    def save_terminal_result(
        self,
        *,
        attempt_id: str,
        manifest: ArtifactManifest,
        receipt: NodeExecutionReceipt,
        disposition: "NodeTerminalDisposition",
    ) -> None:
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_terminal_result",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "artifact_manifest": manifest.model_dump(mode="json"),
                "node_execution_receipt": receipt.model_dump(mode="json"),
                "disposition": disposition.value,
            }
        )
        sequence = receipt.termination_inspection_receipt.inspection_sequence
        name = f"{self._key(attempt_id)}.{sequence}.json"
        existing = self._read("results", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("terminal result is already bound to different exact bytes")
            return
        self._atomic_write("results", name, payload)

    def load_terminal_result(
        self, attempt_id: str, *, inspection_sequence: int
    ) -> tuple[ArtifactManifest, NodeExecutionReceipt, "NodeTerminalDisposition"] | None:
        payload = self._read(
            "results",
            f"{self._key(attempt_id)}.{inspection_sequence}.json",
            optional=True,
        )
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "artifact_manifest",
                "node_execution_receipt",
                "disposition",
            }:
                raise ValueError("terminal result is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_node_terminal_result"
                or decoded["schema_version"] != 1
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("terminal result scope differs")
            manifest = ArtifactManifest.model_validate(decoded["artifact_manifest"])
            receipt = NodeExecutionReceipt.model_validate(decoded["node_execution_receipt"])
            disposition = NodeTerminalDisposition(decoded["disposition"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("node terminal result failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_terminal_result",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "artifact_manifest": manifest.model_dump(mode="json"),
                "node_execution_receipt": receipt.model_dump(mode="json"),
                "disposition": disposition.value,
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("node terminal result bytes are not canonical")
        if (
            receipt.infrastructure_attempt_id != attempt_id
            or receipt.artifact_manifest_sha256 != manifest.manifest_sha256
            or receipt.termination_inspection_receipt.inspection_sequence != inspection_sequence
        ):
            raise LocalStateError("node terminal result receipt and manifest diverge")
        return manifest, receipt, disposition

    def save_pending_termination(
        self,
        *,
        attempt_id: str,
        observation: RuntimeObservation,
        receipt: RuntimeInspectionReceipt,
    ) -> None:
        if (
            receipt.runtime_identity.infrastructure_attempt_id != attempt_id
            or observation.runtime_identity != receipt.runtime_identity
            or observation.state != receipt.state
            or observation.state
            not in {RuntimeInspectionState.TERMINATED, RuntimeInspectionState.ABSENT}
            or observation.inspection_evidence_sha256 != receipt.inspection_evidence_sha256
            or observation.inspected_at != receipt.inspected_at
            or observation.inspected_monotonic_ns != receipt.inspected_monotonic_ns
        ):
            raise LocalStateError("pending termination differs from its exact signed observation")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_pending_termination",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "runtime_observation": observation.model_dump(mode="json"),
                "runtime_inspection_receipt": receipt.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.{receipt.inspection_sequence}.json"
        existing = self._read("terminations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("pending termination is rebound to different exact bytes")
            return
        self._atomic_write("terminations", name, payload)

    def load_pending_termination(
        self, *, attempt_id: str, inspection_sequence: int
    ) -> tuple[RuntimeObservation, RuntimeInspectionReceipt] | None:
        payload = self._read(
            "terminations",
            f"{self._key(attempt_id)}.{inspection_sequence}.json",
            optional=True,
        )
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "runtime_observation",
                "runtime_inspection_receipt",
            }:
                raise ValueError("pending termination is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_node_pending_termination"
                or decoded["schema_version"] != 1
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("pending termination scope differs")
            observation = RuntimeObservation.model_validate(decoded["runtime_observation"])
            receipt = RuntimeInspectionReceipt.model_validate(decoded["runtime_inspection_receipt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("pending termination failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_pending_termination",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "runtime_observation": observation.model_dump(mode="json"),
                "runtime_inspection_receipt": receipt.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("pending termination bytes are not canonical")
        if (
            receipt.inspection_sequence != inspection_sequence
            or receipt.runtime_identity.infrastructure_attempt_id != attempt_id
            or observation.runtime_identity != receipt.runtime_identity
            or observation.state != receipt.state
            or observation.state
            not in {RuntimeInspectionState.TERMINATED, RuntimeInspectionState.ABSENT}
            or observation.inspection_evidence_sha256 != receipt.inspection_evidence_sha256
            or observation.inspected_at != receipt.inspected_at
            or observation.inspected_monotonic_ns != receipt.inspected_monotonic_ns
        ):
            raise LocalStateError("pending termination exact binding differs")
        return observation, receipt

    def save_pending_adoption(
        self,
        *,
        attempt_id: str,
        receipt: AttemptAdoptionReceipt,
        supersedes_adoption_receipt_sha256: str | None = None,
    ) -> None:
        if (
            supersedes_adoption_receipt_sha256 is not None
            and re.fullmatch(_SHA256_PATTERN, supersedes_adoption_receipt_sha256) is None
        ):
            raise LocalStateError("superseded adoption identity is not SHA-256")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_pending_adoption",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "adoption_receipt": receipt.model_dump(mode="json"),
                "supersedes_adoption_receipt_sha256": (
                    supersedes_adoption_receipt_sha256 or "none"
                ),
            }
        )
        name = (
            f"{self._key(attempt_id)}.{receipt.adoption_sequence}."
            f"{receipt.runtime_inspection_receipt.inspection_sequence}.json"
        )
        existing = self._read("adoptions", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("pending adoption is rebound to different exact bytes")
            return
        self._atomic_write("adoptions", name, payload)

    def load_pending_adoption(
        self, *, attempt_id: str, sequence: int, inspection_sequence: int
    ) -> AttemptAdoptionReceipt | None:
        payload = self._read(
            "adoptions",
            f"{self._key(attempt_id)}.{sequence}.{inspection_sequence}.json",
            optional=True,
        )
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "adoption_receipt",
                "supersedes_adoption_receipt_sha256",
            }:
                raise ValueError("pending adoption is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_node_pending_adoption"
                or decoded["schema_version"] != 1
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("pending adoption scope differs")
            receipt = AttemptAdoptionReceipt.model_validate(decoded["adoption_receipt"])
            supersedes = decoded["supersedes_adoption_receipt_sha256"]
            if supersedes != "none" and re.fullmatch(_SHA256_PATTERN, supersedes) is None:
                raise ValueError("pending adoption supersession identity is invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("pending adoption failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_pending_adoption",
                "schema_version": 1,
                "attempt_id": attempt_id,
                "adoption_receipt": receipt.model_dump(mode="json"),
                "supersedes_adoption_receipt_sha256": supersedes,
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("pending adoption bytes are not canonical")
        if (
            receipt.infrastructure_attempt_id != attempt_id
            or receipt.adoption_sequence != sequence
            or receipt.runtime_inspection_receipt.inspection_sequence != inspection_sequence
        ):
            raise LocalStateError("pending adoption identity differs from its journal slot")
        return receipt

    def load_latest_pending_adoption(
        self,
        *,
        attempt_id: str,
        sequence: int,
        at_or_before_inspection_sequence: int,
    ) -> AttemptAdoptionReceipt | None:
        """Return the newest immutable proof for one still-uncommitted rotation."""

        key = self._key(attempt_id)
        pattern = re.compile(rf"^{re.escape(key)}\.{sequence}\.([1-9][0-9]*)\.json$")
        parent = self._child_directory("adoptions")
        try:
            candidates = tuple(
                inspection_sequence
                for name in os.listdir(parent)
                if (match := pattern.fullmatch(name)) is not None
                and (inspection_sequence := int(match.group(1))) <= at_or_before_inspection_sequence
            )
        finally:
            os.close(parent)
        if not candidates:
            return None
        return self.load_pending_adoption(
            attempt_id=attempt_id,
            sequence=sequence,
            inspection_sequence=max(candidates),
        )

    def save_token(self, *, attempt_id: str, fencing_epoch: int, token: str) -> None:
        if fencing_epoch < 1 or len(token) < 43 or any(ord(character) < 33 for character in token):
            raise LocalStateError("raw lease token is weak or contains a delimiter")
        name = f"{self._key(attempt_id)}.{fencing_epoch}.token"
        existing = self._read("tokens", name, optional=True)
        payload = token.encode("utf-8")
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("node-local fence token is already bound to another value")
            return
        self._atomic_write("tokens", name, payload)

    def load_token(self, *, attempt_id: str, fencing_epoch: int, expected_sha256: str) -> str:
        name = f"{self._key(attempt_id)}.{fencing_epoch}.token"
        payload = self._read("tokens", name, optional=False)
        assert payload is not None
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise LocalStateError("node-local raw token differs from allocator token hash")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalStateError("node-local raw token is not UTF-8") from exc

    def load_existing_token(self, *, attempt_id: str, fencing_epoch: int) -> str | None:
        """Load an immutable next-fence rotation intent before minting any new token."""

        name = f"{self._key(attempt_id)}.{fencing_epoch}.token"
        payload = self._read("tokens", name, optional=True)
        if payload is None:
            return None
        try:
            token = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalStateError("node-local raw token is not UTF-8") from exc
        if len(token) < 43 or any(ord(character) < 33 for character in token):
            raise LocalStateError("node-local rotation token is weak or contains a delimiter")
        return token

    @contextmanager
    def attempt_lock(self, *, attempt_id: str, monotonic_ns: int):
        parent = self._child_directory("locks")
        name = f"{self._key(attempt_id)}.lock"
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent,
        )
        try:
            self._validate_private_file(descriptor, label="node attempt lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            metadata = os.fstat(descriptor)
            evidence = _LockEvidence(
                attempt_id=attempt_id,
                evidence_sha256=canonical_sha256(
                    {
                        "schema": "aletheia.node_singleton_lock_evidence.v1",
                        "attempt_id": attempt_id,
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "mode": stat.S_IMODE(metadata.st_mode),
                        "nlink": metadata.st_nlink,
                        "checked_monotonic_ns": monotonic_ns,
                    }
                ),
                checked_monotonic_ns=monotonic_ns,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                descriptor=descriptor,
            )
            yield evidence
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                os.close(parent)

    def workspace(self, attempt_id: str) -> tuple[Path, Path]:
        workspace_root = self.root / "workspaces"
        key = self._key(attempt_id)
        attempt_root = workspace_root / key
        if attempt_root.is_symlink():
            raise LocalStateError("attempt workspace cannot be a symlink")
        attempt_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(attempt_root, 0o700)
        input_root = attempt_root / "input"
        output_root = attempt_root / "output"
        for path in (input_root, output_root):
            if path.is_symlink():
                raise LocalStateError("attempt workspace child cannot be a symlink")
            path.mkdir(mode=0o700, exist_ok=True)
            if not path.is_dir():
                raise LocalStateError("attempt workspace child is not a directory")
        os.chmod(output_root, 0o700)
        return input_root, output_root


class NodeRunOutcome(str, Enum):
    IDLE = "idle"
    LOCKED_BY_PEER = "locked_by_peer"
    RUNNING = "running"
    ADOPTED = "adopted"
    COLLECTED = "collected"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class NodeTerminalDisposition(str, Enum):
    PROCESS_SUCCEEDED = "process_succeeded"
    PROCESS_FAILED = "process_failed"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class NodeRunResult:
    outcome: NodeRunOutcome
    attempt_id: str | None = None
    runtime_identity: NodeRuntimeIdentity | None = None
    inspection_receipt: RuntimeInspectionReceipt | None = None
    adoption_receipt: AttemptAdoptionReceipt | None = None
    artifact_manifest: ArtifactManifest | None = None
    node_execution_receipt: NodeExecutionReceipt | None = None
    terminal_disposition: NodeTerminalDisposition | None = None


class QualificationNodeAgent:
    """One enrolled local worker restricted to signed engineering qualifications."""

    def __init__(
        self,
        *,
        node_authority: WorkerNodeAuthorityVerifier,
        qualification_authority: QualificationAuthorityVerifier,
        node_signing_private_key: bytes,
        boot_id: str,
        allocator_principal_id: str,
        allocator: NodeAllocatorPort,
        runtime: QualificationRuntimePort,
        artifact_quarantine: ArtifactQuarantinePort,
        launch_registry: PinnedLaunchRegistry,
        state_store: NodeLocalStateStore,
        input_materializer: QualificationInputMaterializerPort | None = None,
        clock: NodeClock | None = None,
        inspection_ttl_seconds: int = 10,
    ) -> None:
        if not _SAFE_LABEL.fullmatch(boot_id):
            raise ValueError("node boot id is not canonical")
        if not _SAFE_LABEL.fullmatch(allocator_principal_id):
            raise ValueError("allocator principal id is not canonical")
        if inspection_ttl_seconds < 1 or inspection_ttl_seconds > 60:
            raise ValueError("runtime inspection TTL must be inside 1..60 seconds")
        try:
            public_key = (
                Ed25519PrivateKey.from_private_bytes(node_signing_private_key)
                .public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except ValueError as exc:
            raise ValueError("node signing private key must contain exactly 32 raw bytes") from exc
        if public_key != node_authority.manifest.node_signing_public_key_ed25519_hex:
            raise ValueError("node signing private key differs from enrolled manifest")
        self._node_authority = node_authority
        self._qualification_authority = qualification_authority
        self._private_key = bytes(node_signing_private_key)
        self._boot_id = boot_id
        self._allocator_principal_id = allocator_principal_id
        self._allocator = allocator
        self._runtime = runtime
        self._artifact_quarantine = artifact_quarantine
        self._registry = launch_registry
        self._state = state_store
        self._input_materializer = input_materializer
        self._clock = clock or SystemNodeClock()
        self._inspection_ttl = timedelta(seconds=inspection_ttl_seconds)

    def run_once(self) -> NodeRunResult:
        assignment = self._allocator.pull_qualification_assignment(
            node_id=self._node_authority.manifest.node_id,
            node_manifest_sha256=self._node_authority.manifest.manifest_sha256,
        )
        if assignment is None:
            return NodeRunResult(outcome=NodeRunOutcome.IDLE)
        return self.run_assignment(assignment)

    def run_assignment(self, assignment: QualificationAssignment) -> NodeRunResult:
        intent, node, grant, reservation, spec = self._validate_assignment(assignment)
        attempt_id = reservation.attempt_id
        with self._state.attempt_lock(
            attempt_id=attempt_id, monotonic_ns=self._clock.monotonic_ns()
        ) as initial_lock:
            if initial_lock is None:
                return NodeRunResult(outcome=NodeRunOutcome.LOCKED_BY_PEER, attempt_id=attempt_id)
            token = self._resolve_token(assignment)
            input_root, output_root = self._state.workspace(attempt_id)
            request = self._launch_request(
                intent=intent,
                reservation=reservation,
                spec=spec,
                input_root=input_root,
                output_root=output_root,
            )
            state = self._state.load_state(attempt_id)
            if state is None:
                self._state.save_preparation_scope(
                    attempt_id=attempt_id,
                    intent_sha256=intent.intent_sha256,
                    launch_spec_sha256=spec.launch_spec_sha256,
                )
                materialization_sha256 = self._materialize_and_seal_inputs(
                    intent=intent, input_root=input_root
                )
                self._state.save_input_materialization(
                    attempt_id=attempt_id,
                    materialization_sha256=materialization_sha256,
                )
                runtime_identity = self._runtime.prepare(request=request)
                self._validate_runtime_identity(runtime_identity, request=request)
                state = _AttemptState(
                    attempt_id=attempt_id,
                    execution_id=intent.execution_id,
                    intent_sha256=intent.intent_sha256,
                    node_id=reservation.node_id,
                    boot_id=self._boot_id,
                    launch_spec_sha256=spec.launch_spec_sha256,
                    launch_committed=False,
                    running_confirmed=False,
                    phase=AttemptPhase.PREPARED,
                    fencing_epoch=reservation.fencing_epoch,
                    lease_token_sha256=reservation.lease_token_sha256,
                    runtime_identity=runtime_identity,
                )
                self._state.save_state(state)
            else:
                pending_adoption = self._state.load_latest_pending_adoption(
                    attempt_id=attempt_id,
                    sequence=state.adoption_sequence + 1,
                    at_or_before_inspection_sequence=state.inspection_sequence,
                )
                if pending_adoption is not None:
                    return self._recover_pending_adoption(
                        state=state,
                        reservation=reservation,
                        token=token,
                        receipt=pending_adoption,
                        request=request,
                        lock_evidence=initial_lock,
                    )
                self._validate_recovered_state(state, request=request, reservation=reservation)
                self._validate_runtime_identity(state.runtime_identity, request=request)

            durable_terminal = self._state.load_terminal_result(
                attempt_id,
                inspection_sequence=state.inspection_sequence,
            )
            if durable_terminal is not None:
                manifest, receipt, disposition = durable_terminal
                if reservation.status == "reconciliation_required":
                    # The allocator did not durably accept central verification.  Re-enter the
                    # stopped-proof path; an immutable later proof/result generation may be
                    # issued after expiry without changing the quarantined manifest/tree.
                    durable_terminal = None
                elif reservation.status != "verifying" and not (
                    self._inspection_receipt_is_fresh(receipt.termination_inspection_receipt)
                ):
                    if reservation.status != "terminated":
                        state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
                        self._state.save_state(state)
                        return NodeRunResult(
                            outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                            attempt_id=attempt_id,
                            runtime_identity=state.runtime_identity,
                            artifact_manifest=manifest,
                            node_execution_receipt=receipt,
                            terminal_disposition=disposition,
                        )
                    # The allocator already durably accepted the old stopped proof.  Reinspect
                    # and issue a later proof/result generation before central verification.
                    durable_terminal = None
            if durable_terminal is not None:
                manifest, receipt, disposition = durable_terminal
                try:
                    if reservation.status == "verifying":
                        snapshot = reservation
                    elif reservation.status == "terminated":
                        snapshot = self._allocator.mark_verifying(
                            attempt_id=attempt_id,
                            lease_token=token,
                            fencing_epoch=state.fencing_epoch,
                            node_execution_receipt=receipt,
                            disposition=disposition,
                        )
                    else:
                        raise NodeLeaseRejected(
                            "durable terminal result has no allocator terminal lineage"
                        )
                    reservation = self._validate_allocator_response(
                        snapshot,
                        baseline=reservation,
                        expected_statuses=frozenset({"verifying"}),
                        expected_fencing_epoch=state.fencing_epoch,
                        expected_lease_token_sha256=state.lease_token_sha256,
                        require_live_authority=False,
                        operation="mark_verifying",
                    )
                except NodeLeaseRejected:
                    state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
                    self._state.save_state(state)
                    return NodeRunResult(
                        outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                        attempt_id=attempt_id,
                        runtime_identity=state.runtime_identity,
                        artifact_manifest=manifest,
                        node_execution_receipt=receipt,
                        terminal_disposition=disposition,
                    )
                state = replace(state, phase=AttemptPhase.VERIFYING)
                self._state.save_state(state)
                return NodeRunResult(
                    outcome=NodeRunOutcome.COLLECTED,
                    attempt_id=attempt_id,
                    runtime_identity=state.runtime_identity,
                    inspection_receipt=receipt.termination_inspection_receipt,
                    artifact_manifest=manifest,
                    node_execution_receipt=receipt,
                    terminal_disposition=disposition,
                )

            pending_termination = self._state.load_pending_termination(
                attempt_id=attempt_id,
                inspection_sequence=state.inspection_sequence,
            )
            if pending_termination is not None:
                pending_observation, pending_inspection = pending_termination
                if self._inspection_receipt_is_fresh(pending_inspection):
                    return self._collect_stopped(
                        intent=intent,
                        spec=spec,
                        reservation=reservation,
                        state=state,
                        token=token,
                        observation=pending_observation,
                        output_root=output_root,
                        persisted_inspection=pending_inspection,
                    )

            observation = self._validated_inspection(request=request, state=state)
            if reservation.status == "reconciliation_required":
                if not state.launch_committed and observation.state is not (
                    RuntimeInspectionState.UNKNOWN
                ):
                    return self._local_reconciliation(
                        state=state,
                        reason="prelaunch_runtime_observation_is_unauthorized",
                    )
                return self._recover_reconciliation(
                    intent=intent,
                    reservation=reservation,
                    request=request,
                    state=state,
                    token=token,
                    observation=observation,
                    lock_evidence=initial_lock,
                )
            if observation.state is RuntimeInspectionState.UNKNOWN:
                return self._retain_reconciliation(
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    reason="runtime_inspection_unknown",
                )
            if observation.state is RuntimeInspectionState.TERMINATED and not (
                state.launch_committed
            ):
                return self._local_reconciliation(
                    state=state,
                    reason="prelaunch_terminated_observation_is_unauthorized",
                )
            if observation.state is RuntimeInspectionState.TERMINATED or (
                observation.state is RuntimeInspectionState.ABSENT
                and state.launch_committed
                and state.running_confirmed
                and state.phase
                in {
                    AttemptPhase.RUNNING,
                    AttemptPhase.TERMINATED,
                    AttemptPhase.RECONCILIATION_REQUIRED,
                }
            ):
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    output_root=output_root,
                )
            if observation.state is RuntimeInspectionState.RUNNING:
                if not state.launch_committed:
                    return self._local_reconciliation(
                        state=state,
                        reason="prelaunch_running_observation_is_unauthorized",
                    )
                if state.phase is AttemptPhase.LAUNCH_COMMITTED:
                    try:
                        if reservation.status == "starting":
                            snapshot = self._allocator.mark_running(
                                attempt_id=attempt_id,
                                lease_token=token,
                                fencing_epoch=state.fencing_epoch,
                            )
                            reservation = self._validate_allocator_response(
                                snapshot,
                                baseline=reservation,
                                expected_statuses=frozenset({"running"}),
                                expected_fencing_epoch=state.fencing_epoch,
                                expected_lease_token_sha256=state.lease_token_sha256,
                                require_live_authority=True,
                                operation="mark_running",
                            )
                        elif reservation.status == "running":
                            reservation = self._validate_allocator_response(
                                reservation,
                                baseline=reservation,
                                expected_statuses=frozenset({"running"}),
                                expected_fencing_epoch=state.fencing_epoch,
                                expected_lease_token_sha256=state.lease_token_sha256,
                                require_live_authority=True,
                                operation="recovered_running",
                            )
                        elif reservation.status != "running":
                            return self._local_reconciliation(
                                state=state,
                                reason="launch_commit_allocator_state_is_ambiguous",
                            )
                    except NodeLeaseRejected:
                        return self._lease_loss_result(
                            reservation=reservation,
                            state=state,
                            token=token,
                            request=request,
                        )
                    state = replace(
                        state,
                        phase=AttemptPhase.RUNNING,
                        running_confirmed=True,
                    )
                    self._state.save_state(state)
                return self._heartbeat_running(reservation=reservation, state=state, token=token)

            if (
                observation.state is RuntimeInspectionState.ABSENT
                and state.phase is AttemptPhase.LAUNCH_COMMITTED
            ):
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    output_root=output_root,
                )
            try:
                snapshot = self._allocator.start_attempt(
                    attempt_id=attempt_id,
                    lease_token=token,
                    fencing_epoch=state.fencing_epoch,
                    runtime_identity=state.runtime_identity,
                )
                reservation = self._validate_allocator_response(
                    snapshot,
                    baseline=reservation,
                    expected_statuses=frozenset({"starting"}),
                    expected_fencing_epoch=state.fencing_epoch,
                    expected_lease_token_sha256=state.lease_token_sha256,
                    require_live_authority=True,
                    operation="start_attempt",
                )
            except NodeLeaseRejected:
                return self._lease_loss_result(
                    reservation=reservation,
                    state=state,
                    token=token,
                    request=request,
                )
            state = replace(state, phase=AttemptPhase.START_AUTHORIZED)
            self._state.save_state(state)
            # The durable local commit deliberately precedes the idempotent runtime call.  A
            # crash in the tiny gap fails closed as reconciliation instead of risking a duplicate.
            state = replace(
                state,
                phase=AttemptPhase.LAUNCH_COMMITTED,
                launch_committed=True,
            )
            self._state.save_state(state)
            self._runtime.ensure_started(request=request, identity=state.runtime_identity)
            observation = self._validated_inspection(request=request, state=state)
            if observation.state is RuntimeInspectionState.RUNNING:
                try:
                    snapshot = self._allocator.mark_running(
                        attempt_id=attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                    )
                    reservation = self._validate_allocator_response(
                        snapshot,
                        baseline=reservation,
                        expected_statuses=frozenset({"running"}),
                        expected_fencing_epoch=state.fencing_epoch,
                        expected_lease_token_sha256=state.lease_token_sha256,
                        require_live_authority=True,
                        operation="mark_running",
                    )
                except NodeLeaseRejected:
                    return self._lease_loss_result(
                        reservation=reservation,
                        state=state,
                        token=token,
                        request=request,
                    )
                state = replace(
                    state,
                    phase=AttemptPhase.RUNNING,
                    running_confirmed=True,
                )
                self._state.save_state(state)
                return self._heartbeat_running(reservation=reservation, state=state, token=token)
            if observation.state is RuntimeInspectionState.TERMINATED:
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    output_root=output_root,
                )
            if observation.state is RuntimeInspectionState.ABSENT:
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    output_root=output_root,
                )
            return self._retain_reconciliation(
                reservation=reservation,
                state=state,
                token=token,
                observation=observation,
                reason="launch_outcome_unknown",
            )

    def _validate_assignment(
        self, assignment: QualificationAssignment
    ) -> tuple[
        ExecutionIntent,
        WorkOrderNode,
        EngineeringQualificationGrant,
        NodeReservation,
        PinnedLaunchSpec,
    ]:
        try:
            intent = ExecutionIntent.model_validate(assignment.intent.model_dump(mode="python"))
            node = WorkOrderNode.model_validate(
                assignment.work_order_node.model_dump(mode="python")
            )
            grant = EngineeringQualificationGrant.model_validate(
                assignment.qualification_grant.model_dump(mode="python")
            )
            reservation = assignment.reservation
            if not isinstance(reservation, NodeReservation):
                raise TypeError("reservation is not the closed node adapter projection")
        except (AttributeError, TypeError, ValueError) as exc:
            raise AssignmentRejected("assignment contract failed closed revalidation") from exc
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise AssignmentRejected("node clock must provide timezone-aware UTC")
        try:
            self._qualification_authority.verify_signature(grant, observed_at=now)
        except QualificationVerificationError as exc:
            raise AssignmentRejected("qualification grant is not active and pinned") from exc
        message = grant.message
        if (
            intent.effect_class is not ExecutionEffectClass.REPLAY_SAFE
            or intent.external_request is not None
            or intent.resource_request.network_policy is not NetworkPolicy.NONE
            or intent.resource_request.checkpoint_interval_seconds is not None
            or intent.retry_policy.mode
            not in {ExecutionRetryMode.NEVER, ExecutionRetryMode.IDEMPOTENT_NEW_ATTEMPT}
        ):
            raise AssignmentRejected(
                "PR-4a node accepts replay-safe, network-none, non-checkpoint work only"
            )
        attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
        if (
            reservation.execution_id != intent.execution_id
            or reservation.attempt_id != attempt_id
            or reservation.intent_sha256 != intent.intent_sha256
            or reservation.node_id != self._node_authority.manifest.node_id
            or reservation.grant_sha256 != grant.grant_sha256
            or reservation.fencing_epoch < 1
            or any(
                re.fullmatch(_SHA256_PATTERN, item) is None
                for item in (
                    reservation.admission_sha256,
                    reservation.grant_sha256,
                    reservation.node_inventory_sha256,
                    reservation.resource_lease_sha256,
                    reservation.lease_token_sha256,
                )
            )
            or reservation.lease_expires_at > reservation.hard_deadline
            or (
                reservation.status not in {"reconciliation_required", "terminated", "verifying"}
                and not now < reservation.lease_expires_at
            )
            or (
                reservation.status not in {"reconciliation_required", "terminated", "verifying"}
                and not now < reservation.hard_deadline
            )
            or reservation.status
            not in {
                "reserved",
                "starting",
                "running",
                "reconciliation_required",
                "terminated",
                "verifying",
            }
            or message.intent_sha256 != intent.intent_sha256
            or message.execution_id != intent.execution_id
            or message.infrastructure_attempt_id != attempt_id
        ):
            raise AssignmentRejected("assignment differs from exact allocator/grant authority")
        selected_resource_ids = reservation.selected_resource_ids
        devices = reservation.device_leases
        device_ids = tuple(item.device_id for item in devices)
        hardware_uuids = tuple(item.hardware_uuid for item in devices)
        if (
            not selected_resource_ids
            or selected_resource_ids != tuple(sorted(set(selected_resource_ids)))
            or any(_SAFE_LABEL.fullmatch(item) is None for item in selected_resource_ids)
            or devices != tuple(sorted(devices, key=lambda item: item.device_id))
            or len(set(device_ids)) != len(device_ids)
            or len(set(hardware_uuids)) != len(hardware_uuids)
            or not set(device_ids).issubset(set(selected_resource_ids))
            or reservation.cpu_cores != intent.resource_request.cpu_cores
            or reservation.memory_bytes != intent.resource_request.memory_bytes
            or reservation.scratch_bytes != intent.resource_request.scratch_bytes
            or reservation.exclusive != intent.resource_request.exclusive
            or type(reservation.cpu_cores) is not int
            or type(reservation.memory_bytes) is not int
            or type(reservation.scratch_bytes) is not int
            or reservation.cpu_cores < 1
            or reservation.memory_bytes < 1
            or reservation.scratch_bytes < 1
            or type(reservation.exclusive) is not bool
            or len(devices) != intent.resource_request.accelerator_count
            or any(
                item.fencing_epoch != reservation.fencing_epoch
                or item.state != "held"
                or not _SAFE_LABEL.fullmatch(item.device_id)
                or not _SAFE_LABEL.fullmatch(item.hardware_uuid)
                or item.requested_memory_bytes < 1
                or (
                    intent.resource_request.minimum_accelerator_memory_bytes is not None
                    and item.requested_memory_bytes
                    < intent.resource_request.minimum_accelerator_memory_bytes
                )
                for item in devices
            )
        ):
            raise AssignmentRejected("allocator placement projection is incomplete or divergent")
        if (
            node.node_id != intent.work_order_node_id
            or node.node_sha256 != intent.work_order_node_sha256
            or node.capability_id != intent.capability_id
            or node.capability_manifest_sha256 != intent.capability_manifest_sha256
            or node.command_sha256 != intent.command_sha256
            or node.environment_sha256 != intent.environment_sha256
            or node.execution_parameters_sha256 != intent.execution_parameters_sha256
            or node.resource_request != intent.resource_request
            or node.retry_policy != intent.retry_policy
            or node.expected_artifacts != intent.expected_artifacts
            or node.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        ):
            raise AssignmentRejected("WorkOrder node differs from exact execution intent")
        spec = self._registry.resolve(node)
        if spec is None:
            raise AssignmentRejected("WorkOrder command/environment/capability has no launch pin")
        if spec.runtime_engine != self._node_authority.manifest.container_runtime:
            raise AssignmentRejected("pinned runtime engine differs from enrolled node runtime")
        if tuple(item.artifact_key for item in spec.artifact_paths) != tuple(
            item.artifact_key for item in intent.expected_artifacts
        ):
            raise AssignmentRejected("pinned launch output map differs from declared artifacts")
        return intent, node, grant, reservation, spec

    def _validate_allocator_response(
        self,
        snapshot: NodeReservation,
        *,
        baseline: NodeReservation,
        expected_statuses: frozenset[str],
        expected_fencing_epoch: int,
        expected_lease_token_sha256: str,
        require_live_authority: bool,
        operation: str,
    ) -> NodeReservation:
        """Validate the complete adapter projection before any local/runtime side effect."""

        if not isinstance(snapshot, NodeReservation):
            raise RuntimeRejected(f"allocator {operation} response is not a NodeReservation")
        expected_devices = tuple(
            replace(item, fencing_epoch=expected_fencing_epoch) for item in baseline.device_leases
        )
        if (
            snapshot.execution_id != baseline.execution_id
            or snapshot.attempt_id != baseline.attempt_id
            or snapshot.intent_sha256 != baseline.intent_sha256
            or snapshot.admission_sha256 != baseline.admission_sha256
            or snapshot.grant_sha256 != baseline.grant_sha256
            or snapshot.node_id != baseline.node_id
            or snapshot.node_inventory_sha256 != baseline.node_inventory_sha256
            or snapshot.resource_lease_sha256 != baseline.resource_lease_sha256
            or snapshot.selected_resource_ids != baseline.selected_resource_ids
            or snapshot.cpu_cores != baseline.cpu_cores
            or snapshot.memory_bytes != baseline.memory_bytes
            or snapshot.scratch_bytes != baseline.scratch_bytes
            or snapshot.exclusive != baseline.exclusive
            or snapshot.device_leases != expected_devices
            or snapshot.fencing_epoch != expected_fencing_epoch
            or snapshot.lease_token_sha256 != expected_lease_token_sha256
            or snapshot.hard_deadline != baseline.hard_deadline
            or snapshot.lease_expires_at > snapshot.hard_deadline
        ):
            raise RuntimeRejected(
                f"allocator {operation} response changed exact attempt/resource authority"
            )
        now = self._clock.now()
        if snapshot.status not in expected_statuses:
            raise NodeLeaseRejected(
                f"allocator {operation} did not return an authorized target status"
            )
        if require_live_authority and not (
            now < snapshot.lease_expires_at
            and now < snapshot.hard_deadline
            and now < self._node_authority.active_until
        ):
            raise NodeLeaseRejected(f"allocator {operation} response is no longer live")
        return snapshot

    def _resolve_token(self, assignment: QualificationAssignment) -> str:
        reservation = assignment.reservation
        if assignment.lease_token is not None:
            if hashlib.sha256(assignment.lease_token.encode("utf-8")).hexdigest() != (
                reservation.lease_token_sha256
            ):
                raise AssignmentRejected("raw lease token differs from allocator snapshot")
            self._state.save_token(
                attempt_id=reservation.attempt_id,
                fencing_epoch=reservation.fencing_epoch,
                token=assignment.lease_token,
            )
        return self._state.load_token(
            attempt_id=reservation.attempt_id,
            fencing_epoch=reservation.fencing_epoch,
            expected_sha256=reservation.lease_token_sha256,
        )

    def _recover_pending_adoption(
        self,
        *,
        state: _AttemptState,
        reservation: NodeReservation,
        token: str,
        receipt: AttemptAdoptionReceipt,
        request: RuntimeLaunchRequest,
        lock_evidence: _LockEvidence,
    ) -> NodeRunResult:
        """Replay/refresh evidence or roll forward one journaled rotation and token."""

        if (
            not state.launch_committed
            or receipt.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or receipt.node_id != state.node_id
            or receipt.boot_id != state.boot_id
            or receipt.execution_id != state.execution_id
            or receipt.infrastructure_attempt_id != state.attempt_id
            or receipt.runtime_identity_sha256 != state.runtime_identity.runtime_identity_sha256
            or receipt.adoption_sequence != state.adoption_sequence + 1
            or receipt.previous_fencing_epoch != state.fencing_epoch
            or receipt.previous_lease_token_sha256 != state.lease_token_sha256
            or receipt.new_fencing_epoch != state.fencing_epoch + 1
        ):
            raise LocalStateError("pending adoption differs from exact local running lineage")
        try:
            self._node_authority.verify_signature(
                signing_key_id=receipt.signing_key_id,
                message=receipt.signature_message,
                signature_ed25519_hex=receipt.signature_ed25519_hex,
                signed_at=receipt.adopted_at,
            )
        except QualificationVerificationError as exc:
            raise LocalStateError("pending adoption node signature is invalid") from exc
        new_token = self._state.load_token(
            attempt_id=state.attempt_id,
            fencing_epoch=receipt.new_fencing_epoch,
            expected_sha256=receipt.new_lease_token_sha256,
        )
        snapshot = reservation
        if (
            reservation.fencing_epoch == receipt.previous_fencing_epoch
            and reservation.lease_token_sha256 == receipt.previous_lease_token_sha256
        ):
            if not self._inspection_receipt_is_fresh(receipt.runtime_inspection_receipt):
                if self._clock.now() >= reservation.hard_deadline:
                    return self._local_reconciliation(
                        state=state,
                        reason="pending_adoption_expired_at_hard_deadline",
                    )
                self._validate_recovered_state(
                    state,
                    request=request,
                    reservation=reservation,
                )
                self._validate_runtime_identity(
                    state.runtime_identity,
                    request=request,
                )
                observation = self._validated_inspection(
                    request=request,
                    state=state,
                )
                if observation.state is not RuntimeInspectionState.RUNNING:
                    return self._local_reconciliation(
                        state=state,
                        reason="expired_adoption_refresh_lacks_running_runtime",
                    )
                superseded_receipt_sha256 = receipt.adoption_receipt_sha256
                inspection, state = self._sign_inspection(
                    state=state,
                    observation=observation,
                    token_sha256=state.lease_token_sha256,
                )
                checked_monotonic = max(
                    self._clock.monotonic_ns(),
                    inspection.inspected_monotonic_ns,
                )
                revalidated_lock = lock_evidence.revalidate(
                    monotonic_ns=checked_monotonic,
                )
                receipt = issue_attempt_adoption_receipt(
                    manifest=self._node_authority.manifest,
                    runtime_inspection_receipt=inspection,
                    adoption_sequence=state.adoption_sequence + 1,
                    new_fencing_epoch=receipt.new_fencing_epoch,
                    new_lease_token_sha256=receipt.new_lease_token_sha256,
                    reason=receipt.reason,
                    singleton_lock_evidence_sha256=revalidated_lock.evidence_sha256,
                    singleton_lock_acquired_monotonic_ns=(revalidated_lock.checked_monotonic_ns),
                    allocator_principal_id=self._allocator_principal_id,
                    adopted_at=self._clock.now(),
                    private_key=self._private_key,
                )
                self._state.save_pending_adoption(
                    attempt_id=state.attempt_id,
                    receipt=receipt,
                    supersedes_adoption_receipt_sha256=(superseded_receipt_sha256),
                )
            try:
                snapshot = self._allocator.adopt_attempt(
                    receipt=receipt,
                    previous_lease_token=token,
                    previous_fencing_epoch=receipt.previous_fencing_epoch,
                    new_lease_token=new_token,
                )
            except NodeLeaseRejected:
                return self._local_reconciliation(
                    state=state,
                    reason="pending_adoption_replay_lost_allocator_authority",
                )
            return self._apply_adoption_snapshot(
                snapshot=snapshot,
                baseline=reservation,
                state=state,
                receipt=receipt,
                operation="pending_adoption_replay",
            )
        elif (
            reservation.fencing_epoch == receipt.new_fencing_epoch
            and reservation.lease_token_sha256 == receipt.new_lease_token_sha256
        ):
            return self._apply_adoption_snapshot(
                snapshot=reservation,
                baseline=reservation,
                state=state,
                receipt=receipt,
                operation="pending_adoption_roll_forward",
            )
        else:
            raise LocalStateError("allocator state is neither side of pending adoption")

    def _apply_adoption_snapshot(
        self,
        *,
        snapshot: NodeReservation,
        baseline: NodeReservation,
        state: _AttemptState,
        receipt: AttemptAdoptionReceipt,
        operation: str,
    ) -> NodeRunResult:
        if snapshot.status == "running":
            snapshot = self._validate_allocator_response(
                snapshot,
                baseline=baseline,
                expected_statuses=frozenset({"running"}),
                expected_fencing_epoch=receipt.new_fencing_epoch,
                expected_lease_token_sha256=receipt.new_lease_token_sha256,
                require_live_authority=True,
                operation=operation,
            )
            del snapshot
            phase = AttemptPhase.RUNNING
            running_confirmed = True
            outcome = NodeRunOutcome.ADOPTED
        elif snapshot.status == "reconciliation_required":
            snapshot = self._validate_allocator_response(
                snapshot,
                baseline=baseline,
                expected_statuses=frozenset({"reconciliation_required"}),
                expected_fencing_epoch=receipt.new_fencing_epoch,
                expected_lease_token_sha256=receipt.new_lease_token_sha256,
                require_live_authority=False,
                operation=operation,
            )
            del snapshot
            phase = AttemptPhase.RECONCILIATION_REQUIRED
            running_confirmed = state.running_confirmed
            outcome = NodeRunOutcome.RECONCILIATION_REQUIRED
        else:
            raise RuntimeRejected(
                f"allocator {operation} response has an unsupported adoption status"
            )
        state = replace(
            state,
            phase=phase,
            fencing_epoch=receipt.new_fencing_epoch,
            lease_token_sha256=receipt.new_lease_token_sha256,
            adoption_sequence=receipt.adoption_sequence,
            running_confirmed=running_confirmed,
        )
        self._state.save_state(state)
        return NodeRunResult(
            outcome=outcome,
            attempt_id=state.attempt_id,
            runtime_identity=state.runtime_identity,
            inspection_receipt=receipt.runtime_inspection_receipt,
            adoption_receipt=receipt,
        )

    def _launch_request(
        self,
        *,
        intent: ExecutionIntent,
        reservation: NodeReservation,
        spec: PinnedLaunchSpec,
        input_root: Path,
        output_root: Path,
    ) -> RuntimeLaunchRequest:
        runtime_digest = canonical_sha256(
            {
                "schema": "aletheia.qualification_runtime_locator.v1",
                "node_manifest_sha256": self._node_authority.manifest.manifest_sha256,
                "boot_id": self._boot_id,
                "execution_id": intent.execution_id,
                "attempt_id": reservation.attempt_id,
                "intent_sha256": intent.intent_sha256,
                "launch_spec_sha256": spec.launch_spec_sha256,
            }
        )
        runtime_id = f"qual-{runtime_digest}"
        labels = tuple(
            RuntimeLabel(name=name, value=value)
            for name, value in sorted(
                {
                    "aletheia.attempt_id": reservation.attempt_id,
                    "aletheia.execution_id": intent.execution_id,
                    "aletheia.intent_sha256": intent.intent_sha256,
                    "aletheia.launch_spec_sha256": spec.launch_spec_sha256,
                    "aletheia.node_id": reservation.node_id,
                    "aletheia.node_manifest_sha256": (
                        self._node_authority.manifest.manifest_sha256
                    ),
                    "aletheia.runtime_id": runtime_id,
                }.items()
            )
        )
        return RuntimeLaunchRequest(
            spec=spec,
            node_id=reservation.node_id,
            boot_id=self._boot_id,
            execution_id=intent.execution_id,
            attempt_id=reservation.attempt_id,
            intent_sha256=intent.intent_sha256,
            node_inventory_sha256=reservation.node_inventory_sha256,
            resource_lease_sha256=reservation.resource_lease_sha256,
            selected_resource_ids=reservation.selected_resource_ids,
            cpu_cores=reservation.cpu_cores,
            memory_bytes=reservation.memory_bytes,
            scratch_bytes=reservation.scratch_bytes,
            exclusive=reservation.exclusive,
            device_leases=reservation.device_leases,
            fencing_epoch=reservation.fencing_epoch,
            lease_token_sha256=reservation.lease_token_sha256,
            runtime_id=runtime_id,
            labels=labels,
            input_root=input_root,
            output_root=output_root,
            output_quota_bytes=intent.resource_request.artifact_quota_bytes,
            deadline=min(intent.deadline, reservation.hard_deadline),
        )

    def _validate_runtime_identity(
        self, identity: NodeRuntimeIdentity, *, request: RuntimeLaunchRequest
    ) -> None:
        try:
            identity = NodeRuntimeIdentity.model_validate(identity.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime identity failed closed validation") from exc
        if (
            identity.node_id != request.node_id
            or identity.boot_id != request.boot_id
            or identity.execution_id != request.execution_id
            or identity.infrastructure_attempt_id != request.attempt_id
            or identity.runtime_id != request.runtime_id
            or identity.runtime_engine != request.spec.runtime_engine
            or identity.launch_spec_sha256 != request.spec.launch_spec_sha256
        ):
            raise RuntimeRejected("runtime identity differs from deterministic launch request")

    def _validate_recovered_state(
        self,
        state: _AttemptState,
        *,
        request: RuntimeLaunchRequest,
        reservation: NodeReservation,
    ) -> None:
        if (
            state.attempt_id != request.attempt_id
            or state.execution_id != request.execution_id
            or state.intent_sha256 != request.intent_sha256
            or state.node_id != request.node_id
            or state.boot_id != request.boot_id
            or state.launch_spec_sha256 != request.spec.launch_spec_sha256
            or state.fencing_epoch != reservation.fencing_epoch
            or state.lease_token_sha256 != reservation.lease_token_sha256
        ):
            raise LocalStateError("recovered attempt state differs from pulled authority")

    def _materialize_and_seal_inputs(self, *, intent: ExecutionIntent, input_root: Path) -> str:
        if intent.input_artifact_bindings:
            if self._input_materializer is None:
                raise AssignmentRejected("verified inputs require a pinned input materializer")
            materialization_sha256 = self._input_materializer.ensure_verified_inputs(
                intent=intent, destination=input_root
            )
            if re.fullmatch(_SHA256_PATTERN, materialization_sha256) is None:
                raise LocalStateError("input materializer returned a non-SHA-256 identity")
        else:
            if any(input_root.iterdir()):
                raise LocalStateError("empty input authority has a nonempty staging directory")
            materialization_sha256 = canonical_sha256(
                {
                    "schema": "aletheia.empty_qualification_input_materialization.v1",
                    "intent_sha256": intent.intent_sha256,
                }
            )
        self._seal_read_only_tree(input_root)
        return materialization_sha256

    @classmethod
    def _seal_read_only_tree(cls, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise LocalStateError("input staging root is unsafe")
        for current_root, directory_names, file_names in os.walk(root, topdown=False):
            current = Path(current_root)
            for name in file_names:
                path = current / name
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise LocalStateError("input staging contains a link or non-regular file")
                os.chmod(path, 0o400, follow_symlinks=False)
            for name in directory_names:
                path = current / name
                metadata = path.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LocalStateError("input staging contains a symlink")
                os.chmod(path, 0o500, follow_symlinks=False)
        os.chmod(root, 0o500, follow_symlinks=False)

    def _validated_inspection(
        self, *, request: RuntimeLaunchRequest, state: _AttemptState
    ) -> RuntimeObservation:
        try:
            observation = RuntimeObservation.model_validate(
                self._runtime.inspect(request=request, identity=state.runtime_identity).model_dump(
                    mode="python"
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime inspection failed closed validation") from exc
        if observation.runtime_identity != state.runtime_identity:
            raise RuntimeRejected("runtime inspection changed the exact runtime identity")
        if (
            observation.enforced_placement_sha256 != request.enforced_placement_sha256
            or observation.enforced_fencing_epoch != request.fencing_epoch
        ):
            raise RuntimeRejected(
                "runtime inspection did not enforce the exact allocator placement/fence"
            )
        self._validate_observation_freshness(observation)
        return observation

    def _validate_observation_freshness(self, observation: RuntimeObservation) -> None:
        now = self._clock.now()
        checked_monotonic_ns = self._clock.monotonic_ns()
        age = now - observation.inspected_at
        monotonic_age_ns = checked_monotonic_ns - observation.inspected_monotonic_ns
        if (
            age < timedelta(0)
            or age > self._inspection_ttl
            or monotonic_age_ns < 0
            or monotonic_age_ns > int(self._inspection_ttl.total_seconds() * 1_000_000_000)
        ):
            raise RuntimeRejected("runtime observation is not contemporaneous with node clock")

    def _sign_inspection(
        self,
        *,
        state: _AttemptState,
        observation: RuntimeObservation,
        token_sha256: str,
    ) -> tuple[RuntimeInspectionReceipt, _AttemptState]:
        self._validate_observation_freshness(observation)
        now = self._clock.now()
        expires_at = min(
            observation.inspected_at + self._inspection_ttl,
            self._node_authority.active_until,
        )
        if expires_at <= now:
            raise RuntimeRejected("node enrollment cannot cover a fresh runtime inspection")
        receipt = issue_runtime_inspection_receipt(
            manifest=self._node_authority.manifest,
            runtime_identity=state.runtime_identity,
            fencing_epoch=state.fencing_epoch,
            lease_token_sha256=token_sha256,
            inspection_sequence=state.inspection_sequence + 1,
            state=observation.state,
            inspection_evidence_sha256=observation.inspection_evidence_sha256,
            inspected_at=observation.inspected_at,
            inspected_monotonic_ns=observation.inspected_monotonic_ns,
            expires_at=expires_at,
            private_key=self._private_key,
        )
        state = replace(state, inspection_sequence=state.inspection_sequence + 1)
        self._state.save_state(state)
        return receipt, state

    def _inspection_receipt_is_fresh(self, receipt: RuntimeInspectionReceipt) -> bool:
        now = self._clock.now()
        checked_monotonic_ns = self._clock.monotonic_ns()
        wall_age = now - receipt.inspected_at
        monotonic_age_ns = checked_monotonic_ns - receipt.inspected_monotonic_ns
        return (
            timedelta(0) <= wall_age <= self._inspection_ttl
            and 0 <= monotonic_age_ns <= int(self._inspection_ttl.total_seconds() * 1_000_000_000)
            and now < receipt.expires_at
            and receipt.expires_at <= self._node_authority.active_until
        )

    def _heartbeat_running(
        self, *, reservation: NodeReservation, state: _AttemptState, token: str
    ) -> NodeRunResult:
        if not state.running_confirmed:
            return self._local_reconciliation(
                state=state, reason="runtime_running_was_not_allocator_confirmed"
            )
        try:
            snapshot = self._allocator.heartbeat(
                attempt_id=reservation.attempt_id,
                lease_token=token,
                fencing_epoch=state.fencing_epoch,
            )
            snapshot = self._validate_allocator_response(
                snapshot,
                baseline=reservation,
                expected_statuses=frozenset({"starting", "running"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=True,
                operation="heartbeat",
            )
        except NodeLeaseRejected:
            request_input, request_output = self._state.workspace(reservation.attempt_id)
            # The exact request is reconstructed by the next pull; never launch a duplicate here.
            del request_input, request_output
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=reservation.attempt_id,
                runtime_identity=state.runtime_identity,
            )
        state = replace(state, phase=AttemptPhase.RUNNING)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.RUNNING,
            attempt_id=reservation.attempt_id,
            runtime_identity=state.runtime_identity,
        )

    def _lease_loss_result(
        self,
        *,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        request: RuntimeLaunchRequest,
    ) -> NodeRunResult:
        observation = self._validated_inspection(request=request, state=state)
        state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
        self._state.save_state(state)
        if observation.state is RuntimeInspectionState.UNKNOWN:
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=reservation.attempt_id,
                runtime_identity=state.runtime_identity,
            )
        # A stale allocator rejected the old authority; no further mutation is attempted with it.
        del token
        return NodeRunResult(
            outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
            attempt_id=reservation.attempt_id,
            runtime_identity=state.runtime_identity,
        )

    def _local_reconciliation(self, *, state: _AttemptState, reason: str) -> NodeRunResult:
        """Retain local evidence without minting a release-capable prelaunch receipt."""

        if not reason:
            raise ValueError("local reconciliation reason must be nonempty")
        state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
            attempt_id=state.attempt_id,
            runtime_identity=state.runtime_identity,
        )

    def _retain_reconciliation(
        self,
        *,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        observation: RuntimeObservation,
        reason: str,
    ) -> NodeRunResult:
        inspection, state = self._sign_inspection(
            state=state,
            observation=observation,
            token_sha256=state.lease_token_sha256,
        )
        try:
            snapshot = self._allocator.retain_reconciliation(
                attempt_id=reservation.attempt_id,
                lease_token=token,
                fencing_epoch=state.fencing_epoch,
                inspection_receipt=inspection,
                reason=reason,
            )
            self._validate_allocator_response(
                snapshot,
                baseline=reservation,
                expected_statuses=frozenset({"reconciliation_required"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=False,
                operation="retain_reconciliation",
            )
        except NodeLeaseRejected:
            pass
        state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
            attempt_id=reservation.attempt_id,
            runtime_identity=state.runtime_identity,
            inspection_receipt=inspection,
        )

    def _recover_reconciliation(
        self,
        *,
        intent: ExecutionIntent,
        reservation: NodeReservation,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
        token: str,
        observation: RuntimeObservation,
        lock_evidence: _LockEvidence,
    ) -> NodeRunResult:
        if observation.state is not RuntimeInspectionState.RUNNING:
            if observation.state is RuntimeInspectionState.TERMINATED and (state.launch_committed):
                return self._collect_stopped(
                    intent=intent,
                    spec=request.spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    observation=observation,
                    output_root=request.output_root,
                )
            if observation.state is RuntimeInspectionState.ABSENT:
                if state.launch_committed:
                    return self._collect_stopped(
                        intent=intent,
                        spec=request.spec,
                        reservation=reservation,
                        state=state,
                        token=token,
                        observation=observation,
                        output_root=request.output_root,
                    )
                return self._local_reconciliation(
                    state=state,
                    reason="recovery_absence_has_no_confirmed_running_lineage",
                )
            return self._retain_reconciliation(
                reservation=reservation,
                state=state,
                token=token,
                observation=observation,
                reason="recovery_runtime_unknown",
            )
        if not state.launch_committed:
            return self._local_reconciliation(
                state=state,
                reason="recovery_running_has_no_allocator_start_lineage",
            )
        if self._clock.now() >= reservation.hard_deadline:
            return self._local_reconciliation(
                state=state,
                reason="hard_deadline_forbids_running_runtime_adoption",
            )
        inspection, state = self._sign_inspection(
            state=state,
            observation=observation,
            token_sha256=state.lease_token_sha256,
        )
        checked_monotonic = max(self._clock.monotonic_ns(), inspection.inspected_monotonic_ns)
        revalidated_lock = lock_evidence.revalidate(
            monotonic_ns=checked_monotonic,
        )
        new_fence = state.fencing_epoch + 1
        new_token = self._state.load_existing_token(
            attempt_id=state.attempt_id,
            fencing_epoch=new_fence,
        )
        if new_token is None:
            new_token = secrets.token_urlsafe(32)
            self._state.save_token(
                attempt_id=state.attempt_id,
                fencing_epoch=new_fence,
                token=new_token,
            )
        new_token_sha256 = hashlib.sha256(new_token.encode("utf-8")).hexdigest()
        adopted_at = self._clock.now()
        adoption = issue_attempt_adoption_receipt(
            manifest=self._node_authority.manifest,
            runtime_inspection_receipt=inspection,
            adoption_sequence=state.adoption_sequence + 1,
            new_fencing_epoch=new_fence,
            new_lease_token_sha256=new_token_sha256,
            reason=AttemptAdoptionReason.NODE_AGENT_RECONNECT,
            singleton_lock_evidence_sha256=revalidated_lock.evidence_sha256,
            # The v1 field name is historical: the contract requires this to be the post-
            # inspection lock revalidation time.  The evidence hash also binds the truthful
            # acquisition evidence and its earlier monotonic timestamp.
            singleton_lock_acquired_monotonic_ns=(revalidated_lock.checked_monotonic_ns),
            allocator_principal_id=self._allocator_principal_id,
            adopted_at=adopted_at,
            private_key=self._private_key,
        )
        self._state.save_pending_adoption(
            attempt_id=state.attempt_id,
            receipt=adoption,
        )
        try:
            snapshot = self._allocator.adopt_attempt(
                receipt=adoption,
                previous_lease_token=token,
                previous_fencing_epoch=state.fencing_epoch,
                new_lease_token=new_token,
            )
        except NodeLeaseRejected:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_identity=state.runtime_identity,
                inspection_receipt=inspection,
                adoption_receipt=adoption,
            )
        return self._apply_adoption_snapshot(
            snapshot=snapshot,
            baseline=reservation,
            state=state,
            receipt=adoption,
            operation="adopt_attempt",
        )

    def _collect_stopped(
        self,
        *,
        intent: ExecutionIntent,
        spec: PinnedLaunchSpec,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        observation: RuntimeObservation,
        output_root: Path,
        persisted_inspection: RuntimeInspectionReceipt | None = None,
    ) -> NodeRunResult:
        if not state.launch_committed:
            return self._local_reconciliation(
                state=state,
                reason="stopped_runtime_has_no_durable_launch/running_lineage",
            )
        if not state.running_confirmed and observation.state is RuntimeInspectionState.TERMINATED:
            if reservation.status not in {
                "starting",
                "running",
                "reconciliation_required",
            }:
                return self._local_reconciliation(
                    state=state,
                    reason="terminal_runtime_was_not_allocator_start_bound",
                )
            if reservation.status in {"starting", "running"}:
                try:
                    if reservation.status == "starting":
                        snapshot = self._allocator.mark_running(
                            attempt_id=reservation.attempt_id,
                            lease_token=token,
                            fencing_epoch=state.fencing_epoch,
                        )
                        reservation = self._validate_allocator_response(
                            snapshot,
                            baseline=reservation,
                            expected_statuses=frozenset({"running"}),
                            expected_fencing_epoch=state.fencing_epoch,
                            expected_lease_token_sha256=state.lease_token_sha256,
                            require_live_authority=True,
                            operation="terminal_mark_running",
                        )
                    else:
                        reservation = self._validate_allocator_response(
                            reservation,
                            baseline=reservation,
                            expected_statuses=frozenset({"running"}),
                            expected_fencing_epoch=state.fencing_epoch,
                            expected_lease_token_sha256=state.lease_token_sha256,
                            require_live_authority=True,
                            operation="terminal_recovered_running",
                        )
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="terminal_runtime_running_transition_lost_authority",
                    )
                state = replace(
                    state,
                    phase=AttemptPhase.RUNNING,
                    running_confirmed=True,
                )
                self._state.save_state(state)
        if persisted_inspection is None:
            inspection, state = self._sign_inspection(
                state=state,
                observation=observation,
                token_sha256=state.lease_token_sha256,
            )
            self._state.save_pending_termination(
                attempt_id=state.attempt_id,
                observation=observation,
                receipt=inspection,
            )
        else:
            inspection = persisted_inspection
            if (
                not self._inspection_receipt_is_fresh(inspection)
                or inspection.inspection_sequence != state.inspection_sequence
                or inspection.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
                or inspection.runtime_identity != state.runtime_identity
                or inspection.fencing_epoch != state.fencing_epoch
                or inspection.lease_token_sha256 != state.lease_token_sha256
                or inspection.state != observation.state
                or inspection.inspection_evidence_sha256 != observation.inspection_evidence_sha256
                or inspection.inspected_at != observation.inspected_at
                or inspection.inspected_monotonic_ns != observation.inspected_monotonic_ns
            ):
                raise LocalStateError("persisted termination proof is stale or rebound")
        try:
            if reservation.status == "terminated":
                snapshot = reservation
            else:
                snapshot = self._allocator.mark_terminated(
                    attempt_id=reservation.attempt_id,
                    lease_token=token,
                    fencing_epoch=state.fencing_epoch,
                    inspection_receipt=inspection,
                )
            reservation = self._validate_allocator_response(
                snapshot,
                baseline=reservation,
                expected_statuses=frozenset({"terminated"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=False,
                operation="mark_terminated",
            )
        except NodeLeaseRejected:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_identity=state.runtime_identity,
                inspection_receipt=inspection,
            )
        state = replace(state, phase=AttemptPhase.TERMINATED)
        self._state.save_state(state)
        if observation.state is RuntimeInspectionState.TERMINATED:
            assert observation.exit_code is not None
            assert observation.ended_at is not None
            assert observation.ended_monotonic_ns is not None
            exit_code = observation.exit_code
            ended_at = observation.ended_at
            ended_monotonic_ns = observation.ended_monotonic_ns
        else:
            # ABSENT is a confirmed stopped runtime but has no trustworthy process exit status.
            # The qualification receipt uses the reserved infrastructure-failure sentinel.
            exit_code = 255
            ended_at = observation.inspected_at
            ended_monotonic_ns = observation.inspected_monotonic_ns
        declared = {item.artifact_key: item.relative_path for item in spec.artifact_paths}
        existing = {
            artifact_key: relative_path
            for artifact_key, relative_path in declared.items()
            if (output_root / relative_path).is_file()
        }
        missing_required = any(
            expected.required and expected.artifact_key not in existing
            for expected in intent.expected_artifacts
        )
        if exit_code != 0:
            disposition = NodeTerminalDisposition.PROCESS_FAILED
        elif ended_at > min(intent.deadline, reservation.hard_deadline):
            disposition = NodeTerminalDisposition.TIMEOUT
        elif missing_required:
            disposition = NodeTerminalDisposition.INVALID_OUTPUT
        else:
            disposition = NodeTerminalDisposition.PROCESS_SUCCEEDED
        allow_partial = disposition is not NodeTerminalDisposition.PROCESS_SUCCEEDED
        try:
            manifest = self._artifact_quarantine.quarantine_outputs(
                intent=intent,
                output_root=output_root,
                artifact_paths=existing,
                produced_at=ended_at,
                allow_partial=allow_partial,
            )
        except Exception as exc:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            raise OutputCollectionRejected(
                "stopped runtime output did not match the exact declared tree"
            ) from exc
        receipt = issue_node_execution_receipt(
            manifest=self._node_authority.manifest,
            intent=intent,
            node_inventory_sha256=reservation.node_inventory_sha256,
            resource_lease_sha256=reservation.resource_lease_sha256,
            runtime_identity=state.runtime_identity,
            fencing_epoch=state.fencing_epoch,
            lease_token_sha256=state.lease_token_sha256,
            ended_at=ended_at,
            ended_monotonic_ns=ended_monotonic_ns,
            exit_code=exit_code,
            artifact_manifest=manifest,
            termination_inspection_receipt=inspection,
            signed_at=self._clock.now(),
            private_key=self._private_key,
        )
        self._state.save_terminal_result(
            attempt_id=reservation.attempt_id,
            manifest=manifest,
            receipt=receipt,
            disposition=disposition,
        )
        try:
            snapshot = self._allocator.mark_verifying(
                attempt_id=reservation.attempt_id,
                lease_token=token,
                fencing_epoch=state.fencing_epoch,
                node_execution_receipt=receipt,
                disposition=disposition,
            )
            self._validate_allocator_response(
                snapshot,
                baseline=reservation,
                expected_statuses=frozenset({"verifying"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=False,
                operation="mark_verifying",
            )
        except NodeLeaseRejected:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_identity=state.runtime_identity,
                inspection_receipt=inspection,
                artifact_manifest=manifest,
                node_execution_receipt=receipt,
                terminal_disposition=disposition,
            )
        state = replace(state, phase=AttemptPhase.VERIFYING)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.COLLECTED,
            attempt_id=state.attempt_id,
            runtime_identity=state.runtime_identity,
            inspection_receipt=inspection,
            artifact_manifest=manifest,
            node_execution_receipt=receipt,
            terminal_disposition=disposition,
        )


__all__ = [
    "ArtifactQuarantinePort",
    "AssignmentRejected",
    "LocalStateError",
    "NodeAgentError",
    "NodeAllocatorPort",
    "NodeLeaseRejected",
    "NodeLocalStateStore",
    "NodeReservation",
    "NodeRunOutcome",
    "NodeRunResult",
    "NodeTerminalDisposition",
    "OutputCollectionRejected",
    "PinnedArtifactPath",
    "PinnedEnvironmentVariable",
    "PinnedLaunchRegistry",
    "PinnedLaunchSpec",
    "QualificationAssignment",
    "QualificationInputMaterializerPort",
    "QualificationNodeAgent",
    "QualificationRuntimePort",
    "ReservedDeviceBinding",
    "RuntimeLaunchRequest",
    "RuntimeObservation",
    "RuntimeRejected",
]
