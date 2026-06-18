"""delta_E FEASIBILITY probe — does the reframed estimand actually HOLD on real data, and is the
demonstration authorable + runnable, BEFORE committing a ~700k-token live FULL to it?

Why this exists
---------------
The direction-framing probe showed the delta_E reframe clears the NOVELTY gate. But novelty is not
enough: the OLD bounded-extrapolation hypothesis was genuinely NULL on this data (test stat ~0) and
we only learned that after burning runs. So before a live FULL, answer the EXISTENTIAL question for
cents: on real matbench_expt_gap + Magpie, is delta_E (support-and-topology-matched counterfactual
removal, on an E-free holdout) actually POSITIVE and heterogeneous across elements, with the
matched-vs-matched control ~ 0? And can a reference compute_demonstration be authored that (a) passes
the ② pre-flight non-degeneracy validator and (b) runs through the REAL harness path
(_compute_ai_authored_demonstration: sandbox + probes + the pre-registered rule) to a verdict?

Claude-free (no LLM authoring/critics) -> safe to run in-process.

    conda run -n aletheia python scripts/delta_e_feasibility_probe.py

GREEN (effect real + demo runs) -> a live FULL is a good-odds roll. NULL (delta_E ~ 0 for all
elements) -> reframe again, cheaply, instead of burning the window.
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.coder.sandbox import smoke_test_demonstration
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

# matbench_expt_gap data spec (same as the e2e).
_DATA_SPEC = {"source": "benchmark", "ref": "matbench_expt_gap", "target_column": "gap expt"}

# Reference compute_demonstration for ONE deterministically-chosen element. Sandbox-safe imports
# only (numpy, sklearn, collections). Mirrors the in-process scan's logic so the harness run and the
# scan measure the same estimand. This is the shape the AI author must produce.
_DELTA_E_DEMO_CODE = '''\
def compute_demonstration(X, y, groups, meta):
    import numpy as np
    from collections import Counter
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split

    seed = int(meta.get("random_state", 0))
    X = np.asarray(X, dtype=float); y = np.asarray(y, dtype=float)
    sysarr = np.asarray(groups, dtype=object)
    elems = [set(str(s).split("-")) for s in sysarr]
    n = len(y)
    idx = np.arange(n)
    tr_idx, ho_idx = train_test_split(idx, test_size=0.3, random_state=seed)
    Xs = StandardScaler().fit(X[tr_idx]).transform(X)

    def fit_mae(train_idx, eval_idx):
        m = GradientBoostingRegressor(n_estimators=80, max_depth=3, random_state=0)
        m.fit(X[train_idx], y[train_idx])
        return float(np.mean(np.abs(m.predict(X[eval_idx]) - y[eval_idx])))

    # deterministic target element: most-supported element with train support in [4%, 18%]
    cnt = Counter()
    for i in tr_idx:
        for e in elems[i]:
            cnt[e] += 1
    band = [(e, c) for e, c in cnt.items() if 0.04 * len(tr_idx) <= c <= 0.18 * len(tr_idx)]
    band.sort(key=lambda kv: (-kv[1], kv[0]))
    E = band[0][0] if band else max(cnt, key=lambda e: cnt[e])

    hasE = np.array([E in elems[i] for i in range(n)])
    ho_free = ho_idx[~hasE[ho_idx]]                 # E-free holdout: transfer to OTHER chemistries
    tr_noE = tr_idx[~hasE[tr_idx]]
    n_removed = int(len(tr_idx) - len(tr_noE))
    base = fit_mae(tr_idx, ho_free)
    rise_E = fit_mae(tr_noE, ho_free) - base

    # topology-matched control: remove ONE size-matched k-means cluster (a coherent compositional
    # hole with the SAME topology as carving out an element), measured on the SAME E-free holdout.
    k = max(2, round(len(tr_idx) / max(1, n_removed)))
    lab = KMeans(n_clusters=k, n_init=3, random_state=seed).fit_predict(Xs[tr_idx])
    sizes = Counter(lab)
    order = sorted(sizes, key=lambda c: abs(sizes[c] - n_removed))
    def rm(cl):
        return fit_mae(tr_idx[lab != cl], ho_free) - base
    rise_cl = rm(order[0])
    delta_E = rise_E - rise_cl
    ctrl = abs(rm(order[1]) - rm(order[2])) if len(order) >= 3 else 0.0
    return {
        "test_statistic": float(delta_E),
        "control_statistic": float(ctrl),
        "components": {"element": E, "baseline_mae": base, "rise_E": float(rise_E),
                       "rise_cluster": float(rise_cl), "n_removed": n_removed},
        "detail": ("delta_E for %s: rise_E=%.4f - rise_cluster=%.4f on E-free holdout (n=%d); "
                   "control |riseB-riseC|=%.4f" % (E, rise_E, rise_cl, len(ho_free), ctrl)),
        "n_test": int(len(ho_free)),
        "n_control": int(len(ho_free)),
    }
'''

# Pre-registered decision rule (thresholds in eV; band-gap MAE ~ 0.4 eV). The MEASURED magnitudes
# (reported below) are the real signal; this rule is what the harness run applies to derive holds.
_PREREG = {
    "statistic_name": "delta_E (support-and-topology-matched counterfactual removal)",
    "computation": "MAE rise on E-free holdout from removing element E's train compounds minus the "
                   "rise from removing one size-matched k-means cluster",
    "control_description": "matched-vs-matched: |rise(remove cluster B) - rise(remove cluster C)| "
                           "for two other size-matched clusters",
    "expected_control": "two matched cluster removals are exchangeable, so their rise difference ~ 0 "
                        "if the delta_E gap is genuinely element-specific",
    "supported_if": {"op": ">=", "threshold": 0.02},
    "control_silent_if": {"op": "<=", "threshold": 0.02},
}


def _delta_for_element(X, Xs, y, elems, tr_idx, ho_idx, E, n_est, seed):
    """In-process delta_E for one element (the existential scan). Returns None if under-supported."""
    from collections import Counter

    from sklearn.cluster import KMeans
    from sklearn.ensemble import GradientBoostingRegressor

    n = len(y)
    hasE = np.array([E in elems[i] for i in range(n)])
    ho_free = ho_idx[~hasE[ho_idx]]
    tr_noE = tr_idx[~hasE[tr_idx]]
    n_removed = int(len(tr_idx) - len(tr_noE))
    if len(ho_free) < 30 or n_removed < 15:
        return None

    def fit_mae(tr, ev):
        m = GradientBoostingRegressor(n_estimators=n_est, max_depth=3, random_state=0)
        m.fit(X[tr], y[tr])
        return float(np.mean(np.abs(m.predict(X[ev]) - y[ev])))

    base = fit_mae(tr_idx, ho_free)
    rise_E = fit_mae(tr_noE, ho_free) - base
    k = max(2, round(len(tr_idx) / max(1, n_removed)))
    lab = KMeans(n_clusters=k, n_init=3, random_state=seed).fit_predict(Xs[tr_idx])
    sizes = Counter(lab)
    order = sorted(sizes, key=lambda c: abs(sizes[c] - n_removed))
    rise_cl = fit_mae(tr_idx[lab != order[0]], ho_free) - base
    if len(order) >= 3:
        ctrl = abs((fit_mae(tr_idx[lab != order[1]], ho_free) - base)
                   - (fit_mae(tr_idx[lab != order[2]], ho_free) - base))
    else:
        ctrl = 0.0
    return {"E": E, "delta_E": rise_E - rise_cl, "control": ctrl, "rise_E": rise_E,
            "rise_cluster": rise_cl, "base": base, "n_removed": n_removed, "n_hofree": int(len(ho_free))}


def main() -> int:
    print("=" * 84)
    print("delta_E FEASIBILITY PROBE  (real matbench_expt_gap + Magpie; Claude-free)")
    print("=" * 84)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = plugin.load_data(_DATA_SPEC)
    X, y_ser, _names, groups = plugin.featurize(df, {})
    X = np.asarray(X, dtype=float)
    y = np.asarray(y_ser, dtype=float)
    sysarr = np.asarray(groups, dtype=object)
    elems = [set(str(s).split("-")) for s in sysarr]
    n = len(y)
    print(f"staged: n={n} rows, d={X.shape[1]} Magpie features, "
          f"{len(set(sysarr))} chemical systems  ({time.time()-t0:.1f}s)")

    # ---- 1) EXISTENTIAL SCAN: delta_E across candidate elements -------------------------------
    from collections import Counter

    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler

    seed = 0
    idx = np.arange(n)
    tr_idx, ho_idx = train_test_split(idx, test_size=0.3, random_state=seed)
    Xs = StandardScaler().fit(X[tr_idx]).transform(X)
    tr_cnt = Counter()
    for i in tr_idx:
        for e in elems[i]:
            tr_cnt[e] += 1
    cand = sorted([e for e, c in tr_cnt.items() if 0.04 * len(tr_idx) <= c <= 0.18 * len(tr_idx)],
                  key=lambda e: (-tr_cnt[e], e))[:10]
    print(f"\nscanning {len(cand)} candidate elements (train support 4-18%): {cand}")
    print("-" * 84)
    print(f"{'E':<4} {'delta_E':>9} {'control':>9} {'rise_E':>9} {'rise_clus':>9} {'n_rm':>6} {'n_hofree':>9}")
    scan: list[dict] = []
    for E in cand:
        r = _delta_for_element(X, Xs, y, elems, tr_idx, ho_idx, E, n_est=60, seed=seed)
        if r is None:
            continue
        scan.append(r)
        print(f"{E:<4} {r['delta_E']:>9.4f} {r['control']:>9.4f} {r['rise_E']:>9.4f} "
              f"{r['rise_cluster']:>9.4f} {r['n_removed']:>6d} {r['n_hofree']:>9d}")

    pos = [r for r in scan if r["delta_E"] > 0.01 and r["control"] < 0.02]
    deltas = sorted((r["delta_E"] for r in scan), reverse=True)
    print("-" * 84)
    print(f"elements with delta_E > 0.01 eV AND control < 0.02 eV: {len(pos)}/{len(scan)}  "
          f"-> {[r['E'] for r in pos]}")
    print(f"delta_E range: [{min(deltas):.4f}, {max(deltas):.4f}] eV   "
          f"median={float(np.median(deltas)):.4f}")
    effect_real = len(pos) >= 2  # heterogeneous, real (not a single cherry-pick, not all-null)

    # ---- 2) ② PRE-FLIGHT: does the reference demo pass the non-degeneracy validator? -----------
    print("\n" + "-" * 84)
    print("② pre-flight (smoke_test_demonstration) on the reference compute_demonstration:")
    ok, err = smoke_test_demonstration(_DELTA_E_DEMO_CODE)
    print(f"  pre-flight {'PASS' if ok else 'FAIL: ' + err}")

    # ---- 3) REAL HARNESS PATH: run it through _compute_ai_authored_demonstration --------------
    print("\n" + "-" * 84)
    print("real harness run (_compute_ai_authored_demonstration: sandbox + probes + rule):")
    demo = {"demonstration_code": _DELTA_E_DEMO_CODE, "preregistration": _PREREG,
            "form": "discriminating_instance", "random_state": seed}
    res = plugin._compute_ai_authored_demonstration(demo, _DATA_SPEC)
    if res is None:
        print("  -> None (not_evaluated: no usable code/rule)")
        holds = None
    else:
        holds = res.get("holds")
        print(f"  test_statistic (delta_E) : {res.get('test_statistic')}")
        print(f"  control_statistic        : {res.get('control_statistic')}")
        print(f"  test_triggers (>= {_PREREG['supported_if']['threshold']}) : {res.get('test_triggers')}")
        print(f"  control_silent (<= {_PREREG['control_silent_if']['threshold']}): {res.get('control_silent')}")
        probes = res.get("probes") or {}
        print(f"  probes clean             : {probes.get('clean')}  ({probes.get('note','')[:80]})")
        print(f"  HOLDS                    : {holds}")
        print(f"  detail: {str(res.get('detail',''))[:160]}")

    # ---- verdict --------------------------------------------------------------------------------
    print("=" * 84)
    green = bool(effect_real and ok and holds)
    if green:
        print("✅ GREEN — delta_E HOLDS on real data (heterogeneous, control silent), the reference "
              "demo passes the ② pre-flight AND runs through the real harness path to holds=True. The "
              "estimand is real and authorable -> a live FULL is a good-odds roll.")
    elif effect_real:
        print("🟡 PARTIAL — the EFFECT is real on real data (delta_E > 0 for several elements, control "
              "~ 0), but the reference demo did not cleanly hold via the harness (pre-flight or "
              "rule/probe). The science is there; tighten the reference demo before a live FULL.")
    else:
        print("🔴 NULL — delta_E is ~ 0 across candidate elements (or controls not silent): on THIS "
              "data the estimand does not discriminate. Do NOT burn a live FULL; reframe instead "
              "(as the bounded-extrapolation hypothesis was correctly found null).")
    print(f"elapsed {time.time()-t0:.1f}s")
    print("PROBE_JSON " + json.dumps({
        "n_rows": int(n), "scan": [{k: (round(v, 5) if isinstance(v, float) else v)
                                    for k, v in r.items()} for r in scan],
        "n_positive": len(pos), "effect_real": effect_real,
        "preflight_ok": bool(ok),
        "harness_holds": holds, "harness_test_stat": (res or {}).get("test_statistic"),
        "harness_control_stat": (res or {}).get("control_statistic"),
        "green": green,
    }, default=str))
    return 0 if green else (2 if effect_real else 1)


if __name__ == "__main__":
    raise SystemExit(main())
