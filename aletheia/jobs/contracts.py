"""Compatibility re-exports for the authority-neutral durable-task contracts.

New authority code imports ``aletheia.durable_tasks.contracts`` directly so importing a task
schema never executes the legacy ``aletheia.jobs`` package initializer.
"""

from aletheia.durable_tasks.contracts import (
    DurableTaskModel,
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
    canonical_payload,
    new_task_id,
)

__all__ = [
    "DurableTaskModel",
    "EnqueueReceipt",
    "RecoveryReceipt",
    "RetryPolicy",
    "TaskAttemptSnapshot",
    "TaskExecutionResult",
    "TaskLease",
    "TaskOutcome",
    "TaskSnapshot",
    "TaskSpec",
    "TaskStatus",
    "TerminalCategory",
    "canonical_payload",
    "new_task_id",
]
