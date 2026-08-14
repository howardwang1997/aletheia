"""MCP tools that let the orchestrator submit/poll compute jobs without ever
running training in its own context. The lifecycle driver uses the backend
directly; these expose the same capability to a Claude-driven execution stage.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aletheia.compute.base import JobSpec
from aletheia.compute.factory import get_compute_backend
from aletheia.data.registry import list_datasets
from aletheia.events.bus import get_bus, make_event


def _asset_spec(d: dict[str, Any]) -> dict[str, Any]:
    spec = {
        "asset_id": d["id"],
        "role": d.get("role") or "primary",
        "source": d["source"],
        "ref": d["ref"],
        "uri": d["uri"],
        "target_column": d["target_column"],
        "feature_kind": d["feature_kind"],
        "content_sha256": d.get("content_sha256"),
        "profile": d.get("profile"),
    }
    if d.get("composition_column"):
        spec["composition_column"] = d["composition_column"]
    return spec


def resolve_data_spec(run_id: str) -> dict[str, Any]:
    """Build a data spec from the first ready *primary* DataAsset."""
    for d in list_datasets(run_id):
        if d["status"] == "ready" and (d.get("role") or "primary") == "primary":
            return _asset_spec(d)
    return {"source": "benchmark", "ref": "matbench_expt_gap"}


def resolve_external_data_spec(run_id: str) -> dict[str, Any] | None:
    """Resolve exactly one ready external-validation asset, without exposing it as training data."""
    ready = [
        d for d in list_datasets(run_id)
        if d["status"] == "ready" and d.get("role") == "external_validation"
    ]
    if len(ready) > 1:
        raise RuntimeError("a campaign may seal exactly one external-validation dataset")
    return _asset_spec(ready[0]) if ready else None


def build_compute_tools(
    run_id: str, experiment_id: str | None, domain: str = "materials", dry_run: bool = False
):
    """Return ``[compute_submit, compute_status]`` bound to this run/experiment."""
    from claude_agent_sdk import tool

    @tool(
        "compute_submit",
        "Submit a training/evaluation job for the current experiment design. Pass "
        "the design as a JSON string with keys like model, model_params, test_size.",
        {"design_json": str},
    )
    async def compute_submit(args: dict[str, Any]) -> dict[str, Any]:
        try:
            design = json.loads(args.get("design_json") or "{}")
        except json.JSONDecodeError as e:
            return {"content": [{"type": "text", "text": f"Invalid design_json: {e}"}]}
        spec = JobSpec(
            run_id=run_id,
            domain=domain,
            design=design,
            data_spec=resolve_data_spec(run_id),
            experiment_id=experiment_id,
            dry_run=dry_run,
        )
        job_id = await asyncio.to_thread(get_compute_backend().submit, spec)
        await get_bus().publish(
            make_event(
                "compute_submitted", run_id=run_id, payload={"job_id": job_id, "design": design}
            )
        )
        return {"content": [{"type": "text", "text": f"Submitted job {job_id}."}]}

    @tool(
        "compute_status",
        "Check a submitted job; returns its status and (when finished) the metrics.",
        {"job_id": str},
    )
    async def compute_status(args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id", "")).strip()
        st = await asyncio.to_thread(get_compute_backend().status, job_id)
        await get_bus().publish(
            make_event(
                "compute_status",
                run_id=run_id,
                payload={"job_id": job_id, "status": st.status, "metrics": st.metrics},
            )
        )
        text = f"Job {job_id}: {st.status}"
        if st.metrics:
            text += f" — metrics={st.metrics}"
        if st.error:
            text += f" — error={st.error}"
        return {"content": [{"type": "text", "text": text}]}

    return [compute_submit, compute_status]
