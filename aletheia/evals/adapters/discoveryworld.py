"""Hidden-rule and evaluator-owned action-trace adapter for DiscoveryWorld.

The candidate submits a Python policy program.  During scoring, that program and the official
DiscoveryWorld environment run in separate no-network containers.  A pair of one-way bind mounts
exposes only public observations in one direction and candidate action envelopes in the other;
the scenario seed, governing rule, full scorecard, server code, and signed episode receipt never
enter the candidate container.

The first frozen mini-suite uses the official Combinatorial Chemistry / Easy task because its
explanatory rule has a finite, objective four-hypothesis space.  In addition to the upstream task
completion and procedural score, the adapter records exact rule discovery, pure-substance trials,
objective hypothesis entropy, explicit belief revisions, information gain per action, and a
content-addressed action trace.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Sequence

from pydantic import Field, model_validator

from aletheia.coder.executor import (
    docker_execution_is_infrastructure_failure,
    hardened_docker_command,
    resolve_docker_image,
    run_hardened_container,
    terminate_hardened_container,
)
from aletheia.evals.runner import EvaluationScorerInfrastructureError
from aletheia.evals.schemas import (
    ArtifactRequirement,
    ContaminationPolicy,
    EvalLayer,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    ExecutionExitReason,
    FrozenModel,
    InvalidReason,
    ResourceBudget,
    content_sha256,
)


DISCOVERYWORLD_REPOSITORY_URL = "https://github.com/allenai/discoveryworld"
DISCOVERYWORLD_COMMIT = "fd591323920be0d3786ef350955de1945aa571e5"
DISCOVERYWORLD_VERSION = "0.0.2"
DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256 = (
    "0ef5f45566807083754aa140e5653b9e8260434fc71d977591598b6625e619b1"
)
DISCOVERYWORLD_SOURCE_ARCHIVE_BYTES = 29_491_760
DISCOVERYWORLD_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
DISCOVERYWORLD_API_SHA256 = "c455e32ddb5e676a83b7b3e349dda8262473ca54fec217650497a73603d46dc8"
DISCOVERYWORLD_SCENARIO_MAKER_SHA256 = (
    "1b1055e765b98e5a0dab94f4a31c9ac0f627eb9f52ca5a63f9c4140c3afdbd06"
)
DISCOVERYWORLD_TASK_SCORER_SHA256 = (
    "32755603cc4ce0a706943047e1f9ab0e031eb0d0992b9feaabb226b1b35fd79e"
)
DISCOVERYWORLD_STORAGE_SHED_SHA256 = (
    "f88ac019b7867fac92dc4cf8d8fc61331bdd9df59577a463d54e1c3c75b35d2f"
)
DISCOVERYWORLD_UI_SHA256 = "135f80c0ebc3a909f09cb72306226368b2ca38d657429ded4a700aee76aa3934"
DISCOVERYWORLD_CANARY = "3e844c76-a2c9-4fa7-882a-7b7be0ad41de"
DISCOVERYWORLD_SCENARIO = "Combinatorial Chemistry"
DISCOVERYWORLD_DIFFICULTY = "Easy"
DISCOVERYWORLD_TASK_NAME = "RustedKeyTaskEasy"
DISCOVERYWORLD_HYPOTHESES = (
    ("substance_a", "Pure Substance A is the rust remover."),
    ("substance_b", "Pure Substance B is the rust remover."),
    ("substance_c", "Pure Substance C is the rust remover."),
    ("substance_d", "Pure Substance D is the rust remover."),
)
DISCOVERYWORLD_HYPOTHESIS_IDS = tuple(item[0] for item in DISCOVERYWORLD_HYPOTHESES)

# Four official parametric variations with one instance for each governing substance.  These are
# public validation tasks, not secret prospective tests; source-level spoilers are explicitly
# classified as contamination and the final Frontier Scientist Gate still requires private tasks.
DEFAULT_DISCOVERYWORLD_SPECS = (
    ("chem-easy-v01", 0),
    ("chem-easy-v02", 1),
    ("chem-easy-v03", 2),
    ("chem-easy-v04", 3),
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")
_FORBIDDEN_PROGRAM_REFERENCES = (
    re.compile(r"\b(?:from|import)\s+discoveryworld\b", re.IGNORECASE),
    re.compile(r"criticalHypotheses|criticalQuestions", re.IGNORECASE),
    re.compile(r"getTaskScorecard", re.IGNORECASE),
    re.compile(r"chemicalSolutionDict|rustRemovalDict|scoringInfo", re.IGNORECASE),
    re.compile(r"storage_shed\.py|ScenarioMaker\.py|TaskScorer\.py", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])hidden(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])receipt(?:[/\\]|$)", re.IGNORECASE),
)


def _canonical_bytes(value: FrozenModel | dict[str, Any] | list[Any]) -> bytes:
    if isinstance(value, FrozenModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"asset must be a regular non-symlink file: {path}")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError(f"asset exceeds its byte limit: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_candidate_exit(path: Path, payload: dict[str, Any]) -> None:
    """Replace a candidate-controlled path without following links or retaining directories."""
    path.parent.chmod(0o700)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


class DiscoveryWorldSourceManifest(FrozenModel):
    schema_version: Literal[1] = 1
    benchmark: Literal["DiscoveryWorld"] = "DiscoveryWorld"
    repository_url: str = DISCOVERYWORLD_REPOSITORY_URL
    repository_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    package_version: str = DISCOVERYWORLD_VERSION
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_archive_bytes: int = Field(gt=0)
    license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    api_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_maker_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_shed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_interface_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_license: Literal["Apache-2.0"] = "Apache-2.0"
    art_asset_license: Literal["PixyMoon-project-use-attribution-no-resale"] = (
        "PixyMoon-project-use-attribution-no-resale"
    )
    art_asset_policy: Literal["download-from-upstream-at-image-build-not-vendored"] = (
        "download-from-upstream-at-image-build-not-vendored"
    )
    split: Literal["public-validation"] = "public-validation"
    supported_scenario: Literal["Combinatorial Chemistry"] = DISCOVERYWORLD_SCENARIO
    supported_difficulty: Literal["Easy"] = DISCOVERYWORLD_DIFFICULTY
    supported_world_seeds: tuple[int, ...] = (0, 1, 2, 3, 4)
    upstream_spoiler_risk: Literal["public-source-contains-governing-rules"] = (
        "public-source-contains-governing-rules"
    )

    @model_validator(mode="after")
    def _seeds_are_official_and_unique(self) -> "DiscoveryWorldSourceManifest":
        if self.supported_world_seeds != (0, 1, 2, 3, 4):
            raise ValueError("the official benchmark variations are seeds 0 through 4")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    @classmethod
    def official_public_validation(cls) -> "DiscoveryWorldSourceManifest":
        return cls(
            repository_commit=DISCOVERYWORLD_COMMIT,
            source_archive_sha256=DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256,
            source_archive_bytes=DISCOVERYWORLD_SOURCE_ARCHIVE_BYTES,
            license_sha256=DISCOVERYWORLD_LICENSE_SHA256,
            api_sha256=DISCOVERYWORLD_API_SHA256,
            scenario_maker_sha256=DISCOVERYWORLD_SCENARIO_MAKER_SHA256,
            task_scorer_sha256=DISCOVERYWORLD_TASK_SCORER_SHA256,
            storage_shed_sha256=DISCOVERYWORLD_STORAGE_SHED_SHA256,
            user_interface_sha256=DISCOVERYWORLD_UI_SHA256,
        )


class DiscoveryWorldInstanceSpec(FrozenModel):
    instance_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{2,63}$")
    scenario: Literal["Combinatorial Chemistry"] = DISCOVERYWORLD_SCENARIO
    difficulty: Literal["Easy"] = DISCOVERYWORLD_DIFFICULTY
    world_seed: int = Field(ge=0, le=4)

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class DiscoveryWorldHypothesis(FrozenModel):
    hypothesis_id: Literal["substance_a", "substance_b", "substance_c", "substance_d"]
    claim: str = Field(min_length=1, max_length=256)


class DiscoveryWorldAssetReceipt(FrozenModel):
    """Evaluator-only frozen instance identity extracted by the official environment image."""

    schema_version: Literal[1] = 1
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{2,63}$")
    scenario: Literal["Combinatorial Chemistry"]
    difficulty: Literal["Easy"]
    world_seed: int = Field(ge=0, le=4)
    task_name: Literal["RustedKeyTaskEasy"]
    task_description: str = Field(min_length=1)
    hypothesis_space: tuple[DiscoveryWorldHypothesis, ...] = Field(min_length=4, max_length=4)
    correct_hypothesis_id: Literal["substance_a", "substance_b", "substance_c", "substance_d"]
    critical_hypothesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    critical_question_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    known_actions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    teleport_locations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hypothesis_contract_is_exact(self) -> "DiscoveryWorldAssetReceipt":
        observed = tuple((item.hypothesis_id, item.claim) for item in self.hypothesis_space)
        if observed != DISCOVERYWORLD_HYPOTHESES:
            raise ValueError("DiscoveryWorld hypothesis space drifted from the frozen contract")
        return self

    @property
    def hidden_sha256(self) -> str:
        return _sha256_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self)

    @property
    def spec(self) -> DiscoveryWorldInstanceSpec:
        return DiscoveryWorldInstanceSpec(
            instance_id=self.instance_id,
            scenario=self.scenario,
            difficulty=self.difficulty,
            world_seed=self.world_seed,
        )


class DiscoveryWorldSubsetManifest(FrozenModel):
    schema_version: Literal[1] = 1
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_specs: tuple[DiscoveryWorldInstanceSpec, ...] = Field(min_length=1)
    selection_policy: Literal["explicit-official-variations-no-best-of-n"] = (
        "explicit-official-variations-no-best-of-n"
    )
    intended_use: Literal["public-validation-not-private-frontier-gate"] = (
        "public-validation-not-private-frontier-gate"
    )

    @model_validator(mode="after")
    def _instances_are_unique(self) -> "DiscoveryWorldSubsetManifest":
        ids = [item.instance_id for item in self.instance_specs]
        seeds = [item.world_seed for item in self.instance_specs]
        if len(ids) != len(set(ids)) or len(seeds) != len(set(seeds)):
            raise ValueError("DiscoveryWorld subset instances and world seeds must be unique")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class DiscoveryWorldTraceStep(FrozenModel):
    sequence: int = Field(ge=0)
    kind: Literal["act", "stop"]
    world_action: dict[str, Any] | None = None
    action_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    valid_action: bool | None = None
    world_step_before: int = Field(ge=0)
    world_step_after: int = Field(ge=0)
    beliefs: dict[str, float] = Field(min_length=4, max_length=4)
    hypothesis_note_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    informative_trial_hypothesis_id: (
        Literal["substance_a", "substance_b", "substance_c", "substance_d"] | None
    ) = None
    informative_trial_outcome: Literal["positive", "negative"] | None = None
    objective_remaining_after: tuple[
        Literal["substance_a", "substance_b", "substance_c", "substance_d"], ...
    ] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _trace_contract_is_consistent(self) -> "DiscoveryWorldTraceStep":
        if set(self.beliefs) != set(DISCOVERYWORLD_HYPOTHESIS_IDS):
            raise ValueError("trace beliefs do not cover the frozen hypothesis space")
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in self.beliefs.values()):
            raise ValueError("trace beliefs must be finite probabilities")
        if abs(sum(self.beliefs.values()) - 1.0) > 1e-6:
            raise ValueError("trace beliefs must sum to one")
        if not set(self.objective_remaining_after) <= set(DISCOVERYWORLD_HYPOTHESIS_IDS):
            raise ValueError("objective hypothesis state escaped the frozen space")
        if (self.informative_trial_hypothesis_id is None) != (
            self.informative_trial_outcome is None
        ):
            raise ValueError("informative trials require both a hypothesis and outcome")
        if self.kind == "stop" and (
            self.world_action is not None
            or self.valid_action is not None
            or self.observation_after_sha256 is not None
        ):
            raise ValueError("stop trace steps cannot claim a world transition")
        if self.kind == "act" and (
            self.world_action is None
            or self.valid_action is None
            or self.observation_after_sha256 is None
        ):
            raise ValueError("act trace steps require a complete world transition")
        return self


class DiscoveryWorldEpisodeReceipt(FrozenModel):
    """Raw receipt issued only by the trusted environment container."""

    schema_version: Literal[1] = 1
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_id: str
    scenario: Literal["Combinatorial Chemistry"]
    difficulty: Literal["Easy"]
    world_seed: int = Field(ge=0, le=4)
    task_name: Literal["RustedKeyTaskEasy"]
    critical_hypothesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    correct_hypothesis_id: Literal["substance_a", "substance_b", "substance_c", "substance_d"]
    protocol_valid: bool
    terminal_reason: Literal[
        "candidate_exited",
        "candidate_stopped",
        "action_wait_limit",
        "protocol_breach",
        "official_task_complete",
        "world_action_limit",
    ]
    stopped: bool
    final_hypothesis_id: (
        Literal["substance_a", "substance_b", "substance_c", "substance_d"] | None
    ) = None
    completed: bool
    completed_successfully: bool
    score_normalized: float = Field(ge=0, le=1)
    action_count: int = Field(ge=0)
    valid_action_count: int = Field(ge=0)
    invalid_action_count: int = Field(ge=0)
    tested_hypothesis_ids: tuple[
        Literal["substance_a", "substance_b", "substance_c", "substance_d"], ...
    ] = ()
    objective_remaining: tuple[
        Literal["substance_a", "substance_b", "substance_c", "substance_d"], ...
    ] = Field(min_length=1, max_length=4)
    trace: tuple[DiscoveryWorldTraceStep, ...]
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _episode_is_consistent(self) -> "DiscoveryWorldEpisodeReceipt":
        if self.valid_action_count + self.invalid_action_count != self.action_count:
            raise ValueError("episode action counts are inconsistent")
        if tuple(step.sequence for step in self.trace) != tuple(range(len(self.trace))):
            raise ValueError("episode trace sequence is not contiguous")
        if sum(step.kind == "act" for step in self.trace) != self.action_count:
            raise ValueError("episode trace and action count differ")
        if len(self.trace) != self.action_count + int(self.stopped):
            raise ValueError("episode trace has an unexpected terminal step")
        if self.stopped != (bool(self.trace) and self.trace[-1].kind == "stop"):
            raise ValueError("episode stopped state differs from its terminal trace")
        if self.stopped != (self.final_hypothesis_id is not None):
            raise ValueError("episode final hypothesis differs from its stopped state")
        if self.completed_successfully and not self.completed:
            raise ValueError("successful completion must also be completed")
        if (
            _sha256_bytes(_canonical_bytes([item.model_dump(mode="json") for item in self.trace]))
            != self.trace_sha256
        ):
            raise ValueError("episode trace digest is invalid")
        if not set(self.tested_hypothesis_ids) <= set(DISCOVERYWORLD_HYPOTHESIS_IDS):
            raise ValueError("episode tested an unknown hypothesis")
        if not set(self.objective_remaining) <= set(DISCOVERYWORLD_HYPOTHESIS_IDS):
            raise ValueError("episode objective state escaped the hypothesis space")
        trace_tested = {
            step.informative_trial_hypothesis_id
            for step in self.trace
            if step.informative_trial_hypothesis_id is not None
        }
        if trace_tested != set(self.tested_hypothesis_ids):
            raise ValueError("episode tested hypotheses differ from its trace")
        if self.trace and self.trace[-1].objective_remaining_after != self.objective_remaining:
            raise ValueError("episode final objective state differs from its trace")
        return self


class DiscoveryWorldHarnessManifest(FrozenModel):
    schema_version: Literal[1] = 1
    harness_id: Literal["aletheia-discoveryworld-two-container-v1"] = (
        "aletheia-discoveryworld-two-container-v1"
    )
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    server_entrypoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_environment: dict[str, str] = Field(min_length=1)
    discoveryworld_environment: dict[str, str] = Field(min_length=1)
    reproduction_runs: int = Field(default=2, ge=2, le=5)
    candidate_wall_time_s: int = Field(default=120, gt=0)
    candidate_cpu_seconds: int = Field(default=90, gt=0)
    candidate_memory_mb: int = Field(default=512, gt=0)
    environment_wall_time_s: int = Field(default=150, gt=0)
    environment_cpu_seconds: int = Field(default=120, gt=0)
    environment_memory_mb: int = Field(default=2048, gt=0)
    ready_wait_s: int = Field(default=45, gt=0)
    action_wait_s: int = Field(default=125, gt=0)
    max_world_actions: int = Field(default=80, ge=1, le=500)
    max_program_bytes: int = Field(default=1 << 20, gt=0)
    network_mode: Literal["none"] = "none"
    candidate_discoveryworld_policy: Literal["package-and-evaluator-source-absent"] = (
        "package-and-evaluator-source-absent"
    )
    hidden_contract_policy: Literal["mounted-only-to-environment"] = "mounted-only-to-environment"
    scorecard_policy: Literal["full-scorecard-never-shown-to-candidate"] = (
        "full-scorecard-never-shown-to-candidate"
    )
    world_history_policy: Literal["disabled-replaced-by-authoritative-action-trace"] = (
        "disabled-replaced-by-authoritative-action-trace"
    )
    trusted_server_exit_policy: Literal["atomic-fsynced-receipt-then-immediate-process-exit"] = (
        "atomic-fsynced-receipt-then-immediate-process-exit"
    )
    candidate_terminal_policy: Literal["validated-stop-receipt-terminates-candidate"] = (
        "validated-stop-receipt-terminates-candidate"
    )
    aggregation_policy: Literal["all-runs-retained-no-best-of-n"] = "all-runs-retained-no-best-of-n"

    @model_validator(mode="after")
    def _runtime_contract_is_isolated(self) -> "DiscoveryWorldHarnessManifest":
        if self.candidate_image_id == self.environment_image_id:
            raise ValueError("candidate and hidden-world images must be different immutable images")
        if self.candidate_environment.get("python") in {None, "not-installed"}:
            raise ValueError("DiscoveryWorld candidate image requires Python")
        if self.candidate_environment.get("discoveryworld") != "not-installed":
            raise ValueError("DiscoveryWorld package is forbidden in the candidate image")
        if self.candidate_environment.get("discoveryworld_import") != "absent":
            raise ValueError("DiscoveryWorld import path is forbidden in the candidate image")
        if self.candidate_environment.get("aletheia_source") != "absent":
            raise ValueError("evaluator source is forbidden in the candidate image")
        expected = {
            "discoveryworld": DISCOVERYWORLD_VERSION,
            "source_commit": DISCOVERYWORLD_COMMIT,
            "source_archive_sha256": DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256,
            "api_sha256": DISCOVERYWORLD_API_SHA256,
            "scenario_maker_sha256": DISCOVERYWORLD_SCENARIO_MAKER_SHA256,
            "task_scorer_sha256": DISCOVERYWORLD_TASK_SCORER_SHA256,
            "storage_shed_sha256": DISCOVERYWORLD_STORAGE_SHED_SHA256,
            "user_interface_sha256": DISCOVERYWORLD_UI_SHA256,
            "license_sha256": DISCOVERYWORLD_LICENSE_SHA256,
        }
        drift = {
            key: (self.discoveryworld_environment.get(key), value)
            for key, value in expected.items()
            if self.discoveryworld_environment.get(key) != value
        }
        if drift:
            raise ValueError(f"DiscoveryWorld environment differs from frozen source: {drift}")
        if self.environment_wall_time_s <= self.candidate_wall_time_s:
            raise ValueError("environment wall time must outlive the candidate")
        if self.action_wait_s < self.candidate_wall_time_s:
            raise ValueError("environment action wait must cover the candidate wall time")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class DiscoveryWorldHarnessResult(FrozenModel):
    schema_version: Literal[1] = 1
    instance_id: str
    run_index: int = Field(ge=0)
    candidate_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    program_returncode: int | None = None
    program_exit_reason: ExecutionExitReason
    program_timed_out: bool = False
    program_terminal_receipt_observed: bool = False
    program_wall_time_s: float = Field(ge=0)
    program_log_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_wall_time_s: float = Field(default=0, ge=0)
    environment_log_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_valid: bool = False
    terminal_reason: str = "candidate_exited"
    stopped: bool = False
    task_completed: bool = False
    completed_successfully: bool = False
    procedural_score: float = Field(default=0, ge=0, le=1)
    final_hypothesis_id: str | None = None
    explicit_rule_discovery: bool = False
    action_count: int = Field(default=0, ge=0)
    valid_action_count: int = Field(default=0, ge=0)
    invalid_action_count: int = Field(default=0, ge=0)
    informative_trials: int = Field(default=0, ge=0)
    distinct_hypotheses_tested: int = Field(default=0, ge=0, le=4)
    redundant_trials: int = Field(default=0, ge=0)
    objective_hypotheses_remaining: int = Field(default=4, ge=1, le=4)
    objective_entropy_initial_bits: float = Field(default=2.0, ge=0)
    objective_entropy_final_bits: float = Field(default=2.0, ge=0)
    objective_information_gain_bits: float = Field(default=0, ge=0)
    information_gain_bits_per_action: float = Field(default=0, ge=0)
    reported_entropy_initial_bits: float = Field(default=0, ge=0)
    reported_entropy_final_bits: float = Field(default=0, ge=0)
    hypothesis_revision_count: int = Field(default=0, ge=0)
    revision_opportunities: int = Field(default=0, ge=0)
    successful_revisions: int = Field(default=0, ge=0)
    grounded_belief_updates: int = Field(default=0, ge=0)
    ungrounded_belief_updates: int = Field(default=0, ge=0)
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace: tuple[DiscoveryWorldTraceStep, ...] = ()

    @model_validator(mode="after")
    def _result_is_consistent(self) -> "DiscoveryWorldHarnessResult":
        if self.program_timed_out != (
            self.program_exit_reason is ExecutionExitReason.WALL_TIME_LIMIT
        ):
            raise ValueError("program timeout must match its exit reason")
        if self.program_terminal_receipt_observed and (
            self.program_exit_reason is not ExecutionExitReason.COMPLETED
        ):
            raise ValueError("a trusted terminal receipt must complete candidate execution")
        if self.valid_action_count + self.invalid_action_count != self.action_count:
            raise ValueError("harness action counts are inconsistent")
        if self.successful_revisions > self.revision_opportunities:
            raise ValueError("successful revisions exceed opportunities")
        if self.redundant_trials > self.informative_trials:
            raise ValueError("redundant trials exceed all trials")
        if self.explicit_rule_discovery and self.final_hypothesis_id is None:
            raise ValueError("rule discovery requires a final hypothesis")
        if (
            self.trace
            and _sha256_bytes(
                _canonical_bytes([step.model_dump(mode="json") for step in self.trace])
            )
            != self.trace_sha256
        ):
            raise ValueError("harness trace digest is invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class DiscoveryWorldScientificExitMetrics(FrozenModel):
    """Evaluator-only endpoints used by the frozen F9 K3 ablation.

    The candidate never receives the governing hypothesis.  These values are derived only after
    the trusted episode has ended, from the hidden rule and the authoritative action trace.  The
    source trace identity is retained so an aggregate report cannot substitute self-reported
    metrics for the hidden-world evidence.
    """

    schema_version: Literal[1] = 1
    source_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_complete: bool
    correct_hypothesis_preserved: bool
    posterior_brier_score: float = Field(ge=0.0, le=1.0)
    top_label_confidence: float = Field(ge=0.0, le=1.0)
    top_label_correct: bool
    mechanism_claimed: bool
    false_mechanism_claim: bool
    genuine_discriminating_trials: int = Field(ge=0, le=4)
    discriminating_trial_rate: float = Field(ge=0.0, le=1.0)
    wrong_explanation_elimination_score: float = Field(ge=0.0, le=1.0)
    informative_trials_to_identification: int = Field(ge=1, le=5)
    hypothesis_space_contracted: bool

    @model_validator(mode="after")
    def _claim_and_trace_metrics_are_consistent(self) -> "DiscoveryWorldScientificExitMetrics":
        if self.false_mechanism_claim and not self.mechanism_claimed:
            raise ValueError("a false mechanism requires an issued mechanism claim")
        if not self.trace_complete and (
            self.genuine_discriminating_trials
            or self.discriminating_trial_rate
            or self.wrong_explanation_elimination_score
            or self.hypothesis_space_contracted
        ):
            raise ValueError("incomplete traces cannot receive positive scientific-exit credit")
        return self

    @property
    def metrics_sha256(self) -> str:
        return content_sha256(self)


def derive_discoveryworld_scientific_exit_metrics(
    *,
    result: DiscoveryWorldHarnessResult,
    correct_hypothesis_id: str,
) -> DiscoveryWorldScientificExitMetrics:
    """Derive truth-relative metrics without exposing the hidden rule to the candidate.

    Elimination speed is the area under objective wrong-explanation exclusion over four possible
    pure-substance trials.  Missing trial opportunities are padded with the last evaluator-owned
    hypothesis state, so early correct exclusion scores higher while stopping without testing earns
    no credit.  Posterior quality uses the normalized multiclass Brier score (zero is perfect).
    """

    if correct_hypothesis_id not in DISCOVERYWORLD_HYPOTHESIS_IDS:
        raise ValueError("scientific-exit metrics require a frozen DiscoveryWorld hypothesis")
    act_steps = [step for step in result.trace if step.kind == "act"]
    trace_complete = len(act_steps) == result.action_count and tuple(
        step.sequence for step in result.trace
    ) == tuple(range(len(result.trace)))
    final_beliefs = (
        result.trace[-1].beliefs
        if result.trace
        else {hypothesis_id: 0.25 for hypothesis_id in DISCOVERYWORLD_HYPOTHESIS_IDS}
    )
    posterior_brier = 0.5 * sum(
        (final_beliefs[hypothesis_id] - float(hypothesis_id == correct_hypothesis_id)) ** 2
        for hypothesis_id in DISCOVERYWORLD_HYPOTHESIS_IDS
    )
    top_label = _argmax_unique(final_beliefs)
    top_confidence = max(final_beliefs.values())
    mechanism_claimed = result.stopped and result.final_hypothesis_id is not None
    false_mechanism = bool(
        mechanism_claimed and result.final_hypothesis_id != correct_hypothesis_id
    )

    preserved = True
    contracted = False
    genuine = 0
    identification_trial = len(DISCOVERYWORLD_HYPOTHESIS_IDS) + 1
    elimination_fractions: list[float] = []
    if trace_complete:
        remaining_before = set(DISCOVERYWORLD_HYPOTHESIS_IDS)
        for step in result.trace:
            remaining_after = set(step.objective_remaining_after)
            preserved = preserved and correct_hypothesis_id in remaining_after
            tested_id = step.informative_trial_hypothesis_id
            if tested_id is not None:
                if (
                    tested_id in remaining_before
                    and remaining_after < remaining_before
                    and correct_hypothesis_id in remaining_after
                ):
                    genuine += 1
                wrong_remaining = len(remaining_after - {correct_hypothesis_id})
                elimination_fractions.append((3 - wrong_remaining) / 3)
                if (
                    remaining_after == {correct_hypothesis_id}
                    and identification_trial == len(DISCOVERYWORLD_HYPOTHESIS_IDS) + 1
                ):
                    identification_trial = len(elimination_fractions)
            remaining_before = remaining_after
        contracted = preserved and len(remaining_before) < len(DISCOVERYWORLD_HYPOTHESIS_IDS)

    if elimination_fractions:
        padded = elimination_fractions + [elimination_fractions[-1]] * (
            len(DISCOVERYWORLD_HYPOTHESIS_IDS) - len(elimination_fractions)
        )
        elimination_score = sum(padded[: len(DISCOVERYWORLD_HYPOTHESIS_IDS)]) / len(
            DISCOVERYWORLD_HYPOTHESIS_IDS
        )
    else:
        elimination_score = 0.0
    informative_trials = len(elimination_fractions)
    discrimination_rate = genuine / informative_trials if informative_trials else 0.0

    return DiscoveryWorldScientificExitMetrics(
        source_trace_sha256=result.trace_sha256,
        trace_complete=trace_complete,
        correct_hypothesis_preserved=preserved if trace_complete else False,
        posterior_brier_score=posterior_brier,
        top_label_confidence=top_confidence,
        top_label_correct=top_label == correct_hypothesis_id,
        mechanism_claimed=mechanism_claimed,
        false_mechanism_claim=false_mechanism,
        genuine_discriminating_trials=genuine if trace_complete else 0,
        discriminating_trial_rate=discrimination_rate if trace_complete else 0.0,
        wrong_explanation_elimination_score=elimination_score if trace_complete else 0.0,
        informative_trials_to_identification=identification_trial,
        hypothesis_space_contracted=contracted if trace_complete else False,
    )


class DiscoveryWorldHarness(Protocol):
    @property
    def manifest(self) -> DiscoveryWorldHarnessManifest: ...

    def evaluate(
        self, *, receipt: DiscoveryWorldAssetReceipt, program: bytes, run_index: int
    ) -> DiscoveryWorldHarnessResult: ...


class DiscoveryWorldAdapter:
    def __init__(self, source: DiscoveryWorldSourceManifest | None = None) -> None:
        self.source = source or DiscoveryWorldSourceManifest.official_public_validation()

    def select_subset(
        self,
        specs: Sequence[DiscoveryWorldInstanceSpec] | None = None,
    ) -> DiscoveryWorldSubsetManifest:
        selected = tuple(
            specs
            if specs is not None
            else (
                DiscoveryWorldInstanceSpec(instance_id=instance_id, world_seed=seed)
                for instance_id, seed in DEFAULT_DISCOVERYWORLD_SPECS
            )
        )
        if not selected:
            raise ValueError("DiscoveryWorld mini-suite cannot be empty")
        if any(spec.world_seed not in self.source.supported_world_seeds for spec in selected):
            raise ValueError("DiscoveryWorld subset requested an unsupported benchmark seed")
        return DiscoveryWorldSubsetManifest(
            source_manifest_sha256=self.source.manifest_sha256,
            instance_specs=selected,
        )

    def build_task(
        self,
        *,
        receipt: DiscoveryWorldAssetReceipt,
        scorer: "DiscoveryWorldScorer",
        resource_budget: ResourceBudget,
        test_access_limit: int = 1,
    ) -> EvaluationTask:
        if receipt.source_manifest_sha256 != self.source.manifest_sha256:
            raise ValueError("DiscoveryWorld receipt is bound to another source release")
        hypotheses = "\n".join(
            f"- {item.hypothesis_id}: {item.claim}" for item in receipt.hypothesis_space
        )
        prompt = (
            "DiscoveryWorld public validation: Combinatorial Chemistry / Easy\n\n"
            f"Official task:\n{receipt.task_description}\n\n"
            "Submit one UTF-8 Python artifact of kind 'agent_program'. The independent scorer "
            "runs it twice in a neutral no-network container, beside—but never inside—the "
            "official hidden-world container. Your program reads JSON observations from the "
            "directory in DISCOVERYWORLD_OBSERVATIONS_DIR and atomically writes JSON action "
            "envelopes to DISCOVERYWORLD_ACTIONS_DIR. Read DISCOVERYWORLD_PROTOCOL before acting. "
            "For observation sequence N, write action_NNNN.json. Every envelope must include "
            "schema_version=1, the matching sequence, and a normalized beliefs object over exactly "
            "these hypothesis IDs:\n"
            f"{hypotheses}\n\n"
            "Use kind='act' with world_action for an official DiscoveryWorld JSON action. When "
            "finished, use kind='stop' with final_hypothesis_id. The environment receipt—not your "
            "program—records the action trace, official terminal scorecard, pure-substance trials, "
            "hypothesis entropy, revisions, and cost. Hidden rules, seeds, full scorecards, "
            "DiscoveryWorld source, and scorer code are not mounted. Do not use memorized benchmark "
            "answers or inspect upstream scenario/scorer source; disclose any overlap."
        )
        return EvaluationTask(
            task_id=f"discoveryworld-{receipt.instance_id}",
            version=f"public-validation-{self.source.repository_commit[:12]}-adapter-v1",
            layer=EvalLayer.HIDDEN_RULE_DISCOVERY,
            public_prompt=prompt,
            hidden_asset_ref=(
                "evaluator://hidden/discoveryworld/"
                f"{self.source.manifest_sha256}/{receipt.instance_id}.json"
            ),
            hidden_asset_sha256=receipt.hidden_sha256,
            resource_budget=resource_budget,
            allowed_tools=("python", "filesystem"),
            expected_artifacts=(
                ArtifactRequirement(
                    kind="agent_program", media_type="text/x-python", max_bytes=1 << 20
                ),
            ),
            scorer_ref="evaluator://scorers/discoveryworld-two-container-v1",
            scorer_sha256=scorer.scorer_sha256,
            contamination_policy=ContaminationPolicy(
                forbidden_sources=(
                    "DiscoveryWorld scenario implementation and parametric seed mapping",
                    "DiscoveryWorld criticalHypotheses and criticalQuestions",
                    "DiscoveryWorld full scorecard or evaluator episode receipt",
                    "memorized public benchmark walkthroughs or answers",
                ),
                disclose_training_overlap=True,
                test_access_limit=test_access_limit,
            ),
        )

    def build_suite(
        self,
        *,
        tasks: Sequence[EvaluationTask],
        subset_manifest: DiscoveryWorldSubsetManifest,
        scorer: "DiscoveryWorldScorer",
    ) -> EvaluationSuite:
        expected_ids = tuple(
            f"discoveryworld-{spec.instance_id}" for spec in subset_manifest.instance_specs
        )
        if tuple(task.task_id for task in tasks) != expected_ids:
            raise ValueError("DiscoveryWorld suite order differs from its subset manifest")
        if any(task.scorer_sha256 != scorer.scorer_sha256 for task in tasks):
            raise ValueError("DiscoveryWorld suite tasks are not bound to the loaded scorer")
        scoring_policy_sha256 = content_sha256(
            {
                "policy_id": "discoveryworld-hidden-rule-action-trace-v1",
                "source_manifest_sha256": self.source.manifest_sha256,
                "subset_manifest_sha256": subset_manifest.manifest_sha256,
                "scorer_sha256": scorer.scorer_sha256,
                "scientific_success": (
                    "official completedSuccessfully AND exact structured rule discovery"
                ),
                "trajectory_metrics": (
                    "pure trials, objective entropy, belief revisions, information gain/action"
                ),
                "reproducibility": "two exact evaluator-owned action-trace matches",
                "aggregation": "all-runs-retained-no-best-of-n",
            }
        )
        return EvaluationSuite(
            suite_id="discoveryworld-combinatorial-chemistry-easy-public-mini",
            version=f"{self.source.repository_commit[:12]}-adapter-v1",
            task_manifest_sha256s=tuple(task.manifest_sha256 for task in tasks),
            scoring_policy_sha256=scoring_policy_sha256,
        )

    @staticmethod
    def stage_hidden_asset(
        *, evaluator_root: Path, task: EvaluationTask, receipt: DiscoveryWorldAssetReceipt
    ) -> Path:
        prefix = "evaluator://hidden/"
        if not task.hidden_asset_ref.startswith(prefix):
            raise ValueError("DiscoveryWorld hidden asset ref escaped evaluator storage")
        relative = PurePosixPath(task.hidden_asset_ref[len(prefix) :])
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("DiscoveryWorld hidden asset ref escaped evaluator storage")
        target = Path(evaluator_root) / "hidden_assets" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if _sha256_file(target) != task.hidden_asset_sha256:
                raise ValueError("existing DiscoveryWorld hidden asset has different bytes")
            return target
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(receipt.to_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o400)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return target


def _entropy_bits(probabilities: Sequence[float]) -> float:
    return -sum(value * math.log2(value) for value in probabilities if value > 0)


def _argmax_unique(beliefs: dict[str, float]) -> str | None:
    maximum = max(beliefs.values())
    winners = [key for key, value in beliefs.items() if abs(value - maximum) <= 1e-12]
    return winners[0] if len(winners) == 1 else None


def _trajectory_metrics(
    episode: DiscoveryWorldEpisodeReceipt,
) -> dict[str, int | float]:
    trials = [step for step in episode.trace if step.informative_trial_hypothesis_id is not None]
    tested = [str(step.informative_trial_hypothesis_id) for step in trials]
    distinct_tested = len(set(tested))
    remaining = len(episode.objective_remaining)
    initial_objective_entropy = math.log2(len(DISCOVERYWORLD_HYPOTHESIS_IDS))
    final_objective_entropy = math.log2(remaining)
    information_gain = initial_objective_entropy - final_objective_entropy

    beliefs = [step.beliefs for step in episode.trace]
    reported_initial = (
        _entropy_bits(tuple(beliefs[0].values())) if beliefs else initial_objective_entropy
    )
    reported_final = (
        _entropy_bits(tuple(beliefs[-1].values())) if beliefs else initial_objective_entropy
    )
    revision_count = 0
    grounded_updates = 0
    ungrounded_updates = 0
    for index in range(1, len(episode.trace)):
        previous = episode.trace[index - 1]
        current = episode.trace[index]
        changed = (
            sum(
                abs(current.beliefs[key] - previous.beliefs[key])
                for key in DISCOVERYWORLD_HYPOTHESIS_IDS
            )
            > 1e-6
        )
        if not changed:
            continue
        if _argmax_unique(previous.beliefs) != _argmax_unique(current.beliefs):
            revision_count += 1
        if previous.informative_trial_hypothesis_id is not None:
            grounded_updates += 1
        else:
            ungrounded_updates += 1

    revision_opportunities = 0
    successful_revisions = 0
    for index, step in enumerate(episode.trace[:-1]):
        tested_id = step.informative_trial_hypothesis_id
        if step.informative_trial_outcome != "negative" or tested_id is None:
            continue
        if _argmax_unique(step.beliefs) != tested_id:
            continue
        revision_opportunities += 1
        following = episode.trace[index + 1].beliefs
        if (
            following[tested_id] + 0.05 <= step.beliefs[tested_id]
            and _argmax_unique(following) != tested_id
        ):
            successful_revisions += 1

    return {
        "informative_trials": len(trials),
        "distinct_hypotheses_tested": distinct_tested,
        "redundant_trials": len(trials) - distinct_tested,
        "objective_hypotheses_remaining": remaining,
        "objective_entropy_initial_bits": initial_objective_entropy,
        "objective_entropy_final_bits": final_objective_entropy,
        "objective_information_gain_bits": information_gain,
        "information_gain_bits_per_action": (
            information_gain / episode.action_count if episode.action_count else 0.0
        ),
        "reported_entropy_initial_bits": reported_initial,
        "reported_entropy_final_bits": reported_final,
        "hypothesis_revision_count": revision_count,
        "revision_opportunities": revision_opportunities,
        "successful_revisions": successful_revisions,
        "grounded_belief_updates": grounded_updates,
        "ungrounded_belief_updates": ungrounded_updates,
    }


class DockerDiscoveryWorldHarness:
    """Run a policy and the hidden official world in separate hardened containers."""

    def __init__(
        self,
        *,
        manifest: DiscoveryWorldHarnessManifest,
        scratch_root: Path,
    ) -> None:
        self._manifest = manifest
        self.scratch_root = Path(scratch_root).resolve(strict=False)
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.entrypoint = (
            Path(__file__).with_name("discoveryworld_server_entrypoint.py").resolve(strict=True)
        )
        if _sha256_file(self.entrypoint) != manifest.server_entrypoint_sha256:
            raise ValueError("DiscoveryWorld server entrypoint differs from its manifest")

    @property
    def manifest(self) -> DiscoveryWorldHarnessManifest:
        return self._manifest

    @classmethod
    def from_image_refs(
        cls,
        *,
        candidate_image_ref: str,
        environment_image_ref: str,
        source_manifest_sha256: str,
        scratch_root: Path,
        reproduction_runs: int = 2,
        **resource_limits: int,
    ) -> "DockerDiscoveryWorldHarness":
        scratch = Path(scratch_root).resolve(strict=False)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate_image_id = resolve_docker_image(candidate_image_ref)
        environment_image_id = resolve_docker_image(environment_image_ref)
        candidate_environment = _probe_candidate_environment(
            candidate_image_id, scratch_root=scratch
        )
        discoveryworld_environment = _probe_discoveryworld_environment(
            environment_image_id, scratch_root=scratch
        )
        manifest = DiscoveryWorldHarnessManifest(
            source_manifest_sha256=source_manifest_sha256,
            candidate_image_id=candidate_image_id,
            environment_image_id=environment_image_id,
            server_entrypoint_sha256=server_entrypoint_sha256(),
            candidate_environment=candidate_environment,
            discoveryworld_environment=discoveryworld_environment,
            reproduction_runs=reproduction_runs,
            **resource_limits,
        )
        return cls(manifest=manifest, scratch_root=scratch)

    def freeze_instance(
        self,
        *,
        spec: DiscoveryWorldInstanceSpec,
    ) -> DiscoveryWorldAssetReceipt:
        temporary = tempfile.mkdtemp(prefix="dw-freeze-", dir=self.scratch_root)
        root = Path(temporary).resolve()
        try:
            work = root / "work"
            receipt_dir = root / "receipt"
            work.mkdir(mode=0o700)
            receipt_dir.mkdir(mode=0o700)
            result_path = receipt_dir / "instance.json"
            container_name = f"aletheia-dw-freeze-{uuid.uuid4().hex[:16]}"
            command = hardened_docker_command(
                work,
                image_id=self.manifest.environment_image_id,
                container_name=container_name,
                container_dir="/work",
                writable=True,
                command=["python", "/opt/aletheia-discoveryworld-server.py"],
                additional_mounts=(
                    (self.entrypoint, "/opt/aletheia-discoveryworld-server.py", False),
                    (receipt_dir, "/receipt", True),
                ),
                memory_mb=self.manifest.environment_memory_mb,
                cpus=max(
                    0.01,
                    self.manifest.environment_cpu_seconds / self.manifest.environment_wall_time_s,
                ),
                cpu_seconds=self.manifest.environment_cpu_seconds,
                environment={
                    "DW_MODE": "freeze",
                    "DW_SOURCE_MANIFEST_SHA256": self.manifest.source_manifest_sha256,
                    "DW_INSTANCE_ID": spec.instance_id,
                    "DW_SCENARIO": spec.scenario,
                    "DW_DIFFICULTY": spec.difficulty,
                    "DW_WORLD_SEED": str(spec.world_seed),
                    "DW_RESULT_PATH": "/receipt/instance.json",
                    "SDL_VIDEODRIVER": "dummy",
                    "MPLBACKEND": "Agg",
                },
                include_aletheia_pythonpath=False,
            )
            result = run_hardened_container(
                command,
                container_name=container_name,
                timeout_s=self.manifest.environment_wall_time_s,
                image_id=self.manifest.environment_image_id,
                trusted_terminal_receipt=result_path,
            )
            if (
                result.error is not None
                or docker_execution_is_infrastructure_failure(result)
                or result.timed_out
                or not result.trusted_terminal_receipt_observed
            ):
                raise EvaluationScorerInfrastructureError(
                    "could not freeze official DiscoveryWorld instance: "
                    f"{result.error or result.output[-1000:]}"
                )
            if result_path.is_symlink() or not result_path.is_file():
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld freeze did not issue an instance receipt"
                )
            try:
                frozen = DiscoveryWorldAssetReceipt.model_validate_json(result_path.read_bytes())
            except Exception as exc:
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld freeze issued a malformed instance receipt"
                ) from exc
            if frozen.spec != spec:
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld frozen receipt does not match the requested instance"
                )
            return frozen
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _program_exit_reason(result) -> ExecutionExitReason:
        if result.trusted_terminal_receipt_observed:
            return ExecutionExitReason.COMPLETED
        if result.timed_out:
            return ExecutionExitReason.WALL_TIME_LIMIT
        if result.returncode in {-9, 137, 152}:
            return ExecutionExitReason.RESOURCE_LIMIT
        if result.returncode != 0:
            return ExecutionExitReason.PROCESS_ERROR
        return ExecutionExitReason.COMPLETED

    def evaluate(
        self,
        *,
        receipt: DiscoveryWorldAssetReceipt,
        program: bytes,
        run_index: int,
    ) -> DiscoveryWorldHarnessResult:
        if receipt.source_manifest_sha256 != self.manifest.source_manifest_sha256:
            raise EvaluationScorerInfrastructureError(
                "DiscoveryWorld receipt is bound to another harness source"
            )
        if len(program) > self.manifest.max_program_bytes:
            return DiscoveryWorldHarnessResult(
                instance_id=receipt.instance_id,
                run_index=run_index,
                candidate_image_id=self.manifest.candidate_image_id,
                environment_image_id=self.manifest.environment_image_id,
                program_exit_reason=ExecutionExitReason.RESOURCE_LIMIT,
                program_wall_time_s=0,
                program_log_sha256=_sha256_bytes(b"program-byte-limit"),
                environment_log_sha256=_sha256_bytes(b"not-started"),
                trace_sha256=_sha256_bytes(_canonical_bytes([])),
            )

        temporary = tempfile.mkdtemp(prefix="dw-episode-", dir=self.scratch_root)
        root = Path(temporary).resolve()
        server_result: dict[str, Any] = {}
        server_error: list[BaseException] = []
        server_name: str | None = None
        server_thread: threading.Thread | None = None
        actions_dir: Path | None = None
        try:
            candidate_work = root / "candidate_work"
            server_work = root / "server_work"
            candidate_source = root / "candidate_source"
            protocol_dir = root / "protocol"
            actions_dir = root / "actions"
            observations_dir = root / "observations"
            hidden_dir = root / "hidden"
            receipt_dir = root / "evaluator_receipt"
            for path in (
                candidate_work,
                server_work,
                candidate_source,
                protocol_dir,
                actions_dir,
                observations_dir,
                hidden_dir,
                receipt_dir,
            ):
                path.mkdir(mode=0o700)

            program_path = candidate_source / "agent.py"
            program_path.write_bytes(program)
            os.chmod(program_path, 0o444)
            hidden_path = hidden_dir / "contract.json"
            hidden_path.write_bytes(receipt.to_bytes())
            os.chmod(hidden_path, 0o400)
            protocol_path = protocol_dir / "task.json"
            protocol_payload = {
                "schema_version": 1,
                "protocol_id": "aletheia-discoveryworld-file-bridge-v1",
                "observation_pattern": "observation_{sequence:04d}.json",
                "action_pattern": "action_{sequence:04d}.json",
                "hypothesis_space": [
                    item.model_dump(mode="json") for item in receipt.hypothesis_space
                ],
                "action_envelope": {
                    "act": {
                        "schema_version": 1,
                        "sequence": "integer matching observation",
                        "kind": "act",
                        "world_action": "official DiscoveryWorld JSON action",
                        "beliefs": "probability for every hypothesis ID; sums to 1",
                        "hypothesis_note": "optional text, at most 2000 characters",
                    },
                    "stop": {
                        "schema_version": 1,
                        "sequence": "integer matching observation",
                        "kind": "stop",
                        "final_hypothesis_id": "one hypothesis ID",
                        "beliefs": "probability for every hypothesis ID; sums to 1",
                        "hypothesis_note": "optional text, at most 2000 characters",
                    },
                },
                "max_world_actions": self.manifest.max_world_actions,
                "atomic_write_required": True,
                "hidden_rule_and_scorecard_available": False,
            }
            protocol_path.write_bytes(_canonical_bytes(protocol_payload) + b"\n")
            os.chmod(protocol_path, 0o444)

            server_name = f"aletheia-dw-world-{uuid.uuid4().hex[:16]}"
            server_command = hardened_docker_command(
                server_work,
                image_id=self.manifest.environment_image_id,
                container_name=server_name,
                container_dir="/work",
                writable=True,
                command=["python", "/opt/aletheia-discoveryworld-server.py"],
                additional_mounts=(
                    (self.entrypoint, "/opt/aletheia-discoveryworld-server.py", False),
                    (actions_dir, "/actions", False),
                    (observations_dir, "/observations", True),
                    (hidden_dir, "/hidden", False),
                    (receipt_dir, "/receipt", True),
                ),
                memory_mb=self.manifest.environment_memory_mb,
                cpus=max(
                    0.01,
                    self.manifest.environment_cpu_seconds / self.manifest.environment_wall_time_s,
                ),
                cpu_seconds=self.manifest.environment_cpu_seconds,
                environment={
                    "DW_MODE": "episode",
                    "DW_HIDDEN_CONTRACT": "/hidden/contract.json",
                    "DW_ACTIONS_DIR": "/actions",
                    "DW_OBSERVATIONS_DIR": "/observations",
                    "DW_RESULT_PATH": "/receipt/result.json",
                    "DW_MAX_ACTIONS": str(self.manifest.max_world_actions),
                    "DW_ACTION_WAIT_S": str(self.manifest.action_wait_s),
                    "SDL_VIDEODRIVER": "dummy",
                    "MPLBACKEND": "Agg",
                },
                include_aletheia_pythonpath=False,
            )

            def run_server() -> None:
                try:
                    server_result["result"] = run_hardened_container(
                        server_command,
                        container_name=server_name,
                        timeout_s=self.manifest.environment_wall_time_s,
                        image_id=self.manifest.environment_image_id,
                        trusted_terminal_receipt=receipt_dir / "result.json",
                    )
                except BaseException as exc:  # keep thread failures evaluator-visible
                    server_error.append(exc)

            server_thread = threading.Thread(
                target=run_server,
                name=f"discoveryworld-server-{run_index}",
                daemon=True,
            )
            server_thread.start()
            ready = observations_dir / "ready.json"
            ready_deadline = time.monotonic() + self.manifest.ready_wait_s
            while not ready.is_file():
                if server_error or not server_thread.is_alive():
                    break
                if time.monotonic() >= ready_deadline:
                    break
                time.sleep(0.02)
            if not ready.is_file():
                server_thread.join(timeout=1)
                detail = (
                    str(server_error[0]) if server_error else "environment did not become ready"
                )
                if server_result.get("result") is not None:
                    result = server_result["result"]
                    detail = result.error or result.output[-1000:] or detail
                raise EvaluationScorerInfrastructureError(
                    f"DiscoveryWorld environment failed before the episode: {detail}"
                )

            candidate_name = f"aletheia-dw-candidate-{uuid.uuid4().hex[:16]}"
            candidate_command = hardened_docker_command(
                candidate_work,
                image_id=self.manifest.candidate_image_id,
                container_name=candidate_name,
                container_dir="/work",
                writable=True,
                command=["python", "/candidate/agent.py"],
                additional_mounts=(
                    (candidate_source, "/candidate", False),
                    (protocol_dir, "/protocol", False),
                    (observations_dir, "/observations", False),
                    (actions_dir, "/actions", True),
                ),
                memory_mb=self.manifest.candidate_memory_mb,
                cpus=max(
                    0.01,
                    self.manifest.candidate_cpu_seconds / self.manifest.candidate_wall_time_s,
                ),
                cpu_seconds=self.manifest.candidate_cpu_seconds,
                environment={
                    "DISCOVERYWORLD_PROTOCOL": "/protocol/task.json",
                    "DISCOVERYWORLD_OBSERVATIONS_DIR": "/observations",
                    "DISCOVERYWORLD_ACTIONS_DIR": "/actions",
                    "ALETHEIA_EVAL_SEED": "0",
                    "PYTHONHASHSEED": "0",
                },
                include_aletheia_pythonpath=False,
            )
            candidate = run_hardened_container(
                candidate_command,
                container_name=candidate_name,
                timeout_s=self.manifest.candidate_wall_time_s,
                image_id=self.manifest.candidate_image_id,
                # A validated environment receipt is the terminal response to the candidate's
                # explicit ``stop`` envelope. The candidate cannot mount or write this path; once
                # it appears there is no legal post-terminal work, so the evaluator cleans up the
                # process instead of requiring a Docker client exit notification.
                trusted_terminal_receipt=receipt_dir / "result.json",
            )
            if candidate.error is not None or docker_execution_is_infrastructure_failure(candidate):
                # Exit 125 from authored code is returned as a process error by Docker once the
                # container started; only the shared helper's explicit infrastructure predicate
                # reaches this branch.
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld candidate container infrastructure failed: "
                    f"{candidate.error or candidate.output[-1000:]}"
                )
            exit_payload = {
                "schema_version": 1,
                "returncode": candidate.returncode,
                "timed_out": candidate.timed_out,
            }
            _write_candidate_exit(actions_dir / "candidate_exit.json", exit_payload)
            server_thread.join(timeout=self.manifest.environment_wall_time_s + 5)
            if server_thread.is_alive():
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld environment did not terminate after candidate exit"
                )
            if server_error:
                raise EvaluationScorerInfrastructureError(
                    f"DiscoveryWorld environment thread failed: {server_error[0]}"
                )
            environment = server_result.get("result")
            if environment is None:
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld environment returned no execution receipt"
                )
            if (
                environment.error is not None
                or docker_execution_is_infrastructure_failure(environment)
                or environment.timed_out
                or not environment.trusted_terminal_receipt_observed
            ):
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld environment container failed: "
                    f"{environment.error or environment.output[-1000:]}"
                )
            result_path = receipt_dir / "result.json"
            if result_path.is_symlink() or not result_path.is_file():
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld trusted environment did not issue a receipt"
                )
            try:
                episode = DiscoveryWorldEpisodeReceipt.model_validate_json(result_path.read_bytes())
            except Exception as exc:
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld environment issued a malformed episode receipt"
                ) from exc
            expected_identity = (
                receipt.source_manifest_sha256,
                receipt.instance_id,
                receipt.scenario,
                receipt.difficulty,
                receipt.world_seed,
                receipt.task_name,
                receipt.critical_hypothesis_sha256,
                receipt.correct_hypothesis_id,
            )
            observed_identity = (
                episode.source_manifest_sha256,
                episode.instance_id,
                episode.scenario,
                episode.difficulty,
                episode.world_seed,
                episode.task_name,
                episode.critical_hypothesis_sha256,
                episode.correct_hypothesis_id,
            )
            if observed_identity != expected_identity:
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld episode receipt is not bound to the hidden instance"
                )
            metrics = _trajectory_metrics(episode)
            return DiscoveryWorldHarnessResult(
                instance_id=receipt.instance_id,
                run_index=run_index,
                candidate_image_id=self.manifest.candidate_image_id,
                environment_image_id=self.manifest.environment_image_id,
                program_returncode=candidate.returncode,
                program_exit_reason=self._program_exit_reason(candidate),
                program_timed_out=candidate.timed_out,
                program_terminal_receipt_observed=(candidate.trusted_terminal_receipt_observed),
                program_wall_time_s=candidate.wall_time_s,
                program_log_sha256=_sha256_bytes(candidate.output.encode("utf-8")),
                environment_wall_time_s=environment.wall_time_s,
                environment_log_sha256=_sha256_bytes(environment.output.encode("utf-8")),
                protocol_valid=episode.protocol_valid,
                terminal_reason=episode.terminal_reason,
                stopped=episode.stopped,
                task_completed=episode.completed,
                completed_successfully=episode.completed_successfully,
                procedural_score=episode.score_normalized,
                final_hypothesis_id=episode.final_hypothesis_id,
                explicit_rule_discovery=(
                    episode.final_hypothesis_id == receipt.correct_hypothesis_id
                ),
                action_count=episode.action_count,
                valid_action_count=episode.valid_action_count,
                invalid_action_count=episode.invalid_action_count,
                trace_sha256=episode.trace_sha256,
                trace=episode.trace,
                **metrics,
            )
        finally:
            if server_thread is not None and server_thread.is_alive():
                if actions_dir is not None:
                    try:
                        _write_candidate_exit(
                            actions_dir / "candidate_exit.json",
                            {"schema_version": 1, "cleanup": True},
                        )
                    except OSError:
                        pass
                server_thread.join(timeout=2)
                if server_thread.is_alive() and server_name is not None:
                    terminate_hardened_container(server_name)
                    server_thread.join(timeout=5)
            shutil.rmtree(root, ignore_errors=True)


class DiscoveryWorldScorer:
    """Map trusted official episodes and structured discovery traces to common F7 verdicts."""

    def __init__(
        self,
        *,
        harness: DiscoveryWorldHarness,
        source_manifest_sha256: str,
    ) -> None:
        if not _DIGEST.fullmatch(source_manifest_sha256):
            raise ValueError("DiscoveryWorld scorer requires a source manifest SHA-256")
        self.harness = harness
        self.source_manifest_sha256 = source_manifest_sha256
        self._scorer_sha256 = content_sha256(
            {
                "scorer_id": "aletheia-discoveryworld-hidden-rule-v1",
                "implementation_sha256": _sha256_file(Path(__file__)),
                "server_entrypoint_sha256": server_entrypoint_sha256(),
                "harness_manifest_sha256": harness.manifest.manifest_sha256,
                "source_manifest_sha256": source_manifest_sha256,
                "terminal_policy": "official-completion-and-exact-rule-v1",
                "information_policy": "objective-finite-hypothesis-entropy-v1",
                "reproducibility_policy": "exact-action-trace-all-runs-v1",
            }
        )

    @property
    def scorer_sha256(self) -> str:
        return self._scorer_sha256

    @staticmethod
    def _contaminated(program: str, submission: EvaluationSubmission) -> bool:
        if submission.declared_contamination or DISCOVERYWORLD_CANARY in program:
            return True
        return any(pattern.search(program) for pattern in _FORBIDDEN_PROGRAM_REFERENCES)

    def score(
        self,
        *,
        task: EvaluationTask,
        hidden_asset: bytes,
        submission: EvaluationSubmission,
        artifacts: dict[str, bytes],
    ) -> EvaluationScore:
        try:
            receipt = DiscoveryWorldAssetReceipt.model_validate_json(hidden_asset)
        except Exception as exc:
            raise EvaluationScorerInfrastructureError(
                "DiscoveryWorld hidden asset receipt is malformed"
            ) from exc
        if receipt.source_manifest_sha256 != self.source_manifest_sha256:
            raise EvaluationScorerInfrastructureError(
                "DiscoveryWorld receipt is bound to a different source release"
            )
        if task.task_id != f"discoveryworld-{receipt.instance_id}":
            raise EvaluationScorerInfrastructureError(
                "DiscoveryWorld task and hidden instance do not match"
            )
        program_bytes = artifacts.get("agent_program")
        if program_bytes is None:
            return EvaluationScore(invalid_reasons=(InvalidReason.MISSING_ARTIFACT,))
        try:
            program = program_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return EvaluationScore(invalid_reasons=(InvalidReason.PROTOCOL_BREACH,))
        if self._contaminated(program, submission):
            return EvaluationScore(
                evidence_sha256s={"submitted_program": _sha256_bytes(program_bytes)},
                invalid_reasons=(InvalidReason.CONTAMINATION,),
            )
        try:
            ast.parse(program)
        except SyntaxError:
            # Syntax errors are retained as scientific failures after both frozen executions.
            pass

        results: list[DiscoveryWorldHarnessResult] = []
        for index in range(self.harness.manifest.reproduction_runs):
            result = self.harness.evaluate(receipt=receipt, program=program_bytes, run_index=index)
            if (
                result.instance_id != receipt.instance_id
                or result.run_index != index
                or result.candidate_image_id != self.harness.manifest.candidate_image_id
                or result.environment_image_id != self.harness.manifest.environment_image_id
            ):
                raise EvaluationScorerInfrastructureError(
                    "DiscoveryWorld harness result is not bound to its frozen run"
                )
            results.append(result)

        evidence_sha256s = {
            "submitted_program": _sha256_bytes(program_bytes),
            **{f"harness_run_{item.run_index}": item.receipt_sha256 for item in results},
        }
        evidence_objects = {
            f"harness_run_{item.run_index}": item.model_dump(mode="json", exclude_none=True)
            for item in results
        }
        if any(
            item.program_exit_reason
            in {ExecutionExitReason.WALL_TIME_LIMIT, ExecutionExitReason.RESOURCE_LIMIT}
            for item in results
        ):
            return EvaluationScore(
                objective_scores={"reproducible": 0.0},
                evidence_sha256s=evidence_sha256s,
                evidence_objects=evidence_objects,
                invalid_reasons=(InvalidReason.RESOURCE_LIMIT,),
            )
        if any(not item.protocol_valid for item in results):
            return EvaluationScore(
                objective_scores={"reproducible": 0.0},
                evidence_sha256s=evidence_sha256s,
                evidence_objects=evidence_objects,
                invalid_reasons=(InvalidReason.PROTOCOL_BREACH,),
            )

        reproducibility_keys = {
            (
                item.program_exit_reason,
                item.trace_sha256,
                item.task_completed,
                item.completed_successfully,
                item.procedural_score,
                item.final_hypothesis_id,
                item.explicit_rule_discovery,
                item.objective_information_gain_bits,
                item.hypothesis_revision_count,
            )
            for item in results
        }
        if len(reproducibility_keys) != 1:
            return EvaluationScore(
                objective_scores={"reproducible": 0.0},
                evidence_sha256s=evidence_sha256s,
                evidence_objects=evidence_objects,
                invalid_reasons=(InvalidReason.NON_REPRODUCIBLE,),
            )

        result = results[0]
        scientific_exit_metrics = derive_discoveryworld_scientific_exit_metrics(
            result=result,
            correct_hypothesis_id=receipt.correct_hypothesis_id,
        )
        evidence_sha256s["scientific_exit_metrics"] = scientific_exit_metrics.metrics_sha256
        evidence_objects["scientific_exit_metrics"] = scientific_exit_metrics.model_dump(
            mode="json"
        )
        runnable = result.program_exit_reason is ExecutionExitReason.COMPLETED
        valid_action_rate = (
            result.valid_action_count / result.action_count if result.action_count else 0.0
        )
        revision_rate = (
            result.successful_revisions / result.revision_opportunities
            if result.revision_opportunities
            else 1.0
        )
        belief_updates = result.grounded_belief_updates + result.ungrounded_belief_updates
        grounded_update_rate = (
            result.grounded_belief_updates / belief_updates if belief_updates else 1.0
        )
        scientific_success = (
            runnable
            and result.stopped
            and result.completed_successfully
            and result.explicit_rule_discovery
        )
        return EvaluationScore(
            objective_scores={
                "runnable": float(runnable),
                "task_completion": float(result.completed_successfully),
                "procedural_progress": result.procedural_score,
                "explicit_rule_discovery": float(result.explicit_rule_discovery),
                "valid_action_rate": valid_action_rate,
                "informative_trials": float(result.informative_trials),
                "distinct_hypotheses_tested": float(result.distinct_hypotheses_tested),
                "objective_information_gain_bits": result.objective_information_gain_bits,
                "information_gain_bits_per_action": result.information_gain_bits_per_action,
                "hypothesis_revision_rate": revision_rate,
                "grounded_belief_update_rate": grounded_update_rate,
                "posterior_brier_score": scientific_exit_metrics.posterior_brier_score,
                "top_label_confidence": scientific_exit_metrics.top_label_confidence,
                "top_label_correct": float(scientific_exit_metrics.top_label_correct),
                "mechanism_claim_coverage": float(scientific_exit_metrics.mechanism_claimed),
                "false_mechanism_rate": float(scientific_exit_metrics.false_mechanism_claim),
                "genuine_discriminating_trials": float(
                    scientific_exit_metrics.genuine_discriminating_trials
                ),
                "discriminating_trial_rate": (scientific_exit_metrics.discriminating_trial_rate),
                "wrong_explanation_elimination_score": (
                    scientific_exit_metrics.wrong_explanation_elimination_score
                ),
                "hypothesis_space_contracted": float(
                    scientific_exit_metrics.hypothesis_space_contracted
                ),
                "reproducible": 1.0,
            },
            evidence_sha256s=evidence_sha256s,
            evidence_objects=evidence_objects,
            scientific_success=scientific_success,
        )


def server_entrypoint_sha256() -> str:
    return _sha256_file(Path(__file__).with_name("discoveryworld_server_entrypoint.py"))


def _run_probe(
    *,
    image_ref: str,
    scratch_root: Path,
    probe: str,
    prefix: str,
    memory_mb: int,
) -> dict[str, str]:
    image_id = resolve_docker_image(image_ref)
    root = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=scratch_root))
    try:
        name = f"aletheia-{prefix}-{uuid.uuid4().hex[:16]}"
        result_path = root / "probe.json"
        wrapped_probe = (
            probe
            + r"""
import os as _probe_os
import tempfile as _probe_tempfile
from pathlib import Path as _ProbePath
_probe_path = _ProbePath("/input/probe.json")
_probe_fd, _probe_temporary = _probe_tempfile.mkstemp(
    prefix=".probe.json.", dir=_probe_path.parent
)
with _probe_os.fdopen(_probe_fd, "w", encoding="utf-8") as _probe_handle:
    json.dump(PROBE_RESULT, _probe_handle, sort_keys=True, separators=(",", ":"))
    _probe_handle.write("\n")
    _probe_handle.flush()
    _probe_os.fsync(_probe_handle.fileno())
_probe_os.replace(_probe_temporary, _probe_path)
_probe_os._exit(0)
"""
        )
        command = hardened_docker_command(
            root,
            image_id=image_id,
            container_name=name,
            writable=True,
            command=["python", "-c", wrapped_probe],
            memory_mb=memory_mb,
            cpus=1,
            cpu_seconds=30,
            environment={"SDL_VIDEODRIVER": "dummy", "MPLBACKEND": "Agg"},
            include_aletheia_pythonpath=False,
        )
        result = run_hardened_container(
            command,
            container_name=name,
            timeout_s=45,
            image_id=image_id,
            trusted_terminal_receipt=result_path,
        )
        if (
            result.error is not None
            or result.timed_out
            or not result.trusted_terminal_receipt_observed
        ):
            raise RuntimeError(f"could not inspect {prefix} image: {result.output}")
        try:
            payload = json.loads(result_path.read_bytes())
        except Exception as exc:
            raise RuntimeError(f"could not parse {prefix} image probe") from exc
        return {str(key): str(value) for key, value in payload.items()}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _probe_candidate_environment(image_ref: str, *, scratch_root: Path) -> dict[str, str]:
    probe = r"""
import importlib.metadata as metadata
import importlib.util
import json
from pathlib import Path
import sys
try:
    version = metadata.version("discoveryworld")
except metadata.PackageNotFoundError:
    version = "not-installed"
source_candidates = [Path("/opt/aletheia"), Path("/aletheia"), Path("/workspace/aletheia")]
PROBE_RESULT = {
    "python": sys.version.split()[0],
    "discoveryworld": version,
    "discoveryworld_import": (
        "absent" if importlib.util.find_spec("discoveryworld") is None else "present"
    ),
    "aletheia_source": "present" if any(path.exists() for path in source_candidates) else "absent",
}
"""
    return _run_probe(
        image_ref=image_ref,
        scratch_root=scratch_root,
        probe=probe,
        prefix="dw-candidate-probe",
        memory_mb=256,
    )


def _probe_discoveryworld_environment(image_ref: str, *, scratch_root: Path) -> dict[str, str]:
    probe = r"""
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import sys
root = Path("/opt/discoveryworld-source")
import discoveryworld
installed = Path(discoveryworld.__file__).parent
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def verified(relative):
    source_digest = digest(root / relative)
    installed_digest = digest(installed / relative.removeprefix("discoveryworld/"))
    if source_digest != installed_digest:
        raise RuntimeError("installed DiscoveryWorld code differs from the frozen source tree")
    return source_digest
PROBE_RESULT = {
    "python": sys.version.split()[0],
    "discoveryworld": metadata.version("discoveryworld"),
    "source_commit": os.environ.get("DISCOVERYWORLD_SOURCE_COMMIT", "missing"),
    "source_archive_sha256": os.environ.get("DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256", "missing"),
    "license_sha256": digest(root / "LICENSE.txt"),
    "api_sha256": verified("discoveryworld/DiscoveryWorldAPI.py"),
    "scenario_maker_sha256": verified("discoveryworld/ScenarioMaker.py"),
    "task_scorer_sha256": verified("discoveryworld/TaskScorer.py"),
    "storage_shed_sha256": verified("discoveryworld/scenarios/storage_shed.py"),
    "user_interface_sha256": verified("discoveryworld/UserInterface.py"),
}
"""
    return _run_probe(
        image_ref=image_ref,
        scratch_root=scratch_root,
        probe=probe,
        prefix="dw-environment-probe",
        memory_mb=512,
    )
