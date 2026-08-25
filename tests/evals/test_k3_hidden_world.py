"""F9-S9 frozen hidden-world ablation and scientific-exit tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import timedelta

import pytest
from pydantic import ValidationError

from aletheia.evals.adapters.discoveryworld import (
    DiscoveryWorldHarnessResult,
    DiscoveryWorldTraceStep,
    derive_discoveryworld_scientific_exit_metrics,
)
from aletheia.evals.k3_hidden_world import (
    K3CriterionStatus,
    K3HiddenWorldArmId,
    K3HiddenWorldError,
    K3HiddenWorldPhase,
    aggregate_k3_hidden_world_matrix,
    build_k3_hidden_world_run_plans,
    freeze_k3_hidden_world_acceptance,
    generate_k3_hidden_world_scientific_exit_decision,
    k3_hidden_world_execution_schedule,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.schemas import ExecutionExitReason

from .f7s2_fixtures import SIGNING_KEY
from .k3_hidden_world_fixtures import (
    arms,
    build_suite,
    execute,
    matrix,
    threshold_policy,
    utcnow,
)


def _trace_sha256(trace: tuple[DiscoveryWorldTraceStep, ...]) -> str:
    payload = [step.model_dump(mode="json") for step in trace]
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _step(
    sequence: int,
    *,
    kind: str = "act",
    tested: str | None = None,
    outcome: str | None = None,
    remaining=("substance_a", "substance_b", "substance_c", "substance_d"),
    beliefs=None,
):
    return DiscoveryWorldTraceStep(
        sequence=sequence,
        kind=kind,
        world_action={"action": "USE"} if kind == "act" else None,
        action_sha256=str(sequence + 1) * 64,
        observation_before_sha256="a" * 64,
        observation_after_sha256="b" * 64 if kind == "act" else None,
        valid_action=True if kind == "act" else None,
        world_step_before=sequence,
        world_step_after=sequence + int(kind == "act"),
        beliefs=beliefs
        or {
            "substance_a": 0.25,
            "substance_b": 0.25,
            "substance_c": 0.25,
            "substance_d": 0.25,
        },
        hypothesis_note_sha256="c" * 64,
        informative_trial_hypothesis_id=tested,
        informative_trial_outcome=outcome,
        objective_remaining_after=remaining,
    )


def test_hidden_truth_metrics_reward_early_discrimination_and_score_false_claims():
    trace = (
        _step(
            0,
            tested="substance_a",
            outcome="negative",
            remaining=("substance_b", "substance_c", "substance_d"),
        ),
        _step(
            1,
            tested="substance_b",
            outcome="positive",
            remaining=("substance_b",),
            beliefs={
                "substance_a": 0.05,
                "substance_b": 0.85,
                "substance_c": 0.05,
                "substance_d": 0.05,
            },
        ),
        _step(
            2,
            kind="stop",
            remaining=("substance_b",),
            beliefs={
                "substance_a": 0.02,
                "substance_b": 0.94,
                "substance_c": 0.02,
                "substance_d": 0.02,
            },
        ),
    )
    result = DiscoveryWorldHarnessResult(
        instance_id="truth-metrics",
        run_index=0,
        candidate_image_id="sha256:" + "1" * 64,
        environment_image_id="sha256:" + "2" * 64,
        program_returncode=0,
        program_exit_reason=ExecutionExitReason.COMPLETED,
        program_wall_time_s=0.1,
        program_log_sha256="3" * 64,
        environment_log_sha256="4" * 64,
        protocol_valid=True,
        stopped=True,
        task_completed=True,
        completed_successfully=True,
        procedural_score=1.0,
        final_hypothesis_id="substance_b",
        explicit_rule_discovery=True,
        action_count=2,
        valid_action_count=2,
        informative_trials=2,
        distinct_hypotheses_tested=2,
        trace_sha256=_trace_sha256(trace),
        trace=trace,
    )
    metrics = derive_discoveryworld_scientific_exit_metrics(
        result=result, correct_hypothesis_id="substance_b"
    )
    assert metrics.trace_complete is True
    assert metrics.genuine_discriminating_trials == 2
    assert metrics.discriminating_trial_rate == 1
    assert metrics.wrong_explanation_elimination_score == pytest.approx(5 / 6)
    assert metrics.informative_trials_to_identification == 2
    assert metrics.posterior_brier_score == pytest.approx(0.0024)
    assert metrics.false_mechanism_claim is False

    wrong = result.model_copy(
        update={
            "final_hypothesis_id": "substance_c",
            "explicit_rule_discovery": False,
            "completed_successfully": False,
            "task_completed": False,
        }
    )
    wrong_metrics = derive_discoveryworld_scientific_exit_metrics(
        result=wrong, correct_hypothesis_id="substance_b"
    )
    assert wrong_metrics.false_mechanism_claim is True


def test_preregistration_rejects_treatment_drift_and_underpowered_test(tmp_path):
    suite, _tasks = build_suite(tmp_path / "evaluation")
    validation = matrix(suite, phase=K3HiddenWorldPhase.VALIDATION, repeats=3)
    raw = validation.model_dump(mode="json")
    raw["arms"][1]["tool_policy_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="same tool_policy_sha256"):
        validation.__class__.model_validate(raw)

    with pytest.raises(ValidationError, match="at least 5 repeats"):
        matrix(
            suite,
            phase=K3HiddenWorldPhase.TEST,
            repeats=3,
            parent=validation.manifest_sha256,
        )

    arm_raw = arms()[2].model_dump(mode="json")
    arm_raw["alternative_exclusion_gate"] = False
    with pytest.raises(ValidationError, match="requires epistemic treatment"):
        arms()[2].__class__.model_validate(arm_raw)


def test_schedule_is_blocked_paired_and_run_plans_share_exact_slots(tmp_path):
    suite, _tasks = build_suite(tmp_path / "evaluation")
    plan = matrix(suite, phase=K3HiddenWorldPhase.VALIDATION, repeats=3)
    schedule = k3_hidden_world_execution_schedule(plan)
    assert schedule == k3_hidden_world_execution_schedule(plan)
    assert len(schedule) == 4 * 3 * 3
    for block_index in range(12):
        cells = [cell for cell in schedule if cell.block_index == block_index]
        assert {cell.arm_id for cell in cells} == set(K3HiddenWorldArmId)
        assert (
            len({(cell.task_manifest_sha256, cell.repeat_index, cell.seed) for cell in cells}) == 1
        )
    run_plans = build_k3_hidden_world_run_plans(plan, suite)
    assert all(item.run_plan.slots == plan.slots for item in run_plans)


def test_signed_validation_and_test_pipeline_passes_metrics_but_public_suite_blocks_exit(tmp_path):
    root = tmp_path / "evaluation"
    suite, tasks = build_suite(root)
    test_suite, test_tasks = build_suite(root, suffix="test")
    ledger = EvaluationLedger(root / "evaluator_ledger" / "events.jsonl")
    policy = threshold_policy()

    validation = matrix(
        suite,
        phase=K3HiddenWorldPhase.VALIDATION,
        repeats=3,
        frozen_at=policy.frozen_at,
    )
    validation_result = execute(root, suite, tasks, validation, ledger)
    validation_report = aggregate_k3_hidden_world_matrix(
        matrix=validation,
        suite=suite,
        result=validation_result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    test_plan = matrix(
        test_suite,
        phase=K3HiddenWorldPhase.TEST,
        repeats=5,
        parent=validation.manifest_sha256,
    )
    config = freeze_k3_hidden_world_acceptance(
        config_id="k3-hidden-fixture-acceptance-v1",
        threshold_policy=policy,
        validation_matrix=validation,
        validation_result=validation_result,
        validation_report=validation_report,
        test_matrix=test_plan,
    )
    test_result = execute(root, test_suite, test_tasks, test_plan, ledger)
    report, decision = generate_k3_hidden_world_scientific_exit_decision(
        config=config,
        threshold_policy=policy,
        matrix=test_plan,
        suite=test_suite,
        result=test_result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    assert report.selection_audit.planned_cells == 60
    assert report.selection_audit.observed_cells == 60
    k3 = next(
        arm for arm in report.arms if arm.arm_id is K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES
    )
    assert k3.top_label_ece == pytest.approx(0.05)
    assert k3.posterior_brier_score.mean == pytest.approx(0.01)
    assert k3.false_mechanism_rate == 0
    assert all(effect.valid_pairs == 20 for effect in report.effects)
    assert report.effects[0].mean_difference == pytest.approx(0.50)
    assert report.effects[1].mean_difference == pytest.approx(0.80)
    assert decision.verdict.value == "blocked"
    blocked = {
        item.criterion_id for item in decision.criteria if item.status is K3CriterionStatus.BLOCKED
    }
    assert blocked == {"private_prospective_suite", "private_custody"}

    diagnostic_config = config.model_copy(update={"require_private_prospective_test": False})
    _diagnostic_report, diagnostic = generate_k3_hidden_world_scientific_exit_decision(
        config=diagnostic_config,
        threshold_policy=policy,
        matrix=test_plan,
        suite=test_suite,
        result=test_result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    assert diagnostic.verdict.value == "pass"

    strict_thresholds = policy.thresholds.model_copy(update={"maximum_mean_posterior_brier": 0.001})
    strict_policy = policy.model_copy(update={"thresholds": strict_thresholds})
    strict_config = diagnostic_config.model_copy(
        update={
            "threshold_policy_sha256": strict_policy.policy_sha256,
            "thresholds": strict_thresholds,
        }
    )
    _strict_report, failed = generate_k3_hidden_world_scientific_exit_decision(
        config=strict_config,
        threshold_policy=strict_policy,
        matrix=test_plan,
        suite=test_suite,
        result=test_result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    assert failed.verdict.value == "fail"
    assert (
        next(item for item in failed.criteria if item.criterion_id == "posterior_brier").status
        is K3CriterionStatus.FAIL
    )

    omitted = test_result.model_copy(update={"attempts": test_result.attempts[:-1]})
    with pytest.raises(K3HiddenWorldError, match="retain every ledger attempt"):
        aggregate_k3_hidden_world_matrix(
            matrix=test_plan,
            suite=test_suite,
            result=omitted,
            ledger=ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
        )

    late = diagnostic_config.model_copy(
        update={"calibrated_at": test_result.started_at + timedelta(days=1)}
    )
    _late_report, late_decision = generate_k3_hidden_world_scientific_exit_decision(
        config=late,
        threshold_policy=policy,
        matrix=test_plan,
        suite=test_suite,
        result=test_result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
        decided_at=utcnow() + timedelta(days=2),
    )
    assert late_decision.verdict.value == "fail"


def test_acceptance_freeze_rejects_post_validation_thresholds(tmp_path):
    root = tmp_path / "evaluation"
    suite, tasks = build_suite(root)
    ledger = EvaluationLedger(root / "evaluator_ledger" / "events.jsonl")
    validation = matrix(suite, phase=K3HiddenWorldPhase.VALIDATION, repeats=3)
    result = execute(root, suite, tasks, validation, ledger)
    report = aggregate_k3_hidden_world_matrix(
        matrix=validation,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    test_plan = matrix(
        suite,
        phase=K3HiddenWorldPhase.TEST,
        repeats=5,
        parent=validation.manifest_sha256,
    )
    late_policy = threshold_policy().model_copy(
        update={"frozen_at": result.started_at + timedelta(seconds=1)}
    )
    with pytest.raises(K3HiddenWorldError, match="after validation access"):
        freeze_k3_hidden_world_acceptance(
            config_id="late-policy",
            threshold_policy=late_policy,
            validation_matrix=validation,
            validation_result=result,
            validation_report=report,
            test_matrix=test_plan,
        )


def test_roadmap_cli_validates_frozen_protocol_and_reports_real_blockers():
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/real_k3_hidden_world_e2e.py",
            "--suite",
            "configs/evals/k3_hidden_world_v1.yaml",
            "--repeats",
            "5",
            "--frozen",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["state"] == "protocol_frozen"
    assert payload["scientific_exit_readiness"] == "blocked"
    assert isinstance(payload["validation_suite_exists"], bool)
    assert payload["test_suite_exists"] is False
    assert (
        "private_prospective_hidden_law_test_suite" in payload["unresolved_scientific_exit_inputs"]
    )
