"""Durable task control-plane endpoints; execution lives in independent workers."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from aletheia.jobs.contracts import (
    EnqueueReceipt,
    RecoveryReceipt,
    TaskAttemptSnapshot,
    TaskSnapshot,
    TaskSpec,
    TaskStatus,
)
from aletheia.jobs.queue import (
    DurableTaskQueue,
    IdempotencyConflict,
    TaskDependencyError,
    TaskNotFound,
)

router = APIRouter(prefix="/tasks", tags=["durable-tasks"])
_QUEUE = DurableTaskQueue(principal="api:durable_tasks")


@router.post("", response_model=EnqueueReceipt)
async def enqueue_task(spec: TaskSpec) -> EnqueueReceipt:
    try:
        return await asyncio.to_thread(_QUEUE.enqueue, spec)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TaskDependencyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=tuple[TaskSnapshot, ...])
async def list_tasks(
    run_id: str | None = None,
    status: TaskStatus | None = None,
    limit: int = 500,
) -> tuple[TaskSnapshot, ...]:
    try:
        return await asyncio.to_thread(
            _QUEUE.list,
            run_id=run_id,
            status=status,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{task_id}", response_model=TaskSnapshot)
async def get_task(task_id: str) -> TaskSnapshot:
    try:
        return await asyncio.to_thread(_QUEUE.get, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{task_id}/attempts", response_model=tuple[TaskAttemptSnapshot, ...])
async def get_task_attempts(task_id: str) -> tuple[TaskAttemptSnapshot, ...]:
    try:
        return await asyncio.to_thread(_QUEUE.attempts, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/operations/recover-expired", response_model=RecoveryReceipt)
async def recover_expired_tasks(limit: int = 1_000) -> RecoveryReceipt:
    try:
        return await asyncio.to_thread(_QUEUE.recover_expired, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
