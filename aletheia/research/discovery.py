"""Autonomous discovery loop — the system GENERATES bold candidates and SELF-SCREENS them through the
full rigor filter, surfacing only novel-AND-feasible-AND-grounded survivors. Promoted from
scripts/auto_discovery.py into a tested package module so the driver can run it as a STAGE.

The triage is the scarce filter ("creativity is cheap"). Each candidate carries runnable
`compute_demonstration` code; the harness screens it deterministically (prereg-valid -> code-gate ->
runnable/non-degenerate -> an exploratory signal on the exploration partition), then applies the two
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
AuditableNoveltyGateFn = Callable[[dict[str, Any]], Any]

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


def materials_angle_context(n: int, target_desc: str = "band gap, eV", gaps=None) -> str:
    """Like ``materials_ideate_context`` but asks for ANGLES ONLY (no code). Used in CO-AUTHOR mode:
    grok proposes the oblique angle + pre-registration; a separate orchestrator model writes the code.

    Tuned against the two empirical failure modes (live run d02df7ee: 16/20 angles NULL, 1 real-but-known):
    demand a concrete PHYSICAL MECHANISM (abstract feature-statistic fishing is null on the data) and,
    when given, seed the survey ``gaps`` so angles target unexplored (likelier-novel) territory."""
    gap_block = ""
    if gaps:
        gap_block = ("\n\nThe field's OPEN GAPS from our literature survey — TARGET these (unexplored "
                     "territory is far likelier to clear the NOVELTY gate):\n- "
                     + "\n- ".join(str(g)[:160] for g in list(gaps)[:6]))
    return (
        f"Propose {n} EXTREMELY NOVEL, oblique research ANGLES about a composition->property MODEL "
        f"(Magpie composition features -> {target_desc}). DO NOT write code — an expert engineer "
        "implements each.\n"
        "TWO failure modes wreck most proposals (observed empirically — avoid BOTH):\n"
        "  (1) NULL angles: an abstract feature-statistic pattern (e.g. 'kurtosis of feature X on "
        "subset Y') with NO physical reason shows NO real effect on the data. EVERY angle MUST name a "
        "concrete PHYSICAL/CHEMICAL MECHANISM — a real reason a composition-AVERAGED model fails on a "
        "specific, chemically-defined stratum (a structural / electronic / bonding / site-specific "
        "effect that permutation-invariant composition aggregates provably CANNOT resolve). No "
        "mechanism => NULL => do not propose it.\n"
        "  (2) KNOWN effects: a real effect the literature already characterizes fails the novelty gate. "
        "In particular the TEXTBOOK limitation — 'composition-average / Magpie descriptors cannot encode "
        "local / site / coordination / structural information' — is KNOWN (it is the exact reason graph "
        "neural networks like CGCNN were invented); the gate WILL reject any angle that is an instance of "
        "it, however dressed up (apical-vs-equatorial sites, dimers, lone pairs, hybridization asymmetry "
        "are ALL this). Do NOT propose 'the model misses [structural/site/local/bonding] effect X'.\n"
        "Your novelty must instead be SPECIFIC and SURPRISING: a NAMED chemical family (e.g. "
        "'multi-alkaline-earth cuprates', NOT 'oxides') + a QUANTIFIED mechanism (an actual number — a "
        "doping level, a bond-length or electron-count threshold, a stoichiometric ratio) that even a "
        "structure-aware practitioner would NOT predict. Frame it as a rigorously-mapped SPECIFIC failure "
        "CASE, never a general principle.\n"
        "EACH angle is JSON: {\"title\": str, \"insight\": str (the PHYSICAL MECHANISM + why composition "
        "averaging misses it), \"claim\": str (a falsifiable, checkable statement), \"prereg\": "
        "{\"supported_if\": {\"op\": \">=\"|\">\"|\"<=\"|\"<\", \"threshold\": float}, \"control_silent_if\": "
        "{\"op\": \"<\"|\"<=\", \"threshold\": float}}}.\n"
        "The implementer receives X (Magpie composition features, ~132 dims, PERMUTATION-INVARIANT "
        "aggregates), y (" + target_desc + "), groups (chemical-system STRING per row, e.g. 'As-Ga' = "
        "the sorted element set), and produces a test_statistic capturing your mechanism on the relevant "
        "stratum AND a control_statistic that VANISHES (~0) under a permuted-label/strata null.\n"
        "DO NOT propose (tried, dead): applicability-domain / distance-to-training; error-vs-rarity; "
        "cliffs; epistemic-vs-aleatoric / Bayes-floor; OR any premise depending on formula STRING ORDER "
        "(X is permutation-invariant -> NULL)." + gap_block
        + "\nReturn STRICT JSON: {\"candidates\": [ ... ]}."
    )


CODE_AUTHOR_SYSTEM = (
    "You are an expert ML-for-science engineer. A colleague hands you a discriminating research ANGLE "
    "and the exact data contract; you write the CORRECT, ROBUST code that tests it. You are obsessive "
    "about: (1) the negative control genuinely VANISHING under the null, (2) NEVER returning a 0-sample "
    "test or control, (3) finite statistics (guard divisions/variances), (4) using ONLY allowed imports. "
    "Output ONLY one ```python code block."
)


def code_author_prompt(angle: dict, target_desc: str = "band gap, eV") -> str:
    """Prompt the configured orchestrator to implement ``compute_demonstration`` for one angle, matching the
    exact contract the harness screens (the same data shape + return dict as ``materials_ideate_context``)."""
    import json as _json
    title, insight = str(angle.get("title", "")), str(angle.get("insight", ""))
    claim, prereg = str(angle.get("claim", "")), angle.get("prereg") or {}
    return (
        "Implement EXACTLY `def compute_demonstration(X, y, groups, meta): ...` for this colleague's angle.\n\n"
        f"TITLE: {title}\nINSIGHT (mechanism): {insight}\nCLAIM (falsifiable): {claim}\n"
        f"PRE-REGISTERED DECISION RULE (do NOT change it; your statistics must be on its scale): "
        f"{_json.dumps(prereg)}\n\n"
        "DATA CONTRACT: X = Magpie composition features (~132 dims, PERMUTATION-INVARIANT aggregates); "
        "y = " + target_desc + "; groups = a chemical-system STRING per row, e.g. 'As-Ga' (the sorted "
        "element set — recover elements with group.split('-')); meta = {random_state, preregistration}.\n"
        "RETURN a dict: {\"test_statistic\": float, \"control_statistic\": float, \"n_test\": int>0, "
        "\"n_control\": int>0, \"components\": dict, \"detail\": str}. HOLDS is decided by the HARNESS "
        "(supported_if on test_statistic AND control_silent_if on control_statistic AND probes-clean); "
        "NEVER return 'holds'.\n"
        "The control MUST be a real permuted-label/strata negative control that ~vanishes if the effect "
        "is null. Ensure BOTH n_test and n_control are > 0 on real data (guard empty selections). Make "
        "statistics finite (guard zero variance / division). Use meta['random_state'] for any split.\n"
        "HARD RULES (statically checked): import ONLY from sklearn, numpy, scipy, pandas, math, statistics, "
        "collections, itertools, functools, random, warnings. No file/network/process, no eval/exec/open/"
        "__import__. Output ONLY the ```python block."
    )


def make_worker_code_author(run_id: str, *, target_desc: str = "band gap, eV", model: str | None = None,
                            dry_run: bool = False, loop=None, worker=None, extract=None):
    """Return a SYNC ``author_fn(angle) -> code`` that uses the configured orchestrator provider to
    write ``compute_demonstration`` for a vendor-proposed angle.

    Some transports require the MAIN-thread event loop rather than a fresh loop inside a worker
    thread. Since ``discover`` runs in ``asyncio.to_thread``, pass the driver's running ``loop`` and
    authoring is submitted via ``run_coroutine_threadsafe``. When ``loop is None`` (the synchronous
    standalone CLI), it falls back to ``asyncio.run``. ``loop``/``worker``/``extract`` are injectable
    for offline tests."""
    import asyncio as _asyncio

    def author(angle: dict) -> str:
        _worker, _extract = worker, extract
        if _worker is None:
            from aletheia.orchestrator.worker import run_worker as _worker
        if _extract is None:
            from aletheia.coder.worker import extract_code as _extract
        coro = _worker(run_id, "discovery-coder", code_author_prompt(angle, target_desc),
                       system=CODE_AUTHOR_SYSTEM, model=model, dry_run=dry_run)
        if loop is not None:  # submit to the driver's main loop (the SDK needs the main thread)
            text = _asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=600)
        else:                 # standalone CLI: already on the main thread
            text = _asyncio.run(coro)
        return _extract(text) or ""

    return author


# Backward compatibility for callers written before orchestrator providers were pluggable.
make_claude_code_author = make_worker_code_author


def make_coauthor_ideator(angle_ideate_fn: IdeateFn, author_fn, log=None) -> IdeateFn:
    """Combine a grok angle ideator with an orchestrator code author: grok proposes the
    oblique angle, the configured model writes the code, and the harness screens it. One angle's authoring failing
    drops only that candidate. Returns candidate dicts carrying both the angle fields AND ``code``.

    ``log`` (default no-op) is called per angle with a progress line — pass ``print`` so the otherwise
    silent ~6-24 authoring calls keep emitting output (visibility + it stops a stall watcher firing)."""
    _log = log or (lambda *_a: None)

    def ideate(avoid_titles: list[str], lessons: list[str]) -> list[dict[str, Any]]:
        angles = [a for a in angle_ideate_fn(avoid_titles, lessons) if isinstance(a, dict)]
        _log(f"[coauthor] grok proposed {len(angles)} angle(s); orchestrator authoring code...")
        out: list[dict[str, Any]] = []
        for i, a in enumerate(angles):
            try:
                code = author_fn(a)
            except Exception as exc:  # noqa: BLE001 - one angle's authoring failing shouldn't kill the round
                code, exc_note = "", f" ({type(exc).__name__})"
            else:
                exc_note = ""
            _log(f"[coauthor] {i + 1}/{len(angles)} {'authored' if code else 'FAILED' + exc_note}: "
                 f"{str(a.get('title', '?'))[:54]}")
            if code:
                out.append({**a, "code": code})
        return out

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


# How far the test effect must dominate its (vanishing) control for the discovery PRE-FILTER to
# promote a candidate. Decoupled from grok's blind supported_if threshold; the campaign's
# explore/confirm sets the rigorous bar downstream.
_DISCOVERY_SEP_RATIO = 3.0


def screen_deterministic(cand: dict, plugin, X, y, groups) -> Screened:
    """Cheap, deterministic pre-filter: prereg-valid -> code-gate -> runnable/non-degenerate ->
    runs on the harness-provided EXPLORATION partition -> the effect separates from a vanishing
    control. ``survives`` means promising enough to promote, not confirmatory support. ``holds``
    separately records whether the candidate's original pre-registered rule actually fired."""
    title = str(cand.get("title", "?"))
    code = cand.get("code") or ""
    prereg = cand.get("prereg") or {}
    if not plugin._valid_decision_rules(prereg):
        return Screened(title, "prereg", False, "malformed decision rules")
    ok, reasons = check_code(code, required_function=DEMO_REQUIRED_FUNCTION)
    if not ok:
        return Screened(title, "code_gate", False, str(reasons[:2]))
    # Smoke-test on a REAL-data slice (true feature dims + group dtype), NOT the synthetic
    # 8-col/integer-group fallback: candidates parse `groups` as chemical-system strings, so the
    # fallback would false-kill them (AttributeError / degenerate selection) before the real run.
    import numpy as _np
    _ya = _np.asarray(y)
    _n = len(_ya)
    if _n > 1024:
        _idx = _np.random.default_rng(0).choice(_n, size=1024, replace=False)
        _sample = (_np.asarray(X)[_idx], _ya[_idx], _np.asarray(groups, dtype=str)[_idx])
    else:
        _sample = (_np.asarray(X), _ya, _np.asarray(groups, dtype=str))
    smoke_ok, smoke_err = smoke_test_demonstration(code, sample=_sample)
    if not smoke_ok:
        return Screened(title, "runnability", False, smoke_err[:120])
    res = run_authored_demonstration(code, X, y, groups, {"random_state": 0, "preregistration": prereg})
    if not isinstance(res, dict):
        return Screened(title, "run_real", False, "did not run on real data")
    probes = plugin._demonstration_probes(res)
    ts, cs = float(res.get("test_statistic")), float(res.get("control_statistic"))
    silent = plugin._apply_rule(cs, prereg["control_silent_if"])
    prereg_triggers = plugin._apply_rule(ts, prereg["supported_if"])
    # Decoupled from grok's blind supported_if: a candidate is worth promoting if its control VANISHES
    # (silent) AND the test effect DOMINATES that vanishing control by >= _DISCOVERY_SEP_RATIOx. Uses
    # the control's own scale (its observed magnitude, floored by the silent-bar) so it is scale-free.
    control_bar = abs(float(prereg["control_silent_if"].get("threshold", 0.0) or 0.0)) or 1e-9
    separated = abs(ts) >= _DISCOVERY_SEP_RATIO * max(abs(cs), control_bar)
    clean = bool(probes.get("clean"))
    survives = bool(silent and separated and clean)
    why = "" if survives else (
        "control fired" if not silent
        else "effect too small vs control" if not separated
        else "probe flagged")
    prereg_holds = bool(prereg_triggers and silent and clean)
    return Screened(title, "scored", survives, why, round(ts, 4), round(cs, 4),
                    int(res.get("n_test", 0)), int(res.get("n_control", 0)), prereg_holds)


def screen_novelty_grounding(cand: dict, gateway, run_id: str, *, search_fn, briefing_fn,
                             exclude_vendors: set | None = None) -> dict:
    """The two scarce filters: GROUNDABILITY (citable prior work) + the cross-vendor NOVELTY gate.
    ``exclude_vendors`` lists all author vendors that must be absent from the novelty panel.
    A network failure yields ``grounded=None`` and therefore fails closed."""
    if not exclude_vendors:
        return {
            "grounded": None,
            "n_papers": 0,
            "gate_passed": None,
            "consensus": "error",
            "novelty_objection": "author vendors were not declared for independent review",
            "novelty_pass": False,
        }
    title, claim, insight = str(cand.get("title", "")), str(cand.get("claim", "")), str(cand.get("insight", ""))
    try:
        papers = search_fn((title + " " + claim)[:160], 8)
    except Exception:  # noqa: BLE001
        papers = None
    grounded = None if papers is None else (len(papers) >= 3)
    if grounded is not True:
        return {
            "grounded": grounded,
            "n_papers": 0 if papers is None else len(papers),
            "gate_passed": None,
            "consensus": "not_reviewed",
            "novelty_objection": (
                "literature retrieval unavailable"
                if papers is None
                else f"insufficient literature grounding (n={len(papers)})"
            ),
            "novelty_pass": False,
        }
    hyp = {"statement": claim or title, "rationale": insight, "novelty_note": insight,
           "contribution_type": "paradigm",
           "demonstration": {"form": "discriminating_instance", "claim": claim or title, "capability": "ai_authored"}}
    content = {"hypothesis": hyp, "gaps": [], "literature": briefing_fn(papers) if papers else ""}
    try:
        panel = asyncio.run(gateway.review("direction", content, target_ref="auto-discovery",
                                           run_id=run_id, dry_run=False,
                                           exclude_vendors=exclude_vendors))
    except Exception as exc:  # noqa: BLE001
        return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
                "gate_passed": None, "consensus": "error", "novelty_objection": str(exc)[:60], "novelty_pass": False}
    nov_findings = [f for c in (panel.critiques or []) for f in (c.findings or [])
                    if f.category == "novelty" and f.severity in ("blocker", "major")]
    reviewers = {str(getattr(c, "critic_id", "")) for c in (panel.critiques or [])}
    author_leak = reviewers & set(exclude_vendors)
    # capture the critic's EVIDENCE (why it's known), not just a "major:novelty" tag, so the lesson
    # fed back next round teaches grok the specific known-territory to avoid.
    def _obj_text(f):
        return str(getattr(f, "evidence", "") or getattr(f, "claim", "")
                   or getattr(f, "suggestion", "") or "novelty")[:200]
    return {"grounded": grounded, "n_papers": (0 if papers is None else len(papers)),
            "gate_passed": bool(panel.gate_passed), "consensus": panel.consensus_verdict,
            "novelty_objection": (
                f"author vendor leaked into novelty panel: {', '.join(sorted(author_leak))}"
                if author_leak else _obj_text(nov_findings[0]) if nov_findings else ""
            ),
            "novelty_pass": bool(panel.gate_passed) and not nov_findings and not author_leak}


def screen_auditable_novelty_gate(
    cand: dict[str, Any], gate_fn: AuditableNoveltyGateFn
) -> dict[str, Any]:
    """Use the F8-S5 artifact gate instead of the legacy count+critic shortcut.

    The candidate must carry the exact atomic-claim SHA-256 gated by the returned artifact. Any
    callback failure, wrong artifact type, or identity mismatch fails closed.
    """
    try:
        from aletheia.knowledge.novelty_decision import ResearchDirectionGate

        gate = gate_fn(cand)
        if not isinstance(gate, ResearchDirectionGate):
            raise TypeError("callback did not return a ResearchDirectionGate")
        assessment = gate.novelty_decision.assessment
        candidate_sha256 = str(cand.get("candidate_claim_sha256", ""))
        identity_matches = assessment.candidate_claim_sha256s == (candidate_sha256,)
        coverage_ok = (
            gate.novelty_decision.coverage.decision_verdict.value
            == "coverage_sufficient"
        )
        authorized = bool(gate.experiment_authorized and identity_matches and coverage_ok)
        objection = ""
        if not identity_matches:
            objection = "auditable novelty gate candidate identity mismatch"
        elif not authorized:
            objection = ", ".join(gate.rationale_codes)
        return {
            "grounded": coverage_ok,
            "n_papers": len(assessment.nearest_prior_art),
            "gate_passed": authorized,
            "consensus": gate.disposition.value,
            "novelty_objection": objection,
            "novelty_pass": authorized,
            "auditable_direction_gate_sha256": gate.gate_sha256,
            "novelty_classification": assessment.classification.value,
            "claim_strength_ceiling": assessment.claim_strength_ceiling.value,
        }
    except Exception as exc:  # noqa: BLE001 - the scientific gate must fail closed
        return {
            "grounded": None,
            "n_papers": 0,
            "gate_passed": False,
            "consensus": "error",
            "novelty_objection": f"auditable novelty gate error: {type(exc).__name__}",
            "novelty_pass": False,
        }


def _lesson_from(row: dict) -> str:
    """An actionable, failure-mode-specific lesson fed back to the ideator next round — so it learns
    the RIGHT correction (a null effect needs a real mechanism; a known effect needs more novelty)."""
    title, why = str(row.get("title", "?"))[:50], row.get("why", "")
    if row.get("stage") == "novelty/grnd" and row.get("holds"):
        return (f"'{title}' HELD but was judged NOT NOVEL — known because: {why}. Avoid this territory; "
                "be SPECIFIC (a NAMED chemical family + a QUANTIFIED mechanism), not a general principle")
    if why == "effect too small vs control":
        return f"'{title}' had a clean control but the effect was too WEAK to separate — needs a larger, more stratum-specific mechanism"
    if why == "control fired":
        return f"'{title}' had a non-vanishing control (confounded) — design a clean permuted-strata null"
    return f"'{title}': {why}"


def discover(*, ideate_fn: IdeateFn, plugin, X, y, groups, gateway, run_id: str,
             k_survivors: int = 2, max_rounds: int = 3,
             search_fn=None, briefing_fn=None, novelty_exclude: set | None = None,
             auditable_novelty_gate_fn: AuditableNoveltyGateFn | None = None,
             log=print) -> tuple[list[dict], list[dict]]:
    """Run the discovery loop: up to ``max_rounds`` ideation rounds (each LEARNS from prior
    rejections) or until ``k_survivors`` banked. Returns (survivors, all_screened) as dicts.
    A survivor ran clean, showed a non-trivial exploratory signal, and was grounded + novel.
    It is not confirmatory support until the sealed confirmation stage. ``cand`` dicts carry
    their `code`/`prereg`, so the driver can hand the demonstration straight to the campaign.
    When supplied, ``auditable_novelty_gate_fn`` replaces the legacy count/critic shortcut and
    requires an exact F8-S5 gate bound to ``candidate_claim_sha256``."""
    if auditable_novelty_gate_fn is None and (
        search_fn is None or briefing_fn is None
    ):
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
                vet = (
                    screen_auditable_novelty_gate(cand, auditable_novelty_gate_fn)
                    if auditable_novelty_gate_fn is not None
                    else screen_novelty_grounding(
                        cand,
                        gateway,
                        run_id,
                        search_fn=search_fn,
                        briefing_fn=briefing_fn,
                        exclude_vendors=novelty_exclude,
                    )
                )
                row.update(vet)
                # A retrieval error yields grounded=None; absence of evidence must never pass.
                row["survives"] = bool(vet["novelty_pass"] and vet.get("grounded") is True)
                if not row["survives"]:
                    row["stage"] = "novelty/grnd"
                    row["why"] = vet.get("novelty_objection") or (
                        "literature retrieval unavailable" if vet.get("grounded") is None
                        else f"ungrounded(n={vet.get('n_papers', 0)})"
                        if vet.get("grounded") is False
                        else "novelty gate"
                    )
                else:
                    row["candidate"] = cand  # carry code/prereg for the campaign
            all_rows.append(row)
            if row["survives"]:
                survivors.append(row)
            elif row.get("why"):
                lessons.append(_lesson_from(row))
            log(f"[discover] r{rnd} {'SURVIVE' if row['survives'] else 'killed':<8} {sc.stage:<12} "
                f"{title[:46]}  {('' if row['survives'] else row.get('why', ''))[:30]}")
            if len(survivors) >= k_survivors:
                break
        log(f"[discover] round {rnd}: {len(survivors)} survivor(s) / {len(all_rows)} screened")
    return survivors, all_rows
