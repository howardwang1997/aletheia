"""FastAPI application — the gateway between the dashboard and the lab."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aletheia.api import datasets, events_sse, runs, sessions
from aletheia.db import create_all
from aletheia.orchestrator.session import get_session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 0 convenience: ensure tables exist. Alembic owns migrations later.
    create_all()
    yield
    # Shut down any live conversation sessions cleanly.
    await get_session_manager().close_all()


app = FastAPI(title="Aletheia", version="0.0.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs.router)
app.include_router(sessions.router)
app.include_router(datasets.router)
app.include_router(events_sse.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "aletheia"}
