"""Faithful, durable conversation records for a run, reconstructed from the event ledger.

The bus persists EVERY canonical event — ``assistant_text``, ``thinking``, ``tool_use``,
``tool_result``, ``result`` (with the SDK's token usage), ``system``, plus the domain /
bookkeeping events — to the ``events`` table. So the complete record of what every model said
and did during a run is already there; this module just exports it to durable files so the
precious data survives a DB reset and is readable offline.

Two artifacts per run, under ``artifacts/``:
  - ``transcript_<stem>.jsonl`` — LOSSLESS: every event, in chronological order, full payload.
    The archive (machine-readable, re-ingestible).
  - ``transcript_<stem>.md`` — READABLE: a chronological transcript tagged by lane (each isolated
    worker is its own Claude session), with a token/cost header. Conversational events render in
    full; other events collapse to compact one-liners so nothing is hidden but the narrative reads.

A run's conversation is NOT one linear thread: the orchestrator plus many isolated workers
(``coder``, ``critic``, ``survey:*``, ``analysis:*`` …) each run a fresh Claude session, tagged by
the event ``agent`` (lane). The transcript preserves the true chronological interleaving and tags
every turn with its lane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aletheia.events.store import list_run_events
from aletheia.memory.service import get_run
from aletheia.memory.usage import aggregate_run_usage, format_rate_limit, format_usage, run_rate_limit
from aletheia.paths import artifacts_dir

# rendered as full blocks (the actual dialogue); everything else collapses to a compact line.
_CONVERSATIONAL = {"assistant_text", "thinking", "tool_use", "tool_result", "worker_degraded"}
# md-only readability caps — the .jsonl is always lossless, so the .md may truncate huge tool blobs.
_MAX_TOOL_INPUT = 4000
_MAX_TOOL_RESULT = 8000


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [truncated {len(s) - n} chars — see the .jsonl]"


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, indent=2, ensure_ascii=False, default=str)


def _compact(payload: dict, n: int = 300) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (payload or {}).items())
    return s if len(s) <= n else s[:n] + "…"


def _render_event(e: dict) -> str:
    etype = e.get("type", "")
    agent = e.get("agent") or "?"
    p = e.get("payload") or {}

    if etype == "thinking":
        return f"#### 🧠 [{agent}] thinking\n\n{(p.get('text') or '').strip()}\n"
    if etype == "assistant_text":
        return f"#### 💬 [{agent}] assistant\n\n{(p.get('text') or '').strip()}\n"
    if etype == "tool_use":
        body = _trunc(_as_text(p.get("input")), _MAX_TOOL_INPUT)
        return f"#### 🔧 [{agent}] tool_use → `{p.get('tool')}`\n\n```json\n{body}\n```\n"
    if etype == "tool_result":
        err = " ⚠️ error" if p.get("is_error") else ""
        body = _trunc(_as_text(p.get("content")), _MAX_TOOL_RESULT)
        return f"#### 📥 [{agent}] tool_result{err}\n\n```\n{body}\n```\n"
    if etype == "worker_degraded":
        return f"#### ⛔ [{agent}] worker degraded — {p.get('reason')}\n"
    if etype == "result":
        u = p.get("usage") if isinstance(p.get("usage"), dict) else {}
        cost = p.get("cost_usd")
        cost_s = f"${cost:.4f}" if isinstance(cost, (int, float)) else str(cost)
        toks = (f"in {u.get('input_tokens', 0)} / out {u.get('output_tokens', 0)} / "
                f"cache_read {u.get('cache_read_input_tokens', 0)}") if u else "no usage"
        return f"— *[{agent}] result · {cost_s} · {toks} · {p.get('num_turns')} turns*\n"
    # everything else (system, status, belief_*, campaign_*, claims, demonstration, …): one line
    return f"- `[{agent}] {etype}` — {_compact(p)}\n"


def render_markdown(run_id: str, events: list[dict]) -> str:
    run = get_run(run_id) or {}
    usage = aggregate_run_usage(run_id)
    goal = (run.get("goal") or "").strip().replace("\n", " ")
    lanes: dict[str, int] = {}
    for e in events:
        lanes[e.get("agent") or "?"] = lanes.get(e.get("agent") or "?", 0) + 1

    lines = [
        f"# Conversation transcript — run `{run_id}`",
        "",
        f"- domain: **{run.get('domain')}**  ·  status: **{run.get('status')}**  ·  created: {run.get('created_at')}",
        f"- goal: {goal[:400]}" if goal else "",
        f"- events: **{len(events)}**  ·  lanes: " + ", ".join(
            f"`{a}`×{n}" for a, n in sorted(lanes.items(), key=lambda kv: -kv[1])[:12]),
        f"- token/cost: {format_usage(usage)}",
        f"- {format_rate_limit(run_rate_limit(run_id))}",
        "",
        "> A run is many isolated Claude sessions (one per lane) interleaved in time. Each turn below",
        "> is tagged with its lane. The lossless event dump is the sibling `.jsonl`.",
        "",
        "---",
        "",
    ]
    last_lane: str | None = None
    for e in events:
        etype = e.get("type", "")
        agent = e.get("agent") or "?"
        if etype in _CONVERSATIONAL and agent != last_lane:
            lines.append(f"\n## ▷ lane: `{agent}`\n")
            last_lane = agent
        lines.append(_render_event(e))
    return "\n".join(x for x in lines if x is not None)


def export_transcript(run_id: str, *, stem: str | None = None, out_dir: Path | None = None) -> dict[str, Path]:
    """Write the lossless ``.jsonl`` + readable ``.md`` conversation record for a run.

    Returns ``{"jsonl": Path, "md": Path, "events": <count>}``. Raises only on a genuine IO/DB
    failure — callers that must not break a run (the e2e harness) should wrap in try/except.
    """
    events = list_run_events(run_id)
    out = out_dir or artifacts_dir()
    stem = stem or run_id
    jsonl_path = out / f"transcript_{stem}.jsonl"
    md_path = out / f"transcript_{stem}.md"

    with open(jsonl_path, "w") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    md_path.write_text(render_markdown(run_id, events))
    return {"jsonl": jsonl_path, "md": md_path, "events": len(events)}  # type: ignore[dict-item]
