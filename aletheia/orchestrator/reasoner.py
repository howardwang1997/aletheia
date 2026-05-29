"""Focused, single-turn Claude (Opus 4.7) reasoning for one lifecycle stage.

The driver owns the loop; at each *reasoning* stage it calls ``reason_stage`` for a
short, contextful Opus turn (hypothesis, design rationale, analysis, write-up). The
turn streams to the event bus like any other agent activity. A dry-run path returns
scripted text so the whole loop runs with zero spend.
"""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.events.normalizer import normalize_message
from aletheia.orchestrator.auth import configure_auth, has_credentials

STAGE_SYSTEM = (
    "You are Aletheia, an autonomous AI scientist executing one stage of a research "
    "loop in a lights-out lab. Be rigorous, concrete, and concise. When asked for "
    "JSON, return ONLY a single JSON object."
)


async def reason_stage(
    run_id: str,
    stage: str,
    prompt: str,
    *,
    dry_run: bool,
    dry_text: str | None = None,
    agent: str = "orchestrator",
) -> str:
    """Run one Opus turn for ``stage`` and return its text. Streams events."""
    settings = get_settings()
    if dry_run or not has_credentials(settings):
        text = dry_text or f"[dry-run] {stage} reasoning complete."
        await get_bus().publish(
            make_event("assistant_text", run_id=run_id, agent=agent, payload={"text": text})
        )
        return text

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    configure_auth(settings)
    options = ClaudeAgentOptions(
        model=settings.claude_model,
        system_prompt=STAGE_SYSTEM,
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_budget_usd=settings.budget_usd,
    )
    chunks: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            for evt in normalize_message(msg, run_id, agent=agent):
                await get_bus().publish(evt)
                if evt["type"] == "assistant_text":
                    chunks.append((evt.get("payload") or {}).get("text", ""))
    return "\n".join(chunks).strip()
