"""Frozen source, license, hidden-rule boundary, and task contracts for DiscoveryWorld."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from aletheia.coder.executor import SandboxExecution
from aletheia.evals.adapters.discoveryworld import (
    DEFAULT_DISCOVERYWORLD_SPECS,
    DISCOVERYWORLD_COMMIT,
    DISCOVERYWORLD_HYPOTHESES,
    DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256,
    DiscoveryWorldAdapter,
    DiscoveryWorldAssetReceipt,
    DiscoveryWorldHarnessManifest,
    DiscoveryWorldHypothesis,
    DiscoveryWorldInstanceSpec,
    DiscoveryWorldScorer,
    DiscoveryWorldSourceManifest,
    DockerDiscoveryWorldHarness,
)
from aletheia.evals.schemas import EvaluationTask, ExecutionExitReason, ResourceBudget


ZERO_IMAGE = "sha256:" + "0" * 64
ONE_IMAGE = "sha256:" + "1" * 64
CANDIDATE_ENVIRONMENT = {
    "python": "3.11.0",
    "discoveryworld": "not-installed",
    "discoveryworld_import": "absent",
    "aletheia_source": "absent",
}
DISCOVERYWORLD_ENVIRONMENT = {
    "python": "3.11.0",
    "discoveryworld": "0.0.2",
    "source_commit": DISCOVERYWORLD_COMMIT,
    "source_archive_sha256": DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256,
    "api_sha256": "c455e32ddb5e676a83b7b3e349dda8262473ca54fec217650497a73603d46dc8",
    "scenario_maker_sha256": "1b1055e765b98e5a0dab94f4a31c9ac0f627eb9f52ca5a63f9c4140c3afdbd06",
    "task_scorer_sha256": "32755603cc4ce0a706943047e1f9ab0e031eb0d0992b9feaabb226b1b35fd79e",
    "storage_shed_sha256": "f88ac019b7867fac92dc4cf8d8fc61331bdd9df59577a463d54e1c3c75b35d2f",
    "user_interface_sha256": "135f80c0ebc3a909f09cb72306226368b2ca38d657429ded4a700aee76aa3934",
    "license_sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
}


def source() -> DiscoveryWorldSourceManifest:
    return DiscoveryWorldSourceManifest.official_public_validation()


def receipt(
    release: DiscoveryWorldSourceManifest | None = None,
    *,
    instance_id: str = "chem-easy-test",
    seed: int = 0,
    correct: str = "substance_b",
) -> DiscoveryWorldAssetReceipt:
    release = release or source()
    return DiscoveryWorldAssetReceipt(
        source_manifest_sha256=release.manifest_sha256,
        instance_id=instance_id,
        scenario="Combinatorial Chemistry",
        difficulty="Easy",
        world_seed=seed,
        task_name="RustedKeyTaskEasy",
        task_description="Determine which pure substance removes rust, derust the key, and leave.",
        hypothesis_space=tuple(
            DiscoveryWorldHypothesis(hypothesis_id=hypothesis_id, claim=claim)
            for hypothesis_id, claim in DISCOVERYWORLD_HYPOTHESES
        ),
        correct_hypothesis_id=correct,
        critical_hypothesis_sha256="1" * 64,
        critical_question_sha256="2" * 64,
        known_actions_sha256="3" * 64,
        teleport_locations_sha256="4" * 64,
        initial_observation_sha256="5" * 64,
    )


def manifest(release: DiscoveryWorldSourceManifest | None = None, **overrides):
    release = release or source()
    payload = {
        "source_manifest_sha256": release.manifest_sha256,
        "candidate_image_id": ZERO_IMAGE,
        "environment_image_id": ONE_IMAGE,
        "server_entrypoint_sha256": "6" * 64,
        "candidate_environment": CANDIDATE_ENVIRONMENT,
        "discoveryworld_environment": DISCOVERYWORLD_ENVIRONMENT,
        "candidate_wall_time_s": 10,
        "candidate_cpu_seconds": 5,
        "environment_wall_time_s": 15,
        "environment_cpu_seconds": 10,
        "action_wait_s": 10,
    }
    payload.update(overrides)
    return DiscoveryWorldHarnessManifest(**payload)


@dataclass
class NeverHarness:
    release: DiscoveryWorldSourceManifest

    @property
    def manifest(self):
        return manifest(self.release)

    def evaluate(self, **_kwargs):
        raise AssertionError("contract test must not execute the harness")


def test_official_source_is_exact_public_validation_with_separate_art_license():
    release = source()
    assert release.repository_commit == DISCOVERYWORLD_COMMIT
    assert release.source_archive_sha256 == DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256
    assert release.code_license == "Apache-2.0"
    assert release.art_asset_license == "PixyMoon-project-use-attribution-no-resale"
    assert release.art_asset_policy == "download-from-upstream-at-image-build-not-vendored"
    assert release.split == "public-validation"
    assert release.upstream_spoiler_risk == "public-source-contains-governing-rules"
    assert DEFAULT_DISCOVERYWORLD_SPECS == (
        ("chem-easy-v01", 0),
        ("chem-easy-v02", 1),
        ("chem-easy-v03", 2),
        ("chem-easy-v04", 3),
    )


def test_build_task_exposes_hypotheses_and_protocol_but_not_rule_or_seed(tmp_path):
    release = source()
    frozen = receipt(release)
    harness = NeverHarness(release)
    scorer = DiscoveryWorldScorer(harness=harness, source_manifest_sha256=release.manifest_sha256)
    adapter = DiscoveryWorldAdapter(release)
    task = adapter.build_task(
        receipt=frozen,
        scorer=scorer,
        resource_budget=ResourceBudget(wall_time_s=60, cpu_seconds=30, memory_mb=512),
    )
    public = task.public_view().model_dump(mode="json")
    serialized = json.dumps(public, sort_keys=True)
    assert "substance_a" in serialized and "substance_d" in serialized
    assert "correct_hypothesis_id" not in serialized
    assert "world_seed" not in serialized
    assert '"seed": 0' not in serialized
    assert "critical_hypothesis" not in serialized
    assert "evaluator://hidden" not in serialized
    assert task.layer == 3
    assert task.hidden_asset_sha256 == frozen.hidden_sha256
    assert task.contamination_policy.test_access_limit == 1

    staged = adapter.stage_hidden_asset(evaluator_root=tmp_path, task=task, receipt=frozen)
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == frozen.hidden_sha256
    assert json.loads(staged.read_text())["correct_hypothesis_id"] == "substance_b"


def test_subset_rejects_duplicate_seed_or_id():
    adapter = DiscoveryWorldAdapter(source())
    with pytest.raises(ValidationError, match="unique"):
        adapter.select_subset(
            (
                DiscoveryWorldInstanceSpec(instance_id="same-id", world_seed=0),
                DiscoveryWorldInstanceSpec(instance_id="same-id", world_seed=1),
            )
        )
    with pytest.raises(ValidationError, match="unique"):
        adapter.select_subset(
            (
                DiscoveryWorldInstanceSpec(instance_id="first-id", world_seed=0),
                DiscoveryWorldInstanceSpec(instance_id="other-id", world_seed=0),
            )
        )


def test_harness_rejects_shared_or_source_contaminated_candidate_image():
    with pytest.raises(ValidationError, match="different immutable images"):
        manifest(environment_image_id=ZERO_IMAGE)
    with pytest.raises(ValidationError, match="package is forbidden"):
        manifest(
            candidate_environment={
                **CANDIDATE_ENVIRONMENT,
                "discoveryworld": "0.0.2",
            }
        )
    with pytest.raises(ValidationError, match="evaluator source is forbidden"):
        manifest(
            candidate_environment={
                **CANDIDATE_ENVIRONMENT,
                "aletheia_source": "present",
            }
        )
    with pytest.raises(ValidationError, match="import path is forbidden"):
        manifest(
            candidate_environment={
                **CANDIDATE_ENVIRONMENT,
                "discoveryworld_import": "present",
            }
        )


def test_harness_rejects_official_source_drift():
    with pytest.raises(ValidationError, match="differs from frozen source"):
        manifest(
            discoveryworld_environment={
                **DISCOVERYWORLD_ENVIRONMENT,
                "storage_shed_sha256": "0" * 64,
            }
        )


def test_validated_terminal_receipt_completes_interactive_candidate():
    execution = SandboxExecution(
        returncode=137,
        trusted_terminal_receipt_observed=True,
    )

    assert (
        DockerDiscoveryWorldHarness._program_exit_reason(execution) is ExecutionExitReason.COMPLETED
    )


def test_hidden_asset_staging_rejects_path_escape(tmp_path):
    task = EvaluationTask(
        task_id="discoveryworld-chem-easy-test",
        version="test",
        layer=3,
        public_prompt="Discover the rule.",
        hidden_asset_ref="evaluator://hidden/../escape.json",
        hidden_asset_sha256="a" * 64,
        resource_budget={"wall_time_s": 10, "cpu_seconds": 5, "memory_mb": 128},
        expected_artifacts=(
            {"kind": "agent_program", "media_type": "text/x-python", "max_bytes": 100},
        ),
        scorer_ref="evaluator://scorers/discoveryworld",
        scorer_sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="escaped"):
        DiscoveryWorldAdapter.stage_hidden_asset(
            evaluator_root=tmp_path, task=task, receipt=receipt()
        )
