"""Concrete PostgreSQL allocator-to-qualification-node composition boundary.

The allocator owns database transactions and runtime-control issuance.  This adapter owns only
node-local assignment decryption, durable lease-token handoff, and translation between allocator
DTOs and the deliberately smaller :class:`~aletheia.execution.node_agent.NodeAllocatorPort`.
It never imports persistence records, opens a database session, or holds a signing key.
"""

from __future__ import annotations

import hashlib

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar

from aletheia.execution.allocator import (
    AdmissionConflict,
    LeaseAuthorityError,
    PostgreSQLExecutionAllocator,
    ReservationSnapshot,
    RuntimeProofReplayRejected,
    RuntimeProofReplayRejectionCode,
)
from aletheia.execution.assignment_contracts import (
    AssignmentTransportError,
    NodeAssignmentTransportPin,
    SealedQualificationAssignment,
    open_qualification_assignment,
    x25519_public_key_hex,
)
from aletheia.execution.node_agent import (
    AssignmentRejected,
    NodeAllocatorPort,
    NodeLeaseRejected,
    NodeProofReplayRejected,
    NodeProofReplayRejectionCode,
    NodeReservation,
    NodeRunOutcome,
    NodeRunResult,
    NodeTerminalDisposition,
    PreRuntimeAbsenceDecision,
    PreRuntimeAbsenceDisposition,
    QualificationAssignment,
    QualificationNodeAgent,
    ReservedDeviceBinding,
    RuntimeRejected,
    RuntimeStartAuthorization,
    TerminalArtifactCommit,
)
from aletheia.execution.runtime_contracts import (
    AttemptAdoptionReceipt,
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    QualificationVerificationError,
    RuntimeInspectionReceipt,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    HistoricalPreRuntimeRecoveryLineage,
    HistoricalRuntimeRecoveryGrant,
    NodeRuntimeLaunchReceipt,
    NodeRuntimeTerminationReceipt,
    PreRuntimeAbsenceReceipt,
    QualificationTerminalSubmission,
    QualificationTerminalDeadlineExpiration,
    RuntimeControlAuthorityVerifier,
    RuntimeFenceRebindReceipt,
    RuntimeFenceRebindRequest,
    RuntimeInspectionEvidence,
    RuntimeLaunchAuthorization,
    RuntimeLaunchAuthorizationRequest,
    RuntimePreparation,
    RuntimeTerminationAcceptanceChallenge,
)
from aletheia.execution.schemas import ArtifactManifest, ArtifactVerifiedReceipt, ExecutionIntent
from aletheia.protocols.schemas import WorkOrderNode


class LeaseTokenCustodyPort(Protocol):
    """Durable node-local custody used by both this adapter and ``QualificationNodeAgent``."""

    def save_token(self, *, attempt_id: str, fencing_epoch: int, token: str) -> None: ...

    def load_token(self, *, attempt_id: str, fencing_epoch: int, expected_sha256: str) -> str: ...


class AssignmentClockPort(Protocol):
    """The same UTC clock domain injected into the node agent."""

    def now(self) -> datetime: ...


class SystemAssignmentClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


_T = TypeVar("_T")


class QualificationExecutionWorker:
    """Run one node tick and settle only its authenticated ``COLLECTED`` acceptance.

    Settlement creates the durable qualification-terminal outbox row in the allocator's same
    transaction. Publishing that row to an external broker remains a separate deployment
    responsibility; this worker does not claim that delivery has occurred.
    """

    def __init__(
        self,
        *,
        agent: QualificationNodeAgent,
        allocator: PostgreSQLExecutionAllocator,
        runtime_control_authority: RuntimeControlAuthorityVerifier,
        node_id: str,
        node_manifest_sha256: str,
    ) -> None:
        if not isinstance(agent, QualificationNodeAgent):
            raise TypeError("qualification execution worker requires a concrete node agent")
        if not isinstance(allocator, PostgreSQLExecutionAllocator):
            raise TypeError("qualification execution worker requires the PostgreSQL allocator")
        if not isinstance(runtime_control_authority, RuntimeControlAuthorityVerifier):
            raise TypeError("qualification execution worker requires pinned runtime control")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("qualification execution worker node id must be nonempty")
        if (
            not isinstance(node_manifest_sha256, str)
            or len(node_manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in node_manifest_sha256)
        ):
            raise ValueError("qualification execution worker manifest id must be SHA-256")
        self._agent = agent
        self._allocator = allocator
        self._runtime_control_authority = RuntimeControlAuthorityVerifier(
            runtime_control_authority.pin
        )
        self._node_id = node_id
        self._node_manifest_sha256 = node_manifest_sha256

    def tick(self) -> NodeRunResult:
        try:
            self._allocator.reconcile_expired(
                node_id=self._node_id,
                node_manifest_sha256=self._node_manifest_sha256,
            )
        except (AdmissionConflict, LeaseAuthorityError) as exc:
            raise RuntimeRejected(
                "allocator rejected node-scoped expiry reconciliation"
            ) from exc
        self._adjudicate_expired_terminal()
        try:
            pending = self._allocator.pull_pending_qualification_terminal_settlement(
                node_id=self._node_id,
                node_manifest_sha256=self._node_manifest_sha256,
            )
        except AdmissionConflict as exc:
            raise RuntimeRejected(
                "allocator rejected the pinned pending terminal settlement pull"
            ) from exc
        if pending is not None:
            self._settle(self._validated_acceptance(pending))
        result = self._agent.run_once()
        if result.outcome is not NodeRunOutcome.COLLECTED:
            return result
        self._settle(self._validated_acceptance(result.accepted_terminal_submission))
        return result

    def _adjudicate_expired_terminal(self) -> None:
        try:
            commit = self._allocator.adjudicate_expired_qualification_terminal(
                node_id=self._node_id,
                node_manifest_sha256=self._node_manifest_sha256,
            )
        except (AdmissionConflict, LeaseAuthorityError) as exc:
            raise RuntimeRejected("allocator rejected terminal deadline adjudication") from exc
        if commit is None:
            return
        try:
            expiration = QualificationTerminalDeadlineExpiration.model_validate(
                commit.terminal_expiration.model_dump(mode="python")
            )
            snapshot = PostgreSQLNodeAllocatorAdapter._require_snapshot(
                commit.snapshot,
                operation="qualification terminal deadline adjudication",
            )
            activated_at = commit.activated_at
            replayed = commit.replayed
            outbox_id = commit.outbox_id
            terminal_authority_kind = commit.terminal_authority_kind
            self._runtime_control_authority.verify_historical(
                kind="qualification_terminal_deadline_expiration",
                payload=expiration.signature_payload,
                signature_ed25519_hex=expiration.signature_ed25519_hex,
                policy_sha256=expiration.runtime_control_policy_sha256,
                principal_id=expiration.adjudicated_by_principal_id,
                key_id=expiration.adjudication_key_id,
                signed_at=expiration.authorized_at,
            )
        except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
            raise RuntimeRejected(
                "allocator deadline adjudication is not a closed signed public DTO"
            ) from exc
        if (
            not isinstance(activated_at, datetime)
            or activated_at.tzinfo is None
            or activated_at.utcoffset() != timedelta(0)
            or activated_at < expiration.expired_at
            or type(replayed) is not bool
            or type(outbox_id) is not str
            or outbox_id != f"qto_{expiration.terminal_deadline_expiration_sha256}"
            or terminal_authority_kind != "terminal_deadline_expiration"
            or expiration.node_id != self._node_id
            or expiration.node_manifest_sha256 != self._node_manifest_sha256
            or snapshot.attempt_id != expiration.attempt_id
            or snapshot.execution_id != expiration.execution_id
            or snapshot.intent_sha256 != expiration.intent_sha256
            or snapshot.node_id != expiration.node_id
            or snapshot.node_inventory_sha256 != expiration.node_inventory_sha256
            or snapshot.resource_lease_sha256 != expiration.resource_lease_sha256
            or snapshot.fencing_epoch != expiration.fencing_epoch
            or snapshot.lease_token_sha256 != expiration.lease_token_sha256
            or snapshot.hard_deadline != expiration.hard_deadline
            or snapshot.status != "failed"
        ):
            raise RuntimeRejected(
                "allocator deadline adjudication changed exact terminal authority"
            )

    def _validated_acceptance(self, value: object) -> AcceptedQualificationTerminalSubmission:
        try:
            acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                value.model_dump(mode="python")
            )
            self._runtime_control_authority.verify_historical(
                kind="accepted_qualification_terminal_submission",
                payload=acceptance.signature_payload,
                signature_ed25519_hex=acceptance.signature_ed25519_hex,
                policy_sha256=acceptance.runtime_control_policy_sha256,
                principal_id=acceptance.accepted_by_principal_id,
                key_id=acceptance.acceptance_key_id,
                signed_at=acceptance.accepted_at,
            )
        except (AttributeError, TypeError, ValueError, QualificationVerificationError) as exc:
            raise RuntimeRejected(
                "qualification settlement lacks its authenticated terminal acceptance"
            ) from exc
        if acceptance.node_manifest_sha256 != self._node_manifest_sha256:
            raise RuntimeRejected("qualification settlement belongs to another node manifest")
        return acceptance

    def _settle(self, acceptance: AcceptedQualificationTerminalSubmission) -> None:
        try:
            commit = self._allocator.settle_qualification_terminal(terminal_acceptance=acceptance)
            snapshot = PostgreSQLNodeAllocatorAdapter._require_snapshot(
                commit.snapshot, operation="qualification terminal settlement"
            )
            replayed = commit.replayed
            outbox_id = commit.outbox_id
            terminal_authority_kind = commit.terminal_authority_kind
        except LeaseAuthorityError as exc:
            raise RuntimeRejected("allocator rejected qualification terminal settlement") from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator terminal settlement is not a closed public DTO"
            ) from exc
        expected_status = "succeeded" if acceptance.disposition == "process_succeeded" else "failed"
        if (
            snapshot.attempt_id != acceptance.attempt_id
            or snapshot.node_id != self._node_id
            or snapshot.status != expected_status
            or type(replayed) is not bool
            or type(outbox_id) is not str
            or outbox_id != f"qto_{acceptance.accepted_terminal_submission_sha256}"
            or terminal_authority_kind != "accepted_terminal_submission"
        ):
            raise RuntimeRejected("allocator terminal settlement changed exact acceptance")


class QualificationPreRuntimeCleanupWorker:
    """Expose one exact cleanup command and no assignment polling or settlement surface."""

    def __init__(self, *, agent: QualificationNodeAgent) -> None:
        if not isinstance(agent, QualificationNodeAgent):
            raise TypeError("pre-runtime cleanup worker requires a concrete node agent")
        self._agent = agent

    def recover(self, *, attempt_id: str) -> NodeRunResult:
        result = self._agent.recover_pre_runtime_cleanup(attempt_id=attempt_id)
        if result.attempt_id != attempt_id or result.outcome not in {
            NodeRunOutcome.LOCKED_BY_PEER,
            NodeRunOutcome.PRE_RUNTIME_RELEASED,
            NodeRunOutcome.RECONCILIATION_REQUIRED,
        }:
            raise RuntimeRejected("pre-runtime cleanup worker reached another lifecycle outcome")
        return result


class PostgreSQLNodeAllocatorAdapter(NodeAllocatorPort):
    """Expose a ``PostgreSQLExecutionAllocator`` through the frozen node-agent port.

    ``token_custody`` must be the same durable store passed to ``QualificationNodeAgent``.  An
    initial sealed assignment is decrypted directly into that store before this method returns;
    consequently the returned assignment projection never contains a raw lease token.  Historical
    delivery is accepted only through a signed recovery-only grant and an already-present local
    token for the current fence.
    """

    def __init__(
        self,
        *,
        allocator: PostgreSQLExecutionAllocator,
        transport_pin: NodeAssignmentTransportPin,
        node_transport_private_key: bytes,
        token_custody: LeaseTokenCustodyPort,
        clock: AssignmentClockPort | None = None,
    ) -> None:
        pin = NodeAssignmentTransportPin.model_validate(transport_pin.model_dump(mode="python"))
        private_key = bytes(node_transport_private_key)
        try:
            public_key = x25519_public_key_hex(private_key)
        except ValueError as exc:
            raise ValueError("node assignment transport private key is invalid") from exc
        if public_key != pin.public_key_x25519_hex:
            raise ValueError(
                "node assignment transport private key differs from its deployment pin"
            )
        if not callable(getattr(token_custody, "save_token", None)) or not callable(
            getattr(token_custody, "load_token", None)
        ):
            raise TypeError("node assignment token custody is incomplete")
        self._allocator = allocator
        self._transport_pin = pin
        self._transport_private_key = private_key
        self._token_custody = token_custody
        self._clock = clock or SystemAssignmentClock()

    def pull_qualification_assignment(
        self, *, node_id: str, node_manifest_sha256: str
    ) -> QualificationAssignment | None:
        if (
            node_id != self._transport_pin.node_id
            or node_manifest_sha256 != self._transport_pin.node_manifest_sha256
        ):
            raise AssignmentRejected("assignment pull differs from the pinned node transport scope")
        observed_at = self._utc_now()
        try:
            delivery = self._allocator.pull_assignment_delivery(
                node_id=node_id,
                node_manifest_sha256=node_manifest_sha256,
            )
        except AdmissionConflict as exc:
            raise AssignmentRejected("allocator rejected the pinned assignment delivery") from exc
        if delivery is None:
            return None
        try:
            bundle = EngineeringQualificationBundle.model_validate(
                delivery.bundle.model_dump(mode="python")
            )
            grant = EngineeringQualificationGrant.model_validate(
                delivery.grant.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(delivery.snapshot, operation="assignment pull")
            sealed = (
                SealedQualificationAssignment.model_validate(
                    delivery.sealed_envelope.model_dump(mode="python")
                )
                if delivery.sealed_envelope is not None
                else None
            )
            recovery = (
                HistoricalRuntimeRecoveryGrant.model_validate(
                    delivery.historical_recovery_grant.model_dump(mode="python")
                )
                if delivery.historical_recovery_grant is not None
                else None
            )
            pre_runtime_recovery = (
                HistoricalPreRuntimeRecoveryLineage.model_validate(
                    delivery.historical_pre_runtime_recovery_lineage.model_dump(mode="python")
                )
                if delivery.historical_pre_runtime_recovery_lineage is not None
                else None
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AssignmentRejected(
                "allocator assignment delivery is not a closed public DTO"
            ) from exc
        if (
            sum(authority is not None for authority in (sealed, recovery, pre_runtime_recovery))
            != 1
        ):
            raise AssignmentRejected(
                "allocator assignment delivery must contain exactly one launch or recovery authority"
            )
        intent, node = self._validate_public_assignment_projection(
            bundle=bundle,
            grant=grant,
            snapshot=snapshot,
            node_id=node_id,
            node_manifest_sha256=node_manifest_sha256,
        )
        if sealed is not None:
            if snapshot.status not in {"reserved", "starting", "reconciliation_required"}:
                raise AssignmentRejected(
                    "launch-capable assignment was delivered after durable runtime launch"
                )
            self._open_initial_assignment(
                envelope=sealed,
                snapshot=snapshot,
                observed_at=observed_at,
            )
        elif recovery is not None:
            if snapshot.status not in {
                "running",
                "reconciliation_required",
                "terminated",
                "verifying",
            }:
                raise AssignmentRejected(
                    "historical recovery was delivered before durable runtime launch"
                )
            self._validate_recovery_delivery(recovery=recovery, snapshot=snapshot)
            # Recovery authority never delivers a credential.  Prove that the current fence has
            # already reached durable node-local custody before exposing the projection.
            token = self._token_custody.load_token(
                attempt_id=snapshot.attempt_id,
                fencing_epoch=snapshot.fencing_epoch,
                expected_sha256=snapshot.lease_token_sha256,
            )
            del token
        else:
            assert pre_runtime_recovery is not None
            if snapshot.status not in {"starting", "reconciliation_required"}:
                raise AssignmentRejected(
                    "pre-runtime recovery was delivered outside cleanup-only allocator state"
                )
            self._validate_pre_runtime_recovery_delivery(
                lineage=pre_runtime_recovery,
                snapshot=snapshot,
                node_manifest_sha256=node_manifest_sha256,
            )
            # The lineage proves only a previously committed ticket.  Credential custody must
            # already exist locally, and no credential is re-delivered through this DTO.
            token = self._token_custody.load_token(
                attempt_id=snapshot.attempt_id,
                fencing_epoch=snapshot.fencing_epoch,
                expected_sha256=snapshot.lease_token_sha256,
            )
            del token
        return QualificationAssignment(
            intent=intent,
            work_order_node=node,
            qualification_grant=grant,
            reservation=self.project_reservation(snapshot),
            lease_token=None,
            historical_recovery_grant=recovery,
            historical_pre_runtime_recovery_lineage=pre_runtime_recovery,
        )

    def pull_pre_runtime_cleanup_assignment(
        self,
        *,
        node_id: str,
        node_manifest_sha256: str,
        attempt_id: str,
    ) -> QualificationAssignment | None:
        """Project one named cleanup-only delivery without polling launch-capable work."""

        if (
            node_id != self._transport_pin.node_id
            or node_manifest_sha256 != self._transport_pin.node_manifest_sha256
        ):
            raise AssignmentRejected(
                "pre-runtime cleanup pull differs from pinned node transport scope"
            )
        try:
            delivery = self._allocator.pull_pre_runtime_cleanup_delivery(
                node_id=node_id,
                node_manifest_sha256=node_manifest_sha256,
                attempt_id=attempt_id,
            )
        except AdmissionConflict as exc:
            raise AssignmentRejected(
                "allocator rejected the exact pre-runtime cleanup delivery"
            ) from exc
        if delivery is None:
            return None
        try:
            bundle = EngineeringQualificationBundle.model_validate(
                delivery.bundle.model_dump(mode="python")
            )
            grant = EngineeringQualificationGrant.model_validate(
                delivery.grant.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(
                delivery.snapshot,
                operation="exact pre-runtime cleanup pull",
            )
            lineage = HistoricalPreRuntimeRecoveryLineage.model_validate(
                delivery.historical_pre_runtime_recovery_lineage.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AssignmentRejected(
                "allocator pre-runtime cleanup delivery is not a closed public DTO"
            ) from exc
        if (
            snapshot.attempt_id != attempt_id
            or snapshot.status not in {"starting", "reconciliation_required"}
            or delivery.sealed_envelope is not None
            or delivery.historical_recovery_grant is not None
        ):
            raise AssignmentRejected(
                "exact pre-runtime cleanup delivery contains another authority mode"
            )
        intent, node = self._validate_public_assignment_projection(
            bundle=bundle,
            grant=grant,
            snapshot=snapshot,
            node_id=node_id,
            node_manifest_sha256=node_manifest_sha256,
        )
        self._validate_pre_runtime_recovery_delivery(
            lineage=lineage,
            snapshot=snapshot,
            node_manifest_sha256=node_manifest_sha256,
        )
        token = self._token_custody.load_token(
            attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            expected_sha256=snapshot.lease_token_sha256,
        )
        del token
        return QualificationAssignment(
            intent=intent,
            work_order_node=node,
            qualification_grant=grant,
            reservation=self.project_reservation(snapshot),
            lease_token=None,
            historical_pre_runtime_recovery_lineage=lineage,
        )

    def start_attempt(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        launch_authorization_request: RuntimeLaunchAuthorizationRequest,
    ) -> RuntimeStartAuthorization:
        commit = self._lease_call(
            "runtime start authorization",
            self._allocator.authorize_runtime_start,
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            runtime_preparation=runtime_preparation,
            launch_authorization_request=launch_authorization_request,
        )
        try:
            authorization = RuntimeLaunchAuthorization.model_validate(
                commit.launch_authorization.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(commit.snapshot, operation="runtime start")
            replayed = commit.replayed
            if type(replayed) is not bool:
                raise TypeError("runtime-start replay marker is not a boolean")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator runtime-start commit is not a closed public DTO"
            ) from exc
        if (
            snapshot.attempt_id != attempt_id
            or snapshot.execution_id != runtime_preparation.execution_id
            or snapshot.intent_sha256 != runtime_preparation.intent_sha256
            or snapshot.node_id != runtime_preparation.node_id
            or snapshot.fencing_epoch != fencing_epoch
            or snapshot.fencing_epoch != runtime_preparation.fencing_epoch
            or snapshot.lease_token_sha256 != runtime_preparation.lease_token_sha256
            or snapshot.lease_expires_at != authorization.lease_expires_at
            or snapshot.hard_deadline != authorization.hard_deadline
            or snapshot.status not in {"starting", "reconciliation_required"}
            or (not replayed and snapshot.status != "starting")
            or launch_authorization_request.infrastructure_attempt_id != attempt_id
            or launch_authorization_request.fencing_epoch != fencing_epoch
            or launch_authorization_request.lease_token_sha256
            != runtime_preparation.lease_token_sha256
            or launch_authorization_request.runtime_preparation_sha256
            != runtime_preparation.preparation_sha256
            or authorization.authorization_request_sha256
            != launch_authorization_request.request_sha256
            or authorization.runtime_preparation_sha256 != runtime_preparation.preparation_sha256
            or authorization.infrastructure_attempt_id != attempt_id
            or authorization.execution_id != runtime_preparation.execution_id
            or authorization.intent_sha256 != runtime_preparation.intent_sha256
            or authorization.node_id != runtime_preparation.node_id
            or authorization.node_manifest_sha256 != runtime_preparation.node_manifest_sha256
            or authorization.fencing_epoch != fencing_epoch
            or authorization.lease_token_sha256 != runtime_preparation.lease_token_sha256
            or authorization.enforced_placement_sha256
            != runtime_preparation.enforced_placement_sha256
            or authorization.input_materialization_receipt_sha256
            != runtime_preparation.input_materialization_receipt_sha256
        ):
            raise RuntimeRejected("allocator runtime-start commit changed exact authority")
        return RuntimeStartAuthorization(
            reservation=self.project_reservation(snapshot),
            launch_authorization=authorization,
            replayed=replayed,
        )

    def mark_running(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        node_runtime_launch_receipt: NodeRuntimeLaunchReceipt,
    ) -> NodeReservation:
        commit = self._lease_call(
            "runtime launch acceptance",
            self._allocator.accept_runtime_launch,
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            node_runtime_launch_receipt=node_runtime_launch_receipt,
        )
        try:
            recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                commit.historical_recovery_grant.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(
                commit.snapshot, operation="runtime launch acceptance"
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator runtime-launch commit is not a closed public DTO"
            ) from exc
        try:
            self._validate_recovery_delivery(recovery=recovery, snapshot=snapshot)
        except AssignmentRejected as exc:
            raise RuntimeRejected(
                "allocator runtime-launch recovery commit changed exact authority"
            ) from exc
        if (
            recovery.node_runtime_launch_receipt_sha256
            != node_runtime_launch_receipt.launch_receipt_sha256
        ):
            raise RuntimeRejected(
                "allocator recovery authority changed the accepted launch receipt"
            )
        return self.project_reservation(snapshot)

    def heartbeat(
        self, *, attempt_id: str, lease_token: str, fencing_epoch: int
    ) -> NodeReservation:
        commit = self._lease_call(
            "heartbeat",
            self._allocator.heartbeat,
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
        )
        return self.project_reservation(
            self._require_snapshot(commit.snapshot, operation="heartbeat")
        )

    def retain_reconciliation(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        inspection_receipt: RuntimeInspectionReceipt,
        reason: str,
    ) -> NodeReservation:
        commit = self._lease_call(
            "runtime reconciliation retention",
            self._allocator.retain_runtime_reconciliation,
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            inspection_receipt=inspection_receipt,
            reason=reason,
        )
        return self.project_reservation(
            self._require_snapshot(commit.snapshot, operation="runtime reconciliation retention")
        )

    def resolve_pre_runtime_absence(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        runtime_preparation: RuntimePreparation,
        absence_receipt: PreRuntimeAbsenceReceipt,
        replacement_launch_authorization_request: RuntimeLaunchAuthorizationRequest | None,
    ) -> PreRuntimeAbsenceDecision:
        commit = self._lease_call(
            "pre-runtime absence resolution",
            self._allocator.resolve_runtime_absence,
            proof_replay_code=(
                RuntimeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED
            ),
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            runtime_preparation=runtime_preparation,
            absence_receipt=absence_receipt,
            replacement_launch_authorization_request=(replacement_launch_authorization_request),
        )
        try:
            disposition = PreRuntimeAbsenceDisposition(commit.disposition)
            receipt_sha256 = str(commit.pre_runtime_absence_receipt_sha256)
            returned_request = (
                RuntimeLaunchAuthorizationRequest.model_validate(
                    commit.replacement_launch_authorization_request.model_dump(mode="python")
                )
                if commit.replacement_launch_authorization_request is not None
                else None
            )
            replacement_authorization = (
                RuntimeLaunchAuthorization.model_validate(
                    commit.replacement_launch_authorization.model_dump(mode="python")
                )
                if commit.replacement_launch_authorization is not None
                else None
            )
            snapshot = self._require_snapshot(commit.snapshot, operation="runtime absence")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("allocator absence commit is not a closed public DTO") from exc
        if returned_request != replacement_launch_authorization_request:
            raise RuntimeRejected("allocator absence commit changed the replacement request")
        request = (
            returned_request if disposition is PreRuntimeAbsenceDisposition.REAUTHORIZED else None
        )
        if disposition is PreRuntimeAbsenceDisposition.RELEASED and returned_request is not None:
            raise RuntimeRejected("released absence commit retained a replacement request")
        try:
            return PreRuntimeAbsenceDecision(
                reservation=self.project_reservation(snapshot),
                disposition=disposition,
                pre_runtime_absence_receipt_sha256=receipt_sha256,
                replacement_launch_authorization_request=request,
                replacement_launch_authorization=replacement_authorization,
            )
        except ValueError as exc:
            raise RuntimeRejected("allocator absence commit is internally inconsistent") from exc

    def adopt_attempt(
        self,
        *,
        receipt: AttemptAdoptionReceipt,
        previous_lease_token: str,
        previous_fencing_epoch: int,
        new_lease_token: str,
        runtime_fence_rebind_request: RuntimeFenceRebindRequest,
        runtime_fence_rebind_receipt: RuntimeFenceRebindReceipt,
    ) -> NodeReservation:
        commit = self._lease_call(
            "runtime attempt adoption",
            self._allocator.adopt_runtime_attempt,
            receipt=receipt,
            previous_lease_token=previous_lease_token,
            previous_fencing_epoch=previous_fencing_epoch,
            new_lease_token=new_lease_token,
            runtime_fence_rebind_request=runtime_fence_rebind_request,
            runtime_fence_rebind_receipt=runtime_fence_rebind_receipt,
        )
        try:
            if (
                commit.adoption_receipt_sha256 != receipt.adoption_receipt_sha256
                or commit.runtime_fence_rebind_receipt_sha256
                != runtime_fence_rebind_receipt.rebind_receipt_sha256
            ):
                raise ValueError("adoption commit identities differ")
            snapshot = self._require_snapshot(commit.snapshot, operation="runtime attempt adoption")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("allocator adoption commit is not a closed public DTO") from exc
        return self.project_reservation(snapshot)

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
    ) -> RuntimeTerminationAcceptanceChallenge:
        commit = self._lease_call(
            "runtime termination challenge",
            self._allocator.issue_runtime_termination_challenge,
            proof_replay_code=(
                RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
            ),
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            runtime_preparation=runtime_preparation,
            node_runtime_launch_receipt=node_runtime_launch_receipt,
            termination_evidence=termination_evidence,
            inspection_sequence=inspection_sequence,
            artifact_submission_deadline=artifact_submission_deadline,
        )
        try:
            challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                commit.challenge.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(
                commit.snapshot, operation="runtime termination challenge"
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator termination challenge commit is not a closed public DTO"
            ) from exc
        if (
            snapshot.status != "terminated"
            or challenge.attempt_id != attempt_id
            or challenge.fencing_epoch != fencing_epoch
            or challenge.lease_token_sha256 != snapshot.lease_token_sha256
            or snapshot.attempt_id != attempt_id
            or snapshot.fencing_epoch != fencing_epoch
        ):
            raise RuntimeRejected("allocator termination challenge changed exact fence authority")
        return challenge

    def accept_runtime_termination(
        self,
        *,
        attempt_id: str,
        lease_token: str,
        fencing_epoch: int,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
    ) -> AcceptedRuntimeTermination:
        commit = self._lease_call(
            "runtime termination acceptance",
            self._allocator.accept_runtime_termination,
            proof_replay_code=(
                RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
            ),
            attempt_id=attempt_id,
            lease_token=lease_token,
            fencing_epoch=fencing_epoch,
            challenge=challenge,
            node_runtime_termination_receipt=node_runtime_termination_receipt,
        )
        try:
            accepted = AcceptedRuntimeTermination.model_validate(
                commit.accepted_termination.model_dump(mode="python")
            )
            recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                commit.historical_recovery_grant.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(
                commit.snapshot, operation="runtime termination acceptance"
            )
            if type(commit.charged_microunits) is not int or commit.charged_microunits < 0:
                raise ValueError("terminal charge is invalid")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator termination acceptance commit is not a closed public DTO"
            ) from exc
        try:
            self._validate_recovery_delivery(recovery=recovery, snapshot=snapshot)
        except AssignmentRejected as exc:
            raise RuntimeRejected(
                "allocator termination recovery commit changed exact authority"
            ) from exc
        if (
            snapshot.status != "verifying"
            or snapshot.attempt_id != attempt_id
            or snapshot.fencing_epoch != fencing_epoch
            or snapshot.lease_token_sha256 != accepted.lease_token_sha256
            or accepted.attempt_id != attempt_id
            or accepted.fencing_epoch != fencing_epoch
            or accepted.challenge_sha256 != challenge.challenge_sha256
            or accepted.node_runtime_termination_receipt_sha256
            != node_runtime_termination_receipt.termination_receipt_sha256
            or recovery.runtime_preparation_sha256 != accepted.runtime_preparation_sha256
            or recovery.node_runtime_launch_receipt_sha256
            != accepted.node_runtime_launch_receipt_sha256
            or recovery.accepted_runtime_termination_sha256 != accepted.accepted_termination_sha256
            or recovery.recovery_expires_at != accepted.artifact_submission_deadline
        ):
            raise RuntimeRejected("allocator termination acceptance changed exact authority")
        return accepted

    def replay_accepted_runtime_termination(
        self,
        *,
        recovery_grant: HistoricalRuntimeRecoveryGrant,
        challenge: RuntimeTerminationAcceptanceChallenge,
        node_runtime_termination_receipt: NodeRuntimeTerminationReceipt,
        expected_accepted_runtime_termination_sha256: str,
    ) -> AcceptedRuntimeTermination:
        """Replay only the exact durable acceptance named by historical recovery authority."""

        try:
            recovery = HistoricalRuntimeRecoveryGrant.model_validate(
                recovery_grant.model_dump(mode="python")
            )
            supplied_challenge = RuntimeTerminationAcceptanceChallenge.model_validate(
                challenge.model_dump(mode="python")
            )
            supplied_receipt = NodeRuntimeTerminationReceipt.model_validate(
                node_runtime_termination_receipt.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("accepted termination replay input is not canonical") from exc
        accepted = self._lease_call(
            "accepted runtime termination replay",
            self._allocator.replay_accepted_runtime_termination,
            recovery_grant=recovery,
            challenge=supplied_challenge,
            node_runtime_termination_receipt=supplied_receipt,
            expected_accepted_runtime_termination_sha256=(
                expected_accepted_runtime_termination_sha256
            ),
        )
        try:
            accepted = AcceptedRuntimeTermination.model_validate(accepted.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator accepted-termination replay is not a closed public DTO"
            ) from exc
        if (
            recovery.accepted_runtime_termination_sha256
            != expected_accepted_runtime_termination_sha256
            or accepted.accepted_termination_sha256 != expected_accepted_runtime_termination_sha256
            or recovery.infrastructure_attempt_id != accepted.attempt_id
            or recovery.runtime_preparation_sha256 != accepted.runtime_preparation_sha256
            or recovery.node_runtime_launch_receipt_sha256
            != accepted.node_runtime_launch_receipt_sha256
            or recovery.hard_deadline != accepted.hard_deadline
            or recovery.recovery_expires_at != accepted.artifact_submission_deadline
            or accepted.challenge_sha256 != supplied_challenge.challenge_sha256
            or accepted.node_runtime_termination_receipt_sha256
            != supplied_receipt.termination_receipt_sha256
            or accepted.attempt_id != supplied_challenge.attempt_id
            or accepted.fencing_epoch != supplied_challenge.fencing_epoch
            or accepted.lease_token_sha256 != supplied_challenge.lease_token_sha256
        ):
            raise RuntimeRejected("allocator accepted-termination replay changed exact authority")
        return accepted

    def submit_terminal_artifacts(
        self,
        *,
        accepted_termination: AcceptedRuntimeTermination,
        terminal_submission: QualificationTerminalSubmission,
        artifact_manifest: ArtifactManifest,
        artifact_verified_receipts: tuple[ArtifactVerifiedReceipt, ...],
        disposition: NodeTerminalDisposition,
    ) -> TerminalArtifactCommit:
        if terminal_submission.disposition != disposition.value:
            raise RuntimeRejected("terminal disposition differs from the signed node submission")
        commit = self._lease_call(
            "terminal artifact acceptance",
            self._allocator.accept_terminal_artifacts,
            accepted_termination=accepted_termination,
            terminal_submission=terminal_submission,
            artifact_manifest=artifact_manifest,
            artifact_verified_receipts=artifact_verified_receipts,
        )
        try:
            acceptance = AcceptedQualificationTerminalSubmission.model_validate(
                commit.terminal_acceptance.model_dump(mode="python")
            )
            snapshot = self._require_snapshot(commit.snapshot, operation="terminal artifacts")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected("allocator terminal commit is not a closed public DTO") from exc
        if (
            snapshot.status != "verifying"
            or snapshot.attempt_id != accepted_termination.attempt_id
            or acceptance.attempt_id != accepted_termination.attempt_id
            or acceptance.terminal_submission_sha256
            != terminal_submission.terminal_submission_sha256
        ):
            raise RuntimeRejected("allocator terminal commit changed exact submission authority")
        return TerminalArtifactCommit(
            reservation=self.project_reservation(snapshot),
            terminal_acceptance=acceptance,
        )

    @staticmethod
    def project_reservation(snapshot: ReservationSnapshot) -> NodeReservation:
        """Project the public allocator snapshot without carrying budget or ORM state."""

        if not isinstance(snapshot, ReservationSnapshot):
            raise RuntimeRejected("allocator reservation is not its public snapshot DTO")
        try:
            devices = tuple(
                ReservedDeviceBinding(
                    device_id=item.device_id,
                    hardware_uuid=item.hardware_uuid,
                    fencing_epoch=item.fencing_epoch,
                    requested_memory_bytes=item.requested_memory_bytes,
                    state=item.state,
                )
                for item in snapshot.device_leases
            )
            return NodeReservation(
                execution_id=snapshot.execution_id,
                attempt_id=snapshot.attempt_id,
                intent_sha256=snapshot.intent_sha256,
                admission_sha256=snapshot.admission_sha256,
                grant_sha256=snapshot.grant_sha256,
                node_id=snapshot.node_id,
                node_inventory_sha256=snapshot.node_inventory_sha256,
                resource_lease_sha256=snapshot.resource_lease_sha256,
                selected_resource_ids=tuple(snapshot.selected_resource_ids),
                cpu_cores=snapshot.cpu_cores,
                memory_bytes=snapshot.memory_bytes,
                scratch_bytes=snapshot.scratch_bytes,
                exclusive=snapshot.exclusive,
                device_leases=devices,
                status=snapshot.status,
                fencing_epoch=snapshot.fencing_epoch,
                lease_token_sha256=snapshot.lease_token_sha256,
                lease_expires_at=snapshot.lease_expires_at,
                hard_deadline=snapshot.hard_deadline,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeRejected(
                "allocator reservation snapshot cannot be projected exactly"
            ) from exc

    def _open_initial_assignment(
        self,
        *,
        envelope: SealedQualificationAssignment,
        snapshot: ReservationSnapshot,
        observed_at: datetime,
    ) -> None:
        try:
            secret = open_qualification_assignment(
                envelope=envelope,
                transport_pin=self._transport_pin,
                node_transport_private_key=self._transport_private_key,
                observed_at=observed_at,
            )
        except AssignmentTransportError as exc:
            raise AssignmentRejected(
                "sealed allocator assignment failed node authentication"
            ) from exc
        if (
            secret.infrastructure_attempt_id != snapshot.attempt_id
            or secret.admission_sha256 != snapshot.admission_sha256
            or secret.grant_sha256 != snapshot.grant_sha256
            or secret.bundle_sha256 != snapshot.bundle_sha256
            or secret.node_id != snapshot.node_id
            or secret.node_manifest_sha256 != self._transport_pin.node_manifest_sha256
            or secret.resource_lease_sha256 != snapshot.resource_lease_sha256
            or secret.fencing_epoch != snapshot.fencing_epoch
            or secret.lease_token_sha256 != snapshot.lease_token_sha256
            or secret.expires_at != snapshot.hard_deadline
        ):
            raise AssignmentRejected("decrypted assignment differs from allocator reservation")
        self._token_custody.save_token(
            attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            token=secret.lease_token,
        )
        retained = self._token_custody.load_token(
            attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            expected_sha256=snapshot.lease_token_sha256,
        )
        del retained

    @staticmethod
    def _validate_recovery_delivery(
        *, recovery: HistoricalRuntimeRecoveryGrant, snapshot: ReservationSnapshot
    ) -> None:
        if (
            recovery.admission_sha256 != snapshot.admission_sha256
            or recovery.qualification_grant_sha256 != snapshot.grant_sha256
            or recovery.intent_sha256 != snapshot.intent_sha256
            or recovery.execution_id != snapshot.execution_id
            or recovery.infrastructure_attempt_id != snapshot.attempt_id
            or recovery.hard_deadline != snapshot.hard_deadline
            or (
                snapshot.status == "verifying"
                and recovery.accepted_runtime_termination_sha256 is None
            )
            or (
                snapshot.status in {"running", "terminated"}
                and recovery.accepted_runtime_termination_sha256 is not None
            )
            or recovery.launch_allowed
            or not recovery.recovery_only
        ):
            raise AssignmentRejected("historical recovery grant differs from allocator reservation")

    @staticmethod
    def _validate_pre_runtime_recovery_delivery(
        *,
        lineage: HistoricalPreRuntimeRecoveryLineage,
        snapshot: ReservationSnapshot,
        node_manifest_sha256: str,
    ) -> None:
        preparation = lineage.runtime_preparation
        authorization = lineage.runtime_launch_authorization
        if (
            not lineage.cleanup_only
            or lineage.launch_allowed
            or preparation.node_manifest_sha256 != node_manifest_sha256
            or preparation.node_id != snapshot.node_id
            or preparation.execution_id != snapshot.execution_id
            or preparation.infrastructure_attempt_id != snapshot.attempt_id
            or preparation.intent_sha256 != snapshot.intent_sha256
            or preparation.fencing_epoch != snapshot.fencing_epoch
            or preparation.lease_token_sha256 != snapshot.lease_token_sha256
            or authorization.admission_sha256 != snapshot.admission_sha256
            or authorization.qualification_grant_sha256 != snapshot.grant_sha256
            or authorization.lease_expires_at != snapshot.lease_expires_at
            or authorization.hard_deadline != snapshot.hard_deadline
        ):
            raise AssignmentRejected(
                "historical pre-runtime lineage differs from allocator reservation"
            )

    @staticmethod
    def _validate_public_assignment_projection(
        *,
        bundle: EngineeringQualificationBundle,
        grant: EngineeringQualificationGrant,
        snapshot: ReservationSnapshot,
        node_id: str,
        node_manifest_sha256: str,
    ) -> tuple[ExecutionIntent, WorkOrderNode]:
        intent = bundle.intent
        message = grant.message
        nodes = tuple(
            item for item in bundle.work_order.nodes if item.node_id == intent.work_order_node_id
        )
        if len(nodes) != 1:
            raise AssignmentRejected("qualification bundle lacks its exact WorkOrder node")
        node = nodes[0]
        if (
            bundle.bundle_sha256 != snapshot.bundle_sha256
            or grant.grant_sha256 != snapshot.grant_sha256
            or message.bundle_sha256 != bundle.bundle_sha256
            or message.intent_sha256 != intent.intent_sha256
            or message.execution_id != intent.execution_id
            or message.infrastructure_attempt_id
            != intent.infrastructure_attempt.infrastructure_attempt_id
            or message.work_order_sha256 != bundle.work_order.work_order_sha256
            or snapshot.execution_id != intent.execution_id
            or snapshot.attempt_id != intent.infrastructure_attempt.infrastructure_attempt_id
            or snapshot.intent_sha256 != intent.intent_sha256
            or snapshot.node_id != node_id
            or node_manifest_sha256 == ""
            or node.node_sha256 != intent.work_order_node_sha256
        ):
            raise AssignmentRejected("allocator assignment projection is rebound")
        return (
            ExecutionIntent.model_validate(intent.model_dump(mode="python")),
            WorkOrderNode.model_validate(node.model_dump(mode="python")),
        )

    def _utc_now(self) -> datetime:
        observed_at = self._clock.now()
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
            raise AssignmentRejected("assignment adapter clock must provide timezone-aware UTC")
        return observed_at

    @staticmethod
    def _require_snapshot(value: object, *, operation: str) -> ReservationSnapshot:
        if not isinstance(value, ReservationSnapshot):
            raise RuntimeRejected(f"allocator {operation} omitted its public reservation snapshot")
        return value

    @staticmethod
    def _lease_call(
        operation: str,
        call: Callable[..., _T],
        *,
        proof_replay_code: RuntimeProofReplayRejectionCode | None = None,
        **kwargs: object,
    ) -> _T:
        try:
            return call(**kwargs)
        except RuntimeProofReplayRejected as exc:
            if exc.code is proof_replay_code:
                if exc.code is (
                    RuntimeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                ):
                    node_code = (
                        NodeProofReplayRejectionCode.TERMINATION_CHALLENGE_EXPIRED_UNACCEPTED
                    )
                elif exc.code is (
                    RuntimeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED
                ):
                    node_code = NodeProofReplayRejectionCode.PRE_RUNTIME_ABSENCE_STALE_UNCOMMITTED
                else:  # pragma: no cover - closed enum, retained for future fail-closed changes
                    raise NodeLeaseRejected(f"allocator rejected {operation} authority") from exc
                raise NodeProofReplayRejected(
                    node_code,
                    f"allocator rejected {operation} as refreshable proof replay",
                ) from exc
            raise NodeLeaseRejected(f"allocator rejected {operation} authority") from exc
        except LeaseAuthorityError as exc:
            # Do not relay allocator text: it must never accidentally reflect raw credential input.
            diagnostic = hashlib.sha256(
                b"ALETHEIA_NODE_ALLOCATOR_REJECTION_V1\x00"
                + operation.encode("utf-8")
                + b"\x00"
                + type(exc).__qualname__.encode("utf-8")
                + b"\x00"
                + str(exc).encode("utf-8")
            ).hexdigest()
            raise NodeLeaseRejected(
                f"allocator rejected {operation} authority",
                allocator_rejection_sha256=diagnostic,
            ) from exc


__all__ = [
    "AssignmentClockPort",
    "LeaseTokenCustodyPort",
    "PostgreSQLNodeAllocatorAdapter",
    "QualificationExecutionWorker",
    "QualificationPreRuntimeCleanupWorker",
    "SystemAssignmentClock",
]
