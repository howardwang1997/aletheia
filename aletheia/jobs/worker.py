"""Independent durable worker runtime with automatic lease heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping

from aletheia.jobs.contracts import (
    TaskExecutionResult,
    TaskOutcome,
    TaskSnapshot,
    TerminalCategory,
)
from aletheia.jobs.queue import DurableQueueError, DurableTaskQueue
from aletheia.reproducibility.manifest import content_sha256

TaskHandler = Callable[
    [TaskSnapshot],
    TaskExecutionResult | Awaitable[TaskExecutionResult],
]


class WorkerTaskFailure(RuntimeError):
    """Typed handler failure that can safely be translated into queue state."""

    def __init__(
        self,
        message: str,
        *,
        category: TerminalCategory,
        retry: bool,
        partial_artifact_ids: tuple[str, ...] = (),
        logs_artifact_id: str | None = None,
    ) -> None:
        if category in {
            TerminalCategory.SUCCESS,
            TerminalCategory.LEASE_EXPIRED,
            TerminalCategory.DEPENDENCY_FAILED,
            TerminalCategory.INFRASTRUCTURE_EXHAUSTED,
        }:
            raise ValueError(f"invalid worker failure category: {category.value}")
        super().__init__(message)
        self.category = category
        self.retry = retry
        self.partial_artifact_ids = partial_artifact_ids
        self.logs_artifact_id = logs_artifact_id

    @property
    def detail_sha256(self) -> str:
        return content_sha256(
            {
                "exception_type": type(self).__qualname__,
                "category": self.category.value,
                "message": str(self),
            }
        )


class InfrastructureTaskFailure(WorkerTaskFailure):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(
            message,
            category=TerminalCategory.INFRASTRUCTURE,
            retry=True,
            **kwargs,
        )


class ScientificTaskFailure(WorkerTaskFailure):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(
            message,
            category=TerminalCategory.SCIENTIFIC,
            retry=False,
            **kwargs,
        )


class InvalidTaskOutput(WorkerTaskFailure):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(
            message,
            category=TerminalCategory.INVALID_OUTPUT,
            retry=False,
            **kwargs,
        )


class DurableWorker:
    """Claims only registered task types and keeps the active attempt leased while it runs."""

    def __init__(
        self,
        *,
        worker_id: str,
        worker_manifest_sha256: str,
        handlers: Mapping[str, TaskHandler],
        queue: DurableTaskQueue | None = None,
    ) -> None:
        if not handlers:
            raise ValueError("a durable worker needs at least one registered handler")
        self.worker_id = worker_id
        self.worker_manifest_sha256 = worker_manifest_sha256
        self.handlers = dict(handlers)
        self.queue = queue or DurableTaskQueue(principal=f"worker:{worker_id}")

    async def _invoke(self, task: TaskSnapshot) -> TaskExecutionResult:
        handler = self.handlers[task.task_type]
        if inspect.iscoroutinefunction(handler):
            value = await handler(task)
        else:
            value = await asyncio.to_thread(handler, task)
            if inspect.isawaitable(value):
                value = await value
        if not isinstance(value, TaskExecutionResult):
            raise InvalidTaskOutput("handler did not return TaskExecutionResult")
        return value

    async def _execute_with_heartbeat(self, lease) -> TaskExecutionResult:
        stop = asyncio.Event()

        async def heartbeat_loop() -> None:
            interval = lease.task.retry_policy.heartbeat_interval_seconds
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    await asyncio.to_thread(self.queue.heartbeat, lease)

        execution = asyncio.create_task(self._invoke(lease.task))
        heartbeat = asyncio.create_task(heartbeat_loop())
        try:
            done, _pending = await asyncio.wait(
                {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                heartbeat_exception = heartbeat.exception()
                if heartbeat_exception is not None:
                    execution.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await execution
                    raise heartbeat_exception
                raise RuntimeError("heartbeat loop stopped before task execution")
            return await execution
        finally:
            stop.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def run_once(self) -> TaskOutcome | None:
        lease = await asyncio.to_thread(
            self.queue.claim,
            worker_id=self.worker_id,
            worker_manifest_sha256=self.worker_manifest_sha256,
            task_types=self.handlers,
        )
        if lease is None:
            return None
        try:
            result = await self._execute_with_heartbeat(lease)
        except asyncio.CancelledError:
            # Process cancellation deliberately leaves the lease for restart recovery.  It must
            # not be mislabeled as a scientific failure.
            raise
        except DurableQueueError:
            # Lost/expired ownership cannot safely mutate this attempt; recovery owns it now.
            raise
        except WorkerTaskFailure as exc:
            return await asyncio.to_thread(
                self.queue.fail,
                lease,
                category=exc.category,
                detail_sha256=exc.detail_sha256,
                partial_artifact_ids=exc.partial_artifact_ids,
                logs_artifact_id=exc.logs_artifact_id,
                retry=exc.retry,
            )
        except Exception as exc:
            detail_sha256 = content_sha256(
                {
                    "exception_type": type(exc).__qualname__,
                    "message": str(exc),
                }
            )
            return await asyncio.to_thread(
                self.queue.fail,
                lease,
                category=TerminalCategory.INFRASTRUCTURE,
                detail_sha256=detail_sha256,
                retry=True,
            )
        return await asyncio.to_thread(self.queue.complete, lease, result)

    async def run_forever(
        self,
        *,
        stop: asyncio.Event | None = None,
        idle_seconds: float = 1.0,
    ) -> None:
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be positive")
        stop_event = stop or asyncio.Event()
        await asyncio.to_thread(self.queue.recover_expired)
        while not stop_event.is_set():
            outcome = await self.run_once()
            if outcome is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_seconds)
                except TimeoutError:
                    pass


__all__ = [
    "DurableWorker",
    "InfrastructureTaskFailure",
    "InvalidTaskOutput",
    "ScientificTaskFailure",
    "TaskHandler",
    "WorkerTaskFailure",
]
