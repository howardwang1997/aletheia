"""Durable queue adapter for the existing experiment driver."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aletheia.jobs import (
    DurableTaskQueue,
    EnqueueReceipt,
    RetryPolicy,
    TaskExecutionResult,
    TaskConcurrencyConflict,
    TaskSnapshot,
    TaskSpec,
)
from aletheia.memory.service import get_run
from aletheia.reproducibility.manifest import content_sha256

RESEARCH_DRIVER_TASK_TYPE = "research.experiment_driver.v1"


class DriverTaskInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    dry_run: bool
    operation_id: str = Field(min_length=1, max_length=128)


def enqueue_driver_task(
    run_id: str,
    *,
    dry_run: bool,
    operation_id: str | None = None,
    queue: DurableTaskQueue | None = None,
) -> EnqueueReceipt:
    """Idempotently place one launch/resume operation on the durable worker queue."""

    operation = operation_id or uuid.uuid4().hex
    inputs = DriverTaskInputs(run_id=run_id, dry_run=dry_run, operation_id=operation)
    operation_sha256 = content_sha256(inputs)
    spec = TaskSpec(
        task_id=f"driver-{run_id}-{operation_sha256[:16]}",
        task_type=RESEARCH_DRIVER_TASK_TYPE,
        inputs=inputs.model_dump(mode="json"),
        owner="scheduler-api",
        run_id=run_id,
        idempotency_key=f"driver:{run_id}:{operation_sha256}",
        concurrency_key=f"driver:{run_id}",
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=300,
            heartbeat_interval_seconds=60,
            initial_backoff_seconds=30,
            max_backoff_seconds=300,
        ),
    )
    service = queue or DurableTaskQueue(principal="scheduler:api")
    try:
        return service.enqueue(spec)
    except TaskConcurrencyConflict as exc:
        existing = service.get(exc.existing_task_id)
        existing_inputs = DriverTaskInputs.model_validate(existing.inputs)
        if existing_inputs.run_id == run_id and existing_inputs.dry_run == dry_run:
            return EnqueueReceipt(task=existing, created=False)
        raise


async def run_driver_task(task: TaskSnapshot) -> TaskExecutionResult:
    """Built-in worker handler; the raw lease never crosses into the scientific driver."""

    if task.task_type != RESEARCH_DRIVER_TASK_TYPE:
        raise ValueError(f"unsupported scheduler task type: {task.task_type}")
    inputs = DriverTaskInputs.model_validate(task.inputs)
    if task.run_id != inputs.run_id:
        raise ValueError("driver task run_id conflicts with its queue scope")

    # Imported lazily so API control-plane processes do not instantiate driver dependencies.
    from aletheia.scheduler.driver import ExperimentDriver

    await ExperimentDriver(inputs.run_id, dry_run=inputs.dry_run).run()
    run: dict[str, Any] | None = get_run(inputs.run_id)
    return TaskExecutionResult(
        result_artifact_id=f"ledger:run:{inputs.run_id}",
        result={
            "run_id": inputs.run_id,
            "run_status": None if run is None else run.get("status"),
            "operation_id": inputs.operation_id,
        },
    )


__all__ = [
    "DriverTaskInputs",
    "RESEARCH_DRIVER_TASK_TYPE",
    "enqueue_driver_task",
    "run_driver_task",
]
