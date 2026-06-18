"""Pure scoring of the K2 (campaign learning loop) live-acceptance criteria.

Extracted from ``scripts/real_k2_campaign_e2e.py`` so the acceptance logic is unit-testable and a
zero-verdict run can NEVER be mis-scored as a FULL PASS. Reads ONLY a run's event stream + its
persisted credences — the same surface a reviewer has — and returns a structured set of ✓/✗ checks
plus a three-way verdict. No live run, no LLM, no prints.

The bug this guards against: the per-check ``len(updates) == len(confirm_verdicts)`` is a SPINE
invariant ("the belief moved exactly on harness verdicts, never otherwise") that is correctly,
*vacuously* true when both are 0 — a run that produced no demonstration at all. The fix is NOT to
break that invariant but to add a separate POSITIVE-EVIDENCE gate to FULL: a FULL pass must show at
least one harness-verified confirm-split verdict that actually moved a calibrated belief. Without it,
a multi-round run that only ever recorded ``no_demonstration`` (priors seeded, predictions
pre-registered, credences persisted, a go/no-go pivot — but zero verdicts, zero updates, null
calibration) sailed through as FULL PASS. It now caps at PARTIAL.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Check:
    name: str
    ok: bool | None  # True ✓ / False ✗ / None — n/a (not applicable this run)
    detail: str


@dataclass
class K2Result:
    verdict: str  # "full" | "partial" | "fail"
    checks: list[Check] = field(default_factory=list)
    # surfaced for callers/tests (the numbers the verdict turns on)
    n_reasons: int = 0
    n_updates: int = 0
    n_confirm_verdicts: int = 0
    calibration: float | None = None


def _payloads(events: list[dict], etype: str) -> list[dict]:
    return [e.get("payload") or {} for e in events if e.get("type") == etype]


def _last(events: list[dict], etype: str) -> dict:
    found = _payloads(events, etype)
    return found[-1] if found else {}


def _final_confirm_verdicts(events: list[dict]) -> list[dict]:
    """Return one final harness demonstration verdict per experiment round.

    A live round can emit multiple ``demonstration`` events: the first compute result, a
    reproduction recompute, and a post-audit re-publication that mutates ``holds`` to ``False`` when
    the audit refutes the demonstration. K2 updates belief once per experiment outcome, so
    acceptance must compare belief updates against the final verdict per round, not every
    intermediate snapshot.

    Demonstration payloads do not currently repeat ``exp_id``, so infer the active round from the
    latest preceding ``experiment`` event. Historical/test streams without an ``experiment`` event
    fall back to treating each demonstration as its own round.
    """
    latest_by_round: dict[str, dict] = {}
    active_round: str | None = None
    synthetic = 0
    for e in events:
        payload = e.get("payload") or {}
        if e.get("type") == "experiment":
            active_round = str(payload.get("exp_id") or payload.get("round") or "") or None
        if e.get("type") != "demonstration":
            continue
        key = str(payload.get("exp_id") or active_round or "")
        if not key:
            synthetic += 1
            key = f"demo-{synthetic}"
        latest_by_round[key] = payload
    return [
        d for d in latest_by_round.values()
        if d.get("computed") and d.get("exploration_applied") and isinstance(d.get("holds"), bool)
    ]


def score_k2(events: list[dict], credences: list[dict]) -> K2Result:
    """Score the K2 acceptance criteria from a run's ``events`` (each ``{"type","payload",...}``,
    in chronological order) and its ``credences`` (``list_credences(run_id)``)."""
    priors = _payloads(events, "belief_prior")
    preds = _payloads(events, "belief_prediction")
    updates = _payloads(events, "belief_update")
    reasons = _payloads(events, "campaign_reason")
    plans = _payloads(events, "campaign_plan")
    finished = _last(events, "campaign_finished")
    # Harness-verified confirm-split verdicts: one FINAL demonstration outcome per experiment round.
    # Intermediate compute/reproduction/pre-audit snapshots are not separate campaign outcomes.
    confirm_verdicts = _final_confirm_verdicts(events)

    checks: list[Check] = []

    # 1. A reasoned trajectory: each round carries a typed reason (S2/S3).
    checks.append(Check(
        "reasoned trajectory — a typed reason per round",
        len(reasons) >= 1,
        f"{len(reasons)} campaign_reason event(s): " + ", ".join(
            f"r{r.get('round')}={r.get('reason')}" for r in reasons),
    ))

    # 2. The belief was SEEDED as a weak prior (never asserting belief).
    all_weak = bool(priors) and all(p.get("weak_prior") for p in priors)
    checks.append(Check(
        "belief seeded as a WEAK prior per lineage",
        all_weak,
        f"{len(priors)} belief_prior event(s), all weak_prior={all_weak}",
    ))

    # 3. Pre-registration: a forward prediction was committed, and it PRECEDES every belief_update of
    #    the same lineage in the stream (so predicted−realized surprise can't be back-fitted).
    ordering_ok = len(preds) >= 1
    for i, e in enumerate(events):
        if e.get("type") == "belief_update":
            qk = (e.get("payload") or {}).get("question_key")
            if not any(x.get("type") == "belief_prediction"
                       and (x.get("payload") or {}).get("question_key") == qk
                       for x in events[:i]):
                ordering_ok = False
    checks.append(Check(
        "forward prediction committed BEFORE each verdict (pre-registration)",
        ordering_ok,
        f"{len(preds)} belief_prediction event(s); ordering holds={ordering_ok}",
    ))

    # 4. The belief moved ONLY on harness-verified confirm-split verdicts, and exactly on them.
    #    (A SPINE invariant — correctly, vacuously true at zero. Positive learning is gated below.)
    updates_match_verdicts = len(updates) == len(confirm_verdicts)
    updates_well_formed = all(
        u.get("realized") in (0.0, 1.0) and u.get("surprise") is not None for u in updates)
    checks.append(Check(
        "credence moves ONLY on harness confirm-split verdicts (spine intact)",
        updates_match_verdicts and updates_well_formed,
        f"{len(updates)} belief_update(s) vs {len(confirm_verdicts)} harness confirm-split verdict(s); "
        f"all updates well-formed={updates_well_formed}",
    ))

    # 5. Calibration surfaced (only meaningful once a verdict actually moved the belief).
    calibration = finished.get("calibration")
    cal_ok = (calibration is not None) if updates else None  # n/a if no round produced a verdict
    checks.append(Check(
        "calibration (mean |predicted−realized|) surfaced in the synthesis",
        cal_ok,
        f"calibration={calibration}, n_belief_updates={finished.get('n_belief_updates')}",
    ))

    # 6. Durable persistence of the credences (S6).
    checks.append(Check(
        "credences persisted durably (belief_states)",
        len(credences) >= 1,
        f"{len(credences)} lineage(s): " + ", ".join(
            f"{c.get('question_key', '')[:24]}(a={c.get('alpha', 0):.2f},b={c.get('beta', 0):.2f},"
            f"n={c.get('n_updates')})" for c in credences),
    ))

    # 7. The loop LEARNED across rounds: >=2 rounds ran, and a go/no-go step chose a next experiment
    #    after a reason (round N+1 shaped by round N).
    multi_round = len(reasons) >= 2
    continued = any(p.get("continue") for p in plans)
    checks.append(Check(
        ">=2 rounds, round N+1 shaped by round N's reason (the K2 thesis)",
        multi_round and continued,
        f"{len(reasons)} round(s); a go/no-go chose to continue/pivot={continued}",
    ))

    # 8. The world model never set a verdict: every confirm-split verdict was harness-COMPUTED.
    spine_ok = all(d.get("computed") for d in confirm_verdicts) if confirm_verdicts else None
    checks.append(Check(
        "every verdict harness-owned (no LLM-set holds)",
        spine_ok,
        f"{len(confirm_verdicts)} confirm-split verdict(s), all harness-computed="
        f"{spine_ok if confirm_verdicts else 'n/a (no verdict this run)'}",
    ))

    # --- verdict --------------------------------------------------------------------------------
    core = [checks[i].ok for i in (0, 1, 2, 3, 5)]  # reasoned traj, weak prior, prereg, spine-moves, persist
    core_ok = all(bool(c) for c in core)
    spine_intact = checks[7].ok is not False
    # POSITIVE-EVIDENCE gate (the fix): a FULL pass MUST show the loop actually learned from a real
    # harness verdict — at least one confirm-split verdict that moved a calibrated belief. A
    # multi-round run with zero verdicts/updates is honest but caps at PARTIAL, never FULL.
    positive_evidence = (
        len(updates) >= 1 and len(confirm_verdicts) >= 1 and calibration is not None
    )
    full = core_ok and spine_intact and bool(checks[6].ok) and positive_evidence

    if full:
        verdict = "full"
    elif core_ok and spine_intact:
        verdict = "partial"
    else:
        verdict = "fail"

    return K2Result(
        verdict=verdict,
        checks=checks,
        n_reasons=len(reasons),
        n_updates=len(updates),
        n_confirm_verdicts=len(confirm_verdicts),
        calibration=calibration,
    )
