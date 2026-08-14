"""Pure, content-addressed schemas shared across the evaluator trust boundary.

These objects contain no code execution. Hidden assets and scoring internals are evaluator-only;
the research process receives only ``public_view()`` and writes a submission envelope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import Enum, IntEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def _canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_sha256(value: BaseModel | dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalLayer(IntEnum):
    EPISTEMIC_INVARIANTS = 0
    KNOWLEDGE_BOUNDARY = 1
    SCIENTIFIC_REPRODUCTION = 2
    HIDDEN_RULE_DISCOVERY = 3
    METHOD_INNOVATION = 4
    PRIVATE_PROSPECTIVE = 5


class AttemptStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    SCIENTIFIC_FAILURE = "scientific_failure"
    INVALID = "invalid"
    INFRA_FAILURE = "infra_failure"
    TIMEOUT = "timeout"


class ExecutionExitReason(str, Enum):
    COMPLETED = "completed"
    PROCESS_ERROR = "process_error"
    WALL_TIME_LIMIT = "wall_time_limit"
    RESOURCE_LIMIT = "resource_limit"
    INFRA_FAILURE = "infra_failure"


class InvalidReason(str, Enum):
    HIDDEN_ASSET_ACCESS = "hidden_asset_access"
    CONTAMINATION = "contamination"
    PROTOCOL_BREACH = "protocol_breach"
    FORGED_RECEIPT = "forged_receipt"
    MISSING_ARTIFACT = "missing_artifact"
    UNDECLARED_ATTEMPT = "undeclared_attempt"
    RESOURCE_LIMIT = "resource_limit"
    NON_REPRODUCIBLE = "non_reproducible"
    SCORER_FAILURE = "scorer_failure"


class ResourceBudget(FrozenModel):
    wall_time_s: int = Field(gt=0)
    cpu_seconds: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    gpu_seconds: int = Field(default=0, ge=0)
    token_cap: int | None = Field(default=None, gt=0)
    usd_cap: float | None = Field(default=None, gt=0)


class ArtifactRequirement(FrozenModel):
    kind: str = Field(min_length=1)
    media_type: str
    required: bool = True
    max_bytes: int = Field(gt=0)


class ContaminationPolicy(FrozenModel):
    corpus_cutoff: AwareDatetime | None = None
    forbidden_sources: tuple[str, ...] = ()
    disclose_training_overlap: bool = True
    test_access_limit: int = Field(default=1, ge=1)
    retire_after_access: bool = False


class EvaluationPublicAsset(FrozenModel):
    """Evaluator-stored bytes that are intentionally disclosed to the research plane.

    The evaluator reference never crosses the boundary.  The public task receives only the
    content identity and extraction contract; the runner validates and safely expands the archive
    into a fresh per-attempt workspace.
    """

    schema_version: Literal[1] = 1
    asset_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    evaluator_ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    file_count: int = Field(gt=0)
    expanded_bytes: int = Field(gt=0)
    media_type: Literal["application/gzip"] = "application/gzip"
    archive_format: Literal["tar.gz"] = "tar.gz"
    mount_path: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _paths_are_safe(self) -> "EvaluationPublicAsset":
        prefix = "evaluator://public/"
        if not self.evaluator_ref.startswith(prefix):
            raise ValueError("public asset refs must use evaluator://public/")
        reference = PurePosixPath(self.evaluator_ref[len(prefix) :])
        mount = PurePosixPath(self.mount_path)
        for label, path in (("evaluator ref", reference), ("mount path", mount)):
            if path.is_absolute() or not path.parts or any(
                part in {"", ".", ".."} for part in path.parts
            ):
                raise ValueError(f"public asset {label} must be a normalized relative path")
        if reference.as_posix() != self.evaluator_ref[len(prefix) :]:
            raise ValueError("public asset evaluator ref must be normalized")
        if mount.as_posix() != self.mount_path:
            raise ValueError("public asset mount path must be normalized")
        return self

    def public_view(self) -> "PublicEvaluationAsset":
        return PublicEvaluationAsset(
            asset_id=self.asset_id,
            sha256=self.sha256,
            bytes=self.bytes,
            file_count=self.file_count,
            expanded_bytes=self.expanded_bytes,
            media_type=self.media_type,
            archive_format=self.archive_format,
            mount_path=self.mount_path,
        )


class PublicEvaluationAsset(FrozenModel):
    schema_version: Literal[1] = 1
    asset_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)
    file_count: int = Field(gt=0)
    expanded_bytes: int = Field(gt=0)
    media_type: Literal["application/gzip"]
    archive_format: Literal["tar.gz"]
    mount_path: str


class EvaluationTask(FrozenModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: str = Field(min_length=1)
    layer: EvalLayer
    public_prompt: str = Field(min_length=1)
    hidden_asset_ref: str = Field(min_length=1)
    hidden_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_budget: ResourceBudget
    allowed_tools: tuple[str, ...] = ()
    public_assets: tuple[EvaluationPublicAsset, ...] = ()
    expected_artifacts: tuple[ArtifactRequirement, ...]
    scorer_ref: str = Field(min_length=1)
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contamination_policy: ContaminationPolicy = Field(default_factory=ContaminationPolicy)

    @model_validator(mode="after")
    def _unique_artifact_kinds(self) -> "EvaluationTask":
        kinds = [artifact.kind for artifact in self.expected_artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("expected artifact kinds must be unique")
        asset_ids = [asset.asset_id for asset in self.public_assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("public asset IDs must be unique")
        mounts = [PurePosixPath(asset.mount_path) for asset in self.public_assets]
        for index, mount in enumerate(mounts):
            for other in mounts[index + 1 :]:
                if mount == other or mount in other.parents or other in mount.parents:
                    raise ValueError("public asset mount paths must not overlap")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    def public_view(self) -> "PublicEvaluationTask":
        return PublicEvaluationTask(
            task_id=self.task_id,
            version=self.version,
            layer=self.layer,
            public_prompt=self.public_prompt,
            resource_budget=self.resource_budget,
            allowed_tools=self.allowed_tools,
            public_assets=tuple(asset.public_view() for asset in self.public_assets),
            expected_artifacts=self.expected_artifacts,
            contamination_policy=self.contamination_policy,
            task_manifest_sha256=self.manifest_sha256,
        )


class PublicEvaluationTask(FrozenModel):
    schema_version: Literal[1] = 1
    task_id: str
    version: str
    layer: EvalLayer
    public_prompt: str
    resource_budget: ResourceBudget
    allowed_tools: tuple[str, ...]
    public_assets: tuple[PublicEvaluationAsset, ...] = ()
    expected_artifacts: tuple[ArtifactRequirement, ...]
    contamination_policy: ContaminationPolicy
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationSuite(FrozenModel):
    schema_version: Literal[1] = 1
    suite_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    version: str = Field(min_length=1)
    task_manifest_sha256s: tuple[str, ...] = Field(min_length=1)
    scoring_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen: bool = True

    @model_validator(mode="after")
    def _tasks_are_unique(self) -> "EvaluationSuite":
        if len(self.task_manifest_sha256s) != len(set(self.task_manifest_sha256s)):
            raise ValueError("a task manifest may appear only once in a suite")
        if not self.frozen:
            raise ValueError("formal evaluation suites must be frozen")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class EvaluationAttemptSlot(FrozenModel):
    """One pre-registered run; retries reuse this slot and never create another seed."""

    schema_version: Literal[1] = 1
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_index: int = Field(ge=0)
    seed: int

    @property
    def slot_sha256(self) -> str:
        return content_sha256(self)


class EvaluationRunPlan(FrozenModel):
    """Frozen attempt family used to make best-of-N and seed omission detectable."""

    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slots: tuple[EvaluationAttemptSlot, ...] = Field(min_length=1)
    max_infra_retries_per_slot: int = Field(default=1, ge=0, le=10)
    frozen: bool = True

    @model_validator(mode="after")
    def _slots_are_pre_registered_once(self) -> "EvaluationRunPlan":
        identities = [(slot.task_manifest_sha256, slot.repeat_index) for slot in self.slots]
        if len(identities) != len(set(identities)):
            raise ValueError("a task repeat_index may appear only once in an evaluation run plan")
        if not self.frozen:
            raise ValueError("formal evaluation run plans must be frozen")
        by_task: dict[str, list[EvaluationAttemptSlot]] = {}
        for slot in self.slots:
            by_task.setdefault(slot.task_manifest_sha256, []).append(slot)
        for task_hash, slots in by_task.items():
            repeat_indices = sorted(slot.repeat_index for slot in slots)
            if repeat_indices != list(range(len(slots))):
                raise ValueError(
                    f"task {task_hash} repeat indices must be contiguous and start at zero"
                )
            seeds = [slot.seed for slot in slots]
            if len(seeds) != len(set(seeds)):
                raise ValueError("planned repeats for one task must use unique seeds")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class EvaluationAttempt(FrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_index: int = Field(ge=0)
    seed: int
    status: AttemptStatus = AttemptStatus.CREATED
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    intervention_count: int = Field(default=0, ge=0)
    retry_of_attempt_id: str | None = None
    retry_reason: Literal["infra_failure"] | None = None

    @model_validator(mode="after")
    def _timeline_is_valid(self) -> "EvaluationAttempt":
        if self.ended_at is not None and self.started_at is None:
            raise ValueError("ended_at requires started_at")
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        if bool(self.retry_of_attempt_id) != bool(self.retry_reason):
            raise ValueError("retry lineage and its infrastructure-failure reason are both required")
        terminal = {
            AttemptStatus.COMPLETED,
            AttemptStatus.SCIENTIFIC_FAILURE,
            AttemptStatus.INVALID,
            AttemptStatus.INFRA_FAILURE,
            AttemptStatus.TIMEOUT,
        }
        if self.status is AttemptStatus.CREATED and (
            self.started_at is not None or self.ended_at is not None
        ):
            raise ValueError("created attempts cannot have execution timestamps")
        if self.status in {AttemptStatus.RUNNING, AttemptStatus.SUBMITTED} and (
            self.started_at is None or self.ended_at is not None
        ):
            raise ValueError("active attempts require started_at and cannot have ended_at")
        if self.status in terminal and (self.started_at is None or self.ended_at is None):
            raise ValueError("terminal attempts require started_at and ended_at")
        return self

    @property
    def attempt_sha256(self) -> str:
        return content_sha256(self)


class ExecutorContract(FrozenModel):
    """Evaluator-owned execution capabilities frozen before an attempt starts."""

    schema_version: Literal[1] = 1
    executor_id: str = Field(min_length=1, max_length=256)
    security_level: Literal["hard", "development"]
    sandbox_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    network_mode: Literal["none"] = "none"
    exposed_tools: tuple[str, ...] = ()
    gpu_enabled: bool = False
    usage_metering: Literal["provider_receipt", "executor", "unavailable"] = "unavailable"

    @model_validator(mode="after")
    def _tools_are_unique(self) -> "ExecutorContract":
        if len(self.exposed_tools) != len(set(self.exposed_tools)):
            raise ValueError("executor tool capabilities must be unique")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class EvaluationAttemptManifest(FrozenModel):
    """Immutable manifest staged before research code can execute."""

    schema_version: Literal[1] = 1
    attempt: EvaluationAttempt
    public_task: PublicEvaluationTask
    executor: ExecutorContract
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def _identities_match(self) -> "EvaluationAttemptManifest":
        if self.attempt.status is not AttemptStatus.CREATED:
            raise ValueError("attempt manifests freeze the created attempt state")
        if self.attempt.task_manifest_sha256 != self.public_task.task_manifest_sha256:
            raise ValueError("attempt and public task manifests do not match")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class EvaluationResearchRequest(FrozenModel):
    """The complete—and deliberately minimal—input mounted into the research sandbox."""

    schema_version: Literal[1] = 1
    attempt_id: str
    run_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_index: int = Field(ge=0)
    seed: int
    public_task: PublicEvaluationTask
    submission_directory: Literal["/submission"] = "/submission"
    submission_manifest: Literal["submission.json"] = "submission.json"

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class SubmittedArtifact(FrozenModel):
    kind: str
    media_type: str = Field(min_length=1)
    uri: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class EvaluationSubmission(FrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: str
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[SubmittedArtifact, ...]
    submitted_at: AwareDatetime
    declared_contamination: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _artifact_kinds_are_unique(self) -> "EvaluationSubmission":
        kinds = [artifact.kind for artifact in self.artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("submitted artifact kinds must be unique")
        return self

    @property
    def submission_sha256(self) -> str:
        return content_sha256(self)


class EvaluationScore(FrozenModel):
    schema_version: Literal[1] = 1
    objective_scores: dict[str, float] = Field(default_factory=dict)
    rubric_scores: dict[str, float] = Field(default_factory=dict)
    evidence_sha256s: dict[str, str] = Field(default_factory=dict)
    evidence_objects: dict[str, dict[str, Any]] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    scientific_success: bool | None = None
    invalid_reasons: tuple[InvalidReason, ...] = ()
    adjudication_status: Literal["not_needed", "pending", "resolved"] = "not_needed"

    @model_validator(mode="after")
    def _invalid_is_not_scientific_false(self) -> "EvaluationScore":
        if any(
            not key.strip() or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for key, digest in self.evidence_sha256s.items()
        ):
            raise ValueError("score evidence must use non-empty names and SHA-256 digests")
        unknown_objects = set(self.evidence_objects) - set(self.evidence_sha256s)
        if unknown_objects:
            raise ValueError("score evidence objects require declared SHA-256 identities")
        for name, evidence in self.evidence_objects.items():
            if content_sha256(evidence) != self.evidence_sha256s[name]:
                raise ValueError(f"score evidence object {name!r} does not match its digest")
        if self.invalid_reasons and self.scientific_success is not None:
            raise ValueError(
                "invalid/protocol-breached attempts cannot also receive a scientific verdict"
            )
        return self


class EvaluationExecutionReceipt(FrozenModel):
    """Trusted runner observation; authored stdout is retained only by hash and byte count."""

    schema_version: Literal[1] = 1
    attempt_id: str
    attempt_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resource_budget: ResourceBudget
    started_at: AwareDatetime
    ended_at: AwareDatetime
    wall_time_s: float = Field(ge=0)
    returncode: int | None = None
    timed_out: bool = False
    exit_reason: ExecutionExitReason
    stdout_retained_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout_retained_bytes: int = Field(ge=0)
    stdout_total_bytes: int = Field(ge=0)
    stdout_truncated: bool = False
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_source: Literal["provider_receipt", "executor", "unavailable"] = "unavailable"
    infrastructure_detail: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _exit_is_consistent(self) -> "EvaluationExecutionReceipt":
        if self.ended_at < self.started_at:
            raise ValueError("execution ended_at precedes started_at")
        if self.timed_out != (self.exit_reason is ExecutionExitReason.WALL_TIME_LIMIT):
            raise ValueError("timed_out must match the wall_time_limit exit reason")
        if self.exit_reason is ExecutionExitReason.COMPLETED and self.returncode != 0:
            raise ValueError("completed execution requires returncode zero")
        if self.stdout_total_bytes < self.stdout_retained_bytes:
            raise ValueError("stdout total bytes cannot be smaller than retained bytes")
        if self.stdout_truncated != (self.stdout_total_bytes > self.stdout_retained_bytes):
            raise ValueError("stdout_truncated does not match retained and total byte counts")
        if self.usage_source == "unavailable" and any(
            value is not None for value in (self.input_tokens, self.output_tokens, self.cost_usd)
        ):
            raise ValueError("unavailable usage cannot contain token or cost measurements")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ScorerReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: str
    run_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: EvaluationScore
    scored_at: AwareDatetime

    @model_validator(mode="after")
    def _submission_matches_attempt(self) -> "ScorerReceipt":
        if not self.attempt_id.strip():
            raise ValueError("attempt_id is required")
        if self.score.invalid_reasons and InvalidReason.SCORER_FAILURE in self.score.invalid_reasons:
            raise ValueError("a scorer cannot issue a receipt for its own infrastructure failure")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)

    def verify_submission(self, submission: EvaluationSubmission) -> None:
        """Reject a receipt replayed onto another attempt, task, or submission."""
        if submission.attempt_id != self.attempt_id:
            raise ValueError("receipt attempt_id does not match submission")
        if submission.task_manifest_sha256 != self.task_manifest_sha256:
            raise ValueError("receipt task manifest does not match submission")
        if submission.system_manifest_sha256 != self.system_manifest_sha256:
            raise ValueError("receipt system manifest does not match submission")
        if submission.submission_sha256 != self.submission_sha256:
            raise ValueError("receipt submission hash does not match submitted bytes")

    def verify_attempt(self, attempt: EvaluationAttempt) -> None:
        if attempt.attempt_id != self.attempt_id:
            raise ValueError("receipt attempt_id does not match attempt")
        if attempt.run_plan_sha256 != self.run_plan_sha256:
            raise ValueError("receipt run plan does not match attempt")
        if attempt.task_manifest_sha256 != self.task_manifest_sha256:
            raise ValueError("receipt task manifest does not match attempt")
        if attempt.system_manifest_sha256 != self.system_manifest_sha256:
            raise ValueError("receipt system manifest does not match attempt")


class SignedScorerReceipt(FrozenModel):
    """HMAC envelope issued with an evaluator-only key and bound to every input hash."""

    schema_version: Literal[1] = 1
    receipt: ScorerReceipt
    key_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _message(receipt: ScorerReceipt, key_id: str) -> bytes:
        payload = {"receipt": receipt.model_dump(mode="json"), "key_id": key_id}
        return b"aletheia-scorer-receipt-v1\0" + _canonical_bytes(payload)

    @classmethod
    def issue(cls, receipt: ScorerReceipt, *, key_id: str, key: bytes) -> "SignedScorerReceipt":
        if len(key) < 32:
            raise ValueError("evaluator receipt signing keys must contain at least 32 bytes")
        signature = hmac.new(key, cls._message(receipt, key_id), hashlib.sha256).hexdigest()
        return cls(receipt=receipt, key_id=key_id, hmac_sha256=signature)

    def verify(self, *, key: bytes, expected_key_id: str | None = None) -> None:
        if expected_key_id is not None and self.key_id != expected_key_id:
            raise ValueError("scorer receipt key_id is not trusted")
        expected = hmac.new(
            key, self._message(self.receipt, self.key_id), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, self.hmac_sha256):
            raise ValueError("scorer receipt signature is invalid")

    @property
    def envelope_sha256(self) -> str:
        return content_sha256(self)
