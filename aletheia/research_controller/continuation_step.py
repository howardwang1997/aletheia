"""Durable continuation assessment for one incorporated graph-scoped observation.

The assessment provider is powerless: it receives a closed, content-addressed context and returns
only per-prediction fit assessments.  This service replays the Kernel and all compilation,
validation, admission, and incorporation authority before and after that call, derives the typed
continuation with the pure v2 rule, and appends one exact receipt.  It cannot sign a Kernel command,
admit an observation, or synthesize a legacy Run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    CommittedObservationAdmission,
    CommittedObservationValidationReceipt,
    ObservationAdmissionDisposition,
)
from aletheia.observations.store import (
    ContinuationReceiptWrite,
    ObservationAdmissionWrite,
    ObservationValidationReceiptWrite,
    ProtocolCompilationWrite,
    get_continuation_receipt_by_slot,
    get_observation_admission_by_slot,
    get_observation_validation_receipt_by_slot,
    get_protocol_compilation_by_action,
    record_continuation_receipt,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, verify_compilation
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.protocols.world_models import WorldModelSnapshotV2
from aletheia.research_controller.continuation import (
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
    ContinuationAssessmentProvenance,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    ScientificObservationProjection,
    continuation_assessment_source_sha256,
    derive_continuation_v2,
    project_admitted_scientific_observation,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchActionProposal,
    ResearchEvent,
    canonical_sha256,
)
from aletheia.research_store.store import (
    ResearchKernelStore,
    ResearchObjectArchive,
    ResearchReplayAudit,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"


class ContinuationAssessmentStepError(RuntimeError):
    """Continuation assessment escaped its exact durable scientific source."""


class ContinuationAssessmentUnavailable(ContinuationAssessmentStepError):
    """No independent assessment implementation is currently available."""

    def __init__(self, blocker_codes: tuple[str, ...]) -> None:
        if not blocker_codes or blocker_codes != tuple(sorted(set(blocker_codes))):
            raise ValueError("continuation blockers must be nonempty and canonical")
        self.blocker_codes = blocker_codes
        super().__init__(",".join(blocker_codes))


class ContinuationAssessmentPolicyPin(ControllerModel):
    """Deployment-frozen assessor implementation, principals, fit rules, and identity scheme."""

    schema_name: Literal["aletheia.continuation_assessment_policy_pin"] = (
        "aletheia.continuation_assessment_policy_pin"
    )
    schema_version: Literal[1] = 1
    assessment_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    observed_outcome_identity_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    allowed_assessor_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    allowed_fit_rule_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    missing_active_hypothesis_disposition: Literal["redesign_observable"] = "redesign_observable"
    legacy_continuation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _policy_is_canonical(self) -> "ContinuationAssessmentPolicyPin":
        if self.observed_outcome_identity_policy_sha256 != OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256:
            raise ValueError("continuation policy changed the observed-outcome identity scheme")
        if self.allowed_assessor_principal_ids != tuple(
            sorted(set(self.allowed_assessor_principal_ids))
        ) or any(
            re.fullmatch(_PRINCIPAL_PATTERN, principal) is None
            for principal in self.allowed_assessor_principal_ids
        ):
            raise ValueError("continuation assessor principals must be unique and canonical")
        if self.allowed_fit_rule_sha256s != tuple(
            sorted(set(self.allowed_fit_rule_sha256s))
        ) or any(
            re.fullmatch(_SHA256_PATTERN, fit_rule) is None
            for fit_rule in self.allowed_fit_rule_sha256s
        ):
            raise ValueError("continuation fit rules must be unique and canonical")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class AuthorizedContinuationAssessmentContext(ControllerModel):
    """Minimum exact source disclosed to a powerless fit-assessment provider."""

    schema_name: Literal["aletheia.authorized_continuation_assessment_context"] = (
        "aletheia.authorized_continuation_assessment_context"
    )
    schema_version: Literal[1] = 1
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    recovery_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    action: ResearchActionProposal
    scientific_slot_id: str = Field(pattern=r"^sos_[0-9a-f]{32}$")
    expected_stream_version: int = Field(ge=3)
    expected_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    incorporation_event: ResearchEvent
    world_model: WorldModelSnapshotV2
    observation: ScientificObservationProjection
    compilation_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_campaign_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    excluded_assessor_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    assessment_policy: ContinuationAssessmentPolicyPin
    latest_event_committed_at: AwareDatetime
    execution_access_allowed: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    signing_key_available: Literal[False] = False

    @model_validator(mode="after")
    def _context_is_exact(self) -> "AuthorizedContinuationAssessmentContext":
        event = self.incorporation_event
        payload = event.payload
        graph_scope = self.world_model.graph_scope
        if (
            event.quest_id != self.quest_id
            or event.event_type is not EventType.OBSERVATION_INCORPORATED
            or not isinstance(payload, ObservationIncorporatedPayload)
            or event.sequence != self.expected_stream_version
            or event.event_sha256 != self.expected_tail_event_sha256
            or event.committed_at != self.latest_event_committed_at
            or self.action.quest_id != self.quest_id
            or self.action.object_sha256 == ""
            or payload.action_id != self.action.action_id
            or payload.branch_id != graph_scope.branch_id
            or graph_scope.question_ref != self.action.question_ref
            or self.scientific_slot_id != self.observation.scientific_slot_id
            or payload.scientific_slot_id != self.scientific_slot_id
            or payload.committed_admission_sha256 != self.committed_admission_sha256
            or payload.scientific_observation_sha256
            != self.observation.scientific_observation_sha256
            or payload.source_world_model_sha256 != self.world_model.world_model_sha256
            or payload.source_world_model_sha256 != self.observation.source_world_model_sha256
            or payload.outcome != self.observation.outcome.value
            or self.observation.committed_admission_sha256 != self.committed_admission_sha256
            or self.excluded_assessor_principal_ids
            != tuple(sorted(set(self.excluded_assessor_principal_ids)))
            or any(
                re.fullmatch(_PRINCIPAL_PATTERN, principal) is None
                for principal in self.excluded_assessor_principal_ids
            )
        ):
            raise ValueError("continuation context escaped its incorporated observation")
        return self

    @property
    def assessment_source_sha256(self) -> str:
        return continuation_assessment_source_sha256(
            quest_id=self.quest_id,
            action_sha256=self.action.object_sha256,
            scientific_slot_id=self.scientific_slot_id,
            incorporation_event_sha256=self.incorporation_event.event_sha256,
            world_model_snapshot_sha256=self.world_model.world_model_sha256,
            observation_projection_sha256=self.observation.projection_sha256,
            compilation_sha256=self.compilation_sha256,
            committed_validation_receipt_sha256=self.committed_validation_receipt_sha256,
            validation_campaign_projection_sha256=self.validation_campaign_projection_sha256,
            committed_admission_sha256=self.committed_admission_sha256,
        )

    @property
    def context_sha256(self) -> str:
        return canonical_sha256(self)


class PreparedContinuationAssessment(ControllerModel):
    """Provider result over one exact disclosed context, without mutation authority."""

    schema_name: Literal["aletheia.prepared_continuation_assessment"] = (
        "aletheia.prepared_continuation_assessment"
    )
    schema_version: Literal[1] = 1
    context_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessments: tuple[HypothesisPredictionAssessment, ...] = Field(max_length=64)
    assessment_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    assessed_by_principal_id: str = Field(pattern=_PRINCIPAL_PATTERN)
    assessed_at: AwareDatetime
    legacy_run_synthesized: Literal[False] = False
    legacy_optimize_used: Literal[False] = False
    scientific_authority_conferred: Literal[False] = False

    @model_validator(mode="after")
    def _assessments_are_canonical(self) -> "PreparedContinuationAssessment":
        order = tuple(
            (item.hypothesis_sha256, item.prediction_sha256, item.assessment_sha256)
            for item in self.assessments
        )
        if order != tuple(sorted(set(order))):
            raise ValueError("prepared continuation assessments must be unique and canonical")
        artifacts = tuple(item.assessment_artifact_sha256 for item in self.assessments)
        if len(artifacts) != len(set(artifacts)):
            raise ValueError("continuation assessment artifacts must be unique")
        return self

    @property
    def preparation_sha256(self) -> str:
        return canonical_sha256(self)


class ContinuationAssessmentProviderPort(Protocol):
    """Replaceable external assessment implementation; receives no database or signing key."""

    def assess_continuation(
        self, context: AuthorizedContinuationAssessmentContext
    ) -> PreparedContinuationAssessment: ...


class ContinuationAssessmentArtifactCustodyPort(Protocol):
    """Fresh-byte verifier for every artifact referenced by an assessment or durable retry."""

    def verify_assessment_artifacts(
        self,
        *,
        context: AuthorizedContinuationAssessmentContext,
        assessments: tuple[HypothesisPredictionAssessment, ...],
    ) -> None: ...


class ContinuationMaterializationPort(Protocol):
    authority_binding: ControllerStepAuthorityBinding

    def derive_and_register(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ContinuationReceiptWrite: ...


DatabaseClock = Callable[[Session], datetime]
SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


def _database_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL invariant
        raise ContinuationAssessmentStepError(
            "PostgreSQL did not provide continuation registry time"
        )
    return observed


def _require_continuation_tick(
    *,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
) -> None:
    if (
        plan_recovery_tick(projection) != plan
        or plan.step is not ControllerStep.DERIVE_CONTINUATION
        or wakeup.quest_id != projection.quest_id
        or projection.action_sha256 is None
        or projection.scientific_slot_id is None
        or not projection.action_authorized
        or projection.compilation_disposition is not CompilationDisposition.ACCEPTED
        or not projection.scientific_execution_authorization_registered
        or not projection.execution_terminal_observed
        or not projection.validation_committed
        or not projection.admission_committed
        or not projection.observation_incorporated
        or projection.continuation_committed
        or projection.blocker_codes
    ):
        raise ContinuationAssessmentStepError(
            "continuation assessment received a stale controller tick"
        )


def _validate_prepared_assessment(
    *,
    context: AuthorizedContinuationAssessmentContext,
    prepared: PreparedContinuationAssessment,
) -> PreparedContinuationAssessment:
    try:
        prepared = PreparedContinuationAssessment.model_validate(prepared.model_dump(mode="python"))
        policy = context.assessment_policy
        if (
            prepared.context_sha256 != context.context_sha256
            or prepared.assessment_implementation_sha256 != policy.assessment_implementation_sha256
            or prepared.assessed_by_principal_id not in policy.allowed_assessor_principal_ids
            or prepared.assessed_by_principal_id in context.excluded_assessor_principal_ids
            or prepared.assessed_at < context.latest_event_committed_at
            or any(
                item.fit_rule_sha256 not in policy.allowed_fit_rule_sha256s
                for item in prepared.assessments
            )
        ):
            raise ValueError("prepared continuation assessment escaped its source or policy")
        return prepared
    except ContinuationAssessmentStepError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider-owned values fail closed
        raise ContinuationAssessmentStepError(
            "prepared continuation assessment verification failed closed"
        ) from exc


class DurableContinuationAssessmentService:
    """Two-audit, first-writer-wins continuation derivation and append-only custody."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        object_archive: ResearchObjectArchive,
        provider: ContinuationAssessmentProviderPort,
        artifact_custody: ContinuationAssessmentArtifactCustodyPort,
        assessment_policy: ContinuationAssessmentPolicyPin,
        authority_binding: ControllerStepAuthorityBinding,
        sessions: SessionScopeFactory = session_scope,
        database_clock: DatabaseClock = _database_time,
    ) -> None:
        if (
            not callable(getattr(kernel_store, "audit_in_session", None))
            or not callable(getattr(object_archive, "load_object", None))
            or not callable(getattr(provider, "assess_continuation", None))
            or not callable(getattr(artifact_custody, "verify_assessment_artifacts", None))
            or not callable(sessions)
            or not callable(database_clock)
        ):
            raise TypeError("continuation assessment service dependencies are invalid")
        policy = ContinuationAssessmentPolicyPin.model_validate(
            assessment_policy.model_dump(mode="python")
        )
        binding = ControllerStepAuthorityBinding.model_validate(
            authority_binding.model_dump(mode="python")
        )
        if (
            binding.role is not ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT
            or binding.key_id is not None
            or binding.policy_sha256 != policy.policy_sha256
        ):
            raise ValueError("continuation assessor differs from its pinned policy authority")
        self._kernel_store = kernel_store
        self._object_archive = object_archive
        self._provider = provider
        self._artifact_custody = artifact_custody
        self._policy = policy
        self._sessions = sessions
        self._database_clock = database_clock
        self.authority_binding = binding

    def derive_and_register(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ContinuationReceiptWrite:
        try:
            _require_continuation_tick(wakeup=wakeup, projection=projection, plan=plan)
            with self._sessions() as session:
                context = self._context(
                    session=session,
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                )
                existing = get_continuation_receipt_by_slot(
                    session,
                    quest_id=projection.quest_id,
                    scientific_slot_id=projection.scientific_slot_id,
                )
                if existing is not None:
                    return self._verify_registered(context=context, write=existing)

            prepared = _validate_prepared_assessment(
                context=context,
                prepared=self._provider.assess_continuation(context),
            )
            self._verify_artifact_custody(
                context=context,
                assessments=prepared.assessments,
            )
            provenance = ContinuationAssessmentProvenance(
                assessment_source_sha256=context.assessment_source_sha256,
                assessment_policy_sha256=self._policy.policy_sha256,
                assessment_implementation_sha256=prepared.assessment_implementation_sha256,
                assessed_by_principal_id=prepared.assessed_by_principal_id,
                assessed_at=prepared.assessed_at,
            )
            receipt = derive_continuation_v2(
                world_model=context.world_model,
                observation=context.observation,
                assessments=prepared.assessments,
                assessment_provenance=provenance,
            )

            with self._sessions() as session:
                locked_context = self._context(
                    session=session,
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                )
                if locked_context != context:
                    raise ContinuationAssessmentStepError(
                        "continuation source changed before receipt registration"
                    )
                winner = get_continuation_receipt_by_slot(
                    session,
                    quest_id=projection.quest_id,
                    scientific_slot_id=projection.scientific_slot_id,
                )
                if winner is not None:
                    return self._verify_registered(context=context, write=winner)
                recorded_at = self._database_clock(session)
                if recorded_at < prepared.assessed_at:
                    raise ContinuationAssessmentStepError(
                        "continuation registry time predates its assessment"
                    )
                write = ContinuationReceiptWrite.from_contract(
                    receipt,
                    quest_id=context.quest_id,
                    action_sha256=context.action.object_sha256,
                    observation=context.observation,
                    recorded_at=recorded_at,
                )
                record_continuation_receipt(session, write)
                return self._verify_registered(context=context, write=write)
        except (ContinuationAssessmentUnavailable, ContinuationAssessmentStepError):
            raise
        except Exception as exc:  # noqa: BLE001 - DB/CAS/provider failures fail closed
            raise ContinuationAssessmentStepError("continuation assessment failed closed") from exc

    def _context(
        self,
        *,
        session: Session,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> AuthorizedContinuationAssessmentContext:
        audit_candidate = self._kernel_store.audit_in_session(session, projection.quest_id)
        audit = ResearchReplayAudit.model_validate(audit_candidate.model_dump(mode="python"))
        state = audit.state
        if (
            audit.quest_id != projection.quest_id
            or state.quest_id != projection.quest_id
            or state.terminal
            or state.stream_version != projection.audited_stream_version
            or state.tail_event_sha256 != projection.audited_tail_event_sha256
            or state.snapshot_sha256 != projection.audited_snapshot_sha256
            or len(audit.events) != len(audit.verified_snapshot_sha256s)
            or not audit.events
            or audit.events[-1].event_sha256 != state.tail_event_sha256
            or audit.verified_snapshot_sha256s[-1] != state.snapshot_sha256
        ):
            raise ContinuationAssessmentStepError(
                "Kernel audit differs from the continuation recovery projection"
            )
        action_states = tuple(
            item
            for item in state.actions
            if item.action_ref.object_sha256 == projection.action_sha256
        )
        if (
            len(action_states) != 1
            or action_states[0].lifecycle is not ActionLifecycle.APPLIED
            or action_states[0].observation_evidence_ref is None
        ):
            raise ContinuationAssessmentStepError(
                "continuation source is not one observation-applied action"
            )
        action_state = action_states[0]
        archived = self._object_archive.load_object(action_state.action_ref)
        if not isinstance(archived.payload, ResearchActionProposal):
            raise ContinuationAssessmentStepError("continuation source CAS object is not an action")
        action = archived.payload

        compilation_write = get_protocol_compilation_by_action(
            session,
            quest_id=projection.quest_id,
            action_sha256=projection.action_sha256,
        )
        validation_write = get_observation_validation_receipt_by_slot(
            session,
            quest_id=projection.quest_id,
            scientific_slot_id=projection.scientific_slot_id,
        )
        admission_write = get_observation_admission_by_slot(
            session,
            quest_id=projection.quest_id,
            scientific_slot_id=projection.scientific_slot_id,
        )
        if compilation_write is None or validation_write is None or admission_write is None:
            raise ContinuationAssessmentStepError(
                "continuation source is missing compilation, validation, or admission custody"
            )
        request, result = self._compilation_contract(
            write=compilation_write,
            action_sha256=projection.action_sha256,
        )
        committed_validation = self._validation_contract(validation_write)
        self._admission_contract(
            write=admission_write,
            validation_write=validation_write,
            validation=committed_validation,
        )
        incorporation_events = tuple(
            event
            for event in audit.events
            if event.event_type is EventType.OBSERVATION_INCORPORATED
            and isinstance(event.payload, ObservationIncorporatedPayload)
            and event.sequence == admission_write.incorporated_event_sequence
            and event.event_sha256 == admission_write.incorporated_event_sha256
            and event.payload.action_id == action.action_id
            and event.payload.branch_id == action_state.branch_id
            and event.payload.scientific_slot_id == admission_write.scientific_slot_id
            and event.payload.committed_admission_sha256
            == admission_write.committed_admission_sha256
            and event.payload.scientific_observation_sha256
            == admission_write.admitted_observation_sha256
            and event.payload.outcome == validation_write.outcome
        )
        if (
            incorporation_events != (audit.events[-1],)
            or action_state.decided_event_sha256 != incorporation_events[0].event_sha256
            or action_state.observation_evidence_ref != incorporation_events[0].payload.evidence_ref
        ):
            raise ContinuationAssessmentStepError(
                "continuation admission lacks its exact current Kernel incorporation"
            )
        incorporation = incorporation_events[0]
        binding = committed_validation.message.receipt.message.raw_run.scientific_authorization.message.action_protocol_binding
        proposed = binding.action_proposed_event
        authorized = binding.action_authorized_event
        if (
            binding.action != action
            or binding.compilation_request != request
            or binding.compilation_result != result
            or tuple(event for event in audit.events if event.event_sha256 == proposed.event_sha256)
            != (proposed,)
            or tuple(
                event for event in audit.events if event.event_sha256 == authorized.event_sha256
            )
            != (authorized,)
            or action_state.proposed_event_sha256 != proposed.event_sha256
            or authorized.sequence != proposed.sequence + 1
            or authorized.parent_event_sha256 != proposed.event_sha256
        ):
            raise ContinuationAssessmentStepError(
                "continuation source rebound its authorized action or compilation"
            )
        protocol = request.protocol
        world_model = protocol.world_model
        campaign = committed_validation.message.receipt.message.validation_campaign_projection
        if (
            world_model is None
            or campaign is None
            or campaign.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
            or campaign.validation_batch_sha256 is None
            or campaign.outcome_bin_id is None
            or protocol.graph_scope.scope_binding != audit.scope_binding
            or protocol.graph_scope.branch_id != action_state.branch_id
            or protocol.graph_scope.question_ref != action.question_ref
        ):
            raise ContinuationAssessmentStepError(
                "continuation source lacks graph-scoped F9-v2 validation custody"
            )
        observation = project_admitted_scientific_observation(
            incorporation=incorporation.payload,
            committed_validation=committed_validation,
        )
        authorization = (
            committed_validation.message.receipt.message.raw_run.scientific_authorization.message
        )
        excluded_principals = tuple(
            sorted(
                {
                    action.proposed_by_principal_id,
                    proposed.principal_id,
                    authorized.principal_id,
                    authorization.authorized_by_principal_id,
                    authorization.validator_principal_id,
                    authorization.admission_principal_id,
                    authorization.qualification_grant.message.authorized_by_principal_id,
                    committed_validation.message.receipt.message.raw_run.accepted_terminal_submission.accepted_by_principal_id,
                }
            )
        )
        return AuthorizedContinuationAssessmentContext(
            wakeup_sha256=wakeup.wakeup_sha256,
            recovery_projection_sha256=projection.projection_sha256,
            plan_sha256=plan.plan_sha256,
            quest_id=projection.quest_id,
            action=action,
            scientific_slot_id=projection.scientific_slot_id,
            expected_stream_version=projection.audited_stream_version,
            expected_tail_event_sha256=projection.audited_tail_event_sha256,
            expected_snapshot_sha256=projection.audited_snapshot_sha256,
            incorporation_event=incorporation,
            world_model=world_model,
            observation=observation,
            compilation_sha256=compilation_write.compilation_sha256,
            committed_validation_receipt_sha256=(validation_write.committed_receipt_sha256),
            validation_campaign_projection_sha256=campaign.projection_sha256,
            committed_admission_sha256=admission_write.committed_admission_sha256,
            excluded_assessor_principal_ids=excluded_principals,
            assessment_policy=self._policy,
            latest_event_committed_at=incorporation.committed_at,
        )

    @staticmethod
    def _compilation_contract(
        *,
        write: ProtocolCompilationWrite,
        action_sha256: str,
    ) -> tuple[ProtocolCompilationRequest, ProtocolCompilationResult]:
        try:
            request = ProtocolCompilationRequest.model_validate(write.request_json)
            result = ProtocolCompilationResult.model_validate(write.result_json)
            expected = ProtocolCompilationWrite.from_contract(
                quest_id=write.quest_id,
                action_sha256=action_sha256,
                request=request,
                result=result,
                registered_at=write.registered_at,
            )
            verify_compilation(request, result)
        except Exception as exc:  # noqa: BLE001 - persisted compiler material fails closed
            raise ContinuationAssessmentStepError(
                "continuation compilation custody is invalid"
            ) from exc
        if write != expected or not result.report.accepted or result.work_order is None:
            raise ContinuationAssessmentStepError(
                "continuation requires one exact accepted protocol compilation"
            )
        return request, result

    @staticmethod
    def _validation_contract(
        write: ObservationValidationReceiptWrite,
    ) -> CommittedObservationValidationReceipt:
        try:
            validation = CommittedObservationValidationReceipt.model_validate(
                write.committed_receipt_json
            )
            expected = ObservationValidationReceiptWrite.from_contract(
                validation,
                quest_id=write.quest_id,
            )
        except Exception as exc:  # noqa: BLE001 - signed validation material fails closed
            raise ContinuationAssessmentStepError(
                "continuation validation custody is invalid"
            ) from exc
        message = validation.message.receipt.message
        if (
            write != expected
            or message.disposition is not BridgeValidationDisposition.VALIDATED_CONFIRMATION
            or message.outcome is None
            or message.scientific_observation_sha256 is None
        ):
            raise ContinuationAssessmentStepError(
                "continuation requires one exact confirmed validation"
            )
        return validation

    @staticmethod
    def _admission_contract(
        *,
        write: ObservationAdmissionWrite,
        validation_write: ObservationValidationReceiptWrite,
        validation: CommittedObservationValidationReceipt,
    ) -> CommittedObservationAdmission:
        try:
            admission = CommittedObservationAdmission.model_validate(write.admission_json)
            expected = ObservationAdmissionWrite.from_contract(
                admission,
                quest_id=write.quest_id,
                incorporated_event_sequence=write.incorporated_event_sequence,
                incorporated_event_sha256=write.incorporated_event_sha256,
                incorporated_event_type=write.incorporated_event_type,
            )
        except Exception as exc:  # noqa: BLE001 - signed admission material fails closed
            raise ContinuationAssessmentStepError(
                "continuation admission custody is invalid"
            ) from exc
        decision = admission.message.decision.message
        if (
            write != expected
            or decision.disposition is not ObservationAdmissionDisposition.ADMITTED
            or decision.committed_validation_receipt != validation
            or write.committed_validation_receipt_sha256
            != validation_write.committed_receipt_sha256
            or write.validation_receipt_sha256 != validation_write.validation_receipt_sha256
            or write.authorization_sha256 != validation_write.authorization_sha256
            or write.scientific_slot_id != validation_write.scientific_slot_id
        ):
            raise ContinuationAssessmentStepError(
                "continuation admission was rebound from its validation"
            )
        return admission

    def _verify_registered(
        self,
        *,
        context: AuthorizedContinuationAssessmentContext,
        write: ContinuationReceiptWrite,
    ) -> ContinuationReceiptWrite:
        try:
            receipt = ContinuationReceipt.model_validate(write.receipt_json)
            provenance = receipt.assessment_provenance
            if provenance is None:
                raise ValueError("continuation receipt lacks assessment provenance")
            prepared = PreparedContinuationAssessment(
                context_sha256=context.context_sha256,
                assessments=receipt.assessments,
                assessment_implementation_sha256=(provenance.assessment_implementation_sha256),
                assessed_by_principal_id=provenance.assessed_by_principal_id,
                assessed_at=provenance.assessed_at,
            )
            _validate_prepared_assessment(context=context, prepared=prepared)
            self._verify_artifact_custody(
                context=context,
                assessments=receipt.assessments,
            )
            expected_receipt = derive_continuation_v2(
                world_model=context.world_model,
                observation=context.observation,
                assessments=receipt.assessments,
                assessment_provenance=provenance,
            )
            expected_write = ContinuationReceiptWrite.from_contract(
                expected_receipt,
                quest_id=context.quest_id,
                action_sha256=context.action.object_sha256,
                observation=context.observation,
                recorded_at=write.recorded_at,
            )
            if (
                provenance.assessment_source_sha256 != context.assessment_source_sha256
                or provenance.assessment_policy_sha256 != context.assessment_policy.policy_sha256
                or receipt != expected_receipt
                or write != expected_write
                or write.recorded_at < provenance.assessed_at
            ):
                raise ValueError("registered continuation receipt was rebound")
            return write
        except ContinuationAssessmentStepError:
            raise
        except Exception as exc:  # noqa: BLE001 - persisted receipt fails closed
            raise ContinuationAssessmentStepError(
                "registered continuation receipt verification failed closed"
            ) from exc

    def _verify_artifact_custody(
        self,
        *,
        context: AuthorizedContinuationAssessmentContext,
        assessments: tuple[HypothesisPredictionAssessment, ...],
    ) -> None:
        try:
            self._artifact_custody.verify_assessment_artifacts(
                context=context,
                assessments=assessments,
            )
        except ContinuationAssessmentStepError:
            raise
        except Exception as exc:  # noqa: BLE001 - external artifact verifier fails closed
            raise ContinuationAssessmentStepError(
                "continuation assessment artifact custody failed closed"
            ) from exc


class ContinuationAssessmentStepAdapter:
    """Controller adapter for one deterministic, durable continuation assessment."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        assessments: ContinuationMaterializationPort,
    ) -> None:
        frozen = ControllerStepAdapterManifest.model_validate(manifest.model_dump(mode="python"))
        binding = ControllerStepAuthorityBinding.model_validate(
            assessments.authority_binding.model_dump(mode="python")
        )
        if (
            frozen.step is not ControllerStep.DERIVE_CONTINUATION
            or frozen.authorities != (binding,)
            or binding.role is not ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT
        ):
            raise ValueError("continuation adapter differs from its step manifest")
        self.manifest = frozen
        self._assessments = assessments

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            _require_continuation_tick(wakeup=wakeup, projection=projection, plan=plan)
            write = self._assessments.derive_and_register(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
            )
            write = ContinuationReceiptWrite.model_validate(write.model_dump(mode="python"))
            receipt = ContinuationReceipt.model_validate(write.receipt_json)
            provenance = receipt.assessment_provenance
            if (
                provenance is None
                or write.receipt_sha256 != receipt.receipt_sha256
                or write.quest_id != projection.quest_id
                or write.action_sha256 != projection.action_sha256
                or write.scientific_slot_id != projection.scientific_slot_id
                or write.world_model_snapshot_sha256 != receipt.world_model_snapshot_sha256
                or write.observation_projection_sha256 != receipt.observation_projection_sha256
                or write.disposition != receipt.disposition.value
            ):
                raise ContinuationAssessmentStepError(
                    "continuation service returned a rebound durable result"
                )
        except ContinuationAssessmentUnavailable as exc:
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.BLOCKED,
                result_artifact_sha256s=(),
                blocker_codes=exc.blocker_codes,
            )
        except Exception as exc:  # noqa: BLE001 - adapter/service result fails closed
            raise ControllerStepExecutionError(
                "continuation assessment step failed closed"
            ) from exc
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.COMPLETED,
            result_artifact_sha256s=tuple(
                sorted(
                    {
                        write.receipt_sha256,
                        write.observation_projection_sha256,
                        provenance.provenance_sha256,
                        *(item.assessment_artifact_sha256 for item in receipt.assessments),
                    }
                )
            ),
            blocker_codes=(),
        )


__all__ = [
    "AuthorizedContinuationAssessmentContext",
    "ContinuationAssessmentArtifactCustodyPort",
    "ContinuationAssessmentPolicyPin",
    "ContinuationAssessmentProviderPort",
    "ContinuationAssessmentStepAdapter",
    "ContinuationAssessmentStepError",
    "ContinuationAssessmentUnavailable",
    "ContinuationMaterializationPort",
    "DatabaseClock",
    "DurableContinuationAssessmentService",
    "PreparedContinuationAssessment",
    "SessionScopeFactory",
]
