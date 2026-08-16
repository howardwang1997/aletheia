from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError

from aletheia.data.registry import register_dataset, update_dataset
from aletheia.db import create_all, engine, session_scope
from aletheia.epistemics.persistence import store_world_model_snapshot
from aletheia.memory.service import (
    create_experiment,
    create_run,
    list_scientific_family_attempts,
    record_budget_event,
    register_hypothesis_attempt,
)
from aletheia.memory.ledger import BudgetEvent, HypothesisAttempt
from aletheia.programs import (
    BudgetAllocationSpec,
    BudgetKind,
    CampaignExperimentBindingSpec,
    CampaignRunBindingSpec,
    CampaignSpec,
    DataRole,
    DataRoleAllocationSpec,
    DependencySpec,
    GraphCommandContext,
    GraphNodeState,
    NodeTransitionSpec,
    ProgramGraphConflict,
    ProgramGraphCycleError,
    ProgramGraphInvariantError,
    ProgramGraphStore,
    ProgramGraphTransitionError,
    ProgramQuestionBindingSpec,
    QuestSpec,
    ResearchProgramSpec,
    ScientificFamilySpec,
)
from aletheia.programs.persistence import (
    ResearchGraphDependencyRecord,
    ResearchGraphNodeRecord,
    ResearchGraphTransitionRecord,
)
from aletheia.schema_migrations import schema_diffs
from epistemics.f9s1_fixtures import build_world_model


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _ctx(seed: str, label: str) -> GraphCommandContext:
    return GraphCommandContext(
        idempotency_key=f"f11s3:{seed}:{label}",
        principal="pytest:f11s3",
    )


def _quest(seed: str) -> QuestSpec:
    return QuestSpec(
        identity_key=f"quest-{seed}",
        title=f"Quest {seed}",
        direction="Discover a reproducible mechanism.",
        value_boundary="Prefer truthful negative results over headline metrics.",
        safety_boundary=("No unreviewed external action",),
        resource_boundary={"owner": "human", "currency": "USD"},
    )


def _program(quest: QuestSpec, seed: str, label: str = "main") -> ResearchProgramSpec:
    return ResearchProgramSpec(
        quest_id=quest.node_id,
        identity_key=f"program-{seed}-{label}",
        title=f"Program {label}",
        objective="Discriminate competing causal explanations.",
        problem_domain="materials",
        knowledge_boundary={"as_of": "2026-08-17", "corpus": label},
    )


def _family(program: ResearchProgramSpec, seed: str) -> ScientificFamilySpec:
    return ScientificFamilySpec(
        program_id=program.node_id,
        family_key=f"family-{seed[:24]}",
        title="Shared scientific family",
        scientific_scope="The same confirmatory mechanism claim across campaign restarts.",
        multiplicity_policy={"method": "alpha_spending", "alpha": 0.05},
    )


def _campaign(
    program: ResearchProgramSpec,
    family: ScientificFamilySpec,
    seed: str,
    label: str,
) -> CampaignSpec:
    return CampaignSpec(
        program_id=program.node_id,
        family_id=family.family_id,
        identity_key=f"campaign-{seed}-{label}",
        title=f"Campaign {label}",
        objective="Run a precommitted discrimination sequence.",
        stopping_boundary={"max_confirmations": 3},
    )


def _seed_hierarchy(seed: str):
    store = ProgramGraphStore()
    quest = _quest(seed)
    program = _program(quest, seed)
    family = _family(program, seed)
    campaign = _campaign(program, family, seed, "one")
    store.create_quest(quest, _ctx(seed, "quest"))
    store.create_program(program, _ctx(seed, "program"))
    store.create_scientific_family(family, _ctx(seed, "family"))
    store.create_campaign(campaign, _ctx(seed, "campaign"))
    return store, quest, program, family, campaign


def test_migration_matches_orm_and_has_database_guards() -> None:
    expected = {
        "research_graph_nodes",
        "research_graph_transitions",
        "research_scientific_families",
        "research_campaign_families",
        "research_graph_dependencies",
        "research_program_questions",
        "research_campaign_runs",
        "research_campaign_experiments",
        "research_data_role_allocations",
        "research_budget_allocations",
    }
    with engine().connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        assert schema_diffs(connection) == []
    # The catalog assertion stays separate so SQLAlchemy metadata comparison remains portable.
    with engine().connect() as connection:
        from sqlalchemy import text

        trigger_names = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND "
                    "(tgname LIKE 'trg_research_%' OR "
                    "tgname IN ('trg_hypothesis_attempt_family_binding', "
                    "'trg_allocated_budget_event'))"
                )
            ).scalars()
        )
    assert "trg_research_graph_node_transition" in trigger_names
    assert "trg_research_dependency_cycle" in trigger_names
    assert "trg_hypothesis_attempt_family_binding" in trigger_names
    assert "trg_allocated_budget_event" in trigger_names


def test_hierarchy_lifecycle_and_exact_rebuild_are_ledger_derived() -> None:
    seed = uuid.uuid4().hex
    store, quest, program, family, campaign = _seed_hierarchy(seed)

    first = store.get_quest(quest.node_id)
    second = store.get_quest(quest.node_id)
    assert first == second
    assert first.graph_sha256 == second.graph_sha256
    assert [node.node_type.value for node in first.nodes] == ["campaign", "program", "quest"]
    assert first.campaign_families[0].family_id == family.family_id

    store.transition_node(
        NodeTransitionSpec(
            node_id=quest.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="human direction approved",
        ),
        _ctx(seed, "quest-active"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=program.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="knowledge boundary frozen",
        ),
        _ctx(seed, "program-active"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=campaign.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="prediction and protocol ready",
        ),
        _ctx(seed, "campaign-active"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=campaign.node_id,
            expected_version=2,
            to_state=GraphNodeState.COMPLETED,
            reason="precommitted stopping rule reached",
        ),
        _ctx(seed, "campaign-complete"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=program.node_id,
            expected_version=2,
            to_state=GraphNodeState.COMPLETED,
            reason="all campaigns completed",
        ),
        _ctx(seed, "program-complete"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=quest.node_id,
            expected_version=2,
            to_state=GraphNodeState.COMPLETED,
            reason="all programs completed",
        ),
        _ctx(seed, "quest-complete"),
    )

    rebuilt = store.get_quest(quest.node_id)
    states = {node.node_id: node.state for node in rebuilt.nodes}
    assert states == {
        quest.node_id: GraphNodeState.COMPLETED,
        program.node_id: GraphNodeState.COMPLETED,
        campaign.node_id: GraphNodeState.COMPLETED,
    }
    assert len(rebuilt.transitions) == 9
    assert rebuilt.graph_sha256 != first.graph_sha256


def test_dependency_readiness_and_concurrent_opposite_edges_never_form_cycle() -> None:
    seed = uuid.uuid4().hex
    store = ProgramGraphStore()
    quest = _quest(seed)
    program_a = _program(quest, seed, "a")
    program_b = _program(quest, seed, "b")
    store.create_quest(quest, _ctx(seed, "quest"))
    store.create_program(program_a, _ctx(seed, "program-a"))
    store.create_program(program_b, _ctx(seed, "program-b"))
    store.transition_node(
        NodeTransitionSpec(
            node_id=quest.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="approved",
        ),
        _ctx(seed, "quest-active"),
    )

    barrier = Barrier(2)

    def add(left: ResearchProgramSpec, right: ResearchProgramSpec, label: str):
        barrier.wait()
        try:
            return store.add_dependency(
                DependencySpec(
                    node_id=left.node_id,
                    dependency_node_id=right.node_id,
                    rationale="scientific prerequisite",
                ),
                _ctx(seed, label),
            )
        except (ProgramGraphCycleError, ProgramGraphConflict) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: add(*args),
                (
                    (program_a, program_b, "a-depends-b"),
                    (program_b, program_a, "b-depends-a"),
                ),
            )
        )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, (ProgramGraphCycleError, ProgramGraphConflict)) for item in results) == 1
    with session_scope() as session:
        edges = session.scalars(
            select(ResearchGraphDependencyRecord).where(
                ResearchGraphDependencyRecord.quest_id == quest.node_id
            )
        ).all()
    assert len(edges) == 1

    dependent = program_a if edges[0].node_id == program_a.node_id else program_b
    prerequisite = program_b if dependent is program_a else program_a
    with pytest.raises(ProgramGraphTransitionError, match="prerequisite"):
        store.transition_node(
            NodeTransitionSpec(
                node_id=dependent.node_id,
                expected_version=1,
                to_state=GraphNodeState.ACTIVE,
                reason="too early",
            ),
            _ctx(seed, "dependent-too-early"),
        )
    store.transition_node(
        NodeTransitionSpec(
            node_id=prerequisite.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="ready",
        ),
        _ctx(seed, "prerequisite-active"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=prerequisite.node_id,
            expected_version=2,
            to_state=GraphNodeState.COMPLETED,
            reason="done",
        ),
        _ctx(seed, "prerequisite-complete"),
    )
    store.transition_node(
        NodeTransitionSpec(
            node_id=dependent.node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="prerequisite completed",
        ),
        _ctx(seed, "dependent-active"),
    )


def test_same_idempotency_cannot_rebind_node_content_and_database_rows_are_immutable() -> None:
    seed = uuid.uuid4().hex
    store = ProgramGraphStore()
    quest = _quest(seed)
    context = _ctx(seed, "quest")
    first = store.create_quest(quest, context)
    replay = store.create_quest(quest, context)
    assert first.command.created is True
    assert replay.command.created is False
    changed = quest.model_copy(update={"title": "Changed title"})
    from aletheia.jobs import ScientificIdempotencyConflict

    with pytest.raises(ScientificIdempotencyConflict):
        store.create_quest(changed, context)

    with pytest.raises(DBAPIError, match="immutable|cannot be deleted"):
        with session_scope() as session:
            session.execute(
                update(ResearchGraphNodeRecord)
                .where(ResearchGraphNodeRecord.node_id == quest.node_id)
                .values(spec_json={"forged": True})
            )
    with pytest.raises(DBAPIError, match="append-only"):
        with session_scope() as session:
            transition = session.scalar(
                select(ResearchGraphTransitionRecord).where(
                    ResearchGraphTransitionRecord.node_id == quest.node_id
                )
            )
            assert transition is not None
            session.execute(
                update(ResearchGraphTransitionRecord)
                .where(
                    ResearchGraphTransitionRecord.transition_id
                    == transition.transition_id
                )
                .values(reason="forged")
            )
    assert store.get_quest(quest.node_id).nodes[0].spec["title"] == quest.title


def test_family_identity_spans_campaigns_runs_and_attempt_counts() -> None:
    seed = uuid.uuid4().hex
    store, quest, program, family, campaign_one = _seed_hierarchy(seed)
    campaign_two = _campaign(program, family, seed, "two")
    store.create_campaign(campaign_two, _ctx(seed, "campaign-two"))
    run_one = create_run("family attempt one", domain="materials")
    run_two = create_run("family attempt two", domain="materials")
    store.bind_run(
        CampaignRunBindingSpec(campaign_id=campaign_one.node_id, run_id=run_one),
        _ctx(seed, "run-one"),
    )
    store.bind_run(
        CampaignRunBindingSpec(campaign_id=campaign_two.node_id, run_id=run_two),
        _ctx(seed, "run-two"),
    )
    exp_one = create_experiment(run_one, {"round": 1, "hypothesis": "H1"})
    exp_two = create_experiment(run_two, {"round": 1, "hypothesis": "H2"})
    store.bind_experiment(
        CampaignExperimentBindingSpec(
            campaign_id=campaign_one.node_id,
            experiment_id=exp_one,
        ),
        _ctx(seed, "experiment-one"),
    )
    store.bind_experiment(
        CampaignExperimentBindingSpec(
            campaign_id=campaign_two.node_id,
            experiment_id=exp_two,
        ),
        _ctx(seed, "experiment-two"),
    )
    for index, (run_id, experiment_id) in enumerate(
        ((run_one, exp_one), (run_two, exp_two)), start=1
    ):
        register_hypothesis_attempt(
            run_id,
            experiment_id=experiment_id,
            family_key=family.family_key,
            hypothesis_text=f"hypothesis {index}",
            round_index=index,
            phase="confirmation",
            split_hash=str(index) * 64,
            alpha_allocated=0.01,
        )
    attempts = list_scientific_family_attempts(family.family_id)
    assert len(attempts) == 2
    assert {item["run_id"] for item in attempts} == {run_one, run_two}
    assert {item["research_family_id"] for item in attempts} == {family.family_id}
    with pytest.raises(DBAPIError, match="family identity is immutable"):
        with session_scope() as session:
            session.execute(
                update(HypothesisAttempt)
                .where(HypothesisAttempt.id == attempts[0]["id"])
                .values(research_family_id=None)
            )
    with pytest.raises(RuntimeError, match="family key"):
        register_hypothesis_attempt(
            run_two,
            experiment_id=None,
            family_key="new-campaign-reset",
            hypothesis_text="attempt to reset",
            round_index=1,
            phase="final_holdout",
            split_hash="f" * 64,
            alpha_allocated=0.05,
        )


def test_budget_data_question_run_and_experiment_bindings_rebuild_together() -> None:
    seed = uuid.uuid4().hex
    store, quest, program, _family_spec, campaign = _seed_hierarchy(seed)
    run_id = create_run("program graph integration", domain="materials")
    store.bind_run(
        CampaignRunBindingSpec(campaign_id=campaign.node_id, run_id=run_id),
        _ctx(seed, "run"),
    )
    experiment_id = create_experiment(run_id, {"hypothesis": "H", "round": 1})
    store.bind_experiment(
        CampaignExperimentBindingSpec(
            campaign_id=campaign.node_id,
            experiment_id=experiment_id,
        ),
        _ctx(seed, "experiment"),
    )
    world = build_world_model(run_id, identity_seed=f"f11s3-{seed}")
    store_world_model_snapshot(world)
    store.bind_question(
        ProgramQuestionBindingSpec(
            program_id=program.node_id,
            question_sha256=world.question.question_sha256,
        ),
        _ctx(seed, "question"),
    )
    asset_id = register_dataset(
        run_id,
        "benchmark",
        role="primary",
        ref="synthetic",
        status="ready",
    )
    store.allocate_data(
        DataRoleAllocationSpec(
            scope_node_id=program.node_id,
            data_asset_id=asset_id,
            role=DataRole.TRAINING,
            policy={"reuse": "adaptive_only"},
        ),
        _ctx(seed, "data"),
    )
    with pytest.raises(ProgramGraphConflict, match="external-validation"):
        store.allocate_data(
            DataRoleAllocationSpec(
                scope_node_id=program.node_id,
                data_asset_id=asset_id,
                role=DataRole.EXTERNAL_VALIDATION,
            ),
            _ctx(seed, "bad-data-role"),
        )

    quest_budget = store.allocate_budget(
        BudgetAllocationSpec(
            scope_node_id=quest.node_id,
            kind=BudgetKind.USD,
            cap_microunits=2_000_000,
            policy={"approval": "human"},
        ),
        _ctx(seed, "quest-budget"),
    )
    program_budget = store.allocate_budget(
        BudgetAllocationSpec(
            scope_node_id=program.node_id,
            parent_allocation_id=quest_budget.object_id,
            kind=BudgetKind.USD,
            cap_microunits=1_000_000,
            policy={"stop_at_cap": True},
        ),
        _ctx(seed, "program-budget"),
    )
    assert record_budget_event(
        run_id,
        "usd",
        0.6,
        research_budget_allocation_id=program_budget.object_id,
    ) == pytest.approx(0.6)
    with pytest.raises(RuntimeError, match="cap exceeded"):
        record_budget_event(
            run_id,
            "usd",
            0.5,
            research_budget_allocation_id=program_budget.object_id,
        )
    with pytest.raises(DBAPIError, match="cap exceeded"):
        with session_scope() as session:
            session.add(
                BudgetEvent(
                    run_id=run_id,
                    research_budget_allocation_id=program_budget.object_id,
                    kind="usd",
                    amount=0.5,
                    cumulative=1.1,
                )
            )
            session.flush()

    snapshot = store.get_quest(quest.node_id)
    binding_types = {item.binding_type for item in snapshot.external_bindings}
    assert binding_types == {"run", "experiment", "research_question"}
    assert len(snapshot.data_allocations) == 1
    assert {item.allocation_id for item in snapshot.budget_allocations} == {
        quest_budget.object_id,
        program_budget.object_id,
    }
    update_dataset(asset_id, ref="rebound-after-allocation")
    try:
        with pytest.raises(ProgramGraphInvariantError, match="asset scope changed"):
            store.get_quest(quest.node_id)
    finally:
        update_dataset(asset_id, ref="synthetic")
    assert store.get_quest(quest.node_id).graph_sha256 == snapshot.graph_sha256
