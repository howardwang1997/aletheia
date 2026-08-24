"""PostgreSQL authority facade for fenced local execution.

The legacy durable queue may wake a worker, but it never creates or extends authority.  Every
launch-capable result in this module is backed by one database transaction over an exact signed
qualification, a fresh signed inventory snapshot, a bigint budget hold, and fenced resource rows.

This repository intentionally has no deployable quote/source-budget authority adapter yet.  The
facade therefore requires an explicit resolver and remains a fail-closed qualification harness,
not a production launch composition.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, null, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, sessionmaker

from aletheia.db import session_factory
from aletheia.execution.persistence import (
    _ExecutionAttemptRecord,
    _ExecutionAttemptAdoptionRecord,
    _ExecutionBudgetAuthorizationRecord,
    _ExecutionBudgetEventRecord,
    _ExecutionBudgetHeadRecord,
    _ExecutionBudgetReservationRecord,
    _ExecutionDeviceHeadRecord,
    _ExecutionDeviceLeaseRecord,
    _ExecutionHeadRecord,
    _ExecutionInventoryAttestationRecord,
    _ExecutionInventoryDeviceRecord,
    _ExecutionNodeRecord,
    _ExecutionOutboxRecord,
    _ExecutionQualificationAdmissionRecord,
    _ExecutionResourceLeaseRecord,
    _ExecutionTerminalReceiptRecord,
)
from aletheia.execution.ports import (
    ArchivedExecutionTerminalReceipt,
    ExecutionAuthorityResolverPort,
    ExecutionTerminalReceiptArchivePort,
    VerifiedInputArtifactResolverPort,
)
from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReceipt,
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    NodeHealth,
    NodeInventoryAttestation,
    NodeExecutionReceipt,
    NodeEnrollmentAuthorityPin,
    NodeEnrollmentAuthorityVerifier,
    NodeRuntimeIdentity,
    QualificationAuthorityVerifier,
    TerminalVerificationAttestation,
    TerminalVerificationAuthorityPin,
    TerminalVerificationAuthorityVerifier,
    VerifiedExecutionReceiptResolution,
    VerifiedEngineeringQualification,
    WorkerNodeAuthorityVerifier,
    WorkerNodeEnrollment,
    WorkerNodeManifest,
    verify_engineering_qualification,
    verify_attempt_adoption,
    verify_node_inventory_attestation,
    verify_node_execution_receipt,
    verify_worker_node_enrollment,
)
from aletheia.execution.schemas import (
    DataLocality,
    ExecutionReceipt,
    ExecutionTerminalState,
    NetworkPolicy,
    ResourceKind,
    StaticResourceClass,
    canonical_sha256,
)

MAX_BIGINT = 9_223_372_036_854_775_807
ACTIVE_ATTEMPT_STATES = frozenset(
    {"reserved", "starting", "running", "reconciliation_required", "terminated", "verifying"}
)
TERMINAL_ATTEMPT_STATES = frozenset({"succeeded", "failed", "cancelled"})


class AllocationError(RuntimeError):
    """Base class for fail-closed local execution decisions."""


class AdmissionConflict(AllocationError):
    """An execution identity is already bound to different immutable material."""


class InventoryRejected(AllocationError):
    """A node inventory is stale, regressive, ambiguous, or structurally incompatible."""


class CapacityUnavailable(AllocationError):
    """The exact quoted placement has insufficient live unoccupied capacity."""


class BudgetUnavailable(AllocationError):
    """The exact quoted hold would exceed its immutable bigint authorization."""


class LeaseAuthorityError(AllocationError):
    """A lease token, fencing epoch, state, or runtime binding is stale or invalid."""


@dataclass(frozen=True)
class NodeRegistrationReceipt:
    node_id: str
    node_manifest_sha256: str
    node_authority_pin_sha256: str
    node_enrollment_sha256: str
    registered_at: datetime
    created: bool


@dataclass(frozen=True)
class InventoryAppendReceipt:
    node_id: str
    node_inventory_sha256: str
    boot_id: str
    sequence: int
    received_at: datetime
    valid_until: datetime
    created: bool


@dataclass(frozen=True)
class DeviceLeaseSnapshot:
    device_id: str
    hardware_uuid: str
    fencing_epoch: int
    requested_memory_bytes: int
    state: str


@dataclass(frozen=True)
class ReservationSnapshot:
    execution_id: str
    attempt_id: str
    attempt_number: int
    intent_sha256: str
    admission_sha256: str
    node_id: str
    node_inventory_sha256: str
    status: str
    state_version: int
    fencing_epoch: int
    resource_lease_sha256: str
    selected_resource_ids: tuple[str, ...]
    cpu_cores: int
    memory_bytes: int
    scratch_bytes: int
    exclusive: bool
    device_leases: tuple[DeviceLeaseSnapshot, ...]
    budget_authorization_sha256: str
    cost_quote_sha256: str
    currency_code: str
    held_microunits: int
    reserved_at: datetime
    lease_expires_at: datetime
    hard_deadline: datetime
    reconciliation_reason: str | None


@dataclass(frozen=True)
class ReservationClaim:
    snapshot: ReservationSnapshot
    created: bool
    lease_token: str | None = field(repr=False)


@dataclass(frozen=True)
class AttemptTransitionReceipt:
    snapshot: ReservationSnapshot
    replayed: bool


@dataclass(frozen=True)
class TerminalCommitReceipt:
    execution_receipt_sha256: str
    outbox_id: str
    charged_microunits: int
    snapshot: ReservationSnapshot
    replayed: bool


@dataclass(frozen=True)
class AdoptionCommitReceipt:
    adoption_receipt_sha256: str
    snapshot: ReservationSnapshot
    replayed: bool


@dataclass(frozen=True)
class LocalPricingAuthorityPin:
    """Deployment-owned quote/rate-card allowlist, independent of signed caller material."""

    quote_principal_ids: frozenset[str]
    rate_card_sha256s: frozenset[str]
    pricing_policy_sha256s: frozenset[str]
    currency_codes: frozenset[str]

    def __post_init__(self) -> None:
        if not (
            self.quote_principal_ids
            and self.rate_card_sha256s
            and self.pricing_policy_sha256s
            and self.currency_codes
        ):
            raise ValueError("pricing authority pins must be nonempty")


class PostgreSQLExecutionReceiptArchive(ExecutionTerminalReceiptArchivePort):
    """Read exact terminal receipt bytes without exposing private ORM/session authority."""

    def __init__(
        self,
        *,
        terminal_verification_authority: TerminalVerificationAuthorityVerifier,
        sessions: sessionmaker[Session] | Callable[[], Session] | None = None,
    ) -> None:
        if not isinstance(terminal_verification_authority, TerminalVerificationAuthorityVerifier):
            raise TypeError("terminal receipt archive requires deployment-pinned authority")
        self._terminal_verification_authority = TerminalVerificationAuthorityVerifier(
            terminal_verification_authority.pin
        )
        self._sessions = sessions or session_factory()

    def resolve_execution_receipt(
        self,
        *,
        execution_receipt_sha256: str,
        observed_at: datetime,
    ) -> VerifiedExecutionReceiptResolution | None:
        with self._sessions() as session:
            record = session.get(
                _ExecutionTerminalReceiptRecord,
                execution_receipt_sha256,
            )
            if record is None:
                return None
            receipt = self._validated_receipt(record)
            return VerifiedExecutionReceiptResolution(
                execution_receipt_sha256=execution_receipt_sha256,
                execution_receipt=receipt,
                committed_at=record.committed_at,
                resolved_by_principal_id=("principal:postgresql-execution-receipt-archive"),
                resolved_at=observed_at,
            )

    def _validated_receipt(
        self,
        record: _ExecutionTerminalReceiptRecord,
    ) -> ExecutionReceipt:
        execution_receipt_sha256 = record.receipt_sha256
        try:
            receipt = ExecutionReceipt.model_validate(record.payload_json)
            node_receipt = NodeExecutionReceipt.model_validate(record.node_execution_receipt_json)
            terminal_attestation = TerminalVerificationAttestation.model_validate(
                record.terminal_verification_attestation_json
            )
            terminal_pin = TerminalVerificationAuthorityPin.model_validate(
                record.terminal_verification_authority_pin_json
            )
            if terminal_pin != self._terminal_verification_authority.pin:
                raise ValueError(
                    "stored terminal trust root differs from deployment-pinned authority"
                )
            verified_terminal = self._terminal_verification_authority.verify(
                attestation=terminal_attestation,
                execution_receipt=receipt,
                node_execution_receipt=node_receipt,
                observed_at=record.committed_at,
            )
        except (TypeError, ValueError) as exc:
            raise AdmissionConflict("archived execution receipt bytes are invalid") from exc
        if (
            receipt.execution_receipt_sha256 != execution_receipt_sha256
            or record.payload_sha256 != execution_receipt_sha256
            or record.execution_id != receipt.intent.execution_id
            or record.attempt_id != receipt.intent.infrastructure_attempt.infrastructure_attempt_id
            or record.intent_sha256 != receipt.intent.intent_sha256
            or record.resource_lease_sha256 != receipt.resource_lease_sha256
            or record.terminal_state != receipt.terminal_state.value
            or record.node_execution_receipt_sha256 != node_receipt.node_execution_receipt_sha256
            or receipt.node_execution_receipt_sha256 != node_receipt.node_execution_receipt_sha256
            or record.terminal_verification_attestation_sha256
            != terminal_attestation.attestation_sha256
            or record.terminal_verification_authority_pin_sha256 != canonical_sha256(terminal_pin)
            or record.terminal_verification_policy_sha256 != terminal_pin.policy_sha256
            or record.terminal_verification_key_id != terminal_pin.key_id
            or record.committed_by_principal_id != verified_terminal.verified_by_principal_id
            or receipt.artifact_manifest is None
            or record.artifact_manifest_sha256 != receipt.artifact_manifest.manifest_sha256
            or record.artifact_manifest_json != _model_json(receipt.artifact_manifest)
            or tuple(record.artifact_verified_receipt_sha256s_json)
            != tuple(item.verified_receipt_sha256 for item in receipt.artifact_verified_receipts)
            or record.committed_at < receipt.verified_at
        ):
            raise AdmissionConflict("archived execution receipt row differs from exact bytes")
        return receipt

    def list_terminal_receipts_for_attempt(
        self, *, infrastructure_attempt_id: str
    ) -> tuple[ArchivedExecutionTerminalReceipt, ...]:
        with self._sessions() as session:
            records = tuple(
                session.execute(
                    select(_ExecutionTerminalReceiptRecord)
                    .where(_ExecutionTerminalReceiptRecord.attempt_id == infrastructure_attempt_id)
                    .order_by(_ExecutionTerminalReceiptRecord.receipt_sha256)
                ).scalars()
            )
        receipts: list[ArchivedExecutionTerminalReceipt] = []
        for record in records:
            receipt_sha256 = record.receipt_sha256
            receipt = self._validated_receipt(record)
            receipts.append(
                ArchivedExecutionTerminalReceipt(
                    receipt_sha256=receipt_sha256,
                    attempt_id=record.attempt_id,
                    execution_id=record.execution_id,
                    intent_sha256=record.intent_sha256,
                    resource_lease_sha256=record.resource_lease_sha256,
                    terminal_state=record.terminal_state,
                    payload_sha256=record.payload_sha256,
                    receipt=receipt,
                    node_execution_receipt_sha256=(record.node_execution_receipt_sha256),
                    node_execution_receipt_json=dict(record.node_execution_receipt_json),
                    terminal_verification_attestation_sha256=(
                        record.terminal_verification_attestation_sha256
                    ),
                    terminal_verification_attestation_json=dict(
                        record.terminal_verification_attestation_json
                    ),
                    terminal_verification_authority_pin_sha256=(
                        record.terminal_verification_authority_pin_sha256
                    ),
                    terminal_verification_authority_pin_json=dict(
                        record.terminal_verification_authority_pin_json
                    ),
                    terminal_verification_policy_sha256=(
                        record.terminal_verification_policy_sha256
                    ),
                    terminal_verification_key_id=(record.terminal_verification_key_id),
                    committed_by_principal_id=record.committed_by_principal_id,
                    artifact_manifest_sha256=record.artifact_manifest_sha256,
                    artifact_verified_receipt_sha256s=tuple(
                        record.artifact_verified_receipt_sha256s_json
                    ),
                    committed_at=record.committed_at,
                )
            )
        return tuple(receipts)


def _database_time(session: Session) -> datetime:
    """Read the PostgreSQL wall clock; tests may replace this function, production callers cannot."""

    return session.execute(select(func.clock_timestamp())).scalar_one()


def _model_json(model: object) -> dict[str, object]:
    dump = getattr(model, "model_dump")
    return dump(mode="json")


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}_{canonical_sha256(payload)}"


def _stable_admission_sha256(verified: VerifiedEngineeringQualification) -> str:
    """Hash immutable verified bindings while storing the observation time separately."""

    return canonical_sha256(verified.model_dump(mode="json", exclude={"verified_at"}))


def _compute_capability(value: str) -> tuple[int, int]:
    major, minor = value.split(".", 1)
    return int(major), int(minor)


class PostgreSQLExecutionAllocator:
    """The only supported writer for local execution authority records."""

    def __init__(
        self,
        *,
        authority: QualificationAuthorityVerifier,
        artifact_resolver: VerifiedInputArtifactResolverPort,
        execution_authority_resolver: ExecutionAuthorityResolverPort,
        pricing_authority: LocalPricingAuthorityPin,
        node_authorities: tuple[WorkerNodeAuthorityVerifier, ...],
        terminal_verification_authority: TerminalVerificationAuthorityVerifier,
        allocator_principal_id: str,
        sessions: sessionmaker[Session] | Callable[[], Session] | None = None,
        max_inventory_ttl_seconds: int = 30,
        max_runtime_inspection_ttl_seconds: int = 30,
        heartbeat_extension_seconds: int = 15,
    ) -> None:
        if (
            max_inventory_ttl_seconds < 1
            or max_runtime_inspection_ttl_seconds < 1
            or heartbeat_extension_seconds < 1
        ):
            raise ValueError("allocator TTLs must be positive")
        self._authority = authority
        self._artifact_resolver = artifact_resolver
        self._execution_authority_resolver = execution_authority_resolver
        self._pricing_authority = pricing_authority
        if not allocator_principal_id:
            raise ValueError("allocator principal id must be nonempty")
        self._allocator_principal_id = allocator_principal_id
        if not isinstance(terminal_verification_authority, TerminalVerificationAuthorityVerifier):
            raise TypeError("terminal verification authority must be constructor-pinned")
        self._terminal_verification_authority = TerminalVerificationAuthorityVerifier(
            terminal_verification_authority.pin
        )
        self._node_authorities = {item.manifest.node_id: item for item in node_authorities}
        if not self._node_authorities or len(self._node_authorities) != len(node_authorities):
            raise ValueError("node authorities must be nonempty and unique by node id")
        terminal_key_id = self._terminal_verification_authority.pin.key_id
        forbidden_key_ids = {
            self._authority.pin.key_id,
            *(item.manifest.node_signing_key_id for item in node_authorities),
            *(item.enrollment_authority_pin.key_id for item in node_authorities),
        }
        terminal_principal_id = self._terminal_verification_authority.pin.principal_id
        authority_principal_ids = {
            self._authority.pin.principal_id,
            *(item.manifest.principal_id for item in node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in node_authorities),
        }
        if self._allocator_principal_id in authority_principal_ids:
            raise ValueError("allocator role must be distinct from qualification and node roles")
        forbidden_principal_ids = authority_principal_ids | {self._allocator_principal_id}
        if terminal_key_id in forbidden_key_ids or terminal_principal_id in forbidden_principal_ids:
            raise ValueError(
                "terminal verification role must be distinct from qualification and node roles"
            )
        self._sessions = sessions or session_factory()
        self._max_inventory_ttl = timedelta(seconds=max_inventory_ttl_seconds)
        self._max_runtime_inspection_ttl = timedelta(seconds=max_runtime_inspection_ttl_seconds)
        self._max_runtime_inspection_age_seconds = max_runtime_inspection_ttl_seconds
        self._heartbeat_extension = timedelta(seconds=heartbeat_extension_seconds)

    def register_node(self, node_id: str) -> NodeRegistrationReceipt:
        authority = self._node_authorities.get(node_id)
        if authority is None:
            raise AdmissionConflict("node is absent from deployment-pinned authority")
        manifest = authority.manifest
        pin = authority.enrollment_authority_pin
        enrollment = authority.enrollment
        payload = _model_json(manifest)
        pin_payload = _model_json(pin)
        enrollment_payload = _model_json(enrollment)
        pin_sha256 = canonical_sha256(pin)
        with self._sessions() as session, session.begin():
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"execution-node:{manifest.node_id}", 0)
                    )
                )
            )
            record = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == manifest.node_id)
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_time(session)
            if not pin.active_at(now) or now >= authority.active_until:
                raise AdmissionConflict("deployment-pinned node authority is inactive")
            created = record is None
            if record is None:
                record = _ExecutionNodeRecord(
                    node_id=manifest.node_id,
                    node_manifest_sha256=manifest.manifest_sha256,
                    node_authority_pin_sha256=pin_sha256,
                    node_authority_pin_json=pin_payload,
                    node_enrollment_sha256=enrollment.enrollment_sha256,
                    node_enrollment_json=enrollment_payload,
                    node_principal_id=manifest.principal_id,
                    site_id=manifest.site_id,
                    manifest_json=payload,
                    boot_id=None,
                    state="active",
                    state_version=1,
                    current_inventory_sha256=None,
                    current_inventory_sequence=None,
                    reserved_cpu_cores=0,
                    reserved_memory_bytes=0,
                    reserved_scratch_bytes=0,
                    exclusive_lease_id=None,
                    registered_at=now,
                    updated_at=now,
                )
                session.add(record)
                session.flush()
            if (
                record.node_manifest_sha256 != manifest.manifest_sha256
                or record.node_authority_pin_sha256 != pin_sha256
                or record.node_authority_pin_json != pin_payload
                or record.node_enrollment_sha256 != enrollment.enrollment_sha256
                or record.node_enrollment_json != enrollment_payload
                or record.node_principal_id != manifest.principal_id
                or record.site_id != manifest.site_id
                or record.manifest_json != payload
            ):
                raise AdmissionConflict("node id is already bound to another immutable manifest")
            return NodeRegistrationReceipt(
                node_id=record.node_id,
                node_manifest_sha256=record.node_manifest_sha256,
                node_authority_pin_sha256=record.node_authority_pin_sha256,
                node_enrollment_sha256=record.node_enrollment_sha256,
                registered_at=record.registered_at,
                created=created,
            )

    def _locked_node_authority(
        self,
        node: _ExecutionNodeRecord,
        *,
        observed_at: datetime,
        error_type: type[AllocationError],
    ) -> WorkerNodeAuthorityVerifier:
        """Rebuild exact node authority from the locked row and deployment-pinned root."""

        configured = self._node_authorities.get(node.node_id)
        try:
            manifest = WorkerNodeManifest.model_validate(node.manifest_json)
            pin = NodeEnrollmentAuthorityPin.model_validate(node.node_authority_pin_json)
            enrollment = WorkerNodeEnrollment.model_validate(node.node_enrollment_json)
            if (
                configured is None
                or manifest != configured.manifest
                or enrollment != configured.enrollment
                or pin != configured.enrollment_authority_pin
                or manifest.manifest_sha256 != node.node_manifest_sha256
                or canonical_sha256(pin) != node.node_authority_pin_sha256
                or enrollment.enrollment_sha256 != node.node_enrollment_sha256
                or node.node_principal_id != manifest.principal_id
                or node.site_id != manifest.site_id
            ):
                raise ValueError("locked node authority bytes differ from deployment pins")
            return verify_worker_node_enrollment(
                manifest=manifest,
                enrollment=enrollment,
                enrollment_authority=NodeEnrollmentAuthorityVerifier(pin),
                expected_manifest_sha256=node.node_manifest_sha256,
                observed_at=observed_at,
            )
        except (TypeError, ValueError) as exc:
            raise error_type(
                "node is not bound to exact active deployment-enrolled authority"
            ) from exc

    def append_inventory(self, attestation: NodeInventoryAttestation) -> InventoryAppendReceipt:
        attestation = NodeInventoryAttestation.model_validate(attestation.model_dump(mode="python"))
        with self._sessions() as session, session.begin():
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == attestation.node_id)
                .with_for_update()
            ).scalar_one_or_none()
            if node is None:
                raise InventoryRejected("inventory node is not registered")
            now = _database_time(session)
            authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=InventoryRejected,
            )
            previous_record = None
            previous = None
            if node.current_inventory_sha256 is not None:
                previous_record = session.get(
                    _ExecutionInventoryAttestationRecord,
                    node.current_inventory_sha256,
                )
                if previous_record is None:
                    raise InventoryRejected("node current inventory pointer is incomplete")
                previous = NodeInventoryAttestation.model_validate(previous_record.payload_json)
            existing = session.get(
                _ExecutionInventoryAttestationRecord,
                attestation.inventory_sha256,
            )
            if existing is not None:
                if (
                    existing.payload_json != _model_json(attestation)
                    or node.current_inventory_sha256 != existing.inventory_sha256
                    or node.boot_id != existing.boot_id
                    or node.current_inventory_sequence != existing.sequence
                ):
                    raise InventoryRejected(
                        "inventory replay is stale or bound to different canonical bytes"
                    )
                return InventoryAppendReceipt(
                    node_id=existing.node_id,
                    node_inventory_sha256=existing.inventory_sha256,
                    boot_id=existing.boot_id,
                    sequence=existing.sequence,
                    received_at=existing.received_at,
                    valid_until=existing.valid_until,
                    created=False,
                )
            verify_node_inventory_attestation(
                attestation=attestation,
                authority=authority,
                expected_manifest_sha256=node.node_manifest_sha256,
                observed_at=now,
                previous_attestation=previous,
            )
            valid_until = min(attestation.expires_at, now + self._max_inventory_ttl)
            if (
                attestation.observed_at < now - self._max_inventory_ttl
                or attestation.expires_at - attestation.observed_at > self._max_inventory_ttl
            ):
                raise InventoryRejected(
                    "inventory age/window exceeds the deployment freshness bound"
                )
            if valid_until <= now:
                raise InventoryRejected("inventory has no deployment-bounded freshness window")
            if (
                node.boot_id is not None
                and node.boot_id != attestation.boot_id
                and (
                    node.reserved_cpu_cores
                    or node.reserved_memory_bytes
                    or node.reserved_scratch_bytes
                    or node.exclusive_lease_id is not None
                )
            ):
                raise InventoryRejected(
                    "node reboot is ambiguous while resource leases remain held"
                )
            aggregate = self._inventory_aggregate(attestation)
            if aggregate[6:9] != (
                node.reserved_cpu_cores,
                node.reserved_memory_bytes,
                node.reserved_scratch_bytes,
            ):
                raise InventoryRejected(
                    "signed managed occupancy differs from durable retained leases"
                )
            if aggregate[9] and (
                node.reserved_cpu_cores or node.reserved_memory_bytes or node.reserved_scratch_bytes
            ):
                raise InventoryRejected(
                    "external occupancy appeared while managed resource leases remain held"
                )
            payload = _model_json(attestation)
            session.add(
                _ExecutionInventoryAttestationRecord(
                    inventory_sha256=attestation.inventory_sha256,
                    node_id=attestation.node_id,
                    node_manifest_sha256=attestation.node_manifest_sha256,
                    boot_id=attestation.boot_id,
                    sequence=attestation.sequence,
                    observed_at=attestation.observed_at,
                    observed_monotonic_ns=attestation.observed_monotonic_ns,
                    received_at=now,
                    valid_until=valid_until,
                    cpu_cores=aggregate[0],
                    memory_bytes=aggregate[1],
                    scratch_bytes=aggregate[2],
                    allocatable_cpu_cores=aggregate[3],
                    allocatable_memory_bytes=aggregate[4],
                    allocatable_scratch_bytes=aggregate[5],
                    managed_cpu_cores=aggregate[6],
                    managed_memory_bytes=aggregate[7],
                    managed_scratch_bytes=aggregate[8],
                    external_occupancy=aggregate[9],
                    external_occupancy_sha256=aggregate[10],
                    resource_class_ids_json=list(aggregate[11]),
                    payload_sha256=attestation.inventory_sha256,
                    payload_json=payload,
                    attested_by_principal_id=attestation.principal_id,
                    signing_key_id=attestation.signing_key_id,
                    signature_ed25519_hex=attestation.signature_ed25519_hex,
                )
            )
            session.flush()
            self._append_inventory_devices(session, node=node, attestation=attestation, now=now)
            node.boot_id = attestation.boot_id
            node.current_inventory_sha256 = attestation.inventory_sha256
            node.current_inventory_sequence = attestation.sequence
            node.state_version += 1
            node.updated_at = now
            return InventoryAppendReceipt(
                node_id=node.node_id,
                node_inventory_sha256=attestation.inventory_sha256,
                boot_id=attestation.boot_id,
                sequence=attestation.sequence,
                received_at=now,
                valid_until=valid_until,
                created=True,
            )

    @staticmethod
    def _inventory_aggregate(
        attestation: NodeInventoryAttestation,
    ) -> tuple[
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        bool,
        str | None,
        tuple[str, ...],
    ]:
        cpu_resources = tuple(r for r in attestation.resources if r.kind is ResourceKind.CPU)
        if len(cpu_resources) != 1:
            raise InventoryRejected(
                "PR-4a inventory requires one node-aggregate CPU capacity resource"
            )
        totals = (
            sum(item.cpu_cores_total for item in cpu_resources),
            sum(item.memory_bytes_total for item in cpu_resources),
            sum(item.scratch_bytes_total for item in cpu_resources),
            sum(item.cpu_cores_allocatable for item in cpu_resources),
            sum(item.memory_bytes_allocatable for item in cpu_resources),
            sum(item.scratch_bytes_allocatable for item in cpu_resources),
        )
        managed = (
            sum(item.cpu_cores_managed_occupied for item in cpu_resources),
            sum(item.memory_bytes_managed_occupied for item in cpu_resources),
            sum(item.scratch_bytes_managed_occupied for item in cpu_resources),
        )
        if min(totals[:3]) < 1:
            raise InventoryRejected("CPU inventory totals must be positive")
        occupied = tuple(
            {
                "resource_id": item.resource_id,
                "external_process_count": item.external_process_count,
                "memory_bytes": item.memory_bytes_external_occupied,
                "scratch_bytes": item.scratch_bytes_external_occupied,
                "accelerator_memory_bytes": item.accelerator_memory_bytes_external_occupied or 0,
            }
            for item in attestation.resources
            if item.external_process_count
            or item.memory_bytes_external_occupied
            or item.scratch_bytes_external_occupied
            or (item.accelerator_memory_bytes_external_occupied or 0)
        )
        resource_classes = tuple(
            sorted(
                {class_id for item in attestation.resources for class_id in item.resource_class_ids}
            )
        )
        return (
            *totals,
            *managed,
            bool(occupied),
            canonical_sha256(occupied) if occupied else None,
            resource_classes,
        )

    def _append_inventory_devices(
        self,
        session: Session,
        *,
        node: _ExecutionNodeRecord,
        attestation: NodeInventoryAttestation,
        now: datetime,
    ) -> None:
        resources = tuple(
            item for item in attestation.resources if item.kind is ResourceKind.ACCELERATOR
        )
        active_heads = {
            item.device_id: item
            for item in session.execute(
                select(_ExecutionDeviceHeadRecord)
                .where(
                    _ExecutionDeviceHeadRecord.node_id == node.node_id,
                    _ExecutionDeviceHeadRecord.active_device_lease_id.is_not(None),
                )
                .with_for_update()
            ).scalars()
        }
        by_id = {item.resource_id: item for item in resources}
        missing = set(active_heads) - set(by_id)
        if missing:
            raise InventoryRejected(
                f"inventory removed leased accelerator resources: {sorted(missing)!r}"
            )
        for device_id, head in active_heads.items():
            item = by_id[device_id]
            if item.health is not NodeHealth.HEALTHY or item.external_process_count:
                raise InventoryRejected(
                    "leased accelerator became unhealthy or externally occupied"
                )
        for item in resources:
            if (
                item.accelerator_memory_bytes_managed_occupied
                and item.resource_id not in active_heads
            ):
                raise InventoryRejected(
                    "managed accelerator occupancy lacks a retained durable device lease"
                )
        for item in resources:
            assert item.accelerator_uuid is not None
            assert item.accelerator_model is not None
            assert item.accelerator_memory_bytes_total is not None
            assert item.accelerator_memory_bytes_safety_reserve is not None
            assert item.accelerator_memory_bytes_allocatable is not None
            assert item.accelerator_compute_capability is not None
            occupied = bool(
                item.external_process_count
                or (item.accelerator_memory_bytes_external_occupied or 0)
            )
            occupancy_sha256 = (
                canonical_sha256(
                    {
                        "resource_id": item.resource_id,
                        "external_process_count": item.external_process_count,
                        "accelerator_memory_bytes_external_occupied": (
                            item.accelerator_memory_bytes_external_occupied or 0
                        ),
                    }
                )
                if occupied
                else None
            )
            session.add(
                _ExecutionInventoryDeviceRecord(
                    inventory_sha256=attestation.inventory_sha256,
                    device_id=item.resource_id,
                    node_id=attestation.node_id,
                    hardware_uuid=item.accelerator_uuid,
                    resource_class_ids_json=list(item.resource_class_ids),
                    model=item.accelerator_model,
                    total_memory_bytes=item.accelerator_memory_bytes_total,
                    safety_reserve_bytes=item.accelerator_memory_bytes_safety_reserve,
                    managed_memory_bytes=(item.accelerator_memory_bytes_managed_occupied or 0),
                    allocatable_memory_bytes=item.accelerator_memory_bytes_allocatable,
                    compute_capability=item.accelerator_compute_capability,
                    healthy=item.health is NodeHealth.HEALTHY,
                    external_occupancy=occupied,
                    external_occupancy_sha256=occupancy_sha256,
                    features_json=list(item.features),
                )
            )
        session.flush()
        for item in resources:
            assert item.accelerator_uuid is not None
            head = session.execute(
                select(_ExecutionDeviceHeadRecord)
                .where(
                    _ExecutionDeviceHeadRecord.node_id == node.node_id,
                    _ExecutionDeviceHeadRecord.device_id == item.resource_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            hardware_head = session.execute(
                select(_ExecutionDeviceHeadRecord)
                .where(
                    _ExecutionDeviceHeadRecord.node_id == node.node_id,
                    _ExecutionDeviceHeadRecord.hardware_uuid == item.accelerator_uuid,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if head is None and hardware_head is not None:
                raise InventoryRejected("accelerator hardware UUID changed its stable resource id")
            if head is not None and head.hardware_uuid != item.accelerator_uuid:
                raise InventoryRejected("accelerator resource id changed physical hardware")
            if head is None:
                session.add(
                    _ExecutionDeviceHeadRecord(
                        node_id=node.node_id,
                        device_id=item.resource_id,
                        hardware_uuid=item.accelerator_uuid,
                        current_inventory_sha256=attestation.inventory_sha256,
                        fencing_counter=0,
                        active_device_lease_id=None,
                        state_version=1,
                        updated_at=now,
                    )
                )
            else:
                head.current_inventory_sha256 = attestation.inventory_sha256
                head.state_version += 1
                head.updated_at = now

    def admit_and_reserve(
        self,
        *,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
    ) -> ReservationClaim:
        """Reverify and atomically hold the exact quote, budget, node, and resources."""

        bundle = EngineeringQualificationBundle.model_validate(bundle.model_dump(mode="python"))
        grant = EngineeringQualificationGrant.model_validate(grant.model_dump(mode="python"))
        with self._sessions() as session, session.begin():
            preliminary_now = _database_time(session)
            self._validate_pricing_authority(bundle)
            quote = bundle.cost_quote
            authorization = bundle.budget_authorization
            intent = bundle.intent
            hold = quote.maximum_charge_microunits
            if max(hold, authorization.maximum_cost_microunits) > MAX_BIGINT:
                raise BudgetUnavailable("budget values exceed PostgreSQL signed bigint")
            head_values = dict(
                execution_id=intent.execution_id,
                quest_id=intent.quest_id,
                protocol_sha256=intent.protocol_sha256,
                work_order_id=intent.work_order_id,
                work_order_sha256=intent.work_order_sha256,
                replicate_slot_id=intent.replicate_slot.replicate_slot_id,
                replicate_slot_sha256=intent.replicate_slot.replicate_slot_sha256,
                last_attempt_number=0,
                active_attempt_id=None,
                state_version=1,
                created_at=preliminary_now,
                updated_at=preliminary_now,
            )
            session.execute(
                postgresql_insert(_ExecutionHeadRecord)
                .values(**head_values)
                .on_conflict_do_nothing(index_elements=["execution_id"])
            )
            execution_head = session.execute(
                select(_ExecutionHeadRecord)
                .where(_ExecutionHeadRecord.execution_id == intent.execution_id)
                .with_for_update()
            ).scalar_one()
            self._validate_execution_head(execution_head, bundle)
            attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
            existing_attempt = session.get(_ExecutionAttemptRecord, attempt_id)
            if existing_attempt is not None:
                self._validate_idempotent_attempt(
                    session,
                    existing_attempt,
                    bundle,
                    expected_grant_sha256=grant.grant_sha256,
                )
                return ReservationClaim(
                    snapshot=self._snapshot(session, existing_attempt),
                    created=False,
                    lease_token=None,
                )
            self._validate_retry_lineage(session, execution_head=execution_head, bundle=bundle)
            if execution_head.active_attempt_id is not None:
                raise AdmissionConflict("scientific replicate slot already has an active attempt")
            verify_engineering_qualification(
                bundle=bundle,
                grant=grant,
                authority=self._authority,
                artifact_resolver=self._artifact_resolver,
                authority_resolver=self._execution_authority_resolver,
                observed_at=preliminary_now,
            )
            authorization_values = dict(
                authorization_sha256=authorization.authorization_sha256,
                quest_id=authorization.quest_id,
                protocol_sha256=authorization.protocol_sha256,
                work_order_sha256=authorization.work_order_sha256,
                resource_budget_sha256=authorization.resource_budget_sha256,
                source_budget_authorization_sha256=(
                    authorization.source_budget_authorization_sha256
                ),
                currency_code=authorization.currency_code,
                cap_microunits=authorization.maximum_cost_microunits,
                authorized_at=authorization.authorized_at,
                expires_at=authorization.expires_at,
                authorized_by_principal_id=authorization.authorized_by_principal_id,
                payload_sha256=authorization.authorization_sha256,
                payload_json=_model_json(authorization),
                registered_at=preliminary_now,
            )
            session.execute(
                postgresql_insert(_ExecutionBudgetAuthorizationRecord)
                .values(**authorization_values)
                .on_conflict_do_nothing(index_elements=["authorization_sha256"])
            )
            stored_authorization = session.get(
                _ExecutionBudgetAuthorizationRecord,
                authorization.authorization_sha256,
            )
            if stored_authorization is None or any(
                getattr(stored_authorization, key) != value
                for key, value in authorization_values.items()
                if key != "registered_at"
            ):
                raise AdmissionConflict("budget authorization digest is rebound")
            session.execute(
                postgresql_insert(_ExecutionBudgetHeadRecord)
                .values(
                    authorization_sha256=authorization.authorization_sha256,
                    currency_code=authorization.currency_code,
                    cap_microunits=authorization.maximum_cost_microunits,
                    reserved_microunits=0,
                    spent_microunits=0,
                    state_version=1,
                    updated_at=preliminary_now,
                )
                .on_conflict_do_nothing(index_elements=["authorization_sha256"])
            )
            budget_head = session.execute(
                select(_ExecutionBudgetHeadRecord)
                .where(
                    _ExecutionBudgetHeadRecord.authorization_sha256
                    == authorization.authorization_sha256
                )
                .with_for_update()
            ).scalar_one()
            if (
                budget_head.currency_code != authorization.currency_code
                or budget_head.cap_microunits != authorization.maximum_cost_microunits
            ):
                raise AdmissionConflict("budget head differs from immutable authorization")
            if budget_head.reserved_microunits + budget_head.spent_microunits + hold > (
                budget_head.cap_microunits
            ):
                raise BudgetUnavailable("exact cost quote exceeds remaining authorized budget")
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(
                    _ExecutionNodeRecord.node_manifest_sha256 == quote.selected_node_manifest_sha256
                )
                .with_for_update()
            ).scalar_one_or_none()
            if node is None or node.state != "active":
                raise CapacityUnavailable("quoted node manifest is not registered and active")
            node_authority = self._locked_node_authority(
                node,
                observed_at=preliminary_now,
                error_type=AdmissionConflict,
            )
            manifest = node_authority.manifest
            inventory_record = (
                session.get(
                    _ExecutionInventoryAttestationRecord,
                    node.current_inventory_sha256,
                )
                if node.current_inventory_sha256 is not None
                else None
            )
            if (
                inventory_record is None
                or inventory_record.node_id != node.node_id
                or inventory_record.node_manifest_sha256 != node.node_manifest_sha256
                or inventory_record.boot_id != node.boot_id
                or inventory_record.sequence != node.current_inventory_sequence
                or not inventory_record.received_at
                <= preliminary_now
                < inventory_record.valid_until
            ):
                raise InventoryRejected("quoted node has no exact fresh locked current inventory")
            inventory = NodeInventoryAttestation.model_validate(inventory_record.payload_json)
            verify_node_inventory_attestation(
                attestation=inventory,
                authority=node_authority,
                expected_manifest_sha256=node.node_manifest_sha256,
                observed_at=preliminary_now,
            )
            device_resources = self._validate_exact_placement(
                bundle=bundle,
                manifest=manifest,
                inventory=inventory,
                node=node,
            )
            device_heads = self._lock_device_heads(
                session,
                node=node,
                inventory_sha256=inventory.inventory_sha256,
                device_ids=tuple(item.resource_id for item in device_resources),
            )
            # Every placement-relevant mutable row is locked before the authoritative clock
            # sample.  Re-resolve every external authority at that exact observation so a lock
            # wait cannot cross an expiry and still mint a lease.
            now = _database_time(session)
            verified = verify_engineering_qualification(
                bundle=bundle,
                grant=grant,
                authority=self._authority,
                artifact_resolver=self._artifact_resolver,
                authority_resolver=self._execution_authority_resolver,
                observed_at=now,
            )
            terminal_pin = self._terminal_verification_authority.pin
            if not terminal_pin.active_at(now):
                raise AdmissionConflict(
                    "terminal verification authority is not active at locked DB time"
                )
            active_until = min(
                grant.message.expires_at,
                quote.expires_at,
                authorization.expires_at,
                intent.deadline,
                self._authority.pin.active_until,
                node_authority.active_until,
                terminal_pin.active_until,
            )
            hard_deadline = now + timedelta(seconds=quote.maximum_lease_seconds)
            if hard_deadline > active_until:
                raise AdmissionConflict(
                    "full quoted lease no longer fits inside every locked authority window"
                )
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=AdmissionConflict,
            )
            if (
                inventory_record.node_id != node.node_id
                or inventory_record.node_manifest_sha256 != node.node_manifest_sha256
                or inventory_record.boot_id != node.boot_id
                or inventory_record.sequence != node.current_inventory_sequence
                or not inventory_record.received_at <= now < inventory_record.valid_until
            ):
                raise InventoryRejected(
                    "quoted node inventory expired while waiting for authority locks"
                )
            verify_node_inventory_attestation(
                attestation=inventory,
                authority=node_authority,
                expected_manifest_sha256=node.node_manifest_sha256,
                observed_at=now,
            )
            final_device_resources = self._validate_exact_placement(
                bundle=bundle,
                manifest=node_authority.manifest,
                inventory=inventory,
                node=node,
            )
            if tuple(item.resource_id for item in final_device_resources) != tuple(
                item.resource_id for item in device_resources
            ):
                raise CapacityUnavailable("locked exact device placement changed during admission")
            admission_sha256 = _stable_admission_sha256(verified)
            admission_values = dict(
                admission_sha256=admission_sha256,
                grant_sha256=grant.grant_sha256,
                bundle_sha256=bundle.bundle_sha256,
                intent_sha256=intent.intent_sha256,
                execution_id=intent.execution_id,
                infrastructure_attempt_id=attempt_id,
                budget_authorization_sha256=authorization.authorization_sha256,
                cost_quote_sha256=quote.quote_sha256,
                authority_policy_sha256=(grant.message.qualification_authority_policy_sha256),
                authority_key_id=grant.message.authorization_key_id,
                bundle_json=_model_json(bundle),
                grant_json=_model_json(grant),
                verified_receipt_json=_model_json(verified),
                verified_at=verified.verified_at,
                admitted_at=now,
            )
            session.execute(
                postgresql_insert(_ExecutionQualificationAdmissionRecord)
                .values(**admission_values)
                .on_conflict_do_nothing(index_elements=["admission_sha256"])
            )
            admission = session.get(_ExecutionQualificationAdmissionRecord, admission_sha256)
            if admission is None or any(
                getattr(admission, key) != value
                for key, value in admission_values.items()
                if key not in {"verified_at", "admitted_at"}
            ):
                raise AdmissionConflict("qualification admission digest is rebound")
            previous_fence = session.execute(
                select(func.max(_ExecutionAttemptRecord.fencing_epoch)).where(
                    _ExecutionAttemptRecord.execution_id == intent.execution_id
                )
            ).scalar_one_or_none()
            fencing_base = max(
                [previous_fence or 0, *(item.fencing_counter for item in device_heads)]
            )
            fencing_epoch = fencing_base + 1
            raw_token = secrets.token_urlsafe(32)
            token_hash = _token_sha256(raw_token)
            lease_expires_at = min(now + self._heartbeat_extension, hard_deadline)
            resource_request = intent.resource_request
            lease_payload = {
                "schema_name": "aletheia.local_resource_lease",
                "schema_version": 1,
                "execution_id": intent.execution_id,
                "attempt_id": attempt_id,
                "intent_sha256": intent.intent_sha256,
                "node_id": node.node_id,
                "node_manifest_sha256": node.node_manifest_sha256,
                "inventory_sha256": inventory.inventory_sha256,
                "selected_resource_ids": quote.selected_resource_ids,
                "fencing_epoch_at_acquisition": fencing_epoch,
                "cpu_cores": resource_request.cpu_cores,
                "memory_bytes": resource_request.memory_bytes,
                "scratch_bytes": resource_request.scratch_bytes,
                "accelerator_count": resource_request.accelerator_count,
                "exclusive": resource_request.exclusive,
                "acquired_at": now.isoformat(),
                "hard_deadline": hard_deadline.isoformat(),
            }
            lease_sha256 = canonical_sha256(lease_payload)
            lease_id = _stable_id("rle", {"attempt_id": attempt_id})
            session.add(
                _ExecutionAttemptRecord(
                    attempt_id=attempt_id,
                    execution_id=intent.execution_id,
                    attempt_number=intent.infrastructure_attempt.attempt_number,
                    intent_sha256=intent.intent_sha256,
                    intent_json=_model_json(intent),
                    admission_sha256=admission_sha256,
                    grant_sha256=grant.grant_sha256,
                    bundle_sha256=bundle.bundle_sha256,
                    cost_quote_sha256=quote.quote_sha256,
                    node_id=node.node_id,
                    node_inventory_sha256=inventory.inventory_sha256,
                    status="reserved",
                    state_version=1,
                    fencing_epoch=fencing_epoch,
                    lease_token_sha256=token_hash,
                    adoption_count=0,
                    latest_adoption_sha256=None,
                    last_runtime_inspection_sequence=0,
                    last_runtime_inspection_sha256=None,
                    last_runtime_inspected_at=None,
                    last_runtime_inspected_monotonic_ns=None,
                    authorized_at=grant.message.authorized_at,
                    reserved_at=now,
                    heartbeat_at=now,
                    lease_expires_at=lease_expires_at,
                    hard_deadline=hard_deadline,
                    reconciliation_reason=None,
                    runtime_identity_sha256=None,
                    runtime_identity_json=null(),
                    terminal_receipt_sha256=None,
                    updated_at=now,
                )
            )
            session.add(
                _ExecutionResourceLeaseRecord(
                    lease_id=lease_id,
                    attempt_id=attempt_id,
                    node_id=node.node_id,
                    inventory_sha256=inventory.inventory_sha256,
                    lease_sha256=lease_sha256,
                    lease_json=lease_payload,
                    state="held",
                    fencing_epoch=fencing_epoch,
                    cpu_cores=resource_request.cpu_cores,
                    memory_bytes=resource_request.memory_bytes,
                    scratch_bytes=resource_request.scratch_bytes,
                    exclusive=resource_request.exclusive,
                    accelerator_count=resource_request.accelerator_count,
                    acquired_at=now,
                    heartbeat_at=now,
                    lease_expires_at=lease_expires_at,
                    released_at=None,
                )
            )
            # SQLAlchemy has no public relationships to order these private authority rows;
            # establish the attempt/resource FK parents before adding device leases.
            session.flush()
            requested_device_memory = resource_request.minimum_accelerator_memory_bytes or 1
            for resource, device_head in zip(device_resources, device_heads, strict=True):
                assert resource.accelerator_uuid is not None
                device_lease_id = _stable_id(
                    "dle", {"attempt_id": attempt_id, "device_id": resource.resource_id}
                )
                session.add(
                    _ExecutionDeviceLeaseRecord(
                        device_lease_id=device_lease_id,
                        resource_lease_id=lease_id,
                        attempt_id=attempt_id,
                        node_id=node.node_id,
                        device_id=resource.resource_id,
                        hardware_uuid=resource.accelerator_uuid,
                        state="held",
                        fencing_epoch=fencing_epoch,
                        requested_memory_bytes=requested_device_memory,
                        acquired_at=now,
                        released_at=None,
                    )
                )
                device_head.fencing_counter = fencing_epoch
                device_head.active_device_lease_id = device_lease_id
                device_head.state_version += 1
                device_head.updated_at = now
            reservation_id = _stable_id("brv", {"attempt_id": attempt_id})
            session.add(
                _ExecutionBudgetReservationRecord(
                    reservation_id=reservation_id,
                    authorization_sha256=authorization.authorization_sha256,
                    attempt_id=attempt_id,
                    execution_id=intent.execution_id,
                    cost_quote_sha256=quote.quote_sha256,
                    currency_code=quote.currency_code,
                    fixed_charge_microunits=quote.fixed_charge_microunits,
                    charge_per_second_microunits=(quote.charge_per_second_microunits),
                    maximum_lease_seconds=quote.maximum_lease_seconds,
                    actual_lease_seconds=None,
                    held_microunits=hold,
                    settled_microunits=0,
                    state="held",
                    reserved_at=now,
                    settled_at=None,
                )
            )
            self._append_budget_event(
                session,
                reservation_id=reservation_id,
                authorization_sha256=authorization.authorization_sha256,
                event_type="reserved",
                reserved_delta_microunits=hold,
                spent_delta_microunits=0,
                recorded_at=now,
                details={
                    "cost_quote_sha256": quote.quote_sha256,
                    "fixed_charge_microunits": quote.fixed_charge_microunits,
                    "charge_per_second_microunits": (quote.charge_per_second_microunits),
                    "maximum_lease_seconds": quote.maximum_lease_seconds,
                    "held_microunits": hold,
                },
            )
            budget_head.reserved_microunits += hold
            budget_head.state_version += 1
            budget_head.updated_at = now
            node.reserved_cpu_cores += resource_request.cpu_cores
            node.reserved_memory_bytes += resource_request.memory_bytes
            node.reserved_scratch_bytes += resource_request.scratch_bytes
            if resource_request.exclusive:
                node.exclusive_lease_id = lease_id
            node.state_version += 1
            node.updated_at = now
            execution_head.last_attempt_number = intent.infrastructure_attempt.attempt_number
            execution_head.active_attempt_id = attempt_id
            execution_head.state_version += 1
            execution_head.updated_at = now
            session.flush()
            attempt = session.get(_ExecutionAttemptRecord, attempt_id)
            assert attempt is not None
            return ReservationClaim(
                snapshot=self._snapshot(session, attempt),
                created=True,
                lease_token=raw_token,
            )

    def _validate_pricing_authority(self, bundle: EngineeringQualificationBundle) -> None:
        quote = bundle.cost_quote
        pin = self._pricing_authority
        if (
            quote.quoted_by_principal_id not in pin.quote_principal_ids
            or quote.rate_card_sha256 not in pin.rate_card_sha256s
            or quote.pricing_policy_sha256 not in pin.pricing_policy_sha256s
            or quote.currency_code not in pin.currency_codes
        ):
            raise AdmissionConflict("cost quote is outside deployment-pinned pricing authority")
        if quote.maximum_lease_seconds > bundle.intent.resource_request.wall_time_seconds:
            raise AdmissionConflict("quoted lease exceeds the frozen resource wall-time bound")

    @staticmethod
    def _validate_execution_head(
        head: _ExecutionHeadRecord, bundle: EngineeringQualificationBundle
    ) -> None:
        intent = bundle.intent
        expected = (
            intent.quest_id,
            intent.protocol_sha256,
            intent.work_order_id,
            intent.work_order_sha256,
            intent.replicate_slot.replicate_slot_id,
            intent.replicate_slot.replicate_slot_sha256,
        )
        observed = (
            head.quest_id,
            head.protocol_sha256,
            head.work_order_id,
            head.work_order_sha256,
            head.replicate_slot_id,
            head.replicate_slot_sha256,
        )
        if observed != expected:
            raise AdmissionConflict("execution id is bound to a different scientific slot")

    def _validate_idempotent_attempt(
        self,
        session: Session,
        attempt: _ExecutionAttemptRecord,
        bundle: EngineeringQualificationBundle,
        *,
        expected_grant_sha256: str,
    ) -> None:
        if (
            attempt.execution_id != bundle.intent.execution_id
            or attempt.intent_sha256 != bundle.intent.intent_sha256
            or attempt.intent_json != _model_json(bundle.intent)
            or attempt.bundle_sha256 != bundle.bundle_sha256
            or attempt.grant_sha256 != expected_grant_sha256
        ):
            raise AdmissionConflict("attempt id is already bound to different authority")
        admission = session.get(
            _ExecutionQualificationAdmissionRecord,
            attempt.admission_sha256,
        )
        try:
            stored_bundle = EngineeringQualificationBundle.model_validate(
                admission.bundle_json if admission is not None else None
            )
            stored_grant = EngineeringQualificationGrant.model_validate(
                admission.grant_json if admission is not None else None
            )
            stored_verified = VerifiedEngineeringQualification.model_validate(
                admission.verified_receipt_json if admission is not None else None
            )
            reverified = verify_engineering_qualification(
                bundle=stored_bundle,
                grant=stored_grant,
                authority=self._authority,
                artifact_resolver=self._artifact_resolver,
                authority_resolver=self._execution_authority_resolver,
                observed_at=admission.verified_at,
            )
        except (TypeError, ValueError) as exc:
            raise AdmissionConflict("stored qualification admission is invalid") from exc
        if (
            admission is None
            or stored_bundle != bundle
            or stored_grant.grant_sha256 != expected_grant_sha256
            or admission.bundle_json != _model_json(bundle)
            or admission.grant_json != _model_json(stored_grant)
            or admission.grant_sha256 != expected_grant_sha256
            or admission.bundle_sha256 != bundle.bundle_sha256
            or admission.intent_sha256 != bundle.intent.intent_sha256
            or admission.infrastructure_attempt_id != attempt.attempt_id
            or admission.verified_at != stored_verified.verified_at
            or admission.admitted_at != stored_verified.verified_at
            or reverified != stored_verified
            or admission.admission_sha256 != _stable_admission_sha256(stored_verified)
        ):
            raise AdmissionConflict("attempt id is bound to another qualification receipt")

    @staticmethod
    def _validate_retry_lineage(
        session: Session,
        *,
        execution_head: _ExecutionHeadRecord,
        bundle: EngineeringQualificationBundle,
    ) -> None:
        infrastructure_attempt = bundle.intent.infrastructure_attempt
        if infrastructure_attempt.attempt_number != execution_head.last_attempt_number + 1:
            raise AdmissionConflict("attempt number is not the next durable execution-head value")
        prior = bundle.prior_execution_receipt
        if infrastructure_attempt.attempt_number == 1:
            if execution_head.last_attempt_number != 0 or prior is not None:
                raise AdmissionConflict("initial attempt collides with durable prior lineage")
            return
        if prior is None:
            raise AdmissionConflict("retry lacks its exact prior execution receipt")
        prior_sha256 = prior.execution_receipt_sha256
        terminal = session.get(_ExecutionTerminalReceiptRecord, prior_sha256)
        previous_attempt_id = infrastructure_attempt.previous_attempt_id
        previous_attempt = (
            session.get(_ExecutionAttemptRecord, previous_attempt_id)
            if previous_attempt_id is not None
            else None
        )
        if (
            terminal is None
            or previous_attempt is None
            or terminal.payload_json != _model_json(prior)
            or terminal.execution_id != bundle.intent.execution_id
            or terminal.attempt_id != previous_attempt_id
            or previous_attempt.attempt_number != execution_head.last_attempt_number
            or previous_attempt.status != "failed"
            or previous_attempt.terminal_receipt_sha256 != prior_sha256
            or infrastructure_attempt.prior_confirmed_failure_receipt_sha256 != prior_sha256
        ):
            raise AdmissionConflict(
                "retry prior receipt is absent from this execution database or differs byte-for-byte"
            )

    def _validate_exact_placement(
        self,
        *,
        bundle: EngineeringQualificationBundle,
        manifest: WorkerNodeManifest,
        inventory: NodeInventoryAttestation,
        node: _ExecutionNodeRecord,
    ) -> tuple[object, ...]:
        intent = bundle.intent
        request = intent.resource_request
        quote = bundle.cost_quote
        if request.accelerator_count > 1:
            raise CapacityUnavailable("PR-4a supports at most one local accelerator per attempt")
        if quote.selected_node_manifest_sha256 != manifest.manifest_sha256:
            raise CapacityUnavailable("quoted manifest is not the locked registered node")
        if request.network_policy is not NetworkPolicy.NONE:
            raise CapacityUnavailable("PR-4a local placement permits network-none only")
        if request.network_policy not in manifest.network_policies:
            raise CapacityUnavailable("node manifest does not permit the exact network policy")
        frozen_ports = {
            item.port_id: item for item in bundle.compilation_request.protocol.data_ports
        }
        try:
            input_classifications = {
                frozen_ports[item.input_port_id].data_classification.value
                for item in intent.input_artifact_bindings
            }
        except KeyError as exc:  # The bundle verifier should already reject this; stay closed.
            raise CapacityUnavailable(
                "bound input port is absent from the frozen protocol"
            ) from exc
        data_classifications = input_classifications | {
            item.data_classification for item in intent.expected_artifacts
        }
        if not data_classifications.issubset(set(manifest.allowed_data_classifications)):
            raise CapacityUnavailable(
                "node manifest does not permit every bound input/output classification"
            )
        if request.data_locality is DataLocality.REGION_PINNED:
            raise CapacityUnavailable("PR-4a has no deployment-pinned node region identity")
        if (
            request.data_locality is DataLocality.SITE_PINNED
            and manifest.site_id not in request.locality_labels
        ):
            raise CapacityUnavailable("node site does not satisfy frozen site locality")
        inventory_resources = {item.resource_id: item for item in inventory.resources}
        try:
            selected = tuple(inventory_resources[item] for item in quote.selected_resource_ids)
        except KeyError as exc:
            raise CapacityUnavailable(
                "quoted resource id is absent from current inventory"
            ) from exc
        if tuple(item.resource_id for item in selected) != quote.selected_resource_ids:
            raise CapacityUnavailable("quoted resources are not exact canonical current resources")
        accepted = set(request.accepted_resource_class_ids)
        catalog = {
            item.resource_class_id: item
            for item in bundle.compilation_request.resource_catalog.resource_classes
        }
        for resource in selected:
            if resource.health is not NodeHealth.HEALTHY:
                raise CapacityUnavailable("quoted resource is not healthy")
            if (
                resource.external_process_count
                or resource.memory_bytes_external_occupied
                or resource.scratch_bytes_external_occupied
                or (resource.accelerator_memory_bytes_external_occupied or 0)
            ):
                raise CapacityUnavailable("quoted resource has external occupancy")
            class_ids = accepted.intersection(resource.resource_class_ids)
            candidates = tuple(catalog[item] for item in sorted(class_ids) if item in catalog)
            if not candidates or not any(
                self._resource_class_matches(
                    resource_class=item,
                    resource_kind=resource.kind,
                    manifest=manifest,
                    bundle=bundle,
                )
                for item in candidates
            ):
                raise CapacityUnavailable(
                    "selected live resource lacks a compatible accepted static resource class"
                )
        cpu_resources = tuple(item for item in selected if item.kind is ResourceKind.CPU)
        device_resources = tuple(item for item in selected if item.kind is ResourceKind.ACCELERATOR)
        if len(cpu_resources) != 1 or len(device_resources) != request.accelerator_count:
            raise CapacityUnavailable("quoted CPU/accelerator resource shape differs from request")
        available_cpu = sum(
            item.cpu_cores_allocatable + item.cpu_cores_managed_occupied for item in cpu_resources
        )
        available_memory = sum(
            item.memory_bytes_allocatable + item.memory_bytes_managed_occupied
            for item in cpu_resources
        )
        available_scratch = sum(
            item.scratch_bytes_allocatable + item.scratch_bytes_managed_occupied
            for item in cpu_resources
        )
        if request.exclusive:
            if (
                node.exclusive_lease_id is not None
                or node.reserved_cpu_cores
                or node.reserved_memory_bytes
                or node.reserved_scratch_bytes
            ):
                raise CapacityUnavailable("exclusive request requires an idle managed node")
        elif node.exclusive_lease_id is not None:
            raise CapacityUnavailable("node is retained by an exclusive lease")
        if (
            node.reserved_cpu_cores + request.cpu_cores > available_cpu
            or node.reserved_memory_bytes + request.memory_bytes > available_memory
            or node.reserved_scratch_bytes + request.scratch_bytes > available_scratch
        ):
            raise CapacityUnavailable("locked node capacity cannot satisfy exact request")
        for resource in device_resources:
            assert resource.accelerator_model is not None
            assert resource.accelerator_memory_bytes_allocatable is not None
            assert resource.accelerator_compute_capability is not None
            if (
                resource.accelerator_model not in request.allowed_accelerator_models
                or (resource.accelerator_memory_bytes_managed_occupied or 0)
                or resource.accelerator_memory_bytes_allocatable
                < (request.minimum_accelerator_memory_bytes or 0)
                or _compute_capability(resource.accelerator_compute_capability)
                < _compute_capability(request.minimum_compute_capability or "0.0")
                or not set(request.required_features).issubset(set(resource.features))
            ):
                raise CapacityUnavailable(
                    "accelerator does not satisfy exact model/memory/features"
                )
        return device_resources

    @staticmethod
    def _resource_class_matches(
        *,
        resource_class: StaticResourceClass,
        resource_kind: ResourceKind,
        manifest: WorkerNodeManifest,
        bundle: EngineeringQualificationBundle,
    ) -> bool:
        request = bundle.intent.resource_request
        if (
            resource_class.kind is not resource_kind
            or resource_class.cpu_architecture != manifest.cpu_architecture
            or resource_class.oci_platform != manifest.oci_platform
            or resource_class.container_runtime != manifest.container_runtime
            or request.network_policy not in resource_class.network_policies
            or (
                request.data_locality is DataLocality.SITE_PINNED
                and manifest.site_id not in resource_class.locality_labels
            )
            or (request.exclusive and not resource_class.supports_exclusive)
            or (request.preemptible and not resource_class.supports_preemption)
            or (
                request.checkpoint_interval_seconds is not None
                and not resource_class.supports_checkpointing
            )
        ):
            return False
        if resource_kind is ResourceKind.CPU:
            return (
                request.cpu_cores <= resource_class.cpu_cores
                and request.memory_bytes <= resource_class.memory_bytes
                and request.scratch_bytes <= resource_class.scratch_bytes
                and set(request.required_features).issubset(set(resource_class.features))
            )
        return (
            resource_class.accelerator_model in request.allowed_accelerator_models
            and (resource_class.accelerator_memory_bytes or 0)
            >= (request.minimum_accelerator_memory_bytes or 0)
            and _compute_capability(resource_class.accelerator_compute_capability or "0.0")
            >= _compute_capability(request.minimum_compute_capability or "0.0")
            and set(request.required_features).issubset(set(resource_class.features))
        )

    @staticmethod
    def _lock_device_heads(
        session: Session,
        *,
        node: _ExecutionNodeRecord,
        inventory_sha256: str,
        device_ids: tuple[str, ...],
    ) -> tuple[_ExecutionDeviceHeadRecord, ...]:
        if not device_ids:
            return ()
        heads = tuple(
            session.execute(
                select(_ExecutionDeviceHeadRecord)
                .where(
                    _ExecutionDeviceHeadRecord.node_id == node.node_id,
                    _ExecutionDeviceHeadRecord.device_id.in_(device_ids),
                )
                .order_by(_ExecutionDeviceHeadRecord.device_id)
                .with_for_update()
            ).scalars()
        )
        if tuple(item.device_id for item in heads) != tuple(sorted(device_ids)) or any(
            item.current_inventory_sha256 != inventory_sha256
            or item.active_device_lease_id is not None
            for item in heads
        ):
            raise CapacityUnavailable("accelerator fencing head is absent, stale, or already held")
        return heads

    @staticmethod
    def _append_budget_event(
        session: Session,
        *,
        reservation_id: str,
        authorization_sha256: str,
        event_type: str,
        reserved_delta_microunits: int,
        spent_delta_microunits: int,
        recorded_at: datetime,
        details: dict[str, object] | None = None,
    ) -> None:
        previous = session.execute(
            select(_ExecutionBudgetEventRecord)
            .where(_ExecutionBudgetEventRecord.reservation_id == reservation_id)
            .order_by(_ExecutionBudgetEventRecord.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        sequence = 1 if previous is None else previous.sequence + 1
        previous_sha256 = None if previous is None else previous.event_sha256
        payload = {
            "schema_name": "aletheia.execution_budget_event",
            "schema_version": 1,
            "reservation_id": reservation_id,
            "authorization_sha256": authorization_sha256,
            "sequence": sequence,
            "previous_event_sha256": previous_sha256,
            "event_type": event_type,
            "reserved_delta_microunits": reserved_delta_microunits,
            "spent_delta_microunits": spent_delta_microunits,
            "details": details or {},
            "recorded_at": recorded_at.isoformat(),
        }
        payload_sha256 = canonical_sha256(payload)
        event_sha256 = canonical_sha256(
            {
                "schema_name": "aletheia.execution_budget_event_identity",
                "schema_version": 1,
                "payload_sha256": payload_sha256,
                "previous_event_sha256": previous_sha256,
            }
        )
        session.add(
            _ExecutionBudgetEventRecord(
                event_sha256=event_sha256,
                reservation_id=reservation_id,
                authorization_sha256=authorization_sha256,
                sequence=sequence,
                previous_event_sha256=previous_sha256,
                event_type=event_type,
                reserved_delta_microunits=reserved_delta_microunits,
                spent_delta_microunits=spent_delta_microunits,
                payload_sha256=payload_sha256,
                payload_json=payload,
                recorded_at=recorded_at,
            )
        )

    def load_attempt(self, attempt_id: str) -> ReservationSnapshot | None:
        with self._sessions() as session:
            attempt = session.get(_ExecutionAttemptRecord, attempt_id)
            return None if attempt is None else self._snapshot(session, attempt)

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        new_lease_token: str,
    ) -> AdoptionCommitReceipt:
        """Rotate one same-attempt fence only with fresh node-signed singleton evidence."""

        receipt = AttemptAdoptionReceipt.model_validate(receipt.model_dump(mode="python"))
        if len(new_lease_token) < 43 or _token_sha256(new_lease_token) != (
            receipt.new_lease_token_sha256
        ):
            raise LeaseAuthorityError("adoption raw token is weak or differs from signed hash")
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(
                session, receipt.infrastructure_attempt_id
            )
            if attempt.latest_adoption_sha256 == receipt.adoption_receipt_sha256:
                if (
                    attempt.fencing_epoch != receipt.new_fencing_epoch
                    or attempt.lease_token_sha256 != receipt.new_lease_token_sha256
                ):
                    raise LeaseAuthorityError("adoption receipt identity is rebound")
                return AdoptionCommitReceipt(
                    adoption_receipt_sha256=receipt.adoption_receipt_sha256,
                    snapshot=self._snapshot(session, attempt),
                    replayed=True,
                )
            if attempt.status not in {"running", "reconciliation_required"}:
                raise LeaseAuthorityError("only a running same runtime may be adopted")
            if attempt.runtime_identity_sha256 is None or attempt.runtime_identity_json is None:
                raise LeaseAuthorityError("attempt has no exact stored runtime identity")
            runtime_identity = NodeRuntimeIdentity.model_validate(attempt.runtime_identity_json)
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord).where(
                    _ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id
                )
            ).scalar_one()
            session.execute(
                select(_ExecutionBudgetHeadRecord)
                .where(
                    _ExecutionBudgetHeadRecord.authorization_sha256
                    == reservation.authorization_sha256
                )
                .with_for_update()
            ).scalar_one()
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == attempt.node_id)
                .with_for_update()
            ).scalar_one()
            device_leases = tuple(
                session.execute(
                    select(_ExecutionDeviceLeaseRecord)
                    .where(_ExecutionDeviceLeaseRecord.attempt_id == attempt.attempt_id)
                    .order_by(_ExecutionDeviceLeaseRecord.device_id)
                    .with_for_update()
                ).scalars()
            )
            device_heads = (
                tuple(
                    session.execute(
                        select(_ExecutionDeviceHeadRecord)
                        .where(
                            _ExecutionDeviceHeadRecord.node_id == node.node_id,
                            _ExecutionDeviceHeadRecord.device_id.in_(
                                tuple(item.device_id for item in device_leases)
                            ),
                        )
                        .order_by(_ExecutionDeviceHeadRecord.device_id)
                        .with_for_update()
                    ).scalars()
                )
                if device_leases
                else ()
            )
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord)
                .where(_ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            ).scalar_one()
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord)
                .where(_ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            ).scalar_one()
            now = _database_time(session)
            if now >= attempt.hard_deadline:
                raise LeaseAuthorityError("hard deadline forbids adoption")
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            if runtime_identity.boot_id != node.boot_id:
                raise LeaseAuthorityError("adoption node/runtime differs from deployment pin")
            expected_new_fence = attempt.fencing_epoch + 1
            if receipt.adoption_sequence != attempt.adoption_count + 1:
                raise LeaseAuthorityError("adoption sequence is not the next durable value")
            verify_attempt_adoption(
                receipt=receipt,
                authority=node_authority,
                expected_runtime_identity=runtime_identity,
                expected_previous_fencing_epoch=attempt.fencing_epoch,
                expected_previous_lease_token_sha256=attempt.lease_token_sha256,
                expected_new_fencing_epoch=expected_new_fence,
                expected_new_lease_token_sha256=receipt.new_lease_token_sha256,
                expected_allocator_principal_id=self._allocator_principal_id,
                maximum_inspection_age_seconds=(self._max_runtime_inspection_age_seconds),
                observed_at=now,
            )
            inspection = receipt.runtime_inspection_receipt
            self._validate_runtime_inspection_window(
                attempt,
                inspection_sequence=inspection.inspection_sequence,
                inspection_sha256=inspection.inspection_receipt_sha256,
                inspected_at=inspection.inspected_at,
                inspected_monotonic_ns=inspection.inspected_monotonic_ns,
                expires_at=inspection.expires_at,
                observed_at=now,
            )
            adoption_payload = _model_json(receipt)
            session.add(
                _ExecutionAttemptAdoptionRecord(
                    adoption_sha256=receipt.adoption_receipt_sha256,
                    attempt_id=attempt.attempt_id,
                    sequence=receipt.adoption_sequence,
                    previous_fencing_epoch=receipt.previous_fencing_epoch,
                    new_fencing_epoch=receipt.new_fencing_epoch,
                    previous_lease_token_sha256=receipt.previous_lease_token_sha256,
                    new_lease_token_sha256=receipt.new_lease_token_sha256,
                    runtime_identity_sha256=receipt.runtime_identity_sha256,
                    reason_sha256=canonical_sha256({"reason": receipt.reason.value}),
                    adopted_by_principal_id=receipt.allocator_principal_id,
                    payload_sha256=receipt.adoption_receipt_sha256,
                    payload_json=adoption_payload,
                    adopted_at=receipt.adopted_at,
                )
            )
            session.flush()
            new_expiry = max(
                attempt.lease_expires_at,
                min(now + self._heartbeat_extension, attempt.hard_deadline),
            )
            attempt.status = "running"
            attempt.fencing_epoch = expected_new_fence
            attempt.lease_token_sha256 = receipt.new_lease_token_sha256
            attempt.adoption_count += 1
            attempt.latest_adoption_sha256 = receipt.adoption_receipt_sha256
            attempt.last_runtime_inspection_sequence = inspection.inspection_sequence
            attempt.last_runtime_inspection_sha256 = inspection.inspection_receipt_sha256
            attempt.last_runtime_inspected_at = inspection.inspected_at
            attempt.last_runtime_inspected_monotonic_ns = inspection.inspected_monotonic_ns
            attempt.heartbeat_at = now
            attempt.lease_expires_at = new_expiry
            attempt.reconciliation_reason = None
            attempt.state_version += 1
            attempt.updated_at = now
            resource.state = "held"
            resource.fencing_epoch = expected_new_fence
            resource.heartbeat_at = now
            resource.lease_expires_at = new_expiry
            for device, device_head in zip(device_leases, device_heads, strict=True):
                if device_head.active_device_lease_id != device.device_lease_id:
                    raise LeaseAuthorityError("device adoption differs from retained active lease")
                device.state = "held"
                device.fencing_epoch = expected_new_fence
                device_head.fencing_counter = expected_new_fence
                device_head.state_version += 1
                device_head.updated_at = now
            reservation.state = "held"
            self._append_budget_event(
                session,
                reservation_id=reservation.reservation_id,
                authorization_sha256=reservation.authorization_sha256,
                event_type="adopted",
                reserved_delta_microunits=0,
                spent_delta_microunits=0,
                recorded_at=now,
            )
            session.flush()
            return AdoptionCommitReceipt(
                adoption_receipt_sha256=receipt.adoption_receipt_sha256,
                snapshot=self._snapshot(session, attempt),
                replayed=False,
            )

    def commit_terminal_receipt(
        self,
        *,
        receipt: ExecutionReceipt,
        node_execution_receipt: NodeExecutionReceipt,
        terminal_verification_attestation: TerminalVerificationAttestation,
        lease_token: str,
        fencing_epoch: int,
    ) -> TerminalCommitReceipt:
        """Verify node termination/artifact custody, release holds, and enqueue atomically."""

        receipt = ExecutionReceipt.model_validate(receipt.model_dump(mode="python"))
        node_execution_receipt = NodeExecutionReceipt.model_validate(
            node_execution_receipt.model_dump(mode="python")
        )
        terminal_verification_attestation = TerminalVerificationAttestation.model_validate(
            terminal_verification_attestation.model_dump(mode="python")
        )
        if receipt.terminal_state is ExecutionTerminalState.RECONCILIATION_REQUIRED:
            raise LeaseAuthorityError("an unresolved outcome cannot release retained authority")
        attempt_id = receipt.intent.infrastructure_attempt.infrastructure_attempt_id
        with self._sessions() as session, session.begin():
            execution_head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            existing = session.execute(
                select(_ExecutionTerminalReceiptRecord).where(
                    _ExecutionTerminalReceiptRecord.attempt_id == attempt_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.receipt_sha256 != receipt.execution_receipt_sha256
                    or existing.payload_json != _model_json(receipt)
                    or existing.node_execution_receipt_sha256
                    != node_execution_receipt.node_execution_receipt_sha256
                    or existing.node_execution_receipt_json != _model_json(node_execution_receipt)
                    or existing.terminal_verification_attestation_sha256
                    != terminal_verification_attestation.attestation_sha256
                    or existing.terminal_verification_attestation_json
                    != _model_json(terminal_verification_attestation)
                    or existing.terminal_verification_authority_pin_sha256
                    != canonical_sha256(self._terminal_verification_authority.pin)
                    or existing.terminal_verification_authority_pin_json
                    != _model_json(self._terminal_verification_authority.pin)
                ):
                    raise LeaseAuthorityError("terminal attempt is bound to another receipt")
                outbox = session.execute(
                    select(_ExecutionOutboxRecord).where(
                        _ExecutionOutboxRecord.receipt_sha256 == existing.receipt_sha256
                    )
                ).scalar_one()
                reservation = session.execute(
                    select(_ExecutionBudgetReservationRecord).where(
                        _ExecutionBudgetReservationRecord.attempt_id == attempt_id
                    )
                ).scalar_one()
                return TerminalCommitReceipt(
                    execution_receipt_sha256=existing.receipt_sha256,
                    outbox_id=outbox.outbox_id,
                    charged_microunits=reservation.settled_microunits,
                    snapshot=self._snapshot(session, attempt),
                    replayed=True,
                )
            if attempt.status not in {
                "running",
                "terminated",
                "verifying",
                "reconciliation_required",
            }:
                raise LeaseAuthorityError("attempt is not eligible for terminal verification")
            if attempt.runtime_identity_sha256 is None or attempt.runtime_identity_json is None:
                raise LeaseAuthorityError("terminal attempt lacks its exact runtime identity")
            if receipt.artifact_manifest is None:
                raise LeaseAuthorityError(
                    "terminal receipt requires the exact empty, partial, or complete manifest"
                )
            expected_intent = type(receipt.intent).model_validate(attempt.intent_json)
            if (
                receipt.intent != expected_intent
                or receipt.intent.intent_sha256 != attempt.intent_sha256
                or receipt.intent.execution_id != attempt.execution_id
            ):
                raise LeaseAuthorityError("central receipt differs from locked intent")
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord).where(
                    _ExecutionBudgetReservationRecord.attempt_id == attempt_id
                )
            ).scalar_one()
            budget_head = session.execute(
                select(_ExecutionBudgetHeadRecord)
                .where(
                    _ExecutionBudgetHeadRecord.authorization_sha256
                    == reservation.authorization_sha256
                )
                .with_for_update()
            ).scalar_one()
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == attempt.node_id)
                .with_for_update()
            ).scalar_one()
            device_leases = tuple(
                session.execute(
                    select(_ExecutionDeviceLeaseRecord)
                    .where(_ExecutionDeviceLeaseRecord.attempt_id == attempt_id)
                    .order_by(_ExecutionDeviceLeaseRecord.device_id)
                    .with_for_update()
                ).scalars()
            )
            device_heads = (
                tuple(
                    session.execute(
                        select(_ExecutionDeviceHeadRecord)
                        .where(
                            _ExecutionDeviceHeadRecord.node_id == node.node_id,
                            _ExecutionDeviceHeadRecord.device_id.in_(
                                tuple(item.device_id for item in device_leases)
                            ),
                        )
                        .order_by(_ExecutionDeviceHeadRecord.device_id)
                        .with_for_update()
                    ).scalars()
                )
                if device_leases
                else ()
            )
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord)
                .where(_ExecutionResourceLeaseRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one()
            reservation = session.execute(
                select(_ExecutionBudgetReservationRecord)
                .where(_ExecutionBudgetReservationRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one()
            now = _database_time(session)
            if receipt.verified_at > now:
                raise LeaseAuthorityError("central receipt is newer than locked DB time")
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            runtime_identity = NodeRuntimeIdentity.model_validate(attempt.runtime_identity_json)
            verified_node = verify_node_execution_receipt(
                receipt=node_execution_receipt,
                authority=node_authority,
                expected_intent=expected_intent,
                expected_runtime_identity=runtime_identity,
                expected_node_inventory_sha256=attempt.node_inventory_sha256,
                expected_resource_lease_sha256=resource.lease_sha256,
                expected_artifact_manifest=receipt.artifact_manifest,
                expected_fencing_epoch=attempt.fencing_epoch,
                expected_lease_token_sha256=attempt.lease_token_sha256,
                maximum_inspection_age_seconds=(self._max_runtime_inspection_age_seconds),
                observed_at=now,
            )
            if (
                receipt.worker_node_manifest_sha256 != node.node_manifest_sha256
                or receipt.node_inventory_sha256 != attempt.node_inventory_sha256
                or receipt.resource_lease_sha256 != resource.lease_sha256
                or receipt.node_execution_receipt_sha256
                != verified_node.node_execution_receipt_sha256
                or receipt.started_at != node_execution_receipt.started_at
                or receipt.ended_at != node_execution_receipt.ended_at
                or receipt.verified_at < node_execution_receipt.signed_at
            ):
                raise LeaseAuthorityError("central receipt differs from exact signed node exit")
            termination = node_execution_receipt.termination_inspection_receipt
            self._validate_runtime_inspection_window(
                attempt,
                inspection_sequence=termination.inspection_sequence,
                inspection_sha256=termination.inspection_receipt_sha256,
                inspected_at=termination.inspected_at,
                inspected_monotonic_ns=termination.inspected_monotonic_ns,
                expires_at=termination.expires_at,
                observed_at=now,
            )
            signed_success = node_execution_receipt.exit_code == 0
            claimed_success = receipt.terminal_state is ExecutionTerminalState.ENGINEERING_SUCCEEDED
            if node_execution_receipt.started_at < attempt.reserved_at:
                raise LeaseAuthorityError(
                    "terminal disposition contradicts the signed exit or lease time boundary"
                )
            self._reverify_terminal_artifacts(receipt, observed_at=now)
            try:
                verified_terminal = self._terminal_verification_authority.verify(
                    attestation=terminal_verification_attestation,
                    execution_receipt=receipt,
                    node_execution_receipt=node_execution_receipt,
                    observed_at=now,
                )
            except (TypeError, ValueError) as exc:
                raise LeaseAuthorityError(
                    "terminal disposition lacks exact active signed verification authority"
                ) from exc
            manifest_keys = {item.artifact_key for item in receipt.artifact_manifest.entries}
            required_keys = {
                item.artifact_key for item in expected_intent.expected_artifacts if item.required
            }
            outputs_complete = required_keys.issubset(manifest_keys)
            ended_late = node_execution_receipt.ended_at > attempt.hard_deadline
            if signed_success and not ended_late and outputs_complete:
                deterministic_disposition = claimed_success
            elif signed_success:
                expected_failure_category = "timeout" if ended_late else "invalid_output"
                deterministic_disposition = (
                    receipt.terminal_state is ExecutionTerminalState.EXECUTION_FAILED
                    and receipt.failure is not None
                    and receipt.failure.category.value == expected_failure_category
                )
            else:
                deterministic_disposition = not claimed_success
            if not deterministic_disposition:
                raise LeaseAuthorityError(
                    "terminal disposition contradicts signed exit, deadline, or output closure"
                )
            receipt_sha256 = receipt.execution_receipt_sha256
            terminal_status = {
                ExecutionTerminalState.ENGINEERING_SUCCEEDED: "succeeded",
                ExecutionTerminalState.EXECUTION_FAILED: "failed",
                ExecutionTerminalState.CANCELLED: "cancelled",
            }[receipt.terminal_state]
            receipt_payload = _model_json(receipt)
            node_receipt_payload = _model_json(node_execution_receipt)
            artifact_manifest_sha256 = receipt.artifact_manifest.manifest_sha256
            artifact_receipt_hashes = [
                item.verified_receipt_sha256 for item in receipt.artifact_verified_receipts
            ]
            session.add(
                _ExecutionTerminalReceiptRecord(
                    receipt_sha256=receipt_sha256,
                    attempt_id=attempt_id,
                    execution_id=attempt.execution_id,
                    intent_sha256=attempt.intent_sha256,
                    resource_lease_sha256=resource.lease_sha256,
                    node_execution_receipt_sha256=(
                        node_execution_receipt.node_execution_receipt_sha256
                    ),
                    node_execution_receipt_json=node_receipt_payload,
                    terminal_verification_attestation_sha256=(
                        verified_terminal.terminal_verification_attestation_sha256
                    ),
                    terminal_verification_attestation_json=_model_json(
                        terminal_verification_attestation
                    ),
                    terminal_verification_authority_pin_sha256=canonical_sha256(
                        self._terminal_verification_authority.pin
                    ),
                    terminal_verification_authority_pin_json=_model_json(
                        self._terminal_verification_authority.pin
                    ),
                    terminal_verification_policy_sha256=(
                        self._terminal_verification_authority.pin.policy_sha256
                    ),
                    terminal_verification_key_id=(self._terminal_verification_authority.pin.key_id),
                    terminal_state=receipt.terminal_state.value,
                    payload_sha256=receipt_sha256,
                    payload_json=receipt_payload,
                    artifact_manifest_sha256=artifact_manifest_sha256,
                    artifact_manifest_json=_model_json(receipt.artifact_manifest),
                    artifact_verified_receipt_sha256s_json=artifact_receipt_hashes,
                    committed_by_principal_id=verified_terminal.verified_by_principal_id,
                    committed_at=now,
                )
            )
            session.flush()
            outbox_id = _stable_id("xob", {"receipt_sha256": receipt_sha256})
            session.add(
                _ExecutionOutboxRecord(
                    outbox_id=outbox_id,
                    receipt_sha256=receipt_sha256,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt_id,
                    topic="execution.terminal.v1",
                    delivery_key=f"execution:{attempt.execution_id}:{attempt_id}",
                    payload_sha256=receipt_sha256,
                    payload_json=receipt_payload,
                    status="pending",
                    publish_attempts=0,
                    created_at=now,
                    published_at=None,
                )
            )
            session.flush()
            duration = now - resource.acquired_at
            if duration < timedelta(0):
                raise BudgetUnavailable("database clock precedes the durable lease acquisition")
            duration_microseconds = (
                duration.days * 86_400_000_000
                + duration.seconds * 1_000_000
                + duration.microseconds
            )
            actual_lease_seconds = min(
                (duration_microseconds + 999_999) // 1_000_000,
                reservation.maximum_lease_seconds,
            )
            charged = reservation.fixed_charge_microunits + (
                reservation.charge_per_second_microunits * actual_lease_seconds
            )
            if charged > reservation.held_microunits:
                raise BudgetUnavailable("deterministic settlement exceeds exact quote hold")
            if budget_head.reserved_microunits < reservation.held_microunits:
                raise BudgetUnavailable("budget head lost the exact terminal hold")
            reservation.state = "settled"
            reservation.actual_lease_seconds = actual_lease_seconds
            reservation.settled_microunits = charged
            reservation.settled_at = now
            budget_head.reserved_microunits -= reservation.held_microunits
            budget_head.spent_microunits += charged
            budget_head.state_version += 1
            budget_head.updated_at = now
            self._append_budget_event(
                session,
                reservation_id=reservation.reservation_id,
                authorization_sha256=reservation.authorization_sha256,
                event_type="settled",
                reserved_delta_microunits=-reservation.held_microunits,
                spent_delta_microunits=charged,
                recorded_at=now,
                details={
                    "cost_quote_sha256": reservation.cost_quote_sha256,
                    "fixed_charge_microunits": reservation.fixed_charge_microunits,
                    "charge_per_second_microunits": (reservation.charge_per_second_microunits),
                    "actual_lease_seconds": actual_lease_seconds,
                    "maximum_lease_seconds": reservation.maximum_lease_seconds,
                    "held_microunits": reservation.held_microunits,
                    "charged_microunits": charged,
                    "lease_acquired_at": resource.acquired_at.isoformat(),
                    "settlement_observed_at": now.isoformat(),
                    "node_started_at": node_execution_receipt.started_at.isoformat(),
                    "node_ended_at": node_execution_receipt.ended_at.isoformat(),
                    "node_execution_receipt_sha256": (
                        node_execution_receipt.node_execution_receipt_sha256
                    ),
                },
            )
            resource.state = "released"
            resource.released_at = now
            for device, device_head in zip(device_leases, device_heads, strict=True):
                if device_head.active_device_lease_id != device.device_lease_id:
                    raise LeaseAuthorityError("terminal device fencing head is not exact")
                device.state = "released"
                device.released_at = now
                device_head.active_device_lease_id = None
                device_head.state_version += 1
                device_head.updated_at = now
            if (
                node.reserved_cpu_cores < resource.cpu_cores
                or node.reserved_memory_bytes < resource.memory_bytes
                or node.reserved_scratch_bytes < resource.scratch_bytes
            ):
                raise CapacityUnavailable("node capacity head lost the exact resource hold")
            node.reserved_cpu_cores -= resource.cpu_cores
            node.reserved_memory_bytes -= resource.memory_bytes
            node.reserved_scratch_bytes -= resource.scratch_bytes
            if resource.exclusive:
                if node.exclusive_lease_id != resource.lease_id:
                    raise CapacityUnavailable("exclusive node head differs from resource lease")
                node.exclusive_lease_id = None
            node.state_version += 1
            node.updated_at = now
            attempt.status = terminal_status
            attempt.terminal_receipt_sha256 = receipt_sha256
            attempt.last_runtime_inspection_sequence = termination.inspection_sequence
            attempt.last_runtime_inspection_sha256 = termination.inspection_receipt_sha256
            attempt.last_runtime_inspected_at = termination.inspected_at
            attempt.last_runtime_inspected_monotonic_ns = termination.inspected_monotonic_ns
            attempt.state_version += 1
            attempt.updated_at = now
            execution_head.active_attempt_id = None
            execution_head.state_version += 1
            execution_head.updated_at = now
            session.flush()
            return TerminalCommitReceipt(
                execution_receipt_sha256=receipt_sha256,
                outbox_id=outbox_id,
                charged_microunits=charged,
                snapshot=self._snapshot(session, attempt),
                replayed=False,
            )

    def _reverify_terminal_artifacts(
        self, receipt: ExecutionReceipt, *, observed_at: datetime
    ) -> None:
        if receipt.artifact_manifest is None:
            raise LeaseAuthorityError("terminal receipt has no exact artifact manifest")
        resolved_manifest = self._artifact_resolver.resolve_artifact_manifest(
            manifest_sha256=receipt.artifact_manifest.manifest_sha256,
            observed_at=observed_at,
        )
        if resolved_manifest != receipt.artifact_manifest:
            raise LeaseAuthorityError(
                "terminal artifact manifest is absent from fresh pinned custody"
            )
        manifest_keys = tuple(item.artifact_key for item in receipt.artifact_manifest.entries)
        verified_keys = tuple(
            item.artifact.artifact_key for item in receipt.artifact_verified_receipts
        )
        if verified_keys != manifest_keys:
            raise LeaseAuthorityError(
                "every terminal manifest entry requires one exact canonical verified receipt"
            )
        for expected in receipt.artifact_verified_receipts:
            resolution = self._artifact_resolver.resolve_verified_input_artifact(
                verified_receipt_sha256=expected.verified_receipt_sha256,
                observed_at=observed_at,
            )
            if (
                resolution is None
                or resolution.verified_receipt != expected
                or resolution.artifact_manifest != receipt.artifact_manifest
                or resolution.content_rehash_sha256 != expected.artifact.content_sha256
                or resolution.content_bytes != expected.artifact.bytes
                or resolution.resolved_at != observed_at
            ):
                raise LeaseAuthorityError(
                    "terminal artifact is absent from fresh pinned CAS/custody verification"
                )

    def _validate_runtime_inspection_window(
        self,
        attempt: _ExecutionAttemptRecord,
        *,
        inspection_sequence: int,
        inspection_sha256: str,
        inspected_at: datetime,
        inspected_monotonic_ns: int,
        expires_at: datetime,
        observed_at: datetime,
    ) -> None:
        if (
            inspection_sequence <= attempt.last_runtime_inspection_sequence
            or inspection_sha256 == attempt.last_runtime_inspection_sha256
            or (
                attempt.last_runtime_inspected_at is not None
                and inspected_at <= attempt.last_runtime_inspected_at
            )
            or (
                attempt.last_runtime_inspected_monotonic_ns is not None
                and inspected_monotonic_ns <= attempt.last_runtime_inspected_monotonic_ns
            )
            or inspected_at < observed_at - self._max_runtime_inspection_ttl
            or expires_at - inspected_at > self._max_runtime_inspection_ttl
        ):
            raise LeaseAuthorityError(
                "runtime inspection is stale, overlong, replayed, or sequence-regressive"
            )

    def start_attempt(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_identity: NodeRuntimeIdentity,
    ) -> AttemptTransitionReceipt:
        runtime_identity = NodeRuntimeIdentity.model_validate(
            runtime_identity.model_dump(mode="python")
        )
        if runtime_identity.infrastructure_attempt_id != attempt_id:
            raise LeaseAuthorityError("runtime belongs to another attempt")
        return self._transition_attempt(
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            target_status="starting",
            accepted_sources=frozenset({"reserved"}),
            runtime_identity=runtime_identity,
        )

    def mark_running(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> AttemptTransitionReceipt:
        return self._transition_attempt(
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            target_status="running",
            accepted_sources=frozenset({"starting"}),
        )

    def mark_terminated(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> AttemptTransitionReceipt:
        return self._transition_attempt(
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            target_status="terminated",
            accepted_sources=frozenset({"running"}),
        )

    def mark_verifying(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> AttemptTransitionReceipt:
        return self._transition_attempt(
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            target_status="verifying",
            accepted_sources=frozenset({"terminated", "running"}),
        )

    def _transition_attempt(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        target_status: str,
        accepted_sources: frozenset[str],
        runtime_identity: NodeRuntimeIdentity | None = None,
    ) -> AttemptTransitionReceipt:
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            node = session.execute(
                select(_ExecutionNodeRecord)
                .where(_ExecutionNodeRecord.node_id == attempt.node_id)
                .with_for_update()
            ).scalar_one()
            now = _database_time(session)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            if now >= attempt.lease_expires_at or now >= attempt.hard_deadline:
                self._reconcile_locked(
                    session,
                    attempt=attempt,
                    now=now,
                    reason="lease_expired",
                )
                return AttemptTransitionReceipt(
                    snapshot=self._snapshot(session, attempt), replayed=False
                )
            if attempt.status == target_status:
                if (
                    runtime_identity is not None
                    and attempt.runtime_identity_sha256 != runtime_identity.runtime_identity_sha256
                ):
                    raise LeaseAuthorityError("runtime identity differs on idempotent replay")
                return AttemptTransitionReceipt(
                    snapshot=self._snapshot(session, attempt), replayed=True
                )
            if attempt.status not in accepted_sources:
                raise LeaseAuthorityError(
                    f"cannot transition attempt {attempt.status!r} to {target_status!r}"
                )
            if runtime_identity is not None:
                if (
                    runtime_identity.node_id != attempt.node_id
                    or runtime_identity.boot_id != node.boot_id
                    or runtime_identity.execution_id != attempt.execution_id
                    or runtime_identity.infrastructure_attempt_id != attempt.attempt_id
                ):
                    raise LeaseAuthorityError("runtime identity differs from locked attempt/node")
                inventory_record = session.get(
                    _ExecutionInventoryAttestationRecord,
                    attempt.node_inventory_sha256,
                )
                if (
                    inventory_record is None
                    or not attempt.reserved_at <= runtime_identity.started_at <= now
                    or runtime_identity.started_monotonic_ns
                    < inventory_record.observed_monotonic_ns
                ):
                    raise LeaseAuthorityError(
                        "runtime start is outside the locked reservation/inventory order"
                    )
                if (
                    attempt.runtime_identity_sha256 is not None
                    and attempt.runtime_identity_sha256 != runtime_identity.runtime_identity_sha256
                ):
                    raise LeaseAuthorityError("attempt is already bound to another runtime")
                attempt.runtime_identity_sha256 = runtime_identity.runtime_identity_sha256
                attempt.runtime_identity_json = _model_json(runtime_identity)
            elif attempt.runtime_identity_sha256 is None:
                raise LeaseAuthorityError("attempt has no exact runtime identity")
            attempt.status = target_status
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return AttemptTransitionReceipt(
                snapshot=self._snapshot(session, attempt), replayed=False
            )

    def heartbeat(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> AttemptTransitionReceipt:
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord)
                .where(_ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            ).scalar_one()
            now = _database_time(session)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            if now >= attempt.lease_expires_at or now >= attempt.hard_deadline:
                self._reconcile_locked(
                    session,
                    attempt=attempt,
                    now=now,
                    reason="lease_expired",
                )
                return AttemptTransitionReceipt(
                    snapshot=self._snapshot(session, attempt), replayed=False
                )
            if attempt.status not in {"starting", "running"}:
                raise LeaseAuthorityError("heartbeats require a starting or running attempt")
            new_expiry = min(now + self._heartbeat_extension, attempt.hard_deadline)
            if new_expiry > attempt.lease_expires_at:
                attempt.lease_expires_at = new_expiry
                resource.lease_expires_at = new_expiry
            attempt.heartbeat_at = now
            attempt.state_version += 1
            attempt.updated_at = now
            resource.heartbeat_at = now
            session.flush()
            return AttemptTransitionReceipt(
                snapshot=self._snapshot(session, attempt), replayed=False
            )

    def reconcile_expired(self, *, limit: int = 100) -> tuple[ReservationSnapshot, ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("expiry reconciliation limit is outside 1..10000")
        with self._sessions() as session:
            now = _database_time(session)
            attempt_ids = tuple(
                session.execute(
                    select(_ExecutionAttemptRecord.attempt_id)
                    .where(
                        _ExecutionAttemptRecord.status.in_(
                            ACTIVE_ATTEMPT_STATES - {"reconciliation_required"}
                        ),
                        (
                            (_ExecutionAttemptRecord.lease_expires_at <= now)
                            | (_ExecutionAttemptRecord.hard_deadline <= now)
                        ),
                    )
                    .order_by(
                        _ExecutionAttemptRecord.lease_expires_at,
                        _ExecutionAttemptRecord.attempt_id,
                    )
                    .limit(limit)
                ).scalars()
            )
        snapshots: list[ReservationSnapshot] = []
        for attempt_id in attempt_ids:
            snapshot = self._reconcile_expired_attempt(attempt_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    def _reconcile_expired_attempt(self, attempt_id: str) -> ReservationSnapshot | None:
        with self._sessions() as session, session.begin():
            try:
                _head, attempt = self._lock_execution_attempt(session, attempt_id)
            except LeaseAuthorityError:
                return None
            now = _database_time(session)
            if attempt.status == "reconciliation_required":
                return self._snapshot(session, attempt)
            if attempt.status not in ACTIVE_ATTEMPT_STATES or (
                now < attempt.lease_expires_at and now < attempt.hard_deadline
            ):
                return None
            self._reconcile_locked(
                session,
                attempt=attempt,
                now=now,
                reason="lease_expired",
            )
            session.flush()
            return self._snapshot(session, attempt)

    def _reconcile_locked(
        self,
        session: Session,
        *,
        attempt: _ExecutionAttemptRecord,
        now: datetime,
        reason: str,
    ) -> None:
        if attempt.status == "reconciliation_required":
            return
        if attempt.status not in ACTIVE_ATTEMPT_STATES:
            raise LeaseAuthorityError("terminal attempt cannot enter reconciliation")
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        session.execute(
            select(_ExecutionBudgetHeadRecord)
            .where(
                _ExecutionBudgetHeadRecord.authorization_sha256 == reservation.authorization_sha256
            )
            .with_for_update()
        ).scalar_one()
        session.execute(
            select(_ExecutionNodeRecord)
            .where(_ExecutionNodeRecord.node_id == attempt.node_id)
            .with_for_update()
        ).scalar_one()
        device_leases = tuple(
            session.execute(
                select(_ExecutionDeviceLeaseRecord)
                .where(_ExecutionDeviceLeaseRecord.attempt_id == attempt.attempt_id)
                .order_by(_ExecutionDeviceLeaseRecord.device_id)
                .with_for_update()
            ).scalars()
        )
        if device_leases:
            session.execute(
                select(_ExecutionDeviceHeadRecord)
                .where(
                    _ExecutionDeviceHeadRecord.node_id == attempt.node_id,
                    _ExecutionDeviceHeadRecord.device_id.in_(
                        tuple(item.device_id for item in device_leases)
                    ),
                )
                .order_by(_ExecutionDeviceHeadRecord.device_id)
                .with_for_update()
            ).scalars().all()
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord)
            .where(_ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id)
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord)
            .where(_ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id)
            .with_for_update()
        ).scalar_one()
        locked_now = _database_time(session)
        if locked_now < now:
            raise LeaseAuthorityError("database clock moved backward while acquiring holds")
        now = locked_now
        attempt.status = "reconciliation_required"
        attempt.reconciliation_reason = reason
        attempt.state_version += 1
        attempt.updated_at = now
        resource.state = "reconciliation_required"
        for device in device_leases:
            device.state = "reconciliation_required"
        reservation.state = "reconciliation_required"
        self._append_budget_event(
            session,
            reservation_id=reservation.reservation_id,
            authorization_sha256=reservation.authorization_sha256,
            event_type="reconciliation_required",
            reserved_delta_microunits=0,
            spent_delta_microunits=0,
            recorded_at=now,
        )

    @staticmethod
    def _lock_execution_attempt(
        session: Session, attempt_id: str
    ) -> tuple[_ExecutionHeadRecord, _ExecutionAttemptRecord]:
        execution_id = session.execute(
            select(_ExecutionAttemptRecord.execution_id).where(
                _ExecutionAttemptRecord.attempt_id == attempt_id
            )
        ).scalar_one_or_none()
        if execution_id is None:
            raise LeaseAuthorityError("execution attempt does not exist")
        head = session.execute(
            select(_ExecutionHeadRecord)
            .where(_ExecutionHeadRecord.execution_id == execution_id)
            .with_for_update()
        ).scalar_one()
        attempt = session.execute(
            select(_ExecutionAttemptRecord)
            .where(_ExecutionAttemptRecord.attempt_id == attempt_id)
            .with_for_update()
        ).scalar_one()
        return head, attempt

    @staticmethod
    def _verify_lease_authority(
        attempt: _ExecutionAttemptRecord,
        *,
        lease_token: str,
        fencing_epoch: int,
    ) -> None:
        if fencing_epoch != attempt.fencing_epoch or not hmac.compare_digest(
            _token_sha256(lease_token), attempt.lease_token_sha256
        ):
            raise LeaseAuthorityError("lease token or fencing epoch is stale")

    @staticmethod
    def _require_sha256(value: str, label: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{label} must be a lowercase SHA-256 value")

    @staticmethod
    def _snapshot(session: Session, attempt: _ExecutionAttemptRecord) -> ReservationSnapshot:
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        devices = tuple(
            DeviceLeaseSnapshot(
                device_id=item.device_id,
                hardware_uuid=item.hardware_uuid,
                fencing_epoch=item.fencing_epoch,
                requested_memory_bytes=item.requested_memory_bytes,
                state=item.state,
            )
            for item in session.execute(
                select(_ExecutionDeviceLeaseRecord)
                .where(_ExecutionDeviceLeaseRecord.attempt_id == attempt.attempt_id)
                .order_by(_ExecutionDeviceLeaseRecord.device_id)
            ).scalars()
        )
        return ReservationSnapshot(
            execution_id=attempt.execution_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            intent_sha256=attempt.intent_sha256,
            admission_sha256=attempt.admission_sha256,
            node_id=attempt.node_id,
            node_inventory_sha256=attempt.node_inventory_sha256,
            status=attempt.status,
            state_version=attempt.state_version,
            fencing_epoch=attempt.fencing_epoch,
            resource_lease_sha256=resource.lease_sha256,
            selected_resource_ids=tuple(resource.lease_json["selected_resource_ids"]),
            cpu_cores=resource.cpu_cores,
            memory_bytes=resource.memory_bytes,
            scratch_bytes=resource.scratch_bytes,
            exclusive=resource.exclusive,
            device_leases=devices,
            budget_authorization_sha256=reservation.authorization_sha256,
            cost_quote_sha256=reservation.cost_quote_sha256,
            currency_code=reservation.currency_code,
            held_microunits=reservation.held_microunits,
            reserved_at=attempt.reserved_at,
            lease_expires_at=attempt.lease_expires_at,
            hard_deadline=attempt.hard_deadline,
            reconciliation_reason=attempt.reconciliation_reason,
        )
