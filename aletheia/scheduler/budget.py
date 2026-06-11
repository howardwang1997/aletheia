"""Per-run budget guardrails. The driver charges an estimated cost before each
consequential step and checks caps; a breach pauses the run (status='paused') and
notifies — lights-out, but bounded. Resume re-launches the driver.

The USD cap binds on the GREATER of (a) the conservative forward estimate charged
before each stage and (b) the SDK's *real* reported cost, aggregated from the
persisted ``result`` events (:mod:`aletheia.memory.usage`). The estimate pre-pauses
before a stage that would cross; the real cost is the truth once it has. An optional
``token_cap_per_run`` bounds total tokens — the signal that actually meters the
rolling subscription window (cost reads ~0 under subscription auth, tokens do not).
"""

from __future__ import annotations

import time

from aletheia.config import get_settings
from aletheia.memory.service import budget_spent, get_run, record_budget_event
from aletheia.memory.usage import aggregate_run_usage, recent_window_status


class BudgetPaused(Exception):
    """Raised to unwind the driver loop cleanly when a cap is breached."""


class BudgetTracker:
    def __init__(self, run_id: str) -> None:
        settings = get_settings()
        run = get_run(run_id) or {}
        self.run_id = run_id
        self.cap_usd: float = run.get("budget_cap_usd") or settings.budget_usd
        self.wall_cap_h: float = settings.wall_clock_hours
        self.token_cap: int | None = settings.token_cap_per_run
        self.window_stop_util: float | None = settings.window_stop_utilization
        self._start = time.monotonic()
        self._cum_usd: float = budget_spent(run_id, "usd")
        # The token cap bounds THIS session's NEW tokens, not lifetime. A resumed run reuses the
        # run_id, so the lifetime sum already carries every prior window's tokens — a lifetime cap
        # would trip the instant the cached prefix replays, before any new work. Baseline the
        # accumulated total at construction (0 for a fresh run, which builds its tracker first).
        self._token_baseline: int = (
            aggregate_run_usage(run_id).total_tokens if self.token_cap else 0
        )

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
        usage = aggregate_run_usage(self.run_id)  # one indexed query; reused for both caps
        real_usd = usage.cost_usd
        effective_usd = max(self._cum_usd, real_usd)
        if effective_usd > self.cap_usd:
            out.append({
                "kind": "usd", "spent": effective_usd, "cap": self.cap_usd,
                "real_usd": real_usd, "estimate_usd": self._cum_usd,
            })
        if self.token_cap:
            session_tokens = usage.total_tokens - self._token_baseline
            if session_tokens > self.token_cap:
                out.append({"kind": "tokens", "spent": session_tokens, "cap": self.token_cap,
                            "lifetime": usage.total_tokens})
        if self.window_stop_util is not None:
            # the SDK's LIVE 5h-window reading (time-filtered, resume-safe). Stop BEFORE the wall so
            # the run pauses+checkpoints and a fresh-window resume replays the prefix for 0 tokens,
            # rather than dying mid-stream into 'rejected' and burning the window on retry storms.
            status, util = recent_window_status(self.run_id)
            if status == "rejected" or (util is not None and util >= self.window_stop_util):
                out.append({
                    "kind": "five_hour_window", "status": status,
                    "utilization": util, "threshold": self.window_stop_util,
                })
        eh = self.elapsed_hours()
        if eh > self.wall_cap_h:
            out.append({"kind": "wall_clock_hours", "spent": eh, "cap": self.wall_cap_h})
        return out
