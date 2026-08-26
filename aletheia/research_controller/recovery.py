"""Concrete restart recovery from the Kernel ledger and append-only PR-5 receipts."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.execution.allocator import VerifiedQualificationTerminalSource
from aletheia.observations.store import (
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    ObservationAdmissionWrite,
    ObservationValidationReceiptWrite,
    ProtocolCompilationWrite,
    ScientificExecutionAuthorizationWrite,
    ContinuationReceiptWrite,
    get_continuation_receipt_by_slot,
    get_controller_delivery_by_source,
    get_controller_registration_by_quest,
    get_observation_admission_by_slot,
    get_observation_validation_receipt_by_slot,
    get_protocol_compilation_by_action,
    list_scientific_execution_authorizations,
)
from aletheia.observations.scientific_bridge import (
    CommittedObservationAdmission,
    CommittedObservationValidationReceipt,
    ScientificExecutionAuthorization,
)
from aletheia.protocols.compiler import (
    CompilationVerificationError,
    ProtocolCompilationRequest,
    verify_compilation,
)
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.research_controller.continuation import (
    ContinuationReceipt,
    continuation_assessment_source_sha256,
    derive_continuation_v2,
    project_admitted_scientific_observation,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerManifest,
    ResearchControllerRegistration,
    controller_task_spec,
)
from aletheia.research_kernel.reducer import ActionLifecycle, ActionSnapshot
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
)
from aletheia.research_store.store import ResearchKernelStore, ResearchReplayAudit


class ControllerRecoveryError(RuntimeError):
    """Authoritative recovery inputs were missing, inconsistent, or rebound."""


class QualificationTerminalRecoveryPort(Protocol):
    """Verified PR-4 terminal source used during independent restart recovery."""

    def load_verified_qualification_terminal_source(
        self,
        *,
        execution_id: str,
        attempt_id: str,
    ) -> VerifiedQualificationTerminalSource | None: ...

    def load_qualification_terminal_outbox_in_session(
        self,
        session: Session,
        *,
        execution_id: str,
        attempt_id: str,
    ): ...


def _latest_action(audit: ResearchReplayAudit) -> ActionSnapshot | None:
    sequences = {event.event_sha256: event.sequence for event in audit.events}
    undecided_or_applied = tuple(
        action
        for action in audit.state.actions
        if action.lifecycle not in {ActionLifecycle.REJECTED, ActionLifecycle.SUPERSEDED}
    )
    if not undecided_or_applied:
        return None
    latest = max(
        undecided_or_applied,
        key=lambda item: (
            sequences.get(item.proposed_event_sha256, -1),
            item.action_ref.object_sha256,
        ),
    )
    if latest.lifecycle is ActionLifecycle.APPLIED and latest.observation_evidence_ref is None:
        # A committed structural transition is a barrier: the next ordinary tick starts from the
        # new graph state.  It must not fall back to an older completed observation and repeat that
        # observation's continuation directive.  Exact terminal wakeups bypass this selector and
        # still recover their own action by execution/attempt identity.
        return None
    return latest


def _action_is_eligible(action: ActionSnapshot) -> bool:
    if action.lifecycle in {ActionLifecycle.REJECTED, ActionLifecycle.SUPERSEDED}:
        return False
    if action.lifecycle is ActionLifecycle.APPLIED and action.observation_evidence_ref is None:
        # A structural transition completed this action.  The next tick must propose a new typed
        # action rather than treating the transition directive as an executable experiment.
        return False
    return True


def _registration_contract(
    write: ControllerRegistrationWrite,
) -> ResearchControllerRegistration:
    try:
        registration = ResearchControllerRegistration.model_validate(write.registration_json)
        expected = ControllerRegistrationWrite.from_contract(registration)
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError("persisted controller registration is invalid") from exc
    if write != expected:
        raise ControllerRecoveryError("persisted controller registration was rebound")
    return registration


def _delivery_contract(
    *,
    write: ControllerDeliveryWrite,
    registration: ResearchControllerRegistration,
    manifest: ResearchControllerManifest,
    wakeup: ControllerWakeup,
) -> None:
    task = controller_task_spec(manifest=manifest, wakeup=wakeup)
    try:
        expected = ControllerDeliveryWrite.from_contract(
            registration_sha256=registration.registration_sha256,
            wakeup=wakeup,
            task_id=task.task_id,
            delivered_at=write.delivered_at,
            execution_id=write.execution_id,
            attempt_id=write.attempt_id,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError("persisted controller delivery is invalid") from exc
    if write != expected:
        raise ControllerRecoveryError(
            "controller wakeup differs from its exact durable delivery receipt"
        )


def _compilation_contract(
    *,
    write: ProtocolCompilationWrite,
    audit: ResearchReplayAudit,
    action: ActionSnapshot,
) -> tuple[ProtocolCompilationRequest, ProtocolCompilationResult]:
    try:
        request = ProtocolCompilationRequest.model_validate(write.request_json)
        result = ProtocolCompilationResult.model_validate(write.result_json)
        expected = ProtocolCompilationWrite.from_contract(
            quest_id=audit.quest_id,
            action_sha256=action.action_ref.object_sha256,
            request=request,
            result=result,
            registered_at=write.registered_at,
        )
        verify_compilation(request, result)
    except (TypeError, ValueError, CompilationVerificationError) as exc:
        raise ControllerRecoveryError("persisted protocol compilation is invalid") from exc
    scope = request.protocol.graph_scope
    if (
        write != expected
        or scope.scope_binding != audit.scope_binding
        or scope.branch_id != action.branch_id
    ):
        raise ControllerRecoveryError(
            "persisted protocol compilation was rebound from its audited action scope"
        )
    return request, result


def _authorization_contract(
    *,
    write: ScientificExecutionAuthorizationWrite,
    audit: ResearchReplayAudit,
    action: ActionSnapshot,
    compilation_request: ProtocolCompilationRequest,
    compilation_result: ProtocolCompilationResult,
) -> ScientificExecutionAuthorization:
    try:
        authorization = ScientificExecutionAuthorization.model_validate(write.authorization_json)
        expected = ScientificExecutionAuthorizationWrite.from_contract(
            authorization,
            registered_at=write.registered_at,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError(
            "persisted scientific execution authorization is invalid"
        ) from exc
    binding = authorization.message.action_protocol_binding
    source = binding.action_authorized_event
    audited_sources = tuple(
        event
        for event in audit.events
        if event.sequence == source.sequence and event.event_sha256 == source.event_sha256
    )
    if (
        write != expected
        or binding.action.object_ref != action.action_ref
        or binding.compilation_request != compilation_request
        or binding.compilation_result != compilation_result
        or audited_sources != (source,)
    ):
        raise ControllerRecoveryError(
            "scientific execution authorization was rebound from its audited action"
        )
    return authorization


def _validation_contract(
    *,
    write: ObservationValidationReceiptWrite,
    authorization: ScientificExecutionAuthorization,
) -> CommittedObservationValidationReceipt:
    try:
        validation = CommittedObservationValidationReceipt.model_validate(
            write.committed_receipt_json
        )
        expected = ObservationValidationReceiptWrite.from_contract(
            validation,
            quest_id=write.quest_id,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError(
            "persisted observation validation receipt is invalid"
        ) from exc
    message = validation.message.receipt.message
    if (
        write != expected
        or message.raw_run.scientific_authorization != authorization
        or write.authorization_sha256 != authorization.authorization_sha256
    ):
        raise ControllerRecoveryError(
            "observation validation receipt was rebound from its authorization"
        )
    return validation


def _admission_contract(
    *,
    write: ObservationAdmissionWrite,
    validation_write: ObservationValidationReceiptWrite,
    validation: CommittedObservationValidationReceipt,
) -> CommittedObservationAdmission:
    if (
        write.committed_validation_receipt_sha256 != validation_write.committed_receipt_sha256
        or write.validation_receipt_sha256 != validation_write.validation_receipt_sha256
        or write.authorization_sha256 != validation_write.authorization_sha256
        or write.scientific_slot_id != validation_write.scientific_slot_id
    ):
        raise ControllerRecoveryError(
            "observation admission was rebound from its validation receipt"
        )
    try:
        admission = CommittedObservationAdmission.model_validate(write.admission_json)
        expected = ObservationAdmissionWrite.from_contract(
            admission,
            quest_id=write.quest_id,
            incorporated_event_sequence=write.incorporated_event_sequence,
            incorporated_event_sha256=write.incorporated_event_sha256,
            incorporated_event_type=write.incorporated_event_type,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError("persisted observation admission is invalid") from exc
    if (
        write != expected
        or admission.message.decision.message.committed_validation_receipt != validation
    ):
        raise ControllerRecoveryError(
            "observation admission was rebound from its validation receipt"
        )
    return admission


def _continuation_contract(
    *,
    write: ContinuationReceiptWrite,
    action: ActionSnapshot,
    admission: ObservationAdmissionWrite,
    committed_admission: CommittedObservationAdmission,
    incorporation_event: ResearchEvent,
    compilation_write: ProtocolCompilationWrite,
    compilation_request: ProtocolCompilationRequest,
    validation_write: ObservationValidationReceiptWrite,
) -> ContinuationReceipt:
    try:
        receipt = ContinuationReceipt.model_validate(write.receipt_json)
        incorporation = incorporation_event.payload
        if not isinstance(incorporation, ObservationIncorporatedPayload):
            raise ValueError("continuation source event is not an observation incorporation")
        validation = committed_admission.message.decision.message.committed_validation_receipt
        observation = project_admitted_scientific_observation(
            incorporation=incorporation,
            committed_validation=validation,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError("persisted continuation receipt is invalid") from exc
    protocol = compilation_request.protocol
    world_model = protocol.world_model
    if world_model is None:
        raise ControllerRecoveryError(
            "continuation receipt lacks graph-scoped F9-v2 world-model custody"
        )
    try:
        expected_receipt = derive_continuation_v2(
            world_model=world_model,
            observation=observation,
            assessments=receipt.assessments,
            assessment_provenance=receipt.assessment_provenance,
        )
        expected_write = ContinuationReceiptWrite.from_contract(
            expected_receipt,
            quest_id=write.quest_id,
            action_sha256=write.action_sha256,
            observation=observation,
            recorded_at=write.recorded_at,
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRecoveryError(
            "continuation receipt cannot be rederived from durable authority"
        ) from exc
    provenance = receipt.assessment_provenance
    provenance_is_exact = True
    if provenance is not None:
        validation_message = validation.message.receipt.message
        authorization = validation_message.raw_run.scientific_authorization.message
        campaign = validation_message.validation_campaign_projection
        if campaign is None:
            raise ControllerRecoveryError(
                "continuation assessment provenance lacks its validation campaign"
            )
        excluded_principals = {
            authorization.action_protocol_binding.action.proposed_by_principal_id,
            authorization.action_protocol_binding.action_proposed_event.principal_id,
            authorization.action_protocol_binding.action_authorized_event.principal_id,
            authorization.authorized_by_principal_id,
            authorization.validator_principal_id,
            authorization.admission_principal_id,
            authorization.qualification_grant.message.authorized_by_principal_id,
            validation_message.raw_run.accepted_terminal_submission.accepted_by_principal_id,
        }
        expected_source = continuation_assessment_source_sha256(
            quest_id=write.quest_id,
            action_sha256=write.action_sha256,
            scientific_slot_id=write.scientific_slot_id,
            incorporation_event_sha256=incorporation_event.event_sha256,
            world_model_snapshot_sha256=world_model.world_model_sha256,
            observation_projection_sha256=observation.projection_sha256,
            compilation_sha256=compilation_write.compilation_sha256,
            committed_validation_receipt_sha256=validation_write.committed_receipt_sha256,
            validation_campaign_projection_sha256=campaign.projection_sha256,
            committed_admission_sha256=admission.committed_admission_sha256,
        )
        provenance_is_exact = (
            provenance.assessment_source_sha256 == expected_source
            and provenance.assessed_by_principal_id not in excluded_principals
            and incorporation_event.committed_at <= provenance.assessed_at <= write.recorded_at
        )
    if (
        write.quest_id != action.action_ref.quest_id
        or write.action_sha256 != action.action_ref.object_sha256
        or write.scientific_slot_id != admission.scientific_slot_id
        or write.committed_admission_sha256 != admission.committed_admission_sha256
        or write.scientific_observation_sha256 != admission.admitted_observation_sha256
        or write.receipt_sha256 != receipt.receipt_sha256
        or write.scientific_slot_id != receipt.scientific_slot_id
        or write.world_model_snapshot_sha256 != receipt.world_model_snapshot_sha256
        or write.observation_projection_sha256 != receipt.observation_projection_sha256
        or write.disposition != receipt.disposition.value
        or receipt.world_model_snapshot_sha256 != incorporation.source_world_model_sha256
        or receipt.world_model_snapshot_sha256 != world_model.world_model_sha256
        or receipt != expected_receipt
        or write != expected_write
        or not provenance_is_exact
    ):
        raise ControllerRecoveryError(
            "continuation receipt was rebound from its admitted observation"
        )
    return receipt


class PostgreSQLControllerRecoveryAdapter:
    """Rebuild one deterministic controller projection without an in-memory checkpoint."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        terminal_outbox: QualificationTerminalRecoveryPort,
        manifest: ResearchControllerManifest,
    ) -> None:
        self._kernel_store = kernel_store
        self._terminal_outbox = terminal_outbox
        self._manifest = manifest

    def load(self, wakeup: ControllerWakeup) -> ControllerRecoveryProjection:
        with session_scope() as session:
            registration_write = get_controller_registration_by_quest(session, wakeup.quest_id)
            if registration_write is None:
                raise ControllerRecoveryError("Quest has no durable controller registration")
            registration = _registration_contract(registration_write)
            if (
                registration.registration_id != wakeup.registration_id
                or registration.controller_id != self._manifest.controller_id
                or registration.controller_manifest_sha256 != self._manifest.manifest_sha256
                or registration.controller_principal_id != self._manifest.controller_key
            ):
                raise ControllerRecoveryError(
                    "controller wakeup differs from its deployment-pinned registration"
                )
            delivery = get_controller_delivery_by_source(
                session,
                source_kind=wakeup.source_kind.value,
                source_key=wakeup.source_key,
            )
            if delivery is None:
                raise ControllerRecoveryError(
                    "controller wakeup has no exact durable delivery receipt"
                )
            _delivery_contract(
                write=delivery,
                registration=registration,
                manifest=self._manifest,
                wakeup=wakeup,
            )

            audit = self._kernel_store.audit_in_session(session, wakeup.quest_id)
            self._verify_wakeup_source(
                session=session,
                wakeup=wakeup,
                registration=registration,
                audit=audit,
            )
            all_authorizations = list_scientific_execution_authorizations(
                session, quest_id=wakeup.quest_id
            )
            terminal_authorization = None
            if wakeup.source_kind is ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX:
                terminal_authorizations = tuple(
                    item
                    for item in all_authorizations
                    if item.execution_id == delivery.execution_id
                    and item.attempt_id == delivery.attempt_id
                )
                if len(terminal_authorizations) != 1:
                    raise ControllerRecoveryError(
                        "terminal delivery does not resolve one scientific authorization"
                    )
                terminal_authorization = terminal_authorizations[0]
                terminal_actions = tuple(
                    item
                    for item in audit.state.actions
                    if item.action_ref.object_sha256 == terminal_authorization.action_sha256
                    and _action_is_eligible(item)
                )
                if len(terminal_actions) != 1:
                    raise ControllerRecoveryError(
                        "terminal delivery does not resolve one eligible audited action"
                    )
                action = terminal_actions[0]
            else:
                action = _latest_action(audit)
            if action is None:
                return self._projection_without_action(audit)

            action_sha256 = action.action_ref.object_sha256
            compilation = get_protocol_compilation_by_action(
                session,
                quest_id=wakeup.quest_id,
                action_sha256=action_sha256,
            )
            disposition = CompilationDisposition.MISSING
            compilation_request = None
            compilation_result = None
            if compilation is not None:
                compilation_request, compilation_result = _compilation_contract(
                    write=compilation,
                    audit=audit,
                    action=action,
                )
                disposition = (
                    CompilationDisposition.ACCEPTED
                    if compilation_result.report.accepted
                    else CompilationDisposition.BLOCKED
                )

            authorizations = tuple(
                item for item in all_authorizations if item.action_sha256 == action_sha256
            )
            if len(authorizations) > 1:
                raise ControllerRecoveryError(
                    "one action resolved to multiple scientific execution authorizations"
                )
            authorization = authorizations[0] if authorizations else None
            if terminal_authorization is not None and authorization != terminal_authorization:
                raise ControllerRecoveryError(
                    "terminal delivery selected a different action authorization"
                )
            scientific_slot_id = (
                authorization.scientific_slot_id if authorization is not None else None
            )
            if authorization is not None and disposition is not CompilationDisposition.ACCEPTED:
                raise ControllerRecoveryError(
                    "scientific execution authorization lacks an accepted compilation"
                )
            authorization_contract = None
            if authorization is not None:
                if compilation_request is None or compilation_result is None:
                    raise ControllerRecoveryError(
                        "scientific execution authorization lacks a verified compilation"
                    )
                authorization_contract = _authorization_contract(
                    write=authorization,
                    audit=audit,
                    action=action,
                    compilation_request=compilation_request,
                    compilation_result=compilation_result,
                )

            terminal = None
            terminal_source = None
            validation = None
            admission = None
            continuation = None
            if authorization is not None:
                terminal_source = self._terminal_outbox.load_verified_qualification_terminal_source(
                    execution_id=authorization.execution_id,
                    attempt_id=authorization.attempt_id,
                )
                if terminal_source is not None:
                    try:
                        terminal_source = VerifiedQualificationTerminalSource.model_validate(
                            terminal_source.model_dump(mode="python")
                        )
                    except (AttributeError, TypeError, ValueError) as exc:
                        raise ControllerRecoveryError(
                            "execution terminal verifier returned an invalid source"
                        ) from exc
                terminal = self._terminal_outbox.load_qualification_terminal_outbox_in_session(
                    session,
                    execution_id=authorization.execution_id,
                    attempt_id=authorization.attempt_id,
                )
                validation = get_observation_validation_receipt_by_slot(
                    session,
                    quest_id=wakeup.quest_id,
                    scientific_slot_id=authorization.scientific_slot_id,
                )
                admission = get_observation_admission_by_slot(
                    session,
                    quest_id=wakeup.quest_id,
                    scientific_slot_id=authorization.scientific_slot_id,
                )
                continuation = get_continuation_receipt_by_slot(
                    session,
                    quest_id=wakeup.quest_id,
                    scientific_slot_id=authorization.scientific_slot_id,
                )

            if (terminal is None) != (terminal_source is None) or (
                terminal is not None
                and terminal_source is not None
                and (
                    terminal.execution_id != terminal_source.execution_id
                    or terminal.attempt_id != terminal_source.attempt_id
                    or terminal.outbox_id != terminal_source.outbox_id
                    or terminal.terminal_authority_kind != terminal_source.terminal_authority_kind
                    or terminal.terminal_authority_sha256
                    != terminal_source.terminal_authority_sha256
                    or terminal.payload_sha256 != terminal_source.payload_sha256
                    or terminal.created_at != terminal_source.outbox_created_at
                    or terminal_source.qualification_bundle_sha256
                    != authorization.qualification_bundle_sha256
                    or terminal_source.qualification_grant_sha256
                    != authorization.qualification_grant_sha256
                    or authorization.registered_at >= terminal_source.qualification_admitted_at
                )
            ):
                raise ControllerRecoveryError(
                    "execution terminal outbox lacks its exact verified PR-4 lineage"
                )

            if terminal is not None and (
                authorization is None
                or terminal.execution_id != authorization.execution_id
                or terminal.attempt_id != authorization.attempt_id
            ):
                raise ControllerRecoveryError(
                    "execution terminal authority was rebound from its authorization"
                )

            if wakeup.source_kind is ControllerWakeupKind.EXECUTION_TERMINAL_OUTBOX:
                if (
                    terminal is None
                    or terminal.outbox_id != wakeup.source_key
                    or terminal.terminal_authority_sha256 != wakeup.source_sha256
                    or terminal.execution_id != delivery.execution_id
                    or terminal.attempt_id != delivery.attempt_id
                ):
                    raise ControllerRecoveryError(
                        "execution-terminal wakeup differs from durable PR-4 authority"
                    )

            validation_contract = None
            blocker_codes: tuple[str, ...] = ()
            if validation is not None:
                if authorization_contract is None or terminal is None:
                    raise ControllerRecoveryError(
                        "validation receipt lacks its authorization or terminal authority"
                    )
                validation_contract = _validation_contract(
                    write=validation,
                    authorization=authorization_contract,
                )
                accepted_terminal = (
                    validation_contract.message.receipt.message.raw_run.accepted_terminal_submission
                )
                if (
                    accepted_terminal.accepted_terminal_submission_sha256
                    != terminal.terminal_authority_sha256
                ):
                    raise ControllerRecoveryError(
                        "validation receipt was rebound from its terminal authority"
                    )
                if validation.disposition == "blocked_execution":
                    blocker_codes = ("observation_validation_blocked_execution",)
                elif validation.disposition == "rejected_scientific":
                    blocker_codes = ("observation_validation_rejected_scientific",)

            observation_incorporated = False
            incorporation_payload = None
            admitted_contract = None
            if admission is not None:
                if validation is None or validation_contract is None:
                    raise ControllerRecoveryError(
                        "observation admission lacks its exact validation receipt"
                    )
                admitted_contract = _admission_contract(
                    write=admission,
                    validation_write=validation,
                    validation=validation_contract,
                )
                if validation.disposition != "validated_confirmation" or validation.outcome is None:
                    raise ControllerRecoveryError(
                        "admitted observation lacks a confirmed scientific outcome"
                    )
                matching_events = tuple(
                    event
                    for event in audit.events
                    if event.event_type is EventType.OBSERVATION_INCORPORATED
                    and isinstance(event.payload, ObservationIncorporatedPayload)
                    and event.sequence == admission.incorporated_event_sequence
                    and event.payload.action_id == action.action_ref.object_id
                    and event.payload.branch_id == action.branch_id
                    and event.payload.scientific_slot_id == admission.scientific_slot_id
                    and event.payload.committed_admission_sha256
                    == admission.committed_admission_sha256
                    and event.payload.scientific_observation_sha256
                    == admission.admitted_observation_sha256
                    and event.payload.outcome == validation.outcome
                    and event.event_sha256 == admission.incorporated_event_sha256
                )
                observation_incorporated = len(matching_events) == 1
                if (
                    not observation_incorporated
                    or action.lifecycle is not ActionLifecycle.APPLIED
                    or action.observation_evidence_ref != matching_events[0].payload.evidence_ref
                ):
                    raise ControllerRecoveryError(
                        "admission is missing its exact audited observation event"
                    )
                if observation_incorporated:
                    incorporation_payload = matching_events[0].payload
            if continuation is not None:
                if admission is None or admitted_contract is None or incorporation_payload is None:
                    raise ControllerRecoveryError(
                        "continuation receipt lacks an incorporated admitted observation"
                    )
                _continuation_contract(
                    write=continuation,
                    action=action,
                    admission=admission,
                    committed_admission=admitted_contract,
                    incorporation_event=matching_events[0],
                    compilation_write=compilation,
                    compilation_request=compilation_request,
                    validation_write=validation,
                )

            try:
                return ControllerRecoveryProjection(
                    quest_id=wakeup.quest_id,
                    action_sha256=action_sha256,
                    scientific_slot_id=scientific_slot_id,
                    audited_stream_version=audit.state.stream_version,
                    audited_tail_event_sha256=audit.state.tail_event_sha256,
                    audited_snapshot_sha256=audit.state.snapshot_sha256,
                    action_authorized=action.lifecycle
                    in {ActionLifecycle.AUTHORIZED, ActionLifecycle.APPLIED},
                    compilation_disposition=disposition,
                    scientific_execution_authorization_registered=authorization is not None,
                    execution_terminal_observed=terminal is not None,
                    validation_committed=validation is not None,
                    admission_committed=admission is not None,
                    observation_incorporated=observation_incorporated,
                    continuation_committed=continuation is not None,
                    blocker_codes=blocker_codes,
                )
            except ValueError as exc:
                raise ControllerRecoveryError(
                    "durable controller receipts do not form a monotonic authority chain"
                ) from exc

    @staticmethod
    def _projection_without_action(audit: ResearchReplayAudit) -> ControllerRecoveryProjection:
        if audit.state.tail_event_sha256 is None:
            raise ControllerRecoveryError("registered Quest has no committed Kernel event")
        return ControllerRecoveryProjection(
            quest_id=audit.quest_id,
            action_sha256=None,
            scientific_slot_id=None,
            audited_stream_version=audit.state.stream_version,
            audited_tail_event_sha256=audit.state.tail_event_sha256,
            audited_snapshot_sha256=audit.state.snapshot_sha256,
            action_authorized=False,
            compilation_disposition=CompilationDisposition.MISSING,
            scientific_execution_authorization_registered=False,
            execution_terminal_observed=False,
            validation_committed=False,
            admission_committed=False,
            observation_incorporated=False,
            continuation_committed=False,
            blocker_codes=(),
        )

    @staticmethod
    def _verify_wakeup_source(
        *,
        session: Session,
        wakeup: ControllerWakeup,
        registration: ResearchControllerRegistration,
        audit: ResearchReplayAudit,
    ) -> None:
        del session  # Delivery existence and identity were verified before the source authority.
        if wakeup.source_kind is ControllerWakeupKind.LAUNCH:
            if (
                wakeup.source_key != registration.registration_id
                or wakeup.source_sha256 != registration.launch_request.request_sha256
            ):
                raise ControllerRecoveryError("launch wakeup differs from its frozen request")
            return
        if wakeup.source_kind is ControllerWakeupKind.KERNEL_OUTBOX:
            events = tuple(
                event
                for event in audit.events
                if event.sequence == wakeup.source_stream_version
                and event.event_sha256 == wakeup.source_sha256
            )
            if len(events) != 1 or wakeup.source_key != f"rko_{wakeup.source_sha256[:32]}":
                raise ControllerRecoveryError(
                    "Kernel outbox wakeup is absent from the audited event stream"
                )


__all__ = [
    "ControllerRecoveryError",
    "PostgreSQLControllerRecoveryAdapter",
    "QualificationTerminalRecoveryPort",
]
