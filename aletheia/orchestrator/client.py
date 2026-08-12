"""One-shot provider-neutral orchestrator runtime (Phase 0).

A selected Claude or OpenAI model runs a single task and streams canonical events
to the event bus, exposing the same provider-neutral ``memory_log`` tool. The multi-turn scoping
conversation lives in :mod:`aletheia.orchestrator.session`.

Auth, tools, and the tool gate are shared modules (``auth`` / ``tools`` / ``gate``).
Credential helpers are re-exported here for backward compatibility.
"""

from __future__ import annotations

import asyncio

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.memory.service import log_note
from aletheia.orchestrator.auth import (  # noqa: F401 (re-exported)
    has_credentials,
    machine_has_claude_login,
)
from aletheia.orchestrator.codex_cli import machine_has_codex_subscription  # noqa: F401
from aletheia.orchestrator.tools import build_memory_tool
from aletheia.orchestrator.worker import is_degraded, run_worker

__all__ = [
    "run_task",
    "run_real",
    "run_dryrun",
    "has_credentials",
    "machine_has_claude_login",
    "machine_has_codex_subscription",
]

SYSTEM_PROMPT = (
    "You are Aletheia, an autonomous AI scientist running in a lights-out lab. "
    "Work methodically and record consequential steps. Whenever you make a decision "
    "or reach a conclusion, call the `memory_log` tool to persist a concise note to "
    "the experiment work log (the single source of truth). Keep notes terse and factual."
)


async def run_real(run_id: str, prompt: str) -> None:
    settings = get_settings()
    bus = get_bus()

    await bus.publish(make_event(
        "run_started", run_id=run_id,
        payload={
            "prompt": prompt,
            "mode": "real",
            "provider": settings.orchestrator_provider,
            "transport": settings.orchestrator_transport,
            "model": settings.orchestrator_model,
        },
    ))
    try:
        result = await run_worker(
            run_id, "orchestrator", prompt,
            system=SYSTEM_PROMPT,
            tools=[build_memory_tool(run_id)],
            allowed_tools=["mcp__aletheia__memory_log"],
            max_turns=5,
            dry_run=False,
        )
        if is_degraded(result):
            raise RuntimeError(result)
        await bus.publish(make_event("run_finished", run_id=run_id, payload={"status": "completed"}))
    except Exception as exc:  # noqa: BLE001 — record then re-raise
        await bus.publish(make_event("error", run_id=run_id, payload={"error": str(exc)}))
        raise


async def run_dryrun(run_id: str, prompt: str) -> None:
    """Simulate one orchestrator turn end-to-end (no SDK, no credentials)."""
    bus = get_bus()
    await bus.publish(make_event("run_started", run_id=run_id, payload={"prompt": prompt, "mode": "dry_run"}))
    await bus.publish(
        make_event(
            "assistant_text",
            run_id=run_id,
            payload={"text": "[dry-run] Acknowledged. Recording a work-log entry."},
        )
    )
    await asyncio.sleep(0.1)
    await bus.publish(
        make_event(
            "tool_use",
            run_id=run_id,
            payload={"tool": "mcp__aletheia__memory_log", "input": {"note": "dry-run skeleton check"}},
        )
    )
    decision_id = await asyncio.to_thread(
        log_note, run_id, "dry-run skeleton check: ledger + event bus + SSE wired", "orchestrator"
    )
    await bus.publish(
        make_event("memory_log", run_id=run_id, payload={"note": "dry-run skeleton check", "decision_id": decision_id})
    )
    await bus.publish(
        make_event("result", run_id=run_id, payload={"result": "dry-run complete", "cost_usd": 0.0})
    )
    await bus.publish(make_event("run_finished", run_id=run_id, payload={"status": "completed"}))


async def run_task(run_id: str, prompt: str, dry_run: bool | None = None) -> None:
    """Dispatch to the real SDK run or the dry-run skeleton."""
    settings = get_settings()
    if dry_run is None:
        dry_run = not has_credentials(settings)
    if dry_run:
        await run_dryrun(run_id, prompt)
    else:
        await run_real(run_id, prompt)
