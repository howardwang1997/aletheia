"""Exact, durable protocol compilation for an authorized Research Kernel action.

Protocol authoring remains replaceable and powerless.  This step re-audits the Kernel/CAS source,
bounds a provider request with deployment policy, runs the pure canonical compiler, and registers
the request/result in the append-only compilation registry under one database transaction.  It
does not authorize execution, reserve resources, admit observations, or mutate the Kernel.
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
from aletheia.observations.store import (
    ProtocolCompilationWrite,
    get_protocol_compilation_by_action,
    get_protocol_compilation_by_protocol_version,
    register_protocol_compilation,
)
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    compile_protocol,
    verify_compilation,
)
from aletheia.protocols.schemas import (
    ProtocolActionCategory,
    ProtocolCompilationResult,
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
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    EventType,
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


class ProtocolCompilationStepError(RuntimeError):
    """Compilation escaped its exact authorized graph or durable registry."""


class ProtocolCompilationUnavailable(ProtocolCompilationStepError):
    """No protocol can currently be authored under the frozen registry policy."""

    def __init__(self, blocker_codes: tuple[str, ...]) -> None:
        if not blocker_codes or blocker_codes != tuple(sorted(set(blocker_codes))):
            raise ValueError("protocol-authoring blockers must be nonempty and canonical")
        self.blocker_codes = blocker_codes
        super().__init__(",".join(blocker_codes))


class ActionProtocolCategoryPolicy(ControllerModel):
    """Deployment-reviewed mapping from a Kernel action kind to protocol objective categories."""

    schema_name: Literal["aletheia.action_protocol_category_policy"] = (
        "aletheia.action_protocol_category_policy"
    )
    schema_version: Literal[1] = 1
    action_kind: ActionKind
    allowed_categories: tuple[ProtocolActionCategory, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _categories_are_canonical(self) -> "ActionProtocolCategoryPolicy":
        expected = tuple(sorted(set(self.allowed_categories), key=lambda item: item.value))
        if self.allowed_categories != expected:
            raise ValueError("protocol action categories must be unique and canonical")
        return self


class ProtocolCompilationPolicyPin(ControllerModel):
    """Closed deployment policy over author, catalogs, compiler, and action compatibility."""

    schema_name: Literal["aletheia.protocol_compilation_policy_pin"] = (
        "aletheia.protocol_compilation_policy_pin"
    )
    schema_version: Literal[1] = 1
    capability_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_catalog_sha256: str = Field(pattern=_SHA256_PATTERN)
    compiler_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    allowed_protocol_author_principal_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    action_category_policies: tuple[ActionProtocolCategoryPolicy, ...] = Field(
        min_length=1, max_length=32
    )
    world_model_required_action_kinds: tuple[ActionKind, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def _policy_is_canonical(self) -> "ProtocolCompilationPolicyPin":
        if self.allowed_protocol_author_principal_ids != tuple(
            sorted(set(self.allowed_protocol_author_principal_ids))
        ) or any(
            re.fullmatch(_PRINCIPAL_PATTERN, principal) is None
            for principal in self.allowed_protocol_author_principal_ids
        ):
            raise ValueError("protocol authors must be nonempty, unique, and canonical")
        kinds = tuple(item.action_kind.value for item in self.action_category_policies)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("action-category policies must be unique and canonical")
        expected_world_model_kinds = tuple(
            sorted(set(self.world_model_required_action_kinds), key=lambda item: item.value)
        )
        if self.world_model_required_action_kinds != expected_world_model_kinds:
            raise ValueError("world-model-required action kinds must be unique and canonical")
        if not set(self.world_model_required_action_kinds).issubset(
            {item.action_kind for item in self.action_category_policies}
        ):
            raise ValueError("world-model requirements need an action-category policy")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)

    def allowed_categories_for(self, action_kind: ActionKind) -> tuple[ProtocolActionCategory, ...]:
        matches = tuple(
            item.allowed_categories
            for item in self.action_category_policies
            if item.action_kind is action_kind
        )
        if len(matches) != 1:
            raise ProtocolCompilationUnavailable(("protocol_compilation:action_kind_not_enabled",))
        return matches[0]


class AuthorizedProtocolCompilationContext(ControllerModel):
    """Exact Kernel/CAS source disclosed to a powerless protocol provider."""

    schema_name: Literal["aletheia.authorized_protocol_compilation_context"] = (
        "aletheia.authorized_protocol_compilation_context"
    )
    schema_version: Literal[1] = 1
    wakeup_sha256: str = Field(pattern=_SHA256_PATTERN)
    recovery_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    expected_stream_version: int = Field(ge=2)
    expected_tail_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    action: ResearchActionProposal
    action_proposed_event: ResearchEvent
    action_authorized_event: ResearchEvent
    graph_scope: ProtocolScope
    compilation_policy: ProtocolCompilationPolicyPin
    latest_event_committed_at: AwareDatetime
    direct_kernel_mutation_allowed: Literal[False] = False
    execution_authorization_allowed: Literal[False] = False
    signing_key_available: Literal[False] = False

    @model_validator(mode="after")
    def _context_is_exact(self) -> "AuthorizedProtocolCompilationContext":
        proposed = self.action_proposed_event
        authorized = self.action_authorized_event
        if (
            self.action.quest_id != self.quest_id
            or self.graph_scope.scope_binding.quest_id != self.quest_id
            or self.graph_scope.question_ref != self.action.question_ref
            or self.graph_scope.graph_snapshot_sha256 != self.expected_snapshot_sha256
            or proposed.quest_id != self.quest_id
            or proposed.event_type is not EventType.ACTION_PROPOSED
            or not isinstance(proposed.payload, ActionProposedPayload)
            or proposed.payload.action_ref != self.action.object_ref
            or proposed.payload.branch_id != self.graph_scope.branch_id
            or proposed.parent_event_sha256 != self.action.basis_tail_event_sha256
            or authorized.quest_id != self.quest_id
            or authorized.event_type is not EventType.ACTION_AUTHORIZED
            or not isinstance(authorized.payload, ActionAuthorizedPayload)
            or authorized.payload.action_id != self.action.action_id
            or authorized.payload.branch_id != self.graph_scope.branch_id
            or authorized.sequence != proposed.sequence + 1
            or authorized.parent_event_sha256 != proposed.event_sha256
            or self.expected_stream_version != authorized.sequence
            or self.expected_tail_event_sha256 != authorized.event_sha256
            or self.latest_event_committed_at != authorized.committed_at
            or not self.action.proposed_at <= proposed.committed_at <= authorized.committed_at
        ):
            raise ValueError("protocol compilation context escaped its authorized Kernel action")
        return self

    @property
    def context_sha256(self) -> str:
        return canonical_sha256(self)


class PreparedProtocolCompilation(ControllerModel):
    """Provider-authored protocol request bound to one disclosed compilation context."""

    schema_name: Literal["aletheia.prepared_protocol_compilation"] = (
        "aletheia.prepared_protocol_compilation"
    )
    schema_version: Literal[1] = 1
    context_sha256: str = Field(pattern=_SHA256_PATTERN)
    request: ProtocolCompilationRequest
    prepared_by_principal_id: str = Field(pattern=_PRINCIPAL_PATTERN)
    prepared_at: AwareDatetime
    execution_started: Literal[False] = False
    observation_accessed: Literal[False] = False

    @property
    def preparation_sha256(self) -> str:
        return canonical_sha256(self)


class ProtocolCompilationRequestProviderPort(Protocol):
    """Replaceable protocol author/registry; receives no DB or execution authority."""

    def prepare_protocol(
        self, context: AuthorizedProtocolCompilationContext
    ) -> PreparedProtocolCompilation: ...


class ProtocolCompilationPreparationVerificationPort(Protocol):
    """Deployment-pinned verifier for fresh and durably recovered protocol requests."""

    def verify_prepared_protocol(
        self,
        *,
        context: AuthorizedProtocolCompilationContext,
        prepared: PreparedProtocolCompilation,
    ) -> PreparedProtocolCompilation: ...


class ProtocolCompilationMaterializationPort(Protocol):
    authority_binding: ControllerStepAuthorityBinding

    def compile_and_register(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ProtocolCompilationWrite: ...


def _require_compile_tick(
    *,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
) -> None:
    if (
        plan_recovery_tick(projection) != plan
        or plan.step is not ControllerStep.COMPILE_PROTOCOL
        or wakeup.quest_id != projection.quest_id
        or projection.action_sha256 is None
        or not projection.action_authorized
        or projection.compilation_disposition is not CompilationDisposition.MISSING
    ):
        raise ProtocolCompilationStepError("protocol compilation received a stale controller tick")


def verify_prepared_protocol(
    *,
    context: AuthorizedProtocolCompilationContext,
    prepared: PreparedProtocolCompilation,
) -> PreparedProtocolCompilation:
    try:
        prepared = PreparedProtocolCompilation.model_validate(prepared.model_dump(mode="python"))
        request = prepared.request
        protocol = request.protocol
        policy = context.compilation_policy
        allowed_categories = policy.allowed_categories_for(context.action.kind)
        if (
            prepared.context_sha256 != context.context_sha256
            or prepared.prepared_by_principal_id not in policy.allowed_protocol_author_principal_ids
            or prepared.prepared_at < context.action_authorized_event.committed_at
            or protocol.graph_scope != context.graph_scope
            or protocol.authored_by_principal_id != prepared.prepared_by_principal_id
            or not context.action_authorized_event.committed_at
            <= protocol.authored_at
            <= prepared.prepared_at
            or protocol.objective.action_category not in allowed_categories
            or request.capability_catalog.catalog_sha256 != policy.capability_catalog_sha256
            or request.resource_catalog.catalog_sha256 != policy.resource_catalog_sha256
            or request.compiler_implementation_sha256 != policy.compiler_implementation_sha256
            or (
                context.action.kind in policy.world_model_required_action_kinds
                and protocol.world_model is None
            )
        ):
            raise ValueError("prepared protocol escaped its authorized context or policy")
        return prepared
    except ProtocolCompilationStepError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider-owned values fail closed
        raise ProtocolCompilationStepError("prepared protocol verification failed closed") from exc


DatabaseClock = Callable[[Session], datetime]
SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


def _database_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL invariant
        raise ProtocolCompilationStepError("PostgreSQL did not provide compilation registry time")
    return observed


class DurableProtocolCompilationService:
    """Two-audit, first-writer-wins compilation and append-only registration service."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        object_archive: ResearchObjectArchive,
        provider: ProtocolCompilationRequestProviderPort,
        preparation_verifier: ProtocolCompilationPreparationVerificationPort,
        compilation_policy: ProtocolCompilationPolicyPin,
        authority_binding: ControllerStepAuthorityBinding,
        sessions: SessionScopeFactory = session_scope,
        database_clock: DatabaseClock = _database_time,
    ) -> None:
        if (
            not callable(getattr(kernel_store, "audit_in_session", None))
            or not callable(getattr(object_archive, "load_object", None))
            or not callable(getattr(provider, "prepare_protocol", None))
            or not callable(getattr(preparation_verifier, "verify_prepared_protocol", None))
            or not callable(sessions)
            or not callable(database_clock)
        ):
            raise TypeError("protocol compilation service dependencies are invalid")
        policy = ProtocolCompilationPolicyPin.model_validate(
            compilation_policy.model_dump(mode="python")
        )
        binding = ControllerStepAuthorityBinding.model_validate(
            authority_binding.model_dump(mode="python")
        )
        if (
            binding.role is not ControllerStepAuthorityRole.PROTOCOL_COMPILATION
            or binding.key_id is not None
            or binding.policy_sha256 != policy.policy_sha256
        ):
            raise ValueError("protocol compiler differs from its pinned policy authority")
        self._kernel_store = kernel_store
        self._object_archive = object_archive
        self._provider = provider
        self._preparation_verifier = preparation_verifier
        self._policy = policy
        self._sessions = sessions
        self._database_clock = database_clock
        self.authority_binding = binding

    def compile_and_register(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ProtocolCompilationWrite:
        try:
            _require_compile_tick(wakeup=wakeup, projection=projection, plan=plan)
            with self._sessions() as session:
                context = self._context(
                    session=session,
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                )
                existing = get_protocol_compilation_by_action(
                    session,
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                )
                if existing is not None:
                    return self._verify_registered(session, context=context, write=existing)

            prepared = verify_prepared_protocol(
                context=context,
                prepared=self._provider.prepare_protocol(context),
            )
            verified_prepared = self._preparation_verifier.verify_prepared_protocol(
                context=context,
                prepared=prepared,
            )
            if verified_prepared != prepared:
                raise ProtocolCompilationStepError(
                    "protocol preparation verifier changed provider output"
                )
            result = compile_protocol(prepared.request)
            verify_compilation(prepared.request, result)

            with self._sessions() as session:
                locked_context = self._context(
                    session=session,
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                )
                if locked_context != context:
                    raise ProtocolCompilationStepError(
                        "authorized compilation context changed before registration"
                    )
                winner = get_protocol_compilation_by_action(
                    session,
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                )
                if winner is not None:
                    return self._verify_registered(session, context=context, write=winner)
                registered_at = self._database_clock(session)
                if registered_at < prepared.prepared_at:
                    raise ProtocolCompilationStepError(
                        "compilation registry time predates the prepared protocol"
                    )
                write = ProtocolCompilationWrite.from_contract(
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                    request=prepared.request,
                    result=result,
                    registered_at=registered_at,
                )
                self._verify_revision_parent(session, write)
                register_protocol_compilation(session, write)
                return self._verify_registered(session, context=context, write=write)
        except (ProtocolCompilationUnavailable, ProtocolCompilationStepError):
            raise
        except Exception as exc:  # noqa: BLE001 - DB/CAS/compiler/provider failures fail closed
            raise ProtocolCompilationStepError("protocol compilation failed closed") from exc

    def _context(
        self,
        *,
        session: Session,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> AuthorizedProtocolCompilationContext:
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
            raise ProtocolCompilationStepError(
                "Kernel audit differs from the compilation recovery projection"
            )
        actions = tuple(
            item
            for item in state.actions
            if item.action_ref.object_sha256 == projection.action_sha256
        )
        if len(actions) != 1 or actions[0].lifecycle is not ActionLifecycle.AUTHORIZED:
            raise ProtocolCompilationStepError("compilation source is not one authorized action")
        action_state = actions[0]
        archived = self._object_archive.load_object(action_state.action_ref)
        if not isinstance(archived.payload, ResearchActionProposal):
            raise ProtocolCompilationStepError("compilation source CAS object is not an action")
        action = archived.payload
        proposed = tuple(
            event
            for event in audit.events
            if event.event_sha256 == action_state.proposed_event_sha256
        )
        authorized = tuple(
            event
            for event in audit.events
            if event.event_sha256 == action_state.decided_event_sha256
        )
        if len(proposed) != 1 or len(authorized) != 1:
            raise ProtocolCompilationStepError(
                "action proposal/authorization events are not unique"
            )
        graph_scope = ProtocolScope(
            scope_binding=audit.scope_binding,
            scope_node_id=(
                audit.scope_binding.campaign_id
                or audit.scope_binding.program_id
                or audit.scope_binding.quest_id
            ),
            branch_id=action_state.branch_id,
            question_ref=action.question_ref,
            graph_snapshot_sha256=state.snapshot_sha256,
        )
        return AuthorizedProtocolCompilationContext(
            wakeup_sha256=wakeup.wakeup_sha256,
            recovery_projection_sha256=projection.projection_sha256,
            plan_sha256=plan.plan_sha256,
            quest_id=projection.quest_id,
            expected_stream_version=projection.audited_stream_version,
            expected_tail_event_sha256=projection.audited_tail_event_sha256,
            expected_snapshot_sha256=projection.audited_snapshot_sha256,
            action=action,
            action_proposed_event=proposed[0],
            action_authorized_event=authorized[0],
            graph_scope=graph_scope,
            compilation_policy=self._policy,
            latest_event_committed_at=audit.events[-1].committed_at,
        )

    def _verify_registered(
        self,
        session: Session,
        *,
        context: AuthorizedProtocolCompilationContext,
        write: ProtocolCompilationWrite,
    ) -> ProtocolCompilationWrite:
        try:
            request = ProtocolCompilationRequest.model_validate(write.request_json)
            result = ProtocolCompilationResult.model_validate(write.result_json)
            expected = ProtocolCompilationWrite.from_contract(
                quest_id=context.quest_id,
                action_sha256=context.action.object_sha256,
                request=request,
                result=result,
                registered_at=write.registered_at,
            )
            verify_compilation(request, result)
            synthetic = PreparedProtocolCompilation(
                context_sha256=context.context_sha256,
                request=request,
                prepared_by_principal_id=request.protocol.authored_by_principal_id,
                prepared_at=request.protocol.authored_at,
            )
            verify_prepared_protocol(context=context, prepared=synthetic)
            verified = self._preparation_verifier.verify_prepared_protocol(
                context=context,
                prepared=synthetic,
            )
            if verified != synthetic:
                raise ValueError("protocol preparation verifier changed durable request")
            self._verify_revision_parent(session, write)
            if write != expected or write.registered_at < request.protocol.authored_at:
                raise ValueError("registered compilation differs from canonical persistence")
            return write
        except ProtocolCompilationStepError:
            raise
        except Exception as exc:  # noqa: BLE001 - persisted compilation fails closed
            raise ProtocolCompilationStepError(
                "registered protocol compilation verification failed closed"
            ) from exc

    @staticmethod
    def _verify_revision_parent(session: Session, write: ProtocolCompilationWrite) -> None:
        if write.protocol_version == 1:
            return
        parent = get_protocol_compilation_by_protocol_version(
            session,
            quest_id=write.quest_id,
            protocol_id=write.protocol_id,
            protocol_version=write.protocol_version - 1,
        )
        if parent is None or parent.protocol_sha256 != write.revision_parent_sha256:
            raise ProtocolCompilationStepError(
                "protocol revision does not resolve its exact contiguous registry parent"
            )
        try:
            parent_request = ProtocolCompilationRequest.model_validate(parent.request_json)
            parent_result = ProtocolCompilationResult.model_validate(parent.result_json)
            expected_parent = ProtocolCompilationWrite.from_contract(
                quest_id=parent.quest_id,
                action_sha256=parent.action_sha256,
                request=parent_request,
                result=parent_result,
                registered_at=parent.registered_at,
            )
            verify_compilation(parent_request, parent_result)
        except Exception as exc:  # noqa: BLE001 - parent registry material fails closed
            raise ProtocolCompilationStepError(
                "protocol revision parent is not a canonical compilation"
            ) from exc
        if parent != expected_parent:
            raise ProtocolCompilationStepError("protocol revision parent row was rebound")


class ProtocolCompilationStepAdapter:
    """Controller adapter for one deterministic, durable protocol compilation."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        compilations: ProtocolCompilationMaterializationPort,
    ) -> None:
        frozen = ControllerStepAdapterManifest.model_validate(manifest.model_dump(mode="python"))
        binding = ControllerStepAuthorityBinding.model_validate(
            compilations.authority_binding.model_dump(mode="python")
        )
        if (
            frozen.step is not ControllerStep.COMPILE_PROTOCOL
            or frozen.authorities != (binding,)
            or binding.role is not ControllerStepAuthorityRole.PROTOCOL_COMPILATION
        ):
            raise ValueError("protocol compilation adapter differs from its step manifest")
        self.manifest = frozen
        self._compilations = compilations

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            _require_compile_tick(wakeup=wakeup, projection=projection, plan=plan)
            write = self._compilations.compile_and_register(
                wakeup=wakeup,
                projection=projection,
                plan=plan,
            )
            request = ProtocolCompilationRequest.model_validate(write.request_json)
            result = ProtocolCompilationResult.model_validate(write.result_json)
            expected = ProtocolCompilationWrite.from_contract(
                quest_id=projection.quest_id,
                action_sha256=projection.action_sha256,
                request=request,
                result=result,
                registered_at=write.registered_at,
            )
            verify_compilation(request, result)
            if (
                write != expected
                or request.protocol.graph_scope.scope_binding.quest_id != projection.quest_id
                or request.protocol.graph_scope.graph_snapshot_sha256
                != projection.audited_snapshot_sha256
            ):
                raise ProtocolCompilationStepError(
                    "compilation service returned a rebound durable result"
                )
        except ProtocolCompilationUnavailable as exc:
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.BLOCKED,
                result_artifact_sha256s=(),
                blocker_codes=exc.blocker_codes,
            )
        except Exception as exc:  # noqa: BLE001 - adapter/service result fails closed
            raise ControllerStepExecutionError("protocol compilation step failed closed") from exc
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.COMPLETED,
            result_artifact_sha256s=tuple(
                sorted(
                    {
                        write.compilation_sha256,
                        write.request_sha256,
                        write.result_sha256,
                        write.receipt_sha256,
                    }
                )
            ),
            blocker_codes=(),
        )


__all__ = [
    "ActionProtocolCategoryPolicy",
    "AuthorizedProtocolCompilationContext",
    "DatabaseClock",
    "DurableProtocolCompilationService",
    "PreparedProtocolCompilation",
    "ProtocolCompilationMaterializationPort",
    "ProtocolCompilationPolicyPin",
    "ProtocolCompilationPreparationVerificationPort",
    "ProtocolCompilationRequestProviderPort",
    "ProtocolCompilationStepAdapter",
    "ProtocolCompilationStepError",
    "ProtocolCompilationUnavailable",
    "SessionScopeFactory",
    "verify_prepared_protocol",
]
