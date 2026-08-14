"""F7 issue 11 validation calibration and receipt-linked Frontier Gate reports."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.evals.baselines import (
    BaselineAnalysisPolicy,
    BaselineArmId,
    BaselineMatrixPlan,
    BaselineMatrixRunner,
    MatrixPhase,
)
from aletheia.evals.frontier_gate import (
    ComparisonAcceptanceThreshold,
    ComparisonCalibrationRule,
    ComparisonRequirement,
    FrontierGateAcceptanceConfig,
    FrontierGateError,
    FrontierGateReport,
    FrontierGateTier,
    FrontierGateTrack,
    GateCalibrationPolicy,
    GateEvaluationInput,
    GateVerdict,
    ObjectiveAcceptanceThreshold,
    ObjectiveCalibrationRule,
    PrivateGateInput,
    ReferenceBaselineEvidence,
    SuiteAcceptanceConfig,
    SuiteAcceptanceThresholds,
    SuiteCalibrationPlan,
    ThresholdDirection,
    calibrate_suite_acceptance,
    freeze_frontier_gate_acceptance,
    generate_frontier_gate_report,
    render_frontier_gate_markdown,
    render_frontier_gate_svg,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.private_suite import (
    PrivateSuiteAccessAuthorization,
    PrivateSuiteManifest,
    close_private_suite_access,
)
from aletheia.evals.schemas import content_sha256

from .f7s2_fixtures import EVALUATOR_HASH, SIGNING_KEY, HardExecutor, build_case, write_submission
from .test_baseline_matrix import _arms, _runner
from .test_private_suite_policy import (
    BASE as PRIVATE_BASE,
    _build_case as _build_private_case,
    _materialize as _materialize_private_case,
    _runner as _private_runner,
)


OWNER = "a" * 64
AUDITOR = "b" * 64
OWNER_APPROVAL = "c" * 64
AUDITOR_APPROVAL = "d" * 64


def _analysis() -> BaselineAnalysisPolicy:
    return BaselineAnalysisPolicy(
        secondary_objective_metrics=("exact",),
        bootstrap_resamples=100,
        bootstrap_seed=7409,
    )


def _matrix(
    suite,
    slots,
    *,
    phase: MatrixPhase,
    frozen_at: datetime,
    parent: str | None = None,
) -> BaselineMatrixPlan:
    return BaselineMatrixPlan(
        matrix_id=f"issue11-{phase.value}-matrix-v1",
        suite_manifest_sha256=suite.manifest_sha256,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        phase=phase,
        parent_validation_matrix_sha256=parent,
        arms=_arms(),
        slots=slots,
        max_infra_retries_per_slot=1,
        block_randomization_seed=20260814,
        analysis=_analysis(),
        frozen_at=frozen_at,
    )


def _runners(base_runner, ledger, *, full_failures: int = 0):
    def wrong(context):
        write_submission(context, answer="wrong")

    def full(context):
        write_submission(
            context,
            answer="wrong" if context.request.repeat_index < full_failures else "42",
        )

    return {
        BaselineArmId.DIRECT_MODEL: _runner(
            base_runner,
            ledger,
            HardExecutor(wrong, cost_usd=0.10, usage_metering="provider_receipt"),
        ),
        BaselineArmId.GENERIC_AGENT: _runner(
            base_runner,
            ledger,
            HardExecutor(write_submission, cost_usd=0.20, usage_metering="provider_receipt"),
        ),
        BaselineArmId.ALETHEIA_NO_K2: _runner(
            base_runner,
            ledger,
            HardExecutor(wrong, cost_usd=0.30, usage_metering="provider_receipt"),
        ),
        BaselineArmId.ALETHEIA_FULL_K2: _runner(
            base_runner,
            ledger,
            HardExecutor(full, cost_usd=0.40, usage_metering="provider_receipt"),
        ),
    }


def _execute(base_runner, suite, task, ledger, matrix, *, full_failures: int = 0):
    return BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=_runners(base_runner, ledger, full_failures=full_failures),
    ).run()


def _comparison_rules() -> tuple[ComparisonCalibrationRule, ...]:
    return (
        ComparisonCalibrationRule(
            comparator_arm=BaselineArmId.DIRECT_MODEL,
            requirement=ComparisonRequirement.SUPERIORITY,
            minimum_practical_effect=0.05,
            max_holm_adjusted_p_value=0.05,
            max_mean_paired_cost_increase_usd=1.0,
            rationale="Full K2 must beat the direct frozen base model.",
        ),
        ComparisonCalibrationRule(
            comparator_arm=BaselineArmId.GENERIC_AGENT,
            requirement=ComparisonRequirement.NONINFERIORITY,
            noninferiority_margin=0.10,
            max_mean_paired_cost_increase_usd=1.0,
            rationale="Full K2 must preserve generic-agent scientific performance.",
        ),
        ComparisonCalibrationRule(
            comparator_arm=BaselineArmId.ALETHEIA_NO_K2,
            requirement=ComparisonRequirement.SUPERIORITY,
            minimum_practical_effect=0.05,
            max_holm_adjusted_p_value=0.05,
            max_mean_paired_cost_increase_usd=1.0,
            rationale="K2 campaign learning must contribute a reproducible effect.",
        ),
    )


def _policy() -> GateCalibrationPolicy:
    return GateCalibrationPolicy(
        absolute_max_mean_cost_usd=1.0,
        comparisons=_comparison_rules(),
        objectives=(
            ObjectiveCalibrationRule(
                metric="exact",
                direction=ThresholdDirection.MINIMUM,
                absolute_boundary=0.80,
                allowable_validation_degradation=0.10,
                rationale="Exact scientific conclusion is the fixture's frozen objective.",
            ),
        ),
    )


@dataclass(frozen=True)
class GateCase:
    plan: SuiteCalibrationPlan
    validation_matrix: BaselineMatrixPlan
    validation_suite: object
    validation_result: object
    validation_ledger: object
    test_matrix: BaselineMatrixPlan
    test_suite: object
    test_result: object
    test_ledger: object
    suite_config: SuiteAcceptanceConfig
    program_config: FrontierGateAcceptanceConfig

    @property
    def evaluation(self) -> GateEvaluationInput:
        return GateEvaluationInput(
            track=FrontierGateTrack.SCIENCEAGENTBENCH,
            matrix=self.test_matrix,
            suite=self.test_suite,
            result=self.test_result,
            ledger=self.test_ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
        )


def _gate_case(tmp_path) -> GateCase:
    validation_runner, validation_suite, validation_plan, validation_task, validation_ledger = (
        build_case(tmp_path / "validation", repeats=10)
    )
    test_runner, test_suite, test_plan, test_task, test_ledger = build_case(
        tmp_path / "test", repeats=10
    )
    assert validation_suite == test_suite
    assert validation_task == test_task
    base = datetime.now(timezone.utc) - timedelta(days=2)
    validation_matrix = _matrix(
        validation_suite,
        validation_plan.slots,
        phase=MatrixPhase.VALIDATION,
        frozen_at=base,
    )
    test_matrix = _matrix(
        test_suite,
        test_plan.slots,
        phase=MatrixPhase.TEST,
        parent=validation_matrix.manifest_sha256,
        frozen_at=base + timedelta(minutes=1),
    )
    reference = ReferenceBaselineEvidence(
        reference_id="independent-exact-reference-v1",
        evaluation_suite_manifest_sha256=validation_suite.manifest_sha256,
        baseline_type="reference_implementation",
        covered_task_manifest_sha256s=validation_suite.task_manifest_sha256s,
        pass_at_1=1.0,
        scientific_valid_fraction=1.0,
        evidence_sha256="e" * 64,
        reviewer_principal_sha256=AUDITOR,
        measured_at=base,
    )
    plan = SuiteCalibrationPlan(
        plan_id="scienceagentbench-gate-calibration-v1",
        tier=FrontierGateTier.PILOT,
        track=FrontierGateTrack.SCIENCEAGENTBENCH,
        validation_matrix_manifest_sha256=validation_matrix.manifest_sha256,
        validation_suite_manifest_sha256=validation_suite.manifest_sha256,
        test_matrix_manifest_sha256=test_matrix.manifest_sha256,
        test_suite_manifest_sha256=test_suite.manifest_sha256,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        policy=_policy(),
        reference_baselines=(reference,),
        calibration_owner_principal_sha256=OWNER,
        independent_reviewer_principal_sha256=AUDITOR,
        frozen_at=base + timedelta(minutes=2),
    )
    validation_result = _execute(
        validation_runner,
        validation_suite,
        validation_task,
        validation_ledger,
        validation_matrix,
    )
    calibrated_at = datetime.now(timezone.utc)
    suite_config = calibrate_suite_acceptance(
        plan=plan,
        validation_matrix=validation_matrix,
        validation_suite=validation_suite,
        validation_result=validation_result,
        validation_ledger=validation_ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
        test_matrix=test_matrix,
        test_suite=test_suite,
        suite_config_id="scienceagentbench-acceptance-v1",
        calibrated_at=calibrated_at,
    )
    program_config = freeze_frontier_gate_acceptance(
        program_config_id="aletheia-frontier-gate-pilot-v1",
        version="1.0.0",
        tier=FrontierGateTier.PILOT,
        suites=(suite_config,),
        acceptance_owner_principal_sha256=OWNER,
        independent_auditor_principal_sha256=AUDITOR,
        owner_approval_evidence_sha256=OWNER_APPROVAL,
        auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
        scientific_claim="Full K2 satisfies the frozen pilot benchmark acceptance policy.",
        frozen_at=datetime.now(timezone.utc),
    )
    test_result = _execute(
        test_runner,
        test_suite,
        test_task,
        test_ledger,
        test_matrix,
    )
    return GateCase(
        plan=plan,
        validation_matrix=validation_matrix,
        validation_suite=validation_suite,
        validation_result=validation_result,
        validation_ledger=validation_ledger,
        test_matrix=test_matrix,
        test_suite=test_suite,
        test_result=test_result,
        test_ledger=test_ledger,
        suite_config=suite_config,
        program_config=program_config,
    )


def test_calibration_is_deterministic_and_binds_raw_validation_receipts(tmp_path):
    case = _gate_case(tmp_path)
    repeated = calibrate_suite_acceptance(
        plan=case.plan,
        validation_matrix=case.validation_matrix,
        validation_suite=case.validation_suite,
        validation_result=case.validation_result,
        validation_ledger=case.validation_ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
        test_matrix=case.test_matrix,
        test_suite=case.test_suite,
        suite_config_id=case.suite_config.suite_config_id,
        calibrated_at=case.suite_config.calibrated_at,
    )
    assert repeated == case.suite_config
    assert repeated.validation_result_sha256 == case.validation_result.result_sha256
    assert repeated.thresholds.min_full_k2_pass_at_1 == pytest.approx(0.9)
    assert repeated.thresholds.max_full_k2_mean_cost_usd == pytest.approx(0.6)


def test_calibration_plan_must_precede_validation_and_test_treatments_cannot_drift(tmp_path):
    case = _gate_case(tmp_path)
    before_matrices = case.plan.model_copy(
        update={"frozen_at": case.validation_matrix.frozen_at - timedelta(seconds=1)}
    )
    with pytest.raises(FrontierGateError, match="cannot predate either frozen matrix"):
        calibrate_suite_acceptance(
            plan=before_matrices,
            validation_matrix=case.validation_matrix,
            validation_suite=case.validation_suite,
            validation_result=case.validation_result,
            validation_ledger=case.validation_ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
            test_matrix=case.test_matrix,
            test_suite=case.test_suite,
            suite_config_id="pre-matrix-plan-v1",
        )

    late = case.plan.model_copy(
        update={"frozen_at": case.validation_result.started_at + timedelta(seconds=1)}
    )
    with pytest.raises(FrontierGateError, match="frozen after validation"):
        calibrate_suite_acceptance(
            plan=late,
            validation_matrix=case.validation_matrix,
            validation_suite=case.validation_suite,
            validation_result=case.validation_result,
            validation_ledger=case.validation_ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
            test_matrix=case.test_matrix,
            test_suite=case.test_suite,
            suite_config_id="late-plan-v1",
        )

    drifted = case.test_matrix.model_copy(
        update={
            "analysis": case.test_matrix.analysis.model_copy(
                update={"bootstrap_seed": case.test_matrix.analysis.bootstrap_seed + 1}
            )
        }
    )
    with pytest.raises(FrontierGateError, match="frozen plan"):
        calibrate_suite_acceptance(
            plan=case.plan,
            validation_matrix=case.validation_matrix,
            validation_suite=case.validation_suite,
            validation_result=case.validation_result,
            validation_ledger=case.validation_ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
            test_matrix=drifted,
            test_suite=case.test_suite,
            suite_config_id="drifted-test-v1",
        )


def test_reference_evidence_must_exist_before_freeze_and_cover_validation(tmp_path):
    case = _gate_case(tmp_path)
    raw = case.plan.model_dump()
    raw["reference_baselines"][0]["measured_at"] = case.plan.frozen_at + timedelta(seconds=1)
    with pytest.raises(ValidationError, match="must exist before calibration freeze"):
        SuiteCalibrationPlan.model_validate(raw)

    outside = case.plan.reference_baselines[0].model_copy(
        update={"covered_task_manifest_sha256s": ("f" * 64,)}
    )
    bad_plan = SuiteCalibrationPlan.model_validate(
        case.plan.model_dump() | {"reference_baselines": (outside,)}
    )
    with pytest.raises(FrontierGateError, match="outside validation"):
        calibrate_suite_acceptance(
            plan=bad_plan,
            validation_matrix=case.validation_matrix,
            validation_suite=case.validation_suite,
            validation_result=case.validation_result,
            validation_ledger=case.validation_ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
            test_matrix=case.test_matrix,
            test_suite=case.test_suite,
            suite_config_id="outside-reference-v1",
        )


def test_complete_raw_evidence_produces_receipt_linked_pass_and_renderings(tmp_path):
    case = _gate_case(tmp_path)
    report = generate_frontier_gate_report(
        config=case.program_config,
        evaluations={FrontierGateTrack.SCIENCEAGENTBENCH: case.evaluation},
    )
    assert report.overall_verdict is GateVerdict.PASS
    assert report.scientific_claim_allowed is True
    decision = report.suite_decisions[0]
    assert decision.verdict is GateVerdict.PASS
    assert decision.receipt_index is not None
    assert len(decision.receipt_index.attempts) == 40
    assert all(item.status.value == "pass" for item in decision.criteria)
    markdown = render_frontier_gate_markdown(report)
    svg = render_frontier_gate_svg(report)
    assert "Overall verdict: PASS" in markdown
    assert decision.receipt_index.ledger_head_sha256 in markdown
    assert "<svg" in svg and report.evidence_bundle_sha256 in svg
    repeated = generate_frontier_gate_report(
        config=case.program_config,
        evaluations={FrontierGateTrack.SCIENCEAGENTBENCH: case.evaluation},
        generated_at=report.generated_at,
    )
    assert repeated == report
    tampered = report.model_dump()
    tampered["evidence_bundle_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="evidence-bundle hash is invalid"):
        FrontierGateReport.model_validate(tampered)


def test_measured_threshold_miss_is_fail_not_blocked(tmp_path):
    case = _gate_case(tmp_path)
    test_runner, suite, plan, task, ledger = build_case(tmp_path / "worse", repeats=10)
    assert suite == case.test_suite
    assert plan.slots == case.test_matrix.slots
    result = _execute(
        test_runner,
        suite,
        task,
        ledger,
        case.test_matrix,
        full_failures=2,
    )
    evaluation = GateEvaluationInput(
        track=FrontierGateTrack.SCIENCEAGENTBENCH,
        matrix=case.test_matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    report = generate_frontier_gate_report(
        config=case.program_config,
        evaluations={FrontierGateTrack.SCIENCEAGENTBENCH: evaluation},
    )
    assert report.overall_verdict is GateVerdict.FAIL
    assert report.missing_tracks == ()
    assert any(
        item.status.value == "fail" and "pass_at_1" in item.criterion_id
        for item in report.suite_decisions[0].criteria
    )


def test_missing_track_is_explicitly_blocked(tmp_path):
    case = _gate_case(tmp_path)
    report = generate_frontier_gate_report(config=case.program_config, evaluations={})
    assert report.overall_verdict is GateVerdict.BLOCKED
    assert report.scientific_claim_allowed is False
    assert report.missing_tracks == (FrontierGateTrack.SCIENCEAGENTBENCH,)
    assert report.suite_decisions[0].aggregate_report is None


@pytest.mark.parametrize("mutation", ["omit", "forge"])
def test_report_reaggregates_and_rejects_omitted_or_forged_attempt_evidence(tmp_path, mutation):
    case = _gate_case(tmp_path)
    attempts = list(case.test_result.attempts)
    if mutation == "omit":
        attempts.pop()
    else:
        record = attempts[0]
        assert record.scorer_receipt is not None
        forged = record.scorer_receipt.model_copy(update={"hmac_sha256": "0" * 64})
        attempts[0] = record.model_copy(update={"scorer_receipt": forged})
    result = case.test_result.model_copy(update={"attempts": tuple(attempts)})
    evaluation = GateEvaluationInput(
        track=FrontierGateTrack.SCIENCEAGENTBENCH,
        matrix=case.test_matrix,
        suite=case.test_suite,
        result=result,
        ledger=case.test_ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    with pytest.raises(FrontierGateError, match="raw receipt/ledger audit failed"):
        generate_frontier_gate_report(
            config=case.program_config,
            evaluations={FrontierGateTrack.SCIENCEAGENTBENCH: evaluation},
        )


def _formal_suite(source: SuiteAcceptanceConfig, track: FrontierGateTrack):
    objectives = (
        ObjectiveAcceptanceThreshold(
            metric="false_discovery_rate",
            direction=ThresholdDirection.MAXIMUM,
            threshold=0.05,
            maximum_missing_fraction=0,
            validation_observed_mean=0.02,
        ),
        ObjectiveAcceptanceThreshold(
            metric="calibration_error",
            direction=ThresholdDirection.MAXIMUM,
            threshold=0.10,
            maximum_missing_fraction=0,
            validation_observed_mean=0.05,
        ),
        ObjectiveAcceptanceThreshold(
            metric="evidence_provenance_completeness",
            direction=ThresholdDirection.MINIMUM,
            threshold=0.95,
            maximum_missing_fraction=0,
            validation_observed_mean=1.0,
        ),
        ObjectiveAcceptanceThreshold(
            metric="reproduction_fidelity",
            direction=ThresholdDirection.MINIMUM,
            threshold=0.90,
            maximum_missing_fraction=0,
            validation_observed_mean=0.95,
        ),
    )
    thresholds = SuiteAcceptanceThresholds.model_validate(
        source.thresholds.model_dump() | {"objectives": objectives}
    )
    digit = str(list(FrontierGateTrack).index(track) + 1)
    return SuiteAcceptanceConfig.model_validate(
        source.model_dump()
        | {
            "suite_config_id": f"formal-{track.value}-v1",
            "tier": FrontierGateTier.FRONTIER_GATE,
            "track": track,
            "test_matrix_manifest_sha256": digit * 64,
            "test_suite_manifest_sha256": (str(int(digit) + 4)) * 64,
            "thresholds": thresholds,
        }
    )


def test_formal_freeze_requires_all_four_tracks_and_core_scientific_objectives(tmp_path):
    case = _gate_case(tmp_path)
    raw_plan = case.plan.model_dump() | {"tier": FrontierGateTier.FRONTIER_GATE}
    with pytest.raises(ValidationError, match="formal calibration requires"):
        SuiteCalibrationPlan.model_validate(raw_plan)

    formal = tuple(_formal_suite(case.suite_config, track) for track in FrontierGateTrack)
    with pytest.raises(ValidationError, match="exactly all four tracks"):
        freeze_frontier_gate_acceptance(
            program_config_id="incomplete-formal-gate-v1",
            version="1",
            tier=FrontierGateTier.FRONTIER_GATE,
            suites=formal[:-1],
            acceptance_owner_principal_sha256=OWNER,
            independent_auditor_principal_sha256=AUDITOR,
            owner_approval_evidence_sha256=OWNER_APPROVAL,
            auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
            scientific_claim="Incomplete formal claim must be rejected.",
            frozen_at=datetime.now(timezone.utc),
        )
    complete = freeze_frontier_gate_acceptance(
        program_config_id="complete-formal-gate-v1",
        version="1",
        tier=FrontierGateTier.FRONTIER_GATE,
        suites=formal,
        acceptance_owner_principal_sha256=OWNER,
        independent_auditor_principal_sha256=AUDITOR,
        owner_approval_evidence_sha256=OWNER_APPROVAL,
        auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
        scientific_claim="All four frozen tracks must pass before the claim is allowed.",
        frozen_at=datetime.now(timezone.utc),
    )
    assert set(item.track for item in complete.suites) == set(FrontierGateTrack)
    assert len({content_sha256(item.thresholds) for item in complete.suites}) == 1


def test_formal_readiness_without_live_runs_and_private_custody_is_blocked(tmp_path):
    case = _gate_case(tmp_path)
    formal = tuple(_formal_suite(case.suite_config, track) for track in FrontierGateTrack)
    config = freeze_frontier_gate_acceptance(
        program_config_id="formal-readiness-report-v1",
        version="1",
        tier=FrontierGateTier.FRONTIER_GATE,
        suites=formal,
        acceptance_owner_principal_sha256=OWNER,
        independent_auditor_principal_sha256=AUDITOR,
        owner_approval_evidence_sha256=OWNER_APPROVAL,
        auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
        scientific_claim="Formal scientific readiness requires four real held-out tracks.",
        frozen_at=datetime.now(timezone.utc),
    )
    report = generate_frontier_gate_report(config=config, evaluations={})
    assert report.overall_verdict is GateVerdict.BLOCKED
    assert set(report.missing_tracks) == set(FrontierGateTrack)
    assert report.private_custody is None


def test_program_freeze_rejects_same_person_approval(tmp_path):
    case = _gate_case(tmp_path)
    with pytest.raises(ValidationError, match="independent auditor"):
        freeze_frontier_gate_acceptance(
            program_config_id="self-approved-gate-v1",
            version="1",
            tier=FrontierGateTier.PILOT,
            suites=(case.suite_config,),
            acceptance_owner_principal_sha256=OWNER,
            independent_auditor_principal_sha256=OWNER,
            owner_approval_evidence_sha256=OWNER_APPROVAL,
            auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
            scientific_claim="Self-approved claims are invalid.",
            frozen_at=datetime.now(timezone.utc),
        )


def _private_pilot_suite_config(case) -> SuiteAcceptanceConfig:
    comparisons = (
        ComparisonAcceptanceThreshold(
            comparator_arm=BaselineArmId.DIRECT_MODEL,
            requirement=ComparisonRequirement.SUPERIORITY,
            minimum_effect_or_ci_bound=0.01,
            max_holm_adjusted_p_value=0.05,
            minimum_valid_pair_fraction=0.8,
            max_mean_paired_cost_increase_usd=1.0,
            validation_observed_effect=0.5,
        ),
        ComparisonAcceptanceThreshold(
            comparator_arm=BaselineArmId.GENERIC_AGENT,
            requirement=ComparisonRequirement.NONINFERIORITY,
            minimum_effect_or_ci_bound=-0.2,
            minimum_valid_pair_fraction=0.8,
            max_mean_paired_cost_increase_usd=1.0,
            validation_observed_effect=0.0,
        ),
        ComparisonAcceptanceThreshold(
            comparator_arm=BaselineArmId.ALETHEIA_NO_K2,
            requirement=ComparisonRequirement.SUPERIORITY,
            minimum_effect_or_ci_bound=0.01,
            max_holm_adjusted_p_value=0.05,
            minimum_valid_pair_fraction=0.8,
            max_mean_paired_cost_increase_usd=1.0,
            validation_observed_effect=0.5,
        ),
    )
    thresholds = SuiteAcceptanceThresholds(
        min_full_k2_pass_at_1=0,
        min_full_k2_scientific_success_rate=0,
        min_full_k2_scientific_valid_fraction=0,
        max_full_k2_final_invalid_fraction=1,
        max_full_k2_infrastructure_retry_fraction=1,
        max_full_k2_mean_cost_usd=1,
        comparisons=comparisons,
        objectives=(
            ObjectiveAcceptanceThreshold(
                metric="exact",
                direction=ThresholdDirection.MINIMUM,
                threshold=0,
                maximum_missing_fraction=1,
                validation_observed_mean=1,
            ),
        ),
    )
    return SuiteAcceptanceConfig(
        suite_config_id="private-pilot-acceptance-v1",
        tier=FrontierGateTier.PILOT,
        track=FrontierGateTrack.PRIVATE_PROSPECTIVE,
        calibration_plan_sha256="1" * 64,
        validation_matrix_manifest_sha256="2" * 64,
        validation_result_sha256="3" * 64,
        validation_aggregate_report_sha256="4" * 64,
        validation_ledger_head_sha256="5" * 64,
        test_matrix_manifest_sha256=case.matrix.manifest_sha256,
        test_suite_manifest_sha256=case.suite.manifest_sha256,
        evaluator_manifest_sha256=case.matrix.evaluator_manifest_sha256,
        analysis_policy_sha256=content_sha256(case.matrix.analysis),
        arm_manifest_sha256s=tuple(arm.manifest_sha256 for arm in case.matrix.arms),
        reference_baseline_sha256s=("6" * 64,),
        thresholds=thresholds,
        calibrated_at=PRIVATE_BASE + timedelta(days=3, hours=12),
    )


def test_private_custody_is_a_separate_required_audited_decision(tmp_path):
    case = _build_private_case(tmp_path)
    suite_config = _private_pilot_suite_config(case)
    program = freeze_frontier_gate_acceptance(
        program_config_id="private-pilot-program-v1",
        version="1",
        tier=FrontierGateTier.PILOT,
        suites=(suite_config,),
        acceptance_owner_principal_sha256=OWNER,
        independent_auditor_principal_sha256=AUDITOR,
        owner_approval_evidence_sha256=OWNER_APPROVAL,
        auditor_approval_evidence_sha256=AUDITOR_APPROVAL,
        scientific_claim="Private prospective custody and measured outcomes are both required.",
        frozen_at=PRIVATE_BASE + timedelta(days=4),
    )
    manifest = PrivateSuiteManifest.model_validate(
        case.manifest.model_dump()
        | {
            "acceptance_config_sha256": program.config_sha256,
            "frozen_at": program.frozen_at,
        }
    )
    authorization = PrivateSuiteAccessAuthorization.model_validate(
        case.authorization.model_dump()
        | {
            "private_suite_manifest_sha256": manifest.manifest_sha256,
            "acceptance_config_sha256": program.config_sha256,
            "authorized_at": PRIVATE_BASE + timedelta(days=4),
            "expires_at": PRIVATE_BASE + timedelta(days=7),
        }
    )
    case.manifest = manifest
    case.authorization = authorization
    _materialize_private_case(case)

    def wrong(context):
        write_submission(context, answer="wrong")

    runners = {
        BaselineArmId.DIRECT_MODEL: _private_runner(case, action=wrong),
        BaselineArmId.GENERIC_AGENT: _private_runner(case),
        BaselineArmId.ALETHEIA_NO_K2: _private_runner(case, action=wrong),
        BaselineArmId.ALETHEIA_FULL_K2: _private_runner(case),
    }
    result = BaselineMatrixRunner(
        matrix=case.matrix,
        suite=case.suite,
        tasks={case.task.manifest_sha256: case.task},
        runners=runners,
    ).run()
    close_private_suite_access(
        manifest=case.manifest,
        ledger=case.custody,
        access_id=case.authorization.authorization_id,
        evaluator_root=case.root,
        closed_at=datetime.now(timezone.utc),
    )
    evaluation = GateEvaluationInput(
        track=FrontierGateTrack.PRIVATE_PROSPECTIVE,
        matrix=case.matrix,
        suite=case.suite,
        result=result,
        ledger=EvaluationLedger(case.root / "evaluator_ledger" / "events.jsonl"),
        receipt_keys={"private-test-key": SIGNING_KEY},
    )
    report = generate_frontier_gate_report(
        config=program,
        evaluations={FrontierGateTrack.PRIVATE_PROSPECTIVE: evaluation},
        private_input=PrivateGateInput(manifest=case.manifest, ledger=case.custody),
    )
    assert report.private_custody is not None
    assert report.private_custody.verdict is GateVerdict.PASS
    assert all(item.status.value == "pass" for item in report.private_custody.criteria)
    # Five repeats cannot establish Holm-corrected superiority, so the measured suite still fails.
    assert report.overall_verdict is GateVerdict.FAIL


def _write_json(path: Path, payload: object) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_cli_calibrates_freezes_reports_and_refuses_overwrite(tmp_path):
    case = _gate_case(tmp_path / "case")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    paths = {
        "plan": inputs / "plan.json",
        "validation_matrix": inputs / "validation-matrix.json",
        "validation_suite": inputs / "validation-suite.json",
        "validation_result": inputs / "validation-result.json",
        "test_matrix": inputs / "test-matrix.json",
        "test_suite": inputs / "test-suite.json",
        "test_result": inputs / "test-result.json",
        "freeze_request": inputs / "freeze-request.json",
        "evidence_index": inputs / "evidence-index.json",
    }
    _write_json(paths["plan"], case.plan)
    _write_json(paths["validation_matrix"], case.validation_matrix)
    _write_json(paths["validation_suite"], {"suite": case.validation_suite.model_dump(mode="json")})
    _write_json(paths["validation_result"], case.validation_result)
    _write_json(paths["test_matrix"], case.test_matrix)
    _write_json(paths["test_suite"], {"suite": case.test_suite.model_dump(mode="json")})
    _write_json(paths["test_result"], case.test_result)

    script = Path(__file__).parents[2] / "scripts" / "run_frontier_gate.py"
    environment = os.environ.copy()
    environment["ISSUE11_RECEIPT_KEY_B64"] = base64.b64encode(SIGNING_KEY).decode()
    suite_config_path = tmp_path / "suite-acceptance.json"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "calibrate-suite",
            "--plan",
            str(paths["plan"]),
            "--validation-matrix",
            str(paths["validation_matrix"]),
            "--validation-suite-bundle",
            str(paths["validation_suite"]),
            "--validation-result",
            str(paths["validation_result"]),
            "--validation-ledger",
            str(case.validation_ledger.path),
            "--test-matrix",
            str(paths["test_matrix"]),
            "--test-suite-bundle",
            str(paths["test_suite"]),
            "--suite-config-id",
            case.suite_config.suite_config_id,
            "--calibrated-at",
            case.suite_config.calibrated_at.isoformat(),
            "--receipt-key-env",
            "test-evaluator-key=ISSUE11_RECEIPT_KEY_B64",
            "--output",
            str(suite_config_path),
        ],
        check=True,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert (
        SuiteAcceptanceConfig.model_validate_json(suite_config_path.read_bytes())
        == case.suite_config
    )

    _write_json(
        paths["freeze_request"],
        {
            "program_config_id": case.program_config.program_config_id,
            "version": case.program_config.version,
            "tier": case.program_config.tier.value,
            "acceptance_owner_principal_sha256": OWNER,
            "independent_auditor_principal_sha256": AUDITOR,
            "owner_approval_evidence_sha256": OWNER_APPROVAL,
            "auditor_approval_evidence_sha256": AUDITOR_APPROVAL,
            "scientific_claim": case.program_config.scientific_claim,
            "frozen_at": case.program_config.frozen_at.isoformat(),
        },
    )
    program_path = tmp_path / "program-acceptance.json"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "freeze-config",
            "--freeze-request",
            str(paths["freeze_request"]),
            "--suite-config",
            str(suite_config_path),
            "--output",
            str(program_path),
        ],
        check=True,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert (
        FrontierGateAcceptanceConfig.model_validate_json(program_path.read_bytes())
        == case.program_config
    )

    _write_json(
        paths["evidence_index"],
        {
            "tracks": {
                FrontierGateTrack.SCIENCEAGENTBENCH.value: {
                    "matrix": str(paths["test_matrix"]),
                    "suite_bundle": str(paths["test_suite"]),
                    "result": str(paths["test_result"]),
                    "ledger": str(case.test_ledger.path),
                    "receipt_key_env": ["test-evaluator-key=ISSUE11_RECEIPT_KEY_B64"],
                }
            }
        },
    )
    report_json = tmp_path / "frontier-gate.json"
    report_markdown = tmp_path / "frontier-gate.md"
    report_svg = tmp_path / "frontier-gate.svg"
    command = [
        sys.executable,
        str(script),
        "report",
        "--config",
        str(program_path),
        "--evidence-index",
        str(paths["evidence_index"]),
        "--generated-at",
        datetime.now(timezone.utc).isoformat(),
        "--output-json",
        str(report_json),
        "--output-markdown",
        str(report_markdown),
        "--output-svg",
        str(report_svg),
    ]
    subprocess.run(
        command,
        check=True,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert (
        FrontierGateReport.model_validate_json(report_json.read_bytes()).overall_verdict
        is GateVerdict.PASS
    )
    assert "Overall verdict: PASS" in report_markdown.read_text(encoding="utf-8")
    assert report_svg.read_text(encoding="utf-8").startswith("<svg")

    refused = subprocess.run(
        command,
        check=False,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert refused.returncode != 0
    assert "refusing to replace frozen output" in refused.stderr

    dangling_output = tmp_path / "dangling-report.json"
    dangling_output.symlink_to(tmp_path / "missing-report-target.json")
    symlink_command = list(command)
    replacements = {
        str(report_json): str(dangling_output),
        str(report_markdown): str(tmp_path / "symlink-attempt.md"),
        str(report_svg): str(tmp_path / "symlink-attempt.svg"),
    }
    symlink_command = [replacements.get(value, value) for value in symlink_command]
    refused_symlink = subprocess.run(
        symlink_command,
        check=False,
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert refused_symlink.returncode != 0
    assert "refusing to replace frozen output" in refused_symlink.stderr
