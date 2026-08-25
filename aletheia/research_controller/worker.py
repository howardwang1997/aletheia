"""Durable-task handler for ``research.controller.v1``."""

from __future__ import annotations

from collections.abc import Callable

from aletheia.durable_tasks.contracts import TaskExecutionResult, TaskSnapshot, TaskStatus
from aletheia.research_controller.contracts import (
    CONTROLLER_TASK_TYPE,
    ResearchControllerManifest,
    ResearchControllerTaskInput,
    controller_task_spec,
)
from aletheia.research_controller.service import ResearchControllerService


def research_controller_task_handler(
    *,
    manifest: ResearchControllerManifest,
    service: ResearchControllerService,
) -> Callable[[TaskSnapshot], TaskExecutionResult]:
    """Bind deployment-pinned authority and return a standard durable worker handler."""

    def handle(task: TaskSnapshot) -> TaskExecutionResult:
        if task.task_type != CONTROLLER_TASK_TYPE:
            raise ValueError(f"unsupported controller task type: {task.task_type}")
        if task.run_id is not None:
            raise ValueError("Research Kernel controller tasks cannot synthesize a legacy Run")
        inputs = ResearchControllerTaskInput.model_validate(task.inputs)
        if (
            inputs.controller_id != manifest.controller_id
            or inputs.controller_manifest_sha256 != manifest.manifest_sha256
        ):
            raise ValueError("controller task differs from the deployment-pinned manifest")
        expected = controller_task_spec(
            manifest=manifest,
            wakeup=inputs.wakeup,
            delivery_sha256=inputs.delivery_sha256,
            delivery_generation=inputs.delivery_generation,
            supersedes_task_id=inputs.supersedes_task_id,
        )
        if (
            task.task_id != expected.task_id
            or task.inputs_sha256 != expected.inputs_sha256
            or task.inputs != expected.inputs
            or task.dependency_ids != expected.dependency_ids
            or task.owner != expected.owner
            or task.idempotency_key != expected.idempotency_key
            or task.concurrency_key != expected.concurrency_key
            or task.request_sha256 != expected.request_sha256
            or task.retry_policy != expected.retry_policy
            or task.priority != expected.priority
        ):
            raise ValueError("controller task differs from its frozen deterministic envelope")
        if (
            task.status is not TaskStatus.LEASED
            or task.attempt_count < 1
            or task.active_attempt_id is None
            or task.lease_owner is None
            or task.lease_expires_at is None
        ):
            raise ValueError("controller handler requires an active durable-task lease")
        receipt = service.tick(inputs.wakeup)
        return TaskExecutionResult(
            result_artifact_id=f"research-controller-receipt:{receipt.receipt_sha256}",
            result=receipt.model_dump(mode="json"),
        )

    return handle


__all__ = ["research_controller_task_handler"]
