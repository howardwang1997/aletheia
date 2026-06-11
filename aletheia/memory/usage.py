"""Real token + cost accounting for a run, aggregated from the persisted event ledger.

Every Claude Agent SDK call — the orchestrator AND every isolated worker
(:func:`aletheia.orchestrator.worker.run_worker`) — streams a ``ResultMessage`` that
:func:`aletheia.events.normalizer.normalize_message` turns into a ``result`` event
carrying the SDK's own ``total_cost_usd`` and ``usage`` token counts, which the event
bus persists to the ``events`` table. So the ground truth already lives in the ledger;
this module just SUMS it per run. No hot-path instrumentation and no estimate — the
numbers are exactly what the SDK reported.

Note on subscription auth: when the box is logged into a Claude subscription (rather
than a metered API key) the SDK often reports ``total_cost_usd == 0`` even though real
tokens were spent, so ``cost_usd`` can read low while the token counts are the honest
signal. Tokens — especially ``cache_read_input_tokens`` — are what the rolling usage
window actually meters, so they are the number to watch when a run eats the limit.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from aletheia.db import session_scope
from aletheia.memory.ledger import Event


@dataclass
class RunUsage:
    """SDK-reported cost + token totals for one run, summed across its ``result`` events."""

    run_id: str
    n_calls: int = 0  # result events carrying a usage payload (~= SDK queries)
    num_turns: int = 0  # SDK-reported assistant turns summed across calls
    cost_usd: float = 0.0  # sum of SDK total_cost_usd (often ~0 under subscription auth)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    @property
    def total_tokens(self) -> int:
        """All billable token classes summed. ``cache_creation`` (the ephemeral_* nested
        breakdown) is NOT added here — it is a decomposition of ``cache_creation_input_tokens``
        and would double-count."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d


def _as_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def aggregate_run_usage(run_id: str) -> RunUsage:
    """Sum the SDK-reported cost + token usage across every ``result`` event for ``run_id``.

    Tolerant by construction: a dry-run ``result`` event carries no ``usage`` (its cost is
    0.0, counted but contributing nothing); a stringified ``usage`` (SDK shape drift) is
    skipped for tokens but its cost still counts.
    """
    u = RunUsage(run_id=run_id)
    with session_scope() as s:
        rows = (
            s.query(Event.payload)
            .filter(Event.run_id == run_id, Event.type == "result")
            .all()
        )
    for (payload,) in rows:
        if not isinstance(payload, dict):
            continue
        u.cost_usd += _as_float(payload.get("cost_usd"))
        u.num_turns += _as_int(payload.get("num_turns"))
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue  # dry-run / stringified usage: cost already counted, no tokens to add
        u.n_calls += 1
        u.input_tokens += _as_int(usage.get("input_tokens"))
        u.output_tokens += _as_int(usage.get("output_tokens"))
        u.cache_read_input_tokens += _as_int(usage.get("cache_read_input_tokens"))
        u.cache_creation_input_tokens += _as_int(usage.get("cache_creation_input_tokens"))
        stu = usage.get("server_tool_use")
        if isinstance(stu, dict):
            u.web_search_requests += _as_int(stu.get("web_search_requests"))
            u.web_fetch_requests += _as_int(stu.get("web_fetch_requests"))
    return u


@dataclass
class RateLimitStatus:
    """The Claude **5-hour rolling window** status the SDK reports in ``system`` events during a run.

    This is the window the project actually competes for (E2E and the interactive session share the
    same machine subscription login). ``peak_utilization`` is the highest fraction of the window seen
    while the run was executing; ``rejections`` counts ``five_hour`` ``rejected`` events — the run
    literally hit the wall and was throttled."""

    peak_utilization: float | None = None  # 0..1, max five_hour window fill seen during the run
    worst_status: str | None = None  # allowed | allowed_warning | rejected
    rejections: int = 0  # number of five_hour 'rejected' events (the run was throttled)
    samples: int = 0  # how many five_hour status reports were seen

    def to_dict(self) -> dict:
        return asdict(self)


# the SDK stringifies a RateLimitEvent into the ``system`` event's ``repr``; pull the five_hour
# RateLimitInfo's status + utilization (adjacent fields in a stable order). Other windows
# (seven_day, …) and their own utilization values are intentionally ignored.
_FIVE_HOUR_RE = re.compile(
    r"status='(?P<status>\w+)', resets_at=\d+, rate_limit_type='five_hour', "
    r"utilization=(?P<util>[\d.]+|None)"
)
_STATUS_RANK = {"allowed": 0, "allowed_warning": 1, "rejected": 2}


def run_rate_limit(run_id: str) -> RateLimitStatus:
    """Parse the 5-hour-window status the SDK reported across this run's ``system`` events."""
    rl = RateLimitStatus()
    worst_rank = -1
    with session_scope() as s:
        rows = (
            s.query(Event.payload)
            .filter(Event.run_id == run_id, Event.type == "system")
            .all()
        )
    for (payload,) in rows:
        rep = payload.get("repr") if isinstance(payload, dict) else None
        if not rep or "five_hour" not in rep:
            continue
        for m in _FIVE_HOUR_RE.finditer(rep):
            rl.samples += 1
            st = m.group("status")
            rank = _STATUS_RANK.get(st, 0)
            if rank > worst_rank:
                worst_rank, rl.worst_status = rank, st
            if st == "rejected":
                rl.rejections += 1
            util = m.group("util")
            if util != "None":
                val = float(util)
                if rl.peak_utilization is None or val > rl.peak_utilization:
                    rl.peak_utilization = val
    return rl


def recent_window_status(
    run_id: str, within_minutes: float = 30.0
) -> tuple[str | None, float | None]:
    """The MOST RECENT five_hour (status, utilization) the SDK reported in the last
    ``within_minutes`` — i.e. a LIVE reading from this actively-running session.

    The recency filter is the resume-safety hinge. An actively-executing run emits a RateLimitEvent
    on essentially every call (minutes apart), so 30 min always captures the current state. But a
    'rejected'/high reading from BEFORE a pause is hours old by the time the run resumes — it must
    NOT trip the resumed run, which may have re-entered a freshly-reset window. So we look only at
    readings recent enough to belong to live execution; the first live call after resume produces a
    fresh reading the stop then acts on. Ordered by id so the last match is the live reading;
    ``status`` is that latest reading and ``util`` the latest NUMERIC utilization (a 'rejected'
    reading often carries utilization=None, so the last known number is kept). (None, None) if no
    recent five_hour sample exists."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    with session_scope() as s:
        rows = (
            s.query(Event.payload)
            .filter(Event.run_id == run_id, Event.type == "system", Event.ts >= cutoff)
            .order_by(Event.id.asc())
            .all()
        )
    status: str | None = None
    util: float | None = None
    for (payload,) in rows:
        rep = payload.get("repr") if isinstance(payload, dict) else None
        if not rep or "five_hour" not in rep:
            continue
        for m in _FIVE_HOUR_RE.finditer(rep):
            status = m.group("status")
            u = m.group("util")
            if u != "None":
                util = float(u)
    return status, util


def list_run_ids_with_usage() -> list[str]:
    """Run ids that emitted at least one ``result`` event, newest first (by latest event id)."""
    with session_scope() as s:
        rows = (
            s.query(Event.run_id)
            .filter(Event.type == "result", Event.run_id.isnot(None))
            .group_by(Event.run_id)
            .order_by(func.max(Event.id).desc())
            .all()
        )
    return [r[0] for r in rows]


def format_usage(u: RunUsage) -> str:
    """A one-glance human summary line (used by the e2e harness + the CLI report)."""
    return (
        f"{u.n_calls} SDK calls / {u.num_turns} turns | "
        f"${u.cost_usd:.4f} | {u.total_tokens:,} tok "
        f"(in {u.input_tokens:,} · out {u.output_tokens:,} · "
        f"cache_read {u.cache_read_input_tokens:,} · cache_create {u.cache_creation_input_tokens:,})"
    )


def format_rate_limit(rl: RateLimitStatus) -> str:
    """A one-glance summary of the 5-hour-window pressure this run was under."""
    if not rl.samples:
        return "5h-window: no status reported"
    peak = f"{rl.peak_utilization * 100:.0f}%" if rl.peak_utilization is not None else "n/a"
    rej = f", {rl.rejections} REJECTED (throttled)" if rl.rejections else ""
    return f"5h-window: peak {peak} · worst '{rl.worst_status}'{rej} ({rl.samples} samples)"
