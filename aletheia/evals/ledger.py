"""Append-only, hash-chained evaluator ledger.

The research process never receives this path.  Every state transition is a new
record; terminal attempts and failed retries are therefore impossible to erase
through the runner API.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.evals.schemas import (
    AttemptStatus,
    EvaluationAttempt,
    EvaluationRunPlan,
    FrozenModel,
    content_sha256,
)

LedgerEventType = Literal[
    "run_plan_registered",
    "attempt_state",
    "attempt_manifest_frozen",
    "public_assets_staged",
    "execution_receipt_issued",
    "submission_accepted",
    "score_receipt_issued",
    "score_evidence_recorded",
    "retry_authorized",
]

_TERMINAL = {
    AttemptStatus.COMPLETED,
    AttemptStatus.SCIENTIFIC_FAILURE,
    AttemptStatus.INVALID,
    AttemptStatus.INFRA_FAILURE,
    AttemptStatus.TIMEOUT,
}
_TRANSITIONS = {
    AttemptStatus.CREATED: {AttemptStatus.RUNNING},
    AttemptStatus.RUNNING: {
        AttemptStatus.SUBMITTED,
        AttemptStatus.INVALID,
        AttemptStatus.INFRA_FAILURE,
        AttemptStatus.TIMEOUT,
    },
    AttemptStatus.SUBMITTED: {
        AttemptStatus.COMPLETED,
        AttemptStatus.SCIENTIFIC_FAILURE,
        AttemptStatus.INVALID,
        AttemptStatus.INFRA_FAILURE,
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EvaluationLedgerError(RuntimeError):
    pass


class EvaluationLedgerEvent(FrozenModel):
    schema_version: Literal[1] = 1
    sequence: int = Field(ge=1)
    event_type: LedgerEventType
    occurred_at: AwareDatetime
    attempt_id: str | None = None
    payload: dict[str, Any]
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def calculate_hash(
        *,
        sequence: int,
        event_type: LedgerEventType,
        occurred_at: datetime,
        attempt_id: str | None,
        payload: dict[str, Any],
        previous_event_sha256: str | None,
    ) -> str:
        return content_sha256(
            {
                "schema_version": 1,
                "sequence": sequence,
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "attempt_id": attempt_id,
                "payload": payload,
                "previous_event_sha256": previous_event_sha256,
            }
        )

    @model_validator(mode="after")
    def _hash_is_valid(self) -> "EvaluationLedgerEvent":
        expected = self.calculate_hash(
            sequence=self.sequence,
            event_type=self.event_type,
            occurred_at=self.occurred_at,
            attempt_id=self.attempt_id,
            payload=self.payload,
            previous_event_sha256=self.previous_event_sha256,
        )
        if self.event_sha256 != expected:
            raise ValueError("evaluation ledger event hash is invalid")
        return self


class EvaluationLedger:
    """Concurrency-safe JSONL ledger owned by the evaluator plane."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve(strict=False)

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _decode(handle: Any) -> list[EvaluationLedgerEvent]:
        handle.seek(0)
        events: list[EvaluationLedgerEvent] = []
        previous: str | None = None
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise EvaluationLedgerError(f"blank line in evaluation ledger at {line_number}")
            try:
                event = EvaluationLedgerEvent.model_validate_json(raw)
            except Exception as exc:
                raise EvaluationLedgerError(
                    f"invalid evaluation ledger record at line {line_number}: {exc}"
                ) from exc
            if event.sequence != line_number:
                raise EvaluationLedgerError(
                    f"evaluation ledger sequence gap at line {line_number}: {event.sequence}"
                )
            if event.previous_event_sha256 != previous:
                raise EvaluationLedgerError(
                    f"evaluation ledger hash chain breaks at line {line_number}"
                )
            previous = event.event_sha256
            events.append(event)
        return events

    def events(self) -> tuple[EvaluationLedgerEvent, ...]:
        self._ensure_file()
        with self.path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return tuple(self._decode(handle))
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append(
        self,
        event_type: LedgerEventType,
        payload: dict[str, Any],
        *,
        attempt_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> EvaluationLedgerEvent:
        self._ensure_file()
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                events = self._decode(handle)
                sequence = len(events) + 1
                previous = events[-1].event_sha256 if events else None
                timestamp = occurred_at or _now()
                event_hash = EvaluationLedgerEvent.calculate_hash(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    attempt_id=attempt_id,
                    payload=payload,
                    previous_event_sha256=previous,
                )
                event = EvaluationLedgerEvent(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    attempt_id=attempt_id,
                    payload=payload,
                    previous_event_sha256=previous,
                    event_sha256=event_hash,
                )
                handle.seek(0, os.SEEK_END)
                handle.write(event.model_dump_json(exclude_none=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return event
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @property
    def head_sha256(self) -> str | None:
        events = self.events()
        return events[-1].event_sha256 if events else None

    def register_plan(self, plan: EvaluationRunPlan) -> None:
        def _check(events: list[EvaluationLedgerEvent]) -> bool:
            for event in events:
                if event.event_type != "run_plan_registered":
                    continue
                registered = EvaluationRunPlan.model_validate(event.payload["plan"])
                if event.payload.get("plan_id") != plan.plan_id:
                    same_evaluation = (
                        registered.suite_manifest_sha256 == plan.suite_manifest_sha256
                        and registered.system_manifest_sha256 == plan.system_manifest_sha256
                        and registered.evaluator_manifest_sha256
                        == plan.evaluator_manifest_sha256
                    )
                    if same_evaluation and registered.manifest_sha256 != plan.manifest_sha256:
                        raise EvaluationLedgerError(
                            "suite/system/evaluator identity is already bound to another run plan"
                        )
                    continue
                if event.payload.get("plan_sha256") == plan.manifest_sha256:
                    return True
                raise EvaluationLedgerError(
                    f"run plan id {plan.plan_id!r} is already bound to different content"
                )
            return False

        self._append_if(
            event_type="run_plan_registered",
            payload={
                "plan_id": plan.plan_id,
                "plan_sha256": plan.manifest_sha256,
                "plan": plan.model_dump(mode="json"),
            },
            predicate=lambda events: not _check(events),
        )

    def _append_if(
        self,
        *,
        event_type: LedgerEventType,
        payload: dict[str, Any],
        predicate: Any,
        attempt_id: str | None = None,
    ) -> EvaluationLedgerEvent | None:
        """Evaluate and append under one exclusive file lock."""
        self._ensure_file()
        with self.path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                events = self._decode(handle)
                if not predicate(events):
                    return None
                sequence = len(events) + 1
                previous = events[-1].event_sha256 if events else None
                timestamp = _now()
                event_hash = EvaluationLedgerEvent.calculate_hash(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    attempt_id=attempt_id,
                    payload=payload,
                    previous_event_sha256=previous,
                )
                event = EvaluationLedgerEvent(
                    sequence=sequence,
                    event_type=event_type,
                    occurred_at=timestamp,
                    attempt_id=attempt_id,
                    payload=payload,
                    previous_event_sha256=previous,
                    event_sha256=event_hash,
                )
                handle.seek(0, os.SEEK_END)
                handle.write(event.model_dump_json(exclude_none=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return event
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def claim_attempt(
        self,
        attempt: EvaluationAttempt,
        *,
        slot_sha256: str,
        retry_of_attempt_id: str | None,
        max_infra_retries: int,
    ) -> None:
        """Atomically consume a planned slot or authorize its next infra retry."""

        def _predicate(events: list[EvaluationLedgerEvent]) -> bool:
            created: list[EvaluationAttempt] = []
            latest: dict[str, EvaluationAttempt] = {}
            for event in events:
                if event.event_type != "attempt_state":
                    continue
                state = EvaluationAttempt.model_validate(event.payload["attempt"])
                latest[state.attempt_id] = state
                if (
                    state.status is AttemptStatus.CREATED
                    and state.run_plan_sha256 == attempt.run_plan_sha256
                    and event.payload.get("slot_sha256") == slot_sha256
                ):
                    created.append(state)
            if retry_of_attempt_id is None:
                if created:
                    raise EvaluationLedgerError("pre-registered slot already has an attempt")
                return True
            if not created or created[-1].attempt_id != retry_of_attempt_id:
                raise EvaluationLedgerError("retry must target the latest attempt in the slot lineage")
            original = latest.get(retry_of_attempt_id)
            if original is None or original.status is not AttemptStatus.INFRA_FAILURE:
                raise EvaluationLedgerError(
                    "only evaluator-classified infrastructure failure is retryable"
                )
            if (
                original.task_manifest_sha256 != attempt.task_manifest_sha256
                or original.repeat_index != attempt.repeat_index
                or original.seed != attempt.seed
            ):
                raise EvaluationLedgerError("retry changed the pre-registered slot identity")
            retry_count = sum(item.retry_of_attempt_id is not None for item in created)
            if retry_count >= max_infra_retries:
                raise EvaluationLedgerError(
                    "pre-registered infrastructure retry allowance is exhausted"
                )
            return True

        claimed = self._append_if(
            event_type="attempt_state",
            payload={
                "attempt": attempt.model_dump(mode="json", exclude_none=True),
                "attempt_sha256": attempt.attempt_sha256,
                "slot_sha256": slot_sha256,
            },
            predicate=_predicate,
            attempt_id=attempt.attempt_id,
        )
        if claimed is None:  # pragma: no cover - predicate always claims or raises.
            raise EvaluationLedgerError("attempt slot was not claimed")

    def attempt_states(self, attempt_id: str | None = None) -> tuple[EvaluationAttempt, ...]:
        states: list[EvaluationAttempt] = []
        for event in self.events():
            if event.event_type != "attempt_state":
                continue
            if attempt_id is not None and event.attempt_id != attempt_id:
                continue
            states.append(EvaluationAttempt.model_validate(event.payload["attempt"]))
        return tuple(states)

    def latest_attempt(self, attempt_id: str) -> EvaluationAttempt | None:
        states = self.attempt_states(attempt_id)
        return states[-1] if states else None

    def append_attempt_state(self, attempt: EvaluationAttempt, *, slot_sha256: str) -> None:
        states = self.attempt_states(attempt.attempt_id)
        previous = states[-1] if states else None
        if previous is None:
            if attempt.status is not AttemptStatus.CREATED:
                raise EvaluationLedgerError("the first attempt state must be created")
        else:
            if attempt.status not in _TRANSITIONS.get(previous.status, set()):
                raise EvaluationLedgerError(
                    f"invalid attempt transition {previous.status.value} -> {attempt.status.value}"
                )
            stable_fields = (
                "attempt_id",
                "suite_manifest_sha256",
                "run_plan_sha256",
                "task_manifest_sha256",
                "system_manifest_sha256",
                "repeat_index",
                "seed",
                "intervention_count",
                "retry_of_attempt_id",
                "retry_reason",
            )
            if any(getattr(previous, field) != getattr(attempt, field) for field in stable_fields):
                raise EvaluationLedgerError("attempt identity changed across state transitions")
            if previous.status is AttemptStatus.CREATED:
                if previous.started_at is not None or attempt.started_at is None:
                    raise EvaluationLedgerError("attempt start timestamp was not frozen once")
            elif previous.started_at != attempt.started_at:
                raise EvaluationLedgerError("attempt started_at changed across state transitions")
        self.append(
            "attempt_state",
            {
                "attempt": attempt.model_dump(mode="json", exclude_none=True),
                "attempt_sha256": attempt.attempt_sha256,
                "slot_sha256": slot_sha256,
            },
            attempt_id=attempt.attempt_id,
        )

    def slot_attempts(self, *, plan_sha256: str, slot_sha256: str) -> tuple[EvaluationAttempt, ...]:
        created: list[EvaluationAttempt] = []
        for event in self.events():
            if event.event_type != "attempt_state":
                continue
            if event.payload.get("slot_sha256") != slot_sha256:
                continue
            attempt = EvaluationAttempt.model_validate(event.payload["attempt"])
            if attempt.run_plan_sha256 == plan_sha256 and attempt.status is AttemptStatus.CREATED:
                created.append(attempt)
        return tuple(created)

    def terminal_attempt(self, attempt_id: str) -> EvaluationAttempt | None:
        attempt = self.latest_attempt(attempt_id)
        if attempt is None or attempt.status not in _TERMINAL:
            return None
        return attempt

    def assert_integrity(self) -> dict[str, Any]:
        events = self.events()
        return {
            "path": str(self.path),
            "events": len(events),
            "head_sha256": events[-1].event_sha256 if events else None,
            "file_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }
