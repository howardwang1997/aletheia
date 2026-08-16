"""FastAPI application — the gateway between the dashboard and the lab."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aletheia.api import auth, datasets, events_sse, programs, runs, sessions, tasks
from aletheia.api.deps import require_access
from aletheia.auth.users import bootstrap_owner
from aletheia.db import require_schema_current
from aletheia.orchestrator.session import get_session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runtime code never mutates the schema. Deployments migrate explicitly and startup refuses
    # empty, stale, future, or unversioned databases.
    require_schema_current()
    bootstrap_owner()  # seed the owner's local login from settings (idempotent)
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

# /auth is open (it issues the session); everything else requires a valid session.
# require_access reads for any role, restricts mutations to owner/operator.
_protected = [Depends(require_access)]
app.include_router(auth.router)
app.include_router(runs.router, dependencies=_protected)
app.include_router(sessions.router, dependencies=_protected)
app.include_router(datasets.router, dependencies=_protected)
app.include_router(events_sse.router, dependencies=_protected)
app.include_router(tasks.router, dependencies=_protected)
# Program routes resolve the same dependency themselves so mutation provenance can bind the
# authenticated user rather than accepting a caller-supplied principal.
app.include_router(programs.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok", "service": "aletheia"}


@app.get("/readyz", tags=["meta"])
async def readyz() -> dict:
    status = require_schema_current()
    return {
        "status": "ready",
        "service": "aletheia",
        "schema_revision": status.current_revision,
    }
