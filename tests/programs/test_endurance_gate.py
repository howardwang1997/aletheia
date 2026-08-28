from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.epistemics.persistence import EpistemicResearchQuestionRecord
from aletheia.epistemics.schemas import (
    ResearchQuestion,
    ResearchQuestionKind,
    new_research_question_id,
)
from aletheia.jobs import (
    CORE_ZERO_METRICS,
    FaultBoundary,
    FaultCampaignCommitContext,
    FaultCampaignManifest,
    FaultCampaignStore,
    FaultComparator,
    FaultInjectionOutcome,
    FaultInvariantExpectation,
    FaultMetricObservation,
    FaultRecoveryAction,
    FaultScenarioObservation,
    FaultScenarioSpec,
    evaluate_fault_campaign,
)
from aletheia.memory.service import create_run
from aletheia.programs import (
    REAL_72H_SECONDS,
    BudgetAllocationSpec,
    BudgetKind,
    CampaignRunBindingSpec,
    CampaignSpec,
    EnduranceBudgetState,
    EnduranceCheckpoint,
    EnduranceCheckpointEvidence,
    EnduranceCommandContext,
    EnduranceEfficiencyMetric,
    EnduranceEfficiencyReceipt,
    EnduranceEvidenceClass,
    EnduranceGateDisposition,
    EnduranceGateManifest,
    EnduranceGateReport,
    EnduranceInterruptionKind,
    EnduranceInterruptionReceipt,
    EnduranceLedgerObservation,
    EnduranceReproductionConclusion,
    EnduranceReproductionReceipt,
    EnduranceStrategyFingerprint,
    EnduranceStructuralPivotReceipt,
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
    PortfolioCandidateAssessment,
    PortfolioMeasurementStatus,
    PortfolioProposal,
    PortfolioRiskLevel,
    PortfolioSelectionPolicy,
    PortfolioSlateSpec,
    ProgramGraphStore,
    ProgramQuestionBindingSpec,
    QuestSpec,
    ResearchEnduranceConflict,
    ResearchEnduranceStore,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
    ResearchPortfolioStore,
    ResearchProgramSpec,
    ScientificFamilySpec,
    TaskContextRequest,
    evaluate_endurance_gate,
    prepare_endurance_gate_manifest,
)
from aletheia.programs.persistence import ResearchEnduranceGateRecord
from aletheia.programs.portfolio_schemas import PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256
from aletheia.programs.schemas import (
    BudgetAllocationSnapshot,
    ExternalBindingSnapshot,
    QuestGraphSnapshot,
    ResearchNodeSnapshot,
    ResearchTransitionSnapshot,
)
from aletheia.reproducibility.manifest import content_sha256
from aletheia.schema_migrations import schema_diffs


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _sha(label: str) -> str:
    return content_sha256({"f11s7": label})


def _ctx(seed: str, label: str, principal: str = "pytest:f11s7") -> GraphCommandContext:
    return GraphCommandContext(
        idempotency_key=f"f11s7:{seed[:20]}:{label}",
        principal=principal,
    )


def _endurance_context(seed: str, label: str) -> EnduranceCommandContext:
    return EnduranceCommandContext(
        idempotency_key=f"f11s7:endurance:{seed[:16]}:{label}",
        principal="controller:endurance",
    )


_OUTCOME = {
    FaultBoundary.API_PROCESS: FaultInjectionOutcome.PROCESS_EXIT,
    FaultBoundary.WORKER_PROCESS: FaultInjectionOutcome.PROCESS_EXIT,
    FaultBoundary.DATABASE_CONNECTION: FaultInjectionOutcome.CONNECTION_LOST,
    FaultBoundary.EVALUATOR: FaultInjectionOutcome.TIMEOUT,
    FaultBoundary.PROVIDER: FaultInjectionOutcome.UNAVAILABLE,
    FaultBoundary.DUPLICATE_DELIVERY: FaultInjectionOutcome.DUPLICATE_DELIVERED,
    FaultBoundary.STALE_LEASE: FaultInjectionOutcome.LEASE_EXPIRED,
    FaultBoundary.ARCHIVE_STORAGE: FaultInjectionOutcome.STORAGE_EXHAUSTED,
    FaultBoundary.RUNTIME_IDENTITY: FaultInjectionOutcome.IDENTITY_MISMATCH,
    FaultBoundary.OUTWARD_ACTION: FaultInjectionOutcome.AMBIGUOUS_REMOTE_RESULT,
}


def _passing_fault_report(seed: str, quest_id: str, base: datetime):
    expectations = tuple(
        FaultInvariantExpectation(
            metric=metric,
            comparator=FaultComparator.EXACT,
            expected_value=0,
        )
        for metric in sorted(CORE_ZERO_METRICS, key=lambda item: item.value)
    )
    scenarios = tuple(
        FaultScenarioSpec(
            scenario_id=f"{boundary.value}-{seed[:12]}",
            boundary=boundary,
            injection_point=f"fixture:{boundary.value}",
            expected_outcome=_OUTCOME[boundary],
            required_recovery_actions=(FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
            expectations=expectations,
        )
        for boundary in FaultBoundary
    )
    manifest = FaultCampaignManifest(
        campaign_key=f"endurance-prerequisite-{seed}",
        quest_id=quest_id,
        seed=7,
        harness_code_sha256=_sha(f"{seed}:fault-harness"),
        environment_manifest_sha256=_sha(f"{seed}:fault-environment"),
        scenarios=scenarios,
        created_at=base,
    )
    observations = tuple(
        FaultScenarioObservation(
            scenario_id=spec.scenario_id,
            observed_outcome=spec.expected_outcome,
            injection_confirmed=True,
            recovery_actions=spec.required_recovery_actions,
            metrics=tuple(
                FaultMetricObservation(
                    metric=item.metric,
                    observed_value=0,
                    evidence_sha256=_sha(f"{spec.scenario_id}:{item.metric.value}"),
                )
                for item in expectations
            ),
            evidence_sha256s=(
                _sha(f"{spec.scenario_id}:recovery"),
                _sha(f"{spec.scenario_id}:diagnostic"),
                *(_sha(f"{spec.scenario_id}:{item.metric.value}") for item in expectations),
            ),
            diagnostic_sha256=_sha(f"{spec.scenario_id}:diagnostic"),
            started_at=base + timedelta(seconds=1),
            completed_at=base + timedelta(seconds=2),
        )
        for spec in scenarios
    )
    return evaluate_fault_campaign(
        manifest,
        observations,
        completed_at=base + timedelta(seconds=3),
    )


def _seed_store_prerequisites(seed: str, base: datetime):
    graph = ProgramGraphStore()
    quest = QuestSpec(
        identity_key=f"endurance-quest-{seed}",
        title="Frozen endurance Quest",
        direction="Discriminate a mechanism without hiding negative results.",
        value_boundary="Preserve evidence, budget, and stopping reasons.",
        safety_boundary=("No autonomous outward action",),
    )
    program = ResearchProgramSpec(
        quest_id=quest.node_id,
        identity_key=f"endurance-program-{seed}",
        title="Endurance Program",
        objective="Exercise multiple falsifiable branches.",
        problem_domain="synthetic",
    )
    family = ScientificFamilySpec(
        program_id=program.node_id,
        family_key=f"endurance-family-{seed[:20]}",
        title="Endurance mechanism family",
        scientific_scope="One claim tested through three independent campaign branches.",
    )
    campaigns = tuple(
        CampaignSpec(
            program_id=program.node_id,
            family_id=family.family_id,
            identity_key=f"endurance-campaign-{seed}-{index}",
            title=f"Endurance Campaign {index}",
            objective="Run a prediction-bound branch.",
        )
        for index in range(3)
    )
    graph.create_quest(quest, _ctx(seed, "quest"), now=base - timedelta(seconds=30))
    graph.create_program(program, _ctx(seed, "program"), now=base - timedelta(seconds=29))
    graph.create_scientific_family(
        family,
        _ctx(seed, "family"),
        now=base - timedelta(seconds=28),
    )
    for index, campaign in enumerate(campaigns):
        graph.create_campaign(
            campaign,
            _ctx(seed, f"campaign-{index}"),
            now=base - timedelta(seconds=27 - index),
        )
    for node_id, target, label, offset in (
        (quest.node_id, GraphNodeState.ACTIVE, "quest-active", -23),
        (program.node_id, GraphNodeState.ACTIVE, "program-active", -22),
        (campaigns[0].node_id, GraphNodeState.ACTIVE, "campaign-0-active", -21),
    ):
        graph.transition_node(
            NodeTransitionSpec(
                node_id=node_id,
                expected_version=1,
                to_state=target,
                reason="frozen prerequisite is ready",
            ),
            _ctx(seed, label),
            now=base + timedelta(seconds=offset),
        )
    graph.allocate_budget(
        BudgetAllocationSpec(
            scope_node_id=quest.node_id,
            kind=BudgetKind.USD,
            cap_microunits=10_000_000,
        ),
        _ctx(seed, "budget"),
        now=base - timedelta(seconds=20),
    )
    for index in range(2):
        run_id = create_run(f"Endurance question {index}", domain="synthetic")
        graph.bind_run(
            CampaignRunBindingSpec(campaign_id=campaigns[index].node_id, run_id=run_id),
            _ctx(seed, f"run-{index}"),
            now=base - timedelta(seconds=19 - index),
        )
        question = ResearchQuestion(
            run_id=run_id,
            question_id=new_research_question_id(),
            version=1,
            kind=ResearchQuestionKind.MECHANISM,
            statement=f"Which mechanism explains endurance branch {index}?",
            scope_sha256=_sha(f"{seed}:question-scope:{index}"),
            author_principal_sha256=_sha(f"{seed}:question-author:{index}"),
            frozen_at=base - timedelta(seconds=18 - index),
        )
        with session_scope() as session:
            session.add(
                EpistemicResearchQuestionRecord(
                    question_sha256=question.question_sha256,
                    run_id=run_id,
                    question_id=question.question_id,
                    version=question.version,
                    parent_question_sha256=None,
                    kind=question.kind.value,
                    frozen_at=question.frozen_at,
                    payload_json=question.model_dump(mode="json"),
                )
            )
        graph.bind_question(
            ProgramQuestionBindingSpec(
                program_id=program.node_id,
                question_sha256=question.question_sha256,
            ),
            _ctx(seed, f"question-{index}"),
            now=base - timedelta(seconds=16 - index),
        )
    report = _passing_fault_report(seed, quest.node_id, base - timedelta(seconds=10))
    receipt = FaultCampaignStore().commit(
        report,
        FaultCampaignCommitContext(
            idempotency_key=f"f11s7:fault:{seed[:20]}",
            principal="harness:fault",
        ),
        now=base - timedelta(seconds=6),
    )
    return graph, quest, program, campaigns, report, receipt


def _seed_portfolio_epoch(
    tmp_path: Path,
    *,
    seed: str,
    base: datetime,
    graph_store: ProgramGraphStore,
    quest: QuestSpec,
    program: ResearchProgramSpec,
) -> str:
    memory = ResearchMemoryStore(archive_root=tmp_path / f"endurance-memory-{seed}")
    goal = ResearchMemoryFactSpec(
        scope_node_id=quest.node_id,
        kind=MemoryFactKind.GOAL,
        statement="Prefer the branch that most efficiently covers the frozen questions.",
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
                source_id=f"portfolio-goal-{seed}",
                sha256=_sha(f"{seed}:portfolio-goal-source"),
                uri=f"fixture://{seed}/portfolio-goal",
            ),
        ),
    )
    memory.register_fact(goal, _ctx(seed, "portfolio-goal"), now=base + timedelta(seconds=11))
    memory.compact(
        scope_node_id=quest.node_id,
        task_key="portfolio-plan",
        draft=MemorySummaryDraft(
            producer_provider="fixture",
            producer_model="summary-model",
            prompt_sha256=_sha(f"{seed}:portfolio-summary-prompt"),
            summary_text="The frozen Quest needs a budget-respecting final portfolio.",
            covered_fact_ids=(goal.fact_id,),
        ),
        context=_ctx(seed, "portfolio-compact"),
        now=base + timedelta(seconds=12),
    )
    context_receipt = memory.build_task_context(
        TaskContextRequest(
            scope_node_id=quest.node_id,
            task_key="portfolio-plan",
            consumer_provider="fixture-provider",
            consumer_model="portfolio-proposer",
        ),
        _ctx(seed, "portfolio-context"),
        now=base + timedelta(seconds=13),
    )
    graph = graph_store.get_quest(quest.node_id)
    actions = (
        PortfolioActionSpec(
            action_type=PortfolioActionType.REPAIR_CAPABILITY,
            target_node_id=program.node_id,
            task_key="program:repair-measurement",
            title="Repair the measurement adapter",
            rationale="Reduce execution risk across the frozen branches.",
        ),
        PortfolioActionSpec(
            action_type=PortfolioActionType.PAUSE_PROGRAM,
            target_node_id=program.node_id,
            task_key="program:pause-review",
            title="Pause for evidence review",
            rationale="Preserve a bounded alternative when evidence becomes inconsistent.",
        ),
    )
    proposal = PortfolioProposal(
        quest_id=quest.node_id,
        graph_sha256=graph.graph_sha256,
        memory_context_receipt_id=context_receipt.context_receipt_id,
        proposer_principal="model:portfolio-proposer",
        proposer_provider="fixture-provider",
        proposer_model="portfolio-proposer",
        proposer_model_identity_sha256=_sha(f"{seed}:portfolio-model"),
        prompt_sha256=_sha(f"{seed}:portfolio-prompt"),
        candidates=actions,
        generated_at=base + timedelta(seconds=15),
    )
    assessments = tuple(
        PortfolioCandidateAssessment(
            candidate_id=action.candidate_id,
            action_sha256=action.action_sha256,
            estimated_costs=(),
            estimated_duration_seconds=60,
            risk_level=PortfolioRiskLevel.LOW,
            measurement_status=PortfolioMeasurementStatus.VALIDATED,
            measurement_evidence_sha256=_sha(f"{seed}:{action.candidate_id}:measurement"),
            required_capability_sha256s=(),
            available_capability_sha256s=(),
            required_data_roles=(),
            information_model=None,
            importance_ppm=800_000,
            novelty_ppm=500_000,
            success_probability_ppm=800_000,
            value_evidence_sha256=_sha(f"{seed}:{action.candidate_id}:value"),
            replication_debt_ledger_sha256=_sha(f"{seed}:{action.candidate_id}:replication"),
            replication_debt_before=0,
            expected_replication_debt_reduction=0,
            correlation_tags=(f"correlation-{action.action_type.value}",),
            diversity_tags=(f"diversity-{action.action_type.value}",),
            assessment_evidence_sha256s=(_sha(f"{seed}:{action.candidate_id}:assessment"),),
            completed_at=base + timedelta(seconds=16),
        )
        for action in actions
    )
    slate = PortfolioSlateSpec(
        policy=PortfolioSelectionPolicy(
            policy_id=f"endurance-policy-{seed[:20]}",
            quest_id=quest.node_id,
            selector_code_sha256=PORTFOLIO_SELECTOR_CODE_SHA256,
            frozen_at=base + timedelta(seconds=14),
        ),
        proposal=proposal,
        assessment_batch=PortfolioAssessmentBatch(
            manifest=PortfolioAssessmentManifest(
                assessor_principal="harness:portfolio-assessor",
                assessor_kind=PortfolioAssessorKind.DETERMINISTIC_HARNESS,
                assessor_code_sha256=_sha(f"{seed}:portfolio-assessor"),
                output_schema_sha256=PORTFOLIO_ASSESSMENT_OUTPUT_SCHEMA_SHA256,
                frozen_at=base + timedelta(seconds=15),
            ),
            assessments=assessments,
            completed_at=base + timedelta(seconds=16),
        ),
    )
    store = ResearchPortfolioStore()
    store._memory = memory
    store.register_slate(
        slate,
        _ctx(seed, "portfolio-register", "controller:portfolio"),
        now=base + timedelta(seconds=18),
    )
    store.commit_human_plan(
        slate_id=slate.slate_id,
        plan=HumanPortfolioPlanSpec(
            selected_candidate_ids=(),
            rationale="Human baseline abstains before seeing planner scores.",
            issued_at=base + timedelta(seconds=19),
        ),
        context=_ctx(seed, "portfolio-human", "human:portfolio-reviewer"),
        now=base + timedelta(seconds=19),
    )
    receipt = store.evaluate_slate(
        slate_id=slate.slate_id,
        context=_ctx(seed, "portfolio-evaluate", "harness:portfolio"),
        now=base + timedelta(seconds=25),
    )
    return receipt.object_id


def _seed_in_window_evidence(
    tmp_path: Path,
    *,
    seed: str,
    base: datetime,
    graph: ProgramGraphStore,
    quest: QuestSpec,
    program: ResearchProgramSpec,
    campaigns: tuple[CampaignSpec, ...],
):
    memory = ResearchMemoryStore(archive_root=tmp_path / f"pivot-memory-{seed}")
    negative = ResearchMemoryFactSpec(
        scope_node_id=campaigns[0].node_id,
        kind=MemoryFactKind.NEGATIVE_RESULT,
        statement="The original prediction failed under its locked analysis.",
        detail={"prediction_status": "refuted", "branch": campaigns[0].node_id},
        task_bindings=(
            MemoryTaskBindingSpec(
                task_key="pivot-analysis",
                context_role=MemoryContextRole.REQUIRED,
            ),
        ),
        sources=(
            MemorySourceRef(
                kind=MemorySourceKind.ARTIFACT,
                source_id=f"negative-result-{seed}",
                sha256=_sha(f"{seed}:negative-result-source"),
                uri=f"fixture://{seed}/negative-result",
            ),
        ),
    )
    memory.register_fact(
        negative,
        _ctx(seed, "negative-result", "scientist:branch-zero"),
        now=base + timedelta(seconds=5),
    )
    source_transition = graph.transition_node(
        NodeTransitionSpec(
            node_id=campaigns[0].node_id,
            expected_version=2,
            to_state=GraphNodeState.STOPPED,
            reason="Locked prediction was refuted by the negative result.",
        ),
        _ctx(seed, "source-stop", "scientist:branch-zero"),
        now=base + timedelta(seconds=6),
    )
    successor_transition = graph.transition_node(
        NodeTransitionSpec(
            node_id=campaigns[1].node_id,
            expected_version=1,
            to_state=GraphNodeState.ACTIVE,
            reason="Activate a branch with a changed discriminating prediction.",
        ),
        _ctx(seed, "successor-active", "scientist:branch-one"),
        now=base + timedelta(seconds=7),
    )
    pivot_transition_ids = {
        str(source_transition.command.result["transition_id"]),
        str(successor_transition.command.result["transition_id"]),
    }
    pivot_transition_at = max(
        item.created_at
        for item in graph.get_quest(quest.node_id).transitions
        if item.transition_id in pivot_transition_ids
    )
    pivot_occurred_at = max(
        base + timedelta(seconds=8),
        pivot_transition_at + timedelta(milliseconds=1),
    )
    live_fault = _passing_fault_report(
        f"{seed}-live",
        quest.node_id,
        base + timedelta(seconds=1),
    )
    live_fault_receipt = FaultCampaignStore().commit(
        live_fault,
        FaultCampaignCommitContext(
            idempotency_key=f"f11s7:live-fault:{seed[:20]}",
            principal="harness:fault-live",
        ),
        now=base + timedelta(seconds=5),
    )
    process_result = next(
        item
        for item in live_fault.results
        if next(
            spec for spec in live_fault.manifest.scenarios if spec.scenario_id == item.scenario_id
        ).boundary
        is FaultBoundary.API_PROCESS
    )
    provider_result = next(
        item
        for item in live_fault.results
        if next(
            spec for spec in live_fault.manifest.scenarios if spec.scenario_id == item.scenario_id
        ).boundary
        is FaultBoundary.PROVIDER
    )
    assert live_fault.report_sha256 is not None
    assert live_fault_receipt.report_sha256 == live_fault.report_sha256
    reproductions = (
        EnduranceReproductionReceipt(
            original_campaign_id=campaigns[0].node_id,
            reproduction_campaign_id=campaigns[2].node_id,
            protocol_sha256=_sha(f"{seed}:reproduction-protocol"),
            original_result_sha256=_sha(f"{seed}:original-result"),
            reproduction_result_sha256=_sha(f"{seed}:reproduction-result"),
            conclusion=EnduranceReproductionConclusion.CONTRADICTED,
            evidence_sha256s=(_sha(f"{seed}:reproduction-evidence"),),
            validated_by="harness:reproduction",
            completed_at=base + timedelta(seconds=8),
        ),
    )
    interruptions = tuple(
        sorted(
            (
                EnduranceInterruptionReceipt(
                    kind=EnduranceInterruptionKind.PROCESS_KILL,
                    fault_campaign_id=live_fault_receipt.campaign_id,
                    fault_report_sha256=live_fault.report_sha256,
                    scenario_id=process_result.scenario_id,
                    recovery_evidence_sha256s=(process_result.observation.evidence_sha256s[0],),
                    occurred_at=process_result.observation.completed_at,
                ),
                EnduranceInterruptionReceipt(
                    kind=EnduranceInterruptionKind.PROVIDER_TRANSPORT,
                    fault_campaign_id=live_fault_receipt.campaign_id,
                    fault_report_sha256=live_fault.report_sha256,
                    scenario_id=provider_result.scenario_id,
                    recovery_evidence_sha256s=(provider_result.observation.evidence_sha256s[0],),
                    occurred_at=provider_result.observation.completed_at,
                ),
            ),
            key=lambda item: item.receipt_id,
        )
    )
    before = EnduranceStrategyFingerprint(
        hypothesis_semantics_sha256=_sha(f"{seed}:before-hypothesis"),
        prediction_pattern_sha256=_sha(f"{seed}:before-prediction"),
        capability_input_sha256=_sha(f"{seed}:shared-capability"),
        analysis_plan_sha256=_sha(f"{seed}:before-analysis"),
        discriminated_pairs_sha256=_sha(f"{seed}:before-pairs"),
    )
    pivot = EnduranceStructuralPivotReceipt(
        negative_result_fact_id=negative.fact_id,
        source_campaign_id=campaigns[0].node_id,
        successor_campaign_id=campaigns[1].node_id,
        source_transition_id=str(source_transition.command.result["transition_id"]),
        successor_transition_id=str(successor_transition.command.result["transition_id"]),
        before=before,
        after=before.model_copy(
            update={
                "prediction_pattern_sha256": _sha(f"{seed}:after-prediction"),
                "discriminated_pairs_sha256": _sha(f"{seed}:after-pairs"),
            }
        ),
        assessor_code_sha256=_sha(f"{seed}:pivot-assessor"),
        assessed_by="harness:pivot",
        evidence_sha256s=(_sha(f"{seed}:pivot-evidence"),),
        occurred_at=pivot_occurred_at,
    )
    epoch_id = _seed_portfolio_epoch(
        tmp_path,
        seed=seed,
        base=base,
        graph_store=graph,
        quest=quest,
        program=program,
    )
    return (
        live_fault_receipt,
        EnduranceCheckpointEvidence(
            reproductions=reproductions,
            interruptions=interruptions,
            structural_pivots=(pivot,),
        ),
        epoch_id,
    )


def _pure_graph(base: datetime) -> QuestGraphSnapshot:
    quest_id = "qst_" + "1" * 32
    program_id = "prg_" + "2" * 32
    campaigns = tuple("cmp_" + value * 32 for value in ("3", "4", "5"))
    nodes = (
        ResearchNodeSnapshot(
            node_id=quest_id,
            quest_id=quest_id,
            parent_node_id=None,
            node_type="quest",
            identity_key="pure-quest",
            spec_sha256=_sha("pure-quest-spec"),
            spec={"direction": "frozen"},
            state="active",
            state_version=1,
            created_by="fixture",
            created_at=base,
            updated_at=base,
        ),
        ResearchNodeSnapshot(
            node_id=program_id,
            quest_id=quest_id,
            parent_node_id=quest_id,
            node_type="program",
            identity_key="pure-program",
            spec_sha256=_sha("pure-program-spec"),
            spec={"objective": "test"},
            state="active",
            state_version=1,
            created_by="fixture",
            created_at=base,
            updated_at=base,
        ),
        *(
            ResearchNodeSnapshot(
                node_id=campaign_id,
                quest_id=quest_id,
                parent_node_id=program_id,
                node_type="campaign",
                identity_key=f"pure-campaign-{index}",
                spec_sha256=_sha(f"pure-campaign-{index}"),
                spec={"objective": "test"},
                state="active",
                state_version=1,
                created_by="fixture",
                created_at=base,
                updated_at=base,
            )
            for index, campaign_id in enumerate(campaigns)
        ),
    )
    transitions = tuple(
        ResearchTransitionSnapshot(
            transition_id=f"transition-{index}",
            node_id=campaign_id,
            command_id=f"command-{index}",
            from_state=None,
            to_state="active",
            from_version=0,
            to_version=1,
            reason="branch remains active with an explicit reason",
            principal="fixture",
            created_at=base,
        )
        for index, campaign_id in enumerate(campaigns)
    )
    questions = tuple(_sha(f"pure-question-{index}") for index in range(2))
    bindings = tuple(
        ExternalBindingSnapshot(
            binding_id=f"binding-{index}",
            binding_type="research_question",
            scope_node_id=program_id,
            external_id=value,
            role="primary",
            command_id=f"question-command-{index}",
            created_at=base,
        )
        for index, value in enumerate(questions)
    )
    budget = BudgetAllocationSnapshot(
        allocation_id="bga_" + "6" * 32,
        scope_node_id=quest_id,
        parent_allocation_id=None,
        kind="usd",
        cap_microunits=1_000_000,
        policy_sha256=_sha("pure-budget-policy"),
        policy={},
        command_id="budget-command",
        created_at=base,
    )
    payload = {
        "schema_version": 1,
        "quest_id": quest_id,
        "nodes": nodes,
        "transitions": transitions,
        "dependencies": (),
        "scientific_families": (),
        "campaign_families": (),
        "external_bindings": bindings,
        "data_allocations": (),
        "budget_allocations": (budget,),
        "rebuilt_at": None,
    }
    projection = {
        key: [item.model_dump(mode="json") for item in value] if isinstance(value, tuple) else value
        for key, value in payload.items()
        if key != "rebuilt_at"
    }
    return QuestGraphSnapshot(**payload, graph_sha256=content_sha256(projection))


def _pure_passing_report(base: datetime) -> EnduranceGateReport:
    graph = _pure_graph(base)
    questions = tuple(
        item.external_id
        for item in graph.external_bindings
        if item.binding_type == "research_question"
    )
    campaigns = tuple(
        sorted(item.node_id for item in graph.nodes if item.node_type.value == "campaign")
    )
    manifest = EnduranceGateManifest(
        gate_key="pure-accelerated",
        quest_id=graph.quest_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=30,
        checkpoint_interval_seconds=10,
        maximum_checkpoint_gap_seconds=10,
        frozen_quest_spec_sha256=next(
            item.spec_sha256 for item in graph.nodes if item.node_id == graph.quest_id
        ),
        initial_graph_sha256=graph.graph_sha256,
        frozen_question_sha256s=tuple(sorted(questions)),
        initial_campaign_ids=campaigns,
        frozen_budget_manifest_sha256=_sha("pure-budget-manifest"),
        frozen_data_role_manifest_sha256=_sha("pure-data-manifest"),
        prerequisite_fault_campaign_id="fic_" + "7" * 32,
        prerequisite_fault_report_sha256=_sha("pure-fault-report"),
        harness_code_sha256=_sha("pure-harness"),
        environment_manifest_sha256=_sha("pure-environment"),
    )
    budget = (
        EnduranceBudgetState(
            allocation_id="bga_" + "6" * 32,
            scope_node_id=graph.quest_id,
            kind="usd",
            cap_microunits=1_000_000,
            spent_microunits=100_000,
            available_microunits=900_000,
        ),
    )

    def observation(seconds: int) -> EnduranceLedgerObservation:
        return EnduranceLedgerObservation(
            quest_spec_sha256=manifest.frozen_quest_spec_sha256,
            graph_sha256=graph.graph_sha256,
            question_sha256s=manifest.frozen_question_sha256s,
            campaign_ids=campaigns,
            negative_result_fact_ids=("mem_" + "8" * 32,),
            portfolio_epoch_ids=("pep_" + "9" * 32,),
            budget_state=budget,
            one_time_action_count=0,
            one_time_action_receipt_count=0,
            reconciliation_required_count=0,
            scientific_state_loss_count=0,
            duplicate_scientific_state_count=0,
            duplicate_budget_charge_count=0,
            duplicate_outward_action_count=0,
            unresolved_ambiguity_without_block_count=0,
            event_state_mismatch_count=0,
            observed_at=base + timedelta(seconds=seconds),
        )

    reproduction = EnduranceReproductionReceipt(
        original_campaign_id=campaigns[0],
        reproduction_campaign_id=campaigns[1],
        protocol_sha256=_sha("pure-reproduction-protocol"),
        original_result_sha256=_sha("pure-original-result"),
        reproduction_result_sha256=_sha("pure-reproduction-result"),
        conclusion=EnduranceReproductionConclusion.CONFIRMED,
        evidence_sha256s=(_sha("pure-reproduction-evidence"),),
        validated_by="harness:reproduction",
        completed_at=base + timedelta(seconds=5),
    )
    process = EnduranceInterruptionReceipt(
        kind=EnduranceInterruptionKind.PROCESS_KILL,
        fault_campaign_id="fic_" + "a" * 32,
        fault_report_sha256=_sha("pure-process-report"),
        scenario_id="process-kill",
        recovery_evidence_sha256s=(_sha("pure-process-recovery"),),
        occurred_at=base + timedelta(seconds=6),
    )
    provider = EnduranceInterruptionReceipt(
        kind=EnduranceInterruptionKind.PROVIDER_TRANSPORT,
        fault_campaign_id="fic_" + "b" * 32,
        fault_report_sha256=_sha("pure-provider-report"),
        scenario_id="provider-interruption",
        recovery_evidence_sha256s=(_sha("pure-provider-recovery"),),
        occurred_at=base + timedelta(seconds=7),
    )
    before = EnduranceStrategyFingerprint(
        hypothesis_semantics_sha256=_sha("before-hypothesis"),
        prediction_pattern_sha256=_sha("before-prediction"),
        capability_input_sha256=_sha("shared-capability"),
        analysis_plan_sha256=_sha("before-analysis"),
        discriminated_pairs_sha256=_sha("before-pairs"),
    )
    after = before.model_copy(
        update={
            "prediction_pattern_sha256": _sha("after-prediction"),
            "analysis_plan_sha256": _sha("after-analysis"),
        }
    )
    pivot = EnduranceStructuralPivotReceipt(
        negative_result_fact_id="mem_" + "8" * 32,
        source_campaign_id=campaigns[0],
        successor_campaign_id=campaigns[2],
        source_transition_id="source-stop",
        successor_transition_id="successor-start",
        before=before,
        after=after,
        assessor_code_sha256=_sha("pure-pivot-assessor"),
        assessed_by="harness:pivot",
        evidence_sha256s=(_sha("pure-pivot-evidence"),),
        occurred_at=base + timedelta(seconds=8),
    )
    first = EnduranceCheckpoint(
        gate_id=manifest.gate_id,
        sequence=1,
        parent_sha256=manifest.manifest_sha256,
        observation=observation(10),
        evidence=EnduranceCheckpointEvidence(
            reproductions=(reproduction,),
            interruptions=tuple(sorted((process, provider), key=lambda item: item.receipt_id)),
            structural_pivots=(pivot,),
        ),
    )
    second = EnduranceCheckpoint(
        gate_id=manifest.gate_id,
        sequence=2,
        parent_sha256=first.checkpoint_sha256,
        observation=observation(20),
    )
    efficiency = EnduranceEfficiencyReceipt(
        metric=EnduranceEfficiencyMetric.QUESTION_COVERAGE,
        baseline_value_units=10,
        baseline_cost_microunits=100,
        endurance_value_units=20,
        endurance_cost_microunits=100,
        improvement_ppm=1_000_000,
        evidence_sha256s=(_sha("pure-efficiency-evidence"),),
        assessor_code_sha256=_sha("pure-efficiency-assessor"),
        assessed_by="harness:efficiency",
        assessed_at=base + timedelta(seconds=25),
    )
    return evaluate_endurance_gate(
        manifest=manifest,
        started_at=base,
        completed_at=base + timedelta(seconds=30),
        checkpoints=(first, second),
        final_observation=observation(30),
        final_graph=graph,
        efficiency=efficiency,
    )


def test_contract_separates_accelerated_engineering_from_real_72h() -> None:
    base = datetime(2026, 8, 18, tzinfo=timezone.utc)
    report = _pure_passing_report(base)
    assert report.disposition is EnduranceGateDisposition.PASSED
    assert report.real_72h_passed is False
    assert report.eligible_for_f11_scientific_exit_review is False
    assert report.autonomous_allocation_enabled is False
    with pytest.raises(ValidationError, match="real 72-hour verdict"):
        EnduranceGateReport.model_validate(
            {
                **report.model_dump(mode="json"),
                "real_72h_passed": True,
                "eligible_for_f11_scientific_exit_review": True,
                "report_sha256": None,
            }
        )
    with pytest.raises(ValidationError, match="at least 72 hours"):
        EnduranceGateManifest(
            **{
                **report.manifest.model_dump(mode="python", exclude={"gate_id"}),
                "evidence_class": EnduranceEvidenceClass.REAL_TIME_72H,
                "required_duration_seconds": REAL_72H_SECONDS - 1,
            }
        )


def test_cosmetic_pivot_cannot_satisfy_structural_evidence() -> None:
    base = datetime(2026, 8, 18, tzinfo=timezone.utc)
    before = EnduranceStrategyFingerprint(
        hypothesis_semantics_sha256=_sha("same-hypothesis"),
        prediction_pattern_sha256=_sha("same-prediction"),
        capability_input_sha256=_sha("same-capability"),
        analysis_plan_sha256=_sha("before-wording"),
        discriminated_pairs_sha256=_sha("same-pairs"),
    )
    with pytest.raises(ValidationError, match="predictions or discriminated"):
        EnduranceStructuralPivotReceipt(
            negative_result_fact_id="mem_" + "1" * 32,
            source_campaign_id="cmp_" + "2" * 32,
            successor_campaign_id="cmp_" + "3" * 32,
            source_transition_id="source-stop",
            successor_transition_id="successor-start",
            before=before,
            after=before.model_copy(update={"analysis_plan_sha256": _sha("after-wording")}),
            assessor_code_sha256=_sha("pivot-assessor"),
            assessed_by="harness:pivot",
            evidence_sha256s=(_sha("pivot-evidence"),),
            occurred_at=base,
        )


def test_store_resumes_checkpoint_chain_and_retains_blocked_report() -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    _, quest, _, _, _, fault_receipt = _seed_store_prerequisites(seed, base)
    manifest = prepare_endurance_gate_manifest(
        gate_key=f"accelerated-{seed}",
        quest_id=quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=30,
        checkpoint_interval_seconds=10,
        maximum_checkpoint_gap_seconds=10,
        prerequisite_fault_campaign_id=fault_receipt.campaign_id,
        harness_code_sha256=_sha(f"{seed}:endurance-harness"),
        environment_manifest_sha256=_sha(f"{seed}:endurance-environment"),
    )
    store = ResearchEnduranceStore()
    started = store.start(
        manifest,
        _endurance_context(seed, "start"),
        now=base,
    )
    first_context = _endurance_context(seed, "checkpoint-1")
    first = store.append_checkpoint(
        started.object_id,
        EnduranceCheckpointEvidence(),
        first_context,
        now=base + timedelta(seconds=10),
    )
    replay = store.append_checkpoint(
        started.object_id,
        EnduranceCheckpointEvidence(),
        first_context,
        now=base + timedelta(days=1),
    )
    assert replay.created is False
    assert replay.object_id == first.object_id

    # A fresh process/store resumes from the persisted parent rather than resetting sequence/time.
    resumed = ResearchEnduranceStore()
    resumed.append_checkpoint(
        started.object_id,
        EnduranceCheckpointEvidence(),
        _endurance_context(seed, "checkpoint-2"),
        now=base + timedelta(seconds=20),
    )
    efficiency = EnduranceEfficiencyReceipt(
        metric=EnduranceEfficiencyMetric.QUESTION_COVERAGE,
        baseline_value_units=10,
        baseline_cost_microunits=100,
        endurance_value_units=20,
        endurance_cost_microunits=100,
        improvement_ppm=1_000_000,
        evidence_sha256s=(_sha(f"{seed}:efficiency"),),
        assessor_code_sha256=_sha(f"{seed}:efficiency-code"),
        assessed_by="harness:efficiency",
        assessed_at=base + timedelta(seconds=25),
    )
    resumed.finalize(
        started.object_id,
        _endurance_context(seed, "finalize"),
        efficiency=efficiency,
        now=base + timedelta(seconds=30),
    )
    snapshot = resumed.get(started.object_id)
    assert [item.checkpoint.sequence for item in snapshot.checkpoints] == [1, 2]
    assert snapshot.report is not None
    assert snapshot.report.disposition is EnduranceGateDisposition.BLOCKED
    assert "negative_results:minimum_not_met:0/1" in snapshot.report.blockers
    assert snapshot.report.real_72h_passed is False
    assert resumed.audit(quest.node_id).eligible_for_f11_scientific_exit_review is False

    with pytest.raises(DBAPIError, match="append-only"):
        with engine().begin() as connection:
            connection.execute(
                update(ResearchEnduranceGateRecord)
                .where(ResearchEnduranceGateRecord.gate_id == started.object_id)
                .values(initial_graph_sha256="0" * 64)
            )


def test_accelerated_end_to_end_acceptance_passes_without_claiming_72h(tmp_path: Path) -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(seconds=5)
    graph, quest, program, campaigns, _, _ = _seed_store_prerequisites(seed, base)
    live_fault, evidence, epoch_id = _seed_in_window_evidence(
        tmp_path,
        seed=seed,
        base=base,
        graph=graph,
        quest=quest,
        program=program,
        campaigns=campaigns,
    )
    # Graph transitions intentionally linearize with PostgreSQL time.  Derive the synthetic
    # checkpoint clock after seeding so a slow CI worker cannot move the pivot beyond the first
    # observation, while retaining a bounded maximum gap and a 30-second logical run.
    first_checkpoint_at = max(
        base + timedelta(seconds=10),
        evidence.structural_pivots[0].occurred_at + timedelta(milliseconds=1),
    )
    second_checkpoint_at = first_checkpoint_at + timedelta(seconds=10)
    completed_at = second_checkpoint_at + timedelta(seconds=10)
    manifest = prepare_endurance_gate_manifest(
        gate_key=f"accelerated-complete-{seed}",
        quest_id=quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=30,
        checkpoint_interval_seconds=10,
        maximum_checkpoint_gap_seconds=300,
        prerequisite_fault_campaign_id=live_fault.campaign_id,
        harness_code_sha256=_sha(f"{seed}:complete-harness"),
        environment_manifest_sha256=_sha(f"{seed}:complete-environment"),
    )
    store = ResearchEnduranceStore()
    gate = store.start(manifest, _endurance_context(seed, "complete-start"), now=base)
    store.append_checkpoint(
        gate.object_id,
        evidence,
        _endurance_context(seed, "complete-checkpoint-1"),
        now=first_checkpoint_at,
    )
    store.append_checkpoint(
        gate.object_id,
        EnduranceCheckpointEvidence(),
        _endurance_context(seed, "complete-checkpoint-2"),
        now=second_checkpoint_at,
    )
    store.finalize(
        gate.object_id,
        _endurance_context(seed, "complete-finalize"),
        efficiency=EnduranceEfficiencyReceipt(
            metric=EnduranceEfficiencyMetric.QUESTION_COVERAGE,
            baseline_value_units=10,
            baseline_cost_microunits=100,
            endurance_value_units=20,
            endurance_cost_microunits=100,
            improvement_ppm=1_000_000,
            evidence_sha256s=(_sha(f"{seed}:complete-efficiency"),),
            assessor_code_sha256=_sha(f"{seed}:complete-efficiency-code"),
            assessed_by="harness:efficiency",
            assessed_at=completed_at - timedelta(seconds=4),
        ),
        now=completed_at,
    )
    report = store.get(gate.object_id).report
    assert report is not None
    assert report.disposition is EnduranceGateDisposition.PASSED
    assert report.blockers == ()
    assert report.negative_result_count == 1
    assert report.reproduction_count == 1
    assert report.process_kill_count == 1
    assert report.provider_interruption_count == 1
    assert report.structural_pivot_count == 1
    assert report.portfolio_epoch_count == 1
    assert report.final_portfolio.portfolio_epoch_ids == (epoch_id,)
    assert report.real_72h_passed is False
    assert report.eligible_for_f11_scientific_exit_review is False
    audit = store.audit(quest.node_id)
    assert audit.latest_disposition is EnduranceGateDisposition.PASSED
    assert audit.latest_real_72h_passed is False
    assert audit.eligible_for_f11_scientific_exit_review is False


def test_real_time_store_rejects_clock_override_before_start(monkeypatch) -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    _, quest, _, _, _, fault_receipt = _seed_store_prerequisites(seed, base)
    manifest = prepare_endurance_gate_manifest(
        gate_key=f"real-{seed}",
        quest_id=quest.node_id,
        evidence_class=EnduranceEvidenceClass.REAL_TIME_72H,
        required_duration_seconds=REAL_72H_SECONDS,
        checkpoint_interval_seconds=60 * 60,
        maximum_checkpoint_gap_seconds=2 * 60 * 60,
        prerequisite_fault_campaign_id=fault_receipt.campaign_id,
        harness_code_sha256=_sha(f"{seed}:real-harness"),
        environment_manifest_sha256=_sha(f"{seed}:real-environment"),
    )
    with pytest.raises(ResearchEnduranceConflict, match="caller-supplied clocks"):
        ResearchEnduranceStore().start(
            manifest,
            _endurance_context(seed, "real-start"),
            now=base,
        )
    store = ResearchEnduranceStore()
    gate = store.start(manifest, _endurance_context(seed, "real-start-database-clock"))
    original_observe = ResearchEnduranceStore._observe

    def delayed_observe(*args, **kwargs):
        observation = original_observe(*args, **kwargs)
        args[0].execute(text("SELECT pg_sleep(5.1)"))
        return observation

    monkeypatch.setattr(ResearchEnduranceStore, "_observe", staticmethod(delayed_observe))
    store.finalize(
        gate.object_id,
        _endurance_context(seed, "real-finalize-too-early"),
        efficiency=None,
    )
    report = store.get(gate.object_id).report
    assert report is not None
    assert report.disposition is EnduranceGateDisposition.BLOCKED
    assert report.elapsed_seconds < REAL_72H_SECONDS
    assert report.real_72h_passed is False


def test_endurance_migration_matches_orm_and_guards_exist() -> None:
    with engine().connect() as connection:
        assert {
            "research_endurance_gates",
            "research_endurance_checkpoints",
            "research_endurance_reports",
        }.issubset(inspect(connection).get_table_names())
        assert schema_diffs(connection) == []
        trigger_names = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'trg_research_endurance_%'"
                )
            ).scalars()
        )
        function_bodies = dict(
            connection.execute(
                text(
                    "SELECT proname, pg_get_functiondef(oid) FROM pg_proc "
                    "WHERE proname LIKE 'aletheia%research_endurance%'"
                )
            ).all()
        )
    assert "trg_research_endurance_gate_guard" in trigger_names
    assert "trg_research_endurance_checkpoint_guard" in trigger_names
    assert "trg_research_endurance_report_guard" in trigger_names
    assert "trg_research_endurance_checkpoints_append_only" in trigger_names
    assert (
        "command_input->'manifest'" in function_bodies["aletheia_validate_research_endurance_gate"]
    )
    assert (
        "NEW.process_kill_count"
        in function_bodies["aletheia_validate_research_endurance_checkpoint"]
    )
    assert (
        "NEW.provider_interruption_count"
        in function_bodies["aletheia_validate_research_endurance_checkpoint"]
    )
    assert (
        "COALESCE(command_input->'efficiency', 'null'::jsonb)"
        in function_bodies["aletheia_validate_research_endurance_report"]
    )
    for function_name, timestamp_field in (
        ("aletheia_validate_research_endurance_gate", "started_at"),
        ("aletheia_validate_research_endurance_checkpoint", "observed_at"),
        ("aletheia_validate_research_endurance_report", "completed_at"),
    ):
        body = function_bodies[function_name]
        assert f"NEW.{timestamp_field} IS DISTINCT FROM transaction_timestamp()" in body
        assert f"clock_timestamp() - NEW.{timestamp_field}" not in body
