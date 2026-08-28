"""Closed production composition for the non-root qualification node process.

The guarded five-process runner authenticates this module and its canonical configuration before
calling the factory.  This module closes the remaining semantic boundary: one enrolled node, one
CPU-only OCI policy, three distinct private keys, exact PostgreSQL state, durable local custody,
and the two privileged loopback clients are assembled into one restart-safe polling worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aletheia.config import get_settings
from aletheia.db import expected_schema_revision, session_factory
from aletheia.execution.allocator import (
    LocalPricingAuthorityPin,
    PostgreSQLExecutionAllocator,
    PostgreSQLExecutionReceiptArchive,
)
from aletheia.execution.artifact_store import LocalArtifactStore
from aletheia.execution.assignment_contracts import x25519_public_key_hex
from aletheia.execution.authority_registry import (
    CompositeExecutionAuthorityResolver,
    ExactExecutionCostQuoteRegistry,
    SourceBudgetProjectionRegistry,
)
from aletheia.execution.input_materializer import LocalCASInputMaterializer
from aletheia.execution.input_resolver import LocalVerifiedInputArtifactResolver
from aletheia.execution.node_agent import (
    NodeLocalStateStore,
    PinnedLaunchRegistry,
    PinnedLaunchSpec,
    QualificationNodeAgent,
)
from aletheia.execution.oci_deployment import (
    ImmutableOCIImageLaunchGateVerifier,
    LoopbackOutputQuotaProvisionerClient,
    LoopbackQuotaProvisionerDeploymentPin,
    PinnedOCIImageLayout,
    SystemdDeadlineWatchdogController,
    SystemdWatchdogDeploymentPin,
)
from aletheia.execution.oci_runtime import (
    DeploymentPinnedOCIPolicy,
    LocalQualificationOCIRuntime,
    host_parent_chain_sha256,
)
from aletheia.execution.postgresql_node_adapter import (
    PostgreSQLNodeAllocatorAdapter,
    QualificationExecutionWorker,
)
from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceHandlerSet,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.runtime_contracts import (
    NetworkPolicy,
    NodeEnrollmentAuthorityVerifier,
    QualificationAuthorityVerifier,
    TerminalVerificationAuthorityVerifier,
    WorkerNodeAuthorityVerifier,
    qualification_key_id,
)
from aletheia.execution.runtime_control_issuance import PinnedRuntimeControlIssuanceAuthority
from aletheia.execution.runtime_v2_contracts import RuntimeControlAuthorityPin
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes
from aletheia.execution.terminal_runtime import TerminalNodeAuthorityConfig

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_BOOT_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class QualificationNodeCompositionError(RuntimeError):
    """The node config, live custody, key material, or process binding failed closed."""


class QualificationNodePrivateKeyPinV1(ExecutionModel):
    """Exact node-owned raw private-key file; the key bytes never enter the config."""

    schema_name: Literal["aletheia.qualification_node_private_key_pin"] = (
        "aletheia.qualification_node_private_key_pin"
    )
    schema_version: Literal[1] = 1
    role: Literal["node_signing", "assignment_transport", "runtime_control"]
    algorithm: Literal["ed25519", "x25519"]
    path: str
    file_sha256: str = Field(pattern=_SHA256_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    owner_uid: int = Field(ge=1, le=2**31 - 1)
    owner_gid: int = Field(ge=1, le=2**31 - 1)
    file_mode: Literal[0o400] = 0o400
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_key_bytes: Literal[32] = 32

    @model_validator(mode="after")
    def _key_file_is_closed(self) -> "QualificationNodePrivateKeyPinV1":
        _absolute_path(self.path, label=f"{self.role} private key")
        expected_algorithm = "x25519" if self.role == "assignment_transport" else "ed25519"
        if self.algorithm != expected_algorithm:
            raise ValueError("qualification node key role uses another algorithm")
        return self


class QualificationNodeMutableRootPinV1(ExecutionModel):
    """Exact pre-provisioned 0700 node-owned root used by one mutable subsystem."""

    schema_name: Literal["aletheia.qualification_node_mutable_root_pin"] = (
        "aletheia.qualification_node_mutable_root_pin"
    )
    schema_version: Literal[1] = 1
    purpose: Literal[
        "artifact_store",
        "node_state",
        "input_materialization_journal",
        "runtime_journal",
    ]
    path: str
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=1, le=2**31 - 1)
    owner_gid: int = Field(ge=1, le=2**31 - 1)
    mode: Literal[0o700] = 0o700
    parent_chain_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _root_is_canonical(self) -> "QualificationNodeMutableRootPinV1":
        _absolute_path(self.path, label=f"{self.purpose} root")
        return self


class QualificationNodeServiceConfigV1(ExecutionModel):
    """Canonical authority, runtime, and custody closure for one node process."""

    schema_name: Literal["aletheia.qualification_node_service_config"] = (
        "aletheia.qualification_node_service_config"
    )
    schema_version: Literal[1] = 1
    deployment_id: str = Field(pattern=_IDENTITY_PATTERN)
    process_config_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_url_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    postgresql_role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    qualification_custody: QualificationPreAdmissionCustodyConfig
    runtime_control_authority_pin: RuntimeControlAuthorityPin
    node_authority: TerminalNodeAuthorityConfig
    allowed_rate_card_sha256s: tuple[str, ...] = Field(min_length=1)
    allowed_currency_codes: tuple[str, ...] = Field(min_length=1)
    allocator_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    input_materializer_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    node_signing_key: QualificationNodePrivateKeyPinV1
    assignment_transport_key: QualificationNodePrivateKeyPinV1
    runtime_control_key: QualificationNodePrivateKeyPinV1
    artifact_store_root_pin: QualificationNodeMutableRootPinV1
    node_state_root_pin: QualificationNodeMutableRootPinV1
    input_materialization_journal_root_pin: QualificationNodeMutableRootPinV1
    runtime_journal_root_pin: QualificationNodeMutableRootPinV1
    oci_policy: DeploymentPinnedOCIPolicy
    image_layout: PinnedOCIImageLayout
    quota_deployment: LoopbackQuotaProvisionerDeploymentPin
    watchdog_deployment: SystemdWatchdogDeploymentPin
    launch_specs: tuple[PinnedLaunchSpec, ...] = Field(min_length=1, max_length=1)
    max_inventory_ttl_seconds: int = Field(default=30, ge=1, le=300)
    max_runtime_inspection_ttl_seconds: int = Field(default=30, ge=1, le=60)
    heartbeat_extension_seconds: int = Field(default=15, ge=1, le=300)
    max_runtime_launch_authorization_seconds: int = Field(default=30, ge=1, le=60)
    max_runtime_proof_age_seconds: int = Field(default=30, ge=1, le=60)
    artifact_submission_grace_seconds: int = Field(default=3600, ge=1, le=86_400)
    inspection_ttl_seconds: int = Field(default=10, ge=1, le=60)
    artifact_completion_grace_seconds: int = Field(default=3600, ge=1, le=86_400)
    prepared_at: AwareDatetime
    cpu_only: Literal[True] = True
    database_credentials_loaded: Literal[True] = True
    runtime_control_signing_key_loaded: Literal[True] = True
    node_signing_key_loaded: Literal[True] = True
    assignment_transport_private_key_loaded: Literal[True] = True
    qualification_signing_key_loaded: Literal[False] = False
    terminal_signing_key_loaded: Literal[False] = False
    scientific_signing_key_loaded: Literal[False] = False
    execution_launch_allowed: Literal[True] = True
    runtime_lifecycle_mutation_allowed: Literal[True] = True
    qualification_terminal_commit_allowed: Literal[True] = True
    node_registry_mutation_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _node_authority_and_runtime_are_exact(self) -> "QualificationNodeServiceConfigV1":
        custody = self.qualification_custody
        node = self.node_authority
        manifest = node.manifest
        transport = node.assignment_transport_pin
        if (
            self.prepared_at.utcoffset() != timedelta(0)
            or custody.prepared_at != self.prepared_at
            or not self.runtime_control_authority_pin.active_at(self.prepared_at)
            or not node.enrollment_authority_pin.active_at(self.prepared_at)
            or not transport.active_at(self.prepared_at)
            or not manifest.key_valid_from
            <= self.prepared_at
            < min(manifest.key_expires_at, manifest.key_revoked_at or manifest.key_expires_at)
        ):
            raise ValueError("qualification node authority is inactive or time-rebound")
        if self.allowed_rate_card_sha256s != tuple(
            sorted(set(self.allowed_rate_card_sha256s))
        ) or any(
            re.fullmatch(_SHA256_PATTERN, value) is None for value in self.allowed_rate_card_sha256s
        ):
            raise ValueError("qualification node rate cards must be canonical SHA-256 values")
        if self.allowed_currency_codes != tuple(sorted(set(self.allowed_currency_codes))) or any(
            len(value) != 3 or not value.isalpha() or not value.isupper()
            for value in self.allowed_currency_codes
        ):
            raise ValueError("qualification node currencies must be canonical uppercase codes")
        expected_keys = (
            (self.node_signing_key, "node_signing", manifest.node_signing_key_id),
            (self.assignment_transport_key, "assignment_transport", transport.transport_key_id),
            (
                self.runtime_control_key,
                "runtime_control",
                self.runtime_control_authority_pin.key_id,
            ),
        )
        if any(pin.role != role or pin.key_id != key_id for pin, role, key_id in expected_keys):
            raise ValueError("qualification node private key differs from its authority pin")
        if (
            len({pin.path for pin, _role, _key in expected_keys}) != 3
            or len({pin.file_sha256 for pin, _role, _key in expected_keys}) != 3
        ):
            raise ValueError("qualification node private key files and bytes must be distinct")

        launch = self.launch_specs[0]
        policy = self.oci_policy
        if (
            manifest.operating_system.lower() != "linux"
            or manifest.oci_platform != policy.oci_platform
            or manifest.container_runtime != policy.runtime_engine
            or manifest.sandbox_policy_sha256 != policy.sandbox_policy_sha256
            or manifest.network_policies != (NetworkPolicy.NONE,)
            or launch.runtime_engine != policy.runtime_engine
            or launch.launch_spec_sha256 != policy.launch_spec_sha256
            or launch.command_sha256 != policy.command_sha256
            or launch.environment_sha256 != policy.environment_sha256
            or launch.capability_manifest_sha256 != policy.capability_manifest_sha256
            or launch.executable_sha256 != policy.executable_sha256
            or self.image_layout.policy_sha256 != policy.policy_sha256
        ):
            raise ValueError("qualification node launch registry differs from node or OCI policy")
        if (
            self.quota_deployment.deployment_id != f"{self.deployment_id}:quota"
            or self.watchdog_deployment.deployment_id != f"{self.deployment_id}:watchdog"
            or self.watchdog_deployment.policy_sha256 != policy.policy_sha256
            or self.watchdog_deployment.journal_root != self.runtime_journal_root_pin.path
            or self.artifact_completion_grace_seconds != self.artifact_submission_grace_seconds
        ):
            raise ValueError(
                "qualification node clients differ from deployment or allocator policy"
            )

        root_pins = (
            self.artifact_store_root_pin,
            self.node_state_root_pin,
            self.input_materialization_journal_root_pin,
            self.runtime_journal_root_pin,
        )
        expected_root_bindings = (
            ("artifact_store", custody.artifact_store_root),
            ("node_state", self.node_state_root_pin.path),
            (
                "input_materialization_journal",
                self.input_materialization_journal_root_pin.path,
            ),
            ("runtime_journal", self.watchdog_deployment.journal_root),
        )
        if tuple((item.purpose, item.path) for item in root_pins) != expected_root_bindings:
            raise ValueError("qualification node mutable roots are not an exhaustive exact binding")
        runtime_root = self.runtime_journal_root_pin
        if (
            runtime_root.device != self.watchdog_deployment.journal_root_device
            or runtime_root.inode != self.watchdog_deployment.journal_root_inode
            or runtime_root.mode != self.watchdog_deployment.journal_root_mode
            or runtime_root.parent_chain_sha256
            != self.watchdog_deployment.journal_root_parent_chain_sha256
        ):
            raise ValueError("qualification runtime journal differs from watchdog custody")

        roots = tuple(
            _absolute_path(value, label="qualification node custody root")
            for value in (
                self.artifact_store_root_pin.path,
                custody.authority_registry_root,
                self.node_state_root_pin.path,
                self.input_materialization_journal_root_pin.path,
                self.runtime_journal_root_pin.path,
                self.quota_deployment.workspace_root,
                self.quota_deployment.backing_root,
                self.quota_deployment.state_root,
                str(Path(self.quota_deployment.socket_path).parent),
                self.watchdog_deployment.state_root,
                str(Path(self.watchdog_deployment.socket_path).parent),
                self.image_layout.layout_root,
            )
        )
        if any(
            _paths_overlap(left, right)
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise ValueError("qualification node custody roots overlap")
        for key, _role, _key_id in expected_keys:
            key_path = Path(key.path)
            if any(
                key_path == root or root in key_path.parents or key_path in root.parents
                for root in roots
            ):
                raise ValueError("qualification node private key overlaps a custody root")

        principals = (
            custody.artifact_verifier_principal_id,
            custody.input_resolver_principal_id,
            custody.pricing_authority_pin.principal_id,
            custody.source_budget_authority_pin.principal_id,
            custody.qualification_authority_pin.principal_id,
            custody.terminal_verification_authority_pin.principal_id,
            self.runtime_control_authority_pin.principal_id,
            self.allocator_principal_id,
            self.input_materializer_principal_id,
            manifest.principal_id,
            node.enrollment_authority_pin.principal_id,
            transport.transport_principal_id,
            self.quota_deployment.provisioner_principal_id,
        )
        keys = (
            custody.pricing_authority_pin.key_id,
            custody.source_budget_authority_pin.key_id,
            custody.qualification_authority_pin.key_id,
            custody.terminal_verification_authority_pin.key_id,
            self.runtime_control_authority_pin.key_id,
            manifest.node_signing_key_id,
            node.enrollment_authority_pin.key_id,
            transport.transport_key_id,
        )
        if any(count > 1 for count in Counter(principals).values()) or any(
            count > 1 for count in Counter(keys).values()
        ):
            raise ValueError("qualification node authorities must use distinct principals and keys")
        return self


class QualificationNodeWorkerLoop:
    """Bounded-poll service loop; every tick is independently crash recoverable."""

    def __init__(self, worker: QualificationExecutionWorker) -> None:
        if not isinstance(worker, QualificationExecutionWorker):
            raise TypeError("qualification node loop requires the concrete execution worker")
        self._worker = worker
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, poll_milliseconds: int) -> None:
        if isinstance(poll_milliseconds, bool) or not 50 <= poll_milliseconds <= 60_000:
            raise QualificationNodeCompositionError("node poll interval is outside 50..60000 ms")
        interval_seconds = poll_milliseconds / 1000
        while not self._stop.is_set():
            self._worker.tick()
            self._stop.wait(interval_seconds)


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


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _unique_object(pairs):
    duplicates = sorted(
        key for key, count in Counter(key for key, _value in pairs).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate qualification node config keys: {duplicates}")
    return dict(pairs)


def _load_config(configuration_bytes: bytes) -> QualificationNodeServiceConfigV1:
    try:
        raw = json.loads(configuration_bytes, object_pairs_hook=_unique_object)
        config = QualificationNodeServiceConfigV1.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise QualificationNodeCompositionError("qualification node config is invalid") from exc
    if canonical_json_bytes(config) != configuration_bytes:
        raise QualificationNodeCompositionError("qualification node config is not canonical JSON")
    return config


def _bind_process(
    deployment: QualificationServiceProcessDeploymentV1,
    config: QualificationNodeServiceConfigV1,
) -> QualificationServiceProcessDeploymentV1:
    try:
        process = QualificationServiceProcessDeploymentV1.model_validate(
            deployment.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise QualificationNodeCompositionError(
            "qualification node process deployment is invalid"
        ) from exc
    keys = (config.node_signing_key, config.assignment_transport_key, config.runtime_control_key)
    roots = (
        config.artifact_store_root_pin,
        config.node_state_root_pin,
        config.input_materialization_journal_root_pin,
        config.runtime_journal_root_pin,
    )
    process_identity = (process.process_uid, process.process_gid)
    if (
        process.role is not QualificationServiceRole.NODE
        or process.operation != "run"
        or process.deployment_id != config.deployment_id
        or process.worker_poll_milliseconds is None
        or qualification_service_process_config_binding_sha256(process)
        != config.process_config_binding_sha256
        or any((item.owner_uid, item.owner_gid) != process_identity for item in keys)
        or any((item.owner_uid, item.owner_gid) != process_identity for item in roots)
        or (
            config.oci_policy.workload_uid,
            config.oci_policy.workload_gid,
        )
        != process_identity
        or (
            config.quota_deployment.allowed_client_uid,
            config.quota_deployment.allowed_client_gid,
        )
        != process_identity
        or (
            config.watchdog_deployment.allowed_client_uid,
            config.watchdog_deployment.allowed_client_gid,
        )
        != process_identity
    ):
        raise QualificationNodeCompositionError(
            "qualification node config differs from its process deployment"
        )
    code_root = Path(process.reviewed_code_root)
    config_path = Path(process.composition_config_path)
    key_paths = tuple(Path(item.path) for item in keys)
    if any(
        key_path == config_path
        or code_root == key_path
        or code_root in key_path.parents
        or key_path in code_root.parents
        for key_path in key_paths
    ) or any(key_path.parent == config_path.parent for key_path in key_paths):
        raise QualificationNodeCompositionError(
            "qualification node key custody overlaps reviewed source or configuration"
        )
    return process


def _fresh_private_key(pin: QualificationNodePrivateKeyPinV1) -> bytes:
    path = Path(pin.path)
    try:
        if path.resolve(strict=True) != path:
            raise QualificationNodeCompositionError(f"{pin.role} key path traverses a symlink")
        if host_parent_chain_sha256(path) != pin.parent_chain_sha256:
            raise QualificationNodeCompositionError(f"{pin.role} key parent chain changed")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except (OSError, ValueError) as exc:
        raise QualificationNodeCompositionError(f"{pin.role} key cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != pin.raw_key_bytes
            or before.st_uid != pin.owner_uid
            or before.st_gid != pin.owner_gid
            or stat.S_IMODE(before.st_mode) != pin.file_mode
        ):
            raise QualificationNodeCompositionError(f"{pin.role} key custody differs from pin")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, pin.raw_key_bytes + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > pin.raw_key_bytes:
                break
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        parent_after = host_parent_chain_sha256(path)
    finally:
        os.close(descriptor)
    if (
        len(payload) != pin.raw_key_bytes
        or hashlib.sha256(payload).hexdigest() != pin.file_sha256
        or parent_after != pin.parent_chain_sha256
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise QualificationNodeCompositionError(f"{pin.role} key bytes changed or differ")
    return payload


def _verify_mutable_root(pin: QualificationNodeMutableRootPinV1) -> Path:
    path = Path(pin.path)
    descriptor = -1
    try:
        if path.resolve(strict=True) != path:
            raise QualificationNodeCompositionError(f"{pin.purpose} root traverses a symlink")
        if host_parent_chain_sha256(path) != pin.parent_chain_sha256:
            raise QualificationNodeCompositionError(f"{pin.purpose} root parent chain changed")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        after = os.fstat(descriptor)
        parent_after = host_parent_chain_sha256(path)
    except (OSError, ValueError) as exc:
        raise QualificationNodeCompositionError(f"{pin.purpose} root is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )
    expected = (pin.device, pin.inode, pin.owner_uid, pin.owner_gid, pin.mode)
    if (
        not stat.S_ISDIR(before.st_mode)
        or identity != expected
        or (before.st_dev, before.st_ino, before.st_mode, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_ctime_ns)
        or parent_after != pin.parent_chain_sha256
    ):
        raise QualificationNodeCompositionError(f"{pin.purpose} root custody differs from pin")
    return path


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise QualificationNodeCompositionError("Linux boot identity is unavailable") from exc
    if _BOOT_ID_PATTERN.fullmatch(value) is None:
        raise QualificationNodeCompositionError("Linux boot identity is not canonical")
    return value


def _live_authority_time() -> datetime:
    return datetime.now(timezone.utc)


def _verify_live_database_binding(config: QualificationNodeServiceConfigV1) -> None:
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
        raise QualificationNodeCompositionError(
            "qualification node database identity is unavailable"
        ) from exc
    if tuple(observed) != (config.postgresql_role, 1, config.schema_revision):
        raise QualificationNodeCompositionError(
            "qualification node PostgreSQL role or live schema differs from deployment"
        )


def _compose_worker(config: QualificationNodeServiceConfigV1) -> QualificationExecutionWorker:
    now = _live_authority_time()
    node_config = config.node_authority
    manifest = node_config.manifest
    transport = node_config.assignment_transport_pin
    if (
        not config.runtime_control_authority_pin.active_at(now)
        or not node_config.enrollment_authority_pin.active_at(now)
        or not transport.active_at(now)
        or now >= min(manifest.key_expires_at, manifest.key_revoked_at or manifest.key_expires_at)
    ):
        raise QualificationNodeCompositionError("qualification node authority is not live")

    mutable_roots = tuple(
        _verify_mutable_root(item)
        for item in (
            config.artifact_store_root_pin,
            config.node_state_root_pin,
            config.input_materialization_journal_root_pin,
            config.runtime_journal_root_pin,
        )
    )
    artifact_root, node_state_root, input_journal_root, runtime_journal_root = mutable_roots

    node_key = _fresh_private_key(config.node_signing_key)
    transport_key = _fresh_private_key(config.assignment_transport_key)
    runtime_key = _fresh_private_key(config.runtime_control_key)
    try:
        node_public = (
            Ed25519PrivateKey.from_private_bytes(node_key)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            .hex()
        )
        runtime_public = (
            Ed25519PrivateKey.from_private_bytes(runtime_key)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            .hex()
        )
        transport_public = x25519_public_key_hex(transport_key)
    except ValueError as exc:
        raise QualificationNodeCompositionError(
            "qualification node private key is invalid"
        ) from exc
    if (
        node_public != manifest.node_signing_public_key_ed25519_hex
        or qualification_key_id(node_public) != config.node_signing_key.key_id
        or runtime_public != config.runtime_control_authority_pin.public_key_ed25519_hex
        or qualification_key_id(runtime_public) != config.runtime_control_key.key_id
        or transport_public != transport.public_key_x25519_hex
    ):
        raise QualificationNodeCompositionError("qualification node private key changed authority")

    custody = config.qualification_custody
    terminal_verifier = TerminalVerificationAuthorityVerifier(
        custody.terminal_verification_authority_pin
    )
    receipt_archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=terminal_verifier
    )
    artifact_store = LocalArtifactStore(
        artifact_root,
        verifier_principal_id=custody.artifact_verifier_principal_id,
        object_store_id=custody.artifact_object_store_id,
        max_object_bytes=custody.artifact_max_object_bytes,
    )
    artifact_resolver = LocalVerifiedInputArtifactResolver(
        artifact_store=artifact_store,
        terminal_receipt_archive=receipt_archive,
        resolver_principal_id=custody.input_resolver_principal_id,
    )
    quote_registry = ExactExecutionCostQuoteRegistry(
        Path(custody.authority_registry_root),
        filesystem_pin=custody.authority_registry_filesystem_pin,
        pricing_authority_pin=custody.pricing_authority_pin,
    )
    budget_registry = SourceBudgetProjectionRegistry(
        Path(custody.authority_registry_root),
        filesystem_pin=custody.authority_registry_filesystem_pin,
        source_budget_authority_pin=custody.source_budget_authority_pin,
    )
    authority_resolver = CompositeExecutionAuthorityResolver(
        quote_registry=quote_registry,
        budget_registry=budget_registry,
        execution_receipt_resolver=receipt_archive,
    )
    node_authority = WorkerNodeAuthorityVerifier(
        manifest=manifest,
        enrollment=node_config.enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(node_config.enrollment_authority_pin),
        expected_manifest_sha256=manifest.manifest_sha256,
        observed_at=now,
    )
    qualification_authority = QualificationAuthorityVerifier(custody.qualification_authority_pin)
    runtime_issuer = PinnedRuntimeControlIssuanceAuthority(
        pin=config.runtime_control_authority_pin,
        private_key=runtime_key,
    )
    allocator = PostgreSQLExecutionAllocator(
        authority=qualification_authority,
        artifact_resolver=artifact_resolver,
        execution_authority_resolver=authority_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({custody.pricing_authority_pin.principal_id}),
            rate_card_sha256s=frozenset(config.allowed_rate_card_sha256s),
            pricing_policy_sha256s=frozenset({custody.pricing_authority_pin.policy_sha256}),
            currency_codes=frozenset(config.allowed_currency_codes),
        ),
        node_authorities=(node_authority,),
        node_assignment_transport_pins=(transport,),
        terminal_verification_authority=terminal_verifier,
        allocator_principal_id=config.allocator_principal_id,
        runtime_control_issuer=runtime_issuer,
        sessions=session_factory(),
        max_inventory_ttl_seconds=config.max_inventory_ttl_seconds,
        max_runtime_inspection_ttl_seconds=config.max_runtime_inspection_ttl_seconds,
        heartbeat_extension_seconds=config.heartbeat_extension_seconds,
        max_runtime_launch_authorization_seconds=(config.max_runtime_launch_authorization_seconds),
        max_runtime_proof_age_seconds=config.max_runtime_proof_age_seconds,
        artifact_submission_grace_seconds=config.artifact_submission_grace_seconds,
    )
    quota_controller = LoopbackOutputQuotaProvisionerClient(config.quota_deployment)
    state_store = NodeLocalStateStore(
        node_state_root,
        output_workspace_root_pin=quota_controller.output_workspace_root_pin,
    )
    node_allocator = PostgreSQLNodeAllocatorAdapter(
        allocator=allocator,
        transport_pin=transport,
        node_transport_private_key=transport_key,
        token_custody=state_store,
    )
    launch_spec = config.launch_specs[0]
    materializer = LocalCASInputMaterializer(
        artifact_store=artifact_store,
        journal_root=input_journal_root,
        path_pins=launch_spec.input_paths,
        materializer_principal_id=config.input_materializer_principal_id,
    )
    runtime = LocalQualificationOCIRuntime(
        policy=config.oci_policy,
        journal_root=runtime_journal_root,
        runtime_control_authority=runtime_issuer.authority_verifier,
        output_quota_controller=quota_controller,
        launch_gate_verifier=ImmutableOCIImageLaunchGateVerifier(
            policy=config.oci_policy,
            runtime_control_authority=config.runtime_control_authority_pin,
            image_layout=config.image_layout,
        ),
        deadline_watchdog_controller=SystemdDeadlineWatchdogController(
            policy=config.oci_policy,
            deployment=config.watchdog_deployment,
        ),
    )
    agent = QualificationNodeAgent(
        node_authority=node_authority,
        qualification_authority=qualification_authority,
        runtime_control_authority=runtime_issuer.authority_verifier,
        node_signing_private_key=node_key,
        boot_id=_current_boot_id(),
        allocator_principal_id=config.allocator_principal_id,
        allocator=node_allocator,
        runtime=runtime,
        output_quota_provisioner=LoopbackOutputQuotaProvisionerClient(config.quota_deployment),
        artifact_quarantine=artifact_store,
        launch_registry=PinnedLaunchRegistry(config.launch_specs),
        state_store=state_store,
        input_materializer=materializer,
        inspection_ttl_seconds=config.inspection_ttl_seconds,
        artifact_completion_grace_seconds=config.artifact_completion_grace_seconds,
    )
    if not allocator.runtime_control_issuance_enabled:
        raise QualificationNodeCompositionError("qualification node allocator omitted its signer")
    return QualificationExecutionWorker(
        agent=agent,
        allocator=allocator,
        runtime_control_authority=runtime_issuer.authority_verifier,
        node_id=manifest.node_id,
        node_manifest_sha256=manifest.manifest_sha256,
    )


def compose_node_service(
    *,
    deployment: QualificationServiceProcessDeploymentV1,
    configuration_bytes: bytes,
) -> QualificationServiceHandlerSet:
    """Compose the exact non-root node worker and its durable polling loop."""

    config = _load_config(configuration_bytes)
    process = _bind_process(deployment, config)
    database_url_sha256 = hashlib.sha256(get_settings().database_url.encode("utf-8")).hexdigest()
    if (
        config.database_url_sha256 != database_url_sha256
        or config.schema_revision != expected_schema_revision()
    ):
        raise QualificationNodeCompositionError(
            "qualification node database differs from deployment"
        )
    _verify_live_database_binding(config)
    loop = QualificationNodeWorkerLoop(_compose_worker(config))

    def handler(*, poll_milliseconds: int | None) -> None:
        if poll_milliseconds != process.worker_poll_milliseconds:
            raise QualificationNodeCompositionError(
                "qualification node handler received another poll interval"
            )
        assert poll_milliseconds is not None
        loop.run(poll_milliseconds=poll_milliseconds)

    return QualificationServiceHandlerSet(
        role=process.role,
        operation=process.operation,
        handler=handler,
    )


__all__ = [
    "QualificationNodeCompositionError",
    "QualificationNodeMutableRootPinV1",
    "QualificationNodePrivateKeyPinV1",
    "QualificationNodeServiceConfigV1",
    "QualificationNodeWorkerLoop",
    "compose_node_service",
]
