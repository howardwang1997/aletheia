"""Transactional store and deterministic rebuild for the scientific program graph."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.epistemics.persistence import EpistemicResearchQuestionRecord
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import ScientificCommandRecord
from aletheia.memory.ledger import DataAsset, Experiment, Run
from aletheia.programs.persistence import (
    ResearchBudgetAllocationRecord,
    ResearchCampaignExperimentRecord,
    ResearchCampaignFamilyRecord,
    ResearchCampaignRunRecord,
    ResearchDataRoleAllocationRecord,
    ResearchGraphDependencyRecord,
    ResearchGraphNodeRecord,
    ResearchGraphTransitionRecord,
    ResearchProgramQuestionRecord,
    ResearchScientificFamilyRecord,
)
from aletheia.programs.schemas import (
    BudgetAllocationSnapshot,
    BudgetAllocationSpec,
    CampaignExperimentBindingSpec,
    CampaignFamilySnapshot,
    CampaignRunBindingSpec,
    CampaignSpec,
    DataRole,
    DataRoleAllocationSnapshot,
    DataRoleAllocationSpec,
    DependencySpec,
    ExternalBindingSnapshot,
    GraphCommandContext,
    GraphMutationReceipt,
    GraphNodeState,
    GraphNodeType,
    NodeTransitionSpec,
    ProgramQuestionBindingSpec,
    QuestGraphSnapshot,
    QuestSpec,
    ResearchDependencySnapshot,
    ResearchNodeSnapshot,
    ResearchProgramSpec,
    ResearchTransitionSnapshot,
    ScientificFamilySnapshot,
    ScientificFamilySpec,
)
from aletheia.programs.state import (
    ALLOWED_STATES,
    ALLOWED_TRANSITIONS,
    INITIAL_STATE,
    transition_allowed,
)
from aletheia.reproducibility.manifest import content_sha256


class ProgramGraphError(RuntimeError):
    """Base error for a scientific graph contract or persisted invariant."""


class ProgramGraphNotFound(ProgramGraphError):
    """A graph or external ledger object was not found."""


class ProgramGraphConflict(ProgramGraphError):
    """A stable graph identity was rebound or an allocation conflicts."""


class ProgramGraphCycleError(ProgramGraphError):
    """A scientific dependency would make the graph cyclic."""


class ProgramGraphTransitionError(ProgramGraphError):
    """A requested lifecycle transition is not currently legal."""


class ProgramGraphInvariantError(ProgramGraphError):
    """The persisted graph can no longer be deterministically reconstructed."""


_TSpec = TypeVar("_TSpec", QuestSpec, ResearchProgramSpec, CampaignSpec)


def _record_id(prefix: str, projection: dict[str, Any]) -> str:
    return f"{prefix}_{content_sha256(projection)[:32]}"


def _command_key(context: GraphCommandContext, operation: str) -> str:
    return _record_id(
        "rg",
        {
            "schema": "aletheia.research_graph_command_key.v1",
            "operation": operation,
            "client_idempotency_key": context.idempotency_key,
        },
    )


def _source_key(context: GraphCommandContext) -> str | None:
    if context.source_event_key is None:
        return None
    return _record_id(
        "rgs",
        {
            "schema": "aletheia.research_graph_source_key.v1",
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
        command_type=ScientificCommandType.RESEARCH_GRAPH_MUTATION.value,
        aggregate_type="research_graph",
        aggregate_id=object_id,
        idempotency_key=_command_key(context, operation),
        source_event_key=_source_key(context),
        input={"operation": operation, **payload},
        principal=context.principal,
        event_type=event_type,
    )


def _node_or_error(session: Session, node_id: str) -> ResearchGraphNodeRecord:
    node = session.get(ResearchGraphNodeRecord, node_id)
    if node is None:
        raise ProgramGraphNotFound(f"research graph node not found: {node_id}")
    return node


def _lock_quest(session: Session, quest_id: str) -> ResearchGraphNodeRecord:
    quest = session.scalar(
        select(ResearchGraphNodeRecord)
        .where(ResearchGraphNodeRecord.node_id == quest_id)
        .with_for_update()
    )
    if quest is None or quest.node_type != GraphNodeType.QUEST.value or quest.quest_id != quest_id:
        raise ProgramGraphNotFound(f"quest not found: {quest_id}")
    return quest


def _transition_id(command_id: str) -> str:
    return _record_id(
        "rgt",
        {"schema": "aletheia.research_graph_transition_identity.v1", "command_id": command_id},
    )


def _data_asset_scope(asset: DataAsset) -> dict[str, Any]:
    """Freeze the authority-relevant part of a mutable legacy DataAsset."""

    projection = {
        "schema": "aletheia.allocated_data_asset_scope.v1",
        "data_asset_id": asset.id,
        "run_id": asset.run_id,
        "source_role": asset.role,
        "source": asset.source,
        "ref": asset.ref,
        "content_sha256": asset.content_sha256,
    }
    return {key: value for key, value in projection.items() if value is not None}


def _acyclic(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for node_id, dependency_id in edges:
        adjacency[node_id].add(dependency_id)
    state: dict[str, int] = {}

    def visit(node_id: str) -> bool:
        observed = state.get(node_id, 0)
        if observed == 1:
            return False
        if observed == 2:
            return True
        state[node_id] = 1
        if any(not visit(dependency_id) for dependency_id in adjacency[node_id]):
            return False
        state[node_id] = 2
        return True

    return all(visit(node_id) for node_id in nodes)


def _scientifically_completed(session: Session, node: ResearchGraphNodeRecord) -> bool:
    if node.current_state == GraphNodeState.COMPLETED.value:
        return True
    if node.current_state != GraphNodeState.ARCHIVED.value:
        return False
    last = session.scalar(
        select(ResearchGraphTransitionRecord)
        .where(ResearchGraphTransitionRecord.node_id == node.node_id)
        .order_by(ResearchGraphTransitionRecord.to_version.desc())
        .limit(1)
    )
    return last is not None and last.from_state == GraphNodeState.COMPLETED.value


class ProgramGraphStore:
    """Own the scientific hierarchy; API/UI code only invokes this store and renders snapshots."""

    def __init__(self) -> None:
        self._commands = ScientificTransitionStore()

    @staticmethod
    def _receipt(object_id: str, command: ScientificCommandReceipt) -> GraphMutationReceipt:
        return GraphMutationReceipt(object_id=object_id, command=command)

    def _execute(
        self,
        command: ScientificCommandSpec,
        apply,
        *,
        object_id: str,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        try:
            receipt = self._commands.execute(command, apply, now=now)
        except IntegrityError as exc:
            raise ProgramGraphConflict("research graph identity already has different content") from exc
        return self._receipt(object_id, receipt)

    @staticmethod
    def _initial_node(
        session: Session,
        *,
        spec: _TSpec,
        node_id: str,
        quest_id: str,
        parent_node_id: str | None,
        node_type: GraphNodeType,
        command: ScientificCommandSpec,
        context: GraphCommandContext,
    ) -> str:
        existing = session.get(ResearchGraphNodeRecord, node_id)
        if existing is not None:
            raise ProgramGraphConflict(f"research node identity already exists: {node_id}")
        state = INITIAL_STATE[node_type]
        node = ResearchGraphNodeRecord(
            node_id=node_id,
            quest_id=quest_id,
            parent_node_id=parent_node_id,
            node_type=node_type.value,
            identity_key=spec.identity_key,
            spec_sha256=content_sha256(spec),
            spec_json=spec.model_dump(mode="json"),
            current_state=state.value,
            state_version=1,
            created_by=context.principal,
        )
        session.add(node)
        session.flush()
        assert command.command_id is not None
        transition_id = _transition_id(command.command_id)
        session.add(
            ResearchGraphTransitionRecord(
                transition_id=transition_id,
                node_id=node_id,
                command_id=command.command_id,
                from_state=None,
                to_state=state.value,
                from_version=0,
                to_version=1,
                reason=f"create {node_type.value}",
                principal=context.principal,
            )
        )
        return transition_id

    def create_quest(
        self,
        spec: QuestSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = QuestSpec.model_validate(spec.model_dump(mode="python"))
        command = _scientific_command(
            operation="create_quest",
            object_id=spec.node_id,
            payload={"spec": spec.model_dump(mode="json")},
            context=context,
            event_type="research_quest_created",
        )

        def apply(session: Session) -> ScientificMutation:
            transition_id = self._initial_node(
                session,
                spec=spec,
                node_id=spec.node_id,
                quest_id=spec.node_id,
                parent_node_id=None,
                node_type=GraphNodeType.QUEST,
                command=command,
                context=context,
            )
            return ScientificMutation(
                result={
                    "kind": "quest",
                    "object_id": spec.node_id,
                    "transition_id": transition_id,
                },
                event_projection={"quest_id": spec.node_id, "state": "draft"},
            )

        return self._execute(command, apply, object_id=spec.node_id, now=now)

    def create_program(
        self,
        spec: ResearchProgramSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = ResearchProgramSpec.model_validate(spec.model_dump(mode="python"))
        command = _scientific_command(
            operation="create_program",
            object_id=spec.node_id,
            payload={"spec": spec.model_dump(mode="json")},
            context=context,
            event_type="research_program_created",
        )

        def apply(session: Session) -> ScientificMutation:
            quest = _lock_quest(session, spec.quest_id)
            if quest.current_state in {"completed", "archived"}:
                raise ProgramGraphTransitionError("cannot add a program to a closed quest")
            transition_id = self._initial_node(
                session,
                spec=spec,
                node_id=spec.node_id,
                quest_id=spec.quest_id,
                parent_node_id=spec.quest_id,
                node_type=GraphNodeType.PROGRAM,
                command=command,
                context=context,
            )
            return ScientificMutation(
                result={
                    "kind": "program",
                    "object_id": spec.node_id,
                    "transition_id": transition_id,
                },
                event_projection={
                    "quest_id": spec.quest_id,
                    "program_id": spec.node_id,
                    "state": "proposed",
                },
            )

        return self._execute(command, apply, object_id=spec.node_id, now=now)

    def create_scientific_family(
        self,
        spec: ScientificFamilySpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = ScientificFamilySpec.model_validate(spec.model_dump(mode="python"))
        command = _scientific_command(
            operation="create_scientific_family",
            object_id=spec.family_id,
            payload={"spec": spec.model_dump(mode="json")},
            context=context,
            event_type="research_family_created",
        )

        def apply(session: Session) -> ScientificMutation:
            program = _node_or_error(session, spec.program_id)
            _lock_quest(session, program.quest_id)
            program = _node_or_error(session, spec.program_id)
            if program.node_type != GraphNodeType.PROGRAM.value:
                raise ProgramGraphConflict("scientific family parent must be a program")
            if program.current_state in {"stopped", "completed", "archived"}:
                raise ProgramGraphTransitionError("cannot add a family to a closed program")
            if session.get(ResearchScientificFamilyRecord, spec.family_id) is not None:
                raise ProgramGraphConflict(
                    f"scientific family identity already exists: {spec.family_id}"
                )
            assert command.command_id is not None
            session.add(
                ResearchScientificFamilyRecord(
                    family_id=spec.family_id,
                    quest_id=program.quest_id,
                    program_node_id=program.node_id,
                    family_key=spec.family_key,
                    semantic_sha256=spec.semantic_sha256,
                    spec_sha256=content_sha256(spec),
                    spec_json=spec.model_dump(mode="json"),
                    command_id=command.command_id,
                    created_by=context.principal,
                )
            )
            return ScientificMutation(
                result={"kind": "scientific_family", "object_id": spec.family_id},
                event_projection={
                    "quest_id": program.quest_id,
                    "program_id": program.node_id,
                    "family_id": spec.family_id,
                },
            )

        return self._execute(command, apply, object_id=spec.family_id, now=now)

    def create_campaign(
        self,
        spec: CampaignSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = CampaignSpec.model_validate(spec.model_dump(mode="python"))
        command = _scientific_command(
            operation="create_campaign",
            object_id=spec.node_id,
            payload={"spec": spec.model_dump(mode="json")},
            context=context,
            event_type="research_campaign_created",
        )

        def apply(session: Session) -> ScientificMutation:
            program = _node_or_error(session, spec.program_id)
            _lock_quest(session, program.quest_id)
            program = _node_or_error(session, spec.program_id)
            family = session.get(ResearchScientificFamilyRecord, spec.family_id)
            if program.node_type != GraphNodeType.PROGRAM.value:
                raise ProgramGraphConflict("campaign parent must be a program")
            if family is None or family.program_node_id != program.node_id:
                raise ProgramGraphConflict("campaign family must belong to its program")
            if program.current_state in {"stopped", "completed", "archived"}:
                raise ProgramGraphTransitionError("cannot add a campaign to a closed program")
            transition_id = self._initial_node(
                session,
                spec=spec,
                node_id=spec.node_id,
                quest_id=program.quest_id,
                parent_node_id=program.node_id,
                node_type=GraphNodeType.CAMPAIGN,
                command=command,
                context=context,
            )
            assert command.command_id is not None
            session.add(
                ResearchCampaignFamilyRecord(
                    campaign_node_id=spec.node_id,
                    family_id=spec.family_id,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={
                    "kind": "campaign",
                    "object_id": spec.node_id,
                    "transition_id": transition_id,
                    "family_id": spec.family_id,
                },
                event_projection={
                    "quest_id": program.quest_id,
                    "program_id": program.node_id,
                    "campaign_id": spec.node_id,
                    "family_id": spec.family_id,
                    "state": "planned",
                },
            )

        return self._execute(command, apply, object_id=spec.node_id, now=now)

    @staticmethod
    def _validate_transition_readiness(
        session: Session,
        node: ResearchGraphNodeRecord,
        target: GraphNodeState,
    ) -> None:
        node_type = GraphNodeType(node.node_type)
        source = GraphNodeState(node.current_state)
        if not transition_allowed(node_type, source, target):
            raise ProgramGraphTransitionError(
                f"invalid {node_type.value} transition: {source.value} -> {target.value}"
            )
        if target == GraphNodeState.ACTIVE and node.parent_node_id is not None:
            parent = _node_or_error(session, node.parent_node_id)
            if parent.current_state != GraphNodeState.ACTIVE.value:
                raise ProgramGraphTransitionError("parent must be active before activating child")
        if target == GraphNodeState.ACTIVE:
            dependencies = session.scalars(
                select(ResearchGraphDependencyRecord).where(
                    ResearchGraphDependencyRecord.node_id == node.node_id
                )
            ).all()
            for dependency in dependencies:
                prerequisite = _node_or_error(session, dependency.dependency_node_id)
                if not _scientifically_completed(session, prerequisite):
                    raise ProgramGraphTransitionError(
                        f"scientific prerequisite is not completed: {prerequisite.node_id}"
                    )
        children = session.scalars(
            select(ResearchGraphNodeRecord).where(
                ResearchGraphNodeRecord.parent_node_id == node.node_id
            )
        ).all()
        if target == GraphNodeState.COMPLETED:
            incomplete = [child.node_id for child in children if not _scientifically_completed(session, child)]
            if incomplete:
                raise ProgramGraphTransitionError(
                    "cannot complete a node with incomplete scientific children: "
                    + ", ".join(sorted(incomplete))
                )
        if target in {GraphNodeState.PAUSED, GraphNodeState.STOPPED}:
            active = [child.node_id for child in children if child.current_state == "active"]
            if active:
                raise ProgramGraphTransitionError(
                    "pause/stop active children first: " + ", ".join(sorted(active))
                )

    def transition_node(
        self,
        spec: NodeTransitionSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = NodeTransitionSpec.model_validate(spec.model_dump(mode="python"))
        command = _scientific_command(
            operation="transition_node",
            object_id=spec.node_id,
            payload={"transition": spec.model_dump(mode="json")},
            context=context,
            event_type="research_graph_transitioned",
        )

        def apply(session: Session) -> ScientificMutation:
            observed = _node_or_error(session, spec.node_id)
            _lock_quest(session, observed.quest_id)
            node = session.scalar(
                select(ResearchGraphNodeRecord)
                .where(ResearchGraphNodeRecord.node_id == spec.node_id)
                .with_for_update()
            )
            assert node is not None
            if node.state_version != spec.expected_version:
                raise ProgramGraphTransitionError(
                    f"stale node version: expected {spec.expected_version}, "
                    f"found {node.state_version}"
                )
            self._validate_transition_readiness(session, node, spec.to_state)
            assert command.command_id is not None
            transition_id = _transition_id(command.command_id)
            source_state = node.current_state
            source_version = node.state_version
            session.add(
                ResearchGraphTransitionRecord(
                    transition_id=transition_id,
                    node_id=node.node_id,
                    command_id=command.command_id,
                    from_state=source_state,
                    to_state=spec.to_state.value,
                    from_version=source_version,
                    to_version=source_version + 1,
                    reason=spec.reason,
                    principal=context.principal,
                )
            )
            node.current_state = spec.to_state.value
            node.state_version = source_version + 1
            # The projection timestamp is database-owned and transaction-stable.  ``now`` only
            # controls the command/event receipt in deterministic fault tests.
            node.updated_at = func.now()
            return ScientificMutation(
                result={
                    "kind": "node_transition",
                    "object_id": node.node_id,
                    "transition_id": transition_id,
                    "from_state": source_state,
                    "to_state": spec.to_state.value,
                    "state_version": source_version + 1,
                },
                event_projection={
                    "quest_id": node.quest_id,
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "from_state": source_state,
                    "to_state": spec.to_state.value,
                    "state_version": source_version + 1,
                },
            )

        return self._execute(command, apply, object_id=spec.node_id, now=now)

    def add_dependency(
        self,
        spec: DependencySpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = DependencySpec.model_validate(spec.model_dump(mode="python"))
        edge_id = _record_id(
            "rge",
            {
                "schema": "aletheia.research_dependency_identity.v1",
                "node_id": spec.node_id,
                "dependency_node_id": spec.dependency_node_id,
            },
        )
        command = _scientific_command(
            operation="add_dependency",
            object_id=edge_id,
            payload={"dependency": spec.model_dump(mode="json")},
            context=context,
            event_type="research_dependency_added",
        )

        def apply(session: Session) -> ScientificMutation:
            node = _node_or_error(session, spec.node_id)
            _lock_quest(session, node.quest_id)
            node = _node_or_error(session, spec.node_id)
            dependency = _node_or_error(session, spec.dependency_node_id)
            if node.quest_id != dependency.quest_id:
                raise ProgramGraphConflict("scientific dependencies cannot cross quests")
            if node.node_type != dependency.node_type or node.node_type == GraphNodeType.QUEST.value:
                raise ProgramGraphConflict(
                    "scientific dependencies must connect programs to programs or campaigns to campaigns"
                )
            if node.current_state in {"active", "completed", "stopped", "failed", "archived"}:
                raise ProgramGraphTransitionError(
                    "dependencies must be frozen before a node first becomes active"
                )
            if session.get(ResearchGraphDependencyRecord, edge_id) is not None:
                raise ProgramGraphConflict(f"scientific dependency already exists: {edge_id}")
            rows = session.scalars(
                select(ResearchGraphDependencyRecord).where(
                    ResearchGraphDependencyRecord.quest_id == node.quest_id
                )
            ).all()
            graph_nodes = session.scalars(
                select(ResearchGraphNodeRecord.node_id).where(
                    ResearchGraphNodeRecord.quest_id == node.quest_id
                )
            ).all()
            edges = [(row.node_id, row.dependency_node_id) for row in rows]
            if not _acyclic(graph_nodes, [*edges, (node.node_id, dependency.node_id)]):
                raise ProgramGraphCycleError("scientific dependency would create a cycle")
            assert command.command_id is not None
            session.add(
                ResearchGraphDependencyRecord(
                    edge_id=edge_id,
                    quest_id=node.quest_id,
                    node_id=node.node_id,
                    dependency_node_id=dependency.node_id,
                    rationale=spec.rationale,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={"kind": "dependency", "object_id": edge_id},
                event_projection={
                    "quest_id": node.quest_id,
                    "edge_id": edge_id,
                    "node_id": node.node_id,
                    "dependency_node_id": dependency.node_id,
                },
            )

        return self._execute(command, apply, object_id=edge_id, now=now)

    def bind_run(
        self,
        spec: CampaignRunBindingSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = CampaignRunBindingSpec.model_validate(spec.model_dump(mode="python"))
        binding_id = _record_id(
            "rgb",
            {
                "schema": "aletheia.campaign_run_binding_identity.v1",
                "campaign_id": spec.campaign_id,
                "run_id": spec.run_id,
            },
        )
        command = _scientific_command(
            operation="bind_run",
            object_id=binding_id,
            payload={"binding": spec.model_dump(mode="json")},
            context=context,
            event_type="research_run_bound",
        )

        def apply(session: Session) -> ScientificMutation:
            campaign = _node_or_error(session, spec.campaign_id)
            _lock_quest(session, campaign.quest_id)
            if campaign.node_type != GraphNodeType.CAMPAIGN.value:
                raise ProgramGraphConflict("run scope must be a campaign")
            if session.get(Run, spec.run_id) is None:
                raise ProgramGraphNotFound(f"run not found: {spec.run_id}")
            if session.get(ResearchCampaignRunRecord, binding_id) is not None:
                raise ProgramGraphConflict(f"campaign/run binding already exists: {binding_id}")
            assert command.command_id is not None
            session.add(
                ResearchCampaignRunRecord(
                    binding_id=binding_id,
                    quest_id=campaign.quest_id,
                    campaign_node_id=campaign.node_id,
                    run_id=spec.run_id,
                    role=spec.role,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={"kind": "campaign_run_binding", "object_id": binding_id},
                event_projection={
                    "quest_id": campaign.quest_id,
                    "campaign_id": campaign.node_id,
                    "run_id": spec.run_id,
                    "role": spec.role,
                },
            )

        return self._execute(command, apply, object_id=binding_id, now=now)

    def bind_experiment(
        self,
        spec: CampaignExperimentBindingSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = CampaignExperimentBindingSpec.model_validate(spec.model_dump(mode="python"))
        binding_id = _record_id(
            "rgb",
            {
                "schema": "aletheia.campaign_experiment_binding_identity.v1",
                "campaign_id": spec.campaign_id,
                "experiment_id": spec.experiment_id,
            },
        )
        command = _scientific_command(
            operation="bind_experiment",
            object_id=binding_id,
            payload={"binding": spec.model_dump(mode="json")},
            context=context,
            event_type="research_experiment_bound",
        )

        def apply(session: Session) -> ScientificMutation:
            campaign = _node_or_error(session, spec.campaign_id)
            _lock_quest(session, campaign.quest_id)
            experiment = session.get(Experiment, spec.experiment_id)
            if experiment is None:
                raise ProgramGraphNotFound(f"experiment not found: {spec.experiment_id}")
            run_binding = session.scalar(
                select(ResearchCampaignRunRecord).where(
                    ResearchCampaignRunRecord.campaign_node_id == campaign.node_id,
                    ResearchCampaignRunRecord.run_id == experiment.run_id,
                )
            )
            if run_binding is None:
                raise ProgramGraphConflict(
                    "bind the experiment's run to this campaign before binding the experiment"
                )
            assert command.command_id is not None
            session.add(
                ResearchCampaignExperimentRecord(
                    binding_id=binding_id,
                    quest_id=campaign.quest_id,
                    campaign_node_id=campaign.node_id,
                    experiment_id=experiment.id,
                    run_id=experiment.run_id,
                    role=spec.role,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={"kind": "campaign_experiment_binding", "object_id": binding_id},
                event_projection={
                    "quest_id": campaign.quest_id,
                    "campaign_id": campaign.node_id,
                    "experiment_id": experiment.id,
                    "run_id": experiment.run_id,
                    "role": spec.role,
                },
            )

        return self._execute(command, apply, object_id=binding_id, now=now)

    def bind_question(
        self,
        spec: ProgramQuestionBindingSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = ProgramQuestionBindingSpec.model_validate(spec.model_dump(mode="python"))
        binding_id = _record_id(
            "rgb",
            {
                "schema": "aletheia.program_question_binding_identity.v1",
                "program_id": spec.program_id,
                "question_sha256": spec.question_sha256,
            },
        )
        command = _scientific_command(
            operation="bind_question",
            object_id=binding_id,
            payload={"binding": spec.model_dump(mode="json")},
            context=context,
            event_type="research_question_bound",
        )

        def apply(session: Session) -> ScientificMutation:
            program = _node_or_error(session, spec.program_id)
            _lock_quest(session, program.quest_id)
            if program.node_type != GraphNodeType.PROGRAM.value:
                raise ProgramGraphConflict("question scope must be a program")
            question = session.get(EpistemicResearchQuestionRecord, spec.question_sha256)
            if question is None:
                raise ProgramGraphNotFound(
                    f"research question not found: {spec.question_sha256}"
                )
            campaign_ids = session.scalars(
                select(ResearchGraphNodeRecord.node_id).where(
                    ResearchGraphNodeRecord.parent_node_id == program.node_id,
                    ResearchGraphNodeRecord.node_type == GraphNodeType.CAMPAIGN.value,
                )
            ).all()
            run_binding = session.scalar(
                select(ResearchCampaignRunRecord).where(
                    ResearchCampaignRunRecord.campaign_node_id.in_(campaign_ids),
                    ResearchCampaignRunRecord.run_id == question.run_id,
                )
            )
            if run_binding is None:
                raise ProgramGraphConflict(
                    "question run must already belong to a campaign in this program"
                )
            assert command.command_id is not None
            session.add(
                ResearchProgramQuestionRecord(
                    binding_id=binding_id,
                    quest_id=program.quest_id,
                    program_node_id=program.node_id,
                    question_sha256=question.question_sha256,
                    role=spec.role,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={"kind": "program_question_binding", "object_id": binding_id},
                event_projection={
                    "quest_id": program.quest_id,
                    "program_id": program.node_id,
                    "question_sha256": question.question_sha256,
                    "role": spec.role,
                },
            )

        return self._execute(command, apply, object_id=binding_id, now=now)

    def allocate_data(
        self,
        spec: DataRoleAllocationSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = DataRoleAllocationSpec.model_validate(spec.model_dump(mode="python"))
        allocation_id = _record_id(
            "dga",
            {
                "schema": "aletheia.data_role_allocation_identity.v1",
                "scope_node_id": spec.scope_node_id,
                "data_asset_id": spec.data_asset_id,
                "role": spec.role.value,
            },
        )
        command = _scientific_command(
            operation="allocate_data",
            object_id=allocation_id,
            payload={"allocation": spec.model_dump(mode="json")},
            context=context,
            event_type="research_data_allocated",
        )

        def apply(session: Session) -> ScientificMutation:
            scope = _node_or_error(session, spec.scope_node_id)
            _lock_quest(session, scope.quest_id)
            if scope.node_type not in {GraphNodeType.QUEST.value, GraphNodeType.PROGRAM.value}:
                raise ProgramGraphConflict("data roles can only be allocated at quest/program scope")
            asset = session.get(DataAsset, spec.data_asset_id)
            if asset is None:
                raise ProgramGraphNotFound(f"data asset not found: {spec.data_asset_id}")
            if asset.status != "ready":
                raise ProgramGraphConflict("data asset must be ready before role allocation")
            run_binding = session.scalar(
                select(ResearchCampaignRunRecord).where(
                    ResearchCampaignRunRecord.quest_id == scope.quest_id,
                    ResearchCampaignRunRecord.run_id == asset.run_id,
                )
            )
            if run_binding is None:
                raise ProgramGraphConflict("data asset run is outside the allocation quest")
            if scope.node_type == GraphNodeType.PROGRAM.value:
                campaign = _node_or_error(session, run_binding.campaign_node_id)
                if campaign.parent_node_id != scope.node_id:
                    raise ProgramGraphConflict("data asset run is outside the allocation program")
            if asset.role == "external_validation" and spec.role not in {
                DataRole.EXTERNAL_VALIDATION,
                DataRole.REPLICATION,
            }:
                raise ProgramGraphConflict(
                    "sealed external-validation data cannot be allocated to an adaptive role"
                )
            if asset.role != "external_validation" and spec.role == DataRole.EXTERNAL_VALIDATION:
                raise ProgramGraphConflict(
                    "external-validation allocation requires an external-validation data asset"
                )
            existing = session.scalars(
                select(ResearchDataRoleAllocationRecord).where(
                    ResearchDataRoleAllocationRecord.data_asset_id == asset.id
                )
            ).all()
            if any(
                row.scope_node_id != scope.node_id
                and (row.exclusive or spec.exclusive)
                for row in existing
            ):
                raise ProgramGraphConflict("exclusive data asset is already allocated elsewhere")
            assert command.command_id is not None
            policy_sha256 = content_sha256(spec.policy)
            data_asset_scope = _data_asset_scope(asset)
            data_asset_scope_sha256 = content_sha256(data_asset_scope)
            session.add(
                ResearchDataRoleAllocationRecord(
                    allocation_id=allocation_id,
                    quest_id=scope.quest_id,
                    scope_node_id=scope.node_id,
                    data_asset_id=asset.id,
                    data_asset_run_id=asset.run_id,
                    source_role=asset.role,
                    data_asset_scope_sha256=data_asset_scope_sha256,
                    data_asset_scope_json=data_asset_scope,
                    role=spec.role.value,
                    exclusive=spec.exclusive,
                    policy_sha256=policy_sha256,
                    policy_json=spec.policy,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={
                    "kind": "data_allocation",
                    "object_id": allocation_id,
                    "data_asset_scope": data_asset_scope,
                    "data_asset_scope_sha256": data_asset_scope_sha256,
                },
                event_projection={
                    "quest_id": scope.quest_id,
                    "scope_node_id": scope.node_id,
                    "allocation_id": allocation_id,
                    "data_asset_id": asset.id,
                    "role": spec.role.value,
                    "data_asset_scope_sha256": data_asset_scope_sha256,
                    "policy_sha256": policy_sha256,
                },
            )

        return self._execute(command, apply, object_id=allocation_id, now=now)

    def allocate_budget(
        self,
        spec: BudgetAllocationSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> GraphMutationReceipt:
        spec = BudgetAllocationSpec.model_validate(spec.model_dump(mode="python"))
        allocation_id = _record_id(
            "bga",
            {
                "schema": "aletheia.budget_allocation_identity.v1",
                "scope_node_id": spec.scope_node_id,
                "kind": spec.kind.value,
            },
        )
        command = _scientific_command(
            operation="allocate_budget",
            object_id=allocation_id,
            payload={"allocation": spec.model_dump(mode="json")},
            context=context,
            event_type="research_budget_allocated",
        )

        def apply(session: Session) -> ScientificMutation:
            scope = _node_or_error(session, spec.scope_node_id)
            _lock_quest(session, scope.quest_id)
            if scope.node_type not in {GraphNodeType.QUEST.value, GraphNodeType.PROGRAM.value}:
                raise ProgramGraphConflict("budgets can only be allocated at quest/program scope")
            parent = None
            if scope.node_type == GraphNodeType.QUEST.value:
                if spec.parent_allocation_id is not None:
                    raise ProgramGraphConflict("quest budget cannot have a parent allocation")
            else:
                if spec.parent_allocation_id is None:
                    raise ProgramGraphConflict("program budget requires a quest parent allocation")
                parent = session.get(
                    ResearchBudgetAllocationRecord, spec.parent_allocation_id
                )
                if (
                    parent is None
                    or parent.quest_id != scope.quest_id
                    or parent.scope_node_id != scope.quest_id
                    or parent.kind != spec.kind.value
                ):
                    raise ProgramGraphConflict(
                        "program budget parent must be the same-kind quest allocation"
                    )
                siblings = session.scalars(
                    select(ResearchBudgetAllocationRecord).where(
                        ResearchBudgetAllocationRecord.parent_allocation_id
                        == parent.allocation_id
                    )
                ).all()
                allocated = sum(int(row.cap_microunits) for row in siblings)
                if allocated + spec.cap_microunits > int(parent.cap_microunits):
                    raise ProgramGraphConflict("program allocations exceed the quest budget cap")
            assert command.command_id is not None
            policy_sha256 = content_sha256(spec.policy)
            session.add(
                ResearchBudgetAllocationRecord(
                    allocation_id=allocation_id,
                    quest_id=scope.quest_id,
                    scope_node_id=scope.node_id,
                    parent_allocation_id=(parent.allocation_id if parent is not None else None),
                    kind=spec.kind.value,
                    cap_microunits=spec.cap_microunits,
                    policy_sha256=policy_sha256,
                    policy_json=spec.policy,
                    command_id=command.command_id,
                )
            )
            return ScientificMutation(
                result={"kind": "budget_allocation", "object_id": allocation_id},
                event_projection={
                    "quest_id": scope.quest_id,
                    "scope_node_id": scope.node_id,
                    "allocation_id": allocation_id,
                    "parent_allocation_id": spec.parent_allocation_id,
                    "budget_kind": spec.kind.value,
                    "cap_microunits": spec.cap_microunits,
                    "policy_sha256": policy_sha256,
                },
            )

        return self._execute(command, apply, object_id=allocation_id, now=now)

    @staticmethod
    def _verify_command(
        session: Session,
        command_id: str,
        object_id: str,
    ) -> ScientificCommandReceipt:
        row = session.get(ScientificCommandRecord, command_id)
        if row is None:
            raise ProgramGraphInvariantError(f"graph command is missing: {command_id}")
        try:
            ScientificTransitionStore._verify_event(session, row)
            receipt = ScientificTransitionStore._receipt(row, created=False)
        except Exception as exc:
            raise ProgramGraphInvariantError(
                f"graph command receipt is invalid: {command_id}"
            ) from exc
        if (
            row.command_type != ScientificCommandType.RESEARCH_GRAPH_MUTATION.value
            or row.aggregate_type != "research_graph"
            or row.aggregate_id != object_id
            or receipt.result.get("object_id") != object_id
        ):
            raise ProgramGraphInvariantError(
                f"graph command is rebound away from {object_id}: {command_id}"
            )
        return receipt

    @staticmethod
    def _validate_node_spec(node: ResearchGraphNodeRecord) -> None:
        try:
            if node.node_type == GraphNodeType.QUEST.value:
                spec = QuestSpec.model_validate(node.spec_json)
                expected_id = spec.node_id
                expected_parent = None
                expected_quest = expected_id
            elif node.node_type == GraphNodeType.PROGRAM.value:
                spec = ResearchProgramSpec.model_validate(node.spec_json)
                expected_id = spec.node_id
                expected_parent = spec.quest_id
                expected_quest = spec.quest_id
            elif node.node_type == GraphNodeType.CAMPAIGN.value:
                spec = CampaignSpec.model_validate(node.spec_json)
                expected_id = spec.node_id
                expected_parent = spec.program_id
                expected_quest = node.quest_id
            else:
                raise ValueError("unknown graph node type")
        except Exception as exc:
            raise ProgramGraphInvariantError(f"invalid node spec: {node.node_id}") from exc
        if (
            node.node_id != expected_id
            or node.parent_node_id != expected_parent
            or node.quest_id != expected_quest
            or node.identity_key != spec.identity_key
            or node.spec_sha256 != content_sha256(spec)
        ):
            raise ProgramGraphInvariantError(f"node identity/spec changed: {node.node_id}")

    def get_quest(self, quest_id: str) -> QuestGraphSnapshot:
        with self._session() as session:
            quest = session.get(ResearchGraphNodeRecord, quest_id)
            if quest is None or quest.node_type != GraphNodeType.QUEST.value:
                raise ProgramGraphNotFound(f"quest not found: {quest_id}")
            node_rows = session.scalars(
                select(ResearchGraphNodeRecord)
                .where(ResearchGraphNodeRecord.quest_id == quest_id)
                .order_by(ResearchGraphNodeRecord.node_type, ResearchGraphNodeRecord.node_id)
            ).all()
            transition_rows = session.scalars(
                select(ResearchGraphTransitionRecord)
                .join(
                    ResearchGraphNodeRecord,
                    ResearchGraphNodeRecord.node_id == ResearchGraphTransitionRecord.node_id,
                )
                .where(ResearchGraphNodeRecord.quest_id == quest_id)
                .order_by(
                    ResearchGraphTransitionRecord.node_id,
                    ResearchGraphTransitionRecord.to_version,
                )
            ).all()
            edge_rows = session.scalars(
                select(ResearchGraphDependencyRecord)
                .where(ResearchGraphDependencyRecord.quest_id == quest_id)
                .order_by(ResearchGraphDependencyRecord.edge_id)
            ).all()
            family_rows = session.scalars(
                select(ResearchScientificFamilyRecord)
                .where(ResearchScientificFamilyRecord.quest_id == quest_id)
                .order_by(ResearchScientificFamilyRecord.family_id)
            ).all()
            campaign_family_rows = session.scalars(
                select(ResearchCampaignFamilyRecord)
                .join(
                    ResearchGraphNodeRecord,
                    ResearchGraphNodeRecord.node_id
                    == ResearchCampaignFamilyRecord.campaign_node_id,
                )
                .where(ResearchGraphNodeRecord.quest_id == quest_id)
                .order_by(ResearchCampaignFamilyRecord.campaign_node_id)
            ).all()
            question_rows = session.scalars(
                select(ResearchProgramQuestionRecord)
                .where(ResearchProgramQuestionRecord.quest_id == quest_id)
                .order_by(ResearchProgramQuestionRecord.binding_id)
            ).all()
            run_rows = session.scalars(
                select(ResearchCampaignRunRecord)
                .where(ResearchCampaignRunRecord.quest_id == quest_id)
                .order_by(ResearchCampaignRunRecord.binding_id)
            ).all()
            experiment_rows = session.scalars(
                select(ResearchCampaignExperimentRecord)
                .where(ResearchCampaignExperimentRecord.quest_id == quest_id)
                .order_by(ResearchCampaignExperimentRecord.binding_id)
            ).all()
            data_rows = session.scalars(
                select(ResearchDataRoleAllocationRecord)
                .where(ResearchDataRoleAllocationRecord.quest_id == quest_id)
                .order_by(ResearchDataRoleAllocationRecord.allocation_id)
            ).all()
            budget_rows = session.scalars(
                select(ResearchBudgetAllocationRecord)
                .where(ResearchBudgetAllocationRecord.quest_id == quest_id)
                .order_by(ResearchBudgetAllocationRecord.allocation_id)
            ).all()

            node_by_id = {row.node_id: row for row in node_rows}
            transitions_by_node: dict[str, list[ResearchGraphTransitionRecord]] = defaultdict(list)
            for row in transition_rows:
                transitions_by_node[row.node_id].append(row)

            node_snapshots: list[ResearchNodeSnapshot] = []
            transition_snapshots: list[ResearchTransitionSnapshot] = []
            for node in node_rows:
                self._validate_node_spec(node)
                if node.node_type == GraphNodeType.PROGRAM.value:
                    parent = node_by_id.get(node.parent_node_id or "")
                    if parent is None or parent.node_type != GraphNodeType.QUEST.value:
                        raise ProgramGraphInvariantError(
                            f"program parent is not the quest: {node.node_id}"
                        )
                if node.node_type == GraphNodeType.CAMPAIGN.value:
                    parent = node_by_id.get(node.parent_node_id or "")
                    if parent is None or parent.node_type != GraphNodeType.PROGRAM.value:
                        raise ProgramGraphInvariantError(
                            f"campaign parent is not a program: {node.node_id}"
                        )
                transitions = transitions_by_node[node.node_id]
                if len(transitions) != node.state_version:
                    raise ProgramGraphInvariantError(
                        f"transition count/version mismatch: {node.node_id}"
                    )
                expected_state: GraphNodeState | None = None
                for index, transition in enumerate(transitions, start=1):
                    if (
                        transition.from_version != index - 1
                        or transition.to_version != index
                        or transition.from_state
                        != (expected_state.value if expected_state is not None else None)
                    ):
                        raise ProgramGraphInvariantError(
                            f"non-contiguous transition ledger: {node.node_id}"
                        )
                    target = GraphNodeState(transition.to_state)
                    node_type = GraphNodeType(node.node_type)
                    if index == 1:
                        if target != INITIAL_STATE[node_type]:
                            raise ProgramGraphInvariantError(
                                f"wrong initial state: {node.node_id}"
                            )
                    elif target not in ALLOWED_TRANSITIONS[node_type].get(
                        expected_state, set()
                    ):
                        raise ProgramGraphInvariantError(
                            f"invalid persisted transition: {node.node_id}"
                        )
                    self._verify_command(session, transition.command_id, node.node_id)
                    expected_state = target
                    transition_snapshots.append(
                        ResearchTransitionSnapshot(
                            transition_id=transition.transition_id,
                            node_id=transition.node_id,
                            command_id=transition.command_id,
                            from_state=transition.from_state,
                            to_state=transition.to_state,
                            from_version=transition.from_version,
                            to_version=transition.to_version,
                            reason=transition.reason,
                            principal=transition.principal,
                            created_at=transition.created_at,
                        )
                    )
                if expected_state is None or node.current_state != expected_state.value:
                    raise ProgramGraphInvariantError(
                        f"node projection differs from transition ledger: {node.node_id}"
                    )
                if expected_state not in ALLOWED_STATES[GraphNodeType(node.node_type)]:
                    raise ProgramGraphInvariantError(f"invalid node state: {node.node_id}")
                node_snapshots.append(
                    ResearchNodeSnapshot(
                        node_id=node.node_id,
                        quest_id=node.quest_id,
                        parent_node_id=node.parent_node_id,
                        node_type=node.node_type,
                        identity_key=node.identity_key,
                        spec_sha256=node.spec_sha256,
                        spec=node.spec_json,
                        state=node.current_state,
                        state_version=node.state_version,
                        created_by=node.created_by,
                        created_at=node.created_at,
                        updated_at=node.updated_at,
                    )
                )

            if not _acyclic(
                node_by_id,
                [(row.node_id, row.dependency_node_id) for row in edge_rows],
            ):
                raise ProgramGraphInvariantError("persisted scientific dependency graph is cyclic")
            dependency_snapshots: list[ResearchDependencySnapshot] = []
            for edge in edge_rows:
                left = node_by_id.get(edge.node_id)
                right = node_by_id.get(edge.dependency_node_id)
                if (
                    left is None
                    or right is None
                    or left.node_type != right.node_type
                    or left.node_type == GraphNodeType.QUEST.value
                ):
                    raise ProgramGraphInvariantError(f"invalid dependency endpoints: {edge.edge_id}")
                self._verify_command(session, edge.command_id, edge.edge_id)
                dependency_snapshots.append(
                    ResearchDependencySnapshot(
                        edge_id=edge.edge_id,
                        node_id=edge.node_id,
                        dependency_node_id=edge.dependency_node_id,
                        rationale=edge.rationale,
                        command_id=edge.command_id,
                        created_at=edge.created_at,
                    )
                )

            family_by_id: dict[str, ResearchScientificFamilyRecord] = {}
            family_snapshots: list[ScientificFamilySnapshot] = []
            for family in family_rows:
                try:
                    spec = ScientificFamilySpec.model_validate(family.spec_json)
                except Exception as exc:
                    raise ProgramGraphInvariantError(
                        f"invalid scientific family: {family.family_id}"
                    ) from exc
                program = node_by_id.get(family.program_node_id)
                if (
                    spec.family_id != family.family_id
                    or spec.program_id != family.program_node_id
                    or spec.family_key != family.family_key
                    or content_sha256(spec) != family.spec_sha256
                    or spec.semantic_sha256 != family.semantic_sha256
                    or program is None
                    or program.node_type != GraphNodeType.PROGRAM.value
                ):
                    raise ProgramGraphInvariantError(
                        f"scientific family identity changed: {family.family_id}"
                    )
                self._verify_command(session, family.command_id, family.family_id)
                family_by_id[family.family_id] = family
                family_snapshots.append(
                    ScientificFamilySnapshot(
                        family_id=family.family_id,
                        program_id=family.program_node_id,
                        family_key=family.family_key,
                        semantic_sha256=family.semantic_sha256,
                        spec=family.spec_json,
                        command_id=family.command_id,
                        created_at=family.created_at,
                    )
                )

            campaign_family_snapshots: list[CampaignFamilySnapshot] = []
            campaign_family_by_campaign = {
                row.campaign_node_id: row for row in campaign_family_rows
            }
            campaigns = [
                row for row in node_rows if row.node_type == GraphNodeType.CAMPAIGN.value
            ]
            if set(campaign_family_by_campaign) != {row.node_id for row in campaigns}:
                raise ProgramGraphInvariantError("every campaign must have exactly one family")
            for binding in campaign_family_rows:
                campaign = node_by_id[binding.campaign_node_id]
                family = family_by_id.get(binding.family_id)
                campaign_spec = CampaignSpec.model_validate(campaign.spec_json)
                if (
                    family is None
                    or family.program_node_id != campaign.parent_node_id
                    or campaign_spec.family_id != binding.family_id
                ):
                    raise ProgramGraphInvariantError(
                        f"campaign family is rebound: {campaign.node_id}"
                    )
                # Campaign creation owns both its initial transition and family binding.
                self._verify_command(session, binding.command_id, campaign.node_id)
                campaign_family_snapshots.append(
                    CampaignFamilySnapshot(
                        campaign_id=campaign.node_id,
                        family_id=binding.family_id,
                        command_id=binding.command_id,
                    )
                )

            external_snapshots: list[ExternalBindingSnapshot] = []
            for row in question_rows:
                self._verify_command(session, row.command_id, row.binding_id)
                program = node_by_id.get(row.program_node_id)
                question = session.get(EpistemicResearchQuestionRecord, row.question_sha256)
                descendant_campaign_ids = {
                    item.node_id
                    for item in node_rows
                    if item.parent_node_id == row.program_node_id
                    and item.node_type == GraphNodeType.CAMPAIGN.value
                }
                question_run_is_bound = any(
                    binding.run_id == (question.run_id if question is not None else None)
                    and binding.campaign_node_id in descendant_campaign_ids
                    for binding in run_rows
                )
                if (
                    program is None
                    or program.node_type != GraphNodeType.PROGRAM.value
                    or question is None
                    or not question_run_is_bound
                ):
                    raise ProgramGraphInvariantError(f"invalid question binding: {row.binding_id}")
                external_snapshots.append(
                    ExternalBindingSnapshot(
                        binding_id=row.binding_id,
                        binding_type="research_question",
                        scope_node_id=row.program_node_id,
                        external_id=row.question_sha256,
                        role=row.role,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            for row in run_rows:
                self._verify_command(session, row.command_id, row.binding_id)
                campaign = node_by_id.get(row.campaign_node_id)
                run = session.get(Run, row.run_id)
                if (
                    campaign is None
                    or campaign.node_type != GraphNodeType.CAMPAIGN.value
                    or run is None
                ):
                    raise ProgramGraphInvariantError(f"invalid run binding: {row.binding_id}")
                external_snapshots.append(
                    ExternalBindingSnapshot(
                        binding_id=row.binding_id,
                        binding_type="run",
                        scope_node_id=row.campaign_node_id,
                        external_id=row.run_id,
                        role=row.role,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            run_binding_pairs = {(row.campaign_node_id, row.run_id) for row in run_rows}
            for row in experiment_rows:
                self._verify_command(session, row.command_id, row.binding_id)
                experiment = session.get(Experiment, row.experiment_id)
                if (
                    (row.campaign_node_id, row.run_id) not in run_binding_pairs
                    or experiment is None
                    or experiment.run_id != row.run_id
                ):
                    raise ProgramGraphInvariantError(
                        f"experiment has no matching campaign/run binding: {row.binding_id}"
                    )
                external_snapshots.append(
                    ExternalBindingSnapshot(
                        binding_id=row.binding_id,
                        binding_type="experiment",
                        scope_node_id=row.campaign_node_id,
                        external_id=row.experiment_id,
                        role=row.role,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            external_snapshots.sort(key=lambda item: item.binding_id)

            data_snapshots: list[DataRoleAllocationSnapshot] = []
            allocated_by_asset: dict[str, list[ResearchDataRoleAllocationRecord]] = defaultdict(list)
            for row in data_rows:
                scope = node_by_id.get(row.scope_node_id)
                if scope is None or scope.node_type not in {"quest", "program"}:
                    raise ProgramGraphInvariantError(
                        f"invalid data allocation scope: {row.allocation_id}"
                    )
                if row.policy_sha256 != content_sha256(row.policy_json):
                    raise ProgramGraphInvariantError(
                        f"data allocation policy changed: {row.allocation_id}"
                    )
                self._verify_command(session, row.command_id, row.allocation_id)
                asset = session.get(DataAsset, row.data_asset_id)
                if asset is None:
                    raise ProgramGraphInvariantError(
                        f"allocated data asset is missing: {row.allocation_id}"
                    )
                data_asset_scope = _data_asset_scope(asset)
                data_asset_scope_sha256 = content_sha256(data_asset_scope)
                if (
                    row.data_asset_run_id != asset.run_id
                    or row.source_role != asset.role
                    or row.data_asset_scope_json != data_asset_scope
                    or row.data_asset_scope_sha256 != data_asset_scope_sha256
                ):
                    raise ProgramGraphInvariantError(
                        f"allocated data asset scope changed: {row.allocation_id}"
                    )
                run_binding = next(
                    (binding for binding in run_rows if binding.run_id == asset.run_id),
                    None,
                )
                if run_binding is None or run_binding.quest_id != quest_id:
                    raise ProgramGraphInvariantError(
                        f"allocated data asset run left the quest: {row.allocation_id}"
                    )
                if scope.node_type == "program":
                    campaign = node_by_id.get(run_binding.campaign_node_id)
                    if campaign is None or campaign.parent_node_id != scope.node_id:
                        raise ProgramGraphInvariantError(
                            f"allocated data asset run left the program: {row.allocation_id}"
                        )
                if asset.role == "external_validation" and row.role not in {
                    DataRole.EXTERNAL_VALIDATION.value,
                    DataRole.REPLICATION.value,
                }:
                    raise ProgramGraphInvariantError(
                        f"external data has an adaptive role: {row.allocation_id}"
                    )
                if asset.role != "external_validation" and row.role == DataRole.EXTERNAL_VALIDATION.value:
                    raise ProgramGraphInvariantError(
                        f"adaptive data is relabeled external: {row.allocation_id}"
                    )
                allocated_by_asset[row.data_asset_id].append(row)
                data_snapshots.append(
                    DataRoleAllocationSnapshot(
                        allocation_id=row.allocation_id,
                        scope_node_id=row.scope_node_id,
                        data_asset_id=row.data_asset_id,
                        role=row.role,
                        exclusive=row.exclusive,
                        data_asset_scope_sha256=data_asset_scope_sha256,
                        policy_sha256=row.policy_sha256,
                        policy=row.policy_json,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            for asset_id, allocations in allocated_by_asset.items():
                scopes = {row.scope_node_id for row in allocations}
                if len(scopes) > 1 and any(row.exclusive for row in allocations):
                    raise ProgramGraphInvariantError(
                        f"exclusive data asset has multiple scopes: {asset_id}"
                    )

            budget_by_id = {row.allocation_id: row for row in budget_rows}
            budget_snapshots: list[BudgetAllocationSnapshot] = []
            child_totals: dict[str, int] = defaultdict(int)
            for row in budget_rows:
                scope = node_by_id.get(row.scope_node_id)
                if scope is None or scope.node_type not in {"quest", "program"}:
                    raise ProgramGraphInvariantError(
                        f"invalid budget allocation scope: {row.allocation_id}"
                    )
                if row.policy_sha256 != content_sha256(row.policy_json):
                    raise ProgramGraphInvariantError(
                        f"budget allocation policy changed: {row.allocation_id}"
                    )
                if scope.node_type == "quest" and row.parent_allocation_id is not None:
                    raise ProgramGraphInvariantError("quest allocation cannot have a parent")
                if scope.node_type == "program":
                    parent = budget_by_id.get(row.parent_allocation_id or "")
                    if (
                        parent is None
                        or parent.scope_node_id != quest_id
                        or parent.kind != row.kind
                    ):
                        raise ProgramGraphInvariantError(
                            f"invalid budget parent: {row.allocation_id}"
                        )
                    child_totals[parent.allocation_id] += int(row.cap_microunits)
                self._verify_command(session, row.command_id, row.allocation_id)
                budget_snapshots.append(
                    BudgetAllocationSnapshot(
                        allocation_id=row.allocation_id,
                        scope_node_id=row.scope_node_id,
                        parent_allocation_id=row.parent_allocation_id,
                        kind=row.kind,
                        cap_microunits=row.cap_microunits,
                        policy_sha256=row.policy_sha256,
                        policy=row.policy_json,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            for parent_id, total in child_totals.items():
                if total > int(budget_by_id[parent_id].cap_microunits):
                    raise ProgramGraphInvariantError(
                        f"child allocations exceed quest cap: {parent_id}"
                    )

            payload: dict[str, Any] = {
                "schema_version": 1,
                "quest_id": quest_id,
                "nodes": tuple(node_snapshots),
                "transitions": tuple(transition_snapshots),
                "dependencies": tuple(dependency_snapshots),
                "scientific_families": tuple(family_snapshots),
                "campaign_families": tuple(campaign_family_snapshots),
                "external_bindings": tuple(external_snapshots),
                "data_allocations": tuple(data_snapshots),
                "budget_allocations": tuple(budget_snapshots),
                "rebuilt_at": None,
            }
            projection = {
                key: (
                    [item.model_dump(mode="json") for item in value]
                    if isinstance(value, tuple)
                    else value
                )
                for key, value in payload.items()
                if key != "rebuilt_at"
            }
            payload["graph_sha256"] = content_sha256(projection)
            return QuestGraphSnapshot(**payload)

    @staticmethod
    def _session():
        from aletheia.db import session_scope

        return session_scope()

    def list_quests(self) -> tuple[QuestGraphSnapshot, ...]:
        with self._session() as session:
            quest_ids = tuple(
                session.scalars(
                    select(ResearchGraphNodeRecord.node_id)
                    .where(ResearchGraphNodeRecord.node_type == GraphNodeType.QUEST.value)
                    .order_by(ResearchGraphNodeRecord.created_at, ResearchGraphNodeRecord.node_id)
                ).all()
            )
        return tuple(self.get_quest(quest_id) for quest_id in quest_ids)


__all__ = [
    "ProgramGraphConflict",
    "ProgramGraphCycleError",
    "ProgramGraphError",
    "ProgramGraphInvariantError",
    "ProgramGraphNotFound",
    "ProgramGraphStore",
    "ProgramGraphTransitionError",
]
