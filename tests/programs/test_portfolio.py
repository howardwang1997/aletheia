from __future__ import annotations

import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import DBAPIError

import aletheia.api.programs as programs_api
from aletheia.api.deps import require_access
from aletheia.api.main import app
from aletheia.db import create_all, engine
from aletheia.programs import (
    CampaignSpec,
    CampaignRunBindingSpec,
    BudgetAllocationSpec,
    GraphCommandContext,
    GraphNodeState,
    HumanPortfolioPlanSpec,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    NodeTransitionSpec,
    PORTFOLIO_SELECTOR_CODE_SHA256,
    PortfolioActionSpec,
    PortfolioActionType,
    PortfolioAssessmentBatch,
    PortfolioAssessmentManifest,
    PortfolioAssessorKind,
    PortfolioBudgetAvailability,
    PortfolioCandidateAssessment,
    PortfolioCostEstimate,
    PortfolioHypothesisLikelihood,
    PortfolioInformationModel,
    PortfolioMeasurementStatus,
    PortfolioOutcomeProbability,
    PortfolioPriorProbability,
    PortfolioProposal,
    PortfolioRiskLevel,
    PortfolioSelectionPolicy,
    PortfolioShadowAuditPolicy,
    PortfolioSlateSpec,
    ProgramGraphStore,
    QuestSpec,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
    ResearchPortfolioConflict,
    ResearchPortfolioStale,
    ResearchPortfolioStore,
    ResearchProgramSpec,
    ScientificFamilySpec,
    TaskContextRequest,
)
from aletheia.programs.persistence import ResearchPortfolioSlateRecord
from aletheia.programs.portfolio_harness import (
    derive_information_audit,
    derive_shadow_epoch,
)
from aletheia.programs.portfolio_schemas import (
    PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
)
from aletheia.programs.schemas import BudgetKind, DataRole
from aletheia.memory.service import create_run, record_budget_event
from aletheia.reproducibility.manifest import content_sha256
from aletheia.schema_migrations import schema_diffs


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _ctx(seed: str, label: str, principal: str = "pytest:f11s5") -> GraphCommandContext:
    return GraphCommandContext(
        idempotency_key=f"f11s5:{seed[:20]}:{label}",
        principal=principal,
    )


def _sha(label: str) -> str:
    return content_sha256({"f11s5_fixture": label})


def _information_model(label: str = "discriminating") -> PortfolioInformationModel:
    return PortfolioInformationModel(
        belief_state_sha256=_sha(f"{label}:belief"),
        prediction_receipt_sha256=_sha(f"{label}:prediction"),
        priors=(
            PortfolioPriorProbability(hypothesis_id="h1", probability_ppm=500_000),
            PortfolioPriorProbability(hypothesis_id="h2", probability_ppm=500_000),
        ),
        likelihoods=(
            PortfolioHypothesisLikelihood(
                hypothesis_id="h1",
                outcomes=(
                    PortfolioOutcomeProbability(outcome_id="negative", probability_ppm=100_000),
                    PortfolioOutcomeProbability(outcome_id="positive", probability_ppm=900_000),
                ),
            ),
            PortfolioHypothesisLikelihood(
                hypothesis_id="h2",
                outcomes=(
                    PortfolioOutcomeProbability(outcome_id="negative", probability_ppm=900_000),
                    PortfolioOutcomeProbability(outcome_id="positive", probability_ppm=100_000),
                ),
            ),
        ),
    )


def _seed_active_hierarchy(seed: str, base: datetime):
    graph = ProgramGraphStore()
    quest = QuestSpec(
        identity_key=f"portfolio-quest-{seed}",
        title="Shadow portfolio Quest",
        direction="Discriminate mechanisms across a scientific portfolio.",
        value_boundary="Negative results and replication debt remain visible.",
        safety_boundary=("No autonomous allocation",),
    )
    program = ResearchProgramSpec(
        quest_id=quest.node_id,
        identity_key=f"portfolio-program-{seed}",
        title="Portfolio Program",
        objective="Choose the next falsifiable action.",
        problem_domain="synthetic",
    )
    family = ScientificFamilySpec(
        program_id=program.node_id,
        family_key=f"portfolio-family-{seed[:20]}",
        title="Mechanism family",
        scientific_scope="One causal claim across discriminating and replication actions.",
    )
    campaign = CampaignSpec(
        program_id=program.node_id,
        family_id=family.family_id,
        identity_key=f"portfolio-campaign-{seed}",
        title="Portfolio Campaign",
        objective="Run a prediction-bound discrimination experiment.",
    )
    graph.create_quest(quest, _ctx(seed, "quest"), now=base - timedelta(seconds=20))
    graph.create_program(program, _ctx(seed, "program"), now=base - timedelta(seconds=19))
    graph.create_scientific_family(family, _ctx(seed, "family"), now=base - timedelta(seconds=18))
    graph.create_campaign(campaign, _ctx(seed, "campaign"), now=base - timedelta(seconds=17))
    for node_id, target, label, offset in (
        (quest.node_id, GraphNodeState.ACTIVE, "quest-active", -16),
        (program.node_id, GraphNodeState.ACTIVE, "program-active", -15),
        (campaign.node_id, GraphNodeState.ACTIVE, "campaign-active", -14),
    ):
        graph.transition_node(
            NodeTransitionSpec(
                node_id=node_id,
                expected_version=1,
                to_state=target,
                reason="fixture is ready for shadow portfolio scoring",
            ),
            _ctx(seed, label),
            now=base + timedelta(seconds=offset),
        )
    return graph, quest, program, family, campaign


def _portfolio_context(tmp_path, seed: str, quest: QuestSpec, base: datetime):
    memory = ResearchMemoryStore(archive_root=tmp_path / f"memory-{seed}")
    fact = ResearchMemoryFactSpec(
        scope_node_id=quest.node_id,
        kind=MemoryFactKind.GOAL,
        statement="Prefer the action that can falsify the live mechanism claim.",
        detail={"fixture": seed},
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="portfolio-plan",
                context_role=MemoryContextRole.REQUIRED,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=f"portfolio-source-{seed}",
                sha256=_sha(f"{seed}:memory-source"),
                uri=f"fixture://{seed}/portfolio-memory",
            ),
        ),
    )
    memory.register_fact(
        fact,
        _ctx(seed, "memory-fact"),
        now=base - timedelta(seconds=5),
    )
    memory.compact(
        scope_node_id=quest.node_id,
        task_key="portfolio-plan",
        draft=MemorySummaryDraft(
            producer_provider="fixture",
            producer_model="summary-model",
            prompt_sha256=_sha(f"{seed}:summary-prompt"),
            summary_text="The mechanism remains live and must face a discriminating test.",
            covered_fact_ids=(fact.fact_id,),
        ),
        context=_ctx(seed, "memory-compact"),
        now=base - timedelta(seconds=4),
    )
    receipt = memory.build_task_context(
        TaskContextRequest(
            scope_node_id=quest.node_id,
            task_key="portfolio-plan",
            consumer_provider="fixture-provider",
            consumer_model="planner-model",
        ),
        _ctx(seed, "memory-context"),
        now=base - timedelta(seconds=3),
    )
    return memory, receipt


def _assessment(
    action: PortfolioActionSpec,
    *,
    completed_at: datetime,
    information_model: PortfolioInformationModel | None,
    label: str,
    measurement: PortfolioMeasurementStatus = PortfolioMeasurementStatus.VALIDATED,
    costs: tuple[PortfolioCostEstimate, ...] = (),
    required_roles: tuple[DataRole, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    available_capabilities: tuple[str, ...] = (),
    risk: PortfolioRiskLevel = PortfolioRiskLevel.LOW,
) -> PortfolioCandidateAssessment:
    return PortfolioCandidateAssessment(
        candidate_id=action.candidate_id,
        action_sha256=action.action_sha256,
        estimated_costs=costs,
        estimated_duration_seconds=3_600,
        risk_level=risk,
        measurement_status=measurement,
        measurement_evidence_sha256=(
            _sha(f"{label}:measurement")
            if measurement is PortfolioMeasurementStatus.VALIDATED
            else None
        ),
        required_capability_sha256s=required_capabilities,
        available_capability_sha256s=available_capabilities,
        required_data_roles=required_roles,
        data_readiness_evidence_sha256=(_sha(f"{label}:data") if required_roles else None),
        information_model=information_model,
        importance_ppm=900_000,
        novelty_ppm=700_000,
        success_probability_ppm=800_000,
        value_evidence_sha256=_sha(f"{label}:value"),
        replication_debt_ledger_sha256=_sha(f"{label}:replication-ledger"),
        replication_debt_before=0,
        expected_replication_debt_reduction=0,
        correlation_tags=(f"corr-{label}",),
        diversity_tags=(f"diversity-{label}",),
        assessment_evidence_sha256s=(_sha(f"{label}:assessment"),),
        completed_at=completed_at,
    )


def _seed_slate(tmp_path, seed: str, *, with_budget: bool = False):
    base = datetime.now(timezone.utc) - timedelta(seconds=10)
    graph_store, quest, program, family, campaign = _seed_active_hierarchy(seed, base)
    budget_allocation_id = None
    run_id = None
    if with_budget:
        run_id = create_run("Portfolio budget fixture", domain="synthetic")
        graph_store.bind_run(
            CampaignRunBindingSpec(campaign_id=campaign.node_id, run_id=run_id),
            _ctx(seed, "bind-run"),
            now=base - timedelta(seconds=13),
        )
        quest_budget = graph_store.allocate_budget(
            BudgetAllocationSpec(
                scope_node_id=quest.node_id,
                kind=BudgetKind.USD,
                cap_microunits=10_000_000,
            ),
            _ctx(seed, "quest-budget"),
            now=base - timedelta(seconds=12),
        )
        program_budget = graph_store.allocate_budget(
            BudgetAllocationSpec(
                scope_node_id=program.node_id,
                parent_allocation_id=quest_budget.object_id,
                kind=BudgetKind.USD,
                cap_microunits=5_000_000,
            ),
            _ctx(seed, "program-budget"),
            now=base - timedelta(seconds=11),
        )
        budget_allocation_id = program_budget.object_id
    memory, context_receipt = _portfolio_context(tmp_path, seed, quest, base)
    graph = graph_store.get_quest(quest.node_id)
    graph_times = [item.updated_at for item in graph.nodes]
    graph_times.extend(item.created_at for item in graph.transitions)
    graph_times.extend(item.created_at for item in graph.dependencies)
    graph_times.extend(item.created_at for item in graph.scientific_families)
    graph_times.extend(item.created_at for item in graph.external_bindings)
    graph_times.extend(item.created_at for item in graph.data_allocations)
    graph_times.extend(item.created_at for item in graph.budget_allocations)
    # Graph projections deliberately use database-owned timestamps.  Anchor the
    # synthetic portfolio clock to the actual frozen dependencies instead of the
    # command test clock, which only controls receipts and outbox events.
    base = max(*graph_times, context_receipt.command.committed_at)
    experiment = PortfolioActionSpec(
        action_type=PortfolioActionType.DISCRIMINATING_EXPERIMENT,
        target_node_id=campaign.node_id,
        task_key="campaign:discriminate",
        title="Run the discriminating assay",
        rationale="The prediction separates the two live hypotheses.",
    )
    repair = PortfolioActionSpec(
        action_type=PortfolioActionType.REPAIR_CAPABILITY,
        target_node_id=program.node_id,
        task_key="program:repair",
        title="Repair the measurement adapter",
        rationale="A stronger adapter reduces execution risk across campaigns.",
    )
    proposal = PortfolioProposal(
        quest_id=quest.node_id,
        graph_sha256=graph.graph_sha256,
        memory_context_receipt_id=context_receipt.context_receipt_id,
        proposer_principal="model:planner",
        proposer_provider="fixture-provider",
        proposer_model="planner-model",
        proposer_model_identity_sha256=_sha(f"{seed}:planner-model"),
        prompt_sha256=_sha(f"{seed}:planner-prompt"),
        candidates=(repair, experiment),
        generated_at=base,
    )
    assessments = (
        _assessment(
            experiment,
            completed_at=base,
            information_model=_information_model(seed),
            label="experiment",
        ),
        _assessment(
            repair,
            completed_at=base,
            information_model=None,
            label="repair",
            measurement=PortfolioMeasurementStatus.UNKNOWN,
        ),
    )
    manifest = PortfolioAssessmentManifest(
        assessor_principal="harness:assessor",
        assessor_kind=PortfolioAssessorKind.DETERMINISTIC_HARNESS,
        assessor_code_sha256=_sha(f"{seed}:assessor-code"),
        output_schema_sha256=PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
        frozen_at=base,
    )
    spec = PortfolioSlateSpec(
        policy=PortfolioSelectionPolicy(
            policy_id=f"portfolio-policy-{seed[:20]}",
            quest_id=quest.node_id,
            selector_code_sha256=PORTFOLIO_SELECTOR_CODE_SHA256,
            frozen_at=base,
        ),
        proposal=proposal,
        assessment_batch=PortfolioAssessmentBatch(
            manifest=manifest,
            assessments=assessments,
            completed_at=base,
        ),
    )
    store = ResearchPortfolioStore()
    store._memory = memory
    return {
        "store": store,
        "graph_store": graph_store,
        "quest": quest,
        "program": program,
        "family": family,
        "campaign": campaign,
        "spec": spec,
        "base": base,
        "actions": {item.action_type: item for item in proposal.candidates},
        "memory": memory,
        "run_id": run_id,
        "budget_allocation_id": budget_allocation_id,
    }


def test_migration_matches_orm_and_portfolio_guards_exist() -> None:
    expected = {
        "research_portfolio_slates",
        "research_portfolio_candidates",
        "research_portfolio_human_plans",
        "research_portfolio_epochs",
        "research_portfolio_scores",
    }
    with engine().connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        assert schema_diffs(connection) == []
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'trg_research_portfolio_%'"
                )
            ).scalars()
        )
    assert "trg_research_portfolio_slate_guard" in triggers
    assert "trg_research_portfolio_slate_complete" in triggers
    assert "trg_research_portfolio_human_plan_guard" in triggers
    assert "trg_research_portfolio_epoch_complete" in triggers
    assert "trg_research_portfolio_scores_append_only" in triggers


def test_model_cannot_inject_a_self_score_and_assessor_must_be_independent(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    action = fixture["actions"][PortfolioActionType.DISCRIMINATING_EXPERIMENT]
    with pytest.raises(ValidationError, match="extra"):
        PortfolioActionSpec.model_validate(
            {**action.model_dump(mode="json"), "total_score": 1_000_000}
        )
    spec = fixture["spec"]
    manifest = spec.assessment_batch.manifest.model_copy(
        update={"assessor_principal": spec.proposal.proposer_principal}
    )
    with pytest.raises(ValidationError, match="independent"):
        PortfolioSlateSpec(
            policy=spec.policy,
            proposal=spec.proposal,
            assessment_batch=spec.assessment_batch.model_copy(update={"manifest": manifest}),
        )


def test_hard_filters_are_harness_derived_and_canonical(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    spec = fixture["spec"]
    action = fixture["actions"][PortfolioActionType.DISCRIMINATING_EXPERIMENT]
    bad_information = PortfolioInformationModel(
        belief_state_sha256=_sha("bad-belief"),
        prediction_receipt_sha256=_sha("bad-prediction"),
        priors=_information_model().priors,
        likelihoods=(
            PortfolioHypothesisLikelihood(
                hypothesis_id=hypothesis,
                outcomes=(
                    PortfolioOutcomeProbability(outcome_id="negative", probability_ppm=500_000),
                    PortfolioOutcomeProbability(outcome_id="positive", probability_ppm=500_000),
                ),
            )
            for hypothesis in ("h1", "h2")
        ),
    )
    missing_capability = _sha("missing-capability")
    bad = _assessment(
        action,
        completed_at=fixture["base"] + timedelta(seconds=2),
        information_model=bad_information,
        label="blocked",
        measurement=PortfolioMeasurementStatus.UNKNOWN,
        costs=(PortfolioCostEstimate(kind=BudgetKind.USD, amount_microunits=1),),
        required_roles=(DataRole.CONFIRMATION,),
        required_capabilities=(missing_capability,),
        risk=PortfolioRiskLevel.HIGH,
    )
    assessments = tuple(
        bad if item.candidate_id == bad.candidate_id else item
        for item in spec.assessment_batch.assessments
    )
    blocked_spec = PortfolioSlateSpec(
        policy=spec.policy,
        proposal=spec.proposal,
        assessment_batch=PortfolioAssessmentBatch(
            manifest=spec.assessment_batch.manifest,
            assessments=assessments,
            completed_at=bad.completed_at,
        ),
    )
    human = HumanPortfolioPlanSpec(
        selected_candidate_ids=(action.candidate_id,),
        rationale="Exercise every hard gate.",
        issued_at=fixture["base"] + timedelta(seconds=4),
    )
    derived = derive_shadow_epoch(
        spec=blocked_spec,
        graph=fixture["graph_store"].get_quest(fixture["quest"].node_id),
        budget_state=(),
        human_plan=human,
        evaluated_at=fixture["base"] + timedelta(seconds=5),
    )
    score = next(item for item in derived.scores if item.candidate_id == action.candidate_id)
    assert score.feasible is False
    assert score.information_audit is not None
    assert score.information_audit.expected_information_gain_micronats == 0
    assert score.blockers == tuple(sorted(score.blockers))
    assert "information:eig_below_floor" in score.blockers
    assert "measurement:not_validated:unknown" in score.blockers
    assert "budget:program_allocation_missing:usd" in score.blockers
    assert "data:missing_role:confirmation" in score.blockers
    assert f"capability:missing:{missing_capability}" in score.blockers
    assert "risk:level_exceeded" in score.blockers
    assert "approval:missing" in score.blockers
    assert derived.comparison.human_hard_filter_violations


def test_shadow_workflow_replays_without_mutating_graph_budget_or_actions(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    store = fixture["store"]
    spec = fixture["spec"]
    graph_before = fixture["graph_store"].get_quest(fixture["quest"].node_id)
    register_context = _ctx(seed, "register-slate", "controller:portfolio")
    first = store.register_slate(
        spec,
        register_context,
        now=fixture["base"] + timedelta(seconds=4),
    )
    second = store.register_slate(
        spec,
        register_context,
        now=fixture["base"] + timedelta(days=1),
    )
    assert first.created is True
    assert second.created is False
    assert first.object_id == spec.slate_id

    plan = HumanPortfolioPlanSpec(
        selected_candidate_ids=tuple(item.candidate_id for item in spec.proposal.candidates),
        rationale="Human baseline selects both individually useful actions.",
        issued_at=fixture["base"] + timedelta(seconds=5),
    )
    plan_receipt = store.commit_human_plan(
        slate_id=spec.slate_id,
        plan=plan,
        context=_ctx(seed, "human-plan", "human:reviewer"),
        now=fixture["base"] + timedelta(seconds=5),
    )
    epoch_receipt = store.evaluate_slate(
        slate_id=spec.slate_id,
        context=_ctx(seed, "evaluate", "harness:portfolio"),
        now=fixture["base"] + timedelta(seconds=6),
    )
    slate = store.get_slate(spec.slate_id)
    epoch = store.get_epoch(epoch_receipt.object_id)
    assert slate.human_plan_id == plan_receipt.object_id
    assert slate.epoch_id == epoch.epoch_id
    assert epoch.decision.shadow_only is True
    assert epoch.decision.actions_enqueued is False
    assert set(epoch.decision.selected_candidate_ids) == set(plan.selected_candidate_ids)
    assert epoch.comparison.exact_set_match is True
    assert epoch.comparison.jaccard_ppm == 1_000_000
    assert fixture["graph_store"].get_quest(fixture["quest"].node_id) == graph_before
    audit = store.shadow_audit(
        quest_id=fixture["quest"].node_id,
        policy=PortfolioShadowAuditPolicy(
            minimum_epochs=1,
            minimum_mean_jaccard_ppm=1_000_000,
        ),
    )
    assert audit.eligible_for_human_activation_review is True
    assert audit.autonomous_allocation_enabled is False

    fixture["graph_store"].create_scientific_family(
        ScientificFamilySpec(
            program_id=fixture["program"].node_id,
            family_key=f"post-epoch-{seed[:20]}",
            title="Post-epoch scientific family",
            scientific_scope="A later graph change must not erase the frozen epoch.",
        ),
        _ctx(seed, "post-epoch-family"),
    )
    assert store.get_epoch(epoch.epoch_id) == epoch

    with pytest.raises(DBAPIError, match="append-only"):
        with engine().begin() as connection:
            connection.execute(
                update(ResearchPortfolioSlateRecord)
                .where(ResearchPortfolioSlateRecord.slate_id == spec.slate_id)
                .values(spec_sha256="0" * 64)
            )


def test_graph_change_blocks_new_epoch_but_frozen_slate_stays_auditable(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    store = fixture["store"]
    spec = fixture["spec"]
    store.register_slate(
        spec,
        _ctx(seed, "register", "controller:portfolio"),
        now=fixture["base"] + timedelta(seconds=4),
    )
    plan = HumanPortfolioPlanSpec(
        selected_candidate_ids=(),
        rationale="Human abstains before seeing the planner result.",
        issued_at=fixture["base"] + timedelta(seconds=5),
    )
    store.commit_human_plan(
        slate_id=spec.slate_id,
        plan=plan,
        context=_ctx(seed, "human", "human:reviewer"),
        now=fixture["base"] + timedelta(seconds=5),
    )
    fixture["graph_store"].create_scientific_family(
        ScientificFamilySpec(
            program_id=fixture["program"].node_id,
            family_key=f"post-slate-{seed[:20]}",
            title="Post-slate scientific family",
            scientific_scope="New evidence expands the graph after the slate was frozen.",
        ),
        _ctx(seed, "post-slate-family"),
    )
    with pytest.raises(ResearchPortfolioStale, match="graph changed"):
        store.evaluate_slate(
            slate_id=spec.slate_id,
            context=_ctx(seed, "evaluate", "harness:portfolio"),
            now=fixture["base"] + timedelta(seconds=6),
        )
    frozen = store.get_slate(spec.slate_id)
    assert frozen.graph_snapshot.graph_sha256 == spec.proposal.graph_sha256
    assert frozen.epoch_id is None


def test_budget_spend_after_freeze_blocks_evaluation_without_reserving_more(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed, with_budget=True)
    store = fixture["store"]
    spec = fixture["spec"]
    store.register_slate(
        spec,
        _ctx(seed, "register", "controller:portfolio"),
        now=fixture["base"] + timedelta(seconds=4),
    )
    frozen = store.get_slate(spec.slate_id)
    assert frozen.budget_state[0].available_microunits == 5_000_000
    plan = HumanPortfolioPlanSpec(
        selected_candidate_ids=(),
        rationale="Freeze the human baseline before another Run spends budget.",
        issued_at=fixture["base"] + timedelta(seconds=5),
    )
    store.commit_human_plan(
        slate_id=spec.slate_id,
        plan=plan,
        context=_ctx(seed, "human", "human:reviewer"),
        now=fixture["base"] + timedelta(seconds=5),
    )
    record_budget_event(
        fixture["run_id"],
        "usd",
        0.25,
        research_budget_allocation_id=fixture["budget_allocation_id"],
    )
    with pytest.raises(ResearchPortfolioStale, match="budget changed"):
        store.evaluate_slate(
            slate_id=spec.slate_id,
            context=_ctx(seed, "evaluate", "harness:portfolio"),
            now=fixture["base"] + timedelta(seconds=6),
        )
    assert store.get_slate(spec.slate_id).budget_state == frozen.budget_state


def test_new_scientific_memory_blocks_evaluation_but_not_old_slate_audit(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    store = fixture["store"]
    spec = fixture["spec"]
    store.register_slate(
        spec,
        _ctx(seed, "register", "controller:portfolio"),
        now=fixture["base"] + timedelta(seconds=4),
    )
    store.commit_human_plan(
        slate_id=spec.slate_id,
        plan=HumanPortfolioPlanSpec(
            selected_candidate_ids=(),
            rationale="Commit before the contradictory evidence arrives.",
            issued_at=fixture["base"] + timedelta(seconds=5),
        ),
        context=_ctx(seed, "human", "human:reviewer"),
        now=fixture["base"] + timedelta(seconds=5),
    )
    new_fact = ResearchMemoryFactSpec(
        scope_node_id=fixture["quest"].node_id,
        kind=MemoryFactKind.CONTRADICTION,
        statement="A new contradiction changes the portfolio planning context.",
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="portfolio-plan",
                context_role=MemoryContextRole.SUPPORTING,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=f"late-memory-{seed}",
                sha256=_sha(f"{seed}:late-memory"),
            ),
        ),
    )
    fixture["memory"].register_fact(new_fact, _ctx(seed, "late-memory"))
    with pytest.raises(ResearchPortfolioStale, match="memory context"):
        store.evaluate_slate(
            slate_id=spec.slate_id,
            context=_ctx(seed, "evaluate", "harness:portfolio"),
            now=fixture["base"] + timedelta(seconds=6),
        )
    assert store.get_slate(spec.slate_id).spec == spec


def test_replication_quota_is_forced_or_fails_closed(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    spec = fixture["spec"]
    replication = PortfolioActionSpec(
        action_type=PortfolioActionType.REPLICATION,
        target_node_id=fixture["campaign"].node_id,
        task_key="campaign:replicate",
        title="Independently replicate the mechanism assay",
        rationale="The family has unresolved replication debt.",
    )
    replication_assessment_payload = _assessment(
        replication,
        completed_at=fixture["base"] + timedelta(seconds=2),
        information_model=_information_model(f"{seed}:replication"),
        label="replication",
    ).model_dump(mode="python")
    replication_assessment_payload.update(
        {
            "replication_debt_before": 10,
            "expected_replication_debt_reduction": 5,
            "independent_replication_protocol_sha256": _sha(f"{seed}:replication-protocol"),
        }
    )
    replication_assessment = PortfolioCandidateAssessment.model_validate(
        replication_assessment_payload
    )
    proposal = PortfolioProposal.model_validate(
        {
            **spec.proposal.model_dump(mode="python"),
            "candidates": (*spec.proposal.candidates, replication),
        }
    )
    assessment_batch = PortfolioAssessmentBatch(
        manifest=spec.assessment_batch.manifest,
        assessments=(*spec.assessment_batch.assessments, replication_assessment),
        completed_at=replication_assessment.completed_at,
    )
    quota_spec = PortfolioSlateSpec(
        policy=PortfolioSelectionPolicy.model_validate(
            {
                **spec.policy.model_dump(mode="python"),
                "minimum_replication_actions": 1,
            }
        ),
        proposal=proposal,
        assessment_batch=assessment_batch,
    )
    human = HumanPortfolioPlanSpec(
        selected_candidate_ids=(),
        rationale="Human baseline abstains.",
        issued_at=fixture["base"] + timedelta(seconds=4),
    )
    selected = derive_shadow_epoch(
        spec=quota_spec,
        graph=fixture["graph_store"].get_quest(fixture["quest"].node_id),
        budget_state=(),
        human_plan=human,
        evaluated_at=fixture["base"] + timedelta(seconds=5),
    )
    assert replication.candidate_id in selected.decision.selected_candidate_ids
    selected_entry = next(
        item for item in selected.decision.rankings if item.candidate_id == replication.candidate_id
    )
    assert selected_entry.reasons == ("selected:replication_quota",)

    blocked_payload = replication_assessment.model_dump(mode="python")
    blocked_payload.update(
        {
            "measurement_status": PortfolioMeasurementStatus.UNKNOWN,
            "measurement_evidence_sha256": None,
        }
    )
    blocked_replication = PortfolioCandidateAssessment.model_validate(blocked_payload)
    blocked_spec = PortfolioSlateSpec(
        policy=quota_spec.policy,
        proposal=proposal,
        assessment_batch=PortfolioAssessmentBatch(
            manifest=assessment_batch.manifest,
            assessments=tuple(
                blocked_replication if item.candidate_id == replication.candidate_id else item
                for item in assessment_batch.assessments
            ),
            completed_at=assessment_batch.completed_at,
        ),
    )
    blocked = derive_shadow_epoch(
        spec=blocked_spec,
        graph=fixture["graph_store"].get_quest(fixture["quest"].node_id),
        budget_state=(),
        human_plan=human,
        evaluated_at=fixture["base"] + timedelta(seconds=5),
    )
    assert blocked.decision.disposition.value == "policy_blocked"
    assert blocked.decision.selected_candidate_ids == ()


def test_only_one_blinded_human_plan_can_win_the_concurrent_race(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    store = fixture["store"]
    spec = fixture["spec"]
    store.register_slate(
        spec,
        _ctx(seed, "register", "controller:portfolio"),
        now=fixture["base"] + timedelta(seconds=4),
    )
    plans = (
        HumanPortfolioPlanSpec(
            selected_candidate_ids=(),
            rationale="Concurrent human baseline A.",
            issued_at=fixture["base"] + timedelta(seconds=5),
        ),
        HumanPortfolioPlanSpec(
            selected_candidate_ids=(spec.proposal.candidates[0].candidate_id,),
            rationale="Concurrent human baseline B.",
            issued_at=fixture["base"] + timedelta(seconds=5),
        ),
    )

    def commit(index: int):
        return store.commit_human_plan(
            slate_id=spec.slate_id,
            plan=plans[index],
            context=_ctx(seed, f"human-{index}", f"human:reviewer-{index}"),
            now=fixture["base"] + timedelta(seconds=5),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit, index) for index in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except ResearchPortfolioConflict as exc:
                outcomes.append(exc)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ResearchPortfolioConflict) for item in outcomes) == 1
    assert store.get_slate(spec.slate_id).human_plan_id is not None


def test_portfolio_api_exposes_only_shadow_receipts_and_readiness_review(
    tmp_path, monkeypatch
) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    spec = fixture["spec"]
    monkeypatch.setattr(programs_api, "_PORTFOLIO_STORE", fixture["store"])
    app.dependency_overrides[require_access] = lambda: {
        "id": "portfolio-owner",
        "role": "owner",
    }
    try:
        with TestClient(app) as client:
            registered = client.post(
                "/legacy/research-graph/portfolios/slates",
                json={
                    "idempotency_key": f"api:{seed}:register",
                    "slate": spec.model_dump(mode="json"),
                },
            )
            assert registered.status_code == 200, registered.text
            assert registered.json()["object_id"] == spec.slate_id
            plan = HumanPortfolioPlanSpec(
                selected_candidate_ids=tuple(
                    item.candidate_id for item in spec.proposal.candidates
                ),
                rationale="API human baseline remains observation-blind.",
                issued_at=datetime.fromisoformat(registered.json()["command"]["committed_at"]),
            )
            committed = client.post(
                f"/legacy/research-graph/portfolios/slates/{spec.slate_id}/human-plan",
                json={
                    "idempotency_key": f"api:{seed}:human",
                    "plan": plan.model_dump(mode="json"),
                },
            )
            assert committed.status_code == 200, committed.text
            evaluated = client.post(
                f"/legacy/research-graph/portfolios/slates/{spec.slate_id}/evaluate",
                json={"idempotency_key": f"api:{seed}:evaluate"},
            )
            assert evaluated.status_code == 200, evaluated.text
            epoch_id = evaluated.json()["object_id"]
            epoch = client.get(f"/legacy/research-graph/portfolios/epochs/{epoch_id}")
            assert epoch.status_code == 200
            assert epoch.json()["decision"]["shadow_only"] is True
            assert epoch.json()["decision"]["actions_enqueued"] is False
            listed = client.get(
                f"/legacy/research-graph/quests/{fixture['quest'].node_id}/portfolios"
            )
            assert listed.status_code == 200
            assert listed.json()[0]["epoch_id"] == epoch_id
            audit = client.get(
                f"/legacy/research-graph/quests/{fixture['quest'].node_id}/portfolio-shadow-audit",
                params={
                    "minimum_epochs": 1,
                    "minimum_mean_jaccard_ppm": 1_000_000,
                },
            )
            assert audit.status_code == 200
            assert audit.json()["eligible_for_human_activation_review"] is True
            assert audit.json()["autonomous_allocation_enabled"] is False
    finally:
        app.dependency_overrides.clear()


def test_random_candidate_input_order_has_one_deterministic_decision(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    spec = fixture["spec"]
    graph = fixture["graph_store"].get_quest(fixture["quest"].node_id)
    plan = HumanPortfolioPlanSpec(
        selected_candidate_ids=tuple(item.candidate_id for item in spec.proposal.candidates),
        rationale="Order independence fixture.",
        issued_at=fixture["base"] + timedelta(seconds=4),
    )
    canonical = derive_shadow_epoch(
        spec=spec,
        graph=graph,
        budget_state=(),
        human_plan=plan,
        evaluated_at=fixture["base"] + timedelta(seconds=5),
    )
    for shuffle_seed in range(20):
        candidates = list(spec.proposal.candidates)
        assessments = list(spec.assessment_batch.assessments)
        random.Random(shuffle_seed).shuffle(candidates)
        random.Random(shuffle_seed + 100).shuffle(assessments)
        shuffled = PortfolioSlateSpec(
            policy=spec.policy,
            proposal=spec.proposal.model_copy(update={"candidates": tuple(candidates)}),
            assessment_batch=spec.assessment_batch.model_copy(
                update={"assessments": tuple(assessments)}
            ),
        )
        assert (
            derive_shadow_epoch(
                spec=shuffled,
                graph=graph,
                budget_state=(),
                human_plan=plan,
                evaluated_at=fixture["base"] + timedelta(seconds=5),
            )
            == canonical
        )


def test_batch_budget_is_projected_without_reservation_or_overspend(tmp_path) -> None:
    seed = uuid.uuid4().hex
    fixture = _seed_slate(tmp_path, seed)
    spec = fixture["spec"]
    costly_assessments = []
    for assessment in spec.assessment_batch.assessments:
        payload = assessment.model_dump(mode="python")
        payload["estimated_costs"] = (
            PortfolioCostEstimate(kind=BudgetKind.USD, amount_microunits=600_000),
        )
        costly_assessments.append(PortfolioCandidateAssessment.model_validate(payload))
    costly_spec = PortfolioSlateSpec(
        policy=spec.policy,
        proposal=spec.proposal,
        assessment_batch=PortfolioAssessmentBatch(
            manifest=spec.assessment_batch.manifest,
            assessments=tuple(costly_assessments),
            completed_at=spec.assessment_batch.completed_at,
        ),
    )
    budget = (
        PortfolioBudgetAvailability(
            allocation_id="bga_" + "1" * 32,
            program_id=fixture["program"].node_id,
            kind=BudgetKind.USD,
            cap_microunits=1_000_000,
            spent_microunits=0,
            available_microunits=1_000_000,
        ),
    )
    human = HumanPortfolioPlanSpec(
        selected_candidate_ids=tuple(item.candidate_id for item in spec.proposal.candidates),
        rationale="Human baseline intentionally overcommits the shared budget.",
        issued_at=fixture["base"] + timedelta(seconds=4),
    )
    derived = derive_shadow_epoch(
        spec=costly_spec,
        graph=fixture["graph_store"].get_quest(fixture["quest"].node_id),
        budget_state=budget,
        human_plan=human,
        evaluated_at=fixture["base"] + timedelta(seconds=5),
    )
    assert len(derived.decision.selected_candidate_ids) == 1
    projection = derived.decision.budget_projection[0]
    assert projection.before_microunits == 1_000_000
    assert projection.selected_microunits == 600_000
    assert projection.after_microunits == 400_000
    assert any(
        "batch:budget_exceeded:usd" in reason
        for entry in derived.decision.rankings
        for reason in entry.reasons
    )
    assert derived.comparison.human_batch_constraint_violations


def _random_ppm_distribution(rng: random.Random, count: int) -> tuple[int, ...]:
    cuts = sorted(rng.sample(range(1, 1_000_000), count - 1))
    boundaries = (0, *cuts, 1_000_000)
    return tuple(boundaries[index + 1] - boundaries[index] for index in range(count))


def test_random_information_models_reconcile_entropy_exactly() -> None:
    for seed in range(100):
        rng = random.Random(seed)
        hypothesis_count = rng.randint(2, 5)
        outcome_count = rng.randint(2, 6)
        hypothesis_ids = tuple(f"h{index}" for index in range(hypothesis_count))
        outcome_ids = tuple(f"o{index}" for index in range(outcome_count))
        priors = _random_ppm_distribution(rng, hypothesis_count)
        likelihood_values = {
            hypothesis: _random_ppm_distribution(rng, outcome_count)
            for hypothesis in hypothesis_ids
        }
        model = PortfolioInformationModel(
            belief_state_sha256=_sha(f"random:{seed}:belief"),
            prediction_receipt_sha256=_sha(f"random:{seed}:prediction"),
            priors=tuple(
                PortfolioPriorProbability(
                    hypothesis_id=hypothesis,
                    probability_ppm=probability,
                )
                for hypothesis, probability in zip(hypothesis_ids, priors, strict=True)
            ),
            likelihoods=tuple(
                PortfolioHypothesisLikelihood(
                    hypothesis_id=hypothesis,
                    outcomes=tuple(
                        PortfolioOutcomeProbability(
                            outcome_id=outcome,
                            probability_ppm=probability,
                        )
                        for outcome, probability in zip(
                            outcome_ids,
                            likelihood_values[hypothesis],
                            strict=True,
                        )
                    ),
                )
                for hypothesis in hypothesis_ids
            ),
        )
        audit = derive_information_audit(model)
        assert audit == derive_information_audit(model)
        assert (
            audit.expected_posterior_entropy_micronats + audit.expected_information_gain_micronats
            == audit.prior_entropy_micronats
        )
        assert 0 <= audit.expected_information_gain_ratio_ppm <= 1_000_000

    identical = PortfolioInformationModel(
        belief_state_sha256=_sha("identical:belief"),
        prediction_receipt_sha256=_sha("identical:prediction"),
        priors=(
            PortfolioPriorProbability(hypothesis_id="h1", probability_ppm=500_000),
            PortfolioPriorProbability(hypothesis_id="h2", probability_ppm=500_000),
        ),
        likelihoods=tuple(
            PortfolioHypothesisLikelihood(
                hypothesis_id=hypothesis,
                outcomes=(
                    PortfolioOutcomeProbability(outcome_id="o1", probability_ppm=250_000),
                    PortfolioOutcomeProbability(outcome_id="o2", probability_ppm=750_000),
                ),
            )
            for hypothesis in ("h1", "h2")
        ),
    )
    assert derive_information_audit(identical).expected_information_gain_micronats == 0
