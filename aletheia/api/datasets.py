"""Dataset endpoints — the human's "connect data pipelines" surface.

Lets the dashboard (or a script) register data for a run two ways:
  * by reference (a benchmark name or an API dataset id) — JSON body;
  * by file upload (CSV/parquet) — multipart, profiled on arrival.
Both feed the launch-time data-readiness gate (see ``aletheia/data/registry.py``).
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from aletheia.data.registry import (
    attach_local,
    attach_upload,
    attach_url,
    list_datasets,
    mark_ready,
    register_dataset,
)
from aletheia.events.bus import get_bus, make_event
from aletheia.paths import run_data_dir

router = APIRouter(prefix="/runs", tags=["datasets"])


class RegisterDatasetRequest(BaseModel):
    source: str  # benchmark | upload | api
    role: Literal["primary", "external_validation"] = "primary"
    ref: str | None = None  # benchmark name / api dataset id
    target_column: str | None = None
    composition_column: str | None = None
    content_sha256: str | None = None
    feature_kind: str | None = "composition"
    description: str | None = None
    # benchmark/api are auto-downloadable -> ready by default; uploads come via /upload
    ready: bool = True


@router.get("/{run_id}/datasets")
async def get_datasets(run_id: str) -> list[dict]:
    return await asyncio.to_thread(list_datasets, run_id)


# input "source" values that mean "a local filesystem path" (a single file OR a
# directory) — attach_local auto-detects which and sets the canonical source.
_LOCAL_PATH_SOURCES = {"path", "local", "file", "directory", "dir", "folder"}


@router.post("/{run_id}/datasets")
async def register(run_id: str, req: RegisterDatasetRequest) -> dict:
    src = (req.source or "").lower().strip()

    # 1) a local path (file or directory) — the on-disk case. attach_local profiles
    #    it now (so the profile seeds scoping + the readiness gate opens) and records
    #    the canonical source (upload for a file, directory for a folder).
    if src in _LOCAL_PATH_SOURCES:
        if not req.ref:
            raise HTTPException(400, "ref (the local path) is required")
        try:
            asset = await asyncio.to_thread(
                attach_local,
                run_id,
                req.ref,
                role=req.role,
                target_column=req.target_column,
                composition_column=req.composition_column,
                feature_kind=req.feature_kind,
                description=req.description,
            )
        except FileNotFoundError as e:
            raise HTTPException(400, f"path not found / no tabular files: {e}")
        except Exception as e:  # surface a clear error instead of 500
            raise HTTPException(400, f"could not connect path: {e}")
        return await _registered(run_id, asset)

    # 2) an online URL (download + extract archives)
    if src == "url":
        if not req.ref:
            raise HTTPException(400, "ref (the URL) is required")
        try:
            asset = await asyncio.to_thread(
                attach_url,
                run_id,
                req.ref,
                role=req.role,
                target_column=req.target_column,
                composition_column=req.composition_column,
                feature_kind=req.feature_kind,
                description=req.description,
            )
        except Exception as e:
            raise HTTPException(400, f"could not connect url: {e}")
        return await _registered(run_id, asset)

    # 3) a named benchmark / keyed api source — recorded now, fetched at load time
    if src in ("benchmark", "api"):
        status = "ready" if req.ready else "needed"
        asset_id = await asyncio.to_thread(
            register_dataset,
            run_id,
            src,
            role=req.role,
            ref=req.ref,
            target_column=req.target_column,
            composition_column=req.composition_column,
            content_sha256=req.content_sha256,
            feature_kind=req.feature_kind,
            description=req.description,
            status=status,
            requested_by="human",
        )
        await get_bus().publish(
            make_event(
                "data_registered",
                run_id=run_id,
                payload={"asset_id": asset_id, "source": src, "ref": req.ref, "status": status},
            )
        )
        return {"asset_id": asset_id, "status": status}

    raise HTTPException(
        400,
        "source must be one of: path (file or directory) | url | benchmark | api "
        "(ref is required for path/url)",
    )


async def _registered(run_id: str, asset: dict | None) -> dict:
    if asset is None:
        raise HTTPException(400, "dataset could not be registered")
    await get_bus().publish(
        make_event(
            "data_registered",
            run_id=run_id,
            payload={
                "asset_id": asset["id"],
                "source": asset["source"],
                "ref": asset.get("ref"),
                "status": asset["status"],
                "profile": asset.get("profile"),
            },
        )
    )
    return asset


@router.post("/{run_id}/datasets/upload")
async def upload(
    run_id: str,
    file: UploadFile = File(...),
    target_column: str | None = Form(None),
    composition_column: str | None = Form(None),
    role: str = Form("primary"),
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
        role=role,
        target_column=target_column,
        composition_column=composition_column,
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
