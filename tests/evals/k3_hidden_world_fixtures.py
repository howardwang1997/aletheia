"""Fast signed-receipt fixtures for the F9 hidden-world ablation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aletheia.evals.adapters.discoveryworld import (
    DiscoveryWorldHarnessResult,
    DiscoveryWorldScientificExitMetrics,
)
from aletheia.evals.k3_hidden_world import (
    K3HiddenWorldAnalysisPolicy,
    K3HiddenWorldArm,
    K3HiddenWorldArmId,
    K3HiddenWorldMatrixPlan,
    K3HiddenWorldMatrixRunner,
    K3HiddenWorldPhase,
    K3HiddenWorldSuiteKind,
    K3HiddenWorldThresholdPolicy,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.schemas import (
    ArtifactRequirement,
    ContaminationPolicy,
    EvalLayer,
    EvaluationAttemptSlot,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    ExecutionExitReason,
    ResourceBudget,
    SubmittedArtifact,
    content_sha256,
)

from .f7s2_fixtures import EVALUATOR_HASH, HardExecutor, SIGNING_KEY


SCORER_HASH = "f" * 64
SOURCE_HASH = "e" * 64
HARNESS_HASH = "d" * 64
ARM_SYSTEMS = {
    K3HiddenWorldArmId.HEADLINE_METRIC: "6" * 64,
    K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS: "7" * 64,
    K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES: "8" * 64,
}
SYSTEM_ARMS = {value: key for key, value in ARM_SYSTEMS.items()}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def arms() -> tuple[K3HiddenWorldArm, ...]:
    shared = {
        "base_model_manifest_sha256": "a" * 64,
        "base_task_prompt_sha256": "b" * 64,
        "tool_policy_sha256": "1" * 64,
        "budget_policy_sha256": "2" * 64,
        "wall_time_policy_sha256": "3" * 64,
        "sampling_policy_sha256": "4" * 64,
        "tool_names": (),
    }
    return (
        K3HiddenWorldArm(
            arm_id=K3HiddenWorldArmId.HEADLINE_METRIC,
            system_manifest_sha256=ARM_SYSTEMS[K3HiddenWorldArmId.HEADLINE_METRIC],
            treatment_prompt_sha256="5" * 64,
            epistemic_state="none",
            prediction_policy="none",
            experiment_selection_policy="headline_metric",
            alternative_exclusion_gate=False,
            description="Optimize only the public headline task objective.",
            **shared,
        ),
        K3HiddenWorldArm(
            arm_id=K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS,
            system_manifest_sha256=ARM_SYSTEMS[K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS],
            treatment_prompt_sha256="9" * 64,
            epistemic_state="single_proposition_beta",
            prediction_policy="single_proposition",
            experiment_selection_policy="single_proposition_eig",
            alternative_exclusion_gate=False,
            description="Use the historical single-proposition K2 belief loop.",
            **shared,
        ),
        K3HiddenWorldArm(
            arm_id=K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES,
            system_manifest_sha256=ARM_SYSTEMS[K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES],
            treatment_prompt_sha256="c" * 64,
            epistemic_state="versioned_competing_hypotheses",
            prediction_policy="per_hypothesis_preobservation",
            experiment_selection_policy="multi_hypothesis_eig_discrimination",
            alternative_exclusion_gate=True,
            description="Use the complete F9 competing-hypothesis treatment.",
            **shared,
        ),
    )


class HiddenEndpointScorer:
    @property
    def scorer_sha256(self) -> str:
        return SCORER_HASH

    def score(self, *, submission, artifacts, **_kwargs) -> EvaluationScore:
        arm_id = SYSTEM_ARMS[submission.system_manifest_sha256]
        _payload = json.loads(artifacts["answer"])
        values = {
            K3HiddenWorldArmId.HEADLINE_METRIC: (0.15, 0.10, 0.30, 0.70, False),
            K3HiddenWorldArmId.K2_SINGLE_HYPOTHESIS: (0.40, 0.30, 0.20, 0.80, True),
            K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES: (0.90, 0.90, 0.01, 0.95, True),
        }[arm_id]
        elimination, discrimination, brier, confidence, success = values
        trace_sha256 = hashlib.sha256(b"[]").hexdigest()
        metrics = DiscoveryWorldScientificExitMetrics(
            source_trace_sha256=trace_sha256,
            trace_complete=True,
            correct_hypothesis_preserved=True,
            posterior_brier_score=brier,
            top_label_confidence=confidence,
            top_label_correct=success,
            mechanism_claimed=True,
            false_mechanism_claim=False,
            genuine_discriminating_trials=3
            if arm_id is K3HiddenWorldArmId.K3_COMPETING_HYPOTHESES
            else 1,
            discriminating_trial_rate=discrimination,
            wrong_explanation_elimination_score=elimination,
            informative_trials_to_identification=2,
            hypothesis_space_contracted=True,
        )
        results = []
        for run_index in range(2):
            results.append(
                DiscoveryWorldHarnessResult(
                    instance_id="fixture-hidden-law",
                    run_index=run_index,
                    candidate_image_id="sha256:" + "1" * 64,
                    environment_image_id="sha256:" + "2" * 64,
                    program_returncode=0,
                    program_exit_reason=ExecutionExitReason.COMPLETED,
                    program_wall_time_s=0.01,
                    program_log_sha256="3" * 64,
                    environment_log_sha256="4" * 64,
                    protocol_valid=True,
                    stopped=True,
                    task_completed=success,
                    completed_successfully=success,
                    procedural_score=float(success),
                    final_hypothesis_id="substance_a",
                    explicit_rule_discovery=success,
                    trace_sha256=trace_sha256,
                    trace=(),
                )
            )
        evidence_objects = {
            **{
                f"harness_run_{result.run_index}": result.model_dump(mode="json", exclude_none=True)
                for result in results
            },
            "scientific_exit_metrics": metrics.model_dump(mode="json"),
        }
        evidence_sha256s = {name: content_sha256(value) for name, value in evidence_objects.items()}
        return EvaluationScore(
            objective_scores={
                "reproducible": 1.0,
                "posterior_brier_score": metrics.posterior_brier_score,
                "top_label_confidence": metrics.top_label_confidence,
                "top_label_correct": float(metrics.top_label_correct),
                "mechanism_claim_coverage": float(metrics.mechanism_claimed),
                "false_mechanism_rate": float(metrics.false_mechanism_claim),
                "genuine_discriminating_trials": float(metrics.genuine_discriminating_trials),
                "discriminating_trial_rate": metrics.discriminating_trial_rate,
                "wrong_explanation_elimination_score": (
                    metrics.wrong_explanation_elimination_score
                ),
                "hypothesis_space_contracted": float(metrics.hypothesis_space_contracted),
            },
            evidence_sha256s=evidence_sha256s,
            evidence_objects=evidence_objects,
            scientific_success=success,
        )


def write_submission(context) -> None:
    raw = json.dumps({"repeat": context.request.repeat_index}).encode()
    path = context.submission_inbox / "answer.json"
    path.write_bytes(raw)
    submission = EvaluationSubmission(
        attempt_id=context.request.attempt_id,
        task_manifest_sha256=context.request.public_task.task_manifest_sha256,
        system_manifest_sha256=context.request.system_manifest_sha256,
        artifacts=(
            SubmittedArtifact(
                kind="answer",
                media_type="application/json",
                uri="inbox://answer.json",
                sha256=hashlib.sha256(raw).hexdigest(),
                bytes=len(raw),
            ),
        ),
        submitted_at=utcnow(),
    )
    (context.submission_inbox / "submission.json").write_text(submission.model_dump_json())


def build_suite(
    root: Path, *, suffix: str = "validation"
) -> tuple[EvaluationSuite, dict[str, EvaluationTask]]:
    tasks: list[EvaluationTask] = []
    for index in range(4):
        hidden_bytes = json.dumps({"hidden_law": index}).encode()
        hidden_path = root / "hidden_assets" / f"law-{suffix}-{index}.json"
        hidden_path.parent.mkdir(parents=True, exist_ok=True)
        hidden_path.write_bytes(hidden_bytes)
        tasks.append(
            EvaluationTask(
                task_id=f"hidden-law-{suffix}-{index}",
                version="1.0.0",
                layer=EvalLayer.HIDDEN_RULE_DISCOVERY,
                public_prompt="Discover the hidden governing law.",
                hidden_asset_ref=f"evaluator://hidden/law-{suffix}-{index}.json",
                hidden_asset_sha256=hashlib.sha256(hidden_bytes).hexdigest(),
                resource_budget=ResourceBudget(wall_time_s=10, cpu_seconds=5, memory_mb=128),
                expected_artifacts=(
                    ArtifactRequirement(
                        kind="answer", media_type="application/json", max_bytes=1024
                    ),
                ),
                scorer_ref="evaluator://scorers/k3-hidden-fixture",
                scorer_sha256=SCORER_HASH,
                contamination_policy=ContaminationPolicy(test_access_limit=100),
            )
        )
    suite = EvaluationSuite(
        suite_id=f"k3-hidden-world-fixture-{suffix}",
        version="1.0.0",
        task_manifest_sha256s=tuple(task.manifest_sha256 for task in tasks),
        scoring_policy_sha256="5" * 64,
    )
    return suite, {task.manifest_sha256: task for task in tasks}


def matrix(
    suite: EvaluationSuite,
    *,
    phase: K3HiddenWorldPhase,
    repeats: int,
    parent: str | None = None,
    frozen_at: datetime | None = None,
) -> K3HiddenWorldMatrixPlan:
    return K3HiddenWorldMatrixPlan(
        matrix_id=f"k3-hidden-fixture-{phase.value}",
        suite_manifest_sha256=suite.manifest_sha256,
        discoveryworld_source_manifest_sha256=SOURCE_HASH,
        harness_manifest_sha256=HARNESS_HASH,
        scorer_sha256=SCORER_HASH,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        suite_kind=K3HiddenWorldSuiteKind.PUBLIC_DIAGNOSTIC,
        phase=phase,
        parent_validation_matrix_sha256=parent,
        arms=arms(),
        slots=tuple(
            EvaluationAttemptSlot(
                task_manifest_sha256=task_hash,
                repeat_index=repeat,
                seed=1000 + task_index * 100 + repeat,
            )
            for task_index, task_hash in enumerate(suite.task_manifest_sha256s)
            for repeat in range(repeats)
        ),
        block_randomization_seed=20260815,
        analysis=K3HiddenWorldAnalysisPolicy(
            bootstrap_resamples=100,
            bootstrap_seed=7409,
        ),
        frozen_at=frozen_at or utcnow(),
    )


def runners(root: Path, ledger: EvaluationLedger):
    return {
        arm_id: IndependentEvaluationRunner(
            root=root,
            ledger=ledger,
            executor=HardExecutor(write_submission),
            scorer=HiddenEndpointScorer(),
            evaluator_manifest_sha256=EVALUATOR_HASH,
            receipt_key_id="test-evaluator-key",
            receipt_signing_key=SIGNING_KEY,
        )
        for arm_id in K3HiddenWorldArmId
    }


def execute(root: Path, suite, tasks, plan, ledger):
    return K3HiddenWorldMatrixRunner(
        matrix=plan,
        suite=suite,
        tasks=tasks,
        runners=runners(root, ledger),
    ).run()


def threshold_policy() -> K3HiddenWorldThresholdPolicy:
    return K3HiddenWorldThresholdPolicy(
        policy_id="k3-hidden-world-thresholds-v1",
        rationale=(
            "Freeze paired superiority, calibration, false-mechanism, coverage, and contraction "
            "limits before any validation attempt starts."
        ),
        frozen_at=utcnow() - timedelta(days=1),
    )
