"""Isolated worker: one focused Claude (Opus) task in its own context.

A *worker* runs a single, relatively-independent unit of reasoning in a **fresh,
isolated** ``ClaudeSDKClient`` session and returns only its text. Because each call
is isolated, the FSM driver's main context never accumulates a worker's internal
turns — it keeps only the structured result. Independent workers are plain
coroutines, so callers fan them out with ``asyncio.gather`` for parallel,
context-isolated sub-tasks (e.g. the decomposed ANALYSIS sub-checks).

This generalizes the old single-shot ``reason_stage`` (now a thin shim): same
streaming-to-the-event-bus behavior and dry-run path, plus an optional model
override, tool wiring, and multi-turn allowance.
"""

from __future__ import annotations

from typing import Any

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.events.normalizer import normalize_message
from aletheia.orchestrator.auth import configure_auth, has_credentials

STAGE_SYSTEM = (
    "You are Aletheia, an autonomous AI scientist executing one stage of a research "
    "loop in a lights-out lab. Be rigorous, concrete, and concise. When asked for "
    "JSON, return ONLY a single JSON object."
)


async def run_worker(
    run_id: str,
    label: str,
    prompt: str,
    *,
    system: str = STAGE_SYSTEM,
    model: str | None = None,
    mcp_servers: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    can_use_tool: Any = None,
    max_turns: int = 1,
    dry_run: bool,
    dry_value: str | None = None,
) -> str:
    """Run one isolated worker task and return its text. Streams events tagged with
    ``label`` (the dashboard lane / ledger ``agent``)."""
    settings = get_settings()
    if dry_run or not has_credentials(settings):
        text = dry_value or f"[dry-run] {label} complete."
        await get_bus().publish(
            make_event("assistant_text", run_id=run_id, agent=label, payload={"text": text})
        )
        return text

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    configure_auth(settings)
    opts: dict[str, Any] = dict(
        model=model or settings.claude_model,
        system_prompt=system,
        setting_sources=[],
        max_turns=max_turns,
        max_budget_usd=settings.budget_usd,
    )
    if mcp_servers:
        opts["mcp_servers"] = mcp_servers
        opts["allowed_tools"] = allowed_tools or []
        # gated (no human prompt) when a tool gate is supplied; else autonomous
        opts["permission_mode"] = "default" if can_use_tool else "bypassPermissions"
        if can_use_tool:
            opts["can_use_tool"] = can_use_tool
    else:
        opts["permission_mode"] = "bypassPermissions"
    options = ClaudeAgentOptions(**opts)

    chunks: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            for evt in normalize_message(msg, run_id, agent=label):
                await get_bus().publish(evt)
                if evt["type"] == "assistant_text":
                    chunks.append((evt.get("payload") or {}).get("text", ""))
    return "\n".join(chunks).strip()
