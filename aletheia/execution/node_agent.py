"""Qualification-only, pull-based local execution node agent.

This module intentionally stops at the engineering boundary.  It can run only a deployment-
pinned, replay-safe :class:`WorkOrderNode` in a networkless sandbox and can emit node-signed
runtime/output evidence.  It cannot admit scientific evidence, invent a command, read execution
database tables, or turn a legacy queue message into execution authority.

This PR-4b.0 slice defines the qualification lifecycle boundary and fault harness, not Research
Kernel launch authority.  PostgreSQL allocator and hardened-local runtime adapters meet only the
narrow protocols below: the runtime receives a closed ``RuntimeLaunchRequest`` rather than
caller-controlled argv, and this module never imports allocator-private persistence records.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
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
from pydantic import Field, model_validator

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
    artifact_output_tree_sha256,
    issue_attempt_adoption_receipt,
    issue_node_execution_receipt,
    issue_runtime_inspection_receipt,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    HistoricalPreRuntimeRecoveryLineage,
    HistoricalRuntimeRecoveryGrant,
    InputMaterializationReceipt,
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    NodeRuntimeLaunchReceipt,
    NodeRuntimeTerminationReceipt,
    OutputQuotaProvisioningReceipt,
    PinnedOutputWorkspaceRoot,
    PinnedInputPath,
    PreRuntimeAbsenceReceipt,
    QualificationTerminalSubmission,
    RuntimeControlAuthorityVerifier,
    RuntimeFenceRebindEvidence,
    RuntimeFenceRebindReceipt,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimeLaunchEvidence,
    RuntimePreparation,
    RuntimeTerminationAcceptanceChallenge,
    issue_node_runtime_launch_receipt,
    issue_node_runtime_termination_receipt,
    issue_pre_runtime_absence_receipt,
    issue_qualification_terminal_submission,
    issue_runtime_fence_rebind_receipt,
    validate_pre_runtime_absence_evidence_refresh,
    validate_runtime_fence_rebind_evidence,
    validate_runtime_terminal_evidence_refresh,
    verify_accepted_runtime_termination,
    verify_accepted_qualification_terminal_submission,
    verify_historical_runtime_recovery_grant,
    verify_node_runtime_launch_receipt_historical,
    verify_node_runtime_termination_receipt_historical,
    verify_qualification_terminal_submission,
    verify_runtime_launch_authorization,
    verify_runtime_launch_authorization_ticket_historical,
    verify_runtime_termination_acceptance_challenge,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
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
        "input_materialization_receipt",
        "output_quota_provisioning_receipt",
        "runtime_preparation",
        "runtime_launch_authorization_request",
        "runtime_launch_authorization",
        "node_runtime_launch_receipt",
        "runtime_identity",
        "inspection_sequence",
        "adoption_sequence",
        "runtime_rebind_sequence",
        "runtime_control_journal_sha256",
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


class NodeProofReplayRejectionCode(str, Enum):
    """Only allocator proof outcomes which permit a new node observation generation."""

    TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED = "termination_challenge_expired_unaccepted"
    PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED = "pre_runtime_absence_stale_uncommitted"


class NodeProofReplayRejected(NodeAgentError):
    """Typed adapter signal; callers must never infer refresh authority from message text."""

    def __init__(self, code: NodeProofReplayRejectionCode, message: str) -> None:
        if not isinstance(code, NodeProofReplayRejectionCode):
            raise TypeError("node proof replay rejection code is not closed")
        super().__init__(message)
        self.code = code


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
    input_paths: tuple[PinnedInputPath, ...] = ()
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
        input_ports = tuple(item.input_port_id for item in self.input_paths)
        input_paths = tuple(item.relative_path for item in self.input_paths)
        if input_ports != tuple(sorted(set(input_ports))):
            raise ValueError("pinned input ports must be unique and canonical")
        if len(set(input_paths)) != len(input_paths):
            raise ValueError("pinned inputs cannot share one staged path")
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
class RuntimeStartAuthorization:
    """Atomic allocator projection of STARTING plus its exact signed launch ticket."""

    reservation: NodeReservation
    launch_authorization: RuntimeLaunchAuthorization
    replayed: bool = False


@dataclass(frozen=True)
class TerminalArtifactCommit:
    """Atomic allocator projection of VERIFYING plus signed final qualification acceptance."""

    reservation: NodeReservation
    terminal_acceptance: AcceptedQualificationTerminalSubmission


class PreRuntimeAbsenceDisposition(str, Enum):
    """Only two terminal DB decisions for a fresh never-started proof."""

    REAUTHORIZED = "reauthorized"
    RELEASED = "released"


@dataclass(frozen=True)
class PreRuntimeAbsenceDecision:
    """Atomic allocator result: replacement ticket or final resource release, never both."""

    reservation: NodeReservation
    disposition: PreRuntimeAbsenceDisposition
    pre_runtime_absence_receipt_sha256: str
    replacement_launch_authorization_request: RuntimeLaunchAuthorizationRequest | None = None
    replacement_launch_authorization: RuntimeLaunchAuthorization | None = None

    def __post_init__(self) -> None:
        request = self.replacement_launch_authorization_request
        authorization = self.replacement_launch_authorization
        if re.fullmatch(_SHA256_PATTERN, self.pre_runtime_absence_receipt_sha256) is None:
            raise ValueError("pre-runtime absence decision receipt identity is invalid")
        if self.disposition is PreRuntimeAbsenceDisposition.REAUTHORIZED:
            if (
                request is None
                or authorization is None
                or request.pre_runtime_absence_receipt_sha256
                != self.pre_runtime_absence_receipt_sha256
                or authorization.authorization_request_sha256 != request.request_sha256
            ):
                raise ValueError("reauthorized absence requires its exact replacement ticket")
        elif request is not None or authorization is not None:
            raise ValueError("released absence cannot retain replacement launch authority")


@dataclass(frozen=True)
class _PendingPreRuntimeAbsenceGeneration:
    generation: int
    receipt: PreRuntimeAbsenceReceipt
    replacement_request: RuntimeLaunchAuthorizationRequest | None
    supersedes_absence_receipt_sha256: str | None


@dataclass(frozen=True)
class QualificationAssignment:
    """Allocator-authenticated pull result for one already-admitted qualification attempt."""

    intent: ExecutionIntent
    work_order_node: WorkOrderNode
    qualification_grant: EngineeringQualificationGrant
    reservation: NodeReservation
    lease_token: str | None = None
    historical_recovery_grant: HistoricalRuntimeRecoveryGrant | None = None
    historical_pre_runtime_recovery_lineage: HistoricalPreRuntimeRecoveryLineage | None = None


@dataclass(frozen=True)
class RuntimeLabel:
    name: str
    value: str


@dataclass(frozen=True)
class RuntimeLaunchRequest:
    """Only value a runtime adapter may accept; it has no arbitrary command/mount escape hatch."""

    spec: PinnedLaunchSpec
    node_manifest_sha256: str
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
    input_materialization_receipt: InputMaterializationReceipt
    output_quota_provisioning_receipt: OutputQuotaProvisioningReceipt

    def __post_init__(self) -> None:
        try:
            receipt = OutputQuotaProvisioningReceipt.model_validate(
                self.output_quota_provisioning_receipt.model_dump(mode="python")
            )
            metadata = self.output_root.lstat()
            resolved = self.output_root.resolve(strict=True)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise ValueError("runtime output quota receipt or mount is invalid") from exc
        if (
            self.output_root.is_symlink()
            or resolved != self.output_root
            or receipt.node_manifest_sha256 != self.node_manifest_sha256
            or receipt.node_id != self.node_id
            or receipt.boot_id != self.boot_id
            or receipt.execution_id != self.execution_id
            or receipt.infrastructure_attempt_id != self.attempt_id
            or receipt.intent_sha256 != self.intent_sha256
            or receipt.output_root != str(self.output_root)
            or receipt.output_quota_bytes != self.output_quota_bytes
            or receipt.output_root_device != metadata.st_dev
            or receipt.output_root_inode != metadata.st_ino
            or receipt.output_root_owner_uid != metadata.st_uid
            or receipt.output_root_owner_gid != metadata.st_gid
            or receipt.output_root_mode != stat.S_IMODE(metadata.st_mode)
            or receipt.provisioned_at >= self.deadline
        ):
            raise ValueError("runtime request differs from exact post-mount output quota receipt")

    @property
    def enforced_placement_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.qualification_runtime_placement.v2",
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
                        "requested_memory_bytes": item.requested_memory_bytes,
                        "state": item.state,
                    }
                    for item in self.device_leases
                ),
                "input_materialization_receipt_sha256": (
                    self.input_materialization_receipt.materialization_receipt_sha256
                ),
                "output_quota_provisioning_receipt_sha256": (
                    self.output_quota_provisioning_receipt.provisioning_receipt_sha256
                ),
                "output_quota_bytes": self.output_quota_bytes,
                "deadline": self.deadline.isoformat(),
            }
        )

    @property
    def runtime_request_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": "aletheia.qualification_runtime_request.v2",
                "launch_spec_sha256": self.spec.launch_spec_sha256,
                "node_manifest_sha256": self.node_manifest_sha256,
                "node_id": self.node_id,
                "boot_id": self.boot_id,
                "execution_id": self.execution_id,
                "attempt_id": self.attempt_id,
                "intent_sha256": self.intent_sha256,
                "runtime_id": self.runtime_id,
                "enforced_placement_sha256": self.enforced_placement_sha256,
                "fencing_epoch": self.fencing_epoch,
                "lease_token_sha256": self.lease_token_sha256,
                "input_materialization_receipt_sha256": (
                    self.input_materialization_receipt.materialization_receipt_sha256
                ),
                "output_quota_provisioning_receipt_sha256": (
                    self.output_quota_provisioning_receipt.provisioning_receipt_sha256
                ),
                "input_root": str(self.input_root),
                "output_root": str(self.output_root),
            }
        )


RuntimeObservation = RuntimeInspectionEvidence


class QualificationRuntimePort(Protocol):
    """Non-production facade for a deterministic network-none qualification runtime.

    Methods never accept free-form argv or mounts.  A concrete adapter must remain disabled until
    it can independently enforce and attest every projected resource/device binding and provide a
    crash-idempotent runtime fence-rebind operation for adoption.  Satisfying this protocol grants
    qualification execution only; there is deliberately no PR-5 scientific-admission bridge.
    """

    # ``prepare`` may create only durable engine metadata.  It must not create or start the
    # sandbox/process; ``ensure_started`` is the sole launch operation after allocator authority.
    def prepare(self, *, request: RuntimeLaunchRequest) -> RuntimePreparation: ...

    def inspect(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity | None,
    ) -> RuntimeInspectionEvidence: ...

    def ensure_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> RuntimeLaunchEvidence: ...

    def recover_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None,
    ) -> RuntimeLaunchEvidence | None:
        """Recover only an already-started identity; never create or start engine work."""
        ...

    def cleanup_never_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        preparation: RuntimePreparation,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
    ) -> RuntimeInspectionEvidence:
        """Delete only a journal-proven CREATED/PID0 sandbox and advance absence epoch."""
        ...

    def rebind_fence(
        self,
        *,
        request: RuntimeFenceRebindRequest,
        preparation: RuntimePreparation,
        identity: NodeRuntimeIdentity,
    ) -> RuntimeFenceRebindEvidence: ...


class QualificationInputMaterializerPort(Protocol):
    """Copy only already-verified input objects into the attempt-scoped staging directory."""

    def ensure_verified_inputs(
        self, *, intent: ExecutionIntent, destination: Path
    ) -> InputMaterializationReceipt:
        """Idempotently materialize/reverify exact bytes and return typed exact evidence."""
        ...


class OutputQuotaProvisionerPort(Protocol):
    """Privileged deployment adapter for one attempt-scoped writable byte ceiling."""

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
        """Provision once, or exact-reverify the same durable mount on recovery."""

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

    def verify_manifest(
        self,
        *,
        intent: ExecutionIntent,
        manifest: ArtifactManifest,
    ) -> tuple[ArtifactVerifiedReceipt, ...]: ...


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
        runtime_preparation: RuntimePreparation,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> RuntimeStartAuthorization: ...

    def mark_running(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
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

    def resolve_pre_runtime_absence(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        absence_receipt: PreRuntimeAbsenceReceipt,
        replacement_launch_authorization_request: (RuntimeLaunchAuthorizationRequest | None),
    ) -> PreRuntimeAbsenceDecision: ...

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
        runtime_fence_rebind_request: RuntimeFenceRebindRequest,
        runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt,
    ) -> NodeReservation: ...

    def challenge_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
        termination_evidence: RuntimeInspectionEvidence,
        inspection_sequence: int,
        artifact_submission_deadline: datetime,
    ) -> RuntimeTerminationAcceptanceChallenge: ...

    def accept_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
    ) -> AcceptedRuntimeTermination: ...

    def replay_accepted_runtime_termination(
        self,
        *,
        recovery_grant: HistoricalRuntimeRecoveryGrant,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
        expected_accepted_runtime_termination_sha256: str,
    ) -> AcceptedRuntimeTermination:
        """Read/replay one existing acceptance; this operation must never create one."""
        ...

    def submit_terminal_artifacts(
        self,
        *,
        accepted_termination: AcceptedRuntimeTermination,
        terminal_submission: QualificationTerminalSubmission,
        artifact_manifest: ArtifactManifest,
        artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
        disposition: "NodeTerminalDisposition",
    ) -> TerminalArtifactCommit: ...


class NodeClock(Protocol):
    """UTC wall time plus a suspend-aware, boot-scoped monotonic clock."""

    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class SystemNodeClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        if hasattr(time, "CLOCK_BOOTTIME"):
            return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        return time.monotonic_ns()


class AttemptPhase(str, Enum):
    PREPARED = "prepared"
    START_REQUESTED = "start_requested"
    START_AUTHORIZED = "start_authorized"
    LAUNCH_COMMITTED = "launch_committed"
    RUNNING = "running"
    TERMINATED = "terminated"
    VERIFYING = "verifying"
    PRE_RUNTIME_RELEASED = "pre_runtime_released"
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
    input_materialization_receipt: InputMaterializationReceipt
    output_quota_provisioning_receipt: OutputQuotaProvisioningReceipt
    runtime_preparation: RuntimePreparation
    runtime_launch_authorization_request: RuntimeLaunchAuthorizationRequest | None
    runtime_launch_authorization: RuntimeLaunchAuthorization | None
    node_runtime_launch_receipt: NodeRuntimeLaunchReceipt | None
    runtime_identity: NodeRuntimeIdentity | None
    inspection_sequence: int = 0
    adoption_sequence: int = 0
    runtime_rebind_sequence: int = 0
    runtime_control_journal_sha256: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "schema_name": "aletheia.qualification_node_attempt_state",
            "schema_version": 2,
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
            "input_materialization_receipt": self.input_materialization_receipt.model_dump(
                mode="json"
            ),
            "output_quota_provisioning_receipt": (
                self.output_quota_provisioning_receipt.model_dump(mode="json")
            ),
            "runtime_preparation": self.runtime_preparation.model_dump(mode="json"),
            "runtime_launch_authorization_request": (
                self.runtime_launch_authorization_request.model_dump(mode="json")
                if self.runtime_launch_authorization_request is not None
                else "none"
            ),
            "runtime_launch_authorization": (
                self.runtime_launch_authorization.model_dump(mode="json")
                if self.runtime_launch_authorization is not None
                else "none"
            ),
            "node_runtime_launch_receipt": (
                self.node_runtime_launch_receipt.model_dump(mode="json")
                if self.node_runtime_launch_receipt is not None
                else "none"
            ),
            "runtime_identity": (
                self.runtime_identity.model_dump(mode="json")
                if self.runtime_identity is not None
                else "none"
            ),
            "inspection_sequence": self.inspection_sequence,
            "adoption_sequence": self.adoption_sequence,
            "runtime_rebind_sequence": self.runtime_rebind_sequence,
            "runtime_control_journal_sha256": (self.runtime_control_journal_sha256 or "none"),
        }

    @classmethod
    def parse(cls, payload: object) -> "_AttemptState":
        if not isinstance(payload, dict) or frozenset(payload) != _STATE_SCHEMA_KEYS:
            raise LocalStateError("node attempt state is not a closed schema")
        if (
            payload.get("schema_name") != "aletheia.qualification_node_attempt_state"
            or payload.get("schema_version") != 2
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
                input_materialization_receipt=InputMaterializationReceipt.model_validate(
                    payload["input_materialization_receipt"]
                ),
                output_quota_provisioning_receipt=(
                    OutputQuotaProvisioningReceipt.model_validate(
                        payload["output_quota_provisioning_receipt"]
                    )
                ),
                runtime_preparation=RuntimePreparation.model_validate(
                    payload["runtime_preparation"]
                ),
                runtime_launch_authorization_request=(
                    RuntimeLaunchAuthorizationRequest.model_validate(
                        payload["runtime_launch_authorization_request"]
                    )
                    if payload["runtime_launch_authorization_request"] != "none"
                    else None
                ),
                runtime_launch_authorization=(
                    RuntimeLaunchAuthorization.model_validate(
                        payload["runtime_launch_authorization"]
                    )
                    if payload["runtime_launch_authorization"] != "none"
                    else None
                ),
                node_runtime_launch_receipt=(
                    NodeRuntimeLaunchReceipt.model_validate(payload["node_runtime_launch_receipt"])
                    if payload["node_runtime_launch_receipt"] != "none"
                    else None
                ),
                runtime_identity=(
                    NodeRuntimeIdentity.model_validate(payload["runtime_identity"])
                    if payload["runtime_identity"] != "none"
                    else None
                ),
                inspection_sequence=int(payload["inspection_sequence"]),
                adoption_sequence=int(payload["adoption_sequence"]),
                runtime_rebind_sequence=int(payload["runtime_rebind_sequence"]),
                runtime_control_journal_sha256=(
                    str(payload["runtime_control_journal_sha256"])
                    if payload["runtime_control_journal_sha256"] != "none"
                    else None
                ),
            )
            launch_evidence = (
                state.node_runtime_launch_receipt.launch_evidence
                if state.node_runtime_launch_receipt is not None
                else None
            )
            preparation = state.runtime_preparation
            authorization_request = state.runtime_launch_authorization_request
            authorization = state.runtime_launch_authorization
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
                or state.runtime_rebind_sequence < 0
                or (
                    state.runtime_control_journal_sha256 is not None
                    and re.fullmatch(_SHA256_PATTERN, state.runtime_control_journal_sha256) is None
                )
                or (state.running_confirmed and not state.launch_committed)
                or (state.runtime_identity is None) != (state.node_runtime_launch_receipt is None)
                or (authorization is not None and authorization_request is None)
                or (state.launch_committed and authorization is None)
                or (
                    authorization is not None
                    and (
                        authorization_request is None
                        or authorization.authorization_request_sha256
                        != authorization_request.request_sha256
                        or authorization.runtime_preparation_sha256
                        != preparation.preparation_sha256
                    )
                )
                or (
                    launch_evidence is not None
                    and (
                        launch_evidence.runtime_identity != state.runtime_identity
                        or launch_evidence.preparation_sha256 != preparation.preparation_sha256
                        or launch_evidence.enforced_placement_sha256
                        != preparation.enforced_placement_sha256
                        or launch_evidence.input_materialization_receipt_sha256
                        != preparation.input_materialization_receipt_sha256
                        or launch_evidence.enforced_fencing_epoch != preparation.fencing_epoch
                        or launch_evidence.enforced_lease_token_sha256
                        != preparation.lease_token_sha256
                        or authorization is None
                        or launch_evidence.runtime_launch_authorization_sha256
                        != authorization.authorization_sha256
                        or launch_evidence.runtime_identity.node_id != preparation.node_id
                        or launch_evidence.runtime_identity.boot_id != preparation.boot_id
                        or launch_evidence.runtime_identity.execution_id != preparation.execution_id
                        or launch_evidence.runtime_identity.infrastructure_attempt_id
                        != preparation.infrastructure_attempt_id
                        or launch_evidence.runtime_identity.runtime_id != preparation.runtime_id
                        or launch_evidence.runtime_identity.runtime_engine
                        != preparation.runtime_engine
                        or launch_evidence.runtime_identity.launch_spec_sha256
                        != preparation.launch_spec_sha256
                        or launch_evidence.runtime_identity.started_at < preparation.prepared_at
                        or launch_evidence.runtime_identity.started_monotonic_ns
                        < preparation.prepared_monotonic_ns
                        or state.node_runtime_launch_receipt is None
                        or state.node_runtime_launch_receipt.node_manifest_sha256
                        != preparation.node_manifest_sha256
                    )
                )
                or preparation.infrastructure_attempt_id != state.attempt_id
                or preparation.execution_id != state.execution_id
                or preparation.intent_sha256 != state.intent_sha256
                or preparation.node_id != state.node_id
                or preparation.boot_id != state.boot_id
                or preparation.launch_spec_sha256 != state.launch_spec_sha256
                or preparation.input_materialization_receipt_sha256
                != state.input_materialization_receipt.materialization_receipt_sha256
                or preparation.output_quota_provisioning_receipt_sha256
                != state.output_quota_provisioning_receipt.provisioning_receipt_sha256
                or state.output_quota_provisioning_receipt.infrastructure_attempt_id
                != state.attempt_id
                or state.output_quota_provisioning_receipt.execution_id != state.execution_id
                or state.output_quota_provisioning_receipt.intent_sha256 != state.intent_sha256
                or state.output_quota_provisioning_receipt.node_id != state.node_id
                or state.output_quota_provisioning_receipt.boot_id != state.boot_id
                or state.input_materialization_receipt.infrastructure_attempt_id != state.attempt_id
                or state.input_materialization_receipt.execution_id != state.execution_id
                or state.input_materialization_receipt.intent_sha256 != state.intent_sha256
                or (
                    state.phase
                    in {
                        AttemptPhase.PREPARED,
                        AttemptPhase.START_REQUESTED,
                        AttemptPhase.START_AUTHORIZED,
                    }
                    and (
                        state.launch_committed
                        or state.running_confirmed
                        or state.runtime_identity is not None
                    )
                )
                or (
                    state.phase is AttemptPhase.PREPARED
                    and (authorization_request is not None or authorization is not None)
                )
                or (
                    state.phase is AttemptPhase.START_REQUESTED
                    and (authorization_request is None or authorization is not None)
                )
                or (
                    state.phase is AttemptPhase.START_AUTHORIZED
                    and (authorization_request is None or authorization is None)
                )
                or (
                    state.phase is AttemptPhase.LAUNCH_COMMITTED
                    and (not state.launch_committed or state.running_confirmed)
                )
                or (
                    state.phase is AttemptPhase.RUNNING
                    and (
                        not state.launch_committed
                        or not state.running_confirmed
                        or state.runtime_identity is None
                    )
                )
                or (
                    state.phase in {AttemptPhase.TERMINATED, AttemptPhase.VERIFYING}
                    and (not state.launch_committed or state.runtime_identity is None)
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

    def __init__(
        self,
        root: Path,
        *,
        output_workspace_root_pin: PinnedOutputWorkspaceRoot | None = None,
    ) -> None:
        candidate = Path(root)
        if candidate.is_symlink():
            raise LocalStateError("node state root cannot be a symlink")
        self.output_workspace_root_pin = (
            None
            if output_workspace_root_pin is None
            else PinnedOutputWorkspaceRoot.model_validate(
                output_workspace_root_pin.model_dump(mode="python")
            )
        )
        if self.output_workspace_root_pin is not None:
            self._verify_output_workspace_root(self.output_workspace_root_pin)
            self._reject_state_workspace_overlap(
                candidate,
                Path(self.output_workspace_root_pin.path),
            )
        try:
            # The production external-workspace profile requires its private state parent to be
            # deployment-provisioned, so only this one dentry needs a replayable parent fsync.
            # The no-pin profile remains the explicitly non-production/local fake and preserves
            # its convenient recursive setup behavior.
            candidate.mkdir(
                parents=self.output_workspace_root_pin is None,
                exist_ok=True,
                mode=0o700,
            )
        except OSError as exc:
            raise LocalStateError("node state parent must be pre-provisioned") from exc
        os.chmod(candidate, 0o700)
        self.root = candidate.resolve(strict=True)
        if self.output_workspace_root_pin is not None:
            self._reject_state_workspace_overlap(
                self.root,
                Path(self.output_workspace_root_pin.path),
            )
        parent_descriptor = os.open(
            self.root.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        root_descriptor = self._open_directory(self.root)
        try:
            private_names = [
                "absences",
                "adoptions",
                "attempts",
                "tokens",
                "locks",
                "preparations",
                "rebinds",
                "results",
                "terminations",
            ]
            if output_workspace_root_pin is None:
                private_names.append("workspaces")
            for name in private_names:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
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
                # Flush create-or-existing: an existing child may be the replay residue of a
                # crash after mkdir returned but before the parent dentry reached stable storage.
                os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        if self.output_workspace_root_pin is None:
            self.workspace_root = self.root / "workspaces"
        else:
            self.workspace_root = Path(self.output_workspace_root_pin.path)

    @staticmethod
    def _reject_state_workspace_overlap(state_root: Path, workspace_root: Path) -> None:
        try:
            canonical_state = state_root.resolve(strict=False)
            canonical_workspace = workspace_root.resolve(strict=True)
        except OSError as exc:
            raise LocalStateError(
                "node state/workspace roots cannot be canonically resolved"
            ) from exc
        overlaps = (
            canonical_state == canonical_workspace
            or canonical_state in canonical_workspace.parents
            or canonical_workspace in canonical_state.parents
        )
        if not overlaps and state_root.exists():
            try:
                overlaps = os.path.samefile(canonical_state, canonical_workspace)
            except OSError as exc:
                raise LocalStateError(
                    "node state/workspace inode relationship cannot be verified"
                ) from exc
        if overlaps:
            raise LocalStateError(
                "private node state and shared output workspace custody roots overlap"
            )

    @staticmethod
    def _workspace_parent_chain_sha256(path: Path) -> str:
        identities: list[dict[str, object]] = []
        for parent in reversed(path.parents):
            metadata = parent.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or parent.is_symlink()
                or metadata.st_mode & 0o022
            ):
                raise LocalStateError("output workspace parent custody is unsafe")
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

    @staticmethod
    def _trusted_sealed_workspace_owner_uid() -> int:
        """Production sealed-attempt owner (patchable in unprivileged phase tests)."""

        return 0

    @classmethod
    def _verify_output_workspace_root(cls, pin: PinnedOutputWorkspaceRoot) -> None:
        if sys.platform != "linux":
            raise LocalStateError("pinned output workspace roots are Linux-only")
        path = Path(pin.path)
        flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise LocalStateError("pinned output workspace root is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise LocalStateError("output workspace mount identity is unavailable") from exc
        mount_ids: list[int] = []
        for line in mountinfo.splitlines():
            fields = line.partition(" - ")[0].split()
            if len(fields) >= 5 and fields[4] == str(path):
                try:
                    mount_ids.append(int(fields[0]))
                except ValueError as exc:
                    raise LocalStateError("output workspace mount id is invalid") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != pin.owner_uid
            or metadata.st_gid != pin.owner_gid
            or pin.owner_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != pin.mode
            or metadata.st_dev != pin.device
            or metadata.st_ino != pin.inode
            or mount_ids != [pin.mount_id]
            or cls._workspace_parent_chain_sha256(path) != pin.parent_chain_sha256
        ):
            raise LocalStateError("output workspace root differs from deployment pin")

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

    def _list_names(self, directory: str) -> tuple[str, ...]:
        parent = self._child_directory(directory)
        try:
            names = os.listdir(parent)
        finally:
            os.close(parent)
        if any(not isinstance(name, str) for name in names):  # pragma: no cover - OS contract
            raise LocalStateError("node state directory returned a non-text entry")
        return tuple(sorted(names))

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

    def save_input_materialization(
        self, *, attempt_id: str, receipt: InputMaterializationReceipt
    ) -> None:
        try:
            receipt = InputMaterializationReceipt.model_validate(receipt.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise LocalStateError("input materialization receipt is invalid") from exc
        if receipt.infrastructure_attempt_id != attempt_id:
            raise LocalStateError("input materialization belongs to another attempt")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_input_materialization",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.input.json"
        existing = self._read("preparations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("input materialization changed across recovery")
            return
        self._atomic_write("preparations", name, payload)

    def load_input_materialization(self, *, attempt_id: str) -> InputMaterializationReceipt | None:
        payload = self._read("preparations", f"{self._key(attempt_id)}.input.json", optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "receipt",
            }:
                raise ValueError("input materialization journal is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_input_materialization"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("input materialization journal scope differs")
            receipt = InputMaterializationReceipt.model_validate(decoded["receipt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("input materialization journal failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_input_materialization",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("input materialization journal bytes are not canonical")
        if receipt.infrastructure_attempt_id != attempt_id:
            raise LocalStateError("input materialization journal belongs to another attempt")
        return receipt

    def save_output_quota_provisioning(
        self, *, attempt_id: str, receipt: OutputQuotaProvisioningReceipt
    ) -> None:
        try:
            receipt = OutputQuotaProvisioningReceipt.model_validate(
                receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LocalStateError("output quota provisioning receipt is invalid") from exc
        if receipt.infrastructure_attempt_id != attempt_id:
            raise LocalStateError("output quota provisioning belongs to another attempt")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_output_quota_provisioning",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.quota.json"
        existing = self._read("preparations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("output quota provisioning changed across recovery")
            return
        self._atomic_write("preparations", name, payload)

    def load_output_quota_provisioning(
        self, *, attempt_id: str
    ) -> OutputQuotaProvisioningReceipt | None:
        payload = self._read("preparations", f"{self._key(attempt_id)}.quota.json", optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "receipt",
            }:
                raise ValueError("output quota provisioning journal is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_output_quota_provisioning"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("output quota provisioning journal scope differs")
            receipt = OutputQuotaProvisioningReceipt.model_validate(decoded["receipt"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError(
                "output quota provisioning journal failed closed validation"
            ) from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_output_quota_provisioning",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("output quota provisioning journal bytes are not canonical")
        if receipt.infrastructure_attempt_id != attempt_id:
            raise LocalStateError("output quota provisioning journal belongs to another attempt")
        return receipt

    def save_pre_runtime_absence_request(
        self,
        *,
        attempt_id: str,
        receipt: PreRuntimeAbsenceReceipt,
        replacement_request: RuntimeLaunchAuthorizationRequest | None,
        generation: int = 1,
        supersedes_absence_receipt_sha256: str | None = None,
    ) -> None:
        if (
            generation < 1
            or (generation == 1) != (supersedes_absence_receipt_sha256 is None)
            or (
                supersedes_absence_receipt_sha256 is not None
                and re.fullmatch(_SHA256_PATTERN, supersedes_absence_receipt_sha256) is None
            )
            or receipt.preparation.infrastructure_attempt_id != attempt_id
            or (
                replacement_request is not None
                and (
                    replacement_request.infrastructure_attempt_id != attempt_id
                    or replacement_request.runtime_preparation_sha256 != receipt.preparation_sha256
                    or replacement_request.pre_runtime_absence_epoch
                    != receipt.absence_evidence.prelaunch_absence_epoch
                    or replacement_request.pre_runtime_absence_receipt_sha256
                    != receipt.absence_receipt_sha256
                    or replacement_request.requested_at < receipt.signed_at
                    or replacement_request.requested_monotonic_ns
                    < receipt.absence_evidence.inspected_monotonic_ns
                )
            )
        ):
            raise LocalStateError("pre-runtime absence request scope differs")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_pre_runtime_absence_request",
                "schema_version": 3,
                "attempt_id": attempt_id,
                "generation": generation,
                "absence_receipt": receipt.model_dump(mode="json"),
                "replacement_request": (
                    replacement_request.model_dump(mode="json")
                    if replacement_request is not None
                    else "none"
                ),
                "supersedes_absence_receipt_sha256": (supersedes_absence_receipt_sha256 or "none"),
            }
        )
        absence_epoch = receipt.absence_evidence.prelaunch_absence_epoch
        if absence_epoch is None:
            raise LocalStateError("pre-runtime absence request omitted its epoch")
        name = f"{self._key(attempt_id)}.epoch-{absence_epoch}.generation-{generation}.pending.json"
        existing = self._read("absences", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("pre-runtime absence generation was rebound")
            return
        latest = self.load_latest_pre_runtime_absence_generation(
            attempt_id=attempt_id,
            absence_epoch=absence_epoch,
        )
        if (latest is None and generation != 1) or (
            latest is not None
            and (
                generation != latest.generation + 1
                or supersedes_absence_receipt_sha256 != latest.receipt.absence_receipt_sha256
            )
        ):
            raise LocalStateError("pre-runtime absence generation is not append-only")
        self._atomic_write("absences", name, payload)

    def _load_pre_runtime_absence_generation(
        self, *, attempt_id: str, absence_epoch: int, generation: int
    ) -> _PendingPreRuntimeAbsenceGeneration | None:
        name = f"{self._key(attempt_id)}.epoch-{absence_epoch}.generation-{generation}.pending.json"
        payload = self._read("absences", name, optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "generation",
                "absence_receipt",
                "replacement_request",
                "supersedes_absence_receipt_sha256",
            }:
                raise ValueError("pre-runtime absence request is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_pre_runtime_absence_request"
                or decoded["schema_version"] != 3
                or decoded["attempt_id"] != attempt_id
                or decoded["generation"] != generation
            ):
                raise ValueError("pre-runtime absence request identity differs")
            receipt = PreRuntimeAbsenceReceipt.model_validate(decoded["absence_receipt"])
            request = (
                None
                if decoded["replacement_request"] == "none"
                else RuntimeLaunchAuthorizationRequest.model_validate(
                    decoded["replacement_request"]
                )
            )
            supersedes = (
                None
                if decoded["supersedes_absence_receipt_sha256"] == "none"
                else str(decoded["supersedes_absence_receipt_sha256"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("pre-runtime absence request failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_pre_runtime_absence_request",
                "schema_version": 3,
                "attempt_id": attempt_id,
                "generation": generation,
                "absence_receipt": receipt.model_dump(mode="json"),
                "replacement_request": (
                    request.model_dump(mode="json") if request is not None else "none"
                ),
                "supersedes_absence_receipt_sha256": supersedes or "none",
            }
        )
        if not secrets.compare_digest(payload, expected) or (
            generation < 1
            or (generation == 1) != (supersedes is None)
            or (supersedes is not None and re.fullmatch(_SHA256_PATTERN, supersedes) is None)
            or receipt.preparation.infrastructure_attempt_id != attempt_id
            or receipt.absence_evidence.prelaunch_absence_epoch != absence_epoch
            or (
                request is not None
                and (
                    request.infrastructure_attempt_id != attempt_id
                    or request.runtime_preparation_sha256 != receipt.preparation_sha256
                    or request.pre_runtime_absence_epoch
                    != receipt.absence_evidence.prelaunch_absence_epoch
                    or request.pre_runtime_absence_receipt_sha256 != receipt.absence_receipt_sha256
                    or request.requested_at < receipt.signed_at
                    or request.requested_monotonic_ns
                    < receipt.absence_evidence.inspected_monotonic_ns
                )
            )
        ):
            raise LocalStateError("pre-runtime absence request bytes or scope differ")
        return _PendingPreRuntimeAbsenceGeneration(
            generation=generation,
            receipt=receipt,
            replacement_request=request,
            supersedes_absence_receipt_sha256=supersedes,
        )

    def load_latest_pre_runtime_absence_generation(
        self, *, attempt_id: str, absence_epoch: int
    ) -> _PendingPreRuntimeAbsenceGeneration | None:
        if absence_epoch < 1:
            raise LocalStateError("pre-runtime absence epoch is invalid")
        prefix = f"{self._key(attempt_id)}.epoch-{absence_epoch}.generation-"
        suffix = ".pending.json"
        candidates: list[int] = []
        for name in self._list_names("absences"):
            if name.startswith(prefix) and name.endswith(suffix):
                encoded = name[len(prefix) : -len(suffix)]
                if not encoded.isascii() or not encoded.isdecimal() or encoded.startswith("0"):
                    raise LocalStateError("pre-runtime absence generation filename is invalid")
                candidates.append(int(encoded))
        if not candidates:
            return None
        if len(candidates) != len(set(candidates)):
            raise LocalStateError("pre-runtime absence generation is duplicated")
        latest_generation = max(candidates)
        latest = self._load_pre_runtime_absence_generation(
            attempt_id=attempt_id,
            absence_epoch=absence_epoch,
            generation=latest_generation,
        )
        assert latest is not None
        if set(candidates) != set(range(1, latest_generation + 1)):
            raise LocalStateError("pre-runtime absence generation history has a gap")
        previous: _PendingPreRuntimeAbsenceGeneration | None = None
        for generation in range(1, latest_generation + 1):
            current = self._load_pre_runtime_absence_generation(
                attempt_id=attempt_id,
                absence_epoch=absence_epoch,
                generation=generation,
            )
            assert current is not None
            if previous is not None and (
                current.supersedes_absence_receipt_sha256 != previous.receipt.absence_receipt_sha256
            ):
                raise LocalStateError("pre-runtime absence supersession chain differs")
            previous = current
        return latest

    def load_pre_runtime_absence_request(
        self, *, attempt_id: str, absence_epoch: int
    ) -> tuple[PreRuntimeAbsenceReceipt, RuntimeLaunchAuthorizationRequest | None] | None:
        latest = self.load_latest_pre_runtime_absence_generation(
            attempt_id=attempt_id,
            absence_epoch=absence_epoch,
        )
        if latest is None:
            return None
        return latest.receipt, latest.replacement_request

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

    def save_terminal_submission_result(
        self,
        *,
        attempt_id: str,
        accepted: AcceptedRuntimeTermination,
        manifest: ArtifactManifest,
        artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
        submission: QualificationTerminalSubmission,
        disposition: "NodeTerminalDisposition",
    ) -> None:
        if (
            accepted.attempt_id != attempt_id
            or submission.attempt_id != attempt_id
            or submission.accepted_runtime_termination_sha256
            != accepted.accepted_termination_sha256
            or submission.artifact_manifest_sha256 != manifest.manifest_sha256
            or submission.output_tree_sha256 != artifact_output_tree_sha256(manifest)
            or submission.artifact_verified_receipt_sha256s
            != tuple(sorted(item.verified_receipt_sha256 for item in artifact_verified_receipts))
            or submission.disposition != disposition.value
        ):
            raise LocalStateError("terminal submission differs from accepted termination/tree")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_terminal_submission_result",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "accepted_runtime_termination_sha256": (accepted.accepted_termination_sha256),
                "artifact_manifest": manifest.model_dump(mode="json"),
                "terminal_submission": submission.model_dump(mode="json"),
                "disposition": disposition.value,
            }
        )
        name = f"{self._key(attempt_id)}.{accepted.accepted_termination_sha256}.v2.json"
        existing = self._read("results", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("terminal submission result is rebound")
            return
        self._atomic_write("results", name, payload)

    def load_terminal_submission_result(
        self, *, attempt_id: str, accepted: AcceptedRuntimeTermination
    ) -> tuple[ArtifactManifest, QualificationTerminalSubmission, "NodeTerminalDisposition"] | None:
        name = f"{self._key(attempt_id)}.{accepted.accepted_termination_sha256}.v2.json"
        payload = self._read("results", name, optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "accepted_runtime_termination_sha256",
                "artifact_manifest",
                "terminal_submission",
                "disposition",
            }:
                raise ValueError("terminal submission result is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_terminal_submission_result"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
                or decoded["accepted_runtime_termination_sha256"]
                != accepted.accepted_termination_sha256
            ):
                raise ValueError("terminal submission result scope differs")
            manifest = ArtifactManifest.model_validate(decoded["artifact_manifest"])
            submission = QualificationTerminalSubmission.model_validate(
                decoded["terminal_submission"]
            )
            disposition = NodeTerminalDisposition(decoded["disposition"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("terminal submission result failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_terminal_submission_result",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "accepted_runtime_termination_sha256": (accepted.accepted_termination_sha256),
                "artifact_manifest": manifest.model_dump(mode="json"),
                "terminal_submission": submission.model_dump(mode="json"),
                "disposition": disposition.value,
            }
        )
        if not secrets.compare_digest(payload, expected) or (
            submission.attempt_id != attempt_id
            or submission.accepted_runtime_termination_sha256
            != accepted.accepted_termination_sha256
            or submission.artifact_manifest_sha256 != manifest.manifest_sha256
            or submission.output_tree_sha256 != artifact_output_tree_sha256(manifest)
            or submission.disposition != disposition.value
        ):
            raise LocalStateError("terminal submission result bytes or binding differs")
        return manifest, submission, disposition

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
            or observation.state is not RuntimeInspectionState.TERMINATED
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
            or observation.state is not RuntimeInspectionState.TERMINATED
            or observation.inspection_evidence_sha256 != receipt.inspection_evidence_sha256
            or observation.inspected_at != receipt.inspected_at
            or observation.inspected_monotonic_ns != receipt.inspected_monotonic_ns
        ):
            raise LocalStateError("pending termination exact binding differs")
        return observation, receipt

    def save_runtime_termination_proof(
        self,
        *,
        attempt_id: str,
        challenge: RuntimeTerminationAcceptanceChallenge,
        receipt: NodeRuntimeTerminationReceipt,
    ) -> None:
        if (
            challenge.attempt_id != attempt_id
            or receipt.challenge_sha256 != challenge.challenge_sha256
            or receipt.inspection_sequence != challenge.inspection_sequence
            or receipt.termination_evidence.runtime_identity is None
            or receipt.termination_evidence.runtime_identity.infrastructure_attempt_id != attempt_id
        ):
            raise LocalStateError("runtime termination proof differs from its DB challenge")
        persisted_evidence = self.load_runtime_termination_evidence(
            attempt_id=attempt_id,
            inspection_sequence=challenge.inspection_sequence,
        )
        if persisted_evidence is None or persisted_evidence[1] != receipt.termination_evidence:
            raise LocalStateError("runtime termination proof lost its durable evidence generation")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_runtime_termination_proof",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "challenge": challenge.model_dump(mode="json"),
                "node_runtime_termination_receipt": receipt.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.sequence-{challenge.inspection_sequence}.proof.json"
        existing = self._read("terminations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("runtime termination proof generation is already bound")
            return
        self._atomic_write("terminations", name, payload)

    def save_runtime_termination_evidence(
        self,
        *,
        attempt_id: str,
        inspection_sequence: int,
        evidence: RuntimeInspectionEvidence,
    ) -> None:
        if (
            inspection_sequence < 1
            or evidence.state is not RuntimeInspectionState.TERMINATED
            or evidence.runtime_identity is None
            or evidence.runtime_identity.infrastructure_attempt_id != attempt_id
        ):
            raise LocalStateError("pending terminal evidence is not an exact process exit")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_pending_terminal_evidence",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "inspection_sequence": inspection_sequence,
                "runtime_inspection_evidence": evidence.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.sequence-{inspection_sequence}.evidence.json"
        existing = self._read("terminations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("pending terminal evidence changed across DB replay")
            return
        latest_sequence = self._latest_runtime_termination_sequence(
            attempt_id=attempt_id,
            kind="evidence",
        )
        if latest_sequence is not None:
            if inspection_sequence != latest_sequence + 1:
                raise LocalStateError("pending terminal evidence generation is not append-only")
            previous = self.load_runtime_termination_evidence(
                attempt_id=attempt_id,
                inspection_sequence=latest_sequence,
            )
            assert previous is not None
            try:
                validate_runtime_terminal_evidence_refresh(
                    previous=previous[1],
                    refreshed=evidence,
                )
            except QualificationVerificationError as exc:
                raise LocalStateError(
                    "pending terminal evidence refresh changed immutable engine facts"
                ) from exc
        self._atomic_write("terminations", name, payload)

    def _latest_runtime_termination_sequence(
        self, *, attempt_id: str, kind: Literal["evidence", "proof"]
    ) -> int | None:
        prefix = f"{self._key(attempt_id)}.sequence-"
        suffix = f".{kind}.json"
        candidates: list[int] = []
        for name in self._list_names("terminations"):
            if name.startswith(prefix) and name.endswith(suffix):
                encoded = name[len(prefix) : -len(suffix)]
                if not encoded.isascii() or not encoded.isdecimal() or encoded.startswith("0"):
                    raise LocalStateError("runtime termination generation filename is invalid")
                candidates.append(int(encoded))
        if len(candidates) != len(set(candidates)):
            raise LocalStateError("runtime termination generation is duplicated")
        return max(candidates) if candidates else None

    def load_runtime_termination_evidence(
        self, *, attempt_id: str, inspection_sequence: int | None = None
    ) -> tuple[int, RuntimeInspectionEvidence] | None:
        if inspection_sequence is None:
            inspection_sequence = self._latest_runtime_termination_sequence(
                attempt_id=attempt_id,
                kind="evidence",
            )
            if inspection_sequence is None:
                return None
        if inspection_sequence < 1:
            return None
        payload = self._read(
            "terminations",
            f"{self._key(attempt_id)}.sequence-{inspection_sequence}.evidence.json",
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
                "inspection_sequence",
                "runtime_inspection_evidence",
            }:
                raise ValueError("pending terminal evidence is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_pending_terminal_evidence"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("pending terminal evidence scope differs")
            evidence = RuntimeInspectionEvidence.model_validate(
                decoded["runtime_inspection_evidence"]
            )
            decoded_inspection_sequence = int(decoded["inspection_sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("pending terminal evidence failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_pending_terminal_evidence",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "inspection_sequence": decoded_inspection_sequence,
                "runtime_inspection_evidence": evidence.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected) or (
            decoded_inspection_sequence != inspection_sequence
            or inspection_sequence < 1
            or evidence.state is not RuntimeInspectionState.TERMINATED
            or evidence.runtime_identity is None
            or evidence.runtime_identity.infrastructure_attempt_id != attempt_id
        ):
            raise LocalStateError("pending terminal evidence bytes or identity differs")
        return inspection_sequence, evidence

    def load_runtime_termination_proof(
        self, *, attempt_id: str, inspection_sequence: int | None = None
    ) -> tuple[RuntimeTerminationAcceptanceChallenge, NodeRuntimeTerminationReceipt] | None:
        if inspection_sequence is None:
            inspection_sequence = self._latest_runtime_termination_sequence(
                attempt_id=attempt_id,
                kind="proof",
            )
            if inspection_sequence is None:
                return None
        if inspection_sequence < 1:
            return None
        payload = self._read(
            "terminations",
            f"{self._key(attempt_id)}.sequence-{inspection_sequence}.proof.json",
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
                "challenge",
                "node_runtime_termination_receipt",
            }:
                raise ValueError("runtime termination proof is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_runtime_termination_proof"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("runtime termination proof scope differs")
            challenge = RuntimeTerminationAcceptanceChallenge.model_validate(decoded["challenge"])
            receipt = NodeRuntimeTerminationReceipt.model_validate(
                decoded["node_runtime_termination_receipt"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("runtime termination proof failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_runtime_termination_proof",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "challenge": challenge.model_dump(mode="json"),
                "node_runtime_termination_receipt": receipt.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected) or (
            challenge.attempt_id != attempt_id
            or receipt.challenge_sha256 != challenge.challenge_sha256
            or receipt.inspection_sequence != challenge.inspection_sequence
            or challenge.inspection_sequence != inspection_sequence
        ):
            raise LocalStateError("runtime termination proof bytes or binding differs")
        return challenge, receipt

    def save_accepted_runtime_termination(
        self,
        *,
        attempt_id: str,
        challenge: RuntimeTerminationAcceptanceChallenge,
        receipt: NodeRuntimeTerminationReceipt,
        accepted: AcceptedRuntimeTermination,
    ) -> None:
        if (
            accepted.attempt_id != attempt_id
            or accepted.challenge_sha256 != challenge.challenge_sha256
            or accepted.node_runtime_termination_receipt_sha256
            != receipt.termination_receipt_sha256
            or accepted.inspection_sequence != receipt.inspection_sequence
        ):
            raise LocalStateError("accepted termination differs from its exact proof")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_accepted_runtime_termination",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "challenge": challenge.model_dump(mode="json"),
                "node_runtime_termination_receipt": receipt.model_dump(mode="json"),
                "accepted_runtime_termination": accepted.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.accepted.json"
        existing = self._read("terminations", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("accepted termination is already bound to other bytes")
            return
        self._atomic_write("terminations", name, payload)

    def load_accepted_runtime_termination(
        self, *, attempt_id: str
    ) -> (
        tuple[
            RuntimeTerminationAcceptanceChallenge,
            NodeRuntimeTerminationReceipt,
            AcceptedRuntimeTermination,
        ]
        | None
    ):
        payload = self._read(
            "terminations", f"{self._key(attempt_id)}.accepted.json", optional=True
        )
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "challenge",
                "node_runtime_termination_receipt",
                "accepted_runtime_termination",
            }:
                raise ValueError("accepted termination is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_accepted_runtime_termination"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("accepted termination scope differs")
            challenge = RuntimeTerminationAcceptanceChallenge.model_validate(decoded["challenge"])
            receipt = NodeRuntimeTerminationReceipt.model_validate(
                decoded["node_runtime_termination_receipt"]
            )
            accepted = AcceptedRuntimeTermination.model_validate(
                decoded["accepted_runtime_termination"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LocalStateError("accepted termination failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_accepted_runtime_termination",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "challenge": challenge.model_dump(mode="json"),
                "node_runtime_termination_receipt": receipt.model_dump(mode="json"),
                "accepted_runtime_termination": accepted.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected) or (
            accepted.attempt_id != attempt_id
            or accepted.challenge_sha256 != challenge.challenge_sha256
            or accepted.node_runtime_termination_receipt_sha256
            != receipt.termination_receipt_sha256
            or accepted.inspection_sequence != receipt.inspection_sequence
        ):
            raise LocalStateError("accepted termination bytes or proof binding differs")
        return challenge, receipt, accepted

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

    def save_runtime_rebind(
        self,
        *,
        attempt_id: str,
        request: RuntimeFenceRebindRequest,
        evidence: RuntimeFenceRebindEvidence,
        receipt: RuntimeFenceRebindReceipt,
    ) -> None:
        try:
            validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
        except QualificationVerificationError as exc:
            raise LocalStateError("runtime rebind evidence differs from request") from exc
        if (
            receipt.evidence != evidence
            or receipt.evidence_sha256 != evidence.evidence_sha256
            or request.rebind_sequence < 1
        ):
            raise LocalStateError("runtime rebind receipt differs from exact evidence")
        payload = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_runtime_rebind",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "request": request.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        name = f"{self._key(attempt_id)}.{request.rebind_sequence}.json"
        existing = self._read("rebinds", name, optional=True)
        if existing is not None:
            if not secrets.compare_digest(existing, payload):
                raise LocalStateError("runtime rebind journal changed after durable CAS")
            return
        self._atomic_write("rebinds", name, payload)

    def load_runtime_rebind(
        self, *, attempt_id: str, sequence: int
    ) -> (
        tuple[
            RuntimeFenceRebindRequest,
            RuntimeFenceRebindEvidence,
            RuntimeFenceRebindReceipt,
        ]
        | None
    ):
        payload = self._read("rebinds", f"{self._key(attempt_id)}.{sequence}.json", optional=True)
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, dict) or frozenset(decoded) != {
                "schema_name",
                "schema_version",
                "attempt_id",
                "request",
                "evidence",
                "receipt",
            }:
                raise ValueError("runtime rebind journal is not closed")
            if (
                decoded["schema_name"] != "aletheia.qualification_node_runtime_rebind"
                or decoded["schema_version"] != 2
                or decoded["attempt_id"] != attempt_id
            ):
                raise ValueError("runtime rebind journal scope differs")
            request = RuntimeFenceRebindRequest.model_validate(decoded["request"])
            evidence = RuntimeFenceRebindEvidence.model_validate(decoded["evidence"])
            receipt = RuntimeFenceRebindReceipt.model_validate(decoded["receipt"])
            validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
        except (KeyError, QualificationVerificationError, TypeError, ValueError) as exc:
            raise LocalStateError("runtime rebind journal failed closed validation") from exc
        expected = canonical_json_bytes(
            {
                "schema_name": "aletheia.qualification_node_runtime_rebind",
                "schema_version": 2,
                "attempt_id": attempt_id,
                "request": request.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
            }
        )
        if not secrets.compare_digest(payload, expected):
            raise LocalStateError("runtime rebind journal bytes are not canonical")
        if (
            request.rebind_sequence != sequence
            or receipt.evidence != evidence
            or receipt.evidence_sha256 != evidence.evidence_sha256
        ):
            raise LocalStateError("runtime rebind journal identity differs from its slot")
        return request, evidence, receipt

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
        workspace_root = self.workspace_root
        key = self._key(attempt_id)
        attempt_root = workspace_root / key
        if attempt_root.is_symlink():
            raise LocalStateError("attempt workspace cannot be a symlink")
        attempt_root.mkdir(mode=0o700, exist_ok=True)
        attempt_metadata = attempt_root.lstat()
        sealed = False
        if (
            attempt_metadata.st_uid == os.geteuid()
            and attempt_metadata.st_gid == os.getegid()
            and stat.S_IMODE(attempt_metadata.st_mode) == 0o700
        ):
            os.chmod(attempt_root, 0o700)
        elif (
            self.output_workspace_root_pin is not None
            and attempt_metadata.st_uid == self._trusted_sealed_workspace_owner_uid()
            and attempt_metadata.st_gid == self.output_workspace_root_pin.owner_gid
            and stat.S_IMODE(attempt_metadata.st_mode) == 0o710
        ):
            sealed = True
        elif (
            self.output_workspace_root_pin is not None
            and attempt_metadata.st_uid == self._trusted_sealed_workspace_owner_uid()
            and attempt_metadata.st_gid == self.output_workspace_root_pin.owner_gid
            and stat.S_IMODE(attempt_metadata.st_mode) == 0o700
        ):
            # The independently pinned root service may have durably transferred
            # ownership and then crashed before changing 0700 to the final 0710.
            # The node cannot safely traverse or mutate that phase.  Return only
            # the deterministic child pathnames so the provisioner can reopen the
            # fixed components under its pinned root and repair the same inode.
            return attempt_root / "input", attempt_root / "output"
        else:
            raise LocalStateError("attempt workspace is neither new-node nor root-sealed custody")
        input_root = attempt_root / "input"
        output_root = attempt_root / "output"
        for path in (input_root, output_root):
            if path.is_symlink():
                raise LocalStateError("attempt workspace child cannot be a symlink")
            if sealed:
                if not path.exists():
                    raise LocalStateError("sealed attempt workspace is missing a child")
            else:
                path.mkdir(mode=0o700, exist_ok=True)
            if not path.is_dir():
                raise LocalStateError("attempt workspace child is not a directory")
        input_metadata = input_root.lstat()
        if (
            input_metadata.st_uid != os.geteuid()
            or input_metadata.st_gid != os.getegid()
            or stat.S_IMODE(input_metadata.st_mode) not in (0o500, 0o700)
        ):
            raise LocalStateError("attempt input root custody is unsafe")
        output_metadata = output_root.lstat()
        if not sealed and (
            output_metadata.st_uid != os.geteuid()
            or output_metadata.st_gid != os.getegid()
            or stat.S_IMODE(output_metadata.st_mode) != 0o700
        ):
            raise LocalStateError("attempt output root custody is unsafe")
        if not sealed:
            os.chmod(output_root, 0o700)
        return input_root, output_root


class NodeRunOutcome(str, Enum):
    IDLE = "idle"
    LOCKED_BY_PEER = "locked_by_peer"
    RUNNING = "running"
    ADOPTED = "adopted"
    COLLECTED = "collected"
    PRE_RUNTIME_REAUTHORIZED = "pre_runtime_reauthorized"
    PRE_RUNTIME_RELEASED = "pre_runtime_released"
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
    runtime_preparation: RuntimePreparation | None = None
    runtime_identity: NodeRuntimeIdentity | None = None
    node_runtime_launch_receipt: NodeRuntimeLaunchReceipt | None = None
    pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None = None
    inspection_receipt: RuntimeInspectionReceipt | None = None
    adoption_receipt: AttemptAdoptionReceipt | None = None
    runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt | None = None
    runtime_termination_challenge: RuntimeTerminationAcceptanceChallenge | None = None
    node_runtime_termination_receipt: NodeRuntimeTerminationReceipt | None = None
    accepted_runtime_termination: AcceptedRuntimeTermination | None = None
    accepted_terminal_submission: AcceptedQualificationTerminalSubmission | None = None
    artifact_manifest: ArtifactManifest | None = None
    artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...] = ()
    terminal_submission: QualificationTerminalSubmission | None = None
    node_execution_receipt: NodeExecutionReceipt | None = None
    terminal_disposition: NodeTerminalDisposition | None = None


class QualificationNodeAgent:
    """One enrolled local worker restricted to signed engineering qualifications."""

    def __init__(
        self,
        *,
        node_authority: WorkerNodeAuthorityVerifier,
        qualification_authority: QualificationAuthorityVerifier,
        runtime_control_authority: RuntimeControlAuthorityVerifier,
        node_signing_private_key: bytes,
        boot_id: str,
        allocator_principal_id: str,
        allocator: NodeAllocatorPort,
        runtime: QualificationRuntimePort,
        output_quota_provisioner: OutputQuotaProvisionerPort,
        artifact_quarantine: ArtifactQuarantinePort,
        launch_registry: PinnedLaunchRegistry,
        state_store: NodeLocalStateStore,
        input_materializer: QualificationInputMaterializerPort | None = None,
        clock: NodeClock | None = None,
        inspection_ttl_seconds: int = 10,
        artifact_completion_grace_seconds: int = 3600,
    ) -> None:
        if not _SAFE_LABEL.fullmatch(boot_id):
            raise ValueError("node boot id is not canonical")
        if not _SAFE_LABEL.fullmatch(allocator_principal_id):
            raise ValueError("allocator principal id is not canonical")
        if inspection_ttl_seconds < 1 or inspection_ttl_seconds > 60:
            raise ValueError("runtime inspection TTL must be inside 1..60 seconds")
        if artifact_completion_grace_seconds < 1 or artifact_completion_grace_seconds > 86_400:
            raise ValueError("artifact completion grace must be inside 1..86400 seconds")
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
        provisioner_workspace_pin = getattr(
            output_quota_provisioner,
            "output_workspace_root_pin",
            None,
        )
        if provisioner_workspace_pin is not None:
            try:
                validated_workspace_pin = PinnedOutputWorkspaceRoot.model_validate(
                    provisioner_workspace_pin
                )
            except ValueError as exc:
                raise ValueError("quota provisioner workspace pin is invalid") from exc
            if state_store.output_workspace_root_pin != validated_workspace_pin:
                raise ValueError("quota provisioner and node state use different workspace roots")
        provisioner_minimum = getattr(
            output_quota_provisioner,
            "minimum_output_quota_bytes",
            None,
        )
        if provisioner_minimum is not None and (
            isinstance(provisioner_minimum, bool)
            or provisioner_minimum != MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
        ):
            raise ValueError("quota provisioner filesystem minimum differs from v2 contract")
        self._node_authority = node_authority
        self._qualification_authority = qualification_authority
        self._runtime_control_authority = runtime_control_authority
        self._private_key = bytes(node_signing_private_key)
        self._boot_id = boot_id
        self._allocator_principal_id = allocator_principal_id
        self._allocator = allocator
        self._runtime = runtime
        self._output_quota_provisioner = output_quota_provisioner
        self._minimum_output_quota_bytes = provisioner_minimum
        self._artifact_quarantine = artifact_quarantine
        self._registry = launch_registry
        self._state = state_store
        self._input_materializer = input_materializer
        self._clock = clock or SystemNodeClock()
        self._inspection_ttl = timedelta(seconds=inspection_ttl_seconds)
        self._artifact_completion_grace = timedelta(seconds=artifact_completion_grace_seconds)

    def run_once(self) -> NodeRunResult:
        assignment = self._allocator.pull_qualification_assignment(
            node_id=self._node_authority.manifest.node_id,
            node_manifest_sha256=self._node_authority.manifest.manifest_sha256,
        )
        if assignment is None:
            return NodeRunResult(outcome=NodeRunOutcome.IDLE)
        return self.run_assignment(assignment)

    def run_assignment(self, assignment: QualificationAssignment) -> NodeRunResult:
        (
            intent,
            node,
            grant,
            reservation,
            spec,
            historical_pre_runtime_lineage,
            launch_allowed,
        ) = self._validate_assignment(assignment)
        del node
        if (
            self._minimum_output_quota_bytes is not None
            and intent.resource_request.artifact_quota_bytes < self._minimum_output_quota_bytes
        ):
            raise AssignmentRejected("artifact quota is below the pinned loop filesystem floor")
        attempt_id = reservation.attempt_id
        with self._state.attempt_lock(
            attempt_id=attempt_id, monotonic_ns=self._clock.monotonic_ns()
        ) as initial_lock:
            if initial_lock is None:
                return NodeRunResult(outcome=NodeRunOutcome.LOCKED_BY_PEER, attempt_id=attempt_id)
            state = self._state.load_state(attempt_id)
            if state is None and not launch_allowed:
                raise AssignmentRejected(
                    "historical recovery authority cannot prepare or launch a new runtime"
                )
            token = self._resolve_token(assignment)
            input_root, output_root = self._state.workspace(attempt_id)
            stored_quota = self._state.load_output_quota_provisioning(attempt_id=attempt_id)
            if state is not None and stored_quota != state.output_quota_provisioning_receipt:
                raise LocalStateError(
                    "durable output quota receipt differs from attempt state lineage"
                )
            quota_receipt = self._ensure_output_quota(
                intent=intent,
                reservation=reservation,
                output_root=output_root,
                expected=stored_quota,
            )
            if stored_quota is None:
                self._state.save_output_quota_provisioning(
                    attempt_id=attempt_id,
                    receipt=quota_receipt,
                )
            elif quota_receipt != stored_quota:
                raise LocalStateError(
                    "fresh output quota verification differs from durable receipt"
                )
            if state is None:
                self._state.save_preparation_scope(
                    attempt_id=attempt_id,
                    intent_sha256=intent.intent_sha256,
                    launch_spec_sha256=spec.launch_spec_sha256,
                )
                materialization = self._state.load_input_materialization(attempt_id=attempt_id)
                if materialization is None:
                    materialization = self._materialize_and_seal_inputs(
                        intent=intent,
                        input_root=input_root,
                        spec=spec,
                    )
                    self._state.save_input_materialization(
                        attempt_id=attempt_id,
                        receipt=materialization,
                    )
                else:
                    self._revalidate_materialized_inputs(
                        intent=intent,
                        input_root=input_root,
                        spec=spec,
                        expected=materialization,
                    )
            else:
                materialization = self._state.load_input_materialization(attempt_id=attempt_id)
                if materialization != state.input_materialization_receipt:
                    raise LocalStateError(
                        "durable input receipt differs from attempt state lineage"
                    )
                self._revalidate_materialized_inputs(
                    intent=intent,
                    input_root=input_root,
                    spec=spec,
                    expected=materialization,
                )
            request = self._launch_request(
                intent=intent,
                reservation=reservation,
                spec=spec,
                input_root=input_root,
                output_root=output_root,
                input_materialization_receipt=materialization,
                output_quota_provisioning_receipt=quota_receipt,
            )
            if state is None:
                preparation = self._validate_runtime_preparation(
                    self._runtime.prepare(request=request), request=request
                )
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
                    input_materialization_receipt=materialization,
                    output_quota_provisioning_receipt=quota_receipt,
                    runtime_preparation=preparation,
                    runtime_launch_authorization_request=None,
                    runtime_launch_authorization=None,
                    node_runtime_launch_receipt=None,
                    runtime_identity=None,
                )
                self._state.save_state(state)
            else:
                if not launch_allowed:
                    if historical_pre_runtime_lineage is not None:
                        state = self._recover_historical_pre_runtime_lineage(
                            lineage=historical_pre_runtime_lineage,
                            state=state,
                            reservation=reservation,
                            grant=grant,
                        )
                    else:
                        self._validate_historical_recovery(
                            assignment=assignment,
                            state=state,
                            reservation=reservation,
                            grant=grant,
                        )
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
                if state.runtime_identity is not None:
                    self._validate_runtime_identity(state.runtime_identity, request=request)

            if historical_pre_runtime_lineage is not None:
                cleaned = self._cleanup_never_started(request=request, state=state)
                if cleaned.state is RuntimeInspectionState.ABSENT:
                    return self._pre_runtime_absence_result(
                        state=state,
                        request=request,
                        observation=cleaned,
                        reservation=reservation,
                        token=token,
                        launch_allowed=False,
                    )
                return self._local_reconciliation(
                    state=state,
                    reason="historical_pre_runtime_cleanup_did_not_prove_absence",
                )

            accepted_bundle = self._state.load_accepted_runtime_termination(attempt_id=attempt_id)
            pending_terminal_proof = self._state.load_runtime_termination_proof(
                attempt_id=attempt_id,
                inspection_sequence=state.inspection_sequence,
            )
            pending_terminal_evidence = self._state.load_runtime_termination_evidence(
                attempt_id=attempt_id,
                inspection_sequence=state.inspection_sequence,
            )
            if (
                accepted_bundle is not None
                or pending_terminal_proof is not None
                or pending_terminal_evidence is not None
            ):
                proof = (
                    (accepted_bundle[0], accepted_bundle[1])
                    if accepted_bundle is not None
                    else pending_terminal_proof
                )
                evidence = (
                    proof[1].termination_evidence
                    if proof is not None
                    else (
                        pending_terminal_evidence[1]
                        if pending_terminal_evidence is not None
                        else None
                    )
                )
                assert evidence is not None
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    request=request,
                    observation=evidence,
                    output_root=output_root,
                    persisted_terminal_proof=proof,
                    accepted_bundle=accepted_bundle,
                    pending_inspection_sequence=(
                        pending_terminal_evidence[0]
                        if pending_terminal_evidence is not None
                        else None
                    ),
                    historical_recovery_grant=(
                        assignment.historical_recovery_grant if not launch_allowed else None
                    ),
                )

            if state.phase is AttemptPhase.LAUNCH_COMMITTED and state.runtime_identity is None:
                recovered_state = self._recover_started(request=request, state=state)
                if recovered_state is not None:
                    state = recovered_state
                else:
                    authorization_request = state.runtime_launch_authorization_request
                    authorization = state.runtime_launch_authorization
                    fresh_start_authority = False
                    if (
                        launch_allowed
                        and reservation.status == "starting"
                        and authorization_request is not None
                        and authorization is not None
                    ):
                        try:
                            self._validate_launch_authorization(
                                authorization_request=authorization_request,
                                authorization=authorization,
                                preparation=state.runtime_preparation,
                                reservation=reservation,
                            )
                            fresh_start_authority = True
                        except RuntimeRejected:
                            fresh_start_authority = False
                    if fresh_start_authority:
                        state = self._ensure_started(
                            request=request,
                            state=state,
                            reservation=reservation,
                        )
                    else:
                        cleaned = self._cleanup_never_started(request=request, state=state)
                        if cleaned.state is RuntimeInspectionState.ABSENT:
                            return self._pre_runtime_absence_result(
                                state=state,
                                request=request,
                                observation=cleaned,
                                reservation=reservation,
                                token=token,
                                launch_allowed=launch_allowed,
                            )
                        return self._local_reconciliation(
                            state=state,
                            reason="launch_gap_is_not_proven_started_or_never_started",
                        )

            observation = self._validated_inspection(request=request, state=state)
            if (
                observation.state is RuntimeInspectionState.ABSENT
                and state.phase in {AttemptPhase.PREPARED, AttemptPhase.RECONCILIATION_REQUIRED}
                and self._state.load_pre_runtime_absence_request(
                    attempt_id=state.attempt_id,
                    absence_epoch=observation.prelaunch_absence_epoch or 0,
                )
                is not None
            ):
                return self._pre_runtime_absence_result(
                    state=state,
                    request=request,
                    observation=observation,
                    reservation=reservation,
                    token=token,
                    launch_allowed=launch_allowed,
                )
            if reservation.status == "reconciliation_required":
                if observation.state is RuntimeInspectionState.ABSENT:
                    if not (launch_allowed and state.phase is AttemptPhase.START_REQUESTED):
                        return self._pre_runtime_absence_result(
                            state=state,
                            request=request,
                            observation=observation,
                            reservation=reservation,
                            token=token,
                            launch_allowed=launch_allowed,
                        )
                elif observation.runtime_identity is None:
                    return self._local_reconciliation(
                        state=state, reason="recovery_has_no_actual_runtime_identity"
                    )
                else:
                    return self._recover_reconciliation(
                        intent=intent,
                        reservation=reservation,
                        request=request,
                        state=state,
                        token=token,
                        observation=observation,
                        lock_evidence=initial_lock,
                        launch_allowed=launch_allowed,
                    )
            if observation.state is RuntimeInspectionState.UNKNOWN:
                if observation.runtime_identity is None:
                    return self._local_reconciliation(
                        state=state, reason="prelaunch_runtime_inspection_unknown"
                    )
                return self._retain_reconciliation(
                    reservation=reservation,
                    state=state,
                    token=token,
                    request=request,
                    observation=observation,
                    reason="runtime_inspection_unknown",
                )
            if observation.state is RuntimeInspectionState.TERMINATED:
                return self._collect_stopped(
                    intent=intent,
                    spec=spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    request=request,
                    observation=observation,
                    output_root=output_root,
                )
            if observation.state is RuntimeInspectionState.RUNNING:
                if (
                    not state.launch_committed
                    or state.runtime_identity is None
                    or state.node_runtime_launch_receipt is None
                ):
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
                                node_runtime_launch_receipt=(state.node_runtime_launch_receipt),
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
                        runtime_control_journal_sha256=(observation.runtime_control_journal_sha256),
                    )
                    self._state.save_state(state)
                return self._heartbeat_running(reservation=reservation, state=state, token=token)

            if observation.state is not RuntimeInspectionState.ABSENT:
                return self._local_reconciliation(
                    state=state, reason="prelaunch_runtime_state_is_not_exact_absence"
                )
            if not launch_allowed:
                return self._pre_runtime_absence_result(
                    state=state,
                    request=request,
                    observation=observation,
                    reservation=reservation,
                    token=token,
                    launch_allowed=False,
                )
            if state.phase is AttemptPhase.PREPARED:
                authorization_request = self._new_launch_authorization_request(state=state)
                state = replace(
                    state,
                    phase=AttemptPhase.START_REQUESTED,
                    runtime_launch_authorization_request=authorization_request,
                )
                # The nonce/request is durable before the allocator can commit STARTING.
                self._state.save_state(state)
            if state.phase is AttemptPhase.START_REQUESTED:
                try:
                    authorization_request = state.runtime_launch_authorization_request
                    if authorization_request is None:
                        raise LocalStateError(
                            "start-requested phase lost durable authorization request"
                        )
                    start_authorization = self._allocator.start_attempt(
                        attempt_id=attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                        runtime_preparation=state.runtime_preparation,
                        launch_authorization_request=authorization_request,
                    )
                    if not isinstance(start_authorization, RuntimeStartAuthorization):
                        raise RuntimeRejected(
                            "allocator start response omitted atomic launch authorization"
                        )
                    if type(start_authorization.replayed) is not bool:
                        raise RuntimeRejected(
                            "allocator start response has a non-boolean replay marker"
                        )
                    reservation = self._validate_allocator_response(
                        start_authorization.reservation,
                        baseline=reservation,
                        expected_statuses=(
                            frozenset({"starting", "reconciliation_required"})
                            if start_authorization.replayed
                            else frozenset({"starting"})
                        ),
                        expected_fencing_epoch=state.fencing_epoch,
                        expected_lease_token_sha256=state.lease_token_sha256,
                        require_live_authority=not start_authorization.replayed,
                        operation="start_attempt",
                    )
                    if start_authorization.replayed:
                        self._validate_historical_launch_authorization(
                            authorization_request=authorization_request,
                            authorization=start_authorization.launch_authorization,
                            preparation=state.runtime_preparation,
                            reservation=reservation,
                        )
                    else:
                        self._validate_launch_authorization(
                            authorization_request=authorization_request,
                            authorization=start_authorization.launch_authorization,
                            preparation=state.runtime_preparation,
                            reservation=reservation,
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
                    phase=AttemptPhase.START_AUTHORIZED,
                    runtime_launch_authorization_request=authorization_request,
                    runtime_launch_authorization=(start_authorization.launch_authorization),
                )
                self._state.save_state(state)
            elif state.phase not in {
                AttemptPhase.START_AUTHORIZED,
                AttemptPhase.LAUNCH_COMMITTED,
            }:
                return self._local_reconciliation(
                    state=state, reason="runtime_absence_has_no_launchable local phase"
                )
            if state.phase is AttemptPhase.START_AUTHORIZED:
                authorization_request = state.runtime_launch_authorization_request
                authorization = state.runtime_launch_authorization
                if authorization_request is None or authorization is None:
                    raise LocalStateError(
                        "start-authorized phase lost durable authorization lineage"
                    )
                self._validate_historical_launch_authorization(
                    authorization_request=authorization_request,
                    authorization=authorization,
                    preparation=state.runtime_preparation,
                    reservation=reservation,
                )
                if not self._launch_authority_is_live(
                    authorization_request=authorization_request,
                    authorization=authorization,
                    preparation=state.runtime_preparation,
                    reservation=reservation,
                    state=state,
                ):
                    cleaned = self._cleanup_never_started(request=request, state=state)
                    if cleaned.state is RuntimeInspectionState.ABSENT:
                        return self._pre_runtime_absence_result(
                            state=state,
                            request=request,
                            observation=cleaned,
                            reservation=reservation,
                            token=token,
                            launch_allowed=reservation.status == "starting",
                        )
                    return self._local_reconciliation(
                        state=state,
                        reason="stale_start_authorization_cleanup_did_not_prove_absence",
                    )
            if reservation.status != "starting":
                return self._local_reconciliation(
                    state=state, reason="runtime_launch_requires_allocator starting authority"
                )
            if state.phase is not AttemptPhase.LAUNCH_COMMITTED:
                state = replace(
                    state,
                    phase=AttemptPhase.LAUNCH_COMMITTED,
                    launch_committed=True,
                )
                self._state.save_state(state)
            state = self._ensure_started(
                request=request,
                state=state,
                reservation=reservation,
            )
            observation = self._validated_inspection(request=request, state=state)
            if observation.state is RuntimeInspectionState.RUNNING:
                try:
                    snapshot = self._allocator.mark_running(
                        attempt_id=attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                        node_runtime_launch_receipt=state.node_runtime_launch_receipt,
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
                    runtime_control_journal_sha256=(observation.runtime_control_journal_sha256),
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
                    request=request,
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
        HistoricalPreRuntimeRecoveryLineage | None,
        bool,
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
            historical_recovery = (
                HistoricalRuntimeRecoveryGrant.model_validate(
                    assignment.historical_recovery_grant.model_dump(mode="python")
                )
                if assignment.historical_recovery_grant is not None
                else None
            )
            historical_pre_runtime = (
                HistoricalPreRuntimeRecoveryLineage.model_validate(
                    assignment.historical_pre_runtime_recovery_lineage.model_dump(mode="python")
                )
                if assignment.historical_pre_runtime_recovery_lineage is not None
                else None
            )
            if not isinstance(reservation, NodeReservation):
                raise TypeError("reservation is not the closed node adapter projection")
            if historical_recovery is not None and historical_pre_runtime is not None:
                raise ValueError("assignment contains two historical recovery modes")
            if historical_pre_runtime is not None and assignment.lease_token is not None:
                raise ValueError(
                    "historical pre-runtime recovery assignment cannot carry a raw token"
                )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AssignmentRejected("assignment contract failed closed revalidation") from exc
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise AssignmentRejected("node clock must provide timezone-aware UTC")
        recovery_only_assignment = (
            historical_recovery is not None or historical_pre_runtime is not None
        )
        launch_allowed = not recovery_only_assignment

        def verify_historical_assignment_authority() -> None:
            if historical_recovery is not None:
                verify_historical_runtime_recovery_grant(
                    grant=historical_recovery,
                    authority=self._runtime_control_authority,
                    observed_at=now,
                )
                qualification_observed_at = historical_recovery.admitted_at
            elif historical_pre_runtime is not None:
                verify_runtime_launch_authorization_ticket_historical(
                    authorization=(historical_pre_runtime.runtime_launch_authorization),
                    authorization_request=(
                        historical_pre_runtime.runtime_launch_authorization_request
                    ),
                    preparation=historical_pre_runtime.runtime_preparation,
                    authority=self._runtime_control_authority,
                )
                qualification_observed_at = (
                    historical_pre_runtime.runtime_launch_authorization.issued_at
                )
            else:
                raise QualificationVerificationError(
                    "assignment lacks historical recovery authority"
                )
            self._qualification_authority.verify_signature(
                grant, observed_at=qualification_observed_at
            )

        try:
            self._qualification_authority.verify_signature(grant, observed_at=now)
        except QualificationVerificationError as current_error:
            if not recovery_only_assignment:
                raise AssignmentRejected(
                    "qualification grant is not active and no recovery-only grant exists"
                ) from current_error
            try:
                verify_historical_assignment_authority()
            except QualificationVerificationError as historical_error:
                raise AssignmentRejected(
                    "historical recovery does not bind a previously valid qualification"
                ) from historical_error
            launch_allowed = False
        else:
            if recovery_only_assignment:
                try:
                    verify_historical_assignment_authority()
                except QualificationVerificationError as historical_error:
                    raise AssignmentRejected(
                        "historical recovery does not bind a previously valid qualification"
                    ) from historical_error
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
                "qualification node accepts replay-safe, network-none, non-checkpoint work only"
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
            or reservation.hard_deadline + self._artifact_completion_grace
            > self._node_authority.active_until
            or reservation.hard_deadline + self._artifact_completion_grace
            > self._runtime_control_authority.pin.active_until
            or not now < reservation.hard_deadline + self._artifact_completion_grace
            or (
                historical_pre_runtime is None
                and reservation.status not in {"reconciliation_required", "terminated", "verifying"}
                and not now < reservation.lease_expires_at
            )
            or (
                historical_pre_runtime is None
                and reservation.status not in {"reconciliation_required", "terminated", "verifying"}
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
            or intent.resource_request.accelerator_count != 0
            or bool(devices)
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
        if tuple(item.input_port_id for item in spec.input_paths) != tuple(
            item.input_port_id for item in intent.input_artifact_bindings
        ):
            raise AssignmentRejected("pinned launch input map differs from bound inputs")
        if historical_pre_runtime is not None:
            preparation = historical_pre_runtime.runtime_preparation
            authorization = historical_pre_runtime.runtime_launch_authorization
            if (
                reservation.status not in {"starting", "reconciliation_required"}
                or preparation.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
                or preparation.node_id != reservation.node_id
                or preparation.boot_id != self._boot_id
                or preparation.execution_id != intent.execution_id
                or preparation.infrastructure_attempt_id != reservation.attempt_id
                or preparation.intent_sha256 != intent.intent_sha256
                or preparation.launch_spec_sha256 != spec.launch_spec_sha256
                or preparation.fencing_epoch != reservation.fencing_epoch
                or preparation.lease_token_sha256 != reservation.lease_token_sha256
                or preparation.prepared_at > now
                or authorization.admission_sha256 != reservation.admission_sha256
                or authorization.qualification_grant_sha256 != grant.grant_sha256
                or authorization.lease_expires_at != reservation.lease_expires_at
                or authorization.hard_deadline != reservation.hard_deadline
                or authorization.issued_at > now
            ):
                raise AssignmentRejected(
                    "historical pre-runtime lineage differs from exact assignment authority"
                )
        return (
            intent,
            node,
            grant,
            reservation,
            spec,
            historical_pre_runtime,
            launch_allowed,
        )

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

    def _ensure_runtime_rebind(
        self,
        *,
        state: _AttemptState,
        new_fencing_epoch: int,
        new_lease_token_sha256: str,
    ) -> tuple[RuntimeFenceRebindRequest, RuntimeFenceRebindReceipt]:
        if (
            state.runtime_identity is None
            or state.runtime_control_journal_sha256 is None
            or new_fencing_epoch != state.fencing_epoch + 1
        ):
            raise LocalStateError(
                "runtime fence rebind lacks exact running control-journal lineage"
            )
        sequence = state.runtime_rebind_sequence + 1
        persisted = self._state.load_runtime_rebind(attempt_id=state.attempt_id, sequence=sequence)
        if persisted is not None:
            persisted_request, persisted_evidence, persisted_receipt = persisted
            if (
                persisted_request.previous_fencing_epoch != state.fencing_epoch
                or persisted_request.previous_lease_token_sha256 != state.lease_token_sha256
                or persisted_request.new_fencing_epoch != new_fencing_epoch
                or persisted_request.new_lease_token_sha256 != new_lease_token_sha256
                or persisted_request.preparation_sha256
                != state.runtime_preparation.preparation_sha256
                or persisted_request.runtime_identity_sha256
                != state.runtime_identity.runtime_identity_sha256
            ):
                raise LocalStateError("durable runtime rebind differs from pending adoption")
            try:
                if (
                    persisted_receipt.node_manifest_sha256
                    != self._node_authority.manifest.manifest_sha256
                ):
                    raise QualificationVerificationError(
                        "runtime rebind receipt belongs to another node"
                    )
                self._node_authority.verify_signature(
                    signing_key_id=persisted_receipt.signing_key_id,
                    message=persisted_receipt.signature_message,
                    signature_ed25519_hex=persisted_receipt.signature_ed25519_hex,
                    signed_at=persisted_receipt.signed_at,
                )
            except QualificationVerificationError as exc:
                raise LocalStateError("durable runtime rebind signature is invalid") from exc
            del persisted_evidence
            return persisted_request, persisted_receipt
        requested_at = self._clock.now()
        request = RuntimeFenceRebindRequest(
            preparation_sha256=state.runtime_preparation.preparation_sha256,
            runtime_identity_sha256=state.runtime_identity.runtime_identity_sha256,
            previous_fencing_epoch=state.fencing_epoch,
            previous_lease_token_sha256=state.lease_token_sha256,
            new_fencing_epoch=new_fencing_epoch,
            new_lease_token_sha256=new_lease_token_sha256,
            rebind_sequence=sequence,
            expected_runtime_control_journal_sha256=(state.runtime_control_journal_sha256),
            requested_at=requested_at,
            requested_monotonic_ns=self._clock.monotonic_ns(),
        )
        try:
            evidence = RuntimeFenceRebindEvidence.model_validate(
                self._runtime.rebind_fence(
                    request=request,
                    preparation=state.runtime_preparation,
                    identity=state.runtime_identity,
                ).model_dump(mode="python")
            )
            validate_runtime_fence_rebind_evidence(request=request, evidence=evidence)
            receipt = issue_runtime_fence_rebind_receipt(
                manifest=self._node_authority.manifest,
                request=request,
                evidence=evidence,
                signed_at=self._clock.now(),
                private_key=self._private_key,
            )
        except (AttributeError, QualificationVerificationError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime fence rebind failed closed validation") from exc
        self._state.save_runtime_rebind(
            attempt_id=state.attempt_id,
            request=request,
            evidence=evidence,
            receipt=receipt,
        )
        return request, receipt

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
            or state.runtime_identity is None
            or state.node_runtime_launch_receipt is None
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
        persisted_rebind = self._state.load_runtime_rebind(
            attempt_id=state.attempt_id,
            sequence=state.runtime_rebind_sequence + 1,
        )
        if (
            reservation.fencing_epoch == receipt.previous_fencing_epoch
            and reservation.lease_token_sha256 == receipt.previous_lease_token_sha256
        ):
            if not self._inspection_receipt_is_fresh(receipt.runtime_inspection_receipt):
                if persisted_rebind is not None:
                    return self._local_reconciliation(
                        state=state,
                        reason="runtime_rebound_but_pending_adoption_proof_expired",
                    )
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
            runtime_rebind_request, runtime_rebind_receipt = self._ensure_runtime_rebind(
                state=state,
                new_fencing_epoch=receipt.new_fencing_epoch,
                new_lease_token_sha256=receipt.new_lease_token_sha256,
            )
            try:
                snapshot = self._allocator.adopt_attempt(
                    receipt=receipt,
                    previous_lease_token=token,
                    previous_fencing_epoch=receipt.previous_fencing_epoch,
                    new_lease_token=new_token,
                    runtime_fence_rebind_request=runtime_rebind_request,
                    runtime_fence_rebind_receipt=runtime_rebind_receipt,
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
                runtime_rebind_receipt=runtime_rebind_receipt,
                operation="pending_adoption_replay",
            )
        elif (
            reservation.fencing_epoch == receipt.new_fencing_epoch
            and reservation.lease_token_sha256 == receipt.new_lease_token_sha256
        ):
            if persisted_rebind is None:
                raise LocalStateError(
                    "allocator adoption committed without durable runtime fence rebind"
                )
            _, _, runtime_rebind_receipt = persisted_rebind
            return self._apply_adoption_snapshot(
                snapshot=reservation,
                baseline=reservation,
                state=state,
                receipt=receipt,
                runtime_rebind_receipt=runtime_rebind_receipt,
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
        runtime_rebind_receipt: RuntimeFenceRebindReceipt,
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
            runtime_rebind_sequence=runtime_rebind_receipt.evidence.rebind_sequence,
            runtime_control_journal_sha256=(
                runtime_rebind_receipt.evidence.new_runtime_control_journal_sha256
            ),
            running_confirmed=running_confirmed,
        )
        self._state.save_state(state)
        return NodeRunResult(
            outcome=outcome,
            attempt_id=state.attempt_id,
            runtime_preparation=state.runtime_preparation,
            runtime_identity=state.runtime_identity,
            node_runtime_launch_receipt=state.node_runtime_launch_receipt,
            inspection_receipt=receipt.runtime_inspection_receipt,
            adoption_receipt=receipt,
            runtime_fence_rebind_receipt=runtime_rebind_receipt,
        )

    def _launch_request(
        self,
        *,
        intent: ExecutionIntent,
        reservation: NodeReservation,
        spec: PinnedLaunchSpec,
        input_root: Path,
        output_root: Path,
        input_materialization_receipt: InputMaterializationReceipt,
        output_quota_provisioning_receipt: OutputQuotaProvisioningReceipt,
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
            node_manifest_sha256=self._node_authority.manifest.manifest_sha256,
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
            input_materialization_receipt=input_materialization_receipt,
            output_quota_provisioning_receipt=output_quota_provisioning_receipt,
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

    def _validate_runtime_preparation(
        self, preparation: RuntimePreparation, *, request: RuntimeLaunchRequest
    ) -> RuntimePreparation:
        try:
            preparation = RuntimePreparation.model_validate(preparation.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime preparation failed closed validation") from exc
        now = self._clock.now()
        checked_monotonic_ns = self._clock.monotonic_ns()
        if (
            preparation.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or preparation.node_id != request.node_id
            or preparation.boot_id != request.boot_id
            or preparation.execution_id != request.execution_id
            or preparation.infrastructure_attempt_id != request.attempt_id
            or preparation.intent_sha256 != request.intent_sha256
            or preparation.runtime_id != request.runtime_id
            or preparation.runtime_engine != request.spec.runtime_engine
            or preparation.launch_spec_sha256 != request.spec.launch_spec_sha256
            or preparation.workload_executable_sha256 != request.spec.executable_sha256
            or preparation.workload_argv != request.spec.argv
            or preparation.runtime_request_sha256 != request.runtime_request_sha256
            or preparation.enforced_placement_sha256 != request.enforced_placement_sha256
            or preparation.input_materialization_receipt_sha256
            != request.input_materialization_receipt.materialization_receipt_sha256
            or preparation.output_quota_provisioning_receipt_sha256
            != request.output_quota_provisioning_receipt.provisioning_receipt_sha256
            or preparation.fencing_epoch != request.fencing_epoch
            or preparation.lease_token_sha256 != request.lease_token_sha256
            or preparation.prepared_at > now
            or preparation.prepared_monotonic_ns > checked_monotonic_ns
        ):
            raise RuntimeRejected("runtime preparation differs from exact inert launch request")
        return preparation

    def _new_launch_authorization_request(
        self,
        *,
        state: _AttemptState,
        pre_runtime_absence_receipt: PreRuntimeAbsenceReceipt | None = None,
    ) -> RuntimeLaunchAuthorizationRequest:
        return RuntimeLaunchAuthorizationRequest(
            request_nonce_sha256=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            runtime_preparation_sha256=state.runtime_preparation.preparation_sha256,
            infrastructure_attempt_id=state.attempt_id,
            fencing_epoch=state.fencing_epoch,
            lease_token_sha256=state.lease_token_sha256,
            pre_runtime_absence_epoch=(
                pre_runtime_absence_receipt.absence_evidence.prelaunch_absence_epoch
                if pre_runtime_absence_receipt is not None
                else 0
            ),
            pre_runtime_absence_receipt_sha256=(
                pre_runtime_absence_receipt.absence_receipt_sha256
                if pre_runtime_absence_receipt is not None
                else None
            ),
            requested_at=self._clock.now(),
            requested_monotonic_ns=self._clock.monotonic_ns(),
        )

    def _validate_launch_authorization(
        self,
        *,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        preparation: RuntimePreparation,
        reservation: NodeReservation,
        observed_at: datetime | None = None,
        observed_monotonic_ns: int | None = None,
    ) -> None:
        checked_at = observed_at or self._clock.now()
        checked_monotonic_ns = (
            observed_monotonic_ns
            if observed_monotonic_ns is not None
            else self._clock.monotonic_ns()
        )
        try:
            verify_runtime_launch_authorization(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
                observed_at=checked_at,
                observed_monotonic_ns=checked_monotonic_ns,
            )
        except QualificationVerificationError as exc:
            raise RuntimeRejected("runtime launch authorization is invalid or stale") from exc
        if (
            authorization.admission_sha256 != reservation.admission_sha256
            or authorization.qualification_grant_sha256 != reservation.grant_sha256
            or authorization.lease_expires_at != reservation.lease_expires_at
            or authorization.hard_deadline != reservation.hard_deadline
        ):
            raise RuntimeRejected(
                "runtime launch authorization differs from allocator admission/lease"
            )

    def _validate_historical_launch_authorization(
        self,
        *,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        preparation: RuntimePreparation,
        reservation: NodeReservation,
    ) -> None:
        """Validate immutable ticket lineage without granting a fresh runtime start."""

        try:
            verify_runtime_launch_authorization_ticket_historical(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
            )
        except QualificationVerificationError as exc:
            raise RuntimeRejected("historical runtime launch authorization is invalid") from exc
        if (
            authorization.admission_sha256 != reservation.admission_sha256
            or authorization.qualification_grant_sha256 != reservation.grant_sha256
            or authorization.lease_expires_at != reservation.lease_expires_at
            or authorization.hard_deadline != reservation.hard_deadline
        ):
            raise RuntimeRejected(
                "historical runtime launch authorization differs from allocator authority"
            )

    def _launch_authority_is_live(
        self,
        *,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        preparation: RuntimePreparation,
        reservation: NodeReservation,
        state: _AttemptState,
    ) -> bool:
        """Return whether exact historical lineage still permits the one launch mutation."""

        try:
            self._validate_allocator_response(
                reservation,
                baseline=reservation,
                expected_statuses=frozenset({"starting"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=True,
                operation="historical_start_online_revalidation",
            )
            verify_runtime_launch_authorization(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
                observed_at=self._clock.now(),
                observed_monotonic_ns=self._clock.monotonic_ns(),
            )
        except (NodeLeaseRejected, QualificationVerificationError):
            return False
        return True

    def _launch_absence_receipt(
        self,
        *,
        state: _AttemptState,
        authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> PreRuntimeAbsenceReceipt | None:
        """Load the full signed receipt transitively bound by a replacement ticket."""

        if authorization_request.pre_runtime_absence_epoch == 0:
            if authorization_request.pre_runtime_absence_receipt_sha256 is not None:
                raise LocalStateError("initial launch request unexpectedly binds absence evidence")
            return None
        pending_absence = self._state.load_pre_runtime_absence_request(
            attempt_id=state.attempt_id,
            absence_epoch=authorization_request.pre_runtime_absence_epoch,
        )
        if (
            pending_absence is None
            or pending_absence[1] != authorization_request
            or pending_absence[0].absence_receipt_sha256
            != authorization_request.pre_runtime_absence_receipt_sha256
        ):
            raise LocalStateError(
                "replacement launch lost its exact signed pre-runtime absence receipt"
            )
        return pending_absence[0]

    def _record_runtime_launch_evidence(
        self,
        *,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
        authorization_request: RuntimeLaunchAuthorizationRequest,
        authorization: RuntimeLaunchAuthorization,
        evidence: RuntimeLaunchEvidence,
    ) -> _AttemptState:
        identity = evidence.runtime_identity
        self._validate_runtime_identity(identity, request=request)
        observed_now = self._clock.now()
        checked_monotonic_ns = self._clock.monotonic_ns()
        if (
            evidence.preparation_sha256 != state.runtime_preparation.preparation_sha256
            or evidence.runtime_launch_authorization_sha256 != authorization.authorization_sha256
            or evidence.enforced_placement_sha256 != request.enforced_placement_sha256
            or evidence.input_materialization_receipt_sha256
            != request.input_materialization_receipt.materialization_receipt_sha256
            or evidence.enforced_fencing_epoch != state.fencing_epoch
            or evidence.enforced_lease_token_sha256 != state.lease_token_sha256
            or evidence.observed_at > observed_now
            or evidence.observed_monotonic_ns > checked_monotonic_ns
        ):
            raise RuntimeRejected("runtime launch evidence differs from exact authorized launch")
        try:
            receipt = issue_node_runtime_launch_receipt(
                manifest=self._node_authority.manifest,
                preparation=state.runtime_preparation,
                launch_authorization_request=authorization_request,
                launch_authorization=authorization,
                launch_evidence=evidence,
                runtime_authority=self._runtime_control_authority,
                signed_at=self._clock.now(),
                private_key=self._private_key,
            )
        except (QualificationVerificationError, ValueError) as exc:
            raise RuntimeRejected("runtime launch evidence cannot be node signed") from exc
        state = replace(
            state,
            runtime_identity=identity,
            node_runtime_launch_receipt=receipt,
        )
        self._state.save_state(state)
        return state

    def _ensure_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
        reservation: NodeReservation,
    ) -> _AttemptState:
        authorization_request = state.runtime_launch_authorization_request
        authorization = state.runtime_launch_authorization
        if authorization_request is None or authorization is None:
            raise RuntimeRejected("runtime launch has no persisted signed authorization pair")
        pre_runtime_absence_receipt = self._launch_absence_receipt(
            state=state,
            authorization_request=authorization_request,
        )
        # This is the final node-side check immediately before the only launch-capable call.
        self._validate_allocator_response(
            reservation,
            baseline=reservation,
            expected_statuses=frozenset({"starting"}),
            expected_fencing_epoch=state.fencing_epoch,
            expected_lease_token_sha256=state.lease_token_sha256,
            require_live_authority=True,
            operation="pre_runtime_launch_revalidation",
        )
        self._validate_launch_authorization(
            authorization_request=authorization_request,
            authorization=authorization,
            preparation=state.runtime_preparation,
            reservation=reservation,
        )
        try:
            evidence = RuntimeLaunchEvidence.model_validate(
                self._runtime.ensure_started(
                    request=request,
                    preparation=state.runtime_preparation,
                    authorization_request=authorization_request,
                    authorization=authorization,
                    pre_runtime_absence_receipt=pre_runtime_absence_receipt,
                ).model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime launch evidence failed closed validation") from exc
        return self._record_runtime_launch_evidence(
            request=request,
            state=state,
            authorization_request=authorization_request,
            authorization=authorization,
            evidence=evidence,
        )

    def _recover_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
    ) -> _AttemptState | None:
        authorization_request = state.runtime_launch_authorization_request
        authorization = state.runtime_launch_authorization
        if authorization_request is None or authorization is None:
            raise LocalStateError("launch recovery lost its durable authorization pair")
        absence_receipt = self._launch_absence_receipt(
            state=state,
            authorization_request=authorization_request,
        )
        try:
            recovered = self._runtime.recover_started(
                request=request,
                preparation=state.runtime_preparation,
                authorization_request=authorization_request,
                authorization=authorization,
                pre_runtime_absence_receipt=absence_receipt,
            )
            if recovered is None:
                return None
            evidence = RuntimeLaunchEvidence.model_validate(recovered.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime historical launch recovery failed closed") from exc
        return self._record_runtime_launch_evidence(
            request=request,
            state=state,
            authorization_request=authorization_request,
            authorization=authorization,
            evidence=evidence,
        )

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
            or state.output_quota_provisioning_receipt != request.output_quota_provisioning_receipt
            or state.fencing_epoch != reservation.fencing_epoch
            or state.lease_token_sha256 != reservation.lease_token_sha256
        ):
            raise LocalStateError("recovered attempt state differs from pulled authority")
        preparation = state.runtime_preparation
        if (
            preparation.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or preparation.runtime_id != request.runtime_id
            or preparation.runtime_engine != request.spec.runtime_engine
            or preparation.workload_executable_sha256 != request.spec.executable_sha256
            or preparation.workload_argv != request.spec.argv
            or preparation.enforced_placement_sha256 != request.enforced_placement_sha256
            or preparation.input_materialization_receipt_sha256
            != request.input_materialization_receipt.materialization_receipt_sha256
            or preparation.output_quota_provisioning_receipt_sha256
            != request.output_quota_provisioning_receipt.provisioning_receipt_sha256
            or (state.adoption_sequence == 0 and preparation.fencing_epoch != state.fencing_epoch)
            or (
                state.adoption_sequence == 0
                and preparation.lease_token_sha256 != state.lease_token_sha256
            )
            or state.runtime_rebind_sequence != state.adoption_sequence
        ):
            raise LocalStateError("recovered runtime preparation or fence lineage diverges")
        if state.adoption_sequence == 0:
            self._validate_runtime_preparation(preparation, request=request)
        receipt = state.node_runtime_launch_receipt
        if receipt is not None:
            authorization_request = state.runtime_launch_authorization_request
            authorization = state.runtime_launch_authorization
            if authorization_request is None or authorization is None:
                raise LocalStateError("recovered launch receipt lost authorization lineage")
            try:
                verify_node_runtime_launch_receipt_historical(
                    receipt=receipt,
                    preparation=preparation,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    authority=self._node_authority,
                    runtime_authority=self._runtime_control_authority,
                )
            except QualificationVerificationError as exc:
                raise LocalStateError(
                    "recovered runtime launch/authorization signature is invalid"
                ) from exc

    def _recover_historical_pre_runtime_lineage(
        self,
        *,
        lineage: HistoricalPreRuntimeRecoveryLineage,
        state: _AttemptState,
        reservation: NodeReservation,
        grant: EngineeringQualificationGrant,
    ) -> _AttemptState:
        """Durably recover one DB-committed ticket strictly as cleanup evidence."""

        preparation = lineage.runtime_preparation
        authorization_request = lineage.runtime_launch_authorization_request
        authorization = lineage.runtime_launch_authorization
        try:
            verify_runtime_launch_authorization_ticket_historical(
                authorization=authorization,
                authorization_request=authorization_request,
                preparation=preparation,
                authority=self._runtime_control_authority,
            )
        except QualificationVerificationError as exc:
            raise AssignmentRejected(
                "historical pre-runtime launch authorization is invalid"
            ) from exc
        if (
            state.phase
            not in {
                AttemptPhase.START_REQUESTED,
                AttemptPhase.START_AUTHORIZED,
                AttemptPhase.LAUNCH_COMMITTED,
                AttemptPhase.RECONCILIATION_REQUIRED,
            }
            or state.runtime_preparation != preparation
            or state.runtime_launch_authorization_request != authorization_request
            or state.running_confirmed
            or state.runtime_identity is not None
            or state.node_runtime_launch_receipt is not None
            or (
                state.phase is not AttemptPhase.RECONCILIATION_REQUIRED
                and state.launch_committed != (state.phase is AttemptPhase.LAUNCH_COMMITTED)
            )
            or state.attempt_id != reservation.attempt_id
            or state.execution_id != reservation.execution_id
            or state.intent_sha256 != reservation.intent_sha256
            or state.node_id != reservation.node_id
            or state.fencing_epoch != reservation.fencing_epoch
            or state.lease_token_sha256 != reservation.lease_token_sha256
            or preparation.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or authorization.admission_sha256 != reservation.admission_sha256
            or authorization.qualification_grant_sha256 != grant.grant_sha256
            or authorization.lease_expires_at != reservation.lease_expires_at
            or authorization.hard_deadline != reservation.hard_deadline
        ):
            raise AssignmentRejected(
                "historical pre-runtime recovery differs from durable local lineage"
            )
        if state.phase is AttemptPhase.START_REQUESTED:
            if state.runtime_launch_authorization is not None:
                raise LocalStateError("start-requested state already contains launch authorization")
            state = replace(
                state,
                phase=AttemptPhase.START_AUTHORIZED,
                runtime_launch_authorization=authorization,
            )
            # Persist DB-committed authority before inspecting or mutating runtime state.  This
            # ticket is historical evidence only; the caller always follows with cleanup.
            self._state.save_state(state)
        elif state.runtime_launch_authorization != authorization:
            raise AssignmentRejected(
                "historical pre-runtime recovery changed persisted launch authorization"
            )
        return state

    def _validate_historical_recovery(
        self,
        *,
        assignment: QualificationAssignment,
        state: _AttemptState | None,
        reservation: NodeReservation,
        grant: EngineeringQualificationGrant,
    ) -> None:
        recovery = assignment.historical_recovery_grant
        if recovery is None or state is None or state.node_runtime_launch_receipt is None:
            raise AssignmentRejected(
                "historical recovery requires durable preparation and actual launch receipt"
            )
        now = self._clock.now()
        try:
            verify_historical_runtime_recovery_grant(
                grant=recovery,
                authority=self._runtime_control_authority,
                observed_at=now,
            )
        except QualificationVerificationError as exc:
            raise AssignmentRejected(
                "historical runtime recovery authority is invalid or stale"
            ) from exc
        accepted_bundle = self._state.load_accepted_runtime_termination(attempt_id=state.attempt_id)
        if (
            not now < recovery.recovery_expires_at
            or recovery.admission_sha256 != reservation.admission_sha256
            or recovery.qualification_grant_sha256 != grant.grant_sha256
            or recovery.intent_sha256 != state.intent_sha256
            or recovery.execution_id != state.execution_id
            or recovery.infrastructure_attempt_id != state.attempt_id
            or recovery.runtime_preparation_sha256 != state.runtime_preparation.preparation_sha256
            or recovery.node_runtime_launch_receipt_sha256
            != state.node_runtime_launch_receipt.launch_receipt_sha256
            or recovery.hard_deadline != reservation.hard_deadline
            or (
                recovery.accepted_runtime_termination_sha256 is not None
                and recovery.recovery_expires_at
                != reservation.hard_deadline + self._artifact_completion_grace
            )
        ):
            raise AssignmentRejected(
                "historical recovery grant differs from durable launched lineage"
            )
        expected_accepted_sha256 = recovery.accepted_runtime_termination_sha256
        if accepted_bundle is not None:
            if accepted_bundle[2].accepted_termination_sha256 != expected_accepted_sha256:
                raise AssignmentRejected(
                    "historical recovery grant differs from durable accepted termination"
                )
            return
        if expected_accepted_sha256 is None:
            return
        launch_authorization_request = state.runtime_launch_authorization_request
        launch_authorization = state.runtime_launch_authorization
        pending_proof = self._state.load_runtime_termination_proof(
            attempt_id=state.attempt_id,
            inspection_sequence=state.inspection_sequence,
        )
        if (
            state.runtime_identity is None
            or launch_authorization_request is None
            or launch_authorization is None
            or pending_proof is None
        ):
            raise AssignmentRejected(
                "accepted recovery without local acceptance requires a complete persisted proof"
            )
        challenge, node_termination_receipt = pending_proof
        try:
            verify_node_runtime_termination_receipt_historical(
                receipt=node_termination_receipt,
                challenge=challenge,
                preparation=state.runtime_preparation,
                launch_receipt=state.node_runtime_launch_receipt,
                launch_authorization_request=launch_authorization_request,
                launch_authorization=launch_authorization,
                node_authority=self._node_authority,
                runtime_authority=self._runtime_control_authority,
            )
        except QualificationVerificationError as exc:
            raise AssignmentRejected(
                "accepted recovery local terminal proof is not historically authentic"
            ) from exc
        if (
            challenge.inspection_sequence != state.inspection_sequence
            or challenge.node_inventory_sha256 != reservation.node_inventory_sha256
            or challenge.resource_lease_sha256 != reservation.resource_lease_sha256
            or challenge.fencing_epoch != state.fencing_epoch
            or challenge.lease_token_sha256 != state.lease_token_sha256
            or challenge.hard_deadline != reservation.hard_deadline
            or challenge.artifact_submission_deadline
            != reservation.hard_deadline + self._artifact_completion_grace
            or node_termination_receipt.termination_evidence.runtime_identity
            != state.runtime_identity
        ):
            raise AssignmentRejected(
                "accepted recovery local terminal proof differs from durable runtime/fence"
            )

    def _ensure_output_quota(
        self,
        *,
        intent: ExecutionIntent,
        reservation: NodeReservation,
        output_root: Path,
        expected: OutputQuotaProvisioningReceipt | None,
    ) -> OutputQuotaProvisioningReceipt:
        try:
            receipt = OutputQuotaProvisioningReceipt.model_validate(
                self._output_quota_provisioner.ensure_output_quota(
                    node_manifest_sha256=self._node_authority.manifest.manifest_sha256,
                    node_id=reservation.node_id,
                    boot_id=self._boot_id,
                    execution_id=intent.execution_id,
                    attempt_id=reservation.attempt_id,
                    intent_sha256=intent.intent_sha256,
                    output_root=output_root,
                    output_quota_bytes=intent.resource_request.artifact_quota_bytes,
                    expected_receipt=expected,
                ).model_dump(mode="python")
            )
            metadata = output_root.lstat()
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise LocalStateError(
                "output quota provisioner returned invalid typed evidence"
            ) from exc
        now = self._clock.now()
        if (
            receipt.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or receipt.node_id != reservation.node_id
            or receipt.boot_id != self._boot_id
            or receipt.execution_id != intent.execution_id
            or receipt.infrastructure_attempt_id != reservation.attempt_id
            or receipt.intent_sha256 != intent.intent_sha256
            or receipt.output_root != str(output_root)
            or receipt.output_quota_bytes != intent.resource_request.artifact_quota_bytes
            or receipt.output_root_device != metadata.st_dev
            or receipt.output_root_inode != metadata.st_ino
            or receipt.output_root_owner_uid != metadata.st_uid
            or receipt.output_root_owner_gid != metadata.st_gid
            or receipt.output_root_mode != stat.S_IMODE(metadata.st_mode)
            or receipt.provisioned_at > now
            or receipt.provisioned_at >= min(intent.deadline, reservation.hard_deadline)
        ):
            raise LocalStateError(
                "output quota provisioning differs from assignment or post-mount identity"
            )
        if expected is not None and receipt != expected:
            raise LocalStateError("output quota provisioner rebound durable mount identity")
        return receipt

    def _materialize_and_seal_inputs(
        self,
        *,
        intent: ExecutionIntent,
        input_root: Path,
        spec: PinnedLaunchSpec,
    ) -> InputMaterializationReceipt:
        if intent.input_artifact_bindings:
            if self._input_materializer is None:
                raise AssignmentRejected("verified inputs require a pinned input materializer")
            try:
                receipt = InputMaterializationReceipt.model_validate(
                    self._input_materializer.ensure_verified_inputs(
                        intent=intent, destination=input_root
                    ).model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise LocalStateError("input materializer returned invalid typed evidence") from exc
        else:
            if any(input_root.iterdir()):
                raise LocalStateError("empty input authority has a nonempty staging directory")
            receipt = InputMaterializationReceipt(
                intent_sha256=intent.intent_sha256,
                execution_id=intent.execution_id,
                infrastructure_attempt_id=(intent.infrastructure_attempt.infrastructure_attempt_id),
                entries=(),
                staged_root_identity_sha256=canonical_sha256(
                    {
                        "schema": "aletheia.empty_qualification_input_tree.v2",
                        "intent_sha256": intent.intent_sha256,
                    }
                ),
                materializer_principal_id="principal:node-empty-input-materializer",
                materialized_at=self._clock.now(),
            )
        expected = tuple(
            (
                binding.input_port_id,
                binding.artifact_verified_receipt_sha256,
                pinned.relative_path,
            )
            for binding, pinned in zip(
                intent.input_artifact_bindings, spec.input_paths, strict=True
            )
        )
        actual = tuple(
            (item.input_port_id, item.verified_receipt_sha256, item.relative_path)
            for item in receipt.entries
        )
        if (
            receipt.intent_sha256 != intent.intent_sha256
            or receipt.execution_id != intent.execution_id
            or receipt.infrastructure_attempt_id
            != intent.infrastructure_attempt.infrastructure_attempt_id
            or actual != expected
            or receipt.materialized_at > self._clock.now()
        ):
            raise LocalStateError(
                "input materialization differs from exact bound input/path authority"
            )
        if intent.input_artifact_bindings:
            # Typed materializers own sealing and compute their identity after chmod.  Repeating
            # chmod here changes ctime and invalidates the receipt even when the mode is equal.
            self._validate_read_only_tree(input_root)
        else:
            self._seal_read_only_tree(input_root)
        return receipt

    def _revalidate_materialized_inputs(
        self,
        *,
        intent: ExecutionIntent,
        input_root: Path,
        spec: PinnedLaunchSpec,
        expected: InputMaterializationReceipt,
    ) -> None:
        """Freshly rehash staged bytes before every recovered runtime operation."""

        if intent.input_artifact_bindings:
            actual = self._materialize_and_seal_inputs(
                intent=intent,
                input_root=input_root,
                spec=spec,
            )
            if actual != expected:
                raise LocalStateError(
                    "fresh input revalidation differs from durable materialization receipt"
                )
            return
        empty_root_sha256 = canonical_sha256(
            {
                "schema": "aletheia.empty_qualification_input_tree.v2",
                "intent_sha256": intent.intent_sha256,
            }
        )
        if (
            any(input_root.iterdir())
            or expected.entries
            or expected.intent_sha256 != intent.intent_sha256
            or expected.execution_id != intent.execution_id
            or expected.infrastructure_attempt_id
            != intent.infrastructure_attempt.infrastructure_attempt_id
            or expected.staged_root_identity_sha256 != empty_root_sha256
        ):
            raise LocalStateError("empty input tree differs from durable exact receipt")
        self._seal_read_only_tree(input_root)

    @classmethod
    def _validate_read_only_tree(cls, root: Path) -> None:
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o500:
            raise LocalStateError("typed input staging root is not sealed read-only")
        for current_root, directory_names, file_names in os.walk(root):
            current = Path(current_root)
            for name in directory_names:
                metadata = (current / name).lstat()
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o500:
                    raise LocalStateError("typed input staging contains an unsealed directory")
            for name in file_names:
                metadata = (current / name).lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                ):
                    raise LocalStateError("typed input staging contains an unsafe/unsealed file")

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
                cls._fsync_sealed_path(path, directory=False)
            for name in directory_names:
                path = current / name
                metadata = path.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LocalStateError("input staging contains a symlink")
                os.chmod(path, 0o500, follow_symlinks=False)
                cls._fsync_sealed_path(path, directory=True)
        os.chmod(root, 0o500, follow_symlinks=False)
        cls._fsync_sealed_path(root, directory=True)

    @staticmethod
    def _fsync_sealed_path(path: Path, *, directory: bool) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            os.fsync(descriptor)
        except OSError as exc:
            raise LocalStateError("sealed input metadata could not be made durable") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validated_inspection(
        self, *, request: RuntimeLaunchRequest, state: _AttemptState
    ) -> RuntimeObservation:
        try:
            observation = RuntimeObservation.model_validate(
                self._runtime.inspect(
                    request=request,
                    preparation=state.runtime_preparation,
                    identity=state.runtime_identity,
                ).model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("runtime inspection failed closed validation") from exc
        return self._validate_runtime_observation(
            request=request,
            state=state,
            observation=observation,
        )

    def _validate_runtime_observation(
        self,
        *,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
        observation: RuntimeObservation,
    ) -> RuntimeObservation:
        if observation.runtime_identity != state.runtime_identity:
            raise RuntimeRejected("runtime inspection changed the exact runtime identity")
        if (
            observation.preparation_sha256 != state.runtime_preparation.preparation_sha256
            or observation.enforced_placement_sha256 != request.enforced_placement_sha256
            or observation.input_materialization_receipt_sha256
            != state.input_materialization_receipt.materialization_receipt_sha256
            or observation.enforced_fencing_epoch != request.fencing_epoch
            or observation.enforced_lease_token_sha256 != request.lease_token_sha256
            or (
                state.runtime_control_journal_sha256 is not None
                and observation.runtime_control_journal_sha256
                != state.runtime_control_journal_sha256
            )
        ):
            raise RuntimeRejected(
                "runtime inspection did not enforce the exact allocator placement/fence"
            )
        self._validate_observation_freshness(observation)
        return observation

    def _cleanup_never_started(
        self,
        *,
        request: RuntimeLaunchRequest,
        state: _AttemptState,
    ) -> RuntimeObservation:
        if state.runtime_identity is not None or state.node_runtime_launch_receipt is not None:
            raise RuntimeRejected("never-started cleanup cannot target a launched runtime")
        authorization_request = state.runtime_launch_authorization_request
        authorization = state.runtime_launch_authorization
        if authorization_request is None or authorization is None:
            raise LocalStateError("never-started cleanup lost its authorization lineage")
        try:
            observation = RuntimeObservation.model_validate(
                self._runtime.cleanup_never_started(
                    request=request,
                    preparation=state.runtime_preparation,
                    authorization_request=authorization_request,
                    authorization=authorization,
                ).model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("never-started cleanup evidence failed closed") from exc
        observation = self._validate_runtime_observation(
            request=request,
            state=state,
            observation=observation,
        )
        if observation.state not in {
            RuntimeInspectionState.ABSENT,
            RuntimeInspectionState.UNKNOWN,
        }:
            raise RuntimeRejected("never-started cleanup claimed a process lifecycle state")
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
        if state.runtime_identity is None or observation.runtime_identity is None:
            raise RuntimeRejected("pre-runtime absence/unknown evidence cannot be an exit receipt")
        if observation.state is RuntimeInspectionState.ABSENT:
            raise RuntimeRejected("ABSENT never-started evidence cannot be signed as process exit")
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

    def _pre_runtime_absence_result(
        self,
        *,
        state: _AttemptState,
        request: RuntimeLaunchRequest,
        observation: RuntimeObservation,
        reservation: NodeReservation | None = None,
        token: str | None = None,
        launch_allowed: bool = False,
    ) -> NodeRunResult:
        if (
            state.runtime_identity is not None
            or state.node_runtime_launch_receipt is not None
            or observation.state is not RuntimeInspectionState.ABSENT
            or observation.runtime_identity is not None
        ):
            raise RuntimeRejected("pre-runtime absence receipt requires never-started lineage")
        self._validate_observation_freshness(observation)
        absence_epoch = observation.prelaunch_absence_epoch
        if absence_epoch is None:
            raise RuntimeRejected("pre-runtime absence omitted its exact cleanup epoch")
        pending_generation = self._state.load_latest_pre_runtime_absence_generation(
            attempt_id=state.attempt_id,
            absence_epoch=absence_epoch,
        )
        cleaned_launch = observation.prelaunch_authorization_request_sha256 is not None
        if pending_generation is None:
            prior_authorization_request = (
                state.runtime_launch_authorization_request if cleaned_launch else None
            )
            prior_authorization = state.runtime_launch_authorization if cleaned_launch else None
            signed_at = self._clock.now()
            expires_at = min(
                observation.inspected_at + self._inspection_ttl,
                self._node_authority.active_until,
            )
            try:
                receipt = issue_pre_runtime_absence_receipt(
                    manifest=self._node_authority.manifest,
                    preparation=state.runtime_preparation,
                    absence_evidence=observation,
                    signed_at=signed_at,
                    expires_at=expires_at,
                    private_key=self._private_key,
                    launch_authorization_request=prior_authorization_request,
                    launch_authorization=prior_authorization,
                    runtime_authority=(self._runtime_control_authority if cleaned_launch else None),
                )
            except QualificationVerificationError as exc:
                raise RuntimeRejected("pre-runtime absence cannot be node signed") from exc
            replacement_request = (
                self._new_launch_authorization_request(
                    state=state,
                    pre_runtime_absence_receipt=receipt,
                )
                if launch_allowed
                and reservation is not None
                and self._clock.now() < reservation.lease_expires_at
                and self._clock.now() < reservation.hard_deadline
                and state.phase
                in {
                    AttemptPhase.PREPARED,
                    AttemptPhase.START_AUTHORIZED,
                    AttemptPhase.LAUNCH_COMMITTED,
                    AttemptPhase.RECONCILIATION_REQUIRED,
                }
                and (
                    (
                        not cleaned_launch
                        and state.runtime_launch_authorization_request is None
                        and state.runtime_launch_authorization is None
                    )
                    or (
                        cleaned_launch
                        and state.runtime_launch_authorization_request is not None
                        and state.runtime_launch_authorization is not None
                    )
                )
                else None
            )
            # Preserve the exact proof/request before the allocator transaction can commit.
            self._state.save_pre_runtime_absence_request(
                attempt_id=state.attempt_id,
                receipt=receipt,
                replacement_request=replacement_request,
                generation=1,
            )
            pending_generation = _PendingPreRuntimeAbsenceGeneration(
                generation=1,
                receipt=receipt,
                replacement_request=replacement_request,
                supersedes_absence_receipt_sha256=None,
            )
        else:
            receipt = pending_generation.receipt
            replacement_request = pending_generation.replacement_request
            evidence = receipt.absence_evidence
            prior_request = state.runtime_launch_authorization_request
            prior_authorization = state.runtime_launch_authorization
            if (
                receipt.preparation != state.runtime_preparation
                or (
                    not cleaned_launch
                    and (prior_request is not None or prior_authorization is not None)
                )
                or (
                    cleaned_launch
                    and (
                        prior_request is None
                        or prior_authorization is None
                        or evidence.prelaunch_authorization_request_sha256
                        != prior_request.request_sha256
                        or evidence.prelaunch_authorization_sha256
                        != prior_authorization.authorization_sha256
                    )
                )
            ):
                raise LocalStateError(
                    "pending pre-runtime absence differs from durable launch lineage"
                )

        if reservation is None or token is None:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_preparation=state.runtime_preparation,
                pre_runtime_absence_receipt=receipt,
            )
        try:
            try:
                decision = self._allocator.resolve_pre_runtime_absence(
                    attempt_id=state.attempt_id,
                    lease_token=token,
                    fencing_epoch=state.fencing_epoch,
                    runtime_preparation=state.runtime_preparation,
                    absence_receipt=receipt,
                    replacement_launch_authorization_request=replacement_request,
                )
            except NodeLeaseRejected:
                state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
                self._state.save_state(state)
                return NodeRunResult(
                    outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                    attempt_id=state.attempt_id,
                    runtime_preparation=state.runtime_preparation,
                    pre_runtime_absence_receipt=receipt,
                )
        except NodeProofReplayRejected as exc:
            if exc.code is not NodeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED:
                raise
            refreshed_observation = (
                self._cleanup_never_started(request=request, state=state)
                if cleaned_launch
                else self._validated_inspection(request=request, state=state)
            )
            try:
                validate_pre_runtime_absence_evidence_refresh(
                    previous=receipt.absence_evidence,
                    refreshed=refreshed_observation,
                )
            except QualificationVerificationError as refresh_error:
                raise RuntimeRejected(
                    "fresh absence inspection changed its exact tombstone lineage"
                ) from refresh_error
            prior_authorization_request = (
                state.runtime_launch_authorization_request if cleaned_launch else None
            )
            prior_authorization = state.runtime_launch_authorization if cleaned_launch else None
            signed_at = self._clock.now()
            expires_at = min(
                refreshed_observation.inspected_at + self._inspection_ttl,
                self._node_authority.active_until,
            )
            try:
                refreshed_receipt = issue_pre_runtime_absence_receipt(
                    manifest=self._node_authority.manifest,
                    preparation=state.runtime_preparation,
                    absence_evidence=refreshed_observation,
                    signed_at=signed_at,
                    expires_at=expires_at,
                    private_key=self._private_key,
                    launch_authorization_request=prior_authorization_request,
                    launch_authorization=prior_authorization,
                    runtime_authority=(self._runtime_control_authority if cleaned_launch else None),
                )
            except QualificationVerificationError as refresh_error:
                raise RuntimeRejected("refreshed pre-runtime absence cannot be node signed") from (
                    refresh_error
                )
            refreshed_request = (
                self._new_launch_authorization_request(
                    state=state,
                    pre_runtime_absence_receipt=refreshed_receipt,
                )
                if launch_allowed
                and self._clock.now() < reservation.lease_expires_at
                and self._clock.now() < reservation.hard_deadline
                and replacement_request is not None
                else None
            )
            self._state.save_pre_runtime_absence_request(
                attempt_id=state.attempt_id,
                receipt=refreshed_receipt,
                replacement_request=refreshed_request,
                generation=pending_generation.generation + 1,
                supersedes_absence_receipt_sha256=receipt.absence_receipt_sha256,
            )
            receipt = refreshed_receipt
            replacement_request = refreshed_request
            try:
                decision = self._allocator.resolve_pre_runtime_absence(
                    attempt_id=state.attempt_id,
                    lease_token=token,
                    fencing_epoch=state.fencing_epoch,
                    runtime_preparation=state.runtime_preparation,
                    absence_receipt=receipt,
                    replacement_launch_authorization_request=replacement_request,
                )
            except NodeLeaseRejected:
                state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
                self._state.save_state(state)
                return NodeRunResult(
                    outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                    attempt_id=state.attempt_id,
                    runtime_preparation=state.runtime_preparation,
                    pre_runtime_absence_receipt=receipt,
                )
        except NodeLeaseRejected:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_preparation=state.runtime_preparation,
                pre_runtime_absence_receipt=receipt,
            )
        if (
            not isinstance(decision, PreRuntimeAbsenceDecision)
            or decision.pre_runtime_absence_receipt_sha256 != receipt.absence_receipt_sha256
        ):
            raise RuntimeRejected("allocator absence decision changed exact proof identity")
        if decision.disposition is PreRuntimeAbsenceDisposition.REAUTHORIZED:
            if (
                replacement_request is None
                or decision.replacement_launch_authorization_request != replacement_request
                or decision.replacement_launch_authorization is None
            ):
                raise RuntimeRejected(
                    "allocator absence reauthorization changed durable replacement request"
                )
            snapshot = self._validate_allocator_response(
                decision.reservation,
                baseline=reservation,
                expected_statuses=frozenset({"starting"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=True,
                operation="resolve_pre_runtime_absence_reauthorize",
            )
            self._validate_launch_authorization(
                authorization_request=replacement_request,
                authorization=decision.replacement_launch_authorization,
                preparation=state.runtime_preparation,
                reservation=snapshot,
            )
            state = replace(
                state,
                phase=AttemptPhase.START_AUTHORIZED,
                launch_committed=False,
                running_confirmed=False,
                runtime_launch_authorization_request=replacement_request,
                runtime_launch_authorization=decision.replacement_launch_authorization,
            )
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.PRE_RUNTIME_REAUTHORIZED,
                attempt_id=state.attempt_id,
                runtime_preparation=state.runtime_preparation,
                pre_runtime_absence_receipt=receipt,
            )
        if (
            decision.replacement_launch_authorization_request is not None
            or decision.replacement_launch_authorization is not None
        ):
            raise RuntimeRejected("released pre-runtime absence retained launch authority")
        self._validate_allocator_response(
            decision.reservation,
            baseline=reservation,
            expected_statuses=frozenset({"cancelled"}),
            expected_fencing_epoch=state.fencing_epoch,
            expected_lease_token_sha256=state.lease_token_sha256,
            require_live_authority=False,
            operation="resolve_pre_runtime_absence_release",
        )
        state = replace(
            state,
            phase=AttemptPhase.PRE_RUNTIME_RELEASED,
            launch_committed=False,
            running_confirmed=False,
        )
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.PRE_RUNTIME_RELEASED,
            attempt_id=state.attempt_id,
            runtime_preparation=state.runtime_preparation,
            pre_runtime_absence_receipt=receipt,
        )

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
                runtime_preparation=state.runtime_preparation,
                runtime_identity=state.runtime_identity,
                node_runtime_launch_receipt=state.node_runtime_launch_receipt,
            )
        state = replace(state, phase=AttemptPhase.RUNNING)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.RUNNING,
            attempt_id=reservation.attempt_id,
            runtime_preparation=state.runtime_preparation,
            runtime_identity=state.runtime_identity,
            node_runtime_launch_receipt=state.node_runtime_launch_receipt,
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
            runtime_preparation=state.runtime_preparation,
            runtime_identity=state.runtime_identity,
            node_runtime_launch_receipt=state.node_runtime_launch_receipt,
        )

    def _retain_reconciliation(
        self,
        *,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        request: RuntimeLaunchRequest,
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
            runtime_preparation=state.runtime_preparation,
            runtime_identity=state.runtime_identity,
            node_runtime_launch_receipt=state.node_runtime_launch_receipt,
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
        launch_allowed: bool,
    ) -> NodeRunResult:
        if observation.state is not RuntimeInspectionState.RUNNING:
            if observation.state is RuntimeInspectionState.TERMINATED and (state.launch_committed):
                return self._collect_stopped(
                    intent=intent,
                    spec=request.spec,
                    reservation=reservation,
                    state=state,
                    token=token,
                    request=request,
                    observation=observation,
                    output_root=request.output_root,
                )
            if observation.state is RuntimeInspectionState.ABSENT:
                return self._pre_runtime_absence_result(
                    state=state,
                    request=request,
                    observation=observation,
                    reservation=reservation,
                    token=token,
                    launch_allowed=launch_allowed,
                )
            if observation.runtime_identity is None:
                return self._local_reconciliation(
                    state=state, reason="recovery_unknown_has_no_runtime_identity"
                )
            return self._retain_reconciliation(
                reservation=reservation,
                state=state,
                token=token,
                observation=observation,
                reason="recovery_runtime_unknown",
            )
        if (
            not state.launch_committed
            or state.runtime_identity is None
            or state.node_runtime_launch_receipt is None
        ):
            return self._local_reconciliation(
                state=state,
                reason="recovery_running_has_no_allocator_start_lineage",
            )
        if self._clock.now() >= reservation.hard_deadline:
            return self._local_reconciliation(
                state=state,
                reason="hard_deadline_forbids_running_runtime_adoption",
            )
        if state.runtime_control_journal_sha256 is None:
            state = replace(
                state,
                runtime_control_journal_sha256=(observation.runtime_control_journal_sha256),
            )
            self._state.save_state(state)
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
        runtime_rebind_request, runtime_rebind_receipt = self._ensure_runtime_rebind(
            state=state,
            new_fencing_epoch=new_fence,
            new_lease_token_sha256=new_token_sha256,
        )
        try:
            snapshot = self._allocator.adopt_attempt(
                receipt=adoption,
                previous_lease_token=token,
                previous_fencing_epoch=state.fencing_epoch,
                new_lease_token=new_token,
                runtime_fence_rebind_request=runtime_rebind_request,
                runtime_fence_rebind_receipt=runtime_rebind_receipt,
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
                runtime_fence_rebind_receipt=runtime_rebind_receipt,
            )
        return self._apply_adoption_snapshot(
            snapshot=snapshot,
            baseline=reservation,
            state=state,
            receipt=adoption,
            runtime_rebind_receipt=runtime_rebind_receipt,
            operation="adopt_attempt",
        )

    def _collect_stopped_legacy(
        self,
        *,
        intent: ExecutionIntent,
        spec: PinnedLaunchSpec,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        request: RuntimeLaunchRequest,
        observation: RuntimeObservation,
        output_root: Path,
        persisted_inspection: RuntimeInspectionReceipt | None = None,
    ) -> NodeRunResult:
        if (
            not state.launch_committed
            or state.runtime_identity is None
            or state.node_runtime_launch_receipt is None
            or observation.state is not RuntimeInspectionState.TERMINATED
        ):
            return self._local_reconciliation(
                state=state,
                reason="stopped_runtime_lacks_actual_launch_and_terminal_journal_lineage",
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
                            node_runtime_launch_receipt=(state.node_runtime_launch_receipt),
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
        assert observation.exit_code is not None
        assert observation.ended_at is not None
        assert observation.ended_monotonic_ns is not None
        exit_code = observation.exit_code
        ended_at = observation.ended_at
        ended_monotonic_ns = observation.ended_monotonic_ns
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

    def _issue_terminal_proof_generation(
        self,
        *,
        state: _AttemptState,
        reservation: NodeReservation,
        token: str,
        launch_receipt: NodeRuntimeLaunchReceipt,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
        launch_authorization: RuntimeLaunchAuthorization,
        observation: RuntimeObservation,
        inspection_sequence: int,
        replay_existing_evidence: bool,
    ) -> tuple[RuntimeTerminationAcceptanceChallenge, NodeRuntimeTerminationReceipt]:
        if inspection_sequence != state.inspection_sequence:
            raise LocalStateError(
                "terminal proof generation differs from durable node anti-rollback state"
            )
        if not replay_existing_evidence:
            self._validate_observation_freshness(observation)
        self._state.save_runtime_termination_evidence(
            attempt_id=state.attempt_id,
            inspection_sequence=inspection_sequence,
            evidence=observation,
        )
        challenge = self._allocator.challenge_runtime_termination(
            attempt_id=state.attempt_id,
            lease_token=token,
            fencing_epoch=state.fencing_epoch,
            runtime_preparation=state.runtime_preparation,
            node_runtime_launch_receipt=launch_receipt,
            termination_evidence=observation,
            inspection_sequence=inspection_sequence,
            artifact_submission_deadline=(
                reservation.hard_deadline + self._artifact_completion_grace
            ),
        )
        # An exact replay is attempted before freshness is consulted so a DB-committed challenge
        # can return its decision or the one typed expired-unaccepted outcome.
        if replay_existing_evidence:
            self._validate_observation_freshness(observation)
        try:
            verify_runtime_termination_acceptance_challenge(
                challenge=challenge,
                authority=self._runtime_control_authority,
                observed_at=self._clock.now(),
            )
        except QualificationVerificationError as exc:
            raise RuntimeRejected(
                "allocator termination challenge is unauthenticated or stale"
            ) from exc
        if (
            challenge.attempt_id != state.attempt_id
            or challenge.execution_id != state.execution_id
            or challenge.intent_sha256 != state.intent_sha256
            or challenge.node_manifest_sha256 != self._node_authority.manifest.manifest_sha256
            or challenge.runtime_preparation_sha256 != state.runtime_preparation.preparation_sha256
            or challenge.node_runtime_launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
            or state.runtime_identity is None
            or challenge.runtime_identity_sha256 != state.runtime_identity.runtime_identity_sha256
            or challenge.runtime_inspection_evidence_sha256 != observation.inspection_sha256
            or challenge.inspection_sequence != inspection_sequence
            or challenge.node_inventory_sha256 != reservation.node_inventory_sha256
            or challenge.resource_lease_sha256 != reservation.resource_lease_sha256
            or challenge.fencing_epoch != state.fencing_epoch
            or challenge.lease_token_sha256 != state.lease_token_sha256
            or challenge.hard_deadline != reservation.hard_deadline
            or challenge.artifact_submission_deadline
            != reservation.hard_deadline + self._artifact_completion_grace
        ):
            raise RuntimeRejected("allocator termination challenge changed exact runtime authority")
        signed_at = self._clock.now()
        proof_expires_at = min(
            challenge.expires_at,
            observation.inspected_at + self._inspection_ttl,
            self._node_authority.active_until,
        )
        try:
            node_termination_receipt = issue_node_runtime_termination_receipt(
                challenge=challenge,
                preparation=state.runtime_preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=launch_authorization_request,
                launch_authorization=launch_authorization,
                termination_evidence=observation,
                node_authority=self._node_authority,
                runtime_authority=self._runtime_control_authority,
                signed_at=signed_at,
                expires_at=proof_expires_at,
                private_key=self._private_key,
            )
        except QualificationVerificationError as exc:
            raise RuntimeRejected(
                "full runtime termination evidence cannot be node signed"
            ) from exc
        self._state.save_runtime_termination_proof(
            attempt_id=state.attempt_id,
            challenge=challenge,
            receipt=node_termination_receipt,
        )
        return challenge, node_termination_receipt

    def _collect_stopped(
        self,
        *,
        intent: ExecutionIntent,
        spec: PinnedLaunchSpec,
        reservation: NodeReservation,
        state: _AttemptState,
        token: str,
        request: RuntimeLaunchRequest,
        observation: RuntimeObservation,
        output_root: Path,
        persisted_terminal_proof: (
            tuple[RuntimeTerminationAcceptanceChallenge, NodeRuntimeTerminationReceipt] | None
        ) = None,
        accepted_bundle: (
            tuple[
                RuntimeTerminationAcceptanceChallenge,
                NodeRuntimeTerminationReceipt,
                AcceptedRuntimeTermination,
            ]
            | None
        ) = None,
        pending_inspection_sequence: int | None = None,
        historical_recovery_grant: HistoricalRuntimeRecoveryGrant | None = None,
    ) -> NodeRunResult:
        """Accept fresh full terminal evidence before any potentially long quarantine."""

        launch_receipt = state.node_runtime_launch_receipt
        launch_authorization_request = state.runtime_launch_authorization_request
        launch_authorization = state.runtime_launch_authorization
        if (
            not state.launch_committed
            or state.runtime_identity is None
            or launch_receipt is None
            or launch_authorization_request is None
            or launch_authorization is None
            or observation.state is not RuntimeInspectionState.TERMINATED
            or observation.runtime_identity != state.runtime_identity
        ):
            return self._local_reconciliation(
                state=state,
                reason="stopped_runtime_lacks_actual_launch_and_terminal_journal_lineage",
            )
        if (
            accepted_bundle is None
            and historical_recovery_grant is not None
            and historical_recovery_grant.accepted_runtime_termination_sha256 is not None
        ):
            if persisted_terminal_proof is None:
                raise LocalStateError(
                    "accepted historical recovery lost its complete local terminal proof"
                )
            challenge, node_termination_receipt = persisted_terminal_proof
            expected_accepted_sha256 = historical_recovery_grant.accepted_runtime_termination_sha256
            accepted = self._allocator.replay_accepted_runtime_termination(
                recovery_grant=historical_recovery_grant,
                challenge=challenge,
                node_runtime_termination_receipt=node_termination_receipt,
                expected_accepted_runtime_termination_sha256=expected_accepted_sha256,
            )
            if (
                accepted.accepted_termination_sha256 != expected_accepted_sha256
                or historical_recovery_grant.recovery_expires_at
                != accepted.artifact_submission_deadline
            ):
                raise RuntimeRejected(
                    "replayed runtime termination acceptance differs from recovery grant"
                )
            try:
                verify_accepted_runtime_termination(
                    accepted=accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=state.runtime_preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=launch_authorization_request,
                    launch_authorization=launch_authorization,
                    node_authority=self._node_authority,
                    runtime_authority=self._runtime_control_authority,
                )
            except QualificationVerificationError as exc:
                raise RuntimeRejected(
                    "replayed runtime termination acceptance is unauthenticated"
                ) from exc
            self._state.save_accepted_runtime_termination(
                attempt_id=state.attempt_id,
                challenge=challenge,
                receipt=node_termination_receipt,
                accepted=accepted,
            )
            accepted_bundle = (challenge, node_termination_receipt, accepted)

        if not state.running_confirmed:
            if reservation.status not in {
                "starting",
                "running",
                "reconciliation_required",
                "terminated",
                "verifying",
            }:
                return self._local_reconciliation(
                    state=state,
                    reason="terminal_runtime_was_not_allocator_start_bound",
                )
            if reservation.status == "starting":
                try:
                    snapshot = self._allocator.mark_running(
                        attempt_id=reservation.attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                        node_runtime_launch_receipt=launch_receipt,
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
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="terminal_runtime_running_transition_lost_authority",
                    )
            state = replace(state, phase=AttemptPhase.RUNNING, running_confirmed=True)
            self._state.save_state(state)

        if accepted_bundle is not None:
            challenge, node_termination_receipt, accepted = accepted_bundle
            if accepted.inspection_sequence != state.inspection_sequence:
                raise LocalStateError(
                    "accepted terminal sequence differs from node anti-rollback state"
                )
        else:
            accepted = None
            refresh_previous: RuntimeInspectionEvidence | None = None
            if persisted_terminal_proof is not None:
                challenge, node_termination_receipt = persisted_terminal_proof
                if node_termination_receipt.inspection_sequence != state.inspection_sequence:
                    raise LocalStateError(
                        "persisted terminal proof sequence differs from node anti-rollback state"
                    )
                try:
                    accepted = self._allocator.accept_runtime_termination(
                        attempt_id=state.attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                        challenge=challenge,
                        node_runtime_termination_receipt=node_termination_receipt,
                    )
                except NodeProofReplayRejected as exc:
                    if (
                        exc.code
                        is not NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                    ):
                        raise
                    refresh_previous = node_termination_receipt.termination_evidence
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="runtime termination acceptance lost exact fence authority",
                    )
            else:
                replay_existing_evidence = pending_inspection_sequence is not None
                if pending_inspection_sequence is None:
                    pending_inspection_sequence = state.inspection_sequence + 1
                    state = replace(state, inspection_sequence=pending_inspection_sequence)
                    self._state.save_state(state)
                elif pending_inspection_sequence != state.inspection_sequence:
                    raise LocalStateError(
                        "pending terminal sequence differs from durable node anti-rollback state"
                    )
                try:
                    challenge, node_termination_receipt = self._issue_terminal_proof_generation(
                        state=state,
                        reservation=reservation,
                        token=token,
                        launch_receipt=launch_receipt,
                        launch_authorization_request=launch_authorization_request,
                        launch_authorization=launch_authorization,
                        observation=observation,
                        inspection_sequence=pending_inspection_sequence,
                        replay_existing_evidence=replay_existing_evidence,
                    )
                except NodeProofReplayRejected as exc:
                    if (
                        exc.code
                        is not NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                    ):
                        raise
                    refresh_previous = observation
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="runtime termination challenge lost exact fence authority",
                    )

            if refresh_previous is not None:
                refreshed_observation = self._validated_inspection(request=request, state=state)
                try:
                    validate_runtime_terminal_evidence_refresh(
                        previous=refresh_previous,
                        refreshed=refreshed_observation,
                    )
                except QualificationVerificationError as exc:
                    raise RuntimeRejected(
                        "fresh terminal inspection changed exact engine terminal facts"
                    ) from exc
                observation = refreshed_observation
                refreshed_sequence = state.inspection_sequence + 1
                state = replace(state, inspection_sequence=refreshed_sequence)
                self._state.save_state(state)
                try:
                    challenge, node_termination_receipt = self._issue_terminal_proof_generation(
                        state=state,
                        reservation=reservation,
                        token=token,
                        launch_receipt=launch_receipt,
                        launch_authorization_request=launch_authorization_request,
                        launch_authorization=launch_authorization,
                        observation=observation,
                        inspection_sequence=refreshed_sequence,
                        replay_existing_evidence=False,
                    )
                except NodeProofReplayRejected as exc:
                    raise RuntimeRejected(
                        "fresh terminal proof generation was rejected without replay authority"
                    ) from exc
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="refreshed termination challenge lost exact fence authority",
                    )

            if accepted is None:
                try:
                    accepted = self._allocator.accept_runtime_termination(
                        attempt_id=state.attempt_id,
                        lease_token=token,
                        fencing_epoch=state.fencing_epoch,
                        challenge=challenge,
                        node_runtime_termination_receipt=node_termination_receipt,
                    )
                except NodeLeaseRejected:
                    return self._local_reconciliation(
                        state=state,
                        reason="runtime termination acceptance lost exact fence authority",
                    )
            try:
                verify_accepted_runtime_termination(
                    accepted=accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=state.runtime_preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=launch_authorization_request,
                    launch_authorization=launch_authorization,
                    node_authority=self._node_authority,
                    runtime_authority=self._runtime_control_authority,
                )
            except QualificationVerificationError as exc:
                raise RuntimeRejected(
                    "allocator returned unauthenticated runtime termination acceptance"
                ) from exc
            self._state.save_accepted_runtime_termination(
                attempt_id=state.attempt_id,
                challenge=challenge,
                receipt=node_termination_receipt,
                accepted=accepted,
            )

        try:
            verify_accepted_runtime_termination(
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_termination_receipt,
                preparation=state.runtime_preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=launch_authorization_request,
                launch_authorization=launch_authorization,
                node_authority=self._node_authority,
                runtime_authority=self._runtime_control_authority,
            )
        except QualificationVerificationError as exc:
            raise LocalStateError(
                "durable accepted termination failed historical verification"
            ) from exc
        state = replace(state, phase=AttemptPhase.TERMINATED)
        self._state.save_state(state)

        terminal_result = self._state.load_terminal_submission_result(
            attempt_id=state.attempt_id,
            accepted=accepted,
        )
        if terminal_result is None:
            assert observation.exit_code is not None
            assert observation.ended_at is not None
            exit_code = observation.exit_code
            ended_at = observation.ended_at
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
            try:
                manifest = self._artifact_quarantine.quarantine_outputs(
                    intent=intent,
                    output_root=output_root,
                    artifact_paths=existing,
                    produced_at=ended_at,
                    allow_partial=(disposition is not NodeTerminalDisposition.PROCESS_SUCCEEDED),
                )
            except Exception as exc:
                raise OutputCollectionRejected(
                    "stopped runtime output did not match the exact declared tree"
                ) from exc
            try:
                artifact_verified_receipts = tuple(
                    self._artifact_quarantine.verify_manifest(
                        intent=intent,
                        manifest=manifest,
                    )
                )
                # Artifact-receipt host time is evidence metadata, not node authorization time.
                submitted_at = self._clock.now()
                submission = issue_qualification_terminal_submission(
                    node_authority=self._node_authority,
                    runtime_authority=self._runtime_control_authority,
                    private_key=self._private_key,
                    intent=intent,
                    accepted=accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=state.runtime_preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=launch_authorization_request,
                    launch_authorization=launch_authorization,
                    node_inventory_sha256=reservation.node_inventory_sha256,
                    resource_lease_sha256=reservation.resource_lease_sha256,
                    artifact_manifest=manifest,
                    artifact_verified_receipts=artifact_verified_receipts,
                    disposition=disposition.value,
                    submitted_at=submitted_at,
                )
            except (QualificationVerificationError, TypeError, ValueError) as exc:
                raise OutputCollectionRejected(
                    "verified terminal artifacts cannot be bound to enrolled node authority"
                ) from exc
            self._state.save_terminal_submission_result(
                attempt_id=state.attempt_id,
                accepted=accepted,
                manifest=manifest,
                artifact_verified_receipts=artifact_verified_receipts,
                submission=submission,
                disposition=disposition,
            )
        else:
            manifest, submission, disposition = terminal_result
            try:
                artifact_verified_receipts = tuple(
                    self._artifact_quarantine.verify_manifest(
                        intent=intent,
                        manifest=manifest,
                    )
                )
            except Exception as exc:
                raise OutputCollectionRejected(
                    "durable terminal artifacts failed fresh independent rehash"
                ) from exc

        try:
            verify_qualification_terminal_submission(
                submission=submission,
                intent=intent,
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_termination_receipt,
                preparation=state.runtime_preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=launch_authorization_request,
                launch_authorization=launch_authorization,
                artifact_manifest=manifest,
                artifact_verified_receipts=artifact_verified_receipts,
                expected_node_inventory_sha256=reservation.node_inventory_sha256,
                expected_resource_lease_sha256=reservation.resource_lease_sha256,
                node_authority=self._node_authority,
                runtime_authority=self._runtime_control_authority,
                verified_at=max(self._clock.now(), submission.submitted_at),
            )
        except QualificationVerificationError as exc:
            raise OutputCollectionRejected(
                "terminal submission failed historical proof/artifact verification"
            ) from exc

        try:
            terminal_commit = self._allocator.submit_terminal_artifacts(
                accepted_termination=accepted,
                terminal_submission=submission,
                artifact_manifest=manifest,
                artifact_verified_receipts=artifact_verified_receipts,
                disposition=disposition,
            )
            if not isinstance(terminal_commit, TerminalArtifactCommit):
                raise RuntimeRejected(
                    "allocator terminal submission omitted signed final acceptance"
                )
            verify_accepted_qualification_terminal_submission(
                terminal_acceptance=terminal_commit.terminal_acceptance,
                submission=submission,
                intent=intent,
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_termination_receipt,
                preparation=state.runtime_preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=launch_authorization_request,
                launch_authorization=launch_authorization,
                artifact_manifest=manifest,
                artifact_verified_receipts=artifact_verified_receipts,
                expected_node_inventory_sha256=reservation.node_inventory_sha256,
                expected_resource_lease_sha256=reservation.resource_lease_sha256,
                node_authority=self._node_authority,
                runtime_authority=self._runtime_control_authority,
            )
            self._validate_allocator_response(
                terminal_commit.reservation,
                baseline=reservation,
                expected_statuses=frozenset({"verifying"}),
                expected_fencing_epoch=state.fencing_epoch,
                expected_lease_token_sha256=state.lease_token_sha256,
                require_live_authority=False,
                operation="submit_terminal_artifacts",
            )
        except QualificationVerificationError as exc:
            raise RuntimeRejected("allocator final terminal acceptance is unauthenticated") from exc
        except NodeLeaseRejected:
            state = replace(state, phase=AttemptPhase.RECONCILIATION_REQUIRED)
            self._state.save_state(state)
            return NodeRunResult(
                outcome=NodeRunOutcome.RECONCILIATION_REQUIRED,
                attempt_id=state.attempt_id,
                runtime_preparation=state.runtime_preparation,
                runtime_identity=state.runtime_identity,
                node_runtime_launch_receipt=launch_receipt,
                runtime_termination_challenge=challenge,
                node_runtime_termination_receipt=node_termination_receipt,
                accepted_runtime_termination=accepted,
                accepted_terminal_submission=None,
                artifact_manifest=manifest,
                artifact_verified_receipts=artifact_verified_receipts,
                terminal_submission=submission,
                terminal_disposition=disposition,
            )
        state = replace(state, phase=AttemptPhase.VERIFYING)
        self._state.save_state(state)
        return NodeRunResult(
            outcome=NodeRunOutcome.COLLECTED,
            attempt_id=state.attempt_id,
            runtime_preparation=state.runtime_preparation,
            runtime_identity=state.runtime_identity,
            node_runtime_launch_receipt=launch_receipt,
            runtime_termination_challenge=challenge,
            node_runtime_termination_receipt=node_termination_receipt,
            accepted_runtime_termination=accepted,
            accepted_terminal_submission=terminal_commit.terminal_acceptance,
            artifact_manifest=manifest,
            artifact_verified_receipts=artifact_verified_receipts,
            terminal_submission=submission,
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
    "NodeProofReplayRejected",
    "NodeProofReplayRejectionCode",
    "NodeReservation",
    "NodeRunOutcome",
    "NodeRunResult",
    "NodeTerminalDisposition",
    "OutputCollectionRejected",
    "OutputQuotaProvisionerPort",
    "PinnedArtifactPath",
    "PinnedEnvironmentVariable",
    "PinnedLaunchRegistry",
    "PinnedLaunchSpec",
    "PreRuntimeAbsenceDecision",
    "PreRuntimeAbsenceDisposition",
    "QualificationAssignment",
    "QualificationInputMaterializerPort",
    "QualificationNodeAgent",
    "QualificationRuntimePort",
    "ReservedDeviceBinding",
    "RuntimeLaunchRequest",
    "RuntimeObservation",
    "RuntimeRejected",
    "RuntimeStartAuthorization",
    "TerminalArtifactCommit",
]
