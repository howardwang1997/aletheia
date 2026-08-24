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
from enum import StrEnum
from typing import Literal

from sqlalchemy import func, null, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, sessionmaker

from aletheia.db import session_factory
from aletheia.execution.assignment_contracts import (
    NodeAssignmentTransportPin,
    QualificationAssignmentSecret,
    SealedQualificationAssignment,
    seal_qualification_assignment,
)
from aletheia.execution.persistence import (
    _ExecutionAssignmentEnvelopeRecord,
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
    _ExecutionPreRuntimeAbsenceDecisionRecord,
    _ExecutionQualificationTerminalAcceptanceRecord,
    _ExecutionQualificationTerminalDeadlineExpirationRecord,
    _ExecutionQualificationTerminalOutboxRecord,
    _ExecutionQualificationAdmissionRecord,
    _ExecutionResourceLeaseRecord,
    _ExecutionRuntimeFenceRebindRecord,
    _ExecutionRuntimeLaunchAuthorizationRecord,
    _ExecutionRuntimeLaunchReceiptRecord,
    _ExecutionRuntimePreparationRecord,
    _ExecutionRuntimeTerminationAcceptanceRecord,
    _ExecutionRuntimeTerminationChallengeRecord,
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
    QualificationVerificationError,
    QualificationAuthorityVerifier,
    RuntimeInspectionReceipt,
    RuntimeInspectionState,
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
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    HistoricalPreRuntimeRecoveryLineage,
    HistoricalRuntimeRecoveryGrant,
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    NodeRuntimeLaunchReceipt,
    NodeRuntimeTerminationReceipt,
    PreRuntimeAbsenceReceipt,
    QualificationTerminalSubmission,
    QualificationTerminalDeadlineExpiration,
    RuntimeControlIssuancePort,
    RuntimeFenceRebindReceipt,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimePreparation,
    RuntimeTerminationAcceptanceChallenge,
    verify_accepted_qualification_terminal_submission,
    verify_accepted_runtime_termination,
    verify_historical_runtime_recovery_grant,
    verify_node_runtime_launch_receipt,
    verify_node_runtime_launch_receipt_historical,
    verify_pre_runtime_absence_receipt,
    verify_qualification_terminal_deadline_expiration,
    verify_runtime_fence_rebind_receipt,
    verify_runtime_launch_authorization_ticket_historical,
    verify_runtime_termination_acceptance_challenge,
    validate_runtime_terminal_evidence_refresh,
)
from aletheia.execution.schemas import (
    ArtifactManifest,
    ArtifactVerifiedReceipt,
    DataLocality,
    ExecutionIntent,
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


class RuntimeProofReplayRejectionCode(StrEnum):
    TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED = "termination_challenge_expired_unaccepted"
    PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED = "pre_runtime_absence_stale_uncommitted"


class RuntimeProofReplayRejected(LeaseAuthorityError):
    """Typed retry direction emitted only after proving no durable decision exists."""

    def __init__(self, code: RuntimeProofReplayRejectionCode) -> None:
        self.code = code
        super().__init__(code.value)


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
    grant_sha256: str
    bundle_sha256: str
    node_id: str
    node_inventory_sha256: str
    status: str
    state_version: int
    fencing_epoch: int
    lease_token_sha256: str
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
class SealedAssignmentDelivery:
    """Read-only node delivery; the only credential material remains inside ciphertext."""

    envelope: SealedQualificationAssignment
    bundle: EngineeringQualificationBundle
    grant: EngineeringQualificationGrant
    snapshot: ReservationSnapshot


@dataclass(frozen=True)
class QualificationAssignmentDelivery:
    """Exactly one initial, launched-recovery, or never-launched cleanup form."""

    bundle: EngineeringQualificationBundle
    grant: EngineeringQualificationGrant
    snapshot: ReservationSnapshot
    sealed_envelope: SealedQualificationAssignment | None = None
    historical_recovery_grant: HistoricalRuntimeRecoveryGrant | None = None
    historical_pre_runtime_recovery_lineage: HistoricalPreRuntimeRecoveryLineage | None = None

    def __post_init__(self) -> None:
        forms = (
            self.sealed_envelope,
            self.historical_recovery_grant,
            self.historical_pre_runtime_recovery_lineage,
        )
        if sum(item is not None for item in forms) != 1:
            raise ValueError("assignment delivery must contain exactly one initial/recovery form")
        if self.historical_recovery_grant is not None and (
            self.historical_recovery_grant.launch_allowed
            or not self.historical_recovery_grant.recovery_only
        ):
            raise ValueError("historical assignment delivery cannot authorize launch")
        lineage = self.historical_pre_runtime_recovery_lineage
        if lineage is not None and (
            not lineage.cleanup_only
            or lineage.launch_allowed
            or not lineage.qualification_only
            or lineage.scientific_admission_allowed
        ):
            raise ValueError("pre-runtime recovery delivery cannot authorize launch")


@dataclass(frozen=True)
class RuntimeStartCommit:
    snapshot: ReservationSnapshot
    launch_authorization: RuntimeLaunchAuthorization
    replayed: bool


@dataclass(frozen=True)
class RuntimeLaunchCommit:
    snapshot: ReservationSnapshot
    historical_recovery_grant: HistoricalRuntimeRecoveryGrant
    replayed: bool


@dataclass(frozen=True)
class RuntimeAbsenceCommit:
    snapshot: ReservationSnapshot
    disposition: str
    pre_runtime_absence_receipt_sha256: str
    replacement_launch_authorization_request: RuntimeLaunchAuthorizationRequest | None
    replacement_launch_authorization: RuntimeLaunchAuthorization | None
    replayed: bool


@dataclass(frozen=True)
class RuntimeAdoptionCommit:
    snapshot: ReservationSnapshot
    adoption_receipt_sha256: str
    runtime_fence_rebind_receipt_sha256: str
    replayed: bool


@dataclass(frozen=True)
class RuntimeTerminationChallengeCommit:
    snapshot: ReservationSnapshot
    challenge: RuntimeTerminationAcceptanceChallenge
    replayed: bool


@dataclass(frozen=True)
class RuntimeTerminationCommit:
    snapshot: ReservationSnapshot
    accepted_termination: AcceptedRuntimeTermination
    historical_recovery_grant: HistoricalRuntimeRecoveryGrant
    charged_microunits: int
    replayed: bool


@dataclass(frozen=True)
class RuntimeTerminalArtifactCommit:
    snapshot: ReservationSnapshot
    terminal_acceptance: AcceptedQualificationTerminalSubmission
    replayed: bool


@dataclass(frozen=True)
class QualificationTerminalCommit:
    snapshot: ReservationSnapshot
    outbox_id: str
    replayed: bool
    terminal_authority_kind: Literal["accepted_terminal_submission"] = (
        "accepted_terminal_submission"
    )


@dataclass(frozen=True)
class QualificationTerminalDeadlineExpirationCommit:
    snapshot: ReservationSnapshot
    terminal_expiration: QualificationTerminalDeadlineExpiration
    activated_at: datetime
    outbox_id: str
    replayed: bool
    terminal_authority_kind: Literal["terminal_deadline_expiration"] = (
        "terminal_deadline_expiration"
    )


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
        node_assignment_transport_pins: tuple[NodeAssignmentTransportPin, ...],
        terminal_verification_authority: TerminalVerificationAuthorityVerifier,
        allocator_principal_id: str,
        runtime_control_issuer: RuntimeControlIssuancePort | None = None,
        sessions: sessionmaker[Session] | Callable[[], Session] | None = None,
        max_inventory_ttl_seconds: int = 30,
        max_runtime_inspection_ttl_seconds: int = 30,
        heartbeat_extension_seconds: int = 15,
        max_runtime_launch_authorization_seconds: int = 30,
        max_runtime_proof_age_seconds: int = 30,
        artifact_submission_grace_seconds: int = 3600,
    ) -> None:
        if (
            max_inventory_ttl_seconds < 1
            or max_runtime_inspection_ttl_seconds < 1
            or heartbeat_extension_seconds < 1
            or not 1 <= max_runtime_launch_authorization_seconds <= 60
            or not 1 <= max_runtime_proof_age_seconds <= 60
            or artifact_submission_grace_seconds < 1
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
        self._node_assignment_transport_pins = {
            item.node_id: NodeAssignmentTransportPin.model_validate(item.model_dump(mode="python"))
            for item in node_assignment_transport_pins
        }
        if set(self._node_assignment_transport_pins) != set(self._node_authorities) or len(
            self._node_assignment_transport_pins
        ) != len(node_assignment_transport_pins):
            raise ValueError(
                "every enrolled node requires exactly one deployment-pinned assignment key"
            )
        transport_key_ids = tuple(item.transport_key_id for item in node_assignment_transport_pins)
        transport_principals = tuple(
            item.transport_principal_id for item in node_assignment_transport_pins
        )
        if len(set(transport_key_ids)) != len(transport_key_ids) or len(
            set(transport_principals)
        ) != len(transport_principals):
            raise ValueError("node assignment transport keys and principals must be unique")
        for node_id, transport_pin in self._node_assignment_transport_pins.items():
            node_authority = self._node_authorities[node_id]
            if (
                transport_pin.node_manifest_sha256 != node_authority.manifest.manifest_sha256
                or transport_pin.public_key_x25519_hex
                == node_authority.manifest.node_signing_public_key_ed25519_hex
                or transport_pin.transport_principal_id
                in {
                    node_authority.manifest.principal_id,
                    node_authority.enrollment_authority_pin.principal_id,
                }
            ):
                raise ValueError(
                    "node assignment transport must be exact and separate from node signing"
                )
        non_transport_principals = {
            self._authority.pin.principal_id,
            *(item.manifest.principal_id for item in node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in node_authorities),
        }
        if set(transport_principals) & non_transport_principals:
            raise ValueError("node assignment transport role must be independently declared")
        terminal_key_id = self._terminal_verification_authority.pin.key_id
        forbidden_key_ids = {
            self._authority.pin.key_id,
            *(item.manifest.node_signing_key_id for item in node_authorities),
            *(item.enrollment_authority_pin.key_id for item in node_authorities),
            *(item.transport_key_id for item in node_assignment_transport_pins),
        }
        terminal_principal_id = self._terminal_verification_authority.pin.principal_id
        authority_principal_ids = {
            self._authority.pin.principal_id,
            *(item.manifest.principal_id for item in node_authorities),
            *(item.enrollment_authority_pin.principal_id for item in node_authorities),
            *(item.transport_principal_id for item in node_assignment_transport_pins),
        }
        if self._allocator_principal_id in authority_principal_ids:
            raise ValueError("allocator role must be distinct from qualification and node roles")
        forbidden_principal_ids = authority_principal_ids | {self._allocator_principal_id}
        if terminal_key_id in forbidden_key_ids or terminal_principal_id in forbidden_principal_ids:
            raise ValueError(
                "terminal verification role must be distinct from qualification and node roles"
            )
        self._runtime_control_issuer = runtime_control_issuer
        if runtime_control_issuer is not None:
            try:
                runtime_pin = runtime_control_issuer.authority_pin
                runtime_verifier_pin = runtime_control_issuer.authority_verifier.pin
            except (AttributeError, TypeError, ValueError) as exc:
                raise TypeError(
                    "runtime-control issuer must expose one exact pinned verifier"
                ) from exc
            if runtime_pin != runtime_verifier_pin:
                raise ValueError("runtime-control issuer and verifier pins differ")
            if runtime_pin.key_id in forbidden_key_ids | {
                terminal_key_id
            } or runtime_pin.principal_id in forbidden_principal_ids | {terminal_principal_id}:
                raise ValueError(
                    "runtime-control role must be distinct from qualification, node, and terminal roles"
                )
        self._sessions = sessions or session_factory()
        self._max_inventory_ttl = timedelta(seconds=max_inventory_ttl_seconds)
        self._max_runtime_inspection_ttl = timedelta(seconds=max_runtime_inspection_ttl_seconds)
        self._max_runtime_inspection_age_seconds = max_runtime_inspection_ttl_seconds
        self._heartbeat_extension = timedelta(seconds=heartbeat_extension_seconds)
        self._max_runtime_launch_authorization_seconds = max_runtime_launch_authorization_seconds
        self._max_runtime_proof_age_seconds = max_runtime_proof_age_seconds
        self._artifact_submission_grace = timedelta(seconds=artifact_submission_grace_seconds)

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
            if (
                self._runtime_control_issuer is not None
                and intent.resource_request.artifact_quota_bytes
                < MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES
            ):
                raise AdmissionConflict(
                    "runtime-v2 artifact quota is below the deployable filesystem minimum"
                )
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
            transport_pin = self._node_assignment_transport_pins.get(node.node_id)
            if (
                transport_pin is None
                or transport_pin.node_manifest_sha256 != node.node_manifest_sha256
                or not transport_pin.active_at(now)
            ):
                raise AdmissionConflict(
                    "node assignment transport is absent, rebound, or inactive at locked DB time"
                )
            active_until = min(
                grant.message.expires_at,
                quote.expires_at,
                authorization.expires_at,
                intent.deadline,
                self._authority.pin.active_until,
                node_authority.active_until,
                terminal_pin.active_until,
                transport_pin.active_until,
            )
            hard_deadline = now + timedelta(seconds=quote.maximum_lease_seconds)
            if hard_deadline > active_until:
                raise AdmissionConflict(
                    "full quoted lease no longer fits inside every locked authority window"
                )
            if self._runtime_control_issuer is not None:
                artifact_submission_deadline = hard_deadline + self._artifact_submission_grace
                runtime_pin = self._runtime_control_issuer.authority_pin
                if not runtime_pin.active_at(now) or artifact_submission_deadline > min(
                    node_authority.active_until,
                    runtime_pin.active_until,
                    terminal_pin.active_until,
                ):
                    raise AdmissionConflict(
                        "runtime-v2 authority pins do not cover the fixed artifact grace"
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
            assignment_secret = QualificationAssignmentSecret(
                infrastructure_attempt_id=attempt_id,
                admission_sha256=admission_sha256,
                grant_sha256=grant.grant_sha256,
                bundle_sha256=bundle.bundle_sha256,
                node_id=node.node_id,
                node_manifest_sha256=node.node_manifest_sha256,
                resource_lease_sha256=lease_sha256,
                fencing_epoch=fencing_epoch,
                lease_token=raw_token,
                lease_token_sha256=token_hash,
                issued_at=now,
                expires_at=hard_deadline,
            )
            sealed_assignment = seal_qualification_assignment(
                secret=assignment_secret,
                transport_pin=transport_pin,
            )
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
            # establish the attempt/resource FK parents before adding their immutable envelope
            # and device leases.
            session.flush()
            session.add(
                _ExecutionAssignmentEnvelopeRecord(
                    assignment_envelope_sha256=sealed_assignment.envelope_sha256,
                    assignment_secret_sha256=(sealed_assignment.assignment_secret_sha256),
                    attempt_id=attempt_id,
                    admission_sha256=admission_sha256,
                    grant_sha256=grant.grant_sha256,
                    bundle_sha256=bundle.bundle_sha256,
                    node_id=node.node_id,
                    node_manifest_sha256=node.node_manifest_sha256,
                    resource_lease_sha256=lease_sha256,
                    initial_fencing_epoch=fencing_epoch,
                    lease_token_sha256=token_hash,
                    transport_pin_sha256=transport_pin.pin_sha256,
                    transport_key_id=transport_pin.transport_key_id,
                    transport_pin_json=_model_json(transport_pin),
                    payload_sha256=sealed_assignment.envelope_sha256,
                    payload_json=_model_json(sealed_assignment),
                    issued_at=sealed_assignment.issued_at,
                    expires_at=sealed_assignment.expires_at,
                    created_at=now,
                )
            )
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

    def pull_sealed_assignment(
        self,
        *,
        node_id: str,
        node_manifest_sha256: str,
    ) -> SealedAssignmentDelivery | None:
        """Return one replayable node ciphertext without exposing its raw lease token."""

        transport_pin = self._node_assignment_transport_pins.get(node_id)
        node_authority = self._node_authorities.get(node_id)
        if (
            transport_pin is None
            or node_authority is None
            or transport_pin.node_manifest_sha256 != node_manifest_sha256
            or node_authority.manifest.manifest_sha256 != node_manifest_sha256
        ):
            raise AdmissionConflict("assignment pull differs from deployment-pinned node identity")
        with self._sessions() as session:
            now = _database_time(session)
            if not transport_pin.active_at(now):
                raise AdmissionConflict("node assignment transport is inactive")
            attempt = session.execute(
                select(_ExecutionAttemptRecord)
                .join(
                    _ExecutionAssignmentEnvelopeRecord,
                    _ExecutionAssignmentEnvelopeRecord.attempt_id
                    == _ExecutionAttemptRecord.attempt_id,
                )
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.status.in_(ACTIVE_ATTEMPT_STATES),
                    _ExecutionAttemptRecord.node_runtime_launch_receipt_sha256.is_(None),
                    _ExecutionAssignmentEnvelopeRecord.expires_at > now,
                )
                .order_by(
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if attempt is None:
                return None
            envelope_record = session.execute(
                select(_ExecutionAssignmentEnvelopeRecord).where(
                    _ExecutionAssignmentEnvelopeRecord.attempt_id == attempt.attempt_id
                )
            ).scalar_one_or_none()
            admission = session.get(
                _ExecutionQualificationAdmissionRecord,
                attempt.admission_sha256,
            )
            if envelope_record is None or admission is None:
                raise AdmissionConflict("active attempt lacks immutable assignment authority")
            try:
                envelope = SealedQualificationAssignment.model_validate(
                    envelope_record.payload_json
                )
                bundle = EngineeringQualificationBundle.model_validate(admission.bundle_json)
                grant = EngineeringQualificationGrant.model_validate(admission.grant_json)
            except ValueError as exc:
                raise AdmissionConflict("stored assignment authority is not canonical") from exc
            expected = (
                envelope.envelope_sha256,
                envelope.assignment_secret_sha256,
                envelope.infrastructure_attempt_id,
                envelope.admission_sha256,
                envelope.grant_sha256,
                envelope.bundle_sha256,
                envelope.node_id,
                envelope.node_manifest_sha256,
                envelope.resource_lease_sha256,
                envelope.fencing_epoch,
                envelope.lease_token_sha256,
                envelope.transport_pin_sha256,
                envelope.transport_key_id,
                _model_json(envelope),
                envelope.issued_at,
                envelope.expires_at,
            )
            observed = (
                envelope_record.assignment_envelope_sha256,
                envelope_record.assignment_secret_sha256,
                envelope_record.attempt_id,
                envelope_record.admission_sha256,
                envelope_record.grant_sha256,
                envelope_record.bundle_sha256,
                envelope_record.node_id,
                envelope_record.node_manifest_sha256,
                envelope_record.resource_lease_sha256,
                envelope_record.initial_fencing_epoch,
                envelope_record.lease_token_sha256,
                envelope_record.transport_pin_sha256,
                envelope_record.transport_key_id,
                envelope_record.payload_json,
                envelope_record.issued_at,
                envelope_record.expires_at,
            )
            if (
                observed != expected
                or envelope_record.payload_sha256 != envelope.envelope_sha256
                or envelope_record.transport_pin_sha256 != transport_pin.pin_sha256
                or envelope_record.transport_pin_json != _model_json(transport_pin)
                or bundle.bundle_sha256 != attempt.bundle_sha256
                or grant.grant_sha256 != attempt.grant_sha256
                or admission.bundle_sha256 != bundle.bundle_sha256
                or admission.grant_sha256 != grant.grant_sha256
            ):
                raise AdmissionConflict("stored assignment envelope or admission is rebound")
            return SealedAssignmentDelivery(
                envelope=envelope,
                bundle=bundle,
                grant=grant,
                snapshot=self._snapshot(session, attempt),
            )

    def pull_assignment_delivery(
        self,
        *,
        node_id: str,
        node_manifest_sha256: str,
    ) -> QualificationAssignmentDelivery | None:
        """Prefer signed recovery authority; otherwise return one unexpired initial envelope."""

        transport_pin = self._node_assignment_transport_pins.get(node_id)
        node_authority = self._node_authorities.get(node_id)
        if (
            transport_pin is None
            or node_authority is None
            or transport_pin.node_manifest_sha256 != node_manifest_sha256
            or node_authority.manifest.manifest_sha256 != node_manifest_sha256
        ):
            raise AdmissionConflict("assignment pull differs from deployment-pinned node identity")
        issuer = self._require_runtime_control_issuer()
        with self._sessions() as session:
            now = _database_time(session)
            launch_candidate = session.execute(
                select(_ExecutionRuntimeLaunchReceiptRecord, _ExecutionAttemptRecord)
                .join(
                    _ExecutionAttemptRecord,
                    _ExecutionAttemptRecord.attempt_id
                    == _ExecutionRuntimeLaunchReceiptRecord.attempt_id,
                )
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.node_runtime_launch_receipt_sha256
                    == _ExecutionRuntimeLaunchReceiptRecord.launch_receipt_sha256,
                    _ExecutionAttemptRecord.status.in_(ACTIVE_ATTEMPT_STATES),
                    _ExecutionAttemptRecord.accepted_runtime_termination_sha256.is_(None),
                    _ExecutionRuntimeLaunchReceiptRecord.recovery_expires_at > now,
                )
                .order_by(
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).one_or_none()
            terminal_candidate = session.execute(
                select(
                    _ExecutionRuntimeTerminationAcceptanceRecord,
                    _ExecutionAttemptRecord,
                )
                .join(
                    _ExecutionAttemptRecord,
                    _ExecutionAttemptRecord.attempt_id
                    == _ExecutionRuntimeTerminationAcceptanceRecord.attempt_id,
                )
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.accepted_runtime_termination_sha256
                    == _ExecutionRuntimeTerminationAcceptanceRecord.accepted_termination_sha256,
                    _ExecutionAttemptRecord.status.in_(ACTIVE_ATTEMPT_STATES),
                    _ExecutionRuntimeTerminationAcceptanceRecord.recovery_expires_at > now,
                )
                .order_by(
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).one_or_none()
            candidates: list[
                tuple[
                    _ExecutionRuntimeLaunchReceiptRecord
                    | _ExecutionRuntimeTerminationAcceptanceRecord,
                    _ExecutionAttemptRecord,
                ]
            ] = []
            if launch_candidate is not None:
                candidates.append((launch_candidate[0], launch_candidate[1]))
            if terminal_candidate is not None:
                candidates.append((terminal_candidate[0], terminal_candidate[1]))
            if candidates:
                recovery_row, attempt = min(
                    candidates,
                    key=lambda item: (item[1].reserved_at, item[1].attempt_id),
                )
                admission = session.get(
                    _ExecutionQualificationAdmissionRecord,
                    attempt.admission_sha256,
                )
                if admission is None:
                    raise AdmissionConflict("runtime recovery lacks immutable admission authority")
                try:
                    bundle = EngineeringQualificationBundle.model_validate(admission.bundle_json)
                    grant = EngineeringQualificationGrant.model_validate(admission.grant_json)
                    recovery_grant = HistoricalRuntimeRecoveryGrant.model_validate(
                        recovery_row.recovery_grant_json
                    )
                    verify_historical_runtime_recovery_grant(
                        grant=recovery_grant,
                        authority=issuer.authority_verifier,
                        observed_at=now,
                    )
                except (TypeError, ValueError, QualificationVerificationError) as exc:
                    raise AdmissionConflict(
                        "stored historical runtime recovery authority is invalid"
                    ) from exc
                runtime_pin = issuer.authority_pin
                if (
                    recovery_row.recovery_grant_sha256 != recovery_grant.recovery_grant_sha256
                    or recovery_row.recovery_payload_sha256 != recovery_grant.recovery_grant_sha256
                    or recovery_row.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                    or recovery_row.runtime_control_pin_json != _model_json(runtime_pin)
                    or recovery_grant.infrastructure_attempt_id != attempt.attempt_id
                    or recovery_grant.execution_id != attempt.execution_id
                    or recovery_grant.intent_sha256 != attempt.intent_sha256
                    or recovery_grant.admission_sha256 != attempt.admission_sha256
                    or recovery_grant.qualification_grant_sha256 != attempt.grant_sha256
                    or recovery_grant.runtime_preparation_sha256
                    != attempt.runtime_preparation_sha256
                    or recovery_grant.node_runtime_launch_receipt_sha256
                    != attempt.node_runtime_launch_receipt_sha256
                    or recovery_grant.accepted_runtime_termination_sha256
                    != attempt.accepted_runtime_termination_sha256
                    or recovery_row.recovery_expires_at != recovery_grant.recovery_expires_at
                    or bundle.bundle_sha256 != attempt.bundle_sha256
                    or grant.grant_sha256 != attempt.grant_sha256
                    or recovery_grant.launch_allowed
                    or not recovery_grant.recovery_only
                ):
                    raise AdmissionConflict("historical runtime recovery authority is rebound")
                return QualificationAssignmentDelivery(
                    bundle=bundle,
                    grant=grant,
                    snapshot=self._snapshot(session, attempt),
                    historical_recovery_grant=recovery_grant,
                )
            prelaunch_attempt = session.execute(
                select(_ExecutionAttemptRecord)
                .join(
                    _ExecutionRuntimeLaunchAuthorizationRecord,
                    _ExecutionRuntimeLaunchAuthorizationRecord.authorization_sha256
                    == _ExecutionAttemptRecord.latest_runtime_launch_authorization_sha256,
                )
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.status.in_({"starting", "reconciliation_required"}),
                    _ExecutionAttemptRecord.runtime_preparation_sha256.is_not(None),
                    _ExecutionAttemptRecord.node_runtime_launch_receipt_sha256.is_(None),
                    _ExecutionAttemptRecord.accepted_runtime_termination_sha256.is_(None),
                    _ExecutionAttemptRecord.accepted_terminal_submission_sha256.is_(None),
                    _ExecutionAttemptRecord.hard_deadline > now - self._artifact_submission_grace,
                    ~select(_ExecutionRuntimeLaunchReceiptRecord.launch_receipt_sha256)
                    .where(
                        _ExecutionRuntimeLaunchReceiptRecord.attempt_id
                        == _ExecutionAttemptRecord.attempt_id
                    )
                    .exists(),
                )
                .order_by(
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if prelaunch_attempt is not None:
                admission = session.get(
                    _ExecutionQualificationAdmissionRecord,
                    prelaunch_attempt.admission_sha256,
                )
                if admission is None:
                    raise AdmissionConflict(
                        "pre-runtime recovery lacks immutable admission authority"
                    )
                try:
                    bundle = EngineeringQualificationBundle.model_validate(admission.bundle_json)
                    grant = EngineeringQualificationGrant.model_validate(admission.grant_json)
                    preparation, request, authorization = self._load_current_runtime_authorization(
                        session, prelaunch_attempt
                    )
                    lineage = HistoricalPreRuntimeRecoveryLineage(
                        runtime_preparation=preparation,
                        runtime_launch_authorization_request=request,
                        runtime_launch_authorization=authorization,
                    )
                except (
                    TypeError,
                    ValueError,
                    LeaseAuthorityError,
                    QualificationVerificationError,
                ) as exc:
                    raise AdmissionConflict(
                        "stored pre-runtime recovery lineage is invalid"
                    ) from exc
                if (
                    prelaunch_attempt.hard_deadline + self._artifact_submission_grace <= now
                    or preparation.node_id != node_id
                    or preparation.node_manifest_sha256 != node_manifest_sha256
                    or preparation.infrastructure_attempt_id != prelaunch_attempt.attempt_id
                    or preparation.execution_id != prelaunch_attempt.execution_id
                    or preparation.intent_sha256 != prelaunch_attempt.intent_sha256
                    or admission.infrastructure_attempt_id != prelaunch_attempt.attempt_id
                    or admission.execution_id != prelaunch_attempt.execution_id
                    or admission.intent_sha256 != prelaunch_attempt.intent_sha256
                    or admission.bundle_sha256 != bundle.bundle_sha256
                    or admission.grant_sha256 != grant.grant_sha256
                    or bundle.bundle_sha256 != prelaunch_attempt.bundle_sha256
                    or grant.grant_sha256 != prelaunch_attempt.grant_sha256
                ):
                    raise AdmissionConflict("stored pre-runtime recovery authority is rebound")
                return QualificationAssignmentDelivery(
                    bundle=bundle,
                    grant=grant,
                    snapshot=self._snapshot(session, prelaunch_attempt),
                    historical_pre_runtime_recovery_lineage=lineage,
                )
        initial = self.pull_sealed_assignment(
            node_id=node_id,
            node_manifest_sha256=node_manifest_sha256,
        )
        if initial is None:
            return None
        return QualificationAssignmentDelivery(
            bundle=initial.bundle,
            grant=initial.grant,
            snapshot=initial.snapshot,
            sealed_envelope=initial.envelope,
        )

    def authorize_runtime_start(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> RuntimeStartCommit:
        """Atomically persist one inert preparation and issue its short-lived launch ticket."""

        issuer = self._require_runtime_control_issuer()
        try:
            preparation = RuntimePreparation.model_validate(
                runtime_preparation.model_dump(mode="python")
            )
            authorization_request = RuntimeLaunchAuthorizationRequest.model_validate(
                launch_authorization_request.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("runtime start authority is not canonical") from exc
        if preparation.infrastructure_attempt_id != attempt_id:
            raise LeaseAuthorityError("runtime preparation belongs to another attempt")

        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            budget_head, node, _device_heads, resource, device_leases, reservation = (
                self._lock_runtime_holds(session, attempt)
            )
            now = _database_time(session)
            inventory = session.get(
                _ExecutionInventoryAttestationRecord,
                attempt.node_inventory_sha256,
            )
            if inventory is None:
                raise LeaseAuthorityError("runtime preparation lacks exact inventory authority")
            if (
                preparation.execution_id != attempt.execution_id
                or preparation.intent_sha256 != attempt.intent_sha256
                or preparation.node_id != attempt.node_id
                or preparation.node_manifest_sha256 != node.node_manifest_sha256
                or preparation.boot_id != node.boot_id
                or preparation.fencing_epoch != attempt.fencing_epoch
                or preparation.lease_token_sha256 != attempt.lease_token_sha256
                or not attempt.reserved_at <= preparation.prepared_at <= now
                or preparation.prepared_monotonic_ns < inventory.observed_monotonic_ns
                or authorization_request.runtime_preparation_sha256
                != preparation.preparation_sha256
                or authorization_request.infrastructure_attempt_id != attempt.attempt_id
                or authorization_request.fencing_epoch != attempt.fencing_epoch
                or authorization_request.lease_token_sha256 != attempt.lease_token_sha256
                or authorization_request.pre_runtime_absence_epoch
                != attempt.pre_runtime_absence_count
                or authorization_request.pre_runtime_absence_receipt_sha256
                != attempt.latest_pre_runtime_absence_receipt_sha256
                or not preparation.prepared_at <= authorization_request.requested_at <= now
                or authorization_request.requested_monotonic_ns < preparation.prepared_monotonic_ns
            ):
                raise LeaseAuthorityError(
                    "runtime preparation/request differs from locked attempt authority"
                )

            preparation_record = session.execute(
                select(_ExecutionRuntimePreparationRecord)
                .where(_ExecutionRuntimePreparationRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            preparation_payload = _model_json(preparation)
            if preparation_record is None:
                if attempt.runtime_preparation_sha256 is not None:
                    raise LeaseAuthorityError("runtime preparation head is orphaned")
                preparation_record = _ExecutionRuntimePreparationRecord(
                    preparation_sha256=preparation.preparation_sha256,
                    attempt_id=attempt.attempt_id,
                    execution_id=attempt.execution_id,
                    intent_sha256=attempt.intent_sha256,
                    node_id=attempt.node_id,
                    node_manifest_sha256=preparation.node_manifest_sha256,
                    boot_id=preparation.boot_id,
                    fencing_epoch=preparation.fencing_epoch,
                    lease_token_sha256=preparation.lease_token_sha256,
                    payload_sha256=preparation.preparation_sha256,
                    payload_json=preparation_payload,
                    prepared_at=preparation.prepared_at,
                    prepared_monotonic_ns=preparation.prepared_monotonic_ns,
                    recorded_at=now,
                )
                session.add(preparation_record)
                session.flush()
            elif (
                preparation_record.preparation_sha256 != preparation.preparation_sha256
                or preparation_record.payload_sha256 != preparation.preparation_sha256
                or preparation_record.payload_json != preparation_payload
                or attempt.runtime_preparation_sha256 != preparation_record.preparation_sha256
            ):
                raise LeaseAuthorityError("runtime preparation identity is rebound")

            replay_record = session.execute(
                select(_ExecutionRuntimeLaunchAuthorizationRecord).where(
                    _ExecutionRuntimeLaunchAuthorizationRecord.request_sha256
                    == authorization_request.request_sha256
                )
            ).scalar_one_or_none()
            if replay_record is not None:
                replay_request, replay_authorization = self._validated_launch_authority_record(
                    replay_record,
                    preparation=preparation,
                )
                if (
                    replay_request != authorization_request
                    or replay_record.attempt_id != attempt.attempt_id
                    or replay_record.sequence != attempt.runtime_launch_authorization_count
                    or replay_record.authorization_sha256
                    != attempt.latest_runtime_launch_authorization_sha256
                ):
                    raise LeaseAuthorityError("runtime launch request replay is rebound")
                return RuntimeStartCommit(
                    snapshot=self._snapshot(session, attempt),
                    launch_authorization=replay_authorization,
                    replayed=True,
                )
            if now >= attempt.lease_expires_at or now >= attempt.hard_deadline:
                raise LeaseAuthorityError("expired lease cannot receive runtime launch authority")
            self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            if attempt.status not in {"reserved", "starting"}:
                raise LeaseAuthorityError("attempt cannot receive another launch authorization")
            if attempt.node_runtime_launch_receipt_sha256 is not None:
                raise LeaseAuthorityError("already-launched attempt cannot be reauthorized")

            runtime_pin = issuer.authority_pin
            expires_at = min(
                now + timedelta(seconds=self._max_runtime_launch_authorization_seconds),
                attempt.lease_expires_at,
                attempt.hard_deadline,
                runtime_pin.active_until,
            )
            if expires_at <= now:
                raise LeaseAuthorityError("runtime-control launch window is empty")
            try:
                authorization = issuer.issue_launch_authorization(
                    authorization_request=authorization_request,
                    preparation=preparation,
                    admission_sha256=attempt.admission_sha256,
                    qualification_grant_sha256=attempt.grant_sha256,
                    lease_expires_at=attempt.lease_expires_at,
                    hard_deadline=attempt.hard_deadline,
                    issued_at=now,
                    expires_at=expires_at,
                    max_launch_delay_ns=(
                        self._max_runtime_launch_authorization_seconds * 1_000_000_000
                    ),
                )
                authorization = RuntimeLaunchAuthorization.model_validate(
                    authorization.model_dump(mode="python")
                )
                verify_runtime_launch_authorization_ticket_historical(
                    authorization=authorization,
                    authorization_request=authorization_request,
                    preparation=preparation,
                    authority=issuer.authority_verifier,
                )
            except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError(
                    "runtime-control issuer returned invalid authority"
                ) from exc
            if (
                authorization.admission_sha256 != attempt.admission_sha256
                or authorization.qualification_grant_sha256 != attempt.grant_sha256
                or authorization.issued_at != now
                or authorization.expires_at != expires_at
                or authorization.lease_expires_at != attempt.lease_expires_at
                or authorization.hard_deadline != attempt.hard_deadline
            ):
                raise LeaseAuthorityError("runtime launch ticket differs from requested DB scope")

            sequence = attempt.runtime_launch_authorization_count + 1
            request_payload = _model_json(authorization_request)
            authorization_payload = _model_json(authorization)
            session.add(
                _ExecutionRuntimeLaunchAuthorizationRecord(
                    authorization_sha256=authorization.authorization_sha256,
                    attempt_id=attempt.attempt_id,
                    preparation_sha256=preparation.preparation_sha256,
                    sequence=sequence,
                    request_sha256=authorization_request.request_sha256,
                    pre_runtime_absence_epoch=(authorization_request.pre_runtime_absence_epoch),
                    pre_runtime_absence_receipt_sha256=(
                        authorization_request.pre_runtime_absence_receipt_sha256
                    ),
                    request_payload_sha256=authorization_request.request_sha256,
                    request_json=request_payload,
                    authorization_payload_sha256=authorization.authorization_sha256,
                    authorization_json=authorization_payload,
                    runtime_control_pin_sha256=canonical_sha256(runtime_pin),
                    runtime_control_pin_json=_model_json(runtime_pin),
                    issued_at=authorization.issued_at,
                    expires_at=authorization.expires_at,
                    recorded_at=now,
                )
            )
            session.flush()
            attempt.runtime_preparation_sha256 = preparation.preparation_sha256
            attempt.runtime_launch_authorization_count = sequence
            attempt.latest_runtime_launch_authorization_sha256 = authorization.authorization_sha256
            attempt.status = "starting"
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeStartCommit(
                snapshot=self._snapshot(session, attempt),
                launch_authorization=authorization,
                replayed=False,
            )

    def accept_runtime_launch(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
    ) -> RuntimeLaunchCommit:
        """Accept fresh actual-engine evidence and persist recovery-only authority atomically."""

        issuer = self._require_runtime_control_issuer()
        try:
            receipt = NodeRuntimeLaunchReceipt.model_validate(
                node_runtime_launch_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("node runtime launch receipt is not canonical") from exc
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            _budget_head, node, _device_heads, resource, device_leases, reservation = (
                self._lock_runtime_holds(session, attempt)
            )
            now = _database_time(session)
            preparation, authorization_request, authorization = (
                self._load_current_runtime_authorization(session, attempt)
            )
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            existing = session.execute(
                select(_ExecutionRuntimeLaunchReceiptRecord)
                .where(_ExecutionRuntimeLaunchReceiptRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                stored_receipt = NodeRuntimeLaunchReceipt.model_validate(
                    existing.launch_receipt_json
                )
                recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                    existing.recovery_grant_json
                )
                try:
                    verify_node_runtime_launch_receipt_historical(
                        receipt=stored_receipt,
                        preparation=preparation,
                        launch_authorization_request=authorization_request,
                        launch_authorization=authorization,
                        authority=node_authority,
                        runtime_authority=issuer.authority_verifier,
                    )
                    self._verify_historical_recovery_record(recovery)
                except QualificationVerificationError as exc:
                    raise LeaseAuthorityError("stored runtime launch lineage is invalid") from exc
                if (
                    stored_receipt != receipt
                    or existing.launch_receipt_sha256 != receipt.launch_receipt_sha256
                    or attempt.node_runtime_launch_receipt_sha256 != existing.launch_receipt_sha256
                    or attempt.runtime_identity_sha256
                    != receipt.launch_evidence.runtime_identity_sha256
                ):
                    raise LeaseAuthorityError("runtime launch receipt replay is rebound")
                return RuntimeLaunchCommit(
                    snapshot=self._snapshot(session, attempt),
                    historical_recovery_grant=recovery,
                    replayed=True,
                )
            if attempt.status not in {"starting", "reconciliation_required"}:
                raise LeaseAuthorityError(
                    "attempt is not eligible for historical runtime launch recovery"
                )
            try:
                verify_node_runtime_launch_receipt(
                    receipt=receipt,
                    preparation=preparation,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                    observed_at=now,
                    maximum_age_seconds=self._max_runtime_proof_age_seconds,
                )
            except QualificationVerificationError as exc:
                raise LeaseAuthorityError("runtime launch receipt is stale or invalid") from exc
            runtime_identity = receipt.launch_evidence.runtime_identity
            runtime_pin = issuer.authority_pin
            recovery_expires_at = attempt.hard_deadline + self._artifact_submission_grace
            if now >= recovery_expires_at or recovery_expires_at > runtime_pin.active_until:
                raise LeaseAuthorityError(
                    "runtime-control pin cannot cover the fixed historical recovery window"
                )
            try:
                recovery = issuer.issue_historical_recovery(
                    admission_sha256=attempt.admission_sha256,
                    qualification_grant_sha256=attempt.grant_sha256,
                    intent_sha256=attempt.intent_sha256,
                    execution_id=attempt.execution_id,
                    infrastructure_attempt_id=attempt.attempt_id,
                    runtime_preparation_sha256=preparation.preparation_sha256,
                    node_runtime_launch_receipt_sha256=receipt.launch_receipt_sha256,
                    accepted_runtime_termination_sha256=None,
                    admitted_at=attempt.authorized_at,
                    hard_deadline=attempt.hard_deadline,
                    issued_at=now,
                    recovery_expires_at=recovery_expires_at,
                )
                recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                    recovery.model_dump(mode="python")
                )
                verify_historical_runtime_recovery_grant(
                    grant=recovery,
                    authority=issuer.authority_verifier,
                    observed_at=now,
                )
            except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError("historical recovery authority is invalid") from exc
            launch_payload = _model_json(receipt)
            recovery_payload = _model_json(recovery)
            session.add(
                _ExecutionRuntimeLaunchReceiptRecord(
                    launch_receipt_sha256=receipt.launch_receipt_sha256,
                    attempt_id=attempt.attempt_id,
                    preparation_sha256=preparation.preparation_sha256,
                    authorization_request_sha256=authorization_request.request_sha256,
                    authorization_sha256=authorization.authorization_sha256,
                    runtime_identity_sha256=runtime_identity.runtime_identity_sha256,
                    launch_payload_sha256=receipt.launch_receipt_sha256,
                    launch_receipt_json=launch_payload,
                    recovery_grant_sha256=recovery.recovery_grant_sha256,
                    recovery_payload_sha256=recovery.recovery_grant_sha256,
                    recovery_grant_json=recovery_payload,
                    recovery_expires_at=recovery.recovery_expires_at,
                    runtime_control_pin_sha256=canonical_sha256(runtime_pin),
                    runtime_control_pin_json=_model_json(runtime_pin),
                    signed_at=receipt.signed_at,
                    accepted_at=now,
                )
            )
            session.flush()
            attempt.node_runtime_launch_receipt_sha256 = receipt.launch_receipt_sha256
            attempt.runtime_identity_sha256 = runtime_identity.runtime_identity_sha256
            attempt.runtime_identity_json = _model_json(runtime_identity)
            keep_reconciliation = attempt.status == "reconciliation_required" or now >= min(
                attempt.lease_expires_at, attempt.hard_deadline
            )
            if keep_reconciliation:
                if (
                    resource.state not in {"held", "reconciliation_required"}
                    or reservation.state not in {"held", "reconciliation_required"}
                    or any(
                        item.state not in {"held", "reconciliation_required"}
                        for item in device_leases
                    )
                ):
                    raise LeaseAuthorityError(
                        "historical launch recovery differs from retained holds"
                    )
                entering_reconciliation = reservation.state == "held"
                resource.state = "reconciliation_required"
                reservation.state = "reconciliation_required"
                for item in device_leases:
                    item.state = "reconciliation_required"
                if entering_reconciliation:
                    self._append_budget_event(
                        session,
                        reservation_id=reservation.reservation_id,
                        authorization_sha256=reservation.authorization_sha256,
                        event_type="reconciliation_required",
                        reserved_delta_microunits=0,
                        spent_delta_microunits=0,
                        recorded_at=now,
                        details={
                            "reason": "historical_runtime_launch_recovery",
                            "node_runtime_launch_receipt_sha256": (receipt.launch_receipt_sha256),
                        },
                    )
                attempt.status = "reconciliation_required"
                attempt.reconciliation_reason = "lease_expired"
            else:
                if (
                    resource.state != "held"
                    or reservation.state != "held"
                    or any(item.state != "held" for item in device_leases)
                ):
                    raise LeaseAuthorityError("fresh launch differs from active retained holds")
                attempt.status = "running"
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeLaunchCommit(
                snapshot=self._snapshot(session, attempt),
                historical_recovery_grant=recovery,
                replayed=False,
            )

    def resolve_runtime_absence(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        absence_receipt: PreRuntimeAbsenceReceipt,
        replacement_launch_authorization_request: RuntimeLaunchAuthorizationRequest | None,
    ) -> RuntimeAbsenceCommit:
        """Accept a fresh never-started proof and either reauthorize or release every hold."""

        issuer = self._require_runtime_control_issuer()
        try:
            preparation = RuntimePreparation.model_validate(
                runtime_preparation.model_dump(mode="python")
            )
            receipt = PreRuntimeAbsenceReceipt.model_validate(
                absence_receipt.model_dump(mode="python")
            )
            replacement_request = (
                RuntimeLaunchAuthorizationRequest.model_validate(
                    replacement_launch_authorization_request.model_dump(mode="python")
                )
                if replacement_launch_authorization_request is not None
                else None
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("pre-runtime absence decision is not canonical") from exc
        with self._sessions() as session, session.begin():
            execution_head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            if attempt.node_runtime_launch_receipt_sha256 is not None:
                raise LeaseAuthorityError("a launched runtime cannot use pre-runtime absence")
            locked_holds = self._lock_runtime_holds(session, attempt)
            _budget_head, node, _device_heads, _resource, _devices, _reservation = locked_holds
            now = _database_time(session)
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            inventory = session.get(
                _ExecutionInventoryAttestationRecord,
                attempt.node_inventory_sha256,
            )
            if (
                inventory is None
                or preparation.execution_id != attempt.execution_id
                or preparation.intent_sha256 != attempt.intent_sha256
                or preparation.node_id != attempt.node_id
                or preparation.node_manifest_sha256 != node.node_manifest_sha256
                or preparation.boot_id != node.boot_id
                or preparation.fencing_epoch != attempt.fencing_epoch
                or preparation.lease_token_sha256 != attempt.lease_token_sha256
                or not attempt.reserved_at <= preparation.prepared_at <= now
                or preparation.prepared_monotonic_ns < inventory.observed_monotonic_ns
                or receipt.preparation != preparation
            ):
                raise LeaseAuthorityError(
                    "absence proof preparation differs from locked attempt authority"
                )
            preparation_record = session.execute(
                select(_ExecutionRuntimePreparationRecord)
                .where(_ExecutionRuntimePreparationRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            if preparation_record is None:
                if attempt.runtime_preparation_sha256 is not None:
                    raise LeaseAuthorityError("runtime preparation head is orphaned")
                session.add(
                    _ExecutionRuntimePreparationRecord(
                        preparation_sha256=preparation.preparation_sha256,
                        attempt_id=attempt.attempt_id,
                        execution_id=attempt.execution_id,
                        intent_sha256=attempt.intent_sha256,
                        node_id=attempt.node_id,
                        node_manifest_sha256=preparation.node_manifest_sha256,
                        boot_id=preparation.boot_id,
                        fencing_epoch=preparation.fencing_epoch,
                        lease_token_sha256=preparation.lease_token_sha256,
                        payload_sha256=preparation.preparation_sha256,
                        payload_json=_model_json(preparation),
                        prepared_at=preparation.prepared_at,
                        prepared_monotonic_ns=preparation.prepared_monotonic_ns,
                        recorded_at=now,
                    )
                )
                session.flush()
            else:
                try:
                    stored_preparation = RuntimePreparation.model_validate(
                        preparation_record.payload_json
                    )
                except ValueError as exc:
                    raise LeaseAuthorityError("stored runtime preparation is invalid") from exc
                if (
                    stored_preparation != preparation
                    or preparation_record.preparation_sha256 != preparation.preparation_sha256
                    or preparation_record.payload_sha256 != preparation.preparation_sha256
                    or preparation_record.attempt_id != attempt.attempt_id
                    or attempt.runtime_preparation_sha256 != preparation.preparation_sha256
                ):
                    raise LeaseAuthorityError("runtime preparation identity is rebound")

            prior_request: RuntimeLaunchAuthorizationRequest | None
            prior_authorization: RuntimeLaunchAuthorization | None
            if attempt.runtime_launch_authorization_count == 0:
                if attempt.latest_runtime_launch_authorization_sha256 is not None:
                    raise LeaseAuthorityError("runtime launch authority head is incomplete")
                prior_request = None
                prior_authorization = None
            else:
                stored_preparation, prior_request, prior_authorization = (
                    self._load_current_runtime_authorization(session, attempt)
                )
                if stored_preparation != preparation:
                    raise LeaseAuthorityError("absence proof changed durable preparation")

            existing = session.execute(
                select(_ExecutionPreRuntimeAbsenceDecisionRecord)
                .where(
                    _ExecutionPreRuntimeAbsenceDecisionRecord.absence_receipt_sha256
                    == receipt.absence_receipt_sha256
                )
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.attempt_id != attempt.attempt_id
                    or existing.absence_receipt_json != _model_json(receipt)
                    or existing.prior_authorization_request_sha256
                    != (prior_request.request_sha256 if prior_request else None)
                    or existing.prior_authorization_sha256
                    != (prior_authorization.authorization_sha256 if prior_authorization else None)
                    or existing.disposition
                    != ("reauthorized" if replacement_request is not None else "released")
                    or existing.replacement_request_sha256
                    != (replacement_request.request_sha256 if replacement_request else None)
                    or attempt.latest_pre_runtime_absence_receipt_sha256
                    != receipt.absence_receipt_sha256
                ):
                    raise LeaseAuthorityError("pre-runtime absence replay is rebound")
                replacement_authorization = None
                if existing.replacement_authorization_sha256 is not None:
                    replacement_record = session.get(
                        _ExecutionRuntimeLaunchAuthorizationRecord,
                        existing.replacement_authorization_sha256,
                    )
                    if replacement_record is None:
                        raise LeaseAuthorityError("absence replay lacks replacement authority")
                    replay_request, replacement_authorization = (
                        self._validated_launch_authority_record(
                            replacement_record,
                            preparation=preparation,
                        )
                    )
                    if replay_request != replacement_request:
                        raise LeaseAuthorityError("absence replacement request is rebound")
                return RuntimeAbsenceCommit(
                    snapshot=self._snapshot(session, attempt),
                    disposition=existing.disposition,
                    pre_runtime_absence_receipt_sha256=receipt.absence_receipt_sha256,
                    replacement_launch_authorization_request=replacement_request,
                    replacement_launch_authorization=replacement_authorization,
                    replayed=True,
                )
            try:
                verify_pre_runtime_absence_receipt(
                    receipt=receipt,
                    preparation=preparation,
                    authority=node_authority,
                    observed_at=now,
                    maximum_age_seconds=self._max_runtime_proof_age_seconds,
                    launch_authorization_request=prior_request,
                    launch_authorization=prior_authorization,
                    runtime_authority=(
                        issuer.authority_verifier if prior_authorization is not None else None
                    ),
                )
            except QualificationVerificationError as exc:
                try:
                    verify_pre_runtime_absence_receipt(
                        receipt=receipt,
                        preparation=preparation,
                        authority=node_authority,
                        observed_at=receipt.signed_at,
                        maximum_age_seconds=self._max_runtime_proof_age_seconds,
                        launch_authorization_request=prior_request,
                        launch_authorization=prior_authorization,
                        runtime_authority=(
                            issuer.authority_verifier if prior_authorization is not None else None
                        ),
                    )
                except QualificationVerificationError:
                    raise LeaseAuthorityError("pre-runtime absence proof is invalid") from exc
                raise RuntimeProofReplayRejected(
                    RuntimeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED
                ) from exc
            absence_epoch = attempt.pre_runtime_absence_count + 1
            if receipt.absence_evidence.prelaunch_absence_epoch != absence_epoch:
                raise LeaseAuthorityError("pre-runtime absence epoch is not the next durable value")

            replacement_authorization = None
            disposition = "released"
            replacement_authorization_sha256 = None
            if replacement_request is not None:
                if (
                    attempt.status not in {"reserved", "starting"}
                    or now >= min(attempt.lease_expires_at, attempt.hard_deadline)
                    or replacement_request.runtime_preparation_sha256
                    != preparation.preparation_sha256
                    or replacement_request.infrastructure_attempt_id != attempt.attempt_id
                    or replacement_request.fencing_epoch != attempt.fencing_epoch
                    or replacement_request.lease_token_sha256 != attempt.lease_token_sha256
                    or replacement_request.pre_runtime_absence_epoch != absence_epoch
                    or replacement_request.pre_runtime_absence_receipt_sha256
                    != receipt.absence_receipt_sha256
                    or replacement_request.requested_at < receipt.signed_at
                    or replacement_request.requested_at > now
                    or replacement_request.requested_monotonic_ns
                    < receipt.absence_evidence.inspected_monotonic_ns
                ):
                    raise LeaseAuthorityError(
                        "replacement launch request differs from fresh absence proof"
                    )
                runtime_pin = issuer.authority_pin
                expires_at = min(
                    now + timedelta(seconds=self._max_runtime_launch_authorization_seconds),
                    attempt.lease_expires_at,
                    attempt.hard_deadline,
                    runtime_pin.active_until,
                )
                if expires_at <= now:
                    raise LeaseAuthorityError("replacement runtime launch window is empty")
                try:
                    replacement_authorization = issuer.issue_launch_authorization(
                        authorization_request=replacement_request,
                        preparation=preparation,
                        admission_sha256=attempt.admission_sha256,
                        qualification_grant_sha256=attempt.grant_sha256,
                        lease_expires_at=attempt.lease_expires_at,
                        hard_deadline=attempt.hard_deadline,
                        issued_at=now,
                        expires_at=expires_at,
                        max_launch_delay_ns=(
                            self._max_runtime_launch_authorization_seconds * 1_000_000_000
                        ),
                    )
                    replacement_authorization = RuntimeLaunchAuthorization.model_validate(
                        replacement_authorization.model_dump(mode="python")
                    )
                    verify_runtime_launch_authorization_ticket_historical(
                        authorization=replacement_authorization,
                        authorization_request=replacement_request,
                        preparation=preparation,
                        authority=issuer.authority_verifier,
                    )
                except (
                    AttributeError,
                    TypeError,
                    ValueError,
                    QualificationVerificationError,
                ) as exc:
                    raise LeaseAuthorityError(
                        "replacement runtime-control authority is invalid"
                    ) from exc
                disposition = "reauthorized"
                replacement_authorization_sha256 = replacement_authorization.authorization_sha256
                runtime_pin_sha256 = canonical_sha256(runtime_pin)
                session.add(
                    _ExecutionRuntimeLaunchAuthorizationRecord(
                        authorization_sha256=replacement_authorization.authorization_sha256,
                        attempt_id=attempt.attempt_id,
                        preparation_sha256=preparation.preparation_sha256,
                        sequence=attempt.runtime_launch_authorization_count + 1,
                        request_sha256=replacement_request.request_sha256,
                        pre_runtime_absence_epoch=absence_epoch,
                        pre_runtime_absence_receipt_sha256=receipt.absence_receipt_sha256,
                        request_payload_sha256=replacement_request.request_sha256,
                        request_json=_model_json(replacement_request),
                        authorization_payload_sha256=(
                            replacement_authorization.authorization_sha256
                        ),
                        authorization_json=_model_json(replacement_authorization),
                        runtime_control_pin_sha256=runtime_pin_sha256,
                        runtime_control_pin_json=_model_json(runtime_pin),
                        issued_at=replacement_authorization.issued_at,
                        expires_at=replacement_authorization.expires_at,
                        recorded_at=now,
                    )
                )
            else:
                runtime_pin = issuer.authority_pin
                runtime_pin_sha256 = canonical_sha256(runtime_pin)

            decision_payload = {
                "schema_name": "aletheia.pre_runtime_absence_decision_record",
                "schema_version": 2,
                "attempt_id": attempt.attempt_id,
                "absence_epoch": absence_epoch,
                "absence_receipt_sha256": receipt.absence_receipt_sha256,
                "preparation_sha256": preparation.preparation_sha256,
                "prior_authorization_request_sha256": (
                    prior_request.request_sha256 if prior_request else None
                ),
                "prior_authorization_sha256": (
                    prior_authorization.authorization_sha256 if prior_authorization else None
                ),
                "disposition": disposition,
                "replacement_request_sha256": (
                    replacement_request.request_sha256 if replacement_request else None
                ),
                "replacement_authorization_sha256": replacement_authorization_sha256,
                "decided_at": now.isoformat(),
                "runtime_control_pin_sha256": runtime_pin_sha256,
                "qualification_only": True,
                "scientific_admission_allowed": False,
            }
            decision_sha256 = canonical_sha256(decision_payload)
            session.add(
                _ExecutionPreRuntimeAbsenceDecisionRecord(
                    decision_sha256=decision_sha256,
                    attempt_id=attempt.attempt_id,
                    absence_epoch=absence_epoch,
                    absence_receipt_sha256=receipt.absence_receipt_sha256,
                    preparation_sha256=preparation.preparation_sha256,
                    prior_authorization_request_sha256=(
                        prior_request.request_sha256 if prior_request else None
                    ),
                    prior_authorization_sha256=(
                        prior_authorization.authorization_sha256 if prior_authorization else None
                    ),
                    absence_payload_sha256=receipt.absence_receipt_sha256,
                    absence_receipt_json=_model_json(receipt),
                    disposition=disposition,
                    replacement_request_sha256=(
                        replacement_request.request_sha256 if replacement_request else None
                    ),
                    replacement_authorization_sha256=replacement_authorization_sha256,
                    decision_json=decision_payload,
                    runtime_control_pin_sha256=runtime_pin_sha256,
                    runtime_control_pin_json=_model_json(runtime_pin),
                    decided_at=now,
                )
            )
            session.flush()
            attempt.runtime_preparation_sha256 = preparation.preparation_sha256
            attempt.pre_runtime_absence_count = absence_epoch
            attempt.latest_pre_runtime_absence_receipt_sha256 = receipt.absence_receipt_sha256
            if replacement_authorization is not None:
                attempt.runtime_launch_authorization_count += 1
                attempt.latest_runtime_launch_authorization_sha256 = (
                    replacement_authorization.authorization_sha256
                )
                attempt.status = "starting"
            else:
                budget_head, node, device_heads, resource, devices, reservation = locked_holds
                with session.no_autoflush:
                    self._release_never_started_holds(
                        session,
                        execution_head=execution_head,
                        attempt=attempt,
                        budget_head=budget_head,
                        node=node,
                        device_heads=device_heads,
                        resource=resource,
                        device_leases=devices,
                        reservation=reservation,
                        now=now,
                        absence_receipt_sha256=receipt.absence_receipt_sha256,
                    )
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeAbsenceCommit(
                snapshot=self._snapshot(session, attempt),
                disposition=disposition,
                pre_runtime_absence_receipt_sha256=receipt.absence_receipt_sha256,
                replacement_launch_authorization_request=replacement_request,
                replacement_launch_authorization=replacement_authorization,
                replayed=False,
            )

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        new_lease_token: str,
        previous_lease_token: str | None = None,
        previous_fencing_epoch: int | None = None,
        runtime_fence_rebind_request: RuntimeFenceRebindRequest | None = None,
        runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt | None = None,
    ) -> AdoptionCommitReceipt:
        """Rotate one same-attempt fence only with fresh node-signed singleton evidence."""

        receipt = AttemptAdoptionReceipt.model_validate(receipt.model_dump(mode="python"))
        v2_values = (
            previous_lease_token,
            previous_fencing_epoch,
            runtime_fence_rebind_request,
            runtime_fence_rebind_receipt,
        )
        if any(item is not None for item in v2_values) != all(
            item is not None for item in v2_values
        ):
            raise LeaseAuthorityError(
                "runtime adoption requires the complete rebind request/receipt"
            )
        rebind_request = (
            RuntimeFenceRebindRequest.model_validate(
                runtime_fence_rebind_request.model_dump(mode="python")
            )
            if runtime_fence_rebind_request is not None
            else None
        )
        rebind_receipt = (
            RuntimeFenceRebindReceipt.model_validate(
                runtime_fence_rebind_receipt.model_dump(mode="python")
            )
            if runtime_fence_rebind_receipt is not None
            else None
        )
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
                if attempt.runtime_preparation_sha256 is not None:
                    existing_rebind = session.execute(
                        select(_ExecutionRuntimeFenceRebindRecord).where(
                            _ExecutionRuntimeFenceRebindRecord.adoption_sha256
                            == receipt.adoption_receipt_sha256
                        )
                    ).scalar_one_or_none()
                    if (
                        rebind_request is None
                        or rebind_receipt is None
                        or existing_rebind is None
                        or existing_rebind.request_json != _model_json(rebind_request)
                        or existing_rebind.receipt_json != _model_json(rebind_receipt)
                        or existing_rebind.rebind_receipt_sha256
                        != rebind_receipt.rebind_receipt_sha256
                    ):
                        raise LeaseAuthorityError("runtime adoption replay lacks exact rebind")
                return AdoptionCommitReceipt(
                    adoption_receipt_sha256=receipt.adoption_receipt_sha256,
                    snapshot=self._snapshot(session, attempt),
                    replayed=True,
                )
            if attempt.status not in {"running", "reconciliation_required"}:
                raise LeaseAuthorityError("only a running same runtime may be adopted")
            if (attempt.runtime_preparation_sha256 is not None) != (rebind_request is not None):
                raise LeaseAuthorityError(
                    "runtime-v2 adoption requires one full fence rebind; v1 cannot add one"
                )
            if attempt.runtime_identity_sha256 is None or attempt.runtime_identity_json is None:
                raise LeaseAuthorityError("attempt has no exact stored runtime identity")
            runtime_identity = NodeRuntimeIdentity.model_validate(attempt.runtime_identity_json)
            (
                _budget_head,
                node,
                device_heads,
                resource,
                device_leases,
                reservation,
            ) = self._lock_runtime_holds(session, attempt)
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
            if previous_lease_token is not None and (
                previous_fencing_epoch != attempt.fencing_epoch
                or _token_sha256(previous_lease_token) != attempt.lease_token_sha256
            ):
                raise LeaseAuthorityError("runtime adoption previous raw token/fence is stale")
            if rebind_request is not None and rebind_receipt is not None:
                preparation_record = session.get(
                    _ExecutionRuntimePreparationRecord,
                    attempt.runtime_preparation_sha256,
                )
                if preparation_record is None:
                    raise LeaseAuthorityError("runtime adoption lacks durable preparation")
                preparation = RuntimePreparation.model_validate(preparation_record.payload_json)
                if (
                    rebind_request.preparation_sha256 != preparation.preparation_sha256
                    or rebind_request.runtime_identity_sha256
                    != runtime_identity.runtime_identity_sha256
                    or rebind_request.previous_fencing_epoch != attempt.fencing_epoch
                    or rebind_request.previous_lease_token_sha256 != attempt.lease_token_sha256
                    or rebind_request.new_fencing_epoch != expected_new_fence
                    or rebind_request.new_lease_token_sha256 != receipt.new_lease_token_sha256
                    or rebind_request.rebind_sequence != receipt.adoption_sequence
                    or rebind_request.requested_at < receipt.runtime_inspection_receipt.inspected_at
                ):
                    raise LeaseAuthorityError("runtime fence rebind differs from adoption scope")
                try:
                    verified_rebind = verify_runtime_fence_rebind_receipt(
                        receipt=rebind_receipt,
                        request=rebind_request,
                        authority=node_authority,
                        observed_at=now,
                        maximum_age_seconds=self._max_runtime_proof_age_seconds,
                    )
                except QualificationVerificationError as exc:
                    raise LeaseAuthorityError(
                        "runtime fence rebind proof is stale or invalid"
                    ) from exc
                if (
                    verified_rebind.preparation_sha256 != preparation.preparation_sha256
                    or verified_rebind.runtime_identity_sha256
                    != runtime_identity.runtime_identity_sha256
                    or verified_rebind.new_fencing_epoch != expected_new_fence
                    or verified_rebind.new_lease_token_sha256 != receipt.new_lease_token_sha256
                ):
                    raise LeaseAuthorityError("verified runtime fence rebind is rebound")
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
            if rebind_request is not None and rebind_receipt is not None:
                evidence = rebind_receipt.evidence
                session.add(
                    _ExecutionRuntimeFenceRebindRecord(
                        rebind_receipt_sha256=rebind_receipt.rebind_receipt_sha256,
                        attempt_id=attempt.attempt_id,
                        adoption_sha256=receipt.adoption_receipt_sha256,
                        sequence=receipt.adoption_sequence,
                        request_sha256=rebind_request.request_sha256,
                        evidence_sha256=evidence.evidence_sha256,
                        preparation_sha256=rebind_request.preparation_sha256,
                        runtime_identity_sha256=rebind_request.runtime_identity_sha256,
                        previous_fencing_epoch=rebind_request.previous_fencing_epoch,
                        new_fencing_epoch=rebind_request.new_fencing_epoch,
                        previous_lease_token_sha256=(rebind_request.previous_lease_token_sha256),
                        new_lease_token_sha256=rebind_request.new_lease_token_sha256,
                        request_payload_sha256=rebind_request.request_sha256,
                        request_json=_model_json(rebind_request),
                        receipt_payload_sha256=rebind_receipt.rebind_receipt_sha256,
                        receipt_json=_model_json(rebind_receipt),
                        rebound_at=evidence.rebound_at,
                        accepted_at=now,
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

    def adopt_runtime_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
        runtime_fence_rebind_request: RuntimeFenceRebindRequest,
        runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt,
    ) -> RuntimeAdoptionCommit:
        committed = self.adopt_attempt(
            receipt=receipt,
            new_lease_token=new_lease_token,
            previous_lease_token=previous_lease_token,
            previous_fencing_epoch=previous_fencing_epoch,
            runtime_fence_rebind_request=runtime_fence_rebind_request,
            runtime_fence_rebind_receipt=runtime_fence_rebind_receipt,
        )
        return RuntimeAdoptionCommit(
            snapshot=committed.snapshot,
            adoption_receipt_sha256=committed.adoption_receipt_sha256,
            runtime_fence_rebind_receipt_sha256=(
                runtime_fence_rebind_receipt.rebind_receipt_sha256
            ),
            replayed=committed.replayed,
        )

    def issue_runtime_termination_challenge(
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
    ) -> RuntimeTerminationChallengeCommit:
        """Persist exact terminal engine evidence under a short-lived signed DB challenge."""

        issuer = self._require_runtime_control_issuer()
        try:
            supplied_preparation = RuntimePreparation.model_validate(
                runtime_preparation.model_dump(mode="python")
            )
            supplied_launch_receipt = NodeRuntimeLaunchReceipt.model_validate(
                node_runtime_launch_receipt.model_dump(mode="python")
            )
            evidence = RuntimeInspectionEvidence.model_validate(
                termination_evidence.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError(
                "runtime termination challenge input is not canonical"
            ) from exc
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            _budget_head, node, _device_heads, resource, _device_leases, _reservation = (
                self._lock_runtime_holds(session, attempt)
            )
            now = _database_time(session)
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            preparation, authorization_request, authorization, launch_receipt = (
                self._load_runtime_launch_lineage(
                    session,
                    attempt,
                    node_authority=node_authority,
                )
            )
            del authorization_request, authorization
            if (
                supplied_preparation != preparation
                or supplied_launch_receipt != launch_receipt
                or attempt.status not in {"running", "terminated", "reconciliation_required"}
                or inspection_sequence <= attempt.last_runtime_inspection_sequence
                or evidence.state is not RuntimeInspectionState.TERMINATED
                or evidence.preparation_sha256 != preparation.preparation_sha256
                or evidence.runtime_identity != launch_receipt.launch_evidence.runtime_identity
                or evidence.runtime_identity_sha256 != attempt.runtime_identity_sha256
                or evidence.enforced_placement_sha256 != preparation.enforced_placement_sha256
                or evidence.input_materialization_receipt_sha256
                != preparation.input_materialization_receipt_sha256
                or evidence.enforced_fencing_epoch != attempt.fencing_epoch
                or evidence.enforced_lease_token_sha256 != attempt.lease_token_sha256
                or evidence.engine_terminal_journal_sha256 is None
                or evidence.ended_at is None
                or evidence.exit_code is None
                or evidence.inspected_at > now
            ):
                raise LeaseAuthorityError("termination evidence differs from actual launch/fence")

            runtime_pin = issuer.authority_pin
            existing = session.execute(
                select(_ExecutionRuntimeTerminationChallengeRecord)
                .where(
                    _ExecutionRuntimeTerminationChallengeRecord.attempt_id == attempt_id,
                    _ExecutionRuntimeTerminationChallengeRecord.inspection_sequence
                    == inspection_sequence,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                    existing.challenge_json
                )
                self._verify_runtime_control_record_historical(
                    kind="runtime_termination_acceptance_challenge",
                    model=challenge,
                    payload=challenge.signature_payload,
                    signature_ed25519_hex=challenge.signature_ed25519_hex,
                    principal_id=challenge.challenged_by_principal_id,
                    key_id=challenge.challenge_key_id,
                    policy_sha256=challenge.runtime_control_policy_sha256,
                    signed_at=challenge.challenged_at,
                )
                if (
                    existing.challenge_sha256 != challenge.challenge_sha256
                    or existing.inspection_evidence_json != _model_json(evidence)
                    or existing.inspection_sequence != inspection_sequence
                    or existing.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                    or existing.runtime_control_pin_json != _model_json(runtime_pin)
                ):
                    raise LeaseAuthorityError("runtime termination challenge replay is rebound")
                accepted_row = session.execute(
                    select(_ExecutionRuntimeTerminationAcceptanceRecord).where(
                        _ExecutionRuntimeTerminationAcceptanceRecord.challenge_sha256
                        == challenge.challenge_sha256
                    )
                ).scalar_one_or_none()
                if accepted_row is None and now >= challenge.expires_at:
                    raise RuntimeProofReplayRejected(
                        RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                    )
                if attempt.runtime_termination_challenge_sha256 != challenge.challenge_sha256:
                    raise LeaseAuthorityError(
                        "runtime termination challenge replay is not the durable head"
                    )
                return RuntimeTerminationChallengeCommit(
                    snapshot=self._snapshot(session, attempt),
                    challenge=challenge,
                    replayed=True,
                )
            expected_artifact_deadline = attempt.hard_deadline + self._artifact_submission_grace
            latest_challenge = session.execute(
                select(_ExecutionRuntimeTerminationChallengeRecord)
                .where(_ExecutionRuntimeTerminationChallengeRecord.attempt_id == attempt_id)
                .order_by(_ExecutionRuntimeTerminationChallengeRecord.inspection_sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_challenge is not None:
                try:
                    previous_challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                        latest_challenge.challenge_json
                    )
                    self._verify_runtime_control_record_historical(
                        kind="runtime_termination_acceptance_challenge",
                        model=previous_challenge,
                        payload=previous_challenge.signature_payload,
                        signature_ed25519_hex=(previous_challenge.signature_ed25519_hex),
                        principal_id=previous_challenge.challenged_by_principal_id,
                        key_id=previous_challenge.challenge_key_id,
                        policy_sha256=(previous_challenge.runtime_control_policy_sha256),
                        signed_at=previous_challenge.challenged_at,
                    )
                except (TypeError, ValueError) as exc:
                    raise LeaseAuthorityError(
                        "stored termination challenge generation is invalid"
                    ) from exc
                prior_acceptance = session.execute(
                    select(_ExecutionRuntimeTerminationAcceptanceRecord).where(
                        _ExecutionRuntimeTerminationAcceptanceRecord.challenge_sha256
                        == previous_challenge.challenge_sha256
                    )
                ).scalar_one_or_none()
                if (
                    prior_acceptance is not None
                    or now < previous_challenge.expires_at
                    or latest_challenge.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                    or latest_challenge.runtime_control_pin_json != _model_json(runtime_pin)
                ):
                    raise LeaseAuthorityError(
                        "termination challenge generation is still live or accepted"
                    )
            expires_at = min(
                now + timedelta(seconds=self._max_runtime_proof_age_seconds),
                runtime_pin.active_until,
                node_authority.active_until,
                expected_artifact_deadline,
            )
            if (
                expires_at <= now
                or artifact_submission_deadline != expected_artifact_deadline
                or expected_artifact_deadline > runtime_pin.active_until
                or expected_artifact_deadline > node_authority.active_until
                or inspection_sequence
                != (
                    latest_challenge.inspection_sequence
                    if latest_challenge is not None
                    else attempt.last_runtime_inspection_sequence
                )
                + 1
            ):
                raise LeaseAuthorityError("termination/artifact proof windows exceed active pins")
            if latest_challenge is not None:
                try:
                    previous_evidence = RuntimeInspectionEvidence.model_validate(
                        latest_challenge.inspection_evidence_json
                    )
                    validate_runtime_terminal_evidence_refresh(
                        previous=previous_evidence,
                        refreshed=evidence,
                    )
                except (TypeError, ValueError, QualificationVerificationError) as exc:
                    raise LeaseAuthorityError(
                        "refreshed termination evidence changed immutable engine facts"
                    ) from exc
            try:
                challenge = issuer.issue_termination_challenge(
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    termination_evidence=evidence,
                    inspection_sequence=inspection_sequence,
                    node_inventory_sha256=attempt.node_inventory_sha256,
                    resource_lease_sha256=resource.lease_sha256,
                    fencing_epoch=attempt.fencing_epoch,
                    lease_token_sha256=attempt.lease_token_sha256,
                    hard_deadline=attempt.hard_deadline,
                    artifact_submission_deadline=artifact_submission_deadline,
                    challenged_at=now,
                    expires_at=expires_at,
                )
                challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                    challenge.model_dump(mode="python")
                )
                verify_runtime_termination_acceptance_challenge(
                    challenge=challenge,
                    authority=issuer.authority_verifier,
                    observed_at=now,
                )
            except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError("runtime termination challenge is invalid") from exc
            if (
                challenge.attempt_id != attempt.attempt_id
                or challenge.execution_id != attempt.execution_id
                or challenge.intent_sha256 != attempt.intent_sha256
                or challenge.node_manifest_sha256 != node.node_manifest_sha256
                or challenge.runtime_preparation_sha256 != preparation.preparation_sha256
                or challenge.node_runtime_launch_receipt_sha256
                != launch_receipt.launch_receipt_sha256
                or challenge.runtime_identity_sha256 != attempt.runtime_identity_sha256
                or challenge.runtime_inspection_evidence_sha256 != evidence.inspection_sha256
                or challenge.inspection_sequence != inspection_sequence
                or challenge.node_inventory_sha256 != attempt.node_inventory_sha256
                or challenge.resource_lease_sha256 != resource.lease_sha256
                or challenge.fencing_epoch != attempt.fencing_epoch
                or challenge.lease_token_sha256 != attempt.lease_token_sha256
                or challenge.hard_deadline != attempt.hard_deadline
                or challenge.artifact_submission_deadline != artifact_submission_deadline
                or challenge.challenged_at != now
                or challenge.expires_at != expires_at
            ):
                raise LeaseAuthorityError("issued termination challenge differs from DB scope")
            runtime_pin_sha256 = canonical_sha256(runtime_pin)
            session.add(
                _ExecutionRuntimeTerminationChallengeRecord(
                    challenge_sha256=challenge.challenge_sha256,
                    challenge_id=challenge.challenge_id,
                    attempt_id=attempt.attempt_id,
                    preparation_sha256=preparation.preparation_sha256,
                    launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
                    runtime_identity_sha256=challenge.runtime_identity_sha256,
                    inspection_evidence_sha256=evidence.inspection_sha256,
                    inspection_evidence_json=_model_json(evidence),
                    inspection_sequence=inspection_sequence,
                    challenge_payload_sha256=challenge.challenge_sha256,
                    challenge_json=_model_json(challenge),
                    runtime_control_pin_sha256=runtime_pin_sha256,
                    runtime_control_pin_json=_model_json(runtime_pin),
                    challenged_at=challenge.challenged_at,
                    expires_at=challenge.expires_at,
                )
            )
            session.flush()
            attempt.runtime_termination_challenge_count += 1
            attempt.runtime_termination_challenge_sha256 = challenge.challenge_sha256
            if attempt.status != "reconciliation_required":
                attempt.status = "terminated"
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeTerminationChallengeCommit(
                snapshot=self._snapshot(session, attempt),
                challenge=challenge,
                replayed=False,
            )

    def accept_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
    ) -> RuntimeTerminationCommit:
        """Accept fresh node termination and release compute/budget in the same transaction."""

        issuer = self._require_runtime_control_issuer()
        try:
            supplied_challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                challenge.model_dump(mode="python")
            )
            node_receipt = NodeRuntimeTerminationReceipt.model_validate(
                node_runtime_termination_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError(
                "runtime termination acceptance input is not canonical"
            ) from exc
        with self._sessions() as session, session.begin():
            _execution_head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            budget_head, node, device_heads, resource, device_leases, reservation = (
                self._lock_runtime_holds(session, attempt)
            )
            now = _database_time(session)
            runtime_pin = issuer.authority_pin
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            preparation, authorization_request, authorization, launch_receipt = (
                self._load_runtime_launch_lineage(
                    session,
                    attempt,
                    node_authority=node_authority,
                )
            )
            challenge_record = session.get(
                _ExecutionRuntimeTerminationChallengeRecord,
                supplied_challenge.challenge_sha256,
            )
            if (
                challenge_record is None
                or challenge_record.attempt_id != attempt.attempt_id
                or challenge_record.challenge_json != _model_json(supplied_challenge)
                or challenge_record.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                or challenge_record.runtime_control_pin_json != _model_json(runtime_pin)
                or attempt.runtime_termination_challenge_sha256
                != supplied_challenge.challenge_sha256
            ):
                raise LeaseAuthorityError("termination acceptance lacks its exact DB challenge")
            existing = session.execute(
                select(_ExecutionRuntimeTerminationAcceptanceRecord)
                .where(_ExecutionRuntimeTerminationAcceptanceRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                accepted = AcceptedRuntimeTermination.model_validate(
                    existing.accepted_termination_json
                )
                stored_node_receipt = NodeRuntimeTerminationReceipt.model_validate(
                    existing.node_termination_receipt_json
                )
                recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                    existing.recovery_grant_json
                )
                expiration = QualificationTerminalDeadlineExpiration.model_validate(
                    existing.conditional_terminal_expiration_json
                )
                try:
                    intent = ExecutionIntent.model_validate(attempt.intent_json)
                    verify_accepted_runtime_termination(
                        accepted=accepted,
                        challenge=supplied_challenge,
                        node_termination_receipt=stored_node_receipt,
                        preparation=preparation,
                        launch_receipt=launch_receipt,
                        launch_authorization_request=authorization_request,
                        launch_authorization=authorization,
                        node_authority=node_authority,
                        runtime_authority=issuer.authority_verifier,
                    )
                    self._verify_historical_recovery_record(recovery)
                    verify_qualification_terminal_deadline_expiration(
                        expiration=expiration,
                        intent=intent,
                        accepted=accepted,
                        challenge=supplied_challenge,
                        node_termination_receipt=stored_node_receipt,
                        preparation=preparation,
                        launch_receipt=launch_receipt,
                        launch_authorization_request=authorization_request,
                        launch_authorization=authorization,
                        expected_node_inventory_sha256=attempt.node_inventory_sha256,
                        expected_resource_lease_sha256=resource.lease_sha256,
                        node_authority=node_authority,
                        runtime_authority=issuer.authority_verifier,
                    )
                except (TypeError, ValueError, QualificationVerificationError) as exc:
                    raise LeaseAuthorityError("stored termination acceptance is invalid") from exc
                if (
                    stored_node_receipt != node_receipt
                    or existing.node_termination_receipt_json != _model_json(stored_node_receipt)
                    or existing.accepted_termination_sha256 != accepted.accepted_termination_sha256
                    or existing.accepted_termination_json != _model_json(accepted)
                    or attempt.accepted_runtime_termination_sha256
                    != accepted.accepted_termination_sha256
                    or existing.recovery_grant_sha256 != recovery.recovery_grant_sha256
                    or existing.recovery_payload_sha256 != recovery.recovery_grant_sha256
                    or existing.recovery_grant_json != _model_json(recovery)
                    or existing.recovery_expires_at != recovery.recovery_expires_at
                    or recovery.accepted_runtime_termination_sha256
                    != accepted.accepted_termination_sha256
                    or existing.conditional_terminal_expiration_sha256
                    != expiration.terminal_deadline_expiration_sha256
                    or existing.conditional_terminal_expiration_payload_sha256
                    != expiration.terminal_deadline_expiration_sha256
                    or existing.conditional_terminal_expiration_json != _model_json(expiration)
                    or existing.conditional_terminal_expiration_authorized_at
                    != expiration.authorized_at
                    or existing.conditional_terminal_expiration_expires_at != expiration.expired_at
                    or existing.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                    or existing.runtime_control_pin_json != _model_json(runtime_pin)
                ):
                    raise LeaseAuthorityError("runtime termination replay is rebound")
                return RuntimeTerminationCommit(
                    snapshot=self._snapshot(session, attempt),
                    accepted_termination=accepted,
                    historical_recovery_grant=recovery,
                    charged_microunits=reservation.settled_microunits,
                    replayed=True,
                )
            if attempt.status not in {"terminated", "reconciliation_required"}:
                raise LeaseAuthorityError("attempt is not awaiting termination acceptance")
            try:
                verify_runtime_termination_acceptance_challenge(
                    challenge=supplied_challenge,
                    authority=issuer.authority_verifier,
                    observed_at=now,
                )
            except QualificationVerificationError as exc:
                self._verify_runtime_control_record_historical(
                    kind="runtime_termination_acceptance_challenge",
                    model=supplied_challenge,
                    payload=supplied_challenge.signature_payload,
                    signature_ed25519_hex=supplied_challenge.signature_ed25519_hex,
                    principal_id=supplied_challenge.challenged_by_principal_id,
                    key_id=supplied_challenge.challenge_key_id,
                    policy_sha256=supplied_challenge.runtime_control_policy_sha256,
                    signed_at=supplied_challenge.challenged_at,
                )
                if now >= supplied_challenge.expires_at:
                    raise RuntimeProofReplayRejected(
                        RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                    ) from exc
                raise LeaseAuthorityError("runtime termination challenge is invalid") from exc
            try:
                accepted = issuer.issue_accepted_termination(
                    challenge=supplied_challenge,
                    node_termination_receipt=node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    node_authority=node_authority,
                    accepted_at=now,
                    billable_ended_at=node_receipt.termination_evidence.ended_at,
                    maximum_proof_age_seconds=self._max_runtime_proof_age_seconds,
                )
                accepted = AcceptedRuntimeTermination.model_validate(
                    accepted.model_dump(mode="python")
                )
                verify_accepted_runtime_termination(
                    accepted=accepted,
                    challenge=supplied_challenge,
                    node_termination_receipt=node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
            except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError("fresh runtime termination proof is invalid") from exc
            try:
                recovery = issuer.issue_historical_recovery(
                    admission_sha256=attempt.admission_sha256,
                    qualification_grant_sha256=attempt.grant_sha256,
                    intent_sha256=attempt.intent_sha256,
                    execution_id=attempt.execution_id,
                    infrastructure_attempt_id=attempt.attempt_id,
                    runtime_preparation_sha256=preparation.preparation_sha256,
                    node_runtime_launch_receipt_sha256=(launch_receipt.launch_receipt_sha256),
                    accepted_runtime_termination_sha256=(accepted.accepted_termination_sha256),
                    admitted_at=attempt.authorized_at,
                    hard_deadline=attempt.hard_deadline,
                    issued_at=now,
                    recovery_expires_at=accepted.artifact_submission_deadline,
                )
                recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                    recovery.model_dump(mode="python")
                )
                verify_historical_runtime_recovery_grant(
                    grant=recovery,
                    authority=issuer.authority_verifier,
                    observed_at=now,
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
                QualificationVerificationError,
            ) as exc:
                raise LeaseAuthorityError(
                    "accepted termination recovery authority is invalid"
                ) from exc
            try:
                intent = ExecutionIntent.model_validate(attempt.intent_json)
                expiration = issuer.issue_terminal_deadline_expiration(
                    intent=intent,
                    accepted=accepted,
                    challenge=supplied_challenge,
                    node_termination_receipt=node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                )
                expiration = QualificationTerminalDeadlineExpiration.model_validate(
                    expiration.model_dump(mode="python")
                )
                verify_qualification_terminal_deadline_expiration(
                    expiration=expiration,
                    intent=intent,
                    accepted=accepted,
                    challenge=supplied_challenge,
                    node_termination_receipt=node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=authorization_request,
                    launch_authorization=authorization,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
                QualificationVerificationError,
            ) as exc:
                raise LeaseAuthorityError(
                    "conditional terminal deadline authority is invalid"
                ) from exc
            if (
                expiration.authorized_at != now
                or expiration.expired_at != accepted.artifact_submission_deadline
                or expiration.accepted_runtime_termination_sha256
                != accepted.accepted_termination_sha256
            ):
                raise LeaseAuthorityError(
                    "conditional terminal deadline authority differs from DB scope"
                )
            session.add(
                _ExecutionRuntimeTerminationAcceptanceRecord(
                    accepted_termination_sha256=accepted.accepted_termination_sha256,
                    attempt_id=attempt.attempt_id,
                    challenge_sha256=supplied_challenge.challenge_sha256,
                    node_termination_receipt_sha256=(node_receipt.termination_receipt_sha256),
                    preparation_sha256=preparation.preparation_sha256,
                    launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
                    runtime_identity_sha256=accepted.runtime_identity_sha256,
                    termination_evidence_sha256=(
                        node_receipt.termination_evidence.inspection_sha256
                    ),
                    inspection_sequence=node_receipt.inspection_sequence,
                    node_receipt_payload_sha256=(node_receipt.termination_receipt_sha256),
                    node_termination_receipt_json=_model_json(node_receipt),
                    acceptance_payload_sha256=accepted.accepted_termination_sha256,
                    accepted_termination_json=_model_json(accepted),
                    recovery_grant_sha256=recovery.recovery_grant_sha256,
                    recovery_payload_sha256=recovery.recovery_grant_sha256,
                    recovery_grant_json=_model_json(recovery),
                    recovery_expires_at=recovery.recovery_expires_at,
                    conditional_terminal_expiration_sha256=(
                        expiration.terminal_deadline_expiration_sha256
                    ),
                    conditional_terminal_expiration_payload_sha256=(
                        expiration.terminal_deadline_expiration_sha256
                    ),
                    conditional_terminal_expiration_json=_model_json(expiration),
                    conditional_terminal_expiration_authorized_at=(expiration.authorized_at),
                    conditional_terminal_expiration_expires_at=expiration.expired_at,
                    runtime_control_pin_sha256=canonical_sha256(runtime_pin),
                    runtime_control_pin_json=_model_json(runtime_pin),
                    runtime_ended_at=accepted.runtime_ended_at,
                    accepted_at=accepted.accepted_at,
                )
            )
            session.flush()
            charged = self._release_terminated_holds(
                session,
                attempt=attempt,
                budget_head=budget_head,
                node=node,
                device_heads=device_heads,
                resource=resource,
                device_leases=device_leases,
                reservation=reservation,
                accepted=accepted,
                now=now,
            )
            terminal_evidence = node_receipt.termination_evidence
            attempt.accepted_runtime_termination_sha256 = accepted.accepted_termination_sha256
            attempt.last_runtime_inspection_sequence = node_receipt.inspection_sequence
            attempt.last_runtime_inspection_sha256 = terminal_evidence.inspection_sha256
            attempt.last_runtime_inspected_at = terminal_evidence.inspected_at
            attempt.last_runtime_inspected_monotonic_ns = terminal_evidence.inspected_monotonic_ns
            attempt.status = "verifying"
            attempt.reconciliation_reason = None
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeTerminationCommit(
                snapshot=self._snapshot(session, attempt),
                accepted_termination=accepted,
                historical_recovery_grant=recovery,
                charged_microunits=charged,
                replayed=False,
            )

    def replay_accepted_runtime_termination(
        self,
        *,
        recovery_grant: HistoricalRuntimeRecoveryGrant,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
        expected_accepted_runtime_termination_sha256: str,
    ) -> AcceptedRuntimeTermination:
        """Read an already-committed acceptance; this path can never sign, insert, or release."""

        issuer = self._require_runtime_control_issuer()
        try:
            supplied_recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                recovery_grant.model_dump(mode="python")
            )
            supplied_challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                challenge.model_dump(mode="python")
            )
            supplied_node_receipt = NodeRuntimeTerminationReceipt.model_validate(
                node_runtime_termination_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("accepted termination replay input is not canonical") from exc
        if (
            supplied_recovery.accepted_runtime_termination_sha256
            != expected_accepted_runtime_termination_sha256
            or supplied_recovery.launch_allowed
            or not supplied_recovery.recovery_only
        ):
            raise LeaseAuthorityError("accepted termination replay recovery scope is incomplete")
        attempt_id = supplied_recovery.infrastructure_attempt_id
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            accepted_record = session.execute(
                select(_ExecutionRuntimeTerminationAcceptanceRecord)
                .where(_ExecutionRuntimeTerminationAcceptanceRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_time(session)
            if accepted_record is None:
                raise LeaseAuthorityError(
                    "accepted termination replay cannot create a missing acceptance"
                )
            try:
                verify_historical_runtime_recovery_grant(
                    grant=supplied_recovery,
                    authority=issuer.authority_verifier,
                    observed_at=now,
                )
            except QualificationVerificationError as exc:
                raise LeaseAuthorityError(
                    "accepted termination replay recovery grant is inactive"
                ) from exc
            node = session.get(_ExecutionNodeRecord, attempt.node_id)
            if node is None:
                raise LeaseAuthorityError("accepted termination replay lacks its node")
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            preparation, request, authorization, launch_receipt = self._load_runtime_launch_lineage(
                session,
                attempt,
                node_authority=node_authority,
            )
            challenge_record = session.get(
                _ExecutionRuntimeTerminationChallengeRecord,
                supplied_challenge.challenge_sha256,
            )
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord).where(
                    _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
                )
            ).scalar_one()
            try:
                intent = ExecutionIntent.model_validate(attempt.intent_json)
                accepted = AcceptedRuntimeTermination.model_validate(
                    accepted_record.accepted_termination_json
                )
                stored_node_receipt = NodeRuntimeTerminationReceipt.model_validate(
                    accepted_record.node_termination_receipt_json
                )
                stored_recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                    accepted_record.recovery_grant_json
                )
                expiration = QualificationTerminalDeadlineExpiration.model_validate(
                    accepted_record.conditional_terminal_expiration_json
                )
                verify_accepted_runtime_termination(
                    accepted=accepted,
                    challenge=supplied_challenge,
                    node_termination_receipt=stored_node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=request,
                    launch_authorization=authorization,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
                self._verify_historical_recovery_record(stored_recovery)
                verify_qualification_terminal_deadline_expiration(
                    expiration=expiration,
                    intent=intent,
                    accepted=accepted,
                    challenge=supplied_challenge,
                    node_termination_receipt=stored_node_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=request,
                    launch_authorization=authorization,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
            except (TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError(
                    "stored accepted termination replay chain is invalid"
                ) from exc
            if (
                challenge_record is None
                or challenge_record.attempt_id != attempt.attempt_id
                or challenge_record.challenge_json != _model_json(supplied_challenge)
                or challenge_record.runtime_control_pin_sha256
                != canonical_sha256(issuer.authority_pin)
                or challenge_record.runtime_control_pin_json != _model_json(issuer.authority_pin)
                or stored_node_receipt != supplied_node_receipt
                or accepted_record.node_termination_receipt_json
                != _model_json(supplied_node_receipt)
                or accepted_record.accepted_termination_sha256
                != expected_accepted_runtime_termination_sha256
                or accepted.accepted_termination_sha256
                != expected_accepted_runtime_termination_sha256
                or attempt.accepted_runtime_termination_sha256
                != expected_accepted_runtime_termination_sha256
                or accepted_record.recovery_grant_sha256 != supplied_recovery.recovery_grant_sha256
                or accepted_record.recovery_payload_sha256
                != supplied_recovery.recovery_grant_sha256
                or accepted_record.recovery_grant_json != _model_json(supplied_recovery)
                or stored_recovery != supplied_recovery
                or accepted_record.conditional_terminal_expiration_sha256
                != expiration.terminal_deadline_expiration_sha256
                or accepted_record.conditional_terminal_expiration_payload_sha256
                != expiration.terminal_deadline_expiration_sha256
                or accepted_record.conditional_terminal_expiration_json != _model_json(expiration)
                or accepted_record.conditional_terminal_expiration_authorized_at
                != expiration.authorized_at
                or accepted_record.conditional_terminal_expiration_expires_at
                != expiration.expired_at
                or accepted_record.runtime_control_pin_sha256
                != canonical_sha256(issuer.authority_pin)
                or accepted_record.runtime_control_pin_json != _model_json(issuer.authority_pin)
            ):
                raise LeaseAuthorityError("accepted termination replay is rebound")
            return accepted

    def accept_terminal_artifacts(
        self,
        *,
        accepted_termination: AcceptedRuntimeTermination,
        terminal_submission: QualificationTerminalSubmission,
        artifact_manifest: ArtifactManifest,
        artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
    ) -> RuntimeTerminalArtifactCommit:
        """Linearize independently resolved artifact custody under a signed v2 acceptance."""

        issuer = self._require_runtime_control_issuer()
        try:
            supplied_accepted = AcceptedRuntimeTermination.model_validate(
                accepted_termination.model_dump(mode="python")
            )
            submission = QualificationTerminalSubmission.model_validate(
                terminal_submission.model_dump(mode="python")
            )
            manifest = ArtifactManifest.model_validate(artifact_manifest.model_dump(mode="python"))
            receipts = tuple(
                ArtifactVerifiedReceipt.model_validate(item.model_dump(mode="python"))
                for item in artifact_verified_receipts
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("terminal artifact input is not canonical") from exc
        if receipts != tuple(sorted(receipts, key=lambda item: item.artifact.artifact_key)) or len(
            {item.artifact.artifact_key for item in receipts}
        ) != len(receipts):
            raise LeaseAuthorityError(
                "terminal artifact receipts are not canonical by artifact key"
            )
        attempt_id = supplied_accepted.attempt_id
        with self._sessions() as session, session.begin():
            _execution_head, attempt = self._lock_execution_attempt(session, attempt_id)
            existing = session.execute(
                select(_ExecutionQualificationTerminalAcceptanceRecord)
                .where(_ExecutionQualificationTerminalAcceptanceRecord.attempt_id == attempt_id)
                .with_for_update()
            ).scalar_one_or_none()
            node = session.get(_ExecutionNodeRecord, attempt.node_id)
            if node is None:
                raise LeaseAuthorityError("terminal artifact acceptance lacks its node")
            authority_observed_at = existing.accepted_at if existing is not None else None
            now = _database_time(session)
            node_authority = self._locked_node_authority(
                node,
                observed_at=authority_observed_at or now,
                error_type=LeaseAuthorityError,
            )
            (
                preparation,
                request,
                authorization,
                launch_receipt,
                challenge,
                node_termination_receipt,
                durable_accepted,
                _termination_record,
            ) = self._load_accepted_runtime_termination_lineage(
                session,
                attempt,
                node_authority=node_authority,
            )
            try:
                intent = ExecutionIntent.model_validate(attempt.intent_json)
            except ValueError as exc:
                raise LeaseAuthorityError("stored terminal execution intent is invalid") from exc
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord).where(
                    _ExecutionResourceLeaseRecord.attempt_id == attempt_id
                )
            ).scalar_one()
            if (
                durable_accepted != supplied_accepted
                or supplied_accepted.accepted_termination_sha256
                != attempt.accepted_runtime_termination_sha256
                or intent.intent_sha256 != attempt.intent_sha256
            ):
                raise LeaseAuthorityError("terminal artifacts changed accepted runtime lineage")

            if existing is not None:
                try:
                    stored_submission = QualificationTerminalSubmission.model_validate(
                        existing.terminal_submission_json
                    )
                    stored_manifest = ArtifactManifest.model_validate(
                        existing.artifact_manifest_json
                    )
                    stored_receipts = tuple(
                        ArtifactVerifiedReceipt.model_validate(item)
                        for item in existing.artifact_verified_receipts_json
                    )
                    terminal_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                        existing.accepted_terminal_submission_json
                    )
                    verify_accepted_qualification_terminal_submission(
                        terminal_acceptance=terminal_acceptance,
                        submission=stored_submission,
                        intent=intent,
                        accepted=durable_accepted,
                        challenge=challenge,
                        node_termination_receipt=node_termination_receipt,
                        preparation=preparation,
                        launch_receipt=launch_receipt,
                        launch_authorization_request=request,
                        launch_authorization=authorization,
                        artifact_manifest=stored_manifest,
                        artifact_verified_receipts=stored_receipts,
                        expected_node_inventory_sha256=attempt.node_inventory_sha256,
                        expected_resource_lease_sha256=resource.lease_sha256,
                        node_authority=node_authority,
                        runtime_authority=issuer.authority_verifier,
                    )
                except (TypeError, ValueError, QualificationVerificationError) as exc:
                    raise LeaseAuthorityError(
                        "stored terminal artifact acceptance is invalid"
                    ) from exc
                if (
                    stored_submission != submission
                    or stored_manifest != manifest
                    or stored_receipts != receipts
                    or existing.accepted_terminal_submission_sha256
                    != terminal_acceptance.accepted_terminal_submission_sha256
                    or attempt.accepted_terminal_submission_sha256
                    != terminal_acceptance.accepted_terminal_submission_sha256
                ):
                    raise LeaseAuthorityError("terminal artifact acceptance replay is rebound")
                return RuntimeTerminalArtifactCommit(
                    snapshot=self._snapshot(session, attempt),
                    terminal_acceptance=terminal_acceptance,
                    replayed=True,
                )

            if (
                attempt.status != "verifying"
                or attempt.accepted_terminal_submission_sha256 is not None
                or attempt.terminal_deadline_expiration_sha256 is not None
                or now >= durable_accepted.artifact_submission_deadline
            ):
                raise LeaseAuthorityError("attempt is not inside terminal artifact grace")
            resolved_manifest = self._artifact_resolver.resolve_artifact_manifest(
                manifest_sha256=manifest.manifest_sha256,
                observed_at=now,
            )
            if resolved_manifest != manifest:
                raise LeaseAuthorityError(
                    "terminal artifact manifest is absent from exact pinned custody"
                )
            expected_entries = {item.artifact_key: item for item in manifest.entries}
            if len(expected_entries) != len(manifest.entries) or len(receipts) != len(
                manifest.entries
            ):
                raise LeaseAuthorityError(
                    "terminal artifact receipts do not exactly close the manifest"
                )
            for receipt in receipts:
                resolution = self._artifact_resolver.resolve_verified_input_artifact(
                    verified_receipt_sha256=receipt.verified_receipt_sha256,
                    observed_at=now,
                )
                expected_entry = expected_entries.get(receipt.artifact.artifact_key)
                if (
                    expected_entry is None
                    or receipt.artifact != expected_entry
                    or resolution is None
                    or resolution.verified_receipt != receipt
                    or resolution.artifact_manifest != manifest
                    or resolution.content_rehash_sha256 != receipt.artifact.content_sha256
                    or resolution.content_bytes != receipt.artifact.bytes
                    or resolution.resolved_at != now
                ):
                    raise LeaseAuthorityError(
                        "terminal artifact differs from fresh pinned CAS/custody resolution"
                    )
            try:
                terminal_acceptance = issuer.issue_terminal_submission_acceptance(
                    submission=submission,
                    intent=intent,
                    accepted=durable_accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=request,
                    launch_authorization=authorization,
                    artifact_manifest=manifest,
                    artifact_verified_receipts=receipts,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                    accepted_at=now,
                )
                terminal_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                    terminal_acceptance.model_dump(mode="python")
                )
                verify_accepted_qualification_terminal_submission(
                    terminal_acceptance=terminal_acceptance,
                    submission=submission,
                    intent=intent,
                    accepted=durable_accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=request,
                    launch_authorization=authorization,
                    artifact_manifest=manifest,
                    artifact_verified_receipts=receipts,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
                QualificationVerificationError,
            ) as exc:
                raise LeaseAuthorityError(
                    "terminal artifact submission lacks exact acceptance"
                ) from exc
            runtime_pin = issuer.authority_pin
            receipt_hashes = list(terminal_acceptance.artifact_verified_receipt_sha256s)
            session.add(
                _ExecutionQualificationTerminalAcceptanceRecord(
                    accepted_terminal_submission_sha256=(
                        terminal_acceptance.accepted_terminal_submission_sha256
                    ),
                    attempt_id=attempt.attempt_id,
                    accepted_runtime_termination_sha256=(
                        durable_accepted.accepted_termination_sha256
                    ),
                    terminal_submission_sha256=submission.terminal_submission_sha256,
                    artifact_manifest_sha256=manifest.manifest_sha256,
                    output_tree_sha256=terminal_acceptance.output_tree_sha256,
                    disposition=terminal_acceptance.disposition,
                    submission_payload_sha256=submission.terminal_submission_sha256,
                    terminal_submission_json=_model_json(submission),
                    manifest_payload_sha256=manifest.manifest_sha256,
                    artifact_manifest_json=_model_json(manifest),
                    artifact_verified_receipt_sha256s_json=receipt_hashes,
                    artifact_verified_receipts_json=[_model_json(item) for item in receipts],
                    acceptance_payload_sha256=(
                        terminal_acceptance.accepted_terminal_submission_sha256
                    ),
                    accepted_terminal_submission_json=_model_json(terminal_acceptance),
                    runtime_control_pin_sha256=canonical_sha256(runtime_pin),
                    runtime_control_pin_json=_model_json(runtime_pin),
                    accepted_at=terminal_acceptance.accepted_at,
                )
            )
            session.flush()
            attempt.accepted_terminal_submission_sha256 = (
                terminal_acceptance.accepted_terminal_submission_sha256
            )
            attempt.state_version += 1
            attempt.updated_at = now
            session.flush()
            return RuntimeTerminalArtifactCommit(
                snapshot=self._snapshot(session, attempt),
                terminal_acceptance=terminal_acceptance,
                replayed=False,
            )

    def adjudicate_expired_qualification_terminal(
        self,
        *,
        node_id: str,
        node_manifest_sha256: str,
    ) -> QualificationTerminalDeadlineExpirationCommit | None:
        """Activate one pre-signed no-artifact failure after its exact DB deadline."""

        issuer = self._require_runtime_control_issuer()
        pinned_node_authority = self._node_authorities.get(node_id)
        if (
            pinned_node_authority is None
            or pinned_node_authority.manifest.manifest_sha256 != node_manifest_sha256
        ):
            raise AdmissionConflict(
                "terminal deadline adjudication differs from deployment-pinned node identity"
            )
        with self._sessions() as session, session.begin():
            candidate_id = session.execute(
                select(_ExecutionAttemptRecord.attempt_id)
                .join(
                    _ExecutionRuntimeTerminationAcceptanceRecord,
                    _ExecutionRuntimeTerminationAcceptanceRecord.accepted_termination_sha256
                    == _ExecutionAttemptRecord.accepted_runtime_termination_sha256,
                )
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.status == "verifying",
                    _ExecutionAttemptRecord.accepted_runtime_termination_sha256.is_not(None),
                    _ExecutionAttemptRecord.accepted_terminal_submission_sha256.is_(None),
                    _ExecutionAttemptRecord.terminal_deadline_expiration_sha256.is_(None),
                    ~select(_ExecutionQualificationTerminalOutboxRecord.outbox_id)
                    .where(
                        _ExecutionQualificationTerminalOutboxRecord.attempt_id
                        == _ExecutionAttemptRecord.attempt_id
                    )
                    .exists(),
                )
                .order_by(
                    _ExecutionRuntimeTerminationAcceptanceRecord.conditional_terminal_expiration_expires_at,
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if candidate_id is None:
                return None
            execution_head, attempt = self._lock_execution_attempt(session, candidate_id)
            termination_record = session.execute(
                select(_ExecutionRuntimeTerminationAcceptanceRecord)
                .where(_ExecutionRuntimeTerminationAcceptanceRecord.attempt_id == candidate_id)
                .with_for_update()
            ).scalar_one_or_none()
            now = _database_time(session)
            if termination_record is None:
                raise AdmissionConflict(
                    "terminal deadline adjudication has an orphaned termination head"
                )
            node = session.get(_ExecutionNodeRecord, attempt.node_id)
            if (
                node is None
                or node.node_manifest_sha256 != node_manifest_sha256
                or attempt.node_id != node_id
            ):
                raise AdmissionConflict("terminal deadline adjudication node scope changed")
            node_authority = self._locked_node_authority(
                node,
                observed_at=termination_record.accepted_at,
                error_type=AdmissionConflict,
            )
            try:
                (
                    _preparation,
                    _request,
                    _authorization,
                    _launch_receipt,
                    _challenge,
                    _node_receipt,
                    accepted,
                    validated_termination_record,
                ) = self._load_accepted_runtime_termination_lineage(
                    session,
                    attempt,
                    node_authority=node_authority,
                )
                expiration = QualificationTerminalDeadlineExpiration.model_validate(
                    validated_termination_record.conditional_terminal_expiration_json
                )
            except (LeaseAuthorityError, TypeError, ValueError) as exc:
                raise AdmissionConflict(
                    "terminal deadline adjudication authority is invalid"
                ) from exc
            expiration_sha256 = expiration.terminal_deadline_expiration_sha256
            outbox_id = f"qto_{expiration_sha256}"
            existing_activation = session.get(
                _ExecutionQualificationTerminalDeadlineExpirationRecord,
                expiration_sha256,
            )
            existing_outbox = session.get(
                _ExecutionQualificationTerminalOutboxRecord,
                outbox_id,
            )
            if existing_activation is not None or existing_outbox is not None:
                if (
                    existing_activation is None
                    or existing_outbox is None
                    or existing_activation.attempt_id != attempt.attempt_id
                    or existing_activation.accepted_runtime_termination_sha256
                    != accepted.accepted_termination_sha256
                    or existing_activation.payload_sha256 != expiration_sha256
                    or existing_activation.payload_json != _model_json(expiration)
                    or existing_activation.runtime_control_pin_sha256
                    != canonical_sha256(issuer.authority_pin)
                    or existing_activation.runtime_control_pin_json
                    != _model_json(issuer.authority_pin)
                    or existing_activation.authorized_at != expiration.authorized_at
                    or existing_activation.expired_at != expiration.expired_at
                    or existing_activation.activated_at < expiration.expired_at
                    or existing_outbox.terminal_authority_kind != "terminal_deadline_expiration"
                    or existing_outbox.terminal_authority_sha256 != expiration_sha256
                    or existing_outbox.accepted_terminal_submission_sha256 is not None
                    or existing_outbox.terminal_deadline_expiration_sha256 != expiration_sha256
                    or existing_outbox.execution_id != attempt.execution_id
                    or existing_outbox.attempt_id != attempt.attempt_id
                    or existing_outbox.payload_sha256 != expiration_sha256
                    or existing_outbox.payload_json != _model_json(expiration)
                    or existing_outbox.created_at != existing_activation.activated_at
                    or attempt.terminal_deadline_expiration_sha256 != expiration_sha256
                    or attempt.accepted_terminal_submission_sha256 is not None
                    or attempt.status != "failed"
                    or execution_head.active_attempt_id is not None
                ):
                    raise AdmissionConflict("terminal deadline adjudication replay is rebound")
                return QualificationTerminalDeadlineExpirationCommit(
                    snapshot=self._snapshot(session, attempt),
                    terminal_expiration=expiration,
                    activated_at=existing_activation.activated_at,
                    outbox_id=outbox_id,
                    replayed=True,
                )
            if (
                attempt.status != "verifying"
                or attempt.accepted_runtime_termination_sha256
                != accepted.accepted_termination_sha256
                or attempt.accepted_terminal_submission_sha256 is not None
                or attempt.terminal_deadline_expiration_sha256 is not None
                or execution_head.active_attempt_id != attempt.attempt_id
                or now < expiration.expired_at
            ):
                return None
            runtime_pin = issuer.authority_pin
            activation = _ExecutionQualificationTerminalDeadlineExpirationRecord(
                terminal_deadline_expiration_sha256=expiration_sha256,
                attempt_id=attempt.attempt_id,
                accepted_runtime_termination_sha256=(accepted.accepted_termination_sha256),
                payload_sha256=expiration_sha256,
                payload_json=_model_json(expiration),
                runtime_control_pin_sha256=canonical_sha256(runtime_pin),
                runtime_control_pin_json=_model_json(runtime_pin),
                authorized_at=expiration.authorized_at,
                expired_at=expiration.expired_at,
                activated_at=now,
            )
            session.add(activation)
            session.add(
                _ExecutionQualificationTerminalOutboxRecord(
                    outbox_id=outbox_id,
                    terminal_authority_kind="terminal_deadline_expiration",
                    terminal_authority_sha256=expiration_sha256,
                    accepted_terminal_submission_sha256=None,
                    terminal_deadline_expiration_sha256=expiration_sha256,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.attempt_id,
                    topic="execution.qualification_terminal.v2",
                    delivery_key=(f"execution-v2:{attempt.execution_id}:{attempt.attempt_id}"),
                    payload_sha256=expiration_sha256,
                    payload_json=_model_json(expiration),
                    created_at=now,
                )
            )
            session.flush()
            attempt.terminal_deadline_expiration_sha256 = expiration_sha256
            attempt.status = "failed"
            attempt.state_version += 1
            attempt.updated_at = now
            execution_head.active_attempt_id = None
            execution_head.state_version += 1
            execution_head.updated_at = now
            session.flush()
            return QualificationTerminalDeadlineExpirationCommit(
                snapshot=self._snapshot(session, attempt),
                terminal_expiration=expiration,
                activated_at=now,
                outbox_id=outbox_id,
                replayed=False,
            )

    def pull_pending_qualification_terminal_settlement(
        self,
        *,
        node_id: str,
        node_manifest_sha256: str,
    ) -> AcceptedQualificationTerminalSubmission | None:
        """Read the oldest fully accepted terminal result that still needs v2 settlement."""

        node_authority = self._node_authorities.get(node_id)
        if (
            node_authority is None
            or node_authority.manifest.manifest_sha256 != node_manifest_sha256
        ):
            raise AdmissionConflict(
                "terminal settlement pull differs from deployment-pinned node identity"
            )
        self._require_runtime_control_issuer()
        with self._sessions() as session:
            attempt = session.execute(
                select(_ExecutionAttemptRecord)
                .where(
                    _ExecutionAttemptRecord.node_id == node_id,
                    _ExecutionAttemptRecord.status == "verifying",
                    _ExecutionAttemptRecord.accepted_terminal_submission_sha256.is_not(None),
                    ~select(_ExecutionQualificationTerminalOutboxRecord.outbox_id)
                    .where(
                        _ExecutionQualificationTerminalOutboxRecord.attempt_id
                        == _ExecutionAttemptRecord.attempt_id
                    )
                    .exists(),
                )
                .order_by(
                    _ExecutionAttemptRecord.reserved_at,
                    _ExecutionAttemptRecord.attempt_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if attempt is None:
                return None
            record = session.get(
                _ExecutionQualificationTerminalAcceptanceRecord,
                attempt.accepted_terminal_submission_sha256,
            )
            node = session.get(_ExecutionNodeRecord, attempt.node_id)
            if record is None or node is None:
                raise AdmissionConflict(
                    "pending terminal settlement has an orphaned authority head"
                )
            historical_node_authority = self._locked_node_authority(
                node,
                observed_at=record.accepted_at,
                error_type=AdmissionConflict,
            )
            try:
                return self._validated_qualification_terminal_acceptance_record(
                    session,
                    attempt=attempt,
                    record=record,
                    node_authority=historical_node_authority,
                )
            except LeaseAuthorityError as exc:
                raise AdmissionConflict("pending terminal settlement authority is invalid") from exc

    def settle_qualification_terminal(
        self,
        *,
        terminal_acceptance: AcceptedQualificationTerminalSubmission,
    ) -> QualificationTerminalCommit:
        """Atomically publish one v2 terminal intent and close the execution head."""

        issuer = self._require_runtime_control_issuer()
        try:
            supplied = AcceptedQualificationTerminalSubmission.model_validate(
                terminal_acceptance.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("qualification terminal settlement is not canonical") from exc
        with self._sessions() as session, session.begin():
            execution_head, attempt = self._lock_execution_attempt(session, supplied.attempt_id)
            record = session.execute(
                select(_ExecutionQualificationTerminalAcceptanceRecord)
                .where(
                    _ExecutionQualificationTerminalAcceptanceRecord.attempt_id
                    == supplied.attempt_id
                )
                .with_for_update()
            ).scalar_one_or_none()
            if record is None:
                raise LeaseAuthorityError(
                    "qualification terminal settlement lacks artifact acceptance"
                )
            node = session.get(_ExecutionNodeRecord, attempt.node_id)
            if node is None:
                raise LeaseAuthorityError("qualification terminal settlement lacks its node")
            node_authority = self._locked_node_authority(
                node,
                observed_at=record.accepted_at,
                error_type=LeaseAuthorityError,
            )
            validated_acceptance = self._validated_qualification_terminal_acceptance_record(
                session,
                attempt=attempt,
                record=record,
                node_authority=node_authority,
            )
            (
                preparation,
                request,
                authorization,
                launch_receipt,
                challenge,
                node_termination_receipt,
                accepted,
                _termination_record,
            ) = self._load_accepted_runtime_termination_lineage(
                session,
                attempt,
                node_authority=node_authority,
            )
            try:
                intent = ExecutionIntent.model_validate(attempt.intent_json)
                submission = QualificationTerminalSubmission.model_validate(
                    record.terminal_submission_json
                )
                manifest = ArtifactManifest.model_validate(record.artifact_manifest_json)
                receipts = tuple(
                    ArtifactVerifiedReceipt.model_validate(item)
                    for item in record.artifact_verified_receipts_json
                )
                stored_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                    record.accepted_terminal_submission_json
                )
            except (TypeError, ValueError) as exc:
                raise LeaseAuthorityError(
                    "stored qualification terminal settlement is invalid"
                ) from exc
            resource = session.execute(
                select(_ExecutionResourceLeaseRecord).where(
                    _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
                )
            ).scalar_one()
            try:
                verify_accepted_qualification_terminal_submission(
                    terminal_acceptance=stored_acceptance,
                    submission=submission,
                    intent=intent,
                    accepted=accepted,
                    challenge=challenge,
                    node_termination_receipt=node_termination_receipt,
                    preparation=preparation,
                    launch_receipt=launch_receipt,
                    launch_authorization_request=request,
                    launch_authorization=authorization,
                    artifact_manifest=manifest,
                    artifact_verified_receipts=receipts,
                    expected_node_inventory_sha256=attempt.node_inventory_sha256,
                    expected_resource_lease_sha256=resource.lease_sha256,
                    node_authority=node_authority,
                    runtime_authority=issuer.authority_verifier,
                )
            except QualificationVerificationError as exc:
                raise LeaseAuthorityError(
                    "stored qualification terminal authority is invalid"
                ) from exc
            runtime_pin = issuer.authority_pin
            if (
                validated_acceptance != supplied
                or stored_acceptance != supplied
                or record.accepted_terminal_submission_sha256
                != supplied.accepted_terminal_submission_sha256
                or record.acceptance_payload_sha256 != supplied.accepted_terminal_submission_sha256
                or record.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
                or record.runtime_control_pin_json != _model_json(runtime_pin)
                or attempt.accepted_terminal_submission_sha256
                != supplied.accepted_terminal_submission_sha256
            ):
                raise LeaseAuthorityError("qualification terminal settlement is rebound")
            outbox_id = f"qto_{supplied.accepted_terminal_submission_sha256}"
            existing_outbox = session.get(
                _ExecutionQualificationTerminalOutboxRecord,
                outbox_id,
            )
            terminal_status = (
                "succeeded" if supplied.disposition == "process_succeeded" else "failed"
            )
            if existing_outbox is not None:
                if (
                    existing_outbox.terminal_authority_kind != "accepted_terminal_submission"
                    or existing_outbox.terminal_authority_sha256
                    != supplied.accepted_terminal_submission_sha256
                    or existing_outbox.accepted_terminal_submission_sha256
                    != supplied.accepted_terminal_submission_sha256
                    or existing_outbox.terminal_deadline_expiration_sha256 is not None
                    or existing_outbox.attempt_id != attempt.attempt_id
                    or existing_outbox.execution_id != attempt.execution_id
                    or existing_outbox.payload_sha256
                    != supplied.accepted_terminal_submission_sha256
                    or existing_outbox.payload_json != _model_json(supplied)
                    or attempt.status != terminal_status
                    or execution_head.active_attempt_id is not None
                ):
                    raise LeaseAuthorityError("qualification terminal outbox replay is rebound")
                return QualificationTerminalCommit(
                    snapshot=self._snapshot(session, attempt),
                    outbox_id=outbox_id,
                    replayed=True,
                )
            if (
                attempt.status != "verifying"
                or execution_head.active_attempt_id != attempt.attempt_id
            ):
                raise LeaseAuthorityError(
                    "qualification terminal attempt is not ready for final settlement"
                )
            now = _database_time(session)
            session.add(
                _ExecutionQualificationTerminalOutboxRecord(
                    outbox_id=outbox_id,
                    terminal_authority_kind="accepted_terminal_submission",
                    terminal_authority_sha256=(supplied.accepted_terminal_submission_sha256),
                    accepted_terminal_submission_sha256=(
                        supplied.accepted_terminal_submission_sha256
                    ),
                    terminal_deadline_expiration_sha256=None,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.attempt_id,
                    topic="execution.qualification_terminal.v2",
                    delivery_key=(f"execution-v2:{attempt.execution_id}:{attempt.attempt_id}"),
                    payload_sha256=supplied.accepted_terminal_submission_sha256,
                    payload_json=_model_json(supplied),
                    created_at=now,
                )
            )
            session.flush()
            attempt.status = terminal_status
            attempt.state_version += 1
            attempt.updated_at = now
            execution_head.active_attempt_id = None
            execution_head.state_version += 1
            execution_head.updated_at = now
            session.flush()
            return QualificationTerminalCommit(
                snapshot=self._snapshot(session, attempt),
                outbox_id=outbox_id,
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

    def retain_runtime_reconciliation(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
        reason: str,
    ) -> AttemptTransitionReceipt:
        """Validate an exact UNKNOWN runtime observation and retain every existing hold."""

        if not reason:
            raise ValueError("runtime reconciliation reason must be nonempty")
        try:
            inspection = RuntimeInspectionReceipt.model_validate(
                inspection_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LeaseAuthorityError("runtime reconciliation inspection is not canonical") from exc
        with self._sessions() as session, session.begin():
            _head, attempt = self._lock_execution_attempt(session, attempt_id)
            self._verify_lease_authority(
                attempt,
                lease_token=lease_token,
                fencing_epoch=fencing_epoch,
            )
            _budget_head, node, _device_heads, resource, devices, reservation = (
                self._lock_runtime_holds(session, attempt)
            )
            now = _database_time(session)
            node_authority = self._locked_node_authority(
                node,
                observed_at=now,
                error_type=LeaseAuthorityError,
            )
            if attempt.runtime_identity_json is None:
                raise LeaseAuthorityError("runtime reconciliation lacks an exact identity")
            try:
                identity = NodeRuntimeIdentity.model_validate(attempt.runtime_identity_json)
                node_authority.verify_signature(
                    signing_key_id=inspection.signing_key_id,
                    message=inspection.signature_message,
                    signature_ed25519_hex=inspection.signature_ed25519_hex,
                    signed_at=inspection.inspected_at,
                )
            except (TypeError, ValueError, QualificationVerificationError) as exc:
                raise LeaseAuthorityError(
                    "runtime reconciliation inspection signature is invalid"
                ) from exc
            if (
                inspection.state is not RuntimeInspectionState.UNKNOWN
                or inspection.node_manifest_sha256 != node.node_manifest_sha256
                or inspection.runtime_identity != identity
                or inspection.runtime_identity_sha256 != attempt.runtime_identity_sha256
                or inspection.fencing_epoch != attempt.fencing_epoch
                or inspection.lease_token_sha256 != attempt.lease_token_sha256
                or not inspection.inspected_at <= now < inspection.expires_at
            ):
                raise LeaseAuthorityError(
                    "runtime reconciliation inspection differs from locked authority"
                )
            self._validate_runtime_inspection_window(
                attempt,
                inspection_sequence=inspection.inspection_sequence,
                inspection_sha256=inspection.inspection_receipt_sha256,
                inspected_at=inspection.inspected_at,
                inspected_monotonic_ns=inspection.inspected_monotonic_ns,
                expires_at=inspection.expires_at,
                observed_at=now,
            )
            if (
                resource.state not in {"held", "reconciliation_required"}
                or reservation.state not in {"held", "reconciliation_required"}
                or any(item.state not in {"held", "reconciliation_required"} for item in devices)
            ):
                raise LeaseAuthorityError("runtime reconciliation no longer owns retained holds")
            if attempt.status == "reconciliation_required":
                return AttemptTransitionReceipt(
                    snapshot=self._snapshot(session, attempt), replayed=True
                )
            if attempt.status not in {"starting", "running", "terminated"}:
                raise LeaseAuthorityError("attempt cannot retain runtime reconciliation")
            resource.state = "reconciliation_required"
            reservation.state = "reconciliation_required"
            for item in devices:
                item.state = "reconciliation_required"
            self._append_budget_event(
                session,
                reservation_id=reservation.reservation_id,
                authorization_sha256=reservation.authorization_sha256,
                event_type="reconciliation_required",
                reserved_delta_microunits=0,
                spent_delta_microunits=0,
                recorded_at=now,
                details={
                    "reason": reason,
                    "runtime_inspection_receipt_sha256": (inspection.inspection_receipt_sha256),
                },
            )
            attempt.status = "reconciliation_required"
            attempt.reconciliation_reason = reason
            attempt.state_version += 1
            attempt.updated_at = now
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
                        _ExecutionAttemptRecord.accepted_runtime_termination_sha256.is_(None),
                        _ExecutionAttemptRecord.accepted_terminal_submission_sha256.is_(None),
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
            if (
                attempt.accepted_runtime_termination_sha256 is not None
                or attempt.accepted_terminal_submission_sha256 is not None
            ):
                return None
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
        if (
            attempt.accepted_runtime_termination_sha256 is not None
            or attempt.accepted_terminal_submission_sha256 is not None
        ):
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

    def _require_runtime_control_issuer(self) -> RuntimeControlIssuancePort:
        issuer = self._runtime_control_issuer
        if issuer is None:
            raise LeaseAuthorityError(
                "runtime-v2 lifecycle is disabled without pinned runtime-control custody"
            )
        return issuer

    def _validated_launch_authority_record(
        self,
        record: _ExecutionRuntimeLaunchAuthorizationRecord,
        *,
        preparation: RuntimePreparation,
    ) -> tuple[RuntimeLaunchAuthorizationRequest, RuntimeLaunchAuthorization]:
        issuer = self._require_runtime_control_issuer()
        try:
            request = RuntimeLaunchAuthorizationRequest.model_validate(record.request_json)
            authorization = RuntimeLaunchAuthorization.model_validate(record.authorization_json)
            verify_runtime_launch_authorization_ticket_historical(
                authorization=authorization,
                authorization_request=request,
                preparation=preparation,
                authority=issuer.authority_verifier,
            )
        except (TypeError, ValueError, QualificationVerificationError) as exc:
            raise LeaseAuthorityError("stored runtime launch authority is invalid") from exc
        pin = issuer.authority_pin
        if (
            record.preparation_sha256 != preparation.preparation_sha256
            or record.request_sha256 != request.request_sha256
            or record.request_payload_sha256 != request.request_sha256
            or record.authorization_sha256 != authorization.authorization_sha256
            or record.authorization_payload_sha256 != authorization.authorization_sha256
            or record.pre_runtime_absence_epoch != request.pre_runtime_absence_epoch
            or record.pre_runtime_absence_receipt_sha256
            != request.pre_runtime_absence_receipt_sha256
            or record.request_json != _model_json(request)
            or record.authorization_json != _model_json(authorization)
            or record.runtime_control_pin_sha256 != canonical_sha256(pin)
            or record.runtime_control_pin_json != _model_json(pin)
        ):
            raise LeaseAuthorityError("stored runtime launch authority is rebound")
        return request, authorization

    def _load_current_runtime_authorization(
        self,
        session: Session,
        attempt: _ExecutionAttemptRecord,
    ) -> tuple[
        RuntimePreparation,
        RuntimeLaunchAuthorizationRequest,
        RuntimeLaunchAuthorization,
    ]:
        if (
            attempt.runtime_preparation_sha256 is None
            or attempt.latest_runtime_launch_authorization_sha256 is None
            or attempt.runtime_launch_authorization_count < 1
        ):
            raise LeaseAuthorityError("attempt lacks runtime preparation/launch authority")
        preparation_record = session.get(
            _ExecutionRuntimePreparationRecord,
            attempt.runtime_preparation_sha256,
        )
        authorization_record = session.get(
            _ExecutionRuntimeLaunchAuthorizationRecord,
            attempt.latest_runtime_launch_authorization_sha256,
        )
        if preparation_record is None or authorization_record is None:
            raise LeaseAuthorityError("runtime launch authority head is incomplete")
        try:
            preparation = RuntimePreparation.model_validate(preparation_record.payload_json)
        except ValueError as exc:
            raise LeaseAuthorityError("stored runtime preparation is invalid") from exc
        if (
            preparation_record.attempt_id != attempt.attempt_id
            or preparation_record.preparation_sha256 != preparation.preparation_sha256
            or preparation_record.payload_sha256 != preparation.preparation_sha256
            or preparation_record.payload_json != _model_json(preparation)
            or authorization_record.attempt_id != attempt.attempt_id
            or authorization_record.sequence != attempt.runtime_launch_authorization_count
        ):
            raise LeaseAuthorityError("stored runtime preparation/authorization is rebound")
        request, authorization = self._validated_launch_authority_record(
            authorization_record,
            preparation=preparation,
        )
        return preparation, request, authorization

    def _verify_historical_recovery_record(
        self,
        grant: HistoricalRuntimeRecoveryGrant,
    ) -> None:
        issuer = self._require_runtime_control_issuer()
        grant = HistoricalRuntimeRecoveryGrant.model_validate(grant.model_dump(mode="python"))
        issuer.authority_verifier.verify_historical(
            kind="historical_runtime_recovery_grant",
            payload=grant.signature_payload,
            signature_ed25519_hex=grant.signature_ed25519_hex,
            policy_sha256=grant.runtime_control_policy_sha256,
            principal_id=grant.authorized_by_principal_id,
            key_id=grant.authorization_key_id,
            signed_at=grant.issued_at,
        )
        if grant.launch_allowed or not grant.recovery_only:
            raise QualificationVerificationError("historical recovery record authorizes launch")

    def _verify_runtime_control_record_historical(
        self,
        *,
        kind: str,
        model: object,
        payload: dict[str, object],
        signature_ed25519_hex: str,
        principal_id: str,
        key_id: str,
        policy_sha256: str,
        signed_at: datetime,
    ) -> None:
        del model
        issuer = self._require_runtime_control_issuer()
        try:
            issuer.authority_verifier.verify_historical(
                kind=kind,
                payload=payload,
                signature_ed25519_hex=signature_ed25519_hex,
                policy_sha256=policy_sha256,
                principal_id=principal_id,
                key_id=key_id,
                signed_at=signed_at,
            )
        except QualificationVerificationError as exc:
            raise LeaseAuthorityError("stored runtime-control authority is invalid") from exc

    def _load_runtime_launch_lineage(
        self,
        session: Session,
        attempt: _ExecutionAttemptRecord,
        *,
        node_authority: WorkerNodeAuthorityVerifier,
    ) -> tuple[
        RuntimePreparation,
        RuntimeLaunchAuthorizationRequest,
        RuntimeLaunchAuthorization,
        NodeRuntimeLaunchReceipt,
    ]:
        issuer = self._require_runtime_control_issuer()
        preparation, request, authorization = self._load_current_runtime_authorization(
            session, attempt
        )
        if attempt.node_runtime_launch_receipt_sha256 is None:
            raise LeaseAuthorityError("attempt lacks a durable runtime launch receipt")
        record = session.get(
            _ExecutionRuntimeLaunchReceiptRecord,
            attempt.node_runtime_launch_receipt_sha256,
        )
        if record is None:
            raise LeaseAuthorityError("runtime launch receipt head is orphaned")
        try:
            receipt = NodeRuntimeLaunchReceipt.model_validate(record.launch_receipt_json)
            recovery = HistoricalRuntimeRecoveryGrant.model_validate(record.recovery_grant_json)
            verify_node_runtime_launch_receipt_historical(
                receipt=receipt,
                preparation=preparation,
                launch_authorization_request=request,
                launch_authorization=authorization,
                authority=node_authority,
                runtime_authority=issuer.authority_verifier,
            )
            self._verify_historical_recovery_record(recovery)
        except (TypeError, ValueError, QualificationVerificationError) as exc:
            raise LeaseAuthorityError("stored runtime launch lineage is invalid") from exc
        if (
            record.attempt_id != attempt.attempt_id
            or record.preparation_sha256 != preparation.preparation_sha256
            or record.authorization_request_sha256 != request.request_sha256
            or record.authorization_sha256 != authorization.authorization_sha256
            or record.launch_receipt_sha256 != receipt.launch_receipt_sha256
            or record.launch_payload_sha256 != receipt.launch_receipt_sha256
            or record.launch_receipt_json != _model_json(receipt)
            or record.runtime_identity_sha256 != receipt.launch_evidence.runtime_identity_sha256
            or record.recovery_grant_sha256 != recovery.recovery_grant_sha256
            or record.recovery_payload_sha256 != recovery.recovery_grant_sha256
            or record.recovery_grant_json != _model_json(recovery)
            or record.recovery_expires_at != recovery.recovery_expires_at
            or recovery.accepted_runtime_termination_sha256 is not None
            or record.runtime_control_pin_sha256 != canonical_sha256(issuer.authority_pin)
            or record.runtime_control_pin_json != _model_json(issuer.authority_pin)
            or attempt.runtime_identity_sha256 != record.runtime_identity_sha256
        ):
            raise LeaseAuthorityError("stored runtime launch lineage is rebound")
        return preparation, request, authorization, receipt

    def _load_accepted_runtime_termination_lineage(
        self,
        session: Session,
        attempt: _ExecutionAttemptRecord,
        *,
        node_authority: WorkerNodeAuthorityVerifier,
    ) -> tuple[
        RuntimePreparation,
        RuntimeLaunchAuthorizationRequest,
        RuntimeLaunchAuthorization,
        NodeRuntimeLaunchReceipt,
        RuntimeTerminationAcceptanceChallenge,
        NodeRuntimeTerminationReceipt,
        AcceptedRuntimeTermination,
        _ExecutionRuntimeTerminationAcceptanceRecord,
    ]:
        issuer = self._require_runtime_control_issuer()
        preparation, request, authorization, launch_receipt = self._load_runtime_launch_lineage(
            session,
            attempt,
            node_authority=node_authority,
        )
        if attempt.accepted_runtime_termination_sha256 is None:
            raise LeaseAuthorityError("attempt lacks accepted runtime termination")
        record = session.get(
            _ExecutionRuntimeTerminationAcceptanceRecord,
            attempt.accepted_runtime_termination_sha256,
        )
        if record is None:
            raise LeaseAuthorityError("accepted runtime termination head is orphaned")
        challenge_record = session.get(
            _ExecutionRuntimeTerminationChallengeRecord,
            record.challenge_sha256,
        )
        if challenge_record is None:
            raise LeaseAuthorityError("accepted runtime termination lacks its challenge")
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        try:
            intent = ExecutionIntent.model_validate(attempt.intent_json)
            challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                challenge_record.challenge_json
            )
            node_receipt = NodeRuntimeTerminationReceipt.model_validate(
                record.node_termination_receipt_json
            )
            accepted = AcceptedRuntimeTermination.model_validate(record.accepted_termination_json)
            recovery = HistoricalRuntimeRecoveryGrant.model_validate(record.recovery_grant_json)
            expiration = QualificationTerminalDeadlineExpiration.model_validate(
                record.conditional_terminal_expiration_json
            )
            verify_accepted_runtime_termination(
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_receipt,
                preparation=preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=request,
                launch_authorization=authorization,
                node_authority=node_authority,
                runtime_authority=issuer.authority_verifier,
            )
            self._verify_historical_recovery_record(recovery)
            verify_qualification_terminal_deadline_expiration(
                expiration=expiration,
                intent=intent,
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_receipt,
                preparation=preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=request,
                launch_authorization=authorization,
                expected_node_inventory_sha256=attempt.node_inventory_sha256,
                expected_resource_lease_sha256=resource.lease_sha256,
                node_authority=node_authority,
                runtime_authority=issuer.authority_verifier,
            )
        except (TypeError, ValueError, QualificationVerificationError) as exc:
            raise LeaseAuthorityError("stored accepted runtime termination is invalid") from exc
        runtime_pin = issuer.authority_pin
        if (
            challenge_record.attempt_id != attempt.attempt_id
            or challenge_record.challenge_sha256 != challenge.challenge_sha256
            or challenge_record.challenge_payload_sha256 != challenge.challenge_sha256
            or challenge_record.challenge_json != _model_json(challenge)
            or challenge_record.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
            or challenge_record.runtime_control_pin_json != _model_json(runtime_pin)
            or record.attempt_id != attempt.attempt_id
            or record.accepted_termination_sha256 != accepted.accepted_termination_sha256
            or record.challenge_sha256 != challenge.challenge_sha256
            or record.preparation_sha256 != preparation.preparation_sha256
            or record.launch_receipt_sha256 != launch_receipt.launch_receipt_sha256
            or record.runtime_identity_sha256 != accepted.runtime_identity_sha256
            or record.termination_evidence_sha256
            != node_receipt.termination_evidence.inspection_sha256
            or record.inspection_sequence != accepted.inspection_sequence
            or record.node_termination_receipt_sha256 != node_receipt.termination_receipt_sha256
            or record.node_receipt_payload_sha256 != node_receipt.termination_receipt_sha256
            or record.node_termination_receipt_json != _model_json(node_receipt)
            or record.acceptance_payload_sha256 != accepted.accepted_termination_sha256
            or record.accepted_termination_json != _model_json(accepted)
            or record.recovery_grant_sha256 != recovery.recovery_grant_sha256
            or record.recovery_payload_sha256 != recovery.recovery_grant_sha256
            or record.recovery_grant_json != _model_json(recovery)
            or record.recovery_expires_at != recovery.recovery_expires_at
            or recovery.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
            or record.conditional_terminal_expiration_sha256
            != expiration.terminal_deadline_expiration_sha256
            or record.conditional_terminal_expiration_payload_sha256
            != expiration.terminal_deadline_expiration_sha256
            or record.conditional_terminal_expiration_json != _model_json(expiration)
            or record.conditional_terminal_expiration_authorized_at != expiration.authorized_at
            or record.conditional_terminal_expiration_expires_at != expiration.expired_at
            or record.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
            or record.runtime_control_pin_json != _model_json(runtime_pin)
            or record.runtime_ended_at != accepted.runtime_ended_at
            or record.accepted_at != accepted.accepted_at
        ):
            raise LeaseAuthorityError("stored accepted runtime termination is rebound")
        return (
            preparation,
            request,
            authorization,
            launch_receipt,
            challenge,
            node_receipt,
            accepted,
            record,
        )

    def _validated_qualification_terminal_acceptance_record(
        self,
        session: Session,
        *,
        attempt: _ExecutionAttemptRecord,
        record: _ExecutionQualificationTerminalAcceptanceRecord,
        node_authority: WorkerNodeAuthorityVerifier,
    ) -> AcceptedQualificationTerminalSubmission:
        issuer = self._require_runtime_control_issuer()
        (
            preparation,
            request,
            authorization,
            launch_receipt,
            challenge,
            node_termination_receipt,
            accepted,
            _termination_record,
        ) = self._load_accepted_runtime_termination_lineage(
            session,
            attempt,
            node_authority=node_authority,
        )
        try:
            intent = ExecutionIntent.model_validate(attempt.intent_json)
            submission = QualificationTerminalSubmission.model_validate(
                record.terminal_submission_json
            )
            manifest = ArtifactManifest.model_validate(record.artifact_manifest_json)
            receipts = tuple(
                ArtifactVerifiedReceipt.model_validate(item)
                for item in record.artifact_verified_receipts_json
            )
            terminal_acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                record.accepted_terminal_submission_json
            )
        except (TypeError, ValueError) as exc:
            raise LeaseAuthorityError(
                "stored qualification terminal acceptance is invalid"
            ) from exc
        if receipts != tuple(sorted(receipts, key=lambda item: item.artifact.artifact_key)) or len(
            {item.artifact.artifact_key for item in receipts}
        ) != len(receipts):
            raise LeaseAuthorityError("stored qualification terminal receipts are not canonical")
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord).where(
                _ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        try:
            verify_accepted_qualification_terminal_submission(
                terminal_acceptance=terminal_acceptance,
                submission=submission,
                intent=intent,
                accepted=accepted,
                challenge=challenge,
                node_termination_receipt=node_termination_receipt,
                preparation=preparation,
                launch_receipt=launch_receipt,
                launch_authorization_request=request,
                launch_authorization=authorization,
                artifact_manifest=manifest,
                artifact_verified_receipts=receipts,
                expected_node_inventory_sha256=attempt.node_inventory_sha256,
                expected_resource_lease_sha256=resource.lease_sha256,
                node_authority=node_authority,
                runtime_authority=issuer.authority_verifier,
            )
        except QualificationVerificationError as exc:
            raise LeaseAuthorityError(
                "stored qualification terminal acceptance authority is invalid"
            ) from exc
        runtime_pin = issuer.authority_pin
        receipt_hashes = list(terminal_acceptance.artifact_verified_receipt_sha256s)
        if (
            record.attempt_id != attempt.attempt_id
            or record.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
            or record.terminal_submission_sha256 != submission.terminal_submission_sha256
            or record.submission_payload_sha256 != submission.terminal_submission_sha256
            or record.terminal_submission_json != _model_json(submission)
            or record.artifact_manifest_sha256 != manifest.manifest_sha256
            or record.manifest_payload_sha256 != manifest.manifest_sha256
            or record.artifact_manifest_json != _model_json(manifest)
            or record.output_tree_sha256 != terminal_acceptance.output_tree_sha256
            or record.output_tree_sha256 != submission.output_tree_sha256
            or record.disposition != terminal_acceptance.disposition
            or record.disposition != submission.disposition
            or record.artifact_verified_receipt_sha256s_json != receipt_hashes
            or list(submission.artifact_verified_receipt_sha256s) != receipt_hashes
            or record.artifact_verified_receipts_json != [_model_json(item) for item in receipts]
            or record.acceptance_payload_sha256
            != terminal_acceptance.accepted_terminal_submission_sha256
            or record.accepted_terminal_submission_sha256
            != terminal_acceptance.accepted_terminal_submission_sha256
            or record.accepted_terminal_submission_json != _model_json(terminal_acceptance)
            or record.accepted_at != terminal_acceptance.accepted_at
            or record.runtime_control_pin_sha256 != canonical_sha256(runtime_pin)
            or record.runtime_control_pin_json != _model_json(runtime_pin)
            or attempt.accepted_terminal_submission_sha256
            != terminal_acceptance.accepted_terminal_submission_sha256
        ):
            raise LeaseAuthorityError("stored qualification terminal acceptance row is rebound")
        return terminal_acceptance

    def _release_terminated_holds(
        self,
        session: Session,
        *,
        attempt: _ExecutionAttemptRecord,
        budget_head: _ExecutionBudgetHeadRecord,
        node: _ExecutionNodeRecord,
        device_heads: tuple[_ExecutionDeviceHeadRecord, ...],
        resource: _ExecutionResourceLeaseRecord,
        device_leases: tuple[_ExecutionDeviceLeaseRecord, ...],
        reservation: _ExecutionBudgetReservationRecord,
        accepted: AcceptedRuntimeTermination,
        now: datetime,
    ) -> int:
        if (
            resource.state not in {"held", "reconciliation_required"}
            or reservation.state not in {"held", "reconciliation_required"}
            or any(item.state not in {"held", "reconciliation_required"} for item in device_leases)
            or accepted.attempt_id != attempt.attempt_id
            or accepted.fencing_epoch != attempt.fencing_epoch
            or accepted.lease_token_sha256 != attempt.lease_token_sha256
            or budget_head.reserved_microunits < reservation.held_microunits
        ):
            raise LeaseAuthorityError("accepted runtime termination differs from retained holds")
        duration = accepted.billable_ended_at - resource.acquired_at
        if duration < timedelta(0):
            raise BudgetUnavailable("runtime ended before its durable lease acquisition")
        duration_microseconds = (
            duration.days * 86_400_000_000 + duration.seconds * 1_000_000 + duration.microseconds
        )
        actual_lease_seconds = min(
            (duration_microseconds + 999_999) // 1_000_000,
            reservation.maximum_lease_seconds,
        )
        charged = reservation.fixed_charge_microunits + (
            reservation.charge_per_second_microunits * actual_lease_seconds
        )
        if charged > reservation.held_microunits:
            raise BudgetUnavailable("runtime settlement exceeds its exact quote hold")
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
                "accepted_runtime_termination_sha256": (accepted.accepted_termination_sha256),
                "runtime_ended_at": accepted.runtime_ended_at.isoformat(),
                "billable_ended_at": accepted.billable_ended_at.isoformat(),
                "actual_lease_seconds": actual_lease_seconds,
                "charged_microunits": charged,
            },
        )
        resource.state = "released"
        resource.released_at = now
        for device, device_head in zip(device_leases, device_heads, strict=True):
            if device_head.active_device_lease_id != device.device_lease_id:
                raise LeaseAuthorityError("terminal device head differs from its lease")
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
            raise CapacityUnavailable("node capacity head lost the terminated runtime hold")
        node.reserved_cpu_cores -= resource.cpu_cores
        node.reserved_memory_bytes -= resource.memory_bytes
        node.reserved_scratch_bytes -= resource.scratch_bytes
        if resource.exclusive:
            if node.exclusive_lease_id != resource.lease_id:
                raise CapacityUnavailable("exclusive node head differs from runtime lease")
            node.exclusive_lease_id = None
        node.state_version += 1
        node.updated_at = now
        return charged

    @staticmethod
    def _lock_runtime_holds(
        session: Session,
        attempt: _ExecutionAttemptRecord,
    ) -> tuple[
        _ExecutionBudgetHeadRecord,
        _ExecutionNodeRecord,
        tuple[_ExecutionDeviceHeadRecord, ...],
        _ExecutionResourceLeaseRecord,
        tuple[_ExecutionDeviceLeaseRecord, ...],
        _ExecutionBudgetReservationRecord,
    ]:
        reservation_identity = session.execute(
            select(_ExecutionBudgetReservationRecord).where(
                _ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id
            )
        ).scalar_one()
        budget_head = session.execute(
            select(_ExecutionBudgetHeadRecord)
            .where(
                _ExecutionBudgetHeadRecord.authorization_sha256
                == reservation_identity.authorization_sha256
            )
            .with_for_update()
        ).scalar_one()
        node = session.execute(
            select(_ExecutionNodeRecord)
            .where(_ExecutionNodeRecord.node_id == attempt.node_id)
            .with_for_update()
        ).scalar_one()
        device_ids = tuple(
            session.execute(
                select(_ExecutionDeviceLeaseRecord.device_id)
                .where(_ExecutionDeviceLeaseRecord.attempt_id == attempt.attempt_id)
                .order_by(_ExecutionDeviceLeaseRecord.device_id)
            ).scalars()
        )
        device_heads = (
            tuple(
                session.execute(
                    select(_ExecutionDeviceHeadRecord)
                    .where(
                        _ExecutionDeviceHeadRecord.node_id == attempt.node_id,
                        _ExecutionDeviceHeadRecord.device_id.in_(device_ids),
                    )
                    .order_by(_ExecutionDeviceHeadRecord.device_id)
                    .with_for_update()
                ).scalars()
            )
            if device_ids
            else ()
        )
        resource = session.execute(
            select(_ExecutionResourceLeaseRecord)
            .where(_ExecutionResourceLeaseRecord.attempt_id == attempt.attempt_id)
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
        reservation = session.execute(
            select(_ExecutionBudgetReservationRecord)
            .where(_ExecutionBudgetReservationRecord.attempt_id == attempt.attempt_id)
            .with_for_update()
        ).scalar_one()
        if (
            len(device_heads) != len(device_leases)
            or reservation.reservation_id != reservation_identity.reservation_id
        ):
            raise LeaseAuthorityError("runtime hold lock set is incomplete")
        return budget_head, node, device_heads, resource, device_leases, reservation

    def _release_never_started_holds(
        self,
        session: Session,
        *,
        execution_head: _ExecutionHeadRecord,
        attempt: _ExecutionAttemptRecord,
        budget_head: _ExecutionBudgetHeadRecord,
        node: _ExecutionNodeRecord,
        device_heads: tuple[_ExecutionDeviceHeadRecord, ...],
        resource: _ExecutionResourceLeaseRecord,
        device_leases: tuple[_ExecutionDeviceLeaseRecord, ...],
        reservation: _ExecutionBudgetReservationRecord,
        now: datetime,
        absence_receipt_sha256: str,
    ) -> None:
        if (
            attempt.runtime_identity_sha256 is not None
            or resource.state not in {"held", "reconciliation_required"}
            or reservation.state not in {"held", "reconciliation_required"}
            or any(item.state not in {"held", "reconciliation_required"} for item in device_leases)
            or budget_head.reserved_microunits < reservation.held_microunits
            or execution_head.active_attempt_id != attempt.attempt_id
        ):
            raise LeaseAuthorityError("never-started release differs from retained holds")
        reservation.state = "released"
        reservation.actual_lease_seconds = None
        reservation.settled_microunits = 0
        reservation.settled_at = now
        budget_head.reserved_microunits -= reservation.held_microunits
        budget_head.state_version += 1
        budget_head.updated_at = now
        self._append_budget_event(
            session,
            reservation_id=reservation.reservation_id,
            authorization_sha256=reservation.authorization_sha256,
            event_type="released",
            reserved_delta_microunits=-reservation.held_microunits,
            spent_delta_microunits=0,
            recorded_at=now,
            details={
                "pre_runtime_absence_receipt_sha256": absence_receipt_sha256,
                "never_started": True,
            },
        )
        resource.state = "released"
        resource.released_at = now
        for device, device_head in zip(device_leases, device_heads, strict=True):
            if device_head.active_device_lease_id != device.device_lease_id:
                raise LeaseAuthorityError("never-started device head differs from lease")
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
            raise CapacityUnavailable("node capacity head lost the never-started hold")
        node.reserved_cpu_cores -= resource.cpu_cores
        node.reserved_memory_bytes -= resource.memory_bytes
        node.reserved_scratch_bytes -= resource.scratch_bytes
        if resource.exclusive:
            if node.exclusive_lease_id != resource.lease_id:
                raise CapacityUnavailable("exclusive node head differs from never-started hold")
            node.exclusive_lease_id = None
        node.state_version += 1
        node.updated_at = now
        attempt.status = "cancelled"
        attempt.reconciliation_reason = None
        execution_head.active_attempt_id = None
        execution_head.state_version += 1
        execution_head.updated_at = now

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
            grant_sha256=attempt.grant_sha256,
            bundle_sha256=attempt.bundle_sha256,
            node_id=attempt.node_id,
            node_inventory_sha256=attempt.node_inventory_sha256,
            status=attempt.status,
            state_version=attempt.state_version,
            fencing_epoch=attempt.fencing_epoch,
            lease_token_sha256=attempt.lease_token_sha256,
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
