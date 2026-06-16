"""NOVEL-EFFECT scan — hunt for a NOVEL-leaning discriminating effect that HOLDS on a fresh
composition dataset, avoiding the applicability-domain / error-vs-support family that is
novelty-poison (every AD framing gets rejected as repackaged).

Substrate: superconductivity2018 (Tc), n~16k, composition-based via Magpie. Tc is hard, has sharp
doping CLIFFS and a critical high-Tc tail -- a richer landscape than band gaps for a novel effect.

Effect types tested (each stratified, with a PERMUTED-label control that should give ~0):
  e1 target_tail_signed_bias : median residual(pred-y) high-y stratum - low-y    (regression to mean;
                               sanity -- likely HOLDS, LOW novelty)
  e2 cliff_error             : |error| on CLIFF points (feature-near a train pt but target-FAR) vs
                               SMOOTH points                                       (moderate novelty)
  e3 local_collapse_slope    : slope of residual r on (y - neighborhood_mean_y) at CLIFFS vs SMOOTH --
                               does the model COLLAPSE toward the local mean, MORE at cliffs? a novel
                               decomposition (local-collapse estimand), likely HOLDS

A holder (esp. e2/e3) then goes to direction_framing_probe.py for the novelty gate.

    conda run -n aletheia python scripts/novel_effect_scan.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

# NB: most matminer datasets (superconductivity2018, etc.) fail to download on this box -- stale
# figshare hash validation. Only matbench_expt_gap is cached, so we test the NEW effect TYPES
# (cliff-error, local-collapse) there: band gaps have compositional cliffs + a high-gap tail too, so
# this still chases novelty via the effect family rather than the dataset. Override via env if a
# download starts working.
import os as _os
DATASET = _os.environ.get("SCAN_DATASET", "matbench_expt_gap")
TARGET = _os.environ.get("SCAN_TARGET", "gap expt")
SEED = 0
MAX_ROWS = 12000   # cap for a bounded single-threaded RF fit


def main() -> int:
    print("=" * 88)
    print(f"NOVEL-EFFECT SCAN  ({DATASET} / {TARGET}; Magpie; Claude-free)")
    print("=" * 88)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = plugin.load_data({"source": "benchmark", "ref": DATASET, "target_column": TARGET})
    X, y_ser, _names, _groups = plugin.featurize(df, {})
    X = np.asarray(X, dtype=float)
    y = np.asarray(y_ser, dtype=float)
    n = len(y)
    print(f"staged n={n}, d={X.shape[1]}; y range [{y.min():.2f}, {y.max():.2f}], "
          f"mean {y.mean():.2f}  ({time.time()-t0:.1f}s)")

    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(SEED)
    if n > MAX_ROWS:
        keep = rng.choice(n, MAX_ROWS, replace=False)
        X, y = X[keep], y[keep]
        n = MAX_ROWS
    tr, ho = train_test_split(np.arange(n), test_size=0.3, random_state=SEED)
    sc = StandardScaler().fit(X[tr])
    Xtr, Xho = sc.transform(X[tr]), sc.transform(X[ho])

    rf = RandomForestRegressor(n_estimators=150, n_jobs=1, random_state=SEED).fit(X[tr], y[tr])
    pred = rf.predict(X[ho])
    resid = pred - y[ho]              # signed residual
    err = np.abs(resid)
    print(f"holdout MAE={err.mean():.3f}; fit done ({time.time()-t0:.1f}s)")

    # nearest TRAIN neighbor (feature space) for each holdout point -> cliffness + neighborhood mean
    knn = NearestNeighbors(n_neighbors=8).fit(Xtr)
    dist, nbr = knn.kneighbors(Xho)
    nn1_dist = dist[:, 0] + 1e-9
    y_nn1 = y[tr][nbr[:, 0]]
    nbhd_mean = y[tr][nbr].mean(axis=1)             # local mean of k train neighbors
    cliffness = np.abs(y[ho] - y_nn1) / nn1_dist    # near in features, far in target = cliff

    def terc(v):
        lo, hi = np.quantile(v, [1 / 3, 2 / 3])
        return v <= lo, v >= hi

    ns_lab = {}  # store masks for permutation controls

    def gap_median(vals, lo_mask, hi_mask):
        return float(np.median(vals[hi_mask]) - np.median(vals[lo_mask]))

    def perm_p95(vals, n_lo, n_hi, n_perm=300):
        idx = np.arange(len(vals)); g = []
        for _ in range(n_perm):
            p = rng.permutation(idx)
            ml = np.zeros(len(vals), bool); ml[p[:n_lo]] = True
            mh = np.zeros(len(vals), bool); mh[p[n_lo:n_lo + n_hi]] = True
            g.append(gap_median(vals, ml, mh))
        return float(np.quantile(np.abs(g), 0.95))

    def slope(a, b):  # least-squares slope of b on a
        a = a - a.mean()
        return float((a @ (b - b.mean())) / (a @ a + 1e-12))

    rows = []

    # e1 target-tail signed bias (sanity)
    lo_y, hi_y = terc(y[ho])
    g = gap_median(resid, lo_y, hi_y)
    p95 = perm_p95(resid, int(lo_y.sum()), int(hi_y.sum()))
    rows.append(("e1 target_tail_signed_bias", g, p95, abs(g) > p95))

    # e2 cliff error
    lo_c, hi_c = terc(cliffness)
    g = gap_median(err, lo_c, hi_c)
    p95 = perm_p95(err, int(lo_c.sum()), int(hi_c.sum()))
    rows.append(("e2 cliff_error", g, p95, abs(g) > p95 and g > 0))

    # e3 local-collapse slope at cliffs vs smooth: slope of resid on (y - nbhd_mean).
    # If the model predicts toward the local mean, resid = pred - y ~ nbhd_mean - y = -(y - nbhd_mean),
    # so slope ~ -1 (strong collapse). Compare cliff stratum vs smooth stratum.
    drift = y[ho] - nbhd_mean
    s_cliff = slope(drift[hi_c], resid[hi_c])
    s_smooth = slope(drift[lo_c], resid[lo_c])
    g3 = s_cliff - s_smooth
    # permuted control: shuffle which points are cliff vs smooth
    gp = []
    allmask = hi_c | lo_c
    di, ri = drift[allmask], resid[allmask]
    nc = int(hi_c.sum())
    for _ in range(300):
        p = rng.permutation(len(di))
        gp.append(slope(di[p[:nc]], ri[p[:nc]]) - slope(di[p[nc:nc + int(lo_c.sum())]], ri[p[nc:nc + int(lo_c.sum())]]))
    p95_3 = float(np.quantile(np.abs(gp), 0.95))
    rows.append(("e3 local_collapse_slope", g3, p95_3, abs(g3) > p95_3))
    extra3 = f"(slope cliff={s_cliff:+.2f} vs smooth={s_smooth:+.2f}; ~-1 = collapse to local mean)"

    print("-" * 88)
    print(f"{'effect':<30} {'test':>10} {'ctrl_p95':>10} {'holds?':>8}")
    for name, g, p95, h in rows:
        print(f"{name:<30} {g:>10.3f} {p95:>10.3f} {('HOLD' if h else '-'):>8}")
    print(f"  {extra3}")
    print("-" * 88)
    novel = [r for r in rows if r[3] and r[0].startswith(("e2", "e3"))]
    print(f"novel-leaning holders: {[r[0] for r in novel]}")
    print("=" * 88)
    if novel:
        print(f"✅ A novel-leaning effect HOLDS on {DATASET}: {[r[0] for r in novel]}. Take the strongest "
              "to direction_framing_probe.py for the novelty gate (and validate on a 2nd dataset for the "
              "scope critique) before porting.")
        verdict = 0
    else:
        print(f"🔴 Only the sanity effect (or nothing) held on {DATASET}. Try another effect type / "
              "dataset.")
        verdict = 1
    print(f"elapsed {time.time()-t0:.1f}s")
    print("SCAN_JSON " + json.dumps({"dataset": DATASET, "target": TARGET, "n": int(n),
                                     "rows": [{"effect": r[0], "test": round(r[1], 4),
                                               "ctrl_p95": round(r[2], 4), "holds": r[3]} for r in rows],
                                     "slope_cliff": round(s_cliff, 3), "slope_smooth": round(s_smooth, 3),
                                     "novel_holders": [r[0] for r in novel]}, default=str))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
