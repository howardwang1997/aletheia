"""Focused, single-turn Claude (Opus) reasoning for one lifecycle stage.

Thin shim over :func:`aletheia.orchestrator.worker.run_worker` — kept for the
driver's existing call sites and tests. A reasoning stage is just an isolated
single-turn worker; see ``worker.py`` for the general (parallel, tool-capable,
multi-turn) form.
"""

from __future__ import annotations

from aletheia.orchestrator.worker import STAGE_SYSTEM, run_worker

__all__ = ["reason_stage", "STAGE_SYSTEM"]


async def reason_stage(
    run_id: str,
    stage: str,
    prompt: str,
    *,
    dry_run: bool,
    dry_text: str | None = None,
    agent: str = "orchestrator",
) -> str:
    """Run one isolated Opus turn for ``stage`` and return its text."""
    return await run_worker(
        run_id, agent, prompt, system=STAGE_SYSTEM, dry_run=dry_run, dry_value=dry_text
    )
