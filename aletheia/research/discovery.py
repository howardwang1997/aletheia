"""Autonomous discovery loop — the system GENERATES bold candidates and SELF-SCREENS them through the
full rigor filter, surfacing only novel-AND-feasible-AND-grounded survivors. Promoted from
scripts/auto_discovery.py into a tested package module so the driver can run it as a STAGE.

The triage is the scarce filter ("creativity is cheap"). Each candidate carries runnable
`compute_demonstration` code; the harness screens it deterministically (prereg-valid -> code-gate ->
runnable/non-degenerate -> runs+holds on real data -> non-trivial magnitude), then applies the two
scarce filters (GROUNDABILITY + the cross-vendor NOVELTY gate, author excluded) only to the survivors
of the cheap pass. All dependencies (ideator, gateway, literature search) are injectable so the loop
is unit-testable offline with fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aletheia.coder.demonstration_runner import run_authored_demonstration
from aletheia.coder.sandbox import DEMO_REQUIRED_FUNCTION, check_code, smoke_test_demonstration

# ideate_fn(avoid_titles, lessons) -> list of candidate dicts {title, insight, claim, code, prereg}
IdeateFn = Callable[[list[str], list[str]], list[dict[str, Any]]]

IDEATE_SYSTEM = (
    "You are a brilliant, contrarian ML-for-science researcher who finds OBLIQUE, high-novelty angles "
    "AND writes the code to test them. You are ruthless about feasibility and magnitude: a beautiful "
    "idea that is untestable, mechanical, or tiny is worthless. Output STRICT JSON only."
)


def materials_ideate_context(n: int, target_desc: str = "band gap, eV") -> str:
    """The contract for a composition->property model: each candidate carries a runnable
    `compute_demonstration(X, y, groups, meta)` + a pre-registered decision rule."""
    return (
        f"Propose {n} EXTREMELY NOVEL, oblique research claims about a composition->property MODEL, EACH "
        f"with runnable test code. The harness hands your code X (Magpie composition features, ~132 "
        "dims, PERMUTATION-INVARIANT aggregates), y (" + target_desc + "), groups (chemical-system "
        "STRING per row, e.g. 'As-Ga' = the sorted element set), meta {random_state, preregistration}.\n\n"
        "Each candidate is JSON: {\"title\": str, \"insight\": str, \"claim\": str, \"code\": str, "
        "\"prereg\": {\"supported_if\": {\"op\": \">=\"|\">\"|\"<=\"|\"<\", \"threshold\": float}, "
        "\"control_silent_if\": {\"op\": \"<\"|\"<=\", \"threshold\": float}}}.\n"
        "`code` defines EXACTLY def compute_demonstration(X, y, groups, meta): ... returning {"
        "\"test_statistic\": float, \"control_statistic\": float, \"n_test\": int, \"n_control\": int, "
        "\"components\": dict, \"detail\": str}. The harness decides HOLDS = (supported_if on "
        "test_statistic) AND (control_silent_if on control_statistic) AND probes-clean; never return 'holds'.\n\n"
        "HARD RULES for `code` (statically checked): import ONLY from sklearn, numpy, scipy, pandas, "
        "math, statistics, collections, itertools, functools, random, warnings. No file/network/process, "
        "no eval/exec/open/__import__. Use meta['random_state'] for any split. The control must VANISH "
        "(~0) if the effect is null (a real permuted-label negative control). n_test, n_control >= 20.\n\n"
        "DO NOT propose (tried, dead): applicability-domain / distance-to-training; error-vs-rarity; "
        "cliffs; epistemic-vs-aleatoric / Bayes-floor; OR any premise depending on formula STRING ORDER "
        "(X is permutation-invariant -> NULL). Aim for a LARGE, surprising, real effect with a clean "
        "vanishing control."
    )


def make_vendor_ideator(model: str, base_url: str, key: str, *, system: str = IDEATE_SYSTEM,
                        context: str = "", timeout: float = 240.0, temperature: float = 0.95) -> IdeateFn:
    """Build an ideate_fn backed by an OpenAI-compatible vendor (e.g. grok). Claude-free. Each call
    folds the prior round's rejected titles + failure reasons into the prompt so the loop LEARNS."""
    import json as _json

    ctx = context or materials_ideate_context(6)

    def ideate(avoid_titles: list[str], lessons: list[str]) -> list[dict[str, Any]]:
        from openai import OpenAI
        extra = ""
        if avoid_titles:
            extra += "\n\nALREADY TRIED (propose DIFFERENT angles):\n- " + "\n- ".join(t[:80] for t in avoid_titles[-12:])
        if lessons:
            extra += ("\n\nWHY PRIOR CANDIDATES DIED (avoid these — most died because the test statistic "
                      "did NOT separate from its control): " + "; ".join(lessons[-6:]) + ". Make the test "
                      "statistic LARGE and clearly different from a permuted control, or do not propose it.")
        client = OpenAI(api_key=key, base_url=base_url, max_retries=1, timeout=timeout)
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": system},
                                   {"role": "user", "content": ctx + extra}],
            response_format={"type": "json_object"}, temperature=temperature)
        raw = (resp.choices[0].message.content or "{}").strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
        out = _json.loads(raw)
        if isinstance(out, dict):
            out = out.get("candidates") or out.get("items") or out.get("results") or []
        return out if isinstance(out, list) else []

    return ideate


@dataclass
class Screened:
    title: str
    stage: str                      # where it ended: prereg | code_gate | runnability | run_real | scored | novelty/grnd
    survives: bool
    why: str = ""
    test: float | None = None
    control: float | None = None
    n_test: int = 0
    n_control: int = 0
    holds: bool = False
    grounded: bool | None = None
    n_papers: int = 0
    novelty_consensus: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def screen_deterministic(cand: dict, plugin, X, y, groups) -> Screened:
    """Cheap, deterministic, Claude-free: prereg-valid -> code-gate -> runnable/non-degenerate ->
    runs+holds on REAL data -> non-trivial magnitude. Returns a Screened (survives = passed all)."""
    title = str(cand.get("title", "?"))
    code = cand.get("code") or ""
    prereg = cand.get("prereg") or {}
    if not plugin._valid_decision_rules(prereg):
        return Screened(title, "prereg", False, "malformed decision rules")
    ok, reasons = check_code(code, required_function=DEMO_REQUIRED_FUNCTION)
    if not ok:
        return Screened(title, "code_gate", False, str(reasons[:2]))
    smoke_ok, smoke_err = smoke_test_demonstration(code)
    if not smoke_ok:
        return Screened(title, "runnability", False, smoke_err[:120])
    res = run_authored_demonstration(code, X, y, groups, {"random_state": 0, "preregistration": prereg})
    if not isinstance(res, dict):
        return Screened(title, "run_real", False, "did not run on real data")
    probes = plugin._demonstration_probes(res)
    ts, cs = float(res.get("test_statistic")), float(res.get("control_statistic"))
    triggers = plugin._apply_rule(ts, prereg["supported_if"])
    silent = plugin._apply_rule(cs, prereg["control_silent_if"])
    holds = bool(triggers and silent and probes.get("clean"))
    nontrivial = abs(ts - cs) >= max(1e-9, 0.5 * abs(float(prereg["supported_if"]["threshold"]) or 1e-9))
    why = "" if (holds and nontrivial) else (
        "control fired" if not silent else "test did not trigger" if not triggers
        else "probe flagged" if not probes.get("clean") else "trivial magnitude")
    return Screened(title, "scored", holds and nontrivial, why, round(ts, 4), round(cs, 4),
                    int(res.get("n_test", 0)), int(res.get("n_control", 0)), holds)


def screen_novelty_grounding(cand: dict, gateway, run_id: str, *, search_fn, briefing_fn) -> dict:
    """The two scarce filters, Claude-free: GROUNDABILITY (citable prior work) + the cross-vendor
    NOVELTY gate (author 'anthropic' excluded). A network flake -> grounded=None (not a hard fail)."""
    title, claim, insight = str(cand.get("title", "")), str(cand.get("claim", "")), str(cand.get("insight", ""))
    try:
        papers = search_fn((title + " " + claim)[:160], 8)
    except Exception:  # noqa: BLE001
        papers = None
    grounded = None if papers is None else (len(papers) >= 3)
    hyp = {"statement": claim or title, "rationale": insight, "novelty_note": insight,
           "contribution_type": "paradigm",
           "demonstration": {"form": "discriminating_instance", "claim": claim or title, "capability": "ai_authored"}}
    content = {"hypothesis": hyp, "gaps": [], "literature": briefing_fn(papers) if papers else ""}
    try:
        panel = asyncio.run(gateway.review("direction", content, target_ref="auto-discovery",
                                           run_id=run_id, dry_run=False, exclude_vendors={"anthropic"}))
    except Exception as exc:  # noqa: BLE001
        return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
                "gate_passed": None, "consensus": "error", "novelty_objection": str(exc)[:60], "novelty_pass": False}
    nov_obj = [f"{f.severity}:{f.category}" for c in (panel.critiques or []) for f in (c.findings or [])
               if f.category == "novelty" and f.severity in ("blocker", "major")]
    return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
            "gate_passed": bool(panel.gate_passed), "consensus": panel.consensus_verdict,
            "novelty_objection": (nov_obj[0] if nov_obj else ""),
            "novelty_pass": bool(panel.gate_passed) and not nov_obj}


def discover(*, ideate_fn: IdeateFn, plugin, X, y, groups, gateway, run_id: str,
             k_survivors: int = 2, max_rounds: int = 3,
             search_fn=None, briefing_fn=None, log=print) -> tuple[list[dict], list[dict]]:
    """Run the discovery loop: up to ``max_rounds`` ideation rounds (each LEARNS from prior
    rejections) or until ``k_survivors`` banked. Returns (survivors, all_screened) as dicts.
    A survivor RAN-CLEAN + HELD + NON-TRIVIAL + GROUNDED + NOVEL. ``cand`` dicts of survivors carry
    their `code`/`prereg`, so the driver can hand the demonstration straight to the campaign."""
    if search_fn is None or briefing_fn is None:
        from aletheia.research.literature import briefing as _b, search as _s
        search_fn = search_fn or (lambda q, k: _s(q, k))
        briefing_fn = briefing_fn or _b
    survivors: list[dict] = []
    all_rows: list[dict] = []
    tried: list[str] = []
    lessons: list[str] = []
    for rnd in range(1, max_rounds + 1):
        if len(survivors) >= k_survivors:
            break
        try:
            cands = ideate_fn(tried, lessons)
        except Exception as exc:  # noqa: BLE001
            log(f"[discover] round {rnd} ideation failed: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        for cand in cands:
            title = str(cand.get("title", "?"))
            if title.lower()[:50] in {t.lower()[:50] for t in tried}:
                continue
            tried.append(title)
            sc = screen_deterministic(cand, plugin, X, y, groups)
            row = {"title": sc.title, "stage": sc.stage, "survives": sc.survives, "why": sc.why,
                   "test": sc.test, "control": sc.control, "n_test": sc.n_test, "n_control": sc.n_control,
                   "holds": sc.holds}
            if sc.survives:  # passed the cheap pass -> apply the scarce filters
                vet = screen_novelty_grounding(cand, gateway, run_id, search_fn=search_fn, briefing_fn=briefing_fn)
                row.update(vet)
                row["survives"] = bool(vet["novelty_pass"] and vet.get("grounded") is not False)
                if not row["survives"]:
                    row["stage"] = "novelty/grnd"
                    row["why"] = vet.get("novelty_objection") or (
                        f"ungrounded(n={vet.get('n_papers', 0)})" if vet.get("grounded") is False else "novelty gate")
                else:
                    row["candidate"] = cand  # carry code/prereg for the campaign
            all_rows.append(row)
            if row["survives"]:
                survivors.append(row)
            elif row.get("why"):
                lessons.append(row["why"])
            log(f"[discover] r{rnd} {'SURVIVE' if row['survives'] else 'killed':<8} {sc.stage:<12} "
                f"{title[:46]}  {('' if row['survives'] else row.get('why', ''))[:30]}")
        log(f"[discover] round {rnd}: {len(survivors)} survivor(s) / {len(all_rows)} screened")
    return survivors, all_rows
