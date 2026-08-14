"""F7 issue 9 four-arm preregistration, paired execution, and no-best-of-N audit."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.evals.baselines import (
    AgentScaffold,
    BaselineAnalysisPolicy,
    BaselineArm,
    BaselineArmId,
    BaselineMatrixError,
    BaselineMatrixPlan,
    BaselineMatrixRunner,
    BaselineMismatchDisclosure,
    ComparabilityDimension,
    MatrixPhase,
    baseline_execution_schedule,
    build_baseline_run_plans,
)
from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.statistics import aggregate_baseline_matrix
from aletheia.evals.schemas import AttemptStatus

from .f7s2_fixtures import (
    EVALUATOR_HASH,
    SIGNING_KEY,
    ExactAnswerScorer,
    HardExecutor,
    build_case,
    infra_error,
    write_submission,
)


def _arms() -> tuple[BaselineArm, ...]:
    shared = {
        "base_model_manifest_sha256": "a" * 64,
        "tool_policy_sha256": "1" * 64,
        "budget_policy_sha256": "2" * 64,
        "wall_time_policy_sha256": "3" * 64,
        "tool_names": (),
    }
    return (
        BaselineArm(
            arm_id=BaselineArmId.DIRECT_MODEL,
            system_manifest_sha256="6" * 64,
            agent_scaffold=AgentScaffold.DIRECT,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="b" * 64,
            description="The frozen base model receives the task directly.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.GENERIC_AGENT,
            system_manifest_sha256="7" * 64,
            agent_scaffold=AgentScaffold.GENERIC,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="c" * 64,
            description="A generic research agent without Aletheia campaign learning.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.ALETHEIA_NO_K2,
            system_manifest_sha256="8" * 64,
            agent_scaffold=AgentScaffold.ALETHEIA,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="d" * 64,
            description="Aletheia with the K2 campaign-learning treatment disabled.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.ALETHEIA_FULL_K2,
            system_manifest_sha256="9" * 64,
            agent_scaffold=AgentScaffold.ALETHEIA,
            campaign_learning_enabled=True,
            k2_enabled=True,
            prompt_manifest_sha256="e" * 64,
            description="The complete Aletheia K2 system.",
            **shared,
        ),
    )


def _matrix(suite, slots, *, arms=None, disclosures=(), phase=MatrixPhase.VALIDATION):
    return BaselineMatrixPlan(
        matrix_id="frontier-four-arm-validation-v1",
        suite_manifest_sha256=suite.manifest_sha256,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        phase=phase,
        parent_validation_matrix_sha256=("f" * 64 if phase is MatrixPhase.TEST else None),
        arms=arms or _arms(),
        slots=slots,
        max_infra_retries_per_slot=1,
        block_randomization_seed=20260814,
        analysis=BaselineAnalysisPolicy(
            secondary_objective_metrics=("exact",),
            bootstrap_resamples=100,
            bootstrap_seed=7409,
        ),
        mismatch_disclosures=disclosures,
        frozen_at=datetime.now(timezone.utc),
    )


def _runner(base_runner, ledger, executor):
    return IndependentEvaluationRunner(
        root=base_runner.root,
        ledger=ledger,
        executor=executor,
        scorer=ExactAnswerScorer(),
        evaluator_manifest_sha256=EVALUATOR_HASH,
        receipt_key_id="test-evaluator-key",
        receipt_signing_key=SIGNING_KEY,
    )


def _runners(base_runner, ledger):
    def generic(context):
        write_submission(
            context,
            answer="42" if context.request.repeat_index % 2 == 0 else "wrong",
        )

    def no_k2(context):
        write_submission(
            context,
            answer="42" if context.request.repeat_index == 0 else "wrong",
        )

    return {
        BaselineArmId.DIRECT_MODEL: _runner(
            base_runner,
            ledger,
            HardExecutor(
                lambda context: write_submission(context, answer="wrong"),
                cost_usd=0.10,
                usage_metering="provider_receipt",
            ),
        ),
        BaselineArmId.GENERIC_AGENT: _runner(
            base_runner,
            ledger,
            HardExecutor(generic, cost_usd=0.20, usage_metering="provider_receipt"),
        ),
        BaselineArmId.ALETHEIA_NO_K2: _runner(
            base_runner,
            ledger,
            HardExecutor(no_k2, cost_usd=0.30, usage_metering="provider_receipt"),
        ),
        BaselineArmId.ALETHEIA_FULL_K2: _runner(
            base_runner,
            ledger,
            HardExecutor(write_submission, cost_usd=0.40, usage_metering="provider_receipt"),
        ),
    }


def _executed_matrix(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    matrix = _matrix(suite, plan.slots)
    orchestrator = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=_runners(base_runner, ledger),
    )
    result = orchestrator.run()
    return matrix, suite, result, ledger


def test_preregistration_requires_exact_four_semantic_arms_and_one_base_model(tmp_path):
    _runner_value, suite, plan, _task, _ledger = build_case(tmp_path, repeats=3)
    arms = list(_arms())
    arms[0] = arms[0].model_copy(update={"base_model_manifest_sha256": "4" * 64})
    with pytest.raises(ValidationError, match="same frozen base model"):
        _matrix(suite, plan.slots, arms=tuple(arms))

    with pytest.raises(ValidationError, match="scaffold/campaign/K2"):
        _arms()[0].model_copy(
            update={"agent_scaffold": AgentScaffold.ALETHEIA}
        ).__class__.model_validate(
            _arms()[0].model_copy(update={"agent_scaffold": AgentScaffold.ALETHEIA}).model_dump()
        )

    with pytest.raises(ValidationError, match="canonical order"):
        _matrix(suite, plan.slots, arms=tuple(reversed(_arms())))


def test_test_phase_requires_five_repeats_and_validation_parent(tmp_path):
    _runner_value, suite, plan, _task, _ledger = build_case(tmp_path, repeats=3)
    with pytest.raises(ValidationError, match="at least 5 repeats"):
        _matrix(suite, plan.slots, phase=MatrixPhase.TEST)

    _runner_value, suite, plan, _task, _ledger = build_case(tmp_path / "five", repeats=5)
    raw = _matrix(suite, plan.slots, phase=MatrixPhase.TEST).model_dump()
    raw["parent_validation_matrix_sha256"] = None
    with pytest.raises(ValidationError, match="reference its frozen validation"):
        BaselineMatrixPlan.model_validate(raw)


def test_non_model_mismatch_requires_exact_conditional_disclosure(tmp_path):
    _runner_value, suite, plan, _task, _ledger = build_case(tmp_path, repeats=3)
    arms = list(_arms())
    arms[1] = arms[1].model_copy(update={"tool_policy_sha256": "4" * 64, "tool_names": ("web",)})
    with pytest.raises(ValidationError, match="missing=.*generic_agent:tools"):
        _matrix(suite, plan.slots, arms=tuple(arms))

    disclosure = BaselineMismatchDisclosure(
        comparator_arm=BaselineArmId.GENERIC_AGENT,
        dimension=ComparabilityDimension.TOOLS,
        rationale="The generic harness exposes its upstream fixed search tool.",
        mitigation="Report this comparison as conditional and retain the matched direct/no-K2 arms.",
    )
    matrix = _matrix(
        suite,
        plan.slots,
        arms=tuple(arms),
        disclosures=(disclosure,),
    )
    assert matrix.mismatch_disclosures == (disclosure,)


def test_schedule_is_deterministic_blocked_and_run_plans_share_slots(tmp_path):
    _runner_value, suite, plan, _task, _ledger = build_case(tmp_path, repeats=3)
    matrix = _matrix(suite, plan.slots)
    schedule = baseline_execution_schedule(matrix)
    assert schedule == baseline_execution_schedule(matrix)
    assert len(schedule) == 12
    for block in range(3):
        cells = [cell for cell in schedule if cell.block_index == block]
        assert {cell.arm_id for cell in cells} == set(BaselineArmId)
        assert (
            len({(cell.task_manifest_sha256, cell.repeat_index, cell.seed) for cell in cells}) == 1
        )

    plans = build_baseline_run_plans(matrix, suite)
    assert len({item.run_plan.system_manifest_sha256 for item in plans}) == 4
    assert all(item.run_plan.slots == matrix.slots for item in plans)


def test_complete_matrix_executes_and_aggregates_all_paired_results(tmp_path):
    matrix, suite, result, ledger = _executed_matrix(tmp_path)
    assert len(result.attempts) == 12
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )

    assert report.selection_audit.planned_cells == 12
    assert report.selection_audit.retained_attempts == 12
    assert report.selection_audit.complete is True
    summaries = {arm.arm_id: arm for arm in report.arms}
    assert summaries[BaselineArmId.DIRECT_MODEL].pass_at_1 == 0
    assert summaries[BaselineArmId.GENERIC_AGENT].pass_at_1 == pytest.approx(2 / 3)
    assert summaries[BaselineArmId.ALETHEIA_NO_K2].pass_at_1 == pytest.approx(1 / 3)
    assert summaries[BaselineArmId.ALETHEIA_FULL_K2].pass_at_1 == 1
    assert summaries[BaselineArmId.ALETHEIA_FULL_K2].observed_cost_usd.total == pytest.approx(1.2)

    comparisons = {comparison.comparator_arm: comparison for comparison in report.comparisons}
    direct = comparisons[BaselineArmId.DIRECT_MODEL]
    assert direct.valid_scientific_pairs == 3
    assert direct.candidate_wins == 3
    assert direct.scientific_risk_difference == 1
    assert direct.operational_risk_difference == 1
    assert direct.mean_paired_cost_difference_usd == pytest.approx(0.30)
    assert direct.unconditional_claim_allowed is True
    assert direct.secondary_objective_effects[0].mean_difference == 1


def test_matrix_runner_resumes_a_terminal_schedule_prefix_without_rerunning_it(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    matrix = _matrix(suite, plan.slots)
    runners = _runners(base_runner, ledger)
    orchestrator = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=runners,
    )
    first_cell = baseline_execution_schedule(matrix)[0]
    run_plan = next(
        item.run_plan for item in orchestrator.run_plans if item.arm_id is first_cell.arm_id
    )
    first = runners[first_cell.arm_id].run(
        suite=suite,
        plan=run_plan,
        task=task,
        repeat_index=first_cell.repeat_index,
    )

    resumed = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=runners,
    ).run()
    assert len(resumed.attempts) == 12
    assert (
        sum(record.attempt.attempt_id == first.attempt.attempt_id for record in resumed.attempts)
        == 1
    )
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=resumed,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    assert report.selection_audit.retained_attempts == 12


def test_matrix_resume_fails_closed_on_a_nonterminal_residual_attempt(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    matrix = _matrix(suite, plan.slots)
    runners = _runners(base_runner, ledger)
    orchestrator = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=runners,
    )
    first_cell = baseline_execution_schedule(matrix)[0]
    run_plan = next(
        item.run_plan for item in orchestrator.run_plans if item.arm_id is first_cell.arm_id
    )
    slot = next(
        item
        for item in run_plan.slots
        if item.task_manifest_sha256 == task.manifest_sha256
        and item.repeat_index == first_cell.repeat_index
    )
    attempt = runners[first_cell.arm_id]._new_attempt(  # noqa: SLF001
        plan=run_plan,
        task=task,
        slot=slot,
        retry_of=None,
        intervention_count=0,
    )
    ledger.register_plan(run_plan)
    ledger.claim_attempt(
        attempt,
        slot_sha256=slot.slot_sha256,
        retry_of_attempt_id=None,
        max_infra_retries=run_plan.max_infra_retries_per_slot,
    )

    with pytest.raises(BaselineMatrixError, match="nonterminal attempts"):
        orchestrator.run()


def test_automatic_retry_retains_infrastructure_failure_instead_of_selecting_it_away(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    calls = 0

    def flaky_direct(context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise infra_error("one evaluator daemon interruption")
        write_submission(context, answer="wrong")

    runners = _runners(base_runner, ledger)
    runners[BaselineArmId.DIRECT_MODEL] = _runner(
        base_runner,
        ledger,
        HardExecutor(flaky_direct, cost_usd=0.10, usage_metering="provider_receipt"),
    )
    matrix = _matrix(suite, plan.slots)
    result = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=runners,
    ).run()
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    assert len(result.attempts) == 13
    assert (
        sum(record.attempt.status is AttemptStatus.INFRA_FAILURE for record in result.attempts) == 1
    )
    assert report.selection_audit.authorized_infrastructure_retries == 1
    direct = next(arm for arm in report.arms if arm.arm_id is BaselineArmId.DIRECT_MODEL)
    assert direct.retained_attempts == 4
    assert direct.all_attempt_status_counts[AttemptStatus.INFRA_FAILURE.value] == 1


def test_invalid_attempts_are_reported_but_not_relabelled_scientific_failures(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    runners = _runners(base_runner, ledger)
    runners[BaselineArmId.DIRECT_MODEL] = _runner(
        base_runner,
        ledger,
        HardExecutor(None, cost_usd=0.10, usage_metering="provider_receipt"),
    )
    matrix = _matrix(suite, plan.slots)
    result = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=runners,
    ).run()
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    direct = next(arm for arm in report.arms if arm.arm_id is BaselineArmId.DIRECT_MODEL)
    assert direct.final_status_counts == {AttemptStatus.INVALID.value: 3}
    assert direct.unscored_invalid_attempts == 3
    assert direct.scientific_valid_cells == 0
    assert direct.scientific_success_rate is None
    comparison = next(
        item for item in report.comparisons if item.comparator_arm is BaselineArmId.DIRECT_MODEL
    )
    assert comparison.valid_scientific_pairs == 0
    assert comparison.excluded_scientific_pairs == 3
    assert comparison.operational_risk_difference == 1


def test_disclosed_mismatch_survives_into_claim_scope(tmp_path):
    base_runner, suite, plan, task, ledger = build_case(tmp_path, repeats=3)
    arms = list(_arms())
    arms[1] = arms[1].model_copy(update={"budget_policy_sha256": "4" * 64})
    disclosure = BaselineMismatchDisclosure(
        comparator_arm=BaselineArmId.GENERIC_AGENT,
        dimension=ComparabilityDimension.BUDGET,
        rationale="The upstream generic-agent provider exposes only a coarser fixed budget tier.",
        mitigation="Retain the result only as a conditional diagnostic comparison.",
    )
    matrix = _matrix(
        suite,
        plan.slots,
        arms=tuple(arms),
        disclosures=(disclosure,),
    )
    result = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks={task.manifest_sha256: task},
        runners=_runners(base_runner, ledger),
    ).run()
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys={"test-evaluator-key": SIGNING_KEY},
    )
    comparison = next(
        item for item in report.comparisons if item.comparator_arm is BaselineArmId.GENERIC_AGENT
    )
    assert comparison.comparability_mismatches == (ComparabilityDimension.BUDGET,)
    assert comparison.unconditional_claim_allowed is False


def test_aggregation_rejects_omitted_attempt_and_bad_signature(tmp_path):
    matrix, suite, result, ledger = _executed_matrix(tmp_path)
    omitted = result.model_copy(update={"attempts": result.attempts[:-1]})
    with pytest.raises(BaselineMatrixError, match="retain every ledger attempt"):
        aggregate_baseline_matrix(
            matrix=matrix,
            suite=suite,
            result=omitted,
            ledger=ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
        )

    target = result.attempts[0]
    assert target.scorer_receipt is not None
    forged_envelope = target.scorer_receipt.model_copy(update={"hmac_sha256": "0" * 64})
    forged_record = target.model_copy(update={"scorer_receipt": forged_envelope})
    forged = result.model_copy(update={"attempts": (forged_record, *result.attempts[1:])})
    with pytest.raises(BaselineMatrixError, match="signature is invalid"):
        aggregate_baseline_matrix(
            matrix=matrix,
            suite=suite,
            result=forged,
            ledger=ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
        )


def test_aggregation_rejects_a_ledger_changed_after_result_sealing(tmp_path):
    matrix, suite, result, ledger = _executed_matrix(tmp_path)
    ledger.append(
        "public_assets_staged",
        {"assets": [], "public_task_manifest_sha256": "a" * 64},
        attempt_id="unrelated-later-audit-event",
    )
    with pytest.raises(BaselineMatrixError, match="ledger changed"):
        aggregate_baseline_matrix(
            matrix=matrix,
            suite=suite,
            result=result,
            ledger=ledger,
            receipt_keys={"test-evaluator-key": SIGNING_KEY},
        )


def test_cli_materializes_and_aggregates_a_frozen_matrix(tmp_path):
    matrix, suite, result, ledger = _executed_matrix(tmp_path / "execution")
    matrix_path = tmp_path / "matrix.json"
    suite_path = tmp_path / "suite.json"
    result_path = tmp_path / "result.json"
    materialized_path = tmp_path / "materialized.json"
    report_path = tmp_path / "report.json"
    matrix_path.write_text(matrix.model_dump_json(), encoding="utf-8")
    suite_path.write_text(
        json.dumps({"suite": suite.model_dump(mode="json"), "tasks": []}),
        encoding="utf-8",
    )
    result_path.write_text(result.model_dump_json(), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.getcwd()

    materialize = subprocess.run(
        [
            sys.executable,
            "scripts/run_baseline_matrix.py",
            "materialize",
            "--matrix",
            str(matrix_path),
            "--suite-bundle",
            str(suite_path),
            "--output",
            str(materialized_path),
        ],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert materialize.returncode == 0, materialize.stderr
    materialized = json.loads(materialized_path.read_text())
    assert materialized["matrix_manifest_sha256"] == matrix.manifest_sha256
    assert len(materialized["run_plans"]) == 4
    assert len(materialized["schedule"]) == 12

    environment["BASELINE_TEST_RECEIPT_KEY"] = base64.b64encode(SIGNING_KEY).decode()
    aggregate = subprocess.run(
        [
            sys.executable,
            "scripts/run_baseline_matrix.py",
            "aggregate",
            "--matrix",
            str(matrix_path),
            "--suite-bundle",
            str(suite_path),
            "--result",
            str(result_path),
            "--ledger",
            str(ledger.path),
            "--receipt-key-env",
            "test-evaluator-key=BASELINE_TEST_RECEIPT_KEY",
            "--output",
            str(report_path),
        ],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert aggregate.returncode == 0, aggregate.stderr
    report = json.loads(report_path.read_text())
    assert report["selection_audit"]["complete"] is True
    assert report["selection_audit"]["retained_attempts"] == 12
