"""K2 (campaign learning loop) — deterministic outcome-reason classifier.

After a paradigm experiment runs, the campaign needs to know *why* the demonstration
held or failed, not just a headline metric + an approve/reject token. ``classify_outcome``
is a PURE FUNCTION over the result dicts the harness already produced (the demonstration
result, the reproduction record, the independent-audit verdict, and the results-gate
verdict). It assigns one typed reason from a fixed taxonomy and a concrete *narrowing hint*
that the next-round planner (``_campaign_step``) feeds back to the author — so round N+1 is
provably shaped by round N's reason rather than re-guessing blind.

It NEVER decides ``holds``/``supported``/strength (those stay harness-owned — the world
model proposes, the spine disposes); it only *reads* the already-sealed verdict and explains
it. No I/O, no LLM, fully deterministic and unit-testable.
"""

from __future__ import annotations

import math
from typing import Any

# The reason taxonomy. Each maps a refute/confirm outcome to the single most actionable
# diagnosis the next round can act on.
REASON_GENERALIZED = "generalized"            # held on the held-out confirm split + passed review
REASON_DID_NOT_GENERALIZE = "did_not_generalize"  # effect calibrated on explore vanished on confirm
REASON_THRESHOLD_TOO_STRONG = "threshold_too_strong"  # effect present but pre-reg bar set too high
REASON_CONTROL_NOT_SILENT = "control_not_silent"  # the negative control fired -> not specific
REASON_SAMPLE_STARVED = "sample_starved"      # too few rows on the confirm/test side to decide
REASON_AUDIT_REFUTED = "audit_refuted"        # an independent cross-vendor auditor refuted it
REASON_SCOPE_OVERCLAIM = "scope_overclaim"    # held, but the broader claim outran the demonstration
REASON_CONFOUND = "confound"                  # a leakage/degeneracy probe flagged the statistic
REASON_INFRA_DEGRADED = "infra_degraded"      # didn't run / wasn't independently verified
REASON_NO_DEMONSTRATION = "no_demonstration"  # non-paradigm round (no AI-authored demonstration)

REASONS = (
    REASON_GENERALIZED, REASON_DID_NOT_GENERALIZE, REASON_THRESHOLD_TOO_STRONG,
    REASON_CONTROL_NOT_SILENT, REASON_SAMPLE_STARVED, REASON_AUDIT_REFUTED,
    REASON_SCOPE_OVERCLAIM, REASON_CONFOUND, REASON_INFRA_DEGRADED, REASON_NO_DEMONSTRATION,
)

# Reasons where re-running the SAME hypothesis is pointless — the effect itself was not there
# on held-out data, so the next round should pivot, not re-tune.
_PIVOT_REASONS = frozenset({REASON_DID_NOT_GENERALIZE})


def _near_miss(test_stat: Any, supported_if: Any, frac: float = 0.8) -> bool:
    """True when the test statistic fell only JUST short of the pre-registered bar (the effect
    is present but the threshold was set too strong), vs far short (the effect did not appear
    on confirm at all). Handles both higher-is-better (>=, >) and lower-is-better (<=, <) rules."""
    if not isinstance(supported_if, dict):
        return False
    try:
        op = str(supported_if.get("op"))
        thr = float(supported_if.get("threshold"))
        t = float(test_stat)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(t) and math.isfinite(thr)):
        return False
    if op in (">=", ">"):
        if thr <= 0:
            return t > 0  # no ratio to form; any positive effect counts as a near miss
        return t >= frac * thr
    if op in ("<=", "<"):
        if thr <= 0:
            return False
        return t <= (2.0 - frac) * thr
    return False


def classify_outcome(
    demo: dict | None,
    reproduction: dict | None = None,
    *,
    audit_passed: bool | None = None,
    audit_error: bool = False,
    gate_passed: bool | None = None,
    verdict: str = "",
    min_confirm_n: int = 0,
) -> dict[str, Any]:
    """Classify a finished experiment's demonstration outcome into one typed reason + a
    concrete narrowing hint for the next round.

    Args mirror what ``_run_experiment`` has in scope at its return:
    - ``demo``: ``info["demonstration"]`` (the harness-owned demonstration result, or None).
    - ``reproduction``: ``info["reproduction"]`` (independent reseed record).
    - ``audit_passed`` / ``audit_error``: the ``_audit_demonstration`` two-state result.
    - ``gate_passed`` / ``verdict``: the results-gate panel outcome.
    - ``min_confirm_n``: starvation floor for the confirm partition (0 disables the check).

    Returns ``{"reason", "narrowing_hint", "recoverable", "detail"}``. Pure + deterministic.
    """
    if not isinstance(demo, dict):
        reject = str(verdict).lower() == "reject" or gate_passed is False
        return {
            "reason": REASON_NO_DEMONSTRATION,
            "narrowing_hint": (
                "the results gate rejected this round; address the critique directly"
                if reject else ""
            ),
            "recoverable": not reject,
            "detail": "no AI-authored demonstration on this round",
        }

    holds = demo.get("holds")
    detail_text = str(demo.get("detail") or "").lower()
    audit_refuted = bool(demo.get("audit_refuted"))
    prereg = demo.get("preregistration") or {}
    supported_if = prereg.get("supported_if") or {}
    test_stat = demo.get("test_statistic")
    test_triggers = demo.get("test_triggers")
    control_silent = demo.get("control_silent")
    probes = demo.get("probes") or {}
    probes_clean = probes.get("clean", True)
    n_confirm = demo.get("n_confirm")
    statistic = demo.get("statistic")

    # 1) An independent auditor refuted it (deterministic seal #5 or cross-vendor panel). The
    # most informative signal: the *definition* was challenged, not just the threshold.
    if audit_refuted:
        return {
            "reason": REASON_AUDIT_REFUTED,
            "narrowing_hint": (
                "an independent cross-vendor auditor refuted the demonstration (e.g. a circular or "
                "leaky statistic definition, or a threshold inconsistent with the exploration). "
                "Fix the auditor's specific objection — redefine the statistic to be independent of "
                "the labels it predicts; do NOT re-run the same statistic unchanged."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "audit refuted"),
        }

    # 2) The authored demonstration code itself failed the static code gate at compute time.
    if holds is False and statistic is None and "code gate" in detail_text:
        return {
            "reason": REASON_INFRA_DEGRADED,
            "narrowing_hint": (
                "the authored demonstration code failed the static code gate when it ran. Repair "
                "the code (imports / signature / forbidden ops) and keep the same hypothesis."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "code gate rejection"),
        }

    # 3) Not evaluated (holds is None): the demonstration never produced a verdict.
    if holds is None:
        if "split index does not match" in detail_text or "seal mismatch" in detail_text:
            hint = ("the explore/confirm split index did not match the staged data (seal mismatch). "
                    "Re-run with consistent staging; the hypothesis is untested, not refuted.")
        elif any(k in detail_text for k in ("sample", "too few", "too small", "starved", "min_samples")):
            return {
                "reason": REASON_SAMPLE_STARVED,
                "narrowing_hint": (
                    "the confirm/test partition had too few rows to decide. Scale the data (larger n "
                    "or a coarser grouping) so a held-out confirm split can clear the sample floor."
                ),
                "recoverable": True,
                "detail": str(demo.get("detail") or "starved sample"),
            }
        else:
            hint = ("the demonstration did not execute (sandbox timeout / staging failure). Fix the "
                    "authored code or environment and re-run the same hypothesis — it is untested.")
        return {
            "reason": REASON_INFRA_DEGRADED,
            "narrowing_hint": hint,
            "recoverable": True,
            "detail": str(demo.get("detail") or "not evaluated"),
        }

    # 4) Held on confirm, but it could not be INDEPENDENTLY verified (audit infra errored, audit
    # disabled, or a passing audit below the vendor floor). A positive we cannot yet trust.
    if holds is True and audit_error:
        return {
            "reason": REASON_INFRA_DEGRADED,
            "narrowing_hint": (
                "the demonstration held on the confirm split but could NOT be independently verified "
                "(audit infrastructure degraded or below the cross-vendor floor). Re-run to obtain "
                "independent verification before building a stronger claim on it."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "held but unverified"),
        }

    # 5) Held on confirm.
    if holds is True:
        demo_repro = (reproduction or {}).get("demonstration_reproduced")
        if (reproduction or {}).get("attempted") and demo_repro is False:
            return {
                "reason": REASON_DID_NOT_GENERALIZE,
                "narrowing_hint": (
                    "the effect held once but did NOT reproduce on an independent reseed — likely "
                    "seed-specific noise, not a stable effect. Change the effect/feature being "
                    "tested; do not re-tune the threshold on the same statistic."
                ),
                "recoverable": False,
                "detail": str(demo.get("detail") or "held but did not reproduce"),
            }
        if gate_passed is False or str(verdict).lower() == "reject":
            return {
                "reason": REASON_SCOPE_OVERCLAIM,
                "narrowing_hint": (
                    "the demonstration HELD on the held-out confirm split, but peer review rejected "
                    "the broader claim — the claim outran the single thing demonstrated. NARROW the "
                    "claim to exactly what this confirm-split demonstration shows, OR author a "
                    "separate discriminating demonstration for each still-unshown sub-claim/pillar."
                ),
                "recoverable": True,
                "detail": str(demo.get("detail") or "held; broader claim rejected"),
            }
        return {
            "reason": REASON_GENERALIZED,
            "narrowing_hint": (
                "the effect held on the held-out confirm split and passed review. Build on it: "
                "ablate the responsible mechanism, scale n, or test a boundary where it should break."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "held and verified"),
        }

    # 6) Refuted (holds is False, evaluated). Break down WHY, mirroring the harness verdict order
    # (control specificity -> threshold -> probe), so the diagnosis matches what `holds` checked.
    if min_confirm_n and isinstance(n_confirm, int) and 0 < n_confirm < min_confirm_n:
        return {
            "reason": REASON_SAMPLE_STARVED,
            "narrowing_hint": (
                "the confirm partition cleared staging but was thin; the test was underpowered. "
                "Scale the data so the held-out confirm split is comfortably above the sample floor."
            ),
            "recoverable": True,
            "detail": f"n_confirm={n_confirm} below floor {min_confirm_n}",
        }
    if control_silent is False:
        return {
            "reason": REASON_CONTROL_NOT_SILENT,
            "narrowing_hint": (
                "the negative control FIRED — the statistic is picking up signal even when the "
                "hypothesised cause is removed, so the effect is not specific. Redefine the control "
                "(or the statistic) so a true null genuinely produces no signal."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "control fired"),
        }
    if test_triggers is False:
        if _near_miss(test_stat, supported_if):
            return {
                "reason": REASON_THRESHOLD_TOO_STRONG,
                "narrowing_hint": (
                    "the effect was present on confirm but fell just short of the pre-registered "
                    "bar — it is real but weaker than the explore estimate (the explore->confirm "
                    "seal worked). Calibrate the threshold more conservatively, or gather more data."
                ),
                "recoverable": True,
                "detail": str(demo.get("detail") or "test below threshold (near miss)"),
            }
        return {
            "reason": REASON_DID_NOT_GENERALIZE,
            "narrowing_hint": (
                "the effect calibrated on the explore partition did NOT appear on the held-out "
                "confirm split — it was likely explore-set noise. Change the effect/feature being "
                "tested; do not just lower the threshold."
            ),
            "recoverable": False,
            "detail": str(demo.get("detail") or "test far below threshold"),
        }
    if not probes_clean:
        return {
            "reason": REASON_CONFOUND,
            "narrowing_hint": (
                "a leakage/degeneracy probe flagged the demonstration — there may be label leakage "
                "or a degenerate feature inflating the statistic. Remove the suspected leak and "
                "re-run before trusting the effect."
            ),
            "recoverable": True,
            "detail": str(demo.get("detail") or "probe flagged"),
        }

    # Fallback: refuted for a reason the breakdown didn't name (should be rare).
    return {
        "reason": REASON_DID_NOT_GENERALIZE,
        "narrowing_hint": (
            "the demonstration did not hold on the confirm split. Reconsider the effect or the "
            "statistic rather than re-tuning the threshold."
        ),
        "recoverable": False,
        "detail": str(demo.get("detail") or "did not hold"),
    }
