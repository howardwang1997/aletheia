"""Postgres durable queue with leases, heartbeats, finite retries, and restart recovery."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session, aliased

from aletheia.db import session_scope
from aletheia.durable_tasks.ports import (
    DurableQueueError as DurableQueueError,
    IdempotencyConflict,
    InvalidTaskTransition,
    LeaseExpired,
    LeaseMismatch,
    QueueInvariantError,
    TaskConcurrencyConflict,
    TaskDependencyError,
    TaskNotFound,
)
from aletheia.events.bus import make_event
from aletheia.events.store import persist_event
from aletheia.jobs.contracts import (
    EnqueueReceipt,
    RecoveryReceipt,
    RetryPolicy,
    TaskAttemptSnapshot,
    TaskExecutionResult,
    TaskLease,
    TaskOutcome,
    TaskSnapshot,
    TaskSpec,
    TaskStatus,
    TerminalCategory,
)
from aletheia.jobs.persistence import (
    DurableQueueAuditRecord,
    DurableTaskAttemptRecord,
    DurableTaskDependencyRecord,
    DurableTaskRecord,
)
from aletheia.reproducibility.manifest import content_sha256


_TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}
_FAILED_DEPENDENCY_STATUSES = {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
_INTERNAL_FAILURE_CATEGORIES = {
    TerminalCategory.SUCCESS,
    TerminalCategory.LEASE_EXPIRED,
    TerminalCategory.DEPENDENCY_FAILED,
    TerminalCategory.INFRASTRUCTURE_EXHAUSTED,
}


def _aware_now(value: datetime) -> datetime:
    observed = value
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("queue timestamps must be timezone-aware")
    return observed


def _transaction_time(session: Session, supplied: datetime | None) -> datetime:
    if supplied is not None:
        return _aware_now(supplied)
    observed = session.scalar(select(func.now()))
    if observed is None:  # pragma: no cover - PostgreSQL always returns now()
        return datetime.now(timezone.utc)
    return _aware_now(observed)


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def _normalize_artifact_ids(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if any(not value or len(value) > 512 for value in normalized):
        raise ValueError("artifact ids must contain 1-512 characters")
    return normalized


class DurableTaskQueue:
    """Transactional queue service shared by the API control plane and worker processes.

    Delivery is at-least-once.  A raw lease token is returned once and only its SHA-256 is stored.
    Completion/failure callbacks are attempt-scoped and idempotent, so a late callback from a
    reclaimed worker cannot mutate the current attempt.
    """

    def __init__(self, *, principal: str = "durable_queue") -> None:
        if not principal or len(principal) > 128:
            raise ValueError("queue principal must contain 1-128 characters")
        self.principal = principal

    @staticmethod
    def _dependency_ids(session: Session, task_id: str) -> tuple[str, ...]:
        return tuple(
            session.scalars(
                select(DurableTaskDependencyRecord.dependency_task_id)
                .where(DurableTaskDependencyRecord.task_id == task_id)
                .order_by(DurableTaskDependencyRecord.dependency_task_id)
            ).all()
        )

    @classmethod
    def _task_snapshot(cls, session: Session, row: DurableTaskRecord) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=row.task_id,
            task_type=row.task_type,
            inputs_sha256=row.inputs_sha256,
            inputs=row.inputs_json,
            dependency_ids=cls._dependency_ids(session, row.task_id),
            owner=row.owner,
            run_id=row.run_id,
            idempotency_key=row.idempotency_key,
            concurrency_key=row.concurrency_key,
            request_sha256=row.request_sha256,
            retry_policy=RetryPolicy.model_validate(row.retry_policy_json),
            priority=row.priority,
            status=TaskStatus(row.status),
            attempt_count=row.attempt_count,
            state_version=row.state_version,
            available_at=row.available_at,
            active_attempt_id=row.active_attempt_id,
            lease_owner=row.lease_owner,
            lease_expires_at=row.lease_expires_at,
            result_artifact_id=row.result_artifact_id,
            result_sha256=row.result_sha256,
            result=row.result_json,
            terminal_category=(
                None if row.terminal_category is None else TerminalCategory(row.terminal_category)
            ),
            terminal_detail_sha256=row.terminal_detail_sha256,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _attempt_snapshot(row: DurableTaskAttemptRecord) -> TaskAttemptSnapshot:
        return TaskAttemptSnapshot(
            attempt_id=row.attempt_id,
            task_id=row.task_id,
            attempt_number=row.attempt_number,
            worker_id=row.worker_id,
            worker_manifest_sha256=row.worker_manifest_sha256,
            started_at=row.started_at,
            heartbeat_at=row.heartbeat_at,
            lease_expires_at=row.lease_expires_at,
            ended_at=row.ended_at,
            terminal_category=(
                None if row.terminal_category is None else TerminalCategory(row.terminal_category)
            ),
            terminal_detail_sha256=row.terminal_detail_sha256,
            retry_requested=row.retry_requested,
            retry_scheduled=row.retry_scheduled,
            partial_artifact_ids=tuple(row.partial_artifact_ids_json or []),
            logs_artifact_id=row.logs_artifact_id,
            result_artifact_id=row.result_artifact_id,
            result_sha256=row.result_sha256,
        )

    def _emit(
        self,
        session: Session,
        task: DurableTaskRecord,
        *,
        event_type: str,
        transitioned_at: datetime,
        extra: dict[str, Any] | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "schema": "aletheia.durable_task_event",
            "schema_version": 1,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "status": task.status,
            "state_version": task.state_version,
            "attempt_count": task.attempt_count,
            "active_attempt_id": task.active_attempt_id,
            "available_at": task.available_at.isoformat(),
            "terminal_category": task.terminal_category,
            "transitioned_at": transitioned_at.isoformat(),
        }
        payload.update(extra or {})
        return persist_event(
            make_event(
                event_type,
                run_id=task.run_id,
                agent=self.principal,
                payload=payload,
            ),
            event_key=f"durable-task:{task.task_id}:{task.state_version}",
            session=session,
        )

    def _transition(
        self,
        session: Session,
        task: DurableTaskRecord,
        *,
        event_type: str,
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        task.state_version += 1
        task.updated_at = now
        session.flush()
        self._emit(
            session,
            task,
            event_type=event_type,
            transitioned_at=now,
            extra=extra,
        )

    @staticmethod
    def _dependency_statuses(session: Session, task_id: str) -> tuple[str, ...]:
        dependency = aliased(DurableTaskRecord)
        return tuple(
            session.scalars(
                select(dependency.status)
                .join(
                    DurableTaskDependencyRecord,
                    DurableTaskDependencyRecord.dependency_task_id == dependency.task_id,
                )
                .where(DurableTaskDependencyRecord.task_id == task_id)
            ).all()
        )

    def _enqueue(
        self,
        spec: TaskSpec,
        *,
        now: datetime | None,
        caller_session: Session | None,
    ) -> EnqueueReceipt:
        transaction = session_scope() if caller_session is None else nullcontext(caller_session)
        with transaction as session:
            observed_at = _transaction_time(session, now)
            dependencies = []
            if spec.dependency_ids:
                dependencies = session.scalars(
                    select(DurableTaskRecord).where(
                        DurableTaskRecord.task_id.in_(spec.dependency_ids)
                    )
                ).all()
                found = {row.task_id for row in dependencies}
                missing = sorted(set(spec.dependency_ids) - found)
                if missing:
                    raise TaskDependencyError(f"dependency tasks do not exist: {missing}")

            dependency_statuses = {row.status for row in dependencies}
            if dependency_statuses & _FAILED_DEPENDENCY_STATUSES:
                status = TaskStatus.FAILED.value
                terminal_category = TerminalCategory.DEPENDENCY_FAILED.value
                terminal_detail = content_sha256(
                    {
                        "task_id": spec.task_id,
                        "failed_dependencies": sorted(
                            row.task_id
                            for row in dependencies
                            if row.status in _FAILED_DEPENDENCY_STATUSES
                        ),
                    }
                )
                completed_at = observed_at
            elif dependencies and dependency_statuses != {TaskStatus.SUCCEEDED.value}:
                status = TaskStatus.BLOCKED.value
                terminal_category = None
                terminal_detail = None
                completed_at = None
            else:
                status = TaskStatus.QUEUED.value
                terminal_category = None
                terminal_detail = None
                completed_at = None

            available_at = spec.available_at or observed_at
            values = {
                "task_id": spec.task_id,
                "run_id": spec.run_id,
                "task_type": spec.task_type,
                "inputs_sha256": spec.inputs_sha256,
                "inputs_json": spec.inputs,
                "owner": spec.owner,
                "idempotency_key": spec.idempotency_key,
                "concurrency_key": spec.concurrency_key,
                "request_sha256": spec.request_sha256,
                "retry_policy_json": spec.retry_policy.model_dump(mode="json"),
                "priority": spec.priority,
                "status": status,
                "attempt_count": 0,
                "state_version": 1,
                "available_at": available_at,
                "terminal_category": terminal_category,
                "terminal_detail_sha256": terminal_detail,
                "created_at": observed_at,
                "updated_at": observed_at,
                "completed_at": completed_at,
            }
            inserted = session.scalar(
                postgresql_insert(DurableTaskRecord)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(DurableTaskRecord.task_id)
            )
            session.flush()
            if inserted is None:
                predicates = [
                    DurableTaskRecord.task_id == spec.task_id,
                    DurableTaskRecord.idempotency_key == spec.idempotency_key,
                ]
                if spec.concurrency_key is not None:
                    predicates.append(
                        (DurableTaskRecord.concurrency_key == spec.concurrency_key)
                        & DurableTaskRecord.status.in_(
                            (
                                TaskStatus.BLOCKED.value,
                                TaskStatus.QUEUED.value,
                                TaskStatus.LEASED.value,
                                TaskStatus.RETRY_WAIT.value,
                            )
                        )
                    )
                conflicts = session.scalars(select(DurableTaskRecord).where(or_(*predicates))).all()
                if (
                    len(conflicts) != 1
                    or conflicts[0].task_id != spec.task_id
                    or conflicts[0].idempotency_key != spec.idempotency_key
                    or conflicts[0].request_sha256 != spec.request_sha256
                ):
                    concurrency_conflict = next(
                        (
                            row
                            for row in conflicts
                            if spec.concurrency_key is not None
                            and row.concurrency_key == spec.concurrency_key
                            and row.status not in _TERMINAL_STATUSES
                        ),
                        None,
                    )
                    if concurrency_conflict is not None:
                        raise TaskConcurrencyConflict(
                            "active task already owns the concurrency key",
                            existing_task_id=concurrency_conflict.task_id,
                        )
                    raise IdempotencyConflict(
                        "task id or idempotency key is already bound to different content"
                    )
                return EnqueueReceipt(
                    task=self._task_snapshot(session, conflicts[0]),
                    created=False,
                )

            row = session.get(DurableTaskRecord, spec.task_id)
            if row is None:  # pragma: no cover - guarded by INSERT RETURNING
                raise QueueInvariantError("inserted task is not readable")
            for dependency_id in spec.dependency_ids:
                session.add(
                    DurableTaskDependencyRecord(
                        task_id=spec.task_id,
                        dependency_task_id=dependency_id,
                    )
                )
            session.flush()
            self._emit(
                session,
                row,
                event_type="durable_task_enqueued",
                transitioned_at=observed_at,
                extra={"dependency_ids": list(spec.dependency_ids)},
            )
            return EnqueueReceipt(task=self._task_snapshot(session, row), created=True)

    def enqueue(self, spec: TaskSpec, *, now: datetime | None = None) -> EnqueueReceipt:
        """Enqueue and commit one task in a queue-owned transaction."""

        return self._enqueue(spec, now=now, caller_session=None)

    def enqueue_in_session(
        self,
        session: Session,
        spec: TaskSpec,
        *,
        now: datetime | None = None,
    ) -> EnqueueReceipt:
        """Enqueue inside a caller-owned transaction without committing it.

        This is the atomic composition seam for durable control-plane delivery records.  The
        caller owns commit or rollback; queue events and dependency rows use the same session.
        """

        if not isinstance(session, Session):
            raise TypeError("enqueue_in_session requires a SQLAlchemy Session")
        return self._enqueue(spec, now=now, caller_session=session)

    def get(self, task_id: str) -> TaskSnapshot:
        with session_scope() as session:
            return self.get_in_session(session, task_id)

    def get_in_session(
        self,
        session: Session,
        task_id: str,
        *,
        lock_for_update: bool = False,
    ) -> TaskSnapshot:
        """Read one task in a caller-owned transaction, optionally locking its state row."""

        if not isinstance(session, Session):
            raise TypeError("get_in_session requires a SQLAlchemy Session")
        statement = select(DurableTaskRecord).where(DurableTaskRecord.task_id == task_id)
        if lock_for_update:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise TaskNotFound(f"durable task not found: {task_id}")
        return self._task_snapshot(session, row)

    def list(
        self,
        *,
        run_id: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 500,
    ) -> tuple[TaskSnapshot, ...]:
        if not 1 <= limit <= 5_000:
            raise ValueError("task list limit must be between 1 and 5000")
        with session_scope() as session:
            query = select(DurableTaskRecord)
            if run_id is not None:
                query = query.where(DurableTaskRecord.run_id == run_id)
            if status is not None:
                query = query.where(DurableTaskRecord.status == status.value)
            rows = session.scalars(
                query.order_by(DurableTaskRecord.created_at.asc()).limit(limit)
            ).all()
            return tuple(self._task_snapshot(session, row) for row in rows)

    def attempts(self, task_id: str) -> tuple[TaskAttemptSnapshot, ...]:
        with session_scope() as session:
            if session.get(DurableTaskRecord, task_id) is None:
                raise TaskNotFound(f"durable task not found: {task_id}")
            rows = session.scalars(
                select(DurableTaskAttemptRecord)
                .where(DurableTaskAttemptRecord.task_id == task_id)
                .order_by(DurableTaskAttemptRecord.attempt_number.asc())
            ).all()
            return tuple(self._attempt_snapshot(row) for row in rows)

    def _expire_task(
        self,
        session: Session,
        task: DurableTaskRecord,
        *,
        now: datetime,
    ) -> bool:
        if task.active_attempt_id is None:
            raise QueueInvariantError(f"leased task {task.task_id} has no active attempt")
        attempt = session.get(DurableTaskAttemptRecord, task.active_attempt_id)
        if attempt is None or attempt.ended_at is not None:
            raise QueueInvariantError(f"leased task {task.task_id} has an invalid active attempt")
        attempt.ended_at = now
        attempt.terminal_category = TerminalCategory.LEASE_EXPIRED.value
        detail = content_sha256(
            {
                "task_id": task.task_id,
                "attempt_id": attempt.attempt_id,
                "lease_expires_at": attempt.lease_expires_at.isoformat(),
            }
        )
        attempt.terminal_detail_sha256 = detail

        policy = RetryPolicy.model_validate(task.retry_policy_json)
        reclaimable = (
            TerminalCategory.LEASE_EXPIRED in policy.retryable_categories
            and task.attempt_count < policy.max_attempts
        )
        if reclaimable:
            task.status = TaskStatus.RETRY_WAIT.value
            task.available_at = now + timedelta(seconds=policy.backoff_seconds(task.attempt_count))
            task.terminal_category = None
            task.terminal_detail_sha256 = None
            task.completed_at = None
        else:
            task.status = TaskStatus.FAILED.value
            task.terminal_category = TerminalCategory.INFRASTRUCTURE_EXHAUSTED.value
            task.terminal_detail_sha256 = detail
            task.completed_at = now
        attempt.retry_requested = True
        attempt.retry_scheduled = reclaimable
        expired_attempt_id = task.active_attempt_id
        task.active_attempt_id = None
        task.lease_owner = None
        task.lease_token_sha256 = None
        task.lease_expires_at = None
        self._transition(
            session,
            task,
            event_type="durable_task_lease_expired",
            now=now,
            extra={
                "expired_attempt_id": expired_attempt_id,
                "reclaimable": reclaimable,
            },
        )
        return reclaimable

    def _recover_expired(
        self,
        session: Session,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[list[str], list[str]]:
        rows = session.scalars(
            select(DurableTaskRecord)
            .where(
                DurableTaskRecord.status == TaskStatus.LEASED.value,
                DurableTaskRecord.lease_expires_at <= now,
            )
            .order_by(DurableTaskRecord.lease_expires_at.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        recovered: list[str] = []
        terminalized: list[str] = []
        for task in rows:
            if self._expire_task(session, task, now=now):
                recovered.append(task.task_id)
            else:
                terminalized.append(task.task_id)
        return recovered, terminalized

    def _resolve_blocked(self, session: Session, *, now: datetime) -> list[str]:
        dependency_failed: list[str] = []
        while True:
            changed = False
            rows = session.scalars(
                select(DurableTaskRecord)
                .where(DurableTaskRecord.status == TaskStatus.BLOCKED.value)
                .order_by(DurableTaskRecord.created_at.asc())
                .with_for_update(skip_locked=True)
            ).all()
            for task in rows:
                statuses = self._dependency_statuses(session, task.task_id)
                if any(status in _FAILED_DEPENDENCY_STATUSES for status in statuses):
                    failed_ids = session.scalars(
                        select(DurableTaskDependencyRecord.dependency_task_id)
                        .join(
                            DurableTaskRecord,
                            DurableTaskRecord.task_id
                            == DurableTaskDependencyRecord.dependency_task_id,
                        )
                        .where(
                            DurableTaskDependencyRecord.task_id == task.task_id,
                            DurableTaskRecord.status.in_(_FAILED_DEPENDENCY_STATUSES),
                        )
                    ).all()
                    detail = content_sha256(
                        {"task_id": task.task_id, "failed_dependencies": sorted(failed_ids)}
                    )
                    task.status = TaskStatus.FAILED.value
                    task.terminal_category = TerminalCategory.DEPENDENCY_FAILED.value
                    task.terminal_detail_sha256 = detail
                    task.completed_at = now
                    self._transition(
                        session,
                        task,
                        event_type="durable_task_dependency_failed",
                        now=now,
                        extra={"failed_dependency_ids": sorted(failed_ids)},
                    )
                    dependency_failed.append(task.task_id)
                    changed = True
                elif statuses and all(status == TaskStatus.SUCCEEDED.value for status in statuses):
                    task.status = TaskStatus.QUEUED.value
                    self._transition(
                        session,
                        task,
                        event_type="durable_task_dependencies_ready",
                        now=now,
                    )
                    changed = True
            if not changed:
                return dependency_failed

    def recover_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> RecoveryReceipt:
        if not 1 <= limit <= 100_000:
            raise ValueError("recovery limit must be between 1 and 100000")
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            recovered, terminalized = self._recover_expired(session, now=observed_at, limit=limit)
            dependency_failed = self._resolve_blocked(session, now=observed_at)
            payload = {
                "recovered_task_ids": sorted(recovered),
                "terminalized_task_ids": sorted(terminalized),
                "dependency_failed_task_ids": sorted(dependency_failed),
                "recovered_at": observed_at.isoformat(),
            }
            session.add(
                DurableQueueAuditRecord(
                    audit_type="lease_recovery",
                    principal=self.principal,
                    payload_json=payload,
                    created_at=observed_at,
                )
            )
            return RecoveryReceipt(
                recovered_task_ids=tuple(sorted(recovered)),
                terminalized_task_ids=tuple(sorted(terminalized)),
                dependency_failed_task_ids=tuple(sorted(dependency_failed)),
                recovered_at=observed_at,
            )

    def claim(
        self,
        *,
        worker_id: str,
        worker_manifest_sha256: str,
        task_types: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> TaskLease | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker id must contain 1-128 characters")
        _require_sha256(worker_manifest_sha256, label="worker manifest")
        accepted_types = tuple(sorted(set(task_types or ())))
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            self._recover_expired(session, now=observed_at, limit=1_000)
            self._resolve_blocked(session, now=observed_at)

            dependency = aliased(DurableTaskRecord)
            unsatisfied_dependency = exists(
                select(1)
                .select_from(DurableTaskDependencyRecord)
                .join(
                    dependency,
                    dependency.task_id == DurableTaskDependencyRecord.dependency_task_id,
                )
                .where(
                    DurableTaskDependencyRecord.task_id == DurableTaskRecord.task_id,
                    dependency.status != TaskStatus.SUCCEEDED.value,
                )
            )
            query = select(DurableTaskRecord).where(
                DurableTaskRecord.status.in_(
                    (TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value)
                ),
                DurableTaskRecord.available_at <= observed_at,
                ~unsatisfied_dependency,
            )
            if accepted_types:
                query = query.where(DurableTaskRecord.task_type.in_(accepted_types))
            task = session.scalar(
                query.order_by(
                    DurableTaskRecord.priority.desc(),
                    DurableTaskRecord.available_at.asc(),
                    DurableTaskRecord.created_at.asc(),
                    DurableTaskRecord.task_id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                return None

            policy = RetryPolicy.model_validate(task.retry_policy_json)
            if task.attempt_count >= policy.max_attempts:
                raise QueueInvariantError(f"exhausted task remained claimable: {task.task_id}")
            raw_token = secrets.token_urlsafe(32)
            token_sha256 = _token_sha256(raw_token)
            attempt_id = uuid.uuid4().hex
            attempt_number = task.attempt_count + 1
            lease_expires_at = observed_at + timedelta(seconds=policy.lease_seconds)
            attempt = DurableTaskAttemptRecord(
                attempt_id=attempt_id,
                task_id=task.task_id,
                attempt_number=attempt_number,
                worker_id=worker_id,
                worker_manifest_sha256=worker_manifest_sha256,
                lease_token_sha256=token_sha256,
                started_at=observed_at,
                heartbeat_at=observed_at,
                lease_expires_at=lease_expires_at,
                partial_artifact_ids_json=[],
            )
            session.add(attempt)
            task.status = TaskStatus.LEASED.value
            task.attempt_count = attempt_number
            task.active_attempt_id = attempt_id
            task.lease_owner = worker_id
            task.lease_token_sha256 = token_sha256
            task.lease_expires_at = lease_expires_at
            task.terminal_category = None
            task.terminal_detail_sha256 = None
            task.completed_at = None
            self._transition(
                session,
                task,
                event_type="durable_task_leased",
                now=observed_at,
                extra={
                    "attempt_id": attempt_id,
                    "attempt_number": attempt_number,
                    "worker_id": worker_id,
                    "worker_manifest_sha256": worker_manifest_sha256,
                    "lease_expires_at": lease_expires_at.isoformat(),
                },
            )
            return TaskLease(
                task=self._task_snapshot(session, task),
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                worker_id=worker_id,
                worker_manifest_sha256=worker_manifest_sha256,
                lease_token=raw_token,
                lease_expires_at=lease_expires_at,
            )

    @staticmethod
    def _verify_attempt_token(attempt: DurableTaskAttemptRecord, lease: TaskLease) -> None:
        if (
            attempt.attempt_id != lease.attempt_id
            or attempt.task_id != lease.task.task_id
            or attempt.attempt_number != lease.attempt_number
            or attempt.worker_id != lease.worker_id
            or attempt.worker_manifest_sha256 != lease.worker_manifest_sha256
            or not hmac.compare_digest(
                attempt.lease_token_sha256,
                _token_sha256(lease.lease_token),
            )
        ):
            raise LeaseMismatch("attempt identity or lease token does not match")

    @classmethod
    def _verify_active_lease(
        cls,
        task: DurableTaskRecord,
        attempt: DurableTaskAttemptRecord,
        lease: TaskLease,
        *,
        now: datetime,
    ) -> None:
        cls._verify_attempt_token(attempt, lease)
        if (
            task.status != TaskStatus.LEASED.value
            or task.active_attempt_id != attempt.attempt_id
            or task.lease_owner != lease.worker_id
            or task.lease_token_sha256 is None
            or not hmac.compare_digest(
                task.lease_token_sha256,
                _token_sha256(lease.lease_token),
            )
            or attempt.ended_at is not None
        ):
            raise LeaseMismatch("attempt no longer owns the task lease")
        if task.lease_expires_at is None or task.lease_expires_at <= now:
            raise LeaseExpired("task lease has expired and must be recovered")

    def heartbeat(self, lease: TaskLease, *, now: datetime | None = None) -> TaskLease:
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            task = session.scalar(
                select(DurableTaskRecord)
                .where(DurableTaskRecord.task_id == lease.task.task_id)
                .with_for_update()
            )
            attempt = session.get(DurableTaskAttemptRecord, lease.attempt_id)
            if task is None or attempt is None:
                raise TaskNotFound("leased task or attempt no longer exists")
            self._verify_active_lease(task, attempt, lease, now=observed_at)
            policy = RetryPolicy.model_validate(task.retry_policy_json)
            expires_at = observed_at + timedelta(seconds=policy.lease_seconds)
            task.lease_expires_at = expires_at
            attempt.heartbeat_at = observed_at
            attempt.lease_expires_at = expires_at
            self._transition(
                session,
                task,
                event_type="durable_task_heartbeat",
                now=observed_at,
                extra={
                    "attempt_id": attempt.attempt_id,
                    "worker_id": attempt.worker_id,
                    "lease_expires_at": expires_at.isoformat(),
                },
            )
            return TaskLease(
                task=self._task_snapshot(session, task),
                attempt_id=attempt.attempt_id,
                attempt_number=attempt.attempt_number,
                worker_id=attempt.worker_id,
                worker_manifest_sha256=attempt.worker_manifest_sha256,
                lease_token=lease.lease_token,
                lease_expires_at=expires_at,
            )

    def complete(
        self,
        lease: TaskLease,
        result: TaskExecutionResult,
        *,
        now: datetime | None = None,
    ) -> TaskOutcome:
        result_sha256 = content_sha256(
            {"result_artifact_id": result.result_artifact_id, "result": result.result}
        )
        partial_ids = _normalize_artifact_ids(result.partial_artifact_ids)
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            task = session.scalar(
                select(DurableTaskRecord)
                .where(DurableTaskRecord.task_id == lease.task.task_id)
                .with_for_update()
            )
            attempt = session.get(DurableTaskAttemptRecord, lease.attempt_id)
            if task is None or attempt is None:
                raise TaskNotFound("leased task or attempt no longer exists")
            self._verify_attempt_token(attempt, lease)
            if attempt.ended_at is not None:
                if (
                    attempt.terminal_category == TerminalCategory.SUCCESS.value
                    and attempt.result_artifact_id == result.result_artifact_id
                    and attempt.result_sha256 == result_sha256
                    and tuple(attempt.partial_artifact_ids_json or []) == partial_ids
                    and attempt.logs_artifact_id == result.logs_artifact_id
                ):
                    return TaskOutcome(
                        task=self._task_snapshot(session, task),
                        attempt=self._attempt_snapshot(attempt),
                        replayed=True,
                    )
                raise InvalidTaskTransition("completed attempt was replayed with different output")

            self._verify_active_lease(task, attempt, lease, now=observed_at)
            attempt.ended_at = observed_at
            attempt.terminal_category = TerminalCategory.SUCCESS.value
            attempt.retry_requested = False
            attempt.retry_scheduled = False
            attempt.partial_artifact_ids_json = list(partial_ids)
            attempt.logs_artifact_id = result.logs_artifact_id
            attempt.result_artifact_id = result.result_artifact_id
            attempt.result_sha256 = result_sha256
            task.status = TaskStatus.SUCCEEDED.value
            task.result_artifact_id = result.result_artifact_id
            task.result_sha256 = result_sha256
            task.result_json = result.result
            task.terminal_category = TerminalCategory.SUCCESS.value
            task.terminal_detail_sha256 = None
            task.completed_at = observed_at
            task.active_attempt_id = None
            task.lease_owner = None
            task.lease_token_sha256 = None
            task.lease_expires_at = None
            self._transition(
                session,
                task,
                event_type="durable_task_succeeded",
                now=observed_at,
                extra={
                    "attempt_id": attempt.attempt_id,
                    "result_artifact_id": result.result_artifact_id,
                    "result_sha256": result_sha256,
                    "partial_artifact_ids": list(partial_ids),
                    "partials_are_evidence": False,
                },
            )
            self._resolve_blocked(session, now=observed_at)
            return TaskOutcome(
                task=self._task_snapshot(session, task),
                attempt=self._attempt_snapshot(attempt),
                replayed=False,
            )

    def fail(
        self,
        lease: TaskLease,
        *,
        category: TerminalCategory,
        detail_sha256: str,
        partial_artifact_ids: Iterable[str] = (),
        logs_artifact_id: str | None = None,
        retry: bool = True,
        now: datetime | None = None,
    ) -> TaskOutcome:
        if category in _INTERNAL_FAILURE_CATEGORIES:
            raise ValueError(f"workers cannot report internal category {category.value!r}")
        _require_sha256(detail_sha256, label="failure detail")
        partial_ids = _normalize_artifact_ids(partial_artifact_ids)
        if logs_artifact_id is not None and not 1 <= len(logs_artifact_id) <= 512:
            raise ValueError("logs artifact id must contain 1-512 characters")
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            task = session.scalar(
                select(DurableTaskRecord)
                .where(DurableTaskRecord.task_id == lease.task.task_id)
                .with_for_update()
            )
            attempt = session.get(DurableTaskAttemptRecord, lease.attempt_id)
            if task is None or attempt is None:
                raise TaskNotFound("leased task or attempt no longer exists")
            self._verify_attempt_token(attempt, lease)
            if attempt.ended_at is not None:
                if (
                    attempt.terminal_category == category.value
                    and attempt.terminal_detail_sha256 == detail_sha256
                    and tuple(attempt.partial_artifact_ids_json or []) == partial_ids
                    and attempt.logs_artifact_id == logs_artifact_id
                    and attempt.retry_requested is retry
                ):
                    return TaskOutcome(
                        task=self._task_snapshot(session, task),
                        attempt=self._attempt_snapshot(attempt),
                        replayed=True,
                    )
                raise InvalidTaskTransition("failed attempt was replayed with different content")

            self._verify_active_lease(task, attempt, lease, now=observed_at)
            attempt.ended_at = observed_at
            attempt.terminal_category = category.value
            attempt.terminal_detail_sha256 = detail_sha256
            attempt.partial_artifact_ids_json = list(partial_ids)
            attempt.logs_artifact_id = logs_artifact_id
            policy = RetryPolicy.model_validate(task.retry_policy_json)
            retryable = (
                retry
                and category in policy.retryable_categories
                and task.attempt_count < policy.max_attempts
            )
            attempt.retry_requested = retry
            attempt.retry_scheduled = retryable
            if retryable:
                task.status = TaskStatus.RETRY_WAIT.value
                task.available_at = observed_at + timedelta(
                    seconds=policy.backoff_seconds(task.attempt_count)
                )
                task.terminal_category = None
                task.terminal_detail_sha256 = None
                task.completed_at = None
                event_type = "durable_task_retry_scheduled"
            else:
                task.status = (
                    TaskStatus.CANCELLED.value
                    if category == TerminalCategory.CANCELLED
                    else TaskStatus.FAILED.value
                )
                task.terminal_category = category.value
                task.terminal_detail_sha256 = detail_sha256
                task.completed_at = observed_at
                event_type = "durable_task_failed"
            task.active_attempt_id = None
            task.lease_owner = None
            task.lease_token_sha256 = None
            task.lease_expires_at = None
            self._transition(
                session,
                task,
                event_type=event_type,
                now=observed_at,
                extra={
                    "attempt_id": attempt.attempt_id,
                    "attempt_category": category.value,
                    "detail_sha256": detail_sha256,
                    "retry_scheduled": retryable,
                    "partial_artifact_ids": list(partial_ids),
                    "partials_are_evidence": False,
                },
            )
            if not retryable:
                self._resolve_blocked(session, now=observed_at)
            return TaskOutcome(
                task=self._task_snapshot(session, task),
                attempt=self._attempt_snapshot(attempt),
                replayed=False,
            )
