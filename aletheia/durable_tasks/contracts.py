"""Authority-neutral contracts for durable at-least-once task delivery.

The queue transports engineering work.  Scientific facts remain in their typed ledgers; a task
result is only an artifact pointer and never becomes evidence merely because a worker returned it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$"
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class DurableTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _without_none(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(item) for item in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a task value with canonical JSON v1, without importing an authority graph."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        _without_none(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class TaskStatus(str, Enum):
    BLOCKED = "blocked"
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TerminalCategory(str, Enum):
    SUCCESS = "success"
    INFRASTRUCTURE = "infrastructure"
    LEASE_EXPIRED = "lease_expired"
    SCIENTIFIC = "scientific"
    INVALID_OUTPUT = "invalid_output"
    CANCELLED = "cancelled"
    DEPENDENCY_FAILED = "dependency_failed"
    INFRASTRUCTURE_EXHAUSTED = "infrastructure_exhausted"


_RETRYABLE_CATEGORIES = frozenset({TerminalCategory.INFRASTRUCTURE, TerminalCategory.LEASE_EXPIRED})


class RetryPolicy(DurableTaskModel):
    """Finite retry and lease policy frozen into a task at enqueue time."""

    max_attempts: int = Field(default=3, ge=1, le=100)
    lease_seconds: int = Field(default=300, ge=1, le=86_400)
    heartbeat_interval_seconds: int = Field(default=60, ge=1, le=43_200)
    initial_backoff_seconds: float = Field(default=5.0, ge=0.0, le=86_400.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=100.0)
    max_backoff_seconds: float = Field(default=300.0, ge=0.0, le=604_800.0)
    retryable_categories: tuple[TerminalCategory, ...] = (
        TerminalCategory.INFRASTRUCTURE,
        TerminalCategory.LEASE_EXPIRED,
    )

    @model_validator(mode="after")
    def _validate_policy(self) -> "RetryPolicy":
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff cannot be smaller than initial backoff")
        categories = tuple(dict.fromkeys(self.retryable_categories))
        if set(categories) - _RETRYABLE_CATEGORIES:
            raise ValueError("only infrastructure and lease_expired are retryable")
        object.__setattr__(self, "retryable_categories", categories)
        return self

    def backoff_seconds(self, attempt_number: int) -> float:
        exponent = max(0, attempt_number - 1)
        return min(
            self.max_backoff_seconds,
            self.initial_backoff_seconds * (self.backoff_multiplier**exponent),
        )


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex}"


def canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the JSON-normalized representation that is hashed and written to JSONB."""

    value = json.loads(canonical_json_bytes(payload))
    if not isinstance(value, dict):  # defensive; the public type already requires a mapping
        raise ValueError("task inputs must canonicalize to a JSON object")
    return value


class TaskSpec(DurableTaskModel):
    """Caller-supplied immutable identity and inputs for one durable engineering task."""

    task_id: str = Field(default_factory=new_task_id, pattern=_TASK_ID_PATTERN)
    task_type: str = Field(min_length=1, max_length=96, pattern=_TASK_ID_PATTERN)
    inputs: dict[str, Any] = Field(default_factory=dict)
    dependency_ids: tuple[str, ...] = ()
    owner: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    concurrency_key: str | None = Field(default=None, pattern=_IDEMPOTENCY_KEY_PATTERN)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    available_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _canonicalize(self) -> "TaskSpec":
        normalized = canonical_payload(self.inputs)
        dependencies = tuple(sorted(set(self.dependency_ids)))
        for dependency_id in dependencies:
            if not re.fullmatch(_TASK_ID_PATTERN, dependency_id):
                raise ValueError(f"invalid dependency task id: {dependency_id!r}")
        if self.task_id in dependencies:
            raise ValueError("a task cannot depend on itself")
        object.__setattr__(self, "inputs", normalized)
        object.__setattr__(self, "dependency_ids", dependencies)
        return self

    @property
    def inputs_sha256(self) -> str:
        return canonical_sha256(self.inputs)

    @property
    def request_sha256(self) -> str:
        return canonical_sha256(self)


class TaskSnapshot(DurableTaskModel):
    task_id: str
    task_type: str
    inputs_sha256: str = Field(pattern=_SHA256_PATTERN)
    inputs: dict[str, Any]
    dependency_ids: tuple[str, ...]
    owner: str
    run_id: str | None
    idempotency_key: str
    concurrency_key: str | None
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    retry_policy: RetryPolicy
    priority: int
    status: TaskStatus
    attempt_count: int
    state_version: int
    available_at: AwareDatetime
    active_attempt_id: str | None
    lease_owner: str | None
    lease_expires_at: AwareDatetime | None
    result_artifact_id: str | None
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    result: dict[str, Any] | None
    terminal_category: TerminalCategory | None
    terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class TaskAttemptSnapshot(DurableTaskModel):
    attempt_id: str
    task_id: str
    attempt_number: int
    worker_id: str
    worker_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    started_at: AwareDatetime
    heartbeat_at: AwareDatetime
    lease_expires_at: AwareDatetime
    ended_at: AwareDatetime | None
    terminal_category: TerminalCategory | None
    terminal_detail_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    retry_requested: bool | None
    retry_scheduled: bool | None
    partial_artifact_ids: tuple[str, ...]
    logs_artifact_id: str | None
    result_artifact_id: str | None
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class TaskLease(DurableTaskModel):
    task: TaskSnapshot
    attempt_id: str
    attempt_number: int
    worker_id: str
    worker_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    lease_token: str = Field(min_length=32, repr=False)
    lease_expires_at: AwareDatetime


class TaskOutcome(DurableTaskModel):
    task: TaskSnapshot
    attempt: TaskAttemptSnapshot
    replayed: bool


class EnqueueReceipt(DurableTaskModel):
    task: TaskSnapshot
    created: bool


class RecoveryReceipt(DurableTaskModel):
    recovered_task_ids: tuple[str, ...]
    terminalized_task_ids: tuple[str, ...]
    dependency_failed_task_ids: tuple[str, ...]
    recovered_at: AwareDatetime


class TaskExecutionResult(DurableTaskModel):
    """Worker output pointer; validators decide separately whether it is scientific evidence."""

    result_artifact_id: str = Field(min_length=1, max_length=512)
    result: dict[str, Any] = Field(default_factory=dict)
    logs_artifact_id: str | None = Field(default=None, max_length=512)
    partial_artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_result(self) -> "TaskExecutionResult":
        object.__setattr__(self, "result", canonical_payload(self.result))
        object.__setattr__(
            self,
            "partial_artifact_ids",
            tuple(sorted(set(self.partial_artifact_ids))),
        )
        return self
