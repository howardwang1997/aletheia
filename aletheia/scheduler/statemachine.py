"""The Phase-1 research lifecycle FSM.

Scoping already produced a human-confirmed plan (an Experiment at stage
``experiment_design``), so the launched loop starts at EXPERIMENT_DESIGN and walks
to ARCHIVE. Hard gates (critique_design, review_results) can send it back to design,
bounded by a per-stage loop guard so it can't oscillate forever.
"""

from __future__ import annotations

from aletheia.events.bus import get_bus, make_event
from aletheia.memory.service import log_note

# Ordered stages the driver walks (the two gates are folded into the driver logic).
PHASE1_STAGES = (
    "experiment_design",
    "execution",
    "analysis",
    "optimize",
    "write_up",
    "archive",
)


class LoopGuard:
    """Bounds re-entries into a stage (e.g. design revisions after a reject) so the
    autonomous loop escalates instead of oscillating forever."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._counts: dict[str, int] = {}

    def bump(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def exceeded(self, key: str) -> bool:
        return self._counts.get(key, 0) >= self.limit


async def record_transition(
    run_id: str,
    experiment_id: str | None,
    stage_from: str | None,
    stage_to: str,
    rationale: str,
    actor: str = "orchestrator",
    critique_panel_id: str | None = None,
) -> None:
    """Persist a Decision row + emit a ``stage`` event so the dashboard timeline
    advances. The Decision is the auditable 'why' for the transition."""
    import asyncio

    await asyncio.to_thread(
        log_note,
        run_id,
        rationale,
        actor,
        stage_from,
        stage_to,
        experiment_id,
    )
    await get_bus().publish(
        make_event(
            "stage",
            run_id=run_id,
            payload={"stage": stage_to, "from": stage_from, "rationale": rationale},
        )
    )
