"""Dataset endpoints — the human's "connect data pipelines" surface.

Lets the dashboard (or a script) register data for a run two ways:
  * by reference (a benchmark name or an API dataset id) — JSON body;
  * by file upload (CSV/parquet) — multipart, profiled on arrival.
Both feed the launch-time data-readiness gate (see ``aletheia/data/registry.py``).
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from aletheia.data.registry import attach_upload, list_datasets, mark_ready, register_dataset
from aletheia.events.bus import get_bus, make_event
from aletheia.memory.ledger import DATA_SOURCES
from aletheia.paths import run_data_dir

router = APIRouter(prefix="/runs", tags=["datasets"])


class RegisterDatasetRequest(BaseModel):
    source: str  # benchmark | upload | api
    ref: str | None = None  # benchmark name / api dataset id
    target_column: str | None = None
    feature_kind: str | None = "composition"
    description: str | None = None
    # benchmark/api are auto-downloadable -> ready by default; uploads come via /upload
    ready: bool = True


@router.get("/{run_id}/datasets")
async def get_datasets(run_id: str) -> list[dict]:
    return await asyncio.to_thread(list_datasets, run_id)


@router.post("/{run_id}/datasets")
async def register(run_id: str, req: RegisterDatasetRequest) -> dict:
    if req.source not in DATA_SOURCES:
        raise HTTPException(400, f"source must be one of {DATA_SOURCES}")
    status = "ready" if req.ready else "needed"
    asset_id = await asyncio.to_thread(
        register_dataset,
        run_id,
        req.source,
        ref=req.ref,
        target_column=req.target_column,
        feature_kind=req.feature_kind,
        description=req.description,
        status=status,
        requested_by="human",
    )
    await get_bus().publish(
        make_event(
            "data_registered",
            run_id=run_id,
            payload={"asset_id": asset_id, "source": req.source, "ref": req.ref, "status": status},
        )
    )
    return {"asset_id": asset_id, "status": status}


@router.post("/{run_id}/datasets/upload")
async def upload(
    run_id: str,
    file: UploadFile = File(...),
    target_column: str | None = Form(None),
    feature_kind: str | None = Form("composition"),
    description: str | None = Form(None),
    asset_id: str | None = Form(None),  # satisfy an existing pull request
) -> dict:
    name = os.path.basename(file.filename or "dataset.csv")
    dest = run_data_dir(run_id) / name
    content = await file.read()
    dest.write_bytes(content)
    asset = await asyncio.to_thread(
        attach_upload,
        run_id,
        str(dest),
        asset_id=asset_id,
        target_column=target_column,
        feature_kind=feature_kind,
        description=description,
    )
    if asset is None:
        raise HTTPException(404, "dataset asset not found")
    await get_bus().publish(
        make_event(
            "data_registered",
            run_id=run_id,
            payload={
                "asset_id": asset["id"],
                "source": "upload",
                "uri": asset["uri"],
                "status": "ready",
                "profile": asset.get("profile"),
            },
        )
    )
    return asset


@router.post("/{run_id}/datasets/{asset_id}/ready")
async def satisfy(run_id: str, asset_id: str) -> dict:
    """Mark a pull-requested benchmark/api dataset as satisfied (e.g. key dropped)."""
    asset = await asyncio.to_thread(mark_ready, asset_id)
    if asset is None:
        raise HTTPException(404, "dataset asset not found")
    await get_bus().publish(
        make_event("data_registered", run_id=run_id, payload={"asset_id": asset_id, "status": "ready"})
    )
    return asset
