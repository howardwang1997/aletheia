from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.db import REPO_ROOT, create_all
from aletheia.domains.materials.phonon_commissioning import (
    StructureSignalEvidenceReceipt,
    apply_phonon_quest_commissioning,
    build_phonon_quest_commissioning_manifest,
    local_artifact_identity,
)
from aletheia.domains.materials.phonon_endurance_portfolio import (
    PhononBlindPortfolioSelection,
    PhononEndurancePortfolioConflict,
    commit_phonon_blind_portfolio_plan,
    evaluate_phonon_endurance_portfolio,
    preflight_phonon_portfolio_start,
    prepare_phonon_endurance_portfolio_work_order,
    stage_phonon_endurance_portfolio,
)
from aletheia.domains.materials.phonon_negative_pivot import (
    PhononNegativePivotNotApplicable,
    execute_phonon_negative_result_pivot,
    preflight_phonon_negative_pivot_start,
    prepare_phonon_negative_pivot_work_order,
)
from aletheia.domains.materials.phonon_portfolio_efficiency import (
    assess_phonon_portfolio_efficiency,
    preflight_phonon_portfolio_efficiency_start,
    prepare_phonon_portfolio_efficiency_work_order,
    verify_phonon_portfolio_efficiency_assessment,
    verify_phonon_portfolio_efficiency_work_order,
)
from aletheia.domains.materials.phonon_reproduction import (
    IndependentExtraTreesPolicy,
    PhononIndependentReplayProtocol,
    PhononReplayArtifact,
    PhononReplayCommitReceipt,
    capture_phonon_replay_code_identity,
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
from aletheia.programs import (
    EnduranceCheckpointEvidence,
    EnduranceEvidenceClass,
    EnduranceReproductionConclusion,
    EnduranceReproductionReceipt,
    GraphCommandContext,
    GraphNodeState,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryTaskBindingSpec,
    NodeTransitionSpec,
    PortfolioActionType,
    ProgramGraphStore,
    ResearchEnduranceStore,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
    prepare_endurance_controller_manifest,
    prepare_endurance_gate_manifest,
    run_controller_tick,
    start_endurance_controller_gate,
    submit_controller_evidence,
)

_PACKAGES = ("matminer", "numpy", "pandas", "pymatgen", "scikit-learn", "spglib")

_OUTCOMES = {
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


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _json_bytes(value: object) -> bytes:
    assert hasattr(value, "model_dump")
    payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
            expected_outcome=_OUTCOMES[boundary],
            required_recovery_actions=(FaultRecoveryAction.REPLAY_EXACT_COMMAND,),
            expectations=expectations,
        )
        for boundary in FaultBoundary
    )
    observations = tuple(
        FaultScenarioObservation(
            scenario_id=spec.scenario_id,
            observed_outcome=spec.expected_outcome,
            injection_confirmed=True,
            recovery_actions=spec.required_recovery_actions,
            metrics=tuple(
                FaultMetricObservation(
                    metric=expectation.metric,
                    observed_value=0,
                    evidence_sha256=_digest(
                        f"{spec.scenario_id}:{expectation.metric.value}"
                    ),
                )
                for expectation in expectations
            ),
            evidence_sha256s=(
                _digest(f"{spec.scenario_id}:recovery"),
                _digest(f"{spec.scenario_id}:diagnostic"),
                *(
                    _digest(f"{spec.scenario_id}:{expectation.metric.value}")
                    for expectation in expectations
                ),
            ),
            diagnostic_sha256=_digest(f"{spec.scenario_id}:diagnostic"),
            started_at=base + timedelta(seconds=1),
            completed_at=base + timedelta(seconds=2),
        )
        for spec in scenarios
    )
    return evaluate_fault_campaign(
        FaultCampaignManifest(
            campaign_key=f"portfolio-prerequisite-{seed}",
            quest_id=quest_id,
            seed=7,
            harness_code_sha256=_digest(f"{seed}:fault-harness"),
            environment_manifest_sha256=_digest(f"{seed}:fault-environment"),
            scenarios=scenarios,
            created_at=base,
        ),
        observations,
        completed_at=base + timedelta(seconds=3),
    )


def _commissioning(seed: str):
    evidence = StructureSignalEvidenceReceipt(
        dataset_file=local_artifact_identity(REPO_ROOT / "pyproject.toml"),
        plan_file=local_artifact_identity(REPO_ROOT / "environment.yml"),
        result_file=local_artifact_identity(REPO_ROOT / "README.md"),
        dataset_ref="test_phonons",
        source_uri="https://example.invalid/test-phonons",
        license_expression="CC0-1.0",
        license_uri="https://example.invalid/license",
        structure_column="structure",
        target_column="last phdos peak",
        target_quantity_kind_id="phonon.last_phdos_peak_frequency",
        target_unit_ucum="cm-1",
        row_count=1265,
        protocol_sha256=_digest(f"{seed}:source-protocol"),
        implementation_sha256=_digest(f"{seed}:source-implementation"),
        plan_sha256=_digest(f"{seed}:source-plan"),
        dataset_receipt_sha256=_digest(f"{seed}:source-dataset"),
        result_sha256=_digest(f"{seed}:source-result"),
        result_disposition="robust_aligned_structure_signal",
        result_completed_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
    )
    return build_phonon_quest_commissioning_manifest(
        evidence,
        prepared_at=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        command_principal="pytest:phonon-portfolio",
        identity_namespace=f"test-portfolio-{seed}",
    )


def _protocol(
    *,
    commissioning,
    gate,
    controller,
    reproduction_campaign_id: str,
) -> PhononIndependentReplayProtocol:
    return PhononIndependentReplayProtocol(
        quest_id=commissioning.quest.node_id,
        gate_id=gate.gate_id,
        gate_manifest_sha256=gate.manifest_sha256,
        controller_id=controller.controller_id,
        controller_manifest_sha256=controller.manifest_sha256,
        commissioning_id=commissioning.commissioning_id,
        commissioning_manifest_sha256=commissioning.manifest_sha256,
        original_campaign_id=commissioning.initial_active_campaign_id,
        reproduction_campaign_id=reproduction_campaign_id,
        dataset=PhononReplayArtifact(
            relative_path="fixtures/phonons.json.gz",
            file_sha256=_digest("dataset"),
        ),
        source_plan=PhononReplayArtifact(
            relative_path="fixtures/plan.json",
            file_sha256=_digest("plan-file"),
            content_sha256=_digest("plan-content"),
        ),
        source_result=PhononReplayArtifact(
            relative_path="fixtures/result.json",
            file_sha256=_digest("result-file"),
            content_sha256=commissioning.evidence.result_sha256,
        ),
        source_split_membership_sha256=_digest("split"),
        source_composition_matrix_sha256=_digest("composition"),
        source_structure_matrix_sha256=_digest("structure"),
        estimator_policy=IndependentExtraTreesPolicy(
            n_estimators=64,
            max_depth=10,
            min_samples_leaf=1,
            random_state=20260829,
        ),
        permutation_seed=20260830,
        bootstrap_seed=20260831,
        bootstrap_resamples=200,
        confidence_level=0.9,
        minimum_relative_mae_improvement=0.01,
        required_package_versions={
            name: importlib.metadata.version(name) for name in _PACKAGES
        },
        code_identity=capture_phonon_replay_code_identity(require_committed=False),
        prepared_at=datetime.now(timezone.utc),
        execution_class="engineering",
    )


def test_portfolio_requires_human_plan_then_materializes_one_in_window_shadow_epoch() -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    commissioning = _commissioning(seed)
    apply_phonon_quest_commissioning(commissioning)
    fault = _passing_fault_report(seed, commissioning.quest.node_id, base)
    fault_receipt = FaultCampaignStore().commit(
        fault,
        FaultCampaignCommitContext(
            idempotency_key=f"portfolio-fault:{seed}",
            principal="harness:portfolio-test",
        ),
        now=base + timedelta(seconds=5),
    )
    gate = prepare_endurance_gate_manifest(
        gate_key=f"portfolio-gate-{seed}",
        quest_id=commissioning.quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=120,
        checkpoint_interval_seconds=30,
        maximum_checkpoint_gap_seconds=60,
        prerequisite_fault_campaign_id=fault_receipt.campaign_id,
        harness_code_sha256=_digest(f"{seed}:harness"),
        environment_manifest_sha256=_digest(f"{seed}:environment"),
    )
    relative_root = Path("artifacts") / f"phonon-portfolio-test-{seed}"
    root = REPO_ROOT / relative_root
    controller_path = root / "controller.json"
    protocol_path = root / "protocol.json"
    commissioning_path = root / "commissioning.json"
    portfolio_path = root / "portfolio-work-order.json"
    stage_path = root / "portfolio-stage.json"
    controller = prepare_endurance_controller_manifest(
        gate,
        controller_key=f"portfolio-controller-{seed}",
        principal="controller:portfolio-test",
        spool_root=(relative_root / "spool").as_posix(),
        supervisor_poll_seconds=5,
        prepared_at=base,
        require_committed=False,
    )
    campaigns = {
        item.identity_key.rsplit(":", 1)[-1]: item for item in commissioning.campaigns
    }
    protocol = _protocol(
        commissioning=commissioning,
        gate=gate,
        controller=controller,
        reproduction_campaign_id=campaigns["mechanism-ablation"].node_id,
    )
    try:
        root.mkdir(parents=True, mode=0o700)
        controller_path.write_bytes(_json_bytes(controller))
        protocol_path.write_bytes(_json_bytes(protocol))
        commissioning_path.write_bytes(_json_bytes(commissioning))
        work_order = prepare_phonon_endurance_portfolio_work_order(
            controller=controller,
            controller_path=controller_path,
            protocol=protocol,
            protocol_path=protocol_path,
            commissioning=commissioning,
            commissioning_path=commissioning_path,
            prepared_at=base,
            require_committed=False,
        )
        stage = stage_phonon_endurance_portfolio(work_order)
        portfolio_path.write_bytes(_json_bytes(work_order))
        stage_path.write_bytes(_json_bytes(stage))
        assert stage_phonon_endurance_portfolio(work_order) == stage
        assert stage.planner_output_materialized is False
        assert {item.action_type for item in stage.candidates} == {
            PortfolioActionType.REPLICATION,
            PortfolioActionType.MECHANISM_TEST,
            PortfolioActionType.START_CAMPAIGN,
            PortfolioActionType.ACQUIRE_DATA,
        }
        blocked = preflight_phonon_portfolio_start(work_order, stage)
        assert blocked.ready_for_explicit_gate_start is False
        assert blocked.blockers == ("human_plan:not_committed",)
        with pytest.raises(PhononEndurancePortfolioConflict, match="explicit gate start"):
            evaluate_phonon_endurance_portfolio(work_order, stage)

        replication = next(
            item
            for item in stage.candidates
            if item.action_type is PortfolioActionType.REPLICATION
        )
        selection = PhononBlindPortfolioSelection(
            selected_candidate_ids=(replication.candidate_id,),
            rationale="Human baseline prioritizes the frozen same-source replay first.",
        )
        with pytest.raises(
            PhononEndurancePortfolioConflict,
            match=r"explicit human:\* principal",
        ):
            commit_phonon_blind_portfolio_plan(
                work_order,
                stage,
                selection,
                human_principal="planner:portfolio-test",
            )
        plan = commit_phonon_blind_portfolio_plan(
            work_order,
            stage,
            selection,
            human_principal="human:portfolio-test",
        )
        assert (
            commit_phonon_blind_portfolio_plan(
                work_order,
                stage,
                selection,
                human_principal="human:portfolio-test",
            )
            == plan
        )
        efficiency_work_order = prepare_phonon_portfolio_efficiency_work_order(
            portfolio=work_order,
            portfolio_path=portfolio_path,
            stage=stage,
            stage_path=stage_path,
            prepared_at=plan.committed_at,
            require_committed=False,
        )
        verify_phonon_portfolio_efficiency_work_order(
            efficiency_work_order,
            require_no_epoch=True,
        )
        efficiency_preflight = preflight_phonon_portfolio_efficiency_start(
            efficiency_work_order
        )
        assert efficiency_preflight.ready_for_explicit_gate_start is True
        ready = preflight_phonon_portfolio_start(work_order, stage)
        assert ready.ready_for_explicit_gate_start is True
        started = start_endurance_controller_gate(
            controller,
            artifact_root=REPO_ROOT,
        )
        epoch = evaluate_phonon_endurance_portfolio(work_order, stage)
        assert evaluate_phonon_endurance_portfolio(work_order, stage) == epoch
        assert epoch.epoch.evaluated_at >= started.database_observed_at
        assert epoch.epoch.decision.shadow_only is True
        assert epoch.epoch.decision.actions_enqueued is False
        efficiency = assess_phonon_portfolio_efficiency(efficiency_work_order)
        verify_phonon_portfolio_efficiency_assessment(
            efficiency_work_order,
            efficiency,
        )
        assert efficiency.receipt.assessed_at == epoch.epoch.evaluated_at
        assert efficiency.baseline_candidate_ids == (replication.candidate_id,)
        assert efficiency.meets_gate_floor is True
        assert (
            efficiency.receipt.improvement_ppm
            >= gate.minimum_efficiency_improvement_ppm
        )
        assert efficiency.actions_enqueued is False
        slate_actions = {item.candidate_id: item for item in work_order.actions}
        external_score = next(
            item
            for item in epoch.epoch.scores
            if slate_actions[item.candidate_id].action_type
            is PortfolioActionType.ACQUIRE_DATA
        )
        assert external_score.feasible is False
        assert "data:missing_role:external_validation" in external_score.blockers
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_negative_pivot_requires_exact_contradiction_and_replays_transitions() -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    commissioning = _commissioning(seed)
    apply_phonon_quest_commissioning(commissioning)
    fault_receipt = FaultCampaignStore().commit(
        _passing_fault_report(seed, commissioning.quest.node_id, base),
        FaultCampaignCommitContext(
            idempotency_key=f"pivot-fault:{seed}",
            principal="harness:pivot-test",
        ),
        now=base + timedelta(seconds=5),
    )
    gate = prepare_endurance_gate_manifest(
        gate_key=f"pivot-gate-{seed}",
        quest_id=commissioning.quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=120,
        checkpoint_interval_seconds=30,
        maximum_checkpoint_gap_seconds=60,
        prerequisite_fault_campaign_id=fault_receipt.campaign_id,
        harness_code_sha256=_digest(f"{seed}:harness"),
        environment_manifest_sha256=_digest(f"{seed}:environment"),
    )
    relative_root = Path("artifacts") / f"phonon-pivot-test-{seed}"
    root = REPO_ROOT / relative_root
    controller_path = root / "controller.json"
    protocol_path = root / "protocol.json"
    commissioning_path = root / "commissioning.json"
    controller = prepare_endurance_controller_manifest(
        gate,
        controller_key=f"pivot-controller-{seed}",
        principal="controller:pivot-test",
        spool_root=(relative_root / "spool").as_posix(),
        supervisor_poll_seconds=5,
        prepared_at=base,
        require_committed=False,
    )
    campaigns = {
        item.identity_key.rsplit(":", 1)[-1]: item for item in commissioning.campaigns
    }
    protocol = _protocol(
        commissioning=commissioning,
        gate=gate,
        controller=controller,
        reproduction_campaign_id=campaigns["mechanism-ablation"].node_id,
    )
    try:
        root.mkdir(parents=True, mode=0o700)
        controller_path.write_bytes(_json_bytes(controller))
        protocol_path.write_bytes(_json_bytes(protocol))
        commissioning_path.write_bytes(_json_bytes(commissioning))
        work_order = prepare_phonon_negative_pivot_work_order(
            controller=controller,
            controller_path=controller_path,
            protocol=protocol,
            protocol_path=protocol_path,
            commissioning=commissioning,
            commissioning_path=commissioning_path,
            prepared_at=base,
            transition_principal="controller:pivot-transition-test",
            assessed_by="harness:pivot-assessor-test",
            producer="harness:pivot-producer-test",
            require_committed=False,
        )
        preflight = preflight_phonon_negative_pivot_start(work_order)
        assert preflight.ready_for_explicit_gate_start is True
        started = start_endurance_controller_gate(controller, artifact_root=REPO_ROOT)
        graph = ProgramGraphStore()
        initial = graph.get_quest(work_order.quest_id)
        source = next(
            item for item in initial.nodes if item.node_id == work_order.source_campaign_id
        )
        graph.transition_node(
            NodeTransitionSpec(
                node_id=work_order.source_campaign_id,
                expected_version=source.state_version,
                to_state=GraphNodeState.ACTIVE,
                reason="Activate the frozen same-source replay branch for the test.",
            ),
            GraphCommandContext(
                idempotency_key=f"pivot-test:{seed}:activate-source",
                principal="controller:pivot-reproduction-test",
            ),
        )
        result_sha256 = _digest(f"{seed}:contradicted-result")
        result_id = f"pirr_{result_sha256[:32]}"
        fact_spec = ResearchMemoryFactSpec(
            scope_node_id=work_order.source_campaign_id,
            kind=MemoryFactKind.NEGATIVE_RESULT,
            statement=(
                "Implementation-diverse same-source replay contradicted the preregistered robust "
                "aligned-structure signal and must trigger strategy review without claim repair."
            ),
            detail={
                "schema": "aletheia.phonon_independent_replay_outcome.v1",
                "protocol_id": protocol.protocol_id,
                "protocol_sha256": protocol.protocol_sha256,
                "result_id": result_id,
                "result_sha256": result_sha256,
                "disposition": "no_aligned_structure_advantage",
                "conclusion": "contradicted",
                "same_source_only": True,
                "independent_external_replication_claim_forbidden": True,
                "causal_or_mechanism_claim_forbidden": True,
            },
            task_bindings=(
                MemoryTaskBindingSpec(
                    task_key="phonon-replay-outcome",
                    context_role=MemoryContextRole.REQUIRED,
                ),
                MemoryTaskBindingSpec(
                    task_key="pivot-analysis",
                    context_role=MemoryContextRole.REQUIRED,
                ),
            ),
            sources=(
                MemorySourceRef(
                    kind=MemorySourceKind.ARTIFACT,
                    source_id=f"phonon-independent-replay:{result_id}",
                    sha256=result_sha256,
                    uri=(relative_root / "contradicted-result.json").as_posix(),
                ),
            ),
        )
        fact_receipt = ResearchMemoryStore().register_fact(
            fact_spec,
            GraphCommandContext(
                idempotency_key=f"pivot-test:{seed}:negative-result",
                principal="worker:pivot-reproduction-test",
            ),
        )
        reproduction = EnduranceReproductionReceipt(
            original_campaign_id=work_order.original_campaign_id,
            reproduction_campaign_id=work_order.source_campaign_id,
            protocol_sha256=protocol.protocol_sha256,
            original_result_sha256=str(protocol.source_result.content_sha256),
            reproduction_result_sha256=result_sha256,
            conclusion=EnduranceReproductionConclusion.CONTRADICTED,
            evidence_sha256s=(result_sha256,),
            validated_by="harness:phonon-independent-replay",
            completed_at=started.database_observed_at,
        )
        envelope, envelope_created = submit_controller_evidence(
            controller,
            EnduranceCheckpointEvidence(reproductions=(reproduction,)),
            producer="worker:pivot-reproduction-test",
            submitted_at=started.database_observed_at,
            artifact_root=REPO_ROOT,
        )
        trigger = PhononReplayCommitReceipt(
            protocol_id=str(protocol.protocol_id),
            result_id=result_id,
            result_sha256=result_sha256,
            conclusion=EnduranceReproductionConclusion.CONTRADICTED,
            memory_fact_id=fact_receipt.object_id,
            memory_fact_created=True,
            envelope=envelope,
            envelope_created=envelope_created,
        )
        confirmed_projection = trigger.model_dump(mode="python")
        confirmed_projection["conclusion"] = EnduranceReproductionConclusion.CONFIRMED
        with pytest.raises(PhononNegativePivotNotApplicable, match="requires a contradicted"):
            execute_phonon_negative_result_pivot(
                work_order,
                PhononReplayCommitReceipt.model_validate(confirmed_projection),
                artifact_root=REPO_ROOT,
            )
        still_unpivoted = graph.get_quest(work_order.quest_id)
        assert next(
            item
            for item in still_unpivoted.nodes
            if item.node_id == work_order.source_campaign_id
        ).state is GraphNodeState.ACTIVE
        receipt = execute_phonon_negative_result_pivot(
            work_order,
            trigger,
            artifact_root=REPO_ROOT,
        )
        assert (
            execute_phonon_negative_result_pivot(
                work_order,
                trigger,
                artifact_root=REPO_ROOT,
            )
            == receipt
        )
        final = graph.get_quest(work_order.quest_id)
        states = {item.node_id: item.state for item in final.nodes}
        assert states[work_order.source_campaign_id] is GraphNodeState.STOPPED
        assert states[work_order.successor_campaign_id] is GraphNodeState.ACTIVE
        assert receipt.data_allocated is False
        assert receipt.outward_action_authorized is False
        tick = run_controller_tick(controller, artifact_root=REPO_ROOT)
        assert tick.resulting_checkpoint_count == 1
        assert receipt.pivot.receipt_id in {
            item.receipt_id
            for item in ResearchEnduranceStore()
            .get(work_order.gate_id)
            .checkpoints[0]
            .checkpoint.evidence.structural_pivots
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
