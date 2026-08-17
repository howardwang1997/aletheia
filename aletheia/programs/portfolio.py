"""Transactional shadow portfolio ledger for autonomous research planning audits."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import ScientificCommandRecord
from aletheia.memory.ledger import BudgetEvent
from aletheia.programs.graph import ProgramGraphError, ProgramGraphStore
from aletheia.programs.memory import (
    ResearchMemoryError,
    ResearchMemoryInvariantError,
    ResearchMemoryStore,
)
from aletheia.programs.memory_schemas import TaskContextPayload
from aletheia.programs.persistence import (
    ResearchBudgetAllocationRecord,
    ResearchGraphNodeRecord,
    ResearchMemoryContextReceiptRecord,
    ResearchPortfolioCandidateRecord,
    ResearchPortfolioEpochRecord,
    ResearchPortfolioHumanPlanRecord,
    ResearchPortfolioScoreRecord,
    ResearchPortfolioSlateRecord,
)
from aletheia.programs.portfolio_harness import (
    PORTFOLIO_SELECTOR_CODE_SHA256,
    candidate_program_and_family,
    derive_shadow_epoch,
)
from aletheia.programs.portfolio_schemas import (
    HumanPortfolioPlanSpec,
    PortfolioBudgetAvailability,
    PortfolioCandidateScore,
    PortfolioEpochSnapshot,
    PortfolioMutationReceipt,
    PortfolioSelectionDecision,
    PortfolioShadowAudit,
    PortfolioShadowAuditPolicy,
    PortfolioShadowComparison,
    PortfolioSlateSnapshot,
    PortfolioSlateSpec,
    human_plan_id,
)
from aletheia.programs.schemas import (
    BudgetKind,
    GraphCommandContext,
    GraphNodeType,
    QuestGraphSnapshot,
)
from aletheia.reproducibility.manifest import content_sha256


class ResearchPortfolioError(RuntimeError):
    """Base error for shadow portfolio contracts or persisted invariants."""


class ResearchPortfolioNotFound(ResearchPortfolioError):
    """A requested portfolio slate, human plan, or epoch does not exist."""


class ResearchPortfolioConflict(ResearchPortfolioError):
    """An immutable identity or one-shot shadow workflow step conflicts."""


class ResearchPortfolioStale(ResearchPortfolioError):
    """The graph, budget, or scientific memory changed after a slate was frozen."""


class ResearchPortfolioInvariantError(ResearchPortfolioError):
    """The portfolio ledger cannot be exactly reconstructed and replayed."""


def _record_id(prefix: str, projection: dict[str, Any]) -> str:
    return f"{prefix}_{content_sha256(projection)[:32]}"


def _aware_now(now: datetime | None) -> datetime:
    if now is None:
        # Scientific commands and graph projections use PostgreSQL transaction time.
        # Use the same clock here so causal checks do not depend on host/DB clock skew.
        with session_scope() as session:
            value = session.scalar(select(func.clock_timestamp()))
        if value is None:  # pragma: no cover - PostgreSQL always returns a timestamp
            value = datetime.now(timezone.utc)
    else:
        value = now
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research portfolio timestamp must be timezone-aware")
    return value


def _command_key(context: GraphCommandContext, operation: str) -> str:
    return _record_id(
        "rp",
        {
            "schema": "aletheia.research_portfolio_command_key.v1",
            "operation": operation,
            "client_idempotency_key": context.idempotency_key,
        },
    )


def _source_key(context: GraphCommandContext) -> str | None:
    if context.source_event_key is None:
        return None
    return _record_id(
        "rps",
        {
            "schema": "aletheia.research_portfolio_source_key.v1",
            "source_event_key": context.source_event_key,
        },
    )


def _scientific_command(
    *,
    operation: str,
    object_id: str,
    payload: dict[str, Any],
    context: GraphCommandContext,
    event_type: str,
) -> ScientificCommandSpec:
    return ScientificCommandSpec(
        run_id=None,
        command_type=ScientificCommandType.RESEARCH_PORTFOLIO_MUTATION.value,
        aggregate_type="research_portfolio",
        aggregate_id=object_id,
        idempotency_key=_command_key(context, operation),
        source_event_key=_source_key(context),
        input={"operation": operation, **payload},
        principal=context.principal,
        event_type=event_type,
    )


def _budget_state_sha256(state: tuple[PortfolioBudgetAvailability, ...]) -> str:
    return content_sha256([item.model_dump(mode="json") for item in state])


def _epoch_id(slate_id: str, human_plan_identity: str) -> str:
    return _record_id(
        "pep",
        {
            "schema": "aletheia.research_portfolio_epoch_identity.v1",
            "slate_id": slate_id,
            "human_plan_id": human_plan_identity,
        },
    )


class ResearchPortfolioStore:
    """Freeze, compare, and replay portfolio proposals without granting action authority."""

    def __init__(self) -> None:
        self._commands = ScientificTransitionStore()
        self._graph = ProgramGraphStore()
        self._memory = ResearchMemoryStore()

    @staticmethod
    def _lock_quest(session: Session, quest_id: str) -> ResearchGraphNodeRecord:
        quest = session.scalar(
            select(ResearchGraphNodeRecord)
            .where(ResearchGraphNodeRecord.node_id == quest_id)
            .with_for_update()
        )
        if (
            quest is None
            or quest.node_type != GraphNodeType.QUEST.value
            or quest.quest_id != quest_id
        ):
            raise ResearchPortfolioNotFound(f"portfolio Quest not found: {quest_id}")
        return quest

    @staticmethod
    def _lock_slate(session: Session, slate_id: str) -> ResearchPortfolioSlateRecord:
        slate = session.scalar(
            select(ResearchPortfolioSlateRecord)
            .where(ResearchPortfolioSlateRecord.slate_id == slate_id)
            .with_for_update()
        )
        if slate is None:
            raise ResearchPortfolioNotFound(f"research portfolio slate not found: {slate_id}")
        return slate

    @staticmethod
    def _budget_state(
        session: Session,
        *,
        graph: QuestGraphSnapshot,
    ) -> tuple[PortfolioBudgetAvailability, ...]:
        program_ids = {
            item.node_id for item in graph.nodes if item.node_type is GraphNodeType.PROGRAM
        }
        if not program_ids:
            return ()
        allocations = session.scalars(
            select(ResearchBudgetAllocationRecord)
            .where(
                ResearchBudgetAllocationRecord.quest_id == graph.quest_id,
                ResearchBudgetAllocationRecord.scope_node_id.in_(program_ids),
            )
            .order_by(ResearchBudgetAllocationRecord.allocation_id)
            .with_for_update()
        ).all()
        allocation_ids = [item.allocation_id for item in allocations]
        spent: dict[str, int] = {item: 0 for item in allocation_ids}
        if allocation_ids:
            events = session.scalars(
                select(BudgetEvent)
                .where(BudgetEvent.research_budget_allocation_id.in_(allocation_ids))
                .order_by(BudgetEvent.id)
            ).all()
            for event in events:
                allocation_id = event.research_budget_allocation_id
                if allocation_id is None:  # pragma: no cover - filtered above
                    continue
                amount = int(round(float(event.amount) * 1_000_000))
                if amount < 0:
                    raise ResearchPortfolioInvariantError(
                        f"allocated budget contains a negative charge: {event.id}"
                    )
                spent[allocation_id] += amount
        state: list[PortfolioBudgetAvailability] = []
        for allocation in allocations:
            observed = spent[allocation.allocation_id]
            cap = int(allocation.cap_microunits)
            if observed > cap:
                raise ResearchPortfolioInvariantError(
                    f"portfolio budget spend exceeds its cap: {allocation.allocation_id}"
                )
            state.append(
                PortfolioBudgetAvailability(
                    allocation_id=allocation.allocation_id,
                    program_id=allocation.scope_node_id,
                    kind=BudgetKind(allocation.kind),
                    cap_microunits=cap,
                    spent_microunits=observed,
                    available_microunits=cap - observed,
                )
            )
        return tuple(state)

    @staticmethod
    def _verify_command(
        session: Session,
        *,
        command_id: str,
        object_id: str,
    ) -> ScientificCommandReceipt:
        row = session.get(ScientificCommandRecord, command_id)
        if row is None:
            raise ResearchPortfolioInvariantError(
                f"research portfolio command is missing: {command_id}"
            )
        try:
            ScientificTransitionStore._verify_event(session, row)
            receipt = ScientificTransitionStore._receipt(row, created=False)
        except Exception as exc:
            raise ResearchPortfolioInvariantError(
                f"research portfolio command receipt is invalid: {command_id}"
            ) from exc
        if (
            row.command_type != ScientificCommandType.RESEARCH_PORTFOLIO_MUTATION.value
            or row.aggregate_type != "research_portfolio"
            or row.aggregate_id != object_id
            or receipt.result.get("object_id") != object_id
        ):
            raise ResearchPortfolioInvariantError(
                f"research portfolio command is rebound away from {object_id}: {command_id}"
            )
        return receipt

    @staticmethod
    def _verify_context_for_audit(
        session: Session,
        context_receipt_id: str,
    ) -> tuple[TaskContextPayload, str, str]:
        row = session.get(ResearchMemoryContextReceiptRecord, context_receipt_id)
        if row is None:
            raise ResearchPortfolioInvariantError(
                f"portfolio memory context receipt is missing: {context_receipt_id}"
            )
        try:
            payload = TaskContextPayload.model_validate(row.payload_json)
            command = ResearchMemoryStore._verify_command(
                session,
                command_id=row.command_id,
                object_id=row.context_receipt_id,
                command_type=ScientificCommandType.RESEARCH_MEMORY_CONTEXT,
                aggregate_type="research_memory_context",
            )
        except (ResearchMemoryInvariantError, ValueError, TypeError) as exc:
            raise ResearchPortfolioInvariantError(
                f"portfolio memory context receipt is invalid: {context_receipt_id}"
            ) from exc
        if (
            payload.quest_id != row.quest_id
            or payload.scope_node_id != row.scope_node_id
            or payload.task_key != row.task_key
            or payload.compaction_id != row.compaction_id
            or payload.context_sha256 != row.context_sha256
            or len(payload.prompt_text) != row.prompt_chars
            or row.prompt_chars > row.max_chars
            or command.result.get("context_sha256") != payload.context_sha256
        ):
            raise ResearchPortfolioInvariantError(
                f"portfolio memory context receipt changed: {context_receipt_id}"
            )
        return payload, row.consumer_provider, row.consumer_model

    @staticmethod
    def _validate_context_binding(
        *,
        spec: PortfolioSlateSpec,
        payload: TaskContextPayload,
        consumer_provider: str,
        consumer_model: str,
    ) -> None:
        if (
            payload.quest_id != spec.policy.quest_id
            or payload.scope_node_id != spec.policy.quest_id
            or payload.task_key != spec.policy.memory_task_key
            or consumer_provider != spec.proposal.proposer_provider
            or consumer_model != spec.proposal.proposer_model
            or spec.proposal.memory_context_receipt_id == ""
        ):
            raise ResearchPortfolioConflict(
                "portfolio proposal is outside its Quest-scoped scientific memory context"
            )

    def _load_current_context(self, spec: PortfolioSlateSpec):
        try:
            receipt = self._memory.load_task_context(spec.proposal.memory_context_receipt_id)
        except ResearchMemoryError as exc:
            raise ResearchPortfolioStale(
                "portfolio scientific memory context is no longer current"
            ) from exc
        self._validate_context_binding(
            spec=spec,
            payload=receipt.context,
            consumer_provider=receipt.consumer_provider,
            consumer_model=receipt.consumer_model,
        )
        return receipt

    def _load_current_graph(self, quest_id: str) -> QuestGraphSnapshot:
        try:
            return self._graph.get_quest(quest_id)
        except ProgramGraphError as exc:
            raise ResearchPortfolioStale(
                f"portfolio Quest graph cannot be rebuilt: {quest_id}"
            ) from exc

    def _execute(
        self,
        command: ScientificCommandSpec,
        apply,
        *,
        object_id: str,
        now: datetime,
    ) -> PortfolioMutationReceipt:
        try:
            receipt = self._commands.execute(command, apply, now=now)
        except IntegrityError as exc:
            raise ResearchPortfolioConflict(
                "research portfolio identity already has different content"
            ) from exc
        return PortfolioMutationReceipt(object_id=object_id, command=receipt)

    def _replay(
        self,
        *,
        command_id: str,
        object_id: str,
        operation: str,
        context: GraphCommandContext,
    ) -> PortfolioMutationReceipt:
        with session_scope() as session:
            row = session.get(ScientificCommandRecord, command_id)
            if row is None:
                raise ResearchPortfolioInvariantError(
                    f"portfolio replay command is missing: {command_id}"
                )
            if (
                row.idempotency_key != _command_key(context, operation)
                or row.source_event_key != _source_key(context)
                or row.principal != context.principal
            ):
                raise ResearchPortfolioConflict(
                    f"portfolio {operation} already committed under another command"
                )
            receipt = self._verify_command(
                session,
                command_id=command_id,
                object_id=object_id,
            )
        return PortfolioMutationReceipt(object_id=object_id, command=receipt)

    def register_slate(
        self,
        spec: PortfolioSlateSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> PortfolioMutationReceipt:
        spec = PortfolioSlateSpec.model_validate(spec.model_dump(mode="python"))
        object_id = spec.slate_id
        with session_scope() as session:
            existing = session.get(ResearchPortfolioSlateRecord, object_id)
            if existing is not None:
                if PortfolioSlateSpec.model_validate(existing.spec_json) != spec:
                    raise ResearchPortfolioConflict("portfolio slate identity was rebound")
                command_id = existing.command_id
            else:
                command_id = None
        if command_id is not None:
            return self._replay(
                command_id=command_id,
                object_id=object_id,
                operation="register_slate",
                context=context,
            )

        observed_at = _aware_now(now)
        if observed_at < spec.assessment_batch.completed_at:
            raise ResearchPortfolioConflict("portfolio slate predates its completed assessment")
        if spec.policy.selector_code_sha256 != PORTFOLIO_SELECTOR_CODE_SHA256:
            raise ResearchPortfolioConflict("portfolio slate pins another selector implementation")
        graph = self._load_current_graph(spec.policy.quest_id)
        if graph.graph_sha256 != spec.proposal.graph_sha256:
            raise ResearchPortfolioStale("portfolio proposal graph is no longer current")
        graph_times = [item.updated_at for item in graph.nodes]
        graph_times.extend(item.created_at for item in graph.transitions)
        graph_times.extend(item.created_at for item in graph.dependencies)
        graph_times.extend(item.created_at for item in graph.scientific_families)
        graph_times.extend(item.created_at for item in graph.external_bindings)
        graph_times.extend(item.created_at for item in graph.data_allocations)
        graph_times.extend(item.created_at for item in graph.budget_allocations)
        if graph_times and spec.proposal.generated_at < max(graph_times):
            raise ResearchPortfolioConflict(
                "portfolio proposal predates a committed graph dependency"
            )
        memory_context = self._load_current_context(spec)
        if spec.proposal.generated_at < memory_context.command.committed_at:
            raise ResearchPortfolioConflict(
                "portfolio proposal predates its scientific memory receipt"
            )
        candidate_bindings: list[dict[str, Any]] = []
        assessments = {item.candidate_id: item for item in spec.assessment_batch.assessments}
        for action in spec.proposal.candidates:
            program_id, family_id = candidate_program_and_family(graph, action)
            assessment = assessments[action.candidate_id]
            candidate_bindings.append(
                {
                    "candidate_id": action.candidate_id,
                    "action_sha256": action.action_sha256,
                    "assessment_sha256": assessment.assessment_sha256,
                    "program_id": program_id,
                    "family_id": family_id,
                }
            )
        with session_scope() as session:
            budget_state = self._budget_state(session, graph=graph)
        budget_sha256 = _budget_state_sha256(budget_state)
        command = _scientific_command(
            operation="register_slate",
            object_id=object_id,
            payload={
                "slate_id": object_id,
                "spec_sha256": spec.spec_sha256,
                "graph_sha256": graph.graph_sha256,
                "budget_state_sha256": budget_sha256,
                "candidate_bindings": candidate_bindings,
            },
            context=context,
            event_type="research_portfolio_slate_registered",
        )

        def apply(session: Session) -> ScientificMutation:
            self._lock_quest(session, spec.policy.quest_id)
            current_graph = self._load_current_graph(spec.policy.quest_id)
            if current_graph != graph:
                raise ResearchPortfolioStale(
                    "portfolio graph changed while its slate was being committed"
                )
            self._load_current_context(spec)
            current_budget = self._budget_state(session, graph=current_graph)
            if current_budget != budget_state:
                raise ResearchPortfolioStale(
                    "portfolio budget changed while its slate was being committed"
                )
            assert command.command_id is not None
            session.add(
                ResearchPortfolioSlateRecord(
                    slate_id=object_id,
                    quest_id=spec.policy.quest_id,
                    memory_context_receipt_id=spec.proposal.memory_context_receipt_id,
                    policy_sha256=spec.policy.policy_sha256,
                    proposal_sha256=spec.proposal.proposal_sha256,
                    assessment_batch_sha256=spec.assessment_batch.batch_sha256,
                    spec_sha256=spec.spec_sha256,
                    spec_json=spec.model_dump(mode="json"),
                    graph_sha256=graph.graph_sha256,
                    graph_snapshot_json=graph.model_dump(mode="json"),
                    budget_state_sha256=budget_sha256,
                    budget_state_json=[item.model_dump(mode="json") for item in budget_state],
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=observed_at,
                )
            )
            # Candidate guards resolve the applying command through the parent slate.  Flush the
            # parent explicitly rather than relying on mapper ordering without ORM relationships.
            session.flush()
            for action, binding in zip(spec.proposal.candidates, candidate_bindings, strict=True):
                assessment = assessments[action.candidate_id]
                session.add(
                    ResearchPortfolioCandidateRecord(
                        slate_id=object_id,
                        candidate_id=action.candidate_id,
                        program_id=binding["program_id"],
                        family_id=binding["family_id"],
                        action_type=action.action_type.value,
                        target_node_id=action.target_node_id,
                        action_sha256=action.action_sha256,
                        action_json=action.model_dump(mode="json"),
                        assessment_sha256=assessment.assessment_sha256,
                        assessment_json=assessment.model_dump(mode="json"),
                    )
                )
            return ScientificMutation(
                result={
                    "kind": "portfolio_slate",
                    "object_id": object_id,
                    "spec_sha256": spec.spec_sha256,
                },
                event_projection={
                    "quest_id": spec.policy.quest_id,
                    "slate_id": object_id,
                    "graph_sha256": graph.graph_sha256,
                    "candidate_count": len(candidate_bindings),
                    "shadow_only": True,
                    "actions_enqueued": False,
                },
            )

        return self._execute(command, apply, object_id=object_id, now=observed_at)

    def get_slate(self, slate_id: str) -> PortfolioSlateSnapshot:
        with session_scope() as session:
            row = session.get(ResearchPortfolioSlateRecord, slate_id)
            if row is None:
                raise ResearchPortfolioNotFound(f"research portfolio slate not found: {slate_id}")
            try:
                spec = PortfolioSlateSpec.model_validate(row.spec_json)
                graph = QuestGraphSnapshot.model_validate(row.graph_snapshot_json)
                budget_state = tuple(
                    PortfolioBudgetAvailability.model_validate(item)
                    for item in row.budget_state_json
                )
            except Exception as exc:
                raise ResearchPortfolioInvariantError(
                    f"portfolio slate payload is invalid: {slate_id}"
                ) from exc
            if (
                spec.slate_id != row.slate_id
                or spec.spec_sha256 != row.spec_sha256
                or spec.policy.policy_sha256 != row.policy_sha256
                or spec.proposal.proposal_sha256 != row.proposal_sha256
                or spec.assessment_batch.batch_sha256 != row.assessment_batch_sha256
                or spec.policy.quest_id != row.quest_id
                or spec.proposal.memory_context_receipt_id != row.memory_context_receipt_id
                or graph.quest_id != row.quest_id
                or graph.graph_sha256 != row.graph_sha256
                or _budget_state_sha256(budget_state) != row.budget_state_sha256
            ):
                raise ResearchPortfolioInvariantError(
                    f"portfolio slate bindings changed: {slate_id}"
                )
            self._verify_command(
                session,
                command_id=row.command_id,
                object_id=row.slate_id,
            )
            payload, provider, model = self._verify_context_for_audit(
                session, row.memory_context_receipt_id
            )
            self._validate_context_binding(
                spec=spec,
                payload=payload,
                consumer_provider=provider,
                consumer_model=model,
            )
            candidate_rows = session.scalars(
                select(ResearchPortfolioCandidateRecord)
                .where(ResearchPortfolioCandidateRecord.slate_id == slate_id)
                .order_by(ResearchPortfolioCandidateRecord.candidate_id)
            ).all()
            actions = {item.candidate_id: item for item in spec.proposal.candidates}
            assessments = {item.candidate_id: item for item in spec.assessment_batch.assessments}
            if {item.candidate_id for item in candidate_rows} != set(actions):
                raise ResearchPortfolioInvariantError(
                    f"portfolio slate candidates are incomplete: {slate_id}"
                )
            for candidate in candidate_rows:
                action = actions[candidate.candidate_id]
                assessment = assessments[candidate.candidate_id]
                program_id, family_id = candidate_program_and_family(graph, action)
                if (
                    candidate.program_id != program_id
                    or candidate.family_id != family_id
                    or candidate.action_type != action.action_type.value
                    or candidate.target_node_id != action.target_node_id
                    or candidate.action_sha256 != action.action_sha256
                    or candidate.action_json != action.model_dump(mode="json")
                    or candidate.assessment_sha256 != assessment.assessment_sha256
                    or candidate.assessment_json != assessment.model_dump(mode="json")
                ):
                    raise ResearchPortfolioInvariantError(
                        f"portfolio candidate changed: {candidate.candidate_id}"
                    )
            plan_row = session.scalar(
                select(ResearchPortfolioHumanPlanRecord).where(
                    ResearchPortfolioHumanPlanRecord.slate_id == slate_id
                )
            )
            if plan_row is None:
                plan_identity = None
                plan = None
            else:
                try:
                    plan = HumanPortfolioPlanSpec.model_validate(plan_row.plan_json)
                except Exception as exc:
                    raise ResearchPortfolioInvariantError(
                        f"portfolio human plan payload is invalid: {plan_row.human_plan_id}"
                    ) from exc
                if (
                    plan.plan_sha256 != plan_row.plan_sha256
                    or human_plan_id(slate_id, plan.plan_sha256) != plan_row.human_plan_id
                ):
                    raise ResearchPortfolioInvariantError(
                        f"portfolio human plan changed: {plan_row.human_plan_id}"
                    )
                self._verify_command(
                    session,
                    command_id=plan_row.command_id,
                    object_id=plan_row.human_plan_id,
                )
                plan_identity = plan_row.human_plan_id
            epoch_row = session.scalar(
                select(ResearchPortfolioEpochRecord).where(
                    ResearchPortfolioEpochRecord.slate_id == slate_id
                )
            )
            epoch_identity = epoch_row.epoch_id if epoch_row is not None else None
            snapshot_payload: dict[str, Any] = {
                "slate_id": row.slate_id,
                "spec": spec,
                "graph_snapshot": graph,
                "budget_state": budget_state,
                "human_plan_id": plan_identity,
                "human_plan": plan,
                "epoch_id": epoch_identity,
                "command_id": row.command_id,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
            snapshot_sha256 = content_sha256(
                PortfolioSlateSnapshot.model_construct(
                    **snapshot_payload,
                    snapshot_sha256="0" * 64,
                ).model_dump(mode="json", exclude={"snapshot_sha256"})
            )
            return PortfolioSlateSnapshot(
                **snapshot_payload,
                snapshot_sha256=snapshot_sha256,
            )

    def list_slates(self, quest_id: str) -> tuple[PortfolioSlateSnapshot, ...]:
        with session_scope() as session:
            quest = session.get(ResearchGraphNodeRecord, quest_id)
            if quest is None or quest.node_type != GraphNodeType.QUEST.value:
                raise ResearchPortfolioNotFound(f"portfolio Quest not found: {quest_id}")
            slate_ids = tuple(
                session.scalars(
                    select(ResearchPortfolioSlateRecord.slate_id)
                    .where(ResearchPortfolioSlateRecord.quest_id == quest_id)
                    .order_by(
                        ResearchPortfolioSlateRecord.created_at,
                        ResearchPortfolioSlateRecord.slate_id,
                    )
                ).all()
            )
        return tuple(self.get_slate(item) for item in slate_ids)

    def commit_human_plan(
        self,
        *,
        slate_id: str,
        plan: HumanPortfolioPlanSpec,
        context: GraphCommandContext,
        now: datetime | None = None,
    ) -> PortfolioMutationReceipt:
        plan = HumanPortfolioPlanSpec.model_validate(plan.model_dump(mode="python"))
        object_id = human_plan_id(slate_id, plan.plan_sha256)
        with session_scope() as session:
            existing = session.get(ResearchPortfolioHumanPlanRecord, object_id)
            if existing is not None:
                if (
                    existing.slate_id != slate_id
                    or HumanPortfolioPlanSpec.model_validate(existing.plan_json) != plan
                ):
                    raise ResearchPortfolioConflict("portfolio human plan identity was rebound")
                command_id = existing.command_id
            else:
                command_id = None
        if command_id is not None:
            return self._replay(
                command_id=command_id,
                object_id=object_id,
                operation="commit_human_plan",
                context=context,
            )

        slate = self.get_slate(slate_id)
        if slate.human_plan_id is not None:
            raise ResearchPortfolioConflict("portfolio slate already has a human plan")
        if slate.epoch_id is not None:
            raise ResearchPortfolioConflict("portfolio planner output was already materialized")
        if context.principal in {
            slate.spec.proposal.proposer_principal,
            slate.spec.assessment_batch.manifest.assessor_principal,
        }:
            raise ResearchPortfolioConflict(
                "portfolio human plan principal must be independent and blinded"
            )
        candidates = {item.candidate_id for item in slate.spec.proposal.candidates}
        unknown = set(plan.selected_candidate_ids) - candidates
        if unknown:
            raise ResearchPortfolioConflict(
                f"portfolio human plan selects unknown candidates: {sorted(unknown)}"
            )
        observed_at = _aware_now(now)
        if plan.issued_at < slate.created_at or observed_at < plan.issued_at:
            raise ResearchPortfolioConflict("portfolio human plan has an invalid commit time")
        command = _scientific_command(
            operation="commit_human_plan",
            object_id=object_id,
            payload={
                "slate_id": slate_id,
                "human_plan_id": object_id,
                "plan_sha256": plan.plan_sha256,
                "plan": plan.model_dump(mode="json"),
            },
            context=context,
            event_type="research_portfolio_human_plan_committed",
        )

        def apply(session: Session) -> ScientificMutation:
            self._lock_quest(session, slate.spec.policy.quest_id)
            self._lock_slate(session, slate_id)
            existing_plan = session.scalar(
                select(ResearchPortfolioHumanPlanRecord).where(
                    ResearchPortfolioHumanPlanRecord.slate_id == slate_id
                )
            )
            existing_epoch = session.scalar(
                select(ResearchPortfolioEpochRecord).where(
                    ResearchPortfolioEpochRecord.slate_id == slate_id
                )
            )
            if existing_plan is not None or existing_epoch is not None:
                raise ResearchPortfolioConflict(
                    "portfolio human plan lost its pre-evaluation one-shot race"
                )
            assert command.command_id is not None
            session.add(
                ResearchPortfolioHumanPlanRecord(
                    human_plan_id=object_id,
                    slate_id=slate_id,
                    plan_sha256=plan.plan_sha256,
                    plan_json=plan.model_dump(mode="json"),
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=observed_at,
                )
            )
            return ScientificMutation(
                result={
                    "kind": "portfolio_human_plan",
                    "object_id": object_id,
                    "plan_sha256": plan.plan_sha256,
                },
                event_projection={
                    "quest_id": slate.spec.policy.quest_id,
                    "slate_id": slate_id,
                    "human_plan_id": object_id,
                    "selected_count": len(plan.selected_candidate_ids),
                    "planner_output_access": "none",
                },
            )

        return self._execute(command, apply, object_id=object_id, now=observed_at)

    def evaluate_slate(
        self,
        *,
        slate_id: str,
        context: GraphCommandContext,
        now: datetime | None = None,
    ) -> PortfolioMutationReceipt:
        slate = self.get_slate(slate_id)
        if slate.human_plan_id is None or slate.human_plan is None:
            raise ResearchPortfolioConflict(
                "portfolio evaluation requires a blinded human plan first"
            )
        object_id = _epoch_id(slate_id, slate.human_plan_id)
        with session_scope() as session:
            existing = session.get(ResearchPortfolioEpochRecord, object_id)
            command_id = existing.command_id if existing is not None else None
        if command_id is not None:
            return self._replay(
                command_id=command_id,
                object_id=object_id,
                operation="evaluate_slate",
                context=context,
            )
        if slate.epoch_id is not None:
            raise ResearchPortfolioConflict("portfolio slate already has another epoch")
        if context.principal in {
            slate.spec.proposal.proposer_principal,
            slate.spec.assessment_batch.manifest.assessor_principal,
        }:
            raise ResearchPortfolioConflict(
                "portfolio evaluator principal must be independent from proposal and assessment"
            )
        observed_at = _aware_now(now)
        if observed_at < slate.human_plan.issued_at:
            raise ResearchPortfolioConflict("portfolio evaluation predates its human plan")
        derived = derive_shadow_epoch(
            spec=slate.spec,
            graph=slate.graph_snapshot,
            budget_state=slate.budget_state,
            human_plan=slate.human_plan,
            evaluated_at=observed_at,
        )
        ranking_by_id = {item.candidate_id: item for item in derived.decision.rankings}
        preliminary = _scientific_command(
            operation="evaluate_slate",
            object_id=object_id,
            payload={"slate_id": slate_id},
            context=context,
            event_type="research_portfolio_shadow_evaluated",
        )
        assert preliminary.command_id is not None
        epoch_payload: dict[str, Any] = {
            "epoch_id": object_id,
            "slate_id": slate_id,
            "human_plan_id": slate.human_plan_id,
            "scores": derived.scores,
            "decision": derived.decision,
            "comparison": derived.comparison,
            "evaluated_at": observed_at,
            "command_id": preliminary.command_id,
            "created_by": context.principal,
            "created_at": observed_at,
        }
        epoch_sha256 = content_sha256(
            PortfolioEpochSnapshot.model_construct(
                **epoch_payload,
                epoch_sha256="0" * 64,
            ).model_dump(mode="json", exclude={"epoch_sha256"})
        )
        PortfolioEpochSnapshot(**epoch_payload, epoch_sha256=epoch_sha256)
        score_bindings = [
            {
                "candidate_id": score.candidate_id,
                "score_sha256": score.score_sha256,
                "rank": ranking_by_id[score.candidate_id].rank,
                "selected": ranking_by_id[score.candidate_id].selected,
            }
            for score in derived.scores
        ]
        command = _scientific_command(
            operation="evaluate_slate",
            object_id=object_id,
            payload={
                "slate_id": slate_id,
                "epoch_id": object_id,
                "human_plan_id": slate.human_plan_id,
                "decision_sha256": derived.decision.decision_sha256,
                "comparison_sha256": derived.comparison.comparison_sha256,
                "epoch_sha256": epoch_sha256,
                "score_bindings": score_bindings,
            },
            context=context,
            event_type="research_portfolio_shadow_evaluated",
        )
        if command.command_id != preliminary.command_id:  # pragma: no cover - key defines it
            raise ResearchPortfolioInvariantError("portfolio epoch command identity is unstable")

        def apply(session: Session) -> ScientificMutation:
            self._lock_quest(session, slate.spec.policy.quest_id)
            self._lock_slate(session, slate_id)
            if (
                session.scalar(
                    select(ResearchPortfolioEpochRecord).where(
                        ResearchPortfolioEpochRecord.slate_id == slate_id
                    )
                )
                is not None
            ):
                raise ResearchPortfolioConflict("portfolio epoch lost its one-shot race")
            current_graph = self._load_current_graph(slate.spec.policy.quest_id)
            if current_graph != slate.graph_snapshot:
                raise ResearchPortfolioStale(
                    "portfolio graph changed after the proposal was frozen"
                )
            self._load_current_context(slate.spec)
            current_budget = self._budget_state(session, graph=current_graph)
            if current_budget != slate.budget_state:
                raise ResearchPortfolioStale(
                    "portfolio budget changed after the proposal was frozen"
                )
            plan_row = session.get(ResearchPortfolioHumanPlanRecord, slate.human_plan_id)
            if (
                plan_row is None
                or HumanPortfolioPlanSpec.model_validate(plan_row.plan_json) != slate.human_plan
            ):
                raise ResearchPortfolioInvariantError(
                    "portfolio human plan changed before evaluation"
                )
            replay = derive_shadow_epoch(
                spec=slate.spec,
                graph=slate.graph_snapshot,
                budget_state=slate.budget_state,
                human_plan=slate.human_plan,
                evaluated_at=observed_at,
            )
            if replay != derived:
                raise ResearchPortfolioInvariantError(
                    "portfolio harness is nondeterministic inside its commit boundary"
                )
            assert command.command_id is not None
            session.add(
                ResearchPortfolioEpochRecord(
                    epoch_id=object_id,
                    slate_id=slate_id,
                    quest_id=slate.spec.policy.quest_id,
                    human_plan_id=slate.human_plan_id,
                    score_count=len(derived.scores),
                    decision_sha256=derived.decision.decision_sha256,
                    decision_json=derived.decision.model_dump(mode="json"),
                    comparison_sha256=derived.comparison.comparison_sha256,
                    comparison_json=derived.comparison.model_dump(mode="json"),
                    epoch_sha256=epoch_sha256,
                    shadow_only=True,
                    actions_enqueued=False,
                    evaluated_at=observed_at,
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=observed_at,
                )
            )
            for score in derived.scores:
                ranking = ranking_by_id[score.candidate_id]
                session.add(
                    ResearchPortfolioScoreRecord(
                        epoch_id=object_id,
                        slate_id=slate_id,
                        candidate_id=score.candidate_id,
                        score_sha256=score.score_sha256,
                        score_json=score.model_dump(mode="json"),
                        feasible=score.feasible,
                        base_utility_microscore=score.base_utility_microscore,
                        selected=ranking.selected,
                        rank=ranking.rank,
                    )
                )
            return ScientificMutation(
                result={
                    "kind": "portfolio_shadow_epoch",
                    "object_id": object_id,
                    "epoch_sha256": epoch_sha256,
                },
                event_projection={
                    "quest_id": slate.spec.policy.quest_id,
                    "slate_id": slate_id,
                    "epoch_id": object_id,
                    "disposition": derived.decision.disposition.value,
                    "selected_count": len(derived.decision.selected_candidate_ids),
                    "jaccard_ppm": derived.comparison.jaccard_ppm,
                    "shadow_only": True,
                    "actions_enqueued": False,
                },
            )

        return self._execute(command, apply, object_id=object_id, now=observed_at)

    def get_epoch(self, epoch_id: str) -> PortfolioEpochSnapshot:
        with session_scope() as session:
            row = session.get(ResearchPortfolioEpochRecord, epoch_id)
            if row is None:
                raise ResearchPortfolioNotFound(f"research portfolio epoch not found: {epoch_id}")
            slate_id = row.slate_id
        slate = self.get_slate(slate_id)
        if slate.human_plan_id is None or slate.human_plan is None:
            raise ResearchPortfolioInvariantError(
                f"portfolio epoch lost its human plan: {epoch_id}"
            )
        with session_scope() as session:
            row = session.get(ResearchPortfolioEpochRecord, epoch_id)
            assert row is not None
            try:
                decision = PortfolioSelectionDecision.model_validate(row.decision_json)
                comparison = PortfolioShadowComparison.model_validate(row.comparison_json)
            except Exception as exc:
                raise ResearchPortfolioInvariantError(
                    f"portfolio epoch payload is invalid: {epoch_id}"
                ) from exc
            score_rows = session.scalars(
                select(ResearchPortfolioScoreRecord)
                .where(ResearchPortfolioScoreRecord.epoch_id == epoch_id)
                .order_by(ResearchPortfolioScoreRecord.candidate_id)
            ).all()
            try:
                scores = tuple(
                    PortfolioCandidateScore.model_validate(item.score_json) for item in score_rows
                )
            except Exception as exc:
                raise ResearchPortfolioInvariantError(
                    f"portfolio epoch scores are invalid: {epoch_id}"
                ) from exc
            ranking_by_id = {item.candidate_id: item for item in decision.rankings}
            if (
                row.slate_id != slate.slate_id
                or row.quest_id != slate.spec.policy.quest_id
                or row.human_plan_id != slate.human_plan_id
                or row.score_count != len(scores)
                or row.decision_sha256 != decision.decision_sha256
                or row.comparison_sha256 != comparison.comparison_sha256
                or not row.shadow_only
                or row.actions_enqueued
                or set(ranking_by_id) != {item.candidate_id for item in scores}
            ):
                raise ResearchPortfolioInvariantError(
                    f"portfolio epoch bindings changed: {epoch_id}"
                )
            for score_row, score in zip(score_rows, scores, strict=True):
                ranking = ranking_by_id[score.candidate_id]
                if (
                    score_row.candidate_id != score.candidate_id
                    or score_row.slate_id != slate.slate_id
                    or score_row.score_sha256 != score.score_sha256
                    or score_row.feasible != score.feasible
                    or score_row.base_utility_microscore != score.base_utility_microscore
                    or score_row.selected != ranking.selected
                    or score_row.rank != ranking.rank
                ):
                    raise ResearchPortfolioInvariantError(
                        f"portfolio score receipt changed: {score.candidate_id}"
                    )
            command = self._verify_command(
                session,
                command_id=row.command_id,
                object_id=row.epoch_id,
            )
            replay = derive_shadow_epoch(
                spec=slate.spec,
                graph=slate.graph_snapshot,
                budget_state=slate.budget_state,
                human_plan=slate.human_plan,
                evaluated_at=row.evaluated_at,
            )
            if (
                replay.scores != scores
                or replay.decision != decision
                or replay.comparison != comparison
            ):
                raise ResearchPortfolioInvariantError(
                    f"portfolio epoch no longer replays exactly: {epoch_id}"
                )
            epoch_payload: dict[str, Any] = {
                "epoch_id": row.epoch_id,
                "slate_id": row.slate_id,
                "human_plan_id": row.human_plan_id,
                "scores": scores,
                "decision": decision,
                "comparison": comparison,
                "evaluated_at": row.evaluated_at,
                "command_id": row.command_id,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
            expected_sha256 = content_sha256(
                PortfolioEpochSnapshot.model_construct(
                    **epoch_payload,
                    epoch_sha256="0" * 64,
                ).model_dump(mode="json", exclude={"epoch_sha256"})
            )
            if (
                expected_sha256 != row.epoch_sha256
                or command.result.get("epoch_sha256") != row.epoch_sha256
            ):
                raise ResearchPortfolioInvariantError(f"portfolio epoch hash changed: {epoch_id}")
            return PortfolioEpochSnapshot(
                **epoch_payload,
                epoch_sha256=row.epoch_sha256,
            )

    def shadow_audit(
        self,
        *,
        quest_id: str,
        policy: PortfolioShadowAuditPolicy | None = None,
    ) -> PortfolioShadowAudit:
        policy = policy or PortfolioShadowAuditPolicy()
        with session_scope() as session:
            quest = session.get(ResearchGraphNodeRecord, quest_id)
            if quest is None or quest.node_type != GraphNodeType.QUEST.value:
                raise ResearchPortfolioNotFound(f"portfolio Quest not found: {quest_id}")
            epoch_ids = tuple(
                session.scalars(
                    select(ResearchPortfolioEpochRecord.epoch_id)
                    .where(ResearchPortfolioEpochRecord.quest_id == quest_id)
                    .order_by(
                        ResearchPortfolioEpochRecord.evaluated_at,
                        ResearchPortfolioEpochRecord.epoch_id,
                    )
                ).all()
            )
        epochs = tuple(self.get_epoch(item) for item in epoch_ids)
        epoch_count = len(epochs)
        mean_jaccard = (
            sum(item.comparison.jaccard_ppm for item in epochs) // epoch_count if epoch_count else 0
        )
        hard_violations = sum(len(item.comparison.human_hard_filter_violations) for item in epochs)
        planner_empty = sum(not item.decision.selected_candidate_ids for item in epochs)
        blockers: list[str] = []
        if epoch_count < policy.minimum_epochs:
            blockers.append(f"epochs:minimum_not_met:{epoch_count}/{policy.minimum_epochs}")
        if mean_jaccard < policy.minimum_mean_jaccard_ppm:
            blockers.append(
                "agreement:mean_jaccard_below_floor:"
                f"{mean_jaccard}/{policy.minimum_mean_jaccard_ppm}"
            )
        if hard_violations > policy.maximum_human_hard_filter_violations:
            blockers.append(
                "human:hard_filter_violations_exceeded:"
                f"{hard_violations}/{policy.maximum_human_hard_filter_violations}"
            )
        if planner_empty > policy.maximum_planner_empty_epochs:
            blockers.append(
                "planner:empty_epochs_exceeded:"
                f"{planner_empty}/{policy.maximum_planner_empty_epochs}"
            )
        canonical = tuple(sorted(blockers))
        return PortfolioShadowAudit(
            quest_id=quest_id,
            policy=policy,
            epoch_count=epoch_count,
            mean_jaccard_ppm=mean_jaccard,
            human_hard_filter_violation_count=hard_violations,
            planner_empty_epoch_count=planner_empty,
            eligible_for_human_activation_review=not canonical,
            blockers=canonical,
        )


__all__ = [
    "ResearchPortfolioConflict",
    "ResearchPortfolioError",
    "ResearchPortfolioInvariantError",
    "ResearchPortfolioNotFound",
    "ResearchPortfolioStale",
    "ResearchPortfolioStore",
]
