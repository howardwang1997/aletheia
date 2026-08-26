"""Atomic scientific-observation admission and Research Kernel incorporation.

The independent admission authority and the Research Kernel command authority remain separate.
This coordinator obtains a database-signed admission proof, asks an external Kernel authority to
sign the exact resulting proposal, and commits the admission row plus Kernel event, snapshot,
outbox, and stream head in one caller-owned PostgreSQL transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
from aletheia.observations.scientific_bridge import (
    CommittedObservationAdmission,
    EngineeringQualificationCustodyVerificationPort,
    ObservationAdmissionDecision,
    ObservationDatabaseAuthorityPin,
    ObservationValidationCampaignVerificationPort,
    RawRunCustodyVerificationPort,
    ResearchActionAuthorityVerificationPort,
    ScientificBridgeAuthorityPin,
    ScientificBridgeModel,
    ScientificBridgeVerificationError,
    commit_observation_admission,
    verify_committed_observation_admission,
)
from aletheia.observations.store import (
    ObservationAdmissionWrite,
    ObservationIdentityConflict,
    get_observation_admission_by_decision,
    get_observation_admission_by_slot,
    record_observation_admission,
)
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
)
from aletheia.research_store.store import (
    ResearchCommandReceipt,
    ResearchKernelStore,
    ResearchReplayAudit,
)


class AtomicObservationAdmissionError(RuntimeError):
    """The observation could not be atomically admitted and incorporated."""


class ObservationKernelAuthorizationPort(Protocol):
    """External ordinary Kernel authority; the controller never signs its own proposal."""

    def authorize_observation_incorporation(
        self,
        *,
        proposal: ResearchCommandProposal,
        committed_admission: CommittedObservationAdmission,
        idempotency_key: str,
        source_event_key: str,
    ) -> AuthorizedResearchCommand: ...


class ObservationKernelStorePort(Protocol):
    """Public caller-transaction seams required from the authoritative Kernel store."""

    def audit_in_session(
        self,
        session: Session,
        quest_id: str,
        *,
        expected_scope_binding=None,
    ) -> ResearchReplayAudit: ...

    def commit_in_session(
        self,
        session: Session,
        command: AuthorizedResearchCommand,
    ) -> ResearchCommandReceipt: ...

    def load_command_receipt_for_event_in_session(
        self,
        session: Session,
        *,
        quest_id: str,
        result_event_sha256: str,
    ) -> ResearchCommandReceipt | None: ...


@dataclass(frozen=True)
class ObservationAdmissionVerificationContext:
    """Deployment-owned bridge authorities and custody adapters."""

    qualification_authority: QualificationAuthorityVerifier
    action_authority: ResearchActionAuthorityVerificationPort
    qualification_custody: EngineeringQualificationCustodyVerificationPort
    raw_run_custody: RawRunCustodyVerificationPort
    validation_campaign_custody: ObservationValidationCampaignVerificationPort
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin
    database_authority_pin: ObservationDatabaseAuthorityPin
    database_private_key: bytes = field(repr=False)


class AtomicObservationAdmissionReceipt(ScientificBridgeModel):
    """Proof that one admission and its exact Kernel incorporation committed together."""

    committed_admission: CommittedObservationAdmission
    incorporation_payload: ObservationIncorporatedPayload
    kernel_receipt: ResearchCommandReceipt
    created: bool

    @model_validator(mode="after")
    def _chain_is_exact(self) -> "AtomicObservationAdmissionReceipt":
        decision = self.committed_admission.message.decision.message
        validation = decision.committed_validation_receipt.message.receipt.message
        authorization = validation.raw_run.scientific_authorization.message
        action_binding = authorization.action_protocol_binding
        if (
            self.incorporation_payload.scientific_slot_id != decision.scientific_slot_id
            or self.incorporation_payload.committed_admission_sha256
            != self.committed_admission.committed_admission_sha256
            or self.incorporation_payload.scientific_observation_sha256
            != decision.admitted_observation_sha256
            or self.incorporation_payload.outcome
            != getattr(validation.outcome, "value", validation.outcome)
            or self.incorporation_payload.action_id != action_binding.action.action_id
            or self.incorporation_payload.branch_id
            != action_binding.compilation_request.protocol.graph_scope.branch_id
            or self.kernel_receipt.quest_id != action_binding.action.quest_id
        ):
            raise ValueError("atomic admission receipt contains a rebound scientific chain")
        return self


SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
DatabaseClock = Callable[[Session], datetime]


def _database_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL is the production path
        raise AtomicObservationAdmissionError(
            "PostgreSQL did not provide the observation linearization time"
        )
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
        raise AtomicObservationAdmissionError(
            "observation database time must be timezone-aware UTC"
        )
    return observed


def _admission_material(
    committed_admission: CommittedObservationAdmission,
) -> tuple[str, ObservationIncorporatedPayload]:
    decision = committed_admission.message.decision.message
    validation = decision.committed_validation_receipt.message.receipt.message
    authorization = validation.raw_run.scientific_authorization.message
    binding = authorization.action_protocol_binding
    protocol = binding.compilation_request.protocol
    if decision.admitted_observation_sha256 is None or validation.outcome is None:
        raise AtomicObservationAdmissionError(
            "only an independently validated admitted observation may reach the Kernel"
        )
    if protocol.world_model is None:
        raise AtomicObservationAdmissionError(
            "scientific observation incorporation requires graph-scoped F9 v2 world-model custody"
        )
    payload = ObservationIncorporatedPayload(
        branch_id=protocol.graph_scope.branch_id,
        action_id=binding.action.action_id,
        scientific_slot_id=decision.scientific_slot_id,
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        scientific_observation_sha256=decision.admitted_observation_sha256,
        outcome=validation.outcome.value,
        source_world_model_sha256=protocol.world_model.world_model_sha256,
    )
    return binding.action.quest_id, payload


def _validate_authorized_command(
    *,
    command: AuthorizedResearchCommand,
    proposal: ResearchCommandProposal,
    idempotency_key: str,
    source_event_key: str,
) -> None:
    try:
        command = AuthorizedResearchCommand.model_validate(command.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AtomicObservationAdmissionError(
            "Kernel authority returned an invalid authorized command"
        ) from exc
    if (
        command.quest_id != proposal.quest_id
        or command.scope_binding != proposal.scope_binding
        or command.expected_stream_version != proposal.expected_stream_version
        or command.expected_tail_event_sha256 != proposal.expected_tail_event_sha256
        or command.event_type is not EventType.OBSERVATION_INCORPORATED
        or command.payload != proposal.payload
        or command.proposal_sha256 != proposal.proposal_sha256
        or command.idempotency_key != idempotency_key
        or command.source_event_key != source_event_key
        or command.authorized_at < proposal.proposed_at
        or command.principal_id == proposal.proposed_by_principal_id
    ):
        raise AtomicObservationAdmissionError(
            "Kernel authority rebound or self-authorized the observation proposal"
        )


class PostgreSQLAtomicObservationAdmissionCoordinator:
    """Commit exactly one scientific observation and its Kernel event atomically."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore | ObservationKernelStorePort,
        kernel_authority: ObservationKernelAuthorizationPort,
        verification: ObservationAdmissionVerificationContext,
        controller_principal_id: str,
        session_scope_factory: SessionScopeFactory = session_scope,
        database_clock: DatabaseClock = _database_time,
    ) -> None:
        self._kernel_store = kernel_store
        self._kernel_authority = kernel_authority
        self._verification = verification
        self._controller_principal_id = controller_principal_id
        self._session_scope_factory = session_scope_factory
        self._database_clock = database_clock

    def commit_and_incorporate(
        self,
        decision: ObservationAdmissionDecision,
    ) -> AtomicObservationAdmissionReceipt:
        """Linearize an empty-slot admission and its signed Kernel event in one transaction."""

        try:
            decision = ObservationAdmissionDecision.model_validate(
                decision.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise AtomicObservationAdmissionError(
                "observation admission proposal is structurally invalid"
            ) from exc
        authorization = decision.message.committed_validation_receipt.message.receipt.message.raw_run.scientific_authorization
        quest_id = authorization.message.action_protocol_binding.action.quest_id
        scientific_slot_id = decision.message.scientific_slot_id

        try:
            with self._session_scope_factory() as session:
                existing_decision = get_observation_admission_by_decision(
                    session,
                    decision_sha256=decision.decision_sha256,
                )
                if existing_decision is not None:
                    return self._exact_retry(
                        session=session,
                        existing=existing_decision,
                        decision=decision,
                    )
                existing_slot = get_observation_admission_by_slot(
                    session,
                    quest_id=quest_id,
                    scientific_slot_id=scientific_slot_id,
                )
                if existing_slot is not None:
                    raise ObservationIdentityConflict(
                        "scientific slot is already bound to another admission decision"
                    )

                registered_at = self._database_clock(session)
                context = self._verification
                committed_admission = commit_observation_admission(
                    decision=decision,
                    qualification_authority=context.qualification_authority,
                    action_authority=context.action_authority,
                    qualification_custody=context.qualification_custody,
                    raw_run_custody=context.raw_run_custody,
                    validation_campaign_custody=context.validation_campaign_custody,
                    execution_authority_pin=context.execution_authority_pin,
                    validator_authority_pin=context.validator_authority_pin,
                    admission_authority_pin=context.admission_authority_pin,
                    database_authority_pin=context.database_authority_pin,
                    private_key=context.database_private_key,
                    registered_at=registered_at,
                    commit_clock=lambda: self._database_clock(session),
                )
                committed_at = committed_admission.message.committed_at
                resolved_quest_id, payload = _admission_material(committed_admission)
                if resolved_quest_id != quest_id:
                    raise AtomicObservationAdmissionError(
                        "committed admission changed its Research Kernel Quest"
                    )
                protocol = (
                    authorization.message.action_protocol_binding.compilation_request.protocol
                )
                audit = self._kernel_store.audit_in_session(
                    session,
                    quest_id,
                    expected_scope_binding=protocol.graph_scope.scope_binding,
                )
                action = tuple(
                    item
                    for item in audit.state.actions
                    if item.action_ref.object_id == payload.action_id
                )
                if (
                    len(action) != 1
                    or action[0].branch_id != payload.branch_id
                    or action[0].lifecycle is not ActionLifecycle.AUTHORIZED
                ):
                    raise AtomicObservationAdmissionError(
                        "admitted observation no longer targets one authorized action"
                    )
                proposal = ResearchCommandProposal(
                    quest_id=quest_id,
                    scope_binding=audit.scope_binding,
                    expected_stream_version=audit.state.stream_version,
                    expected_tail_event_sha256=audit.state.tail_event_sha256,
                    event_type=EventType.OBSERVATION_INCORPORATED,
                    payload=payload,
                    proposed_by_principal_id=self._controller_principal_id,
                    proposed_at=committed_at,
                )
                idempotency_key = f"observation-admission:{decision.decision_sha256}"
                source_event_key = f"scientific-slot:{scientific_slot_id}"
                command = self._kernel_authority.authorize_observation_incorporation(
                    proposal=proposal,
                    committed_admission=committed_admission,
                    idempotency_key=idempotency_key,
                    source_event_key=source_event_key,
                )
                _validate_authorized_command(
                    command=command,
                    proposal=proposal,
                    idempotency_key=idempotency_key,
                    source_event_key=source_event_key,
                )
                kernel_receipt = self._kernel_store.commit_in_session(session, command)
                write = ObservationAdmissionWrite.from_contract(
                    committed_admission,
                    quest_id=quest_id,
                    incorporated_event_sequence=kernel_receipt.result_stream_version,
                    incorporated_event_sha256=kernel_receipt.result_event_sha256,
                    incorporated_event_type=EventType.OBSERVATION_INCORPORATED.value,
                )
                append = record_observation_admission(session, write)
                if not append.created or not kernel_receipt.created:
                    raise AtomicObservationAdmissionError(
                        "new admission transaction unexpectedly replayed one of its authorities"
                    )
                finished_at = self._database_clock(session)
                challenge = decision.message.issuance_challenge.message
                if not (
                    committed_at <= finished_at < challenge.expires_at
                    and context.database_authority_pin.active_at(finished_at)
                ):
                    raise AtomicObservationAdmissionError(
                        "atomic admission crossed its database challenge or authority deadline"
                    )
                return AtomicObservationAdmissionReceipt(
                    committed_admission=committed_admission,
                    incorporation_payload=payload,
                    kernel_receipt=kernel_receipt,
                    created=True,
                )
        except (ObservationIdentityConflict, ScientificBridgeVerificationError):
            raise
        except AtomicObservationAdmissionError:
            raise
        except Exception as exc:
            raise AtomicObservationAdmissionError(
                "atomic observation admission failed closed"
            ) from exc

    def _exact_retry(
        self,
        *,
        session: Session,
        existing: ObservationAdmissionWrite,
        decision: ObservationAdmissionDecision,
    ) -> AtomicObservationAdmissionReceipt:
        if existing.decision_sha256 != decision.decision_sha256:
            raise ObservationIdentityConflict(
                "observation admission exact retry changed its decision"
            )
        try:
            committed = CommittedObservationAdmission.model_validate(existing.admission_json)
        except ValueError as exc:
            raise AtomicObservationAdmissionError(
                "persisted committed admission is invalid"
            ) from exc
        if (
            committed.message.decision != decision
            or committed.committed_admission_sha256 != existing.committed_admission_sha256
            or existing.incorporated_event_sha256 is None
        ):
            raise ObservationIdentityConflict(
                "persisted observation admission differs from its exact retry"
            )
        quest_id, payload = _admission_material(committed)
        expected_write = ObservationAdmissionWrite.from_contract(
            committed,
            quest_id=quest_id,
            incorporated_event_sequence=existing.incorporated_event_sequence,
            incorporated_event_sha256=existing.incorporated_event_sha256,
            incorporated_event_type=existing.incorporated_event_type,
        )
        if expected_write != existing:
            raise ObservationIdentityConflict(
                "persisted observation admission row differs from its signed contract"
            )
        context = self._verification
        verify_committed_observation_admission(
            committed_admission=committed,
            qualification_authority=context.qualification_authority,
            action_authority=context.action_authority,
            qualification_custody=context.qualification_custody,
            raw_run_custody=context.raw_run_custody,
            validation_campaign_custody=context.validation_campaign_custody,
            execution_authority_pin=context.execution_authority_pin,
            validator_authority_pin=context.validator_authority_pin,
            admission_authority_pin=context.admission_authority_pin,
            database_authority_pin=context.database_authority_pin,
            observed_at=self._database_clock(session),
        )
        kernel_receipt = self._kernel_store.load_command_receipt_for_event_in_session(
            session,
            quest_id=quest_id,
            result_event_sha256=existing.incorporated_event_sha256,
        )
        if (
            kernel_receipt is None
            or kernel_receipt.created
            or kernel_receipt.result_stream_version != existing.incorporated_event_sequence
        ):
            raise AtomicObservationAdmissionError(
                "persisted admission is missing its exact Kernel command receipt"
            )
        return AtomicObservationAdmissionReceipt(
            committed_admission=committed,
            incorporation_payload=payload,
            kernel_receipt=kernel_receipt,
            created=False,
        )


__all__ = [
    "AtomicObservationAdmissionError",
    "AtomicObservationAdmissionReceipt",
    "ObservationAdmissionVerificationContext",
    "ObservationKernelAuthorizationPort",
    "PostgreSQLAtomicObservationAdmissionCoordinator",
]
