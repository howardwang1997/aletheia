"""Per-run budget guardrails. The driver charges an estimated cost before each
consequential step and checks caps; a breach pauses the run (status='paused') and
notifies — lights-out, but bounded. Resume re-launches the driver.
"""

from __future__ import annotations

import time

from aletheia.config import get_settings
from aletheia.memory.service import budget_spent, get_run, record_budget_event


class BudgetPaused(Exception):
    """Raised to unwind the driver loop cleanly when a cap is breached."""


class BudgetTracker:
    def __init__(self, run_id: str) -> None:
        settings = get_settings()
        run = get_run(run_id) or {}
        self.run_id = run_id
        self.cap_usd: float = run.get("budget_cap_usd") or settings.budget_usd
        self.wall_cap_h: float = settings.wall_clock_hours
        self._start = time.monotonic()
        self._cum_usd: float = budget_spent(run_id, "usd")

    def charge(self, kind: str, amount: float) -> float:
        """Record a charge (kind 'usd') and return the new cumulative spend."""
        cum = record_budget_event(self.run_id, kind, amount)
        if kind == "usd":
            self._cum_usd = cum
        return cum

    @property
    def spent_usd(self) -> float:
        return self._cum_usd

    def elapsed_hours(self) -> float:
        return (time.monotonic() - self._start) / 3600.0

    def breaches(self) -> list[dict]:
        out: list[dict] = []
        if self._cum_usd > self.cap_usd:
            out.append({"kind": "usd", "spent": self._cum_usd, "cap": self.cap_usd})
        eh = self.elapsed_hours()
        if eh > self.wall_cap_h:
            out.append({"kind": "wall_clock_hours", "spent": eh, "cap": self.wall_cap_h})
        return out
