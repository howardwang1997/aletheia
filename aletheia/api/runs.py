"""Run lifecycle endpoints: start a run, list runs, read a run's event history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from aletheia.events.store import list_events
from aletheia.memory.service import create_run, list_runs
from aletheia.orchestrator.client import run_task

router = APIRouter(prefix="/runs", tags=["runs"])

# Keep strong refs to background tasks so they aren't garbage-collected.
_TASKS: set[asyncio.Task] = set()


class StartRunRequest(BaseModel):
    goal: str
    prompt: str | None = None
    domain: str | None = None
    dry_run: bool | None = None  # None -> auto (dry-run if no credentials)


class StartRunResponse(BaseModel):
    run_id: str
    mode: str


@router.post("", response_model=StartRunResponse)
async def start_run(req: StartRunRequest) -> StartRunResponse:
    run_id = await asyncio.to_thread(create_run, req.goal, req.domain)
    prompt = req.prompt or (
        f"Begin work on this goal and log a brief plan: {req.goal}"
    )
    task = asyncio.create_task(run_task(run_id, prompt, dry_run=req.dry_run))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    mode = "dry_run" if req.dry_run else "auto" if req.dry_run is None else "real"
    return StartRunResponse(run_id=run_id, mode=mode)


@router.get("")
async def get_runs() -> list[dict]:
    return await asyncio.to_thread(list_runs)


@router.get("/{run_id}/events")
async def get_run_events(run_id: str, limit: int = 500) -> list[dict]:
    return await asyncio.to_thread(list_events, run_id, limit)
