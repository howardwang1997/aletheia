"""The orchestrator runtime.

Phase 0: a long-lived ``ClaudeSDKClient`` (Claude Opus 4.7) that runs a task,
exposes a ``memory.log`` MCP tool, and streams every message to the event bus.

Auth is a config switch (subscription token vs API key). A ``dry_run`` path lets
us exercise the full DB → event-bus → SSE → dashboard skeleton without spending
quota or needing credentials; it is auto-selected when no credentials are set.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from aletheia.config import Settings, get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.events.normalizer import normalize_message
from aletheia.memory.service import log_note

SYSTEM_PROMPT = (
    "You are Aletheia, an autonomous AI scientist running in a lights-out lab. "
    "Work methodically and record consequential steps. Whenever you make a decision "
    "or reach a conclusion, call the `memory_log` tool to persist a concise note to "
    "the experiment work log (the single source of truth). Keep notes terse and factual."
)


def _configure_auth(settings: Settings) -> None:
    """Make the spawned Claude CLI use the chosen auth path.

    Note the SDK auth precedence puts ANTHROPIC_API_KEY ABOVE CLAUDE_CODE_OAUTH_TOKEN,
    so for subscription mode we must clear the API key from the environment.
    """
    if settings.claude_auth_mode == "subscription":
        if settings.claude_code_oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        if settings.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key


def machine_has_claude_login() -> bool:
    """True if this machine already has a logged-in Claude Code session.

    The spawned ``claude`` CLI reuses these credentials automatically, so in
    subscription mode we don't need an explicit token in `.env`. Checks the macOS
    Keychain, the Linux credentials file, and the `oauthAccount` marker.
    """
    try:
        if platform.system() == "Darwin":
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True,
                timeout=5,
            )
            if r.returncode == 0:
                return True
        if (Path.home() / ".claude" / ".credentials.json").exists():
            return True
        cfg = Path.home() / ".claude.json"
        if cfg.exists():
            data = json.loads(cfg.read_text() or "{}")
            if data.get("oauthAccount"):
                return True
    except Exception:
        return False
    return False


def has_credentials(settings: Settings) -> bool:
    if settings.claude_auth_mode == "subscription":
        # Explicit token OR an existing machine login both work.
        return bool(settings.claude_code_oauth_token) or machine_has_claude_login()
    return bool(settings.anthropic_api_key)


def _build_memory_tool(run_id: str):
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
        # Surface the write on the event bus too, so the dashboard sees it live.
        await get_bus().publish(
            make_event("memory_log", run_id=run_id, payload={"note": note, "decision_id": decision_id})
        )
        return {"content": [{"type": "text", "text": f"Logged work-log entry #{decision_id}."}]}

    return memory_log


async def run_real(run_id: str, prompt: str) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server

    settings = get_settings()
    _configure_auth(settings)
    bus = get_bus()

    server = create_sdk_mcp_server(
        name="aletheia", version="0.0.1", tools=[_build_memory_tool(run_id)]
    )
    options = ClaudeAgentOptions(
        model=settings.claude_model,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"aletheia": server},
        allowed_tools=["mcp__aletheia__memory_log"],
        permission_mode="bypassPermissions",  # lights-out
        setting_sources=[],  # do not load the host's CLAUDE.md / .claude config
        max_budget_usd=settings.budget_usd,  # hard per-run guardrail
    )

    await bus.publish(make_event("run_started", run_id=run_id, payload={"prompt": prompt, "mode": "real"}))
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                for evt in normalize_message(msg, run_id):
                    await bus.publish(evt)
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
