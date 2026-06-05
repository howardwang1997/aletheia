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

import asyncio
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

_DEGRADED_PREFIX = "[worker-unavailable"
# extra outer attempts on a transient API error (the SDK already retries internally)
_OUTER_ATTEMPTS = 2
_BACKOFF_S = 8.0

# Built-in tools a text-only worker must NOT have. Without this, a worker running under
# ``bypassPermissions`` keeps the full default Claude Code toolset, so the model may "help"
# by WRITING its answer to a file (e.g. the coder doing ``Write('/tmp/demo.py', ...)``) and
# returning only prose — then ``extract_code`` gets prose, not a fenced block, and the gate
# rejects it. Text-only workers must return their answer INLINE. (mcp-tool workers opt back
# in via ``allowed_tools``.)
_NO_TOOLS: list[str] = [
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "Bash", "BashOutput",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "KillBash",
]


def degraded_marker(label: str) -> str:
    """Sentinel a worker returns when it could not produce real output (e.g. a
    transient API overload that survived the SDK's own retries). Downstream code
    should ignore degraded results rather than treat the text as a real answer."""
    return f"{_DEGRADED_PREFIX}: {label}]"


def is_degraded(text: str | None) -> bool:
    return (text or "").startswith(_DEGRADED_PREFIX)


def _looks_like_api_error(text: str) -> bool:
    """The SDK surfaces an exhausted-retry API failure as its final assistant text
    (e.g. 'API Error: 529 Overloaded ...'); treat empty output the same way."""
    t = (text or "").strip()
    return (not t) or t.startswith("API Error:") or ("Overloaded" in t[:120])


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
        # text-only worker: no tools. Force an INLINE reply so ``extract_code`` /
        # ``_parse_json`` see the actual answer instead of prose left after a Write tool call.
        opts["permission_mode"] = "bypassPermissions"
        opts["allowed_tools"] = []
        opts["disallowed_tools"] = list(_NO_TOOLS)
    options = ClaudeAgentOptions(**opts)

    last = ""
    for attempt in range(1, _OUTER_ATTEMPTS + 1):
        chunks: list[str] = []
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    for evt in normalize_message(msg, run_id, agent=label):
                        await get_bus().publish(evt)
                        if evt["type"] == "assistant_text":
                            chunks.append((evt.get("payload") or {}).get("text", ""))
            last = "\n".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001 - transient client/transport failures
            last = f"API Error: {exc}"
        if not _looks_like_api_error(last):
            return last
        if attempt < _OUTER_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_S * attempt)
    # transient failure survived the SDK's retries + our outer attempts: degrade
    # cleanly so downstream ignores it instead of ingesting an error string.
    await get_bus().publish(
        make_event("worker_degraded", run_id=run_id, agent=label,
                   payload={"label": label, "reason": last[:200]})
    )
    return degraded_marker(label)
