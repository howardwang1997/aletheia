"""In-process MCP tools exposed to the orchestrator.

These are the *only* tools the scoping conversation may use (enforced by the
tool gate). ``memory_log`` writes work-log notes; ``finalize_goal`` records the
agreed, structured experiment plan — the hand-off artifact for Phase 1.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aletheia.data.registry import get_dataset, list_datasets, register_dataset
from aletheia.events.bus import get_bus, make_event
from aletheia.memory.service import finalize_plan, log_note

# finalize_goal input schema — a complete, structured experiment plan.
FINALIZE_GOAL_FIELDS: dict[str, Any] = {
    "objective": str,
    "domain": str,
    "direction": str,
    "hypothesis": str,
    "dataset": str,
    "method": str,
    "baselines": str,
    "metrics": str,
    "success_criteria": str,
    "risks": str,
    "est_compute": str,
}


def build_memory_tool(run_id: str):
    """A ``memory_log`` MCP tool bound to this run via closure."""
    from claude_agent_sdk import tool

    @tool(
        "memory_log",
        "Append a concise note to the experiment work log (persisted to the ledger).",
        {"note": str},
    )
    async def memory_log(args: dict[str, Any]) -> dict[str, Any]:
        note = str(args.get("note", "")).strip()
        decision_id = await asyncio.to_thread(log_note, run_id, note, "orchestrator")
        await get_bus().publish(
            make_event("memory_log", run_id=run_id, payload={"note": note, "decision_id": decision_id})
        )
        return {"content": [{"type": "text", "text": f"Logged work-log entry #{decision_id}."}]}

    return memory_log


def build_recall_tool(run_id: str):
    """A ``memory_recall`` MCP tool: semantic search over past research (across all
    runs) so scoping plans against prior hypotheses, designs, critiques, and
    conclusions instead of re-running dead ends."""
    from claude_agent_sdk import tool

    from aletheia.memory.vector import recall

    @tool(
        "memory_recall",
        "Search past research memory (all runs) for work relevant to a query — "
        "prior hypotheses, designs, critic findings, and conclusions. Use this "
        "while scoping to avoid repeating approaches that already failed or "
        "converged. Returns the most similar fragments.",
        {"query": str},
    )
    async def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        hits = await asyncio.to_thread(recall, query, run_id=None)
        if not hits:
            text = "No relevant prior research found in memory."
        else:
            lines = [
                f"- [{h['kind']}, sim={h['similarity']:.2f}] {h['text'][:240]}"
                for h in hits
            ]
            text = "Relevant prior research:\n" + "\n".join(lines)
        await get_bus().publish(
            make_event("memory_recall", run_id=run_id, payload={"query": query, "n_hits": len(hits)})
        )
        return {"content": [{"type": "text", "text": text}]}

    return memory_recall


def build_finalize_goal_tool(run_id: str):
    """A ``finalize_goal`` MCP tool: record the agreed experiment plan."""
    from claude_agent_sdk import tool

    @tool(
        "finalize_goal",
        "Record the agreed, concrete experiment plan once the user has confirmed "
        "the research goal. Call this exactly once when scoping is complete.",
        FINALIZE_GOAL_FIELDS,
    )
    async def finalize_goal(args: dict[str, Any]) -> dict[str, Any]:
        plan = {k: str(args.get(k, "")).strip() for k in FINALIZE_GOAL_FIELDS}
        experiment_id = await asyncio.to_thread(finalize_plan, run_id, plan)
        await get_bus().publish(
            make_event(
                "goal_finalized",
                run_id=run_id,
                payload={"plan": plan, "experiment_id": experiment_id},
            )
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Experiment plan recorded (experiment #{experiment_id}). "
                        "The goal is finalized and ready for Phase-1 execution."
                    ),
                }
            ]
        }

    return finalize_goal


# --- data provisioning tools (scoping phase) -------------------------------

# request_data: agent declares a data need (pull scenario).
REQUEST_DATA_FIELDS: dict[str, Any] = {
    "description": str,  # what data is needed and why
    "source": str,  # benchmark | upload | api
    "ref": str,  # benchmark name / api dataset id (optional)
    "target_column": str,  # the prediction target (optional)
    "feature_kind": str,  # e.g. "composition" (optional)
}


def build_request_data_tool(run_id: str):
    """``request_data``: Aletheia declares a dataset it needs. Creates a 'needed'
    DataAsset that surfaces on the dashboard for the human to satisfy."""
    from claude_agent_sdk import tool

    @tool(
        "request_data",
        "Declare a dataset you need to run the experiment. source is one of: "
        "'benchmark' (a known public dataset — give its name as ref), 'upload' (ask "
        "the human for a file), 'directory' (a folder of files), 'url' (an online "
        "file or .zip/.tar.gz — give the link as ref), or 'api' (a keyed source). "
        "The human satisfies it before launch.",
        REQUEST_DATA_FIELDS,
    )
    async def request_data(args: dict[str, Any]) -> dict[str, Any]:
        source = (str(args.get("source", "")).strip() or "benchmark").lower()
        asset_id = await asyncio.to_thread(
            register_dataset,
            run_id,
            source,
            ref=str(args.get("ref", "")).strip() or None,
            target_column=str(args.get("target_column", "")).strip() or None,
            feature_kind=str(args.get("feature_kind", "")).strip() or None,
            description=str(args.get("description", "")).strip() or None,
            status="needed",
            requested_by="agent",
        )
        await get_bus().publish(
            make_event(
                "data_requested",
                run_id=run_id,
                payload={
                    "asset_id": asset_id,
                    "source": source,
                    "ref": args.get("ref"),
                    "description": args.get("description"),
                },
            )
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Recorded data request #{asset_id} (source={source}). "
                        "It is now shown to the human to provide; launch is blocked "
                        "until it is ready."
                    ),
                }
            ]
        }

    return request_data


def build_inspect_dataset_tool(run_id: str):
    """``inspect_dataset``: read the profile of registered datasets (push scenario)
    so the agent designs against the real columns/target."""
    from claude_agent_sdk import tool

    @tool(
        "inspect_dataset",
        "Inspect datasets already provided for this run (columns, dtypes, row count, "
        "target candidates). Pass asset_id to inspect one, or leave empty for all.",
        {"asset_id": str},
    )
    async def inspect_dataset(args: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(args.get("asset_id", "")).strip()
        if asset_id:
            one = await asyncio.to_thread(get_dataset, asset_id)
            assets = [one] if one else []
        else:
            assets = await asyncio.to_thread(list_datasets, run_id)
        if not assets:
            text = "No datasets are registered for this run yet."
        else:
            lines = []
            for a in assets:
                prof = a.get("profile") or {}
                lines.append(
                    f"- #{a['id']} [{a['status']}] source={a['source']} ref={a.get('ref')}\n"
                    f"  rows={prof.get('n_rows')} columns={prof.get('columns')}\n"
                    f"  target_candidates={prof.get('target_candidates')} "
                    f"composition_candidates={prof.get('composition_candidates')}"
                )
            text = "Registered datasets:\n" + "\n".join(lines)
        return {"content": [{"type": "text", "text": text}]}

    return inspect_dataset
