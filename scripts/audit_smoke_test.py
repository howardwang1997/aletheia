"""① AUDIT smoke test — isolate the cross-vendor demonstration audit and prove it can clear the
anti-fakeability vendor floor on a DIRECT connection, WITHOUT a full e2e run.

Why this exists
---------------
K2 FULL is gated by four stochastic stages that must align in one expensive run. The cheapest and
LEAST-proven of them is the cross-vendor demonstration audit: in the proof run (160232) the science
worked end-to-end (held + reproduced + 3 verdicts + belief moved) but the audit STARVED to zero
reachable vendors (grok APIConnectionError, anthropic-CLI ConnectionRefused) → fail-closed. So the
spine's most important gate — `_audit_demonstration` requiring >= `min_review_vendors` (2) DISTINCT
non-author vendors — has NEVER actually fired on a successful demonstration. This script exercises
exactly that gate, in isolation, with a CANNED clean demonstration payload:

    gateway.review("demonstration_audit", payload, ..., exclude_vendors={"anthropic"})

No training, no Claude long-stream (the author vendor is excluded AND a single audit round runs no
rebuttal), so it is Claude-free and safe to run in-process. It costs only a handful of short critic
calls. Run it with FlClash OFF (direct) to test the real reachability question; the per-vendor
breakdown shows which China-reachable auditors (deepseek, zhipu) actually respond on direct.

    conda run -n aletheia python scripts/audit_smoke_test.py

PASS = at least `min_review_vendors` DISTINCT non-author vendors returned a usable review (the floor
the FULL audit needs). The demonstration is deliberately clean, so a reachable, competent panel
should also APPROVE — reported as a secondary signal — but reachability is the thing under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from aletheia.config import get_settings
from aletheia.critics.gateway import CriticGateway
from aletheia.db import create_all
from aletheia.events.bus import get_bus
from aletheia.memory.service import create_run

# --- A realistic, leakage-free, well-controlled demonstration payload ---------------------------
# Hypothesis: prediction error INFLATES for materials containing rare (out-of-distribution) elements,
# and VANISHES on a matched common-element control. This is the same shape `_audit_demonstration`
# passes to the gateway: the authored code string, the computed {test,control} statistics, the
# pre-registration, the harness probe flags, and a one-line detail.
_DEMO_CODE = '''\
def compute_demonstration(X, y, groups, meta):
    """Rare-element error inflation: model error on rare-element materials vs a matched
    common-element control. groups carries the per-sample max element rarity rank."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import KFold

    rng = np.random.default_rng(int(meta.get("random_state", 0)))
    X = np.asarray(X, dtype=float); y = np.asarray(y, dtype=float)
    rarity = np.asarray(groups, dtype=float)  # higher = contains rarer elements

    # honest out-of-fold absolute errors (no train/test leakage): each point scored by a model
    # that never saw it.
    abs_err = np.zeros(len(y))
    for tr, te in KFold(n_splits=5, shuffle=True, random_state=int(meta.get("random_state", 0))).split(X):
        m = GradientBoostingRegressor(random_state=0).fit(X[tr], y[tr])
        abs_err[te] = np.abs(m.predict(X[te]) - y[te])

    # TEST: do the rarest-element materials carry inflated error relative to the median?
    hi = rarity >= np.quantile(rarity, 0.80)        # rare-element tail
    lo = rarity <= np.quantile(rarity, 0.50)        # common-element body
    med = np.median(abs_err[lo]) + 1e-9
    test_stat = float(np.median(abs_err[hi]) / med - 1.0)   # fractional inflation

    # CONTROL: split the COMMON-element body in two at random; the SAME statistic must vanish,
    # because neither half is out-of-distribution. A real OOD effect cannot show here.
    idx = np.where(lo)[0]; rng.shuffle(idx)
    a, b = idx[: len(idx) // 2], idx[len(idx) // 2 :]
    ctrl_stat = float(np.median(abs_err[a]) / (np.median(abs_err[b]) + 1e-9) - 1.0)
    return {
        "test_statistic": test_stat,
        "control_statistic": abs(ctrl_stat),
        "components": {"n_hi": int(hi.sum()), "n_lo": int(lo.sum()),
                       "median_err_hi": float(np.median(abs_err[hi])),
                       "median_err_lo": float(np.median(abs_err[lo]))},
        "detail": "OOF GBT abs-error: rare-element tail vs common-element body; control = common split-half",
        "n_test": int(hi.sum()),
        "n_control": int(len(idx) // 2),
    }
'''

_PREREG = {
    "statistic_name": "rare_element_error_inflation",
    "computation": "fractional inflation of median out-of-fold absolute error on the rarest-element "
                   "tail (>=80th pct rarity) relative to the common-element body (<=50th pct)",
    "control_description": "a random split-half of the COMMON-element body, same statistic across the "
                           "two halves — neither half is out-of-distribution",
    "expected_control": "if error inflation is driven by element rarity (OOD), it must vanish within "
                        "the common-element body where no sample is OOD; the control statistic ~ 0",
    "supported_if": {"op": ">=", "threshold": 0.15},
    "control_silent_if": {"op": "<=", "threshold": 0.05},
}

_PAYLOAD = {
    "demonstration_code": _DEMO_CODE,
    "test_statistic": 0.276,
    "control_statistic": 0.012,
    "preregistration": _PREREG,
    "probes": {"clean": True, "flags": [], "n_test": 142, "n_control": 173, "note": ""},
    "detail": (
        "AI-authored demonstration (rare_element_error_inflation): test=0.276 "
        "(supported_if >=0.15), control=0.012 (silent_if <=0.05) -> holds. "
        "OOF GBT abs-error rare-element tail vs common-element body; control = common split-half."
    ),
}


def _proxy_state() -> str:
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    on = {k: os.environ.get(k) for k in keys if os.environ.get(k)}
    return f"PROXY ON via {on}" if on else "DIRECT (no proxy env set)"


async def main() -> int:
    print("=" * 78)
    print("① CROSS-VENDOR DEMONSTRATION-AUDIT SMOKE TEST")
    print("=" * 78)
    print(f"egress      : {_proxy_state()}")

    create_all()
    run_id = create_run("audit-smoke", domain="materials", status="scoping")
    settings = get_settings()
    floor = int(getattr(settings, "min_review_vendors", 2))
    gateway = CriticGateway()

    # who is enabled (minus the author), who actually has usable credentials, who gets dropped
    author = {"anthropic"}
    enabled = [c.id for c in settings.critics.active if c.id not in author]
    attempted = [p.critic_id for p in gateway._providers(exclude_vendors=author)]
    skipped_no_creds = sorted(set(enabled) - set(attempted))
    print(f"author vendor (excluded): {sorted(author)}")
    print(f"enabled non-author vendors: {enabled}")
    print(f"will attempt (have creds) : {attempted}")
    if skipped_no_creds:
        print(f"skipped (no creds/impl)   : {skipped_no_creds}")
    print(f"vendor floor (min_review_vendors): {floor}")
    print("-" * 78)

    # collect critic_vendor_error events to explain any drops
    drops: list[dict] = []

    async def _collect() -> None:
        async for ev in get_bus().subscribe():
            if ev.get("type") == "critic_vendor_error":
                drops.append(ev.get("payload", {}))

    collector = asyncio.create_task(_collect())

    t0 = time.time()
    panel = await gateway.review(
        "demonstration_audit", _PAYLOAD, target_ref="audit-smoke",  # target_ref column is VARCHAR(32)
        run_id=run_id, dry_run=False, mode="paradigm", exclude_vendors=author,
    )
    dt = time.time() - t0
    await asyncio.sleep(0.2)  # let any trailing vendor-error events drain
    collector.cancel()

    responded = sorted({c.critic_id for c in (panel.critiques or [])})
    errored = sorted(set(attempted) - set(responded))
    print(f"elapsed: {dt:.1f}s")
    print("-" * 78)
    print("PER-VENDOR REVIEWS:")
    for c in panel.critiques or []:
        nf = len(c.findings or [])
        print(f"  ✓ {c.critic_id:<10} [{c.stance:<11}] {c.verdict:<20} "
              f"conf={c.confidence:.2f} findings={nf} :: {(c.summary or '')[:80]}")
    if "system" in responded:
        print("  ⚠ 'system' present → fail-closed _blocked_panel (no real reviewer ran)")
    for d in drops:
        print(f"  ✗ {d.get('vendor','?'):<10} [{d.get('stance','?'):<11}] DROPPED :: {d.get('error','')[:90]}")
    for v in errored:
        if v not in {d.get("vendor") for d in drops}:
            print(f"  ✗ {v:<10} dropped (no review, no error event captured)")

    distinct_real = sorted(set(responded) - {"system"})
    n_real = len(distinct_real)
    print("-" * 78)
    print(f"distinct REAL auditors: {n_real} ({distinct_real})  vs floor {floor}")
    print(f"consensus verdict     : {panel.consensus_verdict}   gate_passed={panel.gate_passed}")
    print("=" * 78)

    floor_ok = n_real >= floor
    if floor_ok:
        print(f"✅ SMOKE PASS — {n_real} >= {floor} distinct non-author auditors reachable on this "
              f"egress. The FULL audit's vendor floor CAN be met here.")
        if panel.gate_passed:
            print("   (secondary) the clean demo also PASSED the audit — the gate works end to end.")
        else:
            print(f"   (secondary) reachable, but the panel REJECTED ({panel.consensus_verdict}); "
                  f"inspect findings above — the demo or its framing drew a real objection.")
    else:
        print(f"❌ SMOKE FAIL — only {n_real} distinct auditor(s) reachable (< {floor}). On THIS egress "
              f"the FULL audit would starve and fail-closed, exactly as in run 160232.")
        print("   → try the other egress (FlClash on/off), or confirm deepseek + zhipu both respond.")
    print("=" * 78)

    # machine-readable line for logs/automation
    print("SMOKE_RESULT " + json.dumps({
        "egress": _proxy_state(), "floor": floor, "attempted": attempted,
        "responded": responded, "distinct_real": distinct_real, "n_real": n_real,
        "errored": errored, "skipped_no_creds": skipped_no_creds,
        "consensus": panel.consensus_verdict, "gate_passed": panel.gate_passed,
        "floor_ok": floor_ok, "elapsed_s": round(dt, 1),
    }))
    return 0 if floor_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
