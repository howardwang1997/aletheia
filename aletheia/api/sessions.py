"""Conversational scoping endpoints.

A "session" is a Run in the `scoping` stage backed by a long-lived
ConversationSession. The dashboard creates one, streams `/events?run_id=…`, and
posts user messages; the agent converses and finalizes a plan.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aletheia.memory.service import create_run, get_run
from aletheia.orchestrator.session import auto_dry_run, get_session_manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    goal_seed: str | None = None
    domain: str | None = None
    dry_run: bool | None = None  # None -> auto (dry-run if no credentials)


class CreateSessionResponse(BaseModel):
    run_id: str
    mode: str


class MessageRequest(BaseModel):
    text: str


@router.post("", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    dry = req.dry_run if req.dry_run is not None else auto_dry_run()
    run_id = await asyncio.to_thread(
        create_run, req.goal_seed or "(scoping)", req.domain, None, None, None, None, "scoping"
    )
    manager = get_session_manager()
    manager.create(run_id, dry)
    if req.goal_seed:
        await manager.send(run_id, req.goal_seed)
    return CreateSessionResponse(run_id=run_id, mode="dry_run" if dry else "real")


@router.post("/{run_id}/messages")
async def send_message(run_id: str, req: MessageRequest) -> dict:
    ok = await get_session_manager().send(run_id, req.text)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "queued"}


@router.post("/{run_id}/interrupt")
async def interrupt(run_id: str) -> dict:
    ok = await get_session_manager().interrupt(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="session not found")
    return {"status": "interrupted"}


@router.get("/{run_id}")
async def get_session(run_id: str) -> dict:
    run = await asyncio.to_thread(get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run
