"""Six F7-S3 outcomes plus hidden-rule trajectory metrics for DiscoveryWorld."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from aletheia.evals.adapters.discoveryworld import (
    DISCOVERYWORLD_CANARY,
    DiscoveryWorldHarnessResult,
    DiscoveryWorldScorer,
)
from aletheia.evals.schemas import (
    EvaluationSubmission,
    EvaluationTask,
    ExecutionExitReason,
    InvalidReason,
    SubmittedArtifact,
)

from .test_discoveryworld_contract import manifest, receipt, source


EMPTY_TRACE_SHA = hashlib.sha256(b"[]").hexdigest()


def task() -> EvaluationTask:
    return EvaluationTask(
        task_id="discoveryworld-chem-easy-test",
        version="test",
        layer=3,
        public_prompt="Discover the hidden rust-removal rule.",
        hidden_asset_ref="evaluator://hidden/discoveryworld/test.json",
        hidden_asset_sha256="a" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=(
            {
                "kind": "agent_program",
                "media_type": "text/x-python",
                "max_bytes": 1 << 20,
            },
        ),
        scorer_ref="evaluator://scorers/discoveryworld",
        scorer_sha256="b" * 64,
    )


def submission(program: bytes, *, contamination: tuple[str, ...] = ()):
    return EvaluationSubmission(
        attempt_id="attempt-1",
        task_manifest_sha256="c" * 64,
        system_manifest_sha256="d" * 64,
        artifacts=(
            SubmittedArtifact(
                kind="agent_program",
                media_type="text/x-python",
                uri="inbox://agent.py",
                sha256=hashlib.sha256(program).hexdigest(),
                bytes=len(program),
            ),
        ),
        submitted_at=datetime.now(timezone.utc),
        declared_contamination=contamination,
    )


@dataclass
class ScriptedHarness:
    results: list[dict]
    calls: list[int] = field(default_factory=list)

    @property
    def manifest(self):
        return manifest(reproduction_runs=len(self.results))

    def evaluate(self, *, receipt, program, run_index):
        self.calls.append(run_index)
        payload = {
            "instance_id": receipt.instance_id,
            "run_index": run_index,
            "candidate_image_id": self.manifest.candidate_image_id,
            "environment_image_id": self.manifest.environment_image_id,
            "program_returncode": 0,
            "program_exit_reason": ExecutionExitReason.COMPLETED,
            "program_wall_time_s": 0.01,
            "program_log_sha256": hashlib.sha256(program).hexdigest(),
            "environment_log_sha256": "e" * 64,
            "trace_sha256": EMPTY_TRACE_SHA,
        }
        payload.update(self.results[run_index])
        return DiscoveryWorldHarnessResult.model_validate(payload)


def outcome(
    *,
    completion: bool,
    rule: bool,
    trace_sha256: str = EMPTY_TRACE_SHA,
    exit_reason: ExecutionExitReason = ExecutionExitReason.COMPLETED,
    protocol_valid: bool = True,
) -> dict:
    return {
        "program_returncode": 0 if exit_reason is ExecutionExitReason.COMPLETED else 1,
        "program_exit_reason": exit_reason,
        "program_timed_out": exit_reason is ExecutionExitReason.WALL_TIME_LIMIT,
        "protocol_valid": protocol_valid,
        "terminal_reason": "candidate_stopped",
        "stopped": True,
        "task_completed": completion,
        "completed_successfully": completion,
        "procedural_score": 1.0 if completion else 0.25,
        "final_hypothesis_id": "substance_b" if rule else "substance_a",
        "explicit_rule_discovery": rule,
        "action_count": 8,
        "valid_action_count": 7,
        "invalid_action_count": 1,
        "informative_trials": 2,
        "distinct_hypotheses_tested": 2,
        "objective_hypotheses_remaining": 1 if rule else 3,
        "objective_entropy_final_bits": 0.0 if rule else 1.584962500721156,
        "objective_information_gain_bits": 2.0 if rule else 0.415037499278844,
        "information_gain_bits_per_action": 0.25 if rule else 0.0518796874098555,
        "reported_entropy_initial_bits": 2.0,
        "reported_entropy_final_bits": 0.0 if rule else 1.0,
        "hypothesis_revision_count": 1,
        "revision_opportunities": 1,
        "successful_revisions": 1,
        "grounded_belief_updates": 1,
        "ungrounded_belief_updates": 0,
        "trace_sha256": trace_sha256,
    }


def run_score(results, program=b"print('policy')\n", *, declared=()):
    release = source()
    harness = ScriptedHarness(results)
    scorer = DiscoveryWorldScorer(harness=harness, source_manifest_sha256=release.manifest_sha256)
    score = scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(program, contamination=declared),
        artifacts={"agent_program": program},
    )
    return score, harness


def test_correct_terminal_task_and_explicit_rule_are_scientific_success():
    same = outcome(completion=True, rule=True)
    score, harness = run_score([same, same])
    assert score.scientific_success is True
    assert score.objective_scores["task_completion"] == 1
    assert score.objective_scores["explicit_rule_discovery"] == 1
    assert score.objective_scores["objective_information_gain_bits"] == 2
    assert score.objective_scores["information_gain_bits_per_action"] == 0.25
    assert score.objective_scores["hypothesis_revision_rate"] == 1
    assert harness.calls == [0, 1]


def test_only_running_or_wrong_rule_is_scientific_false_not_invalid():
    same = outcome(completion=False, rule=False)
    score, _ = run_score([same, same])
    assert score.scientific_success is False
    assert score.invalid_reasons == ()
    assert score.objective_scores["procedural_progress"] == 0.25

    solved_by_luck = outcome(completion=True, rule=False)
    score, _ = run_score([solved_by_luck, solved_by_luck])
    assert score.scientific_success is False
    assert score.objective_scores["task_completion"] == 1
    assert score.objective_scores["explicit_rule_discovery"] == 0


@pytest.mark.parametrize(
    "program",
    [
        f"print('{DISCOVERYWORLD_CANARY}')\n".encode(),
        b"from discoveryworld import DiscoveryWorldAPI\n",
        b"print('criticalHypotheses')\n",
        b"print('getTaskScorecard')\n",
        b"open('/hidden/contract.json').read()\n",
        b"print('chemicalSolutionDict')\n",
    ],
)
def test_peeking_and_canary_are_contamination_without_world_access(program):
    score, harness = run_score([outcome(completion=True, rule=True)] * 2, program)
    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert score.scientific_success is None
    assert harness.calls == []


def test_declared_public_answer_overlap_is_contamination():
    score, harness = run_score(
        [outcome(completion=True, rule=True)] * 2,
        declared=("memorized official seed answer",),
    )
    assert score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert harness.calls == []


def test_missing_agent_program_is_invalid():
    release = source()
    harness = ScriptedHarness([outcome(completion=True, rule=True)] * 2)
    scorer = DiscoveryWorldScorer(harness=harness, source_manifest_sha256=release.manifest_sha256)
    score = scorer.score(
        task=task(),
        hidden_asset=receipt(release).to_bytes(),
        submission=submission(b"print('x')\n"),
        artifacts={},
    )
    assert score.invalid_reasons == (InvalidReason.MISSING_ARTIFACT,)
    assert harness.calls == []


def test_different_action_traces_are_non_reproducible_no_best_of_two():
    first = outcome(completion=True, rule=True, trace_sha256="1" * 64)
    second = outcome(completion=True, rule=True, trace_sha256="2" * 64)
    score, harness = run_score([first, second])
    assert score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)
    assert score.scientific_success is None
    assert harness.calls == [0, 1]


@pytest.mark.parametrize(
    "reason", [ExecutionExitReason.WALL_TIME_LIMIT, ExecutionExitReason.RESOURCE_LIMIT]
)
def test_over_budget_is_invalid(reason):
    limited = outcome(completion=False, rule=False, exit_reason=reason)
    score, _ = run_score([limited, limited])
    assert score.invalid_reasons == (InvalidReason.RESOURCE_LIMIT,)
    assert score.scientific_success is None


def test_malformed_action_protocol_is_invalid_not_scientific_false():
    breached = outcome(completion=False, rule=False, protocol_valid=False)
    score, _ = run_score([breached, breached])
    assert score.invalid_reasons == (InvalidReason.PROTOCOL_BREACH,)
    assert score.scientific_success is None
