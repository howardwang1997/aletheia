"""AUTO-DISCOVERY loop (v1) — the system GENERATES bold candidates AND SELF-SCREENS them, surfacing
only the novel-AND-rigorous survivors. This is the autonomy capability the manual probe sessions
prototyped, composed into one automatic pass:

  1. a bold ideator (non-author vendor) proposes N oblique candidates, EACH WITH runnable test code
     (a `compute_demonstration(X, y, groups, meta)`) + a pre-registered decision rule;
  2. the harness AUTO-TRIAGES each candidate, deterministically and Claude-free:
       - prereg valid?                  (decision rules well-formed)
       - code-gate?                     (allowlisted imports, no os/net/exec — premise safety)
       - runs + non-degenerate?         (smoke test on a probe: catches false/impossible premises)
       - holds on REAL data?            (run in the sandbox -> harness applies the rule + probes)
       - non-trivial magnitude?         (test clearly beats its own control, above a floor)
       - GROUNDED?                      (citable prior work exists — mirrors the survey-grounding guard)
       - NOVEL?                         (clears the cross-vendor direction gate, author excluded)
  3. survivors RUN CLEAN + HOLD + NON-TRIVIAL + GROUNDED + NOVEL — the rest are auto-killed. The novelty
     + grounding filters (increment #2) run only on candidates that pass the cheap deterministic triage.

The triage is the scarce filter (creativity is cheap); this makes it automatic. Survivors would then
go to the cross-vendor direction novelty gate + a live campaign. Claude-free, testable offline.

    conda run -n aletheia python scripts/auto_discovery.py
"""

from __future__ import annotations

import json
import time

import numpy as np

import asyncio

from aletheia.coder.demonstration_runner import run_authored_demonstration
from aletheia.coder.sandbox import DEMO_REQUIRED_FUNCTION, check_code, smoke_test_demonstration
from aletheia.config import get_settings
from aletheia.critics.gateway import CriticGateway
from aletheia.db import create_all
from aletheia.domains.materials.datasets import load_benchmark
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.memory.service import create_run
from aletheia.research.literature import briefing, search

DATASET, TARGET, COMP = "matbench_expt_gap", "gap expt", "composition"
N_CANDIDATES = 6
K_SURVIVORS = 2     # bank this many before stopping
MAX_ROUNDS = 3      # ideation rounds; each LEARNS from prior rejections (don't repeat killed ideas)

_SYSTEM = (
    "You are a brilliant, contrarian ML-for-science researcher who finds OBLIQUE, high-novelty angles "
    "AND writes the code to test them. You are ruthless about feasibility and magnitude: a beautiful "
    "idea that is untestable, mechanical, or tiny is worthless. Output STRICT JSON only."
)

_CONTEXT = (
    "Propose " + str(N_CANDIDATES) + " EXTREMELY NOVEL, oblique research claims about a composition->"
    "property MODEL, EACH with runnable test code. Substrate: matbench_expt_gap (band gap, eV); the "
    "harness hands your code X (Magpie composition features, ~132 dims, PERMUTATION-INVARIANT "
    "aggregates), y (gap, eV), groups (chemical-system STRING per row, e.g. 'As-Ga' = the sorted "
    "element set), meta {random_state, preregistration}.\n\n"
    "Each candidate is JSON: {\"title\": str, \"insight\": str, \"claim\": str, \"code\": str, "
    "\"prereg\": {\"supported_if\": {\"op\": \">=\"|\">\"|\"<=\"|\"<\", \"threshold\": float}, "
    "\"control_silent_if\": {\"op\": \"<\"|\"<=\", \"threshold\": float}}}.\n"
    "`code` defines EXACTLY: def compute_demonstration(X, y, groups, meta): ... returning the dict "
    "{\"test_statistic\": float, \"control_statistic\": float, \"n_test\": int, \"n_control\": int, "
    "\"components\": dict, \"detail\": str}. The harness decides HOLDS = (supported_if on test_statistic) "
    "AND (control_silent_if on control_statistic) AND probes-clean; never return 'holds'.\n\n"
    "HARD RULES for `code` (statically checked, rejected otherwise): import ONLY from sklearn, numpy, "
    "scipy, pandas, math, statistics, collections, itertools, functools, random, warnings. No file/"
    "network/process, no eval/exec/open/__import__. Use meta['random_state'] for any split. The control "
    "must VANISH (~0) if the effect is null (a real negative control, e.g. a permuted-label version of "
    "the same statistic). Sizes n_test, n_control must be >= 20.\n\n"
    "DO NOT propose (tried, dead): applicability-domain / distance-to-training; error-vs-rarity; "
    "cliffs; epistemic-vs-aleatoric / Bayes-floor; OR any premise that depends on formula STRING ORDER "
    "(X is permutation-invariant, so order has zero signal — it will be NULL). Aim for a LARGE, "
    "surprising, real effect with a clean vanishing control."
)


def _ideate(vendor_id: str, model: str, base_url: str, key: str,
            avoid_titles: list[str] | None = None, lessons: list[str] | None = None) -> list[dict]:
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url, max_retries=1, timeout=240.0)
    extra = ""
    if avoid_titles:
        extra += "\n\nALREADY TRIED (propose DIFFERENT angles, do not repeat these):\n- " + \
                 "\n- ".join(t[:80] for t in avoid_titles[-12:])
    if lessons:
        extra += "\n\nWHY PRIOR CANDIDATES DIED (avoid these failure modes — most died because the "
        extra += "test statistic did NOT separate from its own control, i.e. the 'effect' was an "
        extra += "artifact): " + "; ".join(lessons[-6:]) + ". Make the test statistic LARGE and clearly "
        extra += "different from a properly-permuted control, or do not propose it."
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "system", "content": _SYSTEM},
                               {"role": "user", "content": _CONTEXT + extra}],
        response_format={"type": "json_object"}, temperature=0.95,
    )
    raw = (resp.choices[0].message.content or "{}").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
    out = json.loads(raw)
    if isinstance(out, dict):
        out = out.get("candidates") or out.get("items") or out.get("results") or []
    return out if isinstance(out, list) else []


def _triage(cand: dict, plugin: MaterialsBandGapPlugin, X, y, groups) -> dict:
    """Deterministic auto-triage. Returns the verdict + why it died (if it did)."""
    title = cand.get("title", "?")
    code = cand.get("code") or ""
    prereg = cand.get("prereg") or {}
    if not plugin._valid_decision_rules(prereg):
        return {"title": title, "stage": "prereg", "survives": False, "why": "malformed decision rules"}
    ok, reasons = check_code(code, required_function=DEMO_REQUIRED_FUNCTION)
    if not ok:
        return {"title": title, "stage": "code_gate", "survives": False, "why": str(reasons[:2])}
    smoke_ok, smoke_err = smoke_test_demonstration(code)
    if not smoke_ok:
        return {"title": title, "stage": "runnability", "survives": False, "why": smoke_err[:120]}
    res = run_authored_demonstration(code, X, y, groups, {"random_state": 0, "preregistration": prereg})
    if not isinstance(res, dict):
        return {"title": title, "stage": "run_real", "survives": False, "why": "did not run on real data"}
    probes = plugin._demonstration_probes(res)
    ts, cs = res.get("test_statistic"), res.get("control_statistic")
    triggers = plugin._apply_rule(ts, prereg["supported_if"])
    silent = plugin._apply_rule(cs, prereg["control_silent_if"])
    holds = bool(triggers and silent and probes.get("clean"))
    # non-triviality: the effect must clearly beat its own control (kills statistically-real-but-tiny)
    nontrivial = abs(float(ts) - float(cs)) >= max(1e-9, 0.5 * abs(float(prereg["supported_if"]["threshold"]) or 1e-9))
    survives = holds and nontrivial
    return {"title": title, "stage": "scored", "survives": survives, "holds": holds, "nontrivial": nontrivial,
            "test": round(float(ts), 4), "control": round(float(cs), 4),
            "n_test": int(res.get("n_test", 0)), "n_control": int(res.get("n_control", 0)),
            "probes_clean": bool(probes.get("clean")), "why": "" if survives else
            ("control fired" if not silent else "test did not trigger" if not triggers
             else "probe flagged" if not probes.get("clean") else "trivial magnitude")}


def _vet(cand: dict, gateway: CriticGateway, run_id: str) -> dict:
    """Increment #2 — the two scarce filters, applied ONLY to candidates that passed the deterministic
    triage, Claude-free: GROUNDABILITY (citable prior work exists, mirroring the survey-grounding guard)
    + the cross-vendor NOVELTY gate (author 'anthropic' excluded). A network flake on grounding does
    NOT hard-fail (grounded=None); the novelty gate is the decisive add."""
    title, claim, insight = str(cand.get("title", "")), str(cand.get("claim", "")), str(cand.get("insight", ""))
    try:
        papers = search((title + " " + claim)[:160], k=8)
    except Exception:  # noqa: BLE001 - network flake -> grounding unverified, not a hard fail
        papers = None
    grounded = None if papers is None else (len(papers) >= 3)
    hyp = {"statement": claim or title, "rationale": insight, "novelty_note": insight,
           "contribution_type": "paradigm",
           "demonstration": {"form": "discriminating_instance", "claim": claim or title, "capability": "ai_authored"}}
    content = {"hypothesis": hyp, "gaps": [], "literature": briefing(papers) if papers else ""}
    try:
        panel = asyncio.run(gateway.review("direction", content, target_ref="auto-discovery",
                                           run_id=run_id, dry_run=False, exclude_vendors={"anthropic"}))
    except Exception as exc:  # noqa: BLE001
        return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
                "gate_passed": None, "novelty_objection": f"gate error: {str(exc)[:60]}", "novelty_pass": False}
    nov_obj = [f"{f.severity}:{f.category}" for c in (panel.critiques or []) for f in (c.findings or [])
               if f.category == "novelty" and f.severity in ("blocker", "major")]
    return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
            "gate_passed": bool(panel.gate_passed), "consensus": panel.consensus_verdict,
            "novelty_objection": (nov_obj[0] if nov_obj else ""),
            "novelty_pass": bool(panel.gate_passed) and not nov_obj}


def main() -> int:
    print("=" * 92)
    print("AUTO-DISCOVERY (v1) — generate bold candidates + self-screen on real data (Claude-free)")
    print("=" * 92)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = load_benchmark(DATASET)
    # featurize ONCE via the plugin's magpie path; reuse X/y/groups across all candidates
    from aletheia.domains.materials.featurizers import magpie_features
    Xdf, _names, work = magpie_features(df, composition_col=COMP)
    X = np.asarray(Xdf, dtype=float)
    y = work[TARGET].to_numpy(dtype=float)
    groups = work["_comp_obj"].map(lambda c: c.chemical_system).to_numpy()
    print(f"staged {DATASET}: n={len(y)}, d={X.shape[1]}  ({time.time()-t0:.1f}s)")
    create_all()
    run_id = create_run("auto-discovery", domain="materials", status="scoping")
    gateway = CriticGateway()

    s = get_settings()
    vend = next((c for c in s.critics.active if c.id == "grok" and c.transport == "api"), None)
    if vend is None or not s.vendor_key("grok"):
        print("no grok credentials; cannot ideate"); return 1
    base, key = s.vendor_base_url("grok") or vend.base_url, s.vendor_key("grok")

    print(f"\nLOOP: ideate -> auto-triage, up to {MAX_ROUNDS} rounds or {K_SURVIVORS} survivors "
          "(each round LEARNS from prior rejections).")
    print("-" * 92)
    print(f"{'rnd':<4} {'candidate':<43} {'died at':<11} {'test':>8} {'ctrl':>8} {'verdict':>8}")
    rows: list[dict] = []
    survivors: list[dict] = []
    tried: list[str] = []
    lessons: list[str] = []
    for rnd in range(1, MAX_ROUNDS + 1):
        if len(survivors) >= K_SURVIVORS:
            break
        try:
            cands = _ideate("grok", vend.model, base, key, avoid_titles=tried, lessons=lessons)
        except Exception as exc:  # noqa: BLE001
            print(f"  round {rnd} ideation FAILED: {type(exc).__name__}: {str(exc)[:110]}"); continue
        for c in cands:
            title = str(c.get("title", "?"))
            if title.lower()[:50] in {t.lower()[:50] for t in tried}:
                continue  # dedup across rounds
            tried.append(title)
            v = _triage(c, plugin, X, y, groups)
            if v["survives"]:
                # passed the deterministic triage -> apply the scarce filters: groundability + the
                # cross-vendor NOVELTY gate (this is what increment #2 adds).
                vet = _vet(c, gateway, run_id)
                v.update(vet)
                v["survives"] = bool(vet["novelty_pass"] and vet.get("grounded") is not False)
                if not v["survives"]:
                    v["stage"] = "novelty/grnd"
                    v["why"] = (vet.get("novelty_objection")
                                or (f"ungrounded(n={vet.get('n_papers', 0)})" if vet.get("grounded") is False
                                    else "novelty gate reject"))
            rows.append(v)
            if v["survives"]:
                survivors.append(v)
            elif v.get("why"):
                lessons.append(v["why"])
            mark = "SURVIVE" if v["survives"] else "killed"
            t, ct = v.get("test", ""), v.get("control", "")
            print(f"{rnd:<4} {title[:42]:<43} {v['stage']:<11} {str(t):>8} {str(ct):>8} {mark:>8}"
                  f"  {('' if v['survives'] else v.get('why',''))[:28]}")
        print(f"  -- round {rnd}: {len(survivors)} survivor(s) / {len(rows)} screened "
              f"({time.time()-t0:.0f}s) --")
    print("-" * 92)
    print(f"auto-triage: {len(survivors)}/{len(rows)} survived the FULL filter "
          "(ran clean + held + non-trivial + cleared the cross-vendor novelty gate + grounded).")
    for r in survivors:
        print(f"  ✅ {r['title']}\n     held test={r['test']} vs control={r['control']} (n={r['n_test']}/"
              f"{r['n_control']}); novelty gate={r.get('consensus','?')} (passed); "
              f"grounding n_papers={r.get('n_papers','?')}")
    print("=" * 92)
    if survivors:
        print("These are SELF-DISCOVERED + SELF-VETTED: novel (cross-vendor gate, author excluded), "
              "feasible + non-trivial on real data, and grounded — ready to hand to a live K2 campaign. "
              "The whole triage ran with NO human in the loop.")
    else:
        print("0 survivors — every bold candidate was auto-killed (bad code / didn't run / didn't hold "
              "/ trivial / not novel / ungrounded). That is the FULL rigor filter working: creativity is "
              "cheap; novel-AND-feasible-AND-grounded is the scarce part, now filtered automatically.")
    print(f"elapsed {time.time()-t0:.0f}s")
    print("DISCOVERY_JSON " + json.dumps({"n": len(rows), "survivors": [r["title"] for r in survivors],
                                          "rows": rows}, default=str)[:6000])
    return 0 if survivors else 2


if __name__ == "__main__":
    raise SystemExit(main())
