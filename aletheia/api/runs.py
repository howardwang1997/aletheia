"""Run lifecycle endpoints: start a run, list runs, read a run's event history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aletheia.data.registry import all_ready, pending_datasets
from aletheia.events.store import list_events
from aletheia.memory.service import (
    create_run,
    get_run,
    list_claims,
    list_literature_findings,
    list_runs,
    list_scorecards,
    list_sota_results,
)
from aletheia.orchestrator.client import run_task
from aletheia.orchestrator.session import auto_dry_run
from aletheia.scheduler.driver import launch_driver

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


@router.get("/{run_id}/claims")
async def get_run_claims(run_id: str, experiment_id: str | None = None) -> list[dict]:
    """The evidence ledger: every claim this run made + its strength/status + evidence
    refs — so a reviewer can see what is supported vs speculative."""
    return await asyncio.to_thread(list_claims, run_id, experiment_id)


@router.get("/{run_id}/literature")
async def get_run_literature(run_id: str) -> list[dict]:
    """Structured prior-work rows the SURVEY distilled (method/dataset/metric/result/gap)."""
    return await asyncio.to_thread(list_literature_findings, run_id)


@router.get("/{run_id}/sota")
async def get_run_sota(run_id: str, domain: str | None = None) -> list[dict]:
    """Structured published SOTA rows the run compares against."""
    return await asyncio.to_thread(list_sota_results, run_id, domain)


@router.get("/{run_id}/scorecards")
async def get_run_scorecards(run_id: str, experiment_id: str | None = None) -> list[dict]:
    """Pre-execution hypothesis scorecards (per-dimension scores + proceed/block)."""
    return await asyncio.to_thread(list_scorecards, run_id, experiment_id)


class LaunchRequest(BaseModel):
    dry_run: bool | None = None  # None -> auto (dry-run if selected provider lacks credentials)


class LaunchResponse(BaseModel):
    run_id: str
    status: str
    mode: str


@router.post("/{run_id}/launch", response_model=LaunchResponse)
async def launch(run_id: str, req: LaunchRequest) -> LaunchResponse:
    """Start the lights-out experiment loop. Gated on data-readiness: refuses until
    every declared dataset is ready (the human's connect-data step)."""
    run = await asyncio.to_thread(get_run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if not await asyncio.to_thread(all_ready, run_id):
        pending = await asyncio.to_thread(pending_datasets, run_id)
        raise HTTPException(
            409,
            {
                "error": "data not ready",
                "pending": [
                    {"id": p["id"], "source": p["source"], "ref": p["ref"], "status": p["status"]}
                    for p in pending
                ],
            },
        )
    dry_run = req.dry_run if req.dry_run is not None else auto_dry_run()
    launch_driver(run_id, dry_run=dry_run)
    return LaunchResponse(run_id=run_id, status="launched", mode="dry_run" if dry_run else "real")


@router.post("/{run_id}/resume", response_model=LaunchResponse)
async def resume(run_id: str, req: LaunchRequest) -> LaunchResponse:
    """Resume a paused run by re-launching the driver loop."""
    run = await asyncio.to_thread(get_run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    dry_run = req.dry_run if req.dry_run is not None else auto_dry_run()
    launch_driver(run_id, dry_run=dry_run)
    return LaunchResponse(run_id=run_id, status="resumed", mode="dry_run" if dry_run else "real")
