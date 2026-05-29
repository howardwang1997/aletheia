"""IAM policy — the irreversible-op guardrails around GitHub actions.

Lights-out, but never reckless: repo creation is rate-capped per day, the agent
may only touch repos under the configured prefix (``aletheia-*``), and deletion is
structurally impossible (no backend method) — this module makes that explicit and
gives the driver an allow/deny decision with a reason to log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from aletheia.config import get_settings

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Decision:
    allow: bool
    reason: str


def slugify(text: str, *, maxlen: int = 40) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s[:maxlen].strip("-")) or "exp"


def repo_name(domain: str | None, slug: str) -> str:
    """`aletheia-<domain>-<slug>` (master-plan naming)."""
    prefix = get_settings().iam_repo_prefix
    parts = [prefix, slugify(domain or "lab", maxlen=20), slugify(slug, maxlen=30)]
    return "-".join(p for p in parts if p)


def branch_name(exp_id: str, slug: str) -> str:
    """`exp/<exp_id>-<slug>` (branch-per-experiment)."""
    return f"exp/{exp_id[:12]}-{slugify(slug, maxlen=24)}"


def _has_prefix(name: str) -> bool:
    prefix = get_settings().iam_repo_prefix
    # accept "<prefix>" or "<prefix>-..." but not "<prefix>foo"
    return name == prefix or name.startswith(prefix + "-")


def check_repo_create(name: str, created_today: int) -> Decision:
    """Gate repo creation: name must be under the prefix, and the daily cap must
    not be exceeded."""
    if not _has_prefix(name):
        return Decision(False, f"repo '{name}' is outside the '{get_settings().iam_repo_prefix}-' namespace")
    cap = get_settings().iam_create_repo_daily_cap
    if created_today >= cap:
        return Decision(False, f"daily repo-creation cap reached ({created_today}/{cap})")
    return Decision(True, "ok")


def check_push(name: str) -> Decision:
    """The agent may only push to repos it owns under the prefix."""
    if not _has_prefix(name):
        return Decision(False, f"refusing to push outside the '{get_settings().iam_repo_prefix}-' namespace: {name}")
    return Decision(True, "ok")


def check_delete(name: str) -> Decision:
    """Deletion is never autonomous."""
    return Decision(False, "repo deletion is never performed autonomously")


def created_repos_last_24h() -> int:
    """Count repo-creation events in the last 24h (the daily-cap denominator)."""
    from sqlalchemy import func

    from aletheia.db import session_scope
    from aletheia.memory.ledger import Event

    with session_scope() as s:
        cutoff = func.now() - timedelta(hours=24)
        return (
            s.query(func.count(Event.id))
            .filter(Event.type == "iam_repo_created", Event.ts >= cutoff)
            .scalar()
            or 0
        )
