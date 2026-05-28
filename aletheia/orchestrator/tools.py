"""In-process MCP tools exposed to the orchestrator.

These are the *only* tools the scoping conversation may use (enforced by the
tool gate). ``memory_log`` writes work-log notes; ``finalize_goal`` records the
agreed, structured experiment plan — the hand-off artifact for Phase 1.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
