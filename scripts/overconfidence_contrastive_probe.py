"""OVERCONFIDENCE contrastive probe — is s3 (support-conditional overconfidence) a REAL,
UQ-method-dependent pathology, or a by-construction artifact?

The direction probe accepted s3 through the gate but raised a sharp VALIDITY major: since RF
inter-tree variance u provably collapses at extrapolation edges and error is flat, (|err| - u) may
rise in sparse regions ALMOST BY CONSTRUCTION. To settle it, compute the SAME support-conditional
overconfidence statistic for THREE uncertainty estimators, each MEAN-CALIBRATED to the same scale
(mean u = mean |err|) so they differ ONLY in WHERE they place uncertainty across training support:

  u_rf    : RF inter-tree std            (the held candidate; collapses in sparse regions)
  u_flat  : constant width (conformal-style, marginally calibrated)  -- the null
  u_dist  : distance-aware (mean k-NN distance to train; INFLATES at boundaries) -- the "correct" UQ

statistic = median(|err| - u) in SPARSE tercile - DENSE tercile, with a permuted-support control.

If u_rf gives a strong POSITIVE gap (overconfident in sparse) while u_dist gives NEGATIVE (cautious)
and u_flat ~ the error gap, then s3 is NOT by-construction: it isolates a UQ-DEPENDENT pathology, and
the contribution becomes 'a support-conditional reliability diagnostic that discriminates UQ methods
-- RF variance is dangerously overconfident in sparse composition regions where a distance-aware UQ
correctly flags caution.' If all three behave alike, s3 is mechanical -> not worth a FULL.

    conda run -n aletheia python scripts/overconfidence_contrastive_probe.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

_DATA_SPEC = {"source": "benchmark", "ref": "matbench_expt_gap", "target_column": "gap expt"}
SEED = 0


def main() -> int:
    print("=" * 86)
    print("OVERCONFIDENCE CONTRASTIVE PROBE  (RF vs flat vs distance-aware UQ; Claude-free)")
    print("=" * 86)
    t0 = time.time()
    plugin = MaterialsBandGapPlugin()
    df = plugin.load_data(_DATA_SPEC)
    X, y_ser, _names, _groups = plugin.featurize(df, {})
    X = np.asarray(X, dtype=float)
    y = np.asarray(y_ser, dtype=float)
    n = len(y)

    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler

    tr, ho = train_test_split(np.arange(n), test_size=0.3, random_state=SEED)
    sc = StandardScaler().fit(X[tr])
    Xtr, Xho = sc.transform(X[tr]), sc.transform(X[ho])

    rf = RandomForestRegressor(n_estimators=200, n_jobs=1, random_state=SEED).fit(X[tr], y[tr])
    per_tree = np.stack([t.predict(X[ho]) for t in rf.estimators_], axis=0)
    pred = per_tree.mean(axis=0)
    err = np.abs(pred - y[ho])

    # support measure: mean distance to k nearest TRAIN neighbors (large = sparse)
    nn = NearestNeighbors(n_neighbors=10).fit(Xtr)
    dist, _ = nn.kneighbors(Xho)
    support = dist.mean(axis=1)
    q_lo, q_hi = np.quantile(support, [1 / 3, 2 / 3])
    sparse, dense = support >= q_hi, support <= q_lo

    # three uncertainty estimators, then MEAN-CALIBRATE each so mean(u) == mean(err)
    def calibrate(u):
        u = np.asarray(u, dtype=float)
        return u * (err.mean() / (u.mean() + 1e-12))

    u_rf = calibrate(per_tree.std(axis=0))               # collapses in sparse (the candidate)
    u_flat = calibrate(np.ones(len(err)))                # constant width (conformal-style null)
    u_dist = calibrate(support)                          # inflates at boundaries (correct UQ)
    uqs = {"u_rf (inter-tree std)": u_rf, "u_flat (conformal-style)": u_flat,
           "u_dist (distance-aware)": u_dist}

    rng = np.random.default_rng(SEED)
    ns, nd = int(sparse.sum()), int(dense.sum())

    def gap(vals, ms, md):
        return float(np.median(vals[ms]) - np.median(vals[md]))

    def perm_p95(vals, n_perm=300):
        idx = np.arange(len(vals)); g = []
        for _ in range(n_perm):
            p = rng.permutation(idx)
            ms = np.zeros(len(vals), bool); ms[p[:ns]] = True
            md = np.zeros(len(vals), bool); md[p[ns:ns + nd]] = True
            g.append(gap(vals, ms, md))
        return float(np.quantile(np.abs(g), 0.95))

    print(f"staged n={n}; holdout MAE={err.mean():.4f}; sparse n={ns}, dense n={nd}  "
          f"({time.time()-t0:.1f}s)")
    print(f"raw error sparse-dense gap (s1): {gap(err, sparse, dense):+.4f} "
          f"(AD intuition would be positive; here it is not)")
    print("-" * 86)
    print(f"{'UQ method':<26} {'(err-u) gap':>12} {'ctrl_p95':>10} {'verdict':>16}")
    rows = []
    for name, u in uqs.items():
        d = err - u
        g = gap(d, sparse, dense)
        p95 = perm_p95(d)
        if g > p95:
            v = "OVERconfident"
        elif g < -p95:
            v = "UNDERconfident"
        else:
            v = "~calibrated"
        rows.append({"uq": name, "gap": round(g, 4), "ctrl_p95": round(p95, 4), "verdict": v})
        print(f"{name:<26} {g:>+12.4f} {p95:>10.4f} {v:>16}")

    print("-" * 86)
    rf_row = next(r for r in rows if r["uq"].startswith("u_rf"))
    dist_row = next(r for r in rows if r["uq"].startswith("u_dist"))
    raw_err_gap = gap(err, sparse, dense)
    # the contrast 'discriminates' if RF reads overconfident while a boundary-aware UQ does not...
    discriminates = (rf_row["verdict"] == "OVERconfident"
                     and dist_row["verdict"] in ("UNDERconfident", "~calibrated")
                     and rf_row["gap"] - dist_row["gap"] > max(rf_row["ctrl_p95"], dist_row["ctrl_p95"]))
    # ...BUT 'overconfidence' is only a real RELIABILITY danger if error is actually HIGHER in sparse.
    # If error is LOWER there (raw_err_gap <= 0), the model is more confident where it is more accurate
    # -> the (err-u) gap is a relative-calibration residual, not a pathology. Gate on the science.
    danger = raw_err_gap > 0
    print("=" * 86)
    if discriminates and danger:
        print("✅ A REAL, UQ-dependent overconfidence: error is HIGHER in sparse AND RF uncertainty "
              "collapses there while a boundary-aware UQ flags caution. Defensible diagnostic -> "
              "re-frame + re-novelty-probe, then port.")
        verdict = 0
    elif discriminates and not danger:
        print("🟡 MECHANICAL, not a reliability danger. The RF-vs-distance contrast separates, BUT error "
              f"is LOWER in sparse regions (raw error gap {raw_err_gap:+.3f}). RF being more confident "
              "where it is MORE accurate is appropriate, not pathological; the (err-u) gap is a "
              "relative-calibration residual. The distance-aware/AD UQ is the one that is WRONG here "
              "(overcautious). s3 is NOT a robust 'overconfidence' pathology -> do NOT port it to a "
              "FULL. On this data k-NN support simply does not predict error.")
        verdict = 2
    else:
        print("🔴 The contrast did NOT separate -> s3 is likely the by-construction artifact the critic "
              "warned about; NOT worth a FULL.")
        verdict = 1
    print(f"elapsed {time.time()-t0:.1f}s")
    print("CONTRAST_JSON " + json.dumps({
        "raw_error_gap": round(gap(err, sparse, dense), 4), "rows": rows,
        "discriminates": discriminates}, default=str))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
