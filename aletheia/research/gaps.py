"""Conversational pre-research: discover GENUINE open gaps in a research direction.

Surveys the literature, then synthesizes RANKED gaps — each with WHY it is genuinely
open (a question the field has not answered or not even asked, NOT merely an
under-reported combination of known methods), supporting citations, and a candidate
framing (performance vs paradigm + a discriminating demonstration). Intended for a human
to review + pick one BEFORE an autonomous run is launched from it (so ideation starts
from a genuinely open problem the human steered toward, not a seeded/known direction)."""

from __future__ import annotations

import asyncio
import json

from aletheia.orchestrator.worker import is_degraded, run_worker
from aletheia.research import literature

_GAP_SYSTEM = (
    "You are a rigorous research strategist who finds GENUINE open problems — questions "
    "the field has NOT answered or has not even asked — not merely under-reported "
    "combinations of known methods. You are skeptical: if the literature already addresses "
    "a candidate gap, you discard it. You never inflate a commonplace into a 'gap'."
)


def _parse_list(text: str | None) -> list[dict]:
    """Extract the outermost JSON array from a model reply (tolerates ``` fences /
    surrounding prose). Returns the list of dict gaps, or [] if unparseable."""
    if not text:
        return []
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b == -1 or b < a:
        return []
    try:
        return [g for g in json.loads(text[a:b + 1]) if isinstance(g, dict)]
    except Exception:
        return []


async def discover_gaps(
    run_id: str, domain: str, direction: str, *, k_gaps: int = 5, dry_run: bool = False
) -> list[dict]:
    """Survey ``direction`` in ``domain`` and synthesize the ``k_gaps`` most GENUINELY
    open gaps, ranked. Each gap: ``{rank, statement, why_open, evidence, candidate_framing
    {contribution_type, demonstration}, openness, feasibility}``. Best-effort — a degraded
    LLM call or empty survey yields []."""
    papers = [] if dry_run else await asyncio.to_thread(literature.search, direction, 10)
    brief = literature.briefing(papers, limit=8)
    prompt = (
        f"DOMAIN: {domain}\nDIRECTION: {direction}\n\n"
        + (brief + "\n\n" if brief else "")
        + f"Identify the {k_gaps} most GENUINELY OPEN gaps in this direction — ranked, "
        "most-open first. A genuine gap is a question the field has NOT answered (or not "
        "asked), NOT just an under-reported combination of known methods. Be skeptical: if "
        "the literature above already addresses a candidate, DROP it; do not inflate a "
        "commonplace.\n"
        "For each gap give: rank (1=most open), statement, why_open (why it is genuinely "
        "unsolved/unasked — name the closest prior work it is open RELATIVE TO), evidence "
        "(short citations from the literature), candidate_framing {contribution_type: "
        "'performance' | 'paradigm', demonstration: a concrete, checkable discriminating "
        "test the new frame would make}, openness (0..1 confidence it is truly open), "
        "feasibility (0..1, CPU/minutes).\n"
        'Return ONLY a JSON array: [{"rank":1,"statement":"...","why_open":"...",'
        '"evidence":["..."],"candidate_framing":{"contribution_type":"...","demonstration":'
        '"..."},"openness":0.0,"feasibility":0.0}].'
    )
    text = await run_worker(
        run_id, "gap_discovery", prompt, system=_GAP_SYSTEM, dry_run=dry_run,
        dry_value=(
            '[{"rank":1,"statement":"(dry-run) example genuinely-open gap",'
            '"why_open":"no prior work addresses X under condition Y","evidence":[],'
            '"candidate_framing":{"contribution_type":"paradigm","demonstration":'
            '"a case the incumbent frame provably cannot handle"},"openness":0.6,'
            '"feasibility":0.9}]'
        ),
    )
    if is_degraded(text):
        return []
    gaps = _parse_list(text)
    gaps.sort(key=lambda g: g.get("rank") if isinstance(g.get("rank"), (int, float)) else 99)
    return gaps[:k_gaps]
