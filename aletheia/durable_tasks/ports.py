"""Narrow authority-neutral ports for durable engineering-task delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from aletheia.durable_tasks.contracts import EnqueueReceipt, TaskSnapshot, TaskSpec


class DurableQueueError(RuntimeError):
    """Base class for durable queue contract violations."""


class TaskNotFound(DurableQueueError):
    pass


class TaskDependencyError(DurableQueueError):
    pass


class IdempotencyConflict(DurableQueueError):
    pass


class TaskConcurrencyConflict(IdempotencyConflict):
    def __init__(self, message: str, *, existing_task_id: str) -> None:
        super().__init__(message)
        self.existing_task_id = existing_task_id


class InvalidTaskTransition(DurableQueueError):
    pass


class LeaseMismatch(DurableQueueError):
    pass


class LeaseExpired(DurableQueueError):
    pass


class QueueInvariantError(DurableQueueError):
    pass


class DurableTaskQueuePort(Protocol):
    """Caller-owned transaction seam used by protected controller adapters."""

    def enqueue_in_session(
        self,
        session: Session,
        spec: TaskSpec,
        *,
        now: datetime | None = None,
    ) -> EnqueueReceipt: ...

    def get_in_session(
        self,
        session: Session,
        task_id: str,
        *,
        lock_for_update: bool = False,
    ) -> TaskSnapshot: ...


__all__ = [
    "DurableQueueError",
    "DurableTaskQueuePort",
    "IdempotencyConflict",
    "InvalidTaskTransition",
    "LeaseExpired",
    "LeaseMismatch",
    "QueueInvariantError",
    "TaskConcurrencyConflict",
    "TaskDependencyError",
    "TaskNotFound",
]
