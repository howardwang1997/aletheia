"""EFFECT-HOLD probe — find a discriminating statistic that actually HOLDS on real
matbench_expt_gap + Magpie, BEFORE spending novelty-probe calls or a live FULL on it.

Lesson from delta_e_feasibility_probe.py: a NOVEL estimand that is NULL on the data (delta_E) is as
useless as a REAL one that is not novel (raw rarity -> error). So gate on HOLD first (cheap,
in-process, Claude-free), THEN take a holder to the direction_framing_probe for novelty.

What we know: prediction error IS elevated where training support is thin (run 160232 held; the AD
effect is real but not novel). The candidate one level up — likely BOTH real and novel — is about the
model's UNCERTAINTY: does its own epistemic uncertainty TRACK that elevated error, or is the model
systematically OVERCONFIDENT exactly where it is wrong? We decompose, each stratified by training
support with a PERMUTED-support negative control (the structure that held at 160232):

  s1 error_vs_support      : median|err| sparse - dense                 (sanity; should HOLD)
  s2 uncertainty_vs_support: median u sparse - dense                    (does UQ rise with sparsity?)
  s3 overconfidence        : median(|err| - u) sparse - dense           (NOVEL: error rises MORE than u)
  s4 error_over_uncert     : median(|err|/u) sparse - dense             (NOVEL: calibration ratio)

A statistic HOLDS if |test| is materially > |control| (permuted support) in the expected direction.

    conda run -n aletheia python scripts/effect_hold_probe.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

_DATA_SPEC = {"source": "benchmark", "ref": "matbench_expt_gap", "target_column": "gap expt"}
SEED = 0


def main() -> int:
    print("=" * 84)
    print("EFFECT-HOLD PROBE  (real matbench_expt_gap + Magpie; Claude-free)")
    print("=" * 84)
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

    # model + epistemic uncertainty (per-tree prediction std)
    rf = RandomForestRegressor(n_estimators=200, max_depth=None, n_jobs=1, random_state=SEED)
    rf.fit(X[tr], y[tr])
    per_tree = np.stack([t.predict(X[ho]) for t in rf.estimators_], axis=0)  # (T, n_ho)
    pred = per_tree.mean(axis=0)
    u = per_tree.std(axis=0) + 1e-9                       # epistemic uncertainty
    err = np.abs(pred - y[ho])                            # realized error
    print(f"staged n={n}, d={X.shape[1]}; holdout MAE={err.mean():.4f} eV, "
          f"mean u={u.mean():.4f}  ({time.time()-t0:.1f}s)")

    # training-support measure: mean distance to k nearest TRAIN neighbors (large = sparse/far)
    nn = NearestNeighbors(n_neighbors=10).fit(Xtr)
    dist, _ = nn.kneighbors(Xho)
    support = dist.mean(axis=1)                           # higher = thinner support
    q_lo, q_hi = np.quantile(support, [1 / 3, 2 / 3])
    sparse = support >= q_hi                              # top tercile = sparse
    dense = support <= q_lo                               # bottom tercile = dense
    print(f"support strata: sparse(n={sparse.sum()})  dense(n={dense.sum()})")

    rng = np.random.default_rng(SEED)

    def strat_gap(values, mask_sparse, mask_dense):
        return float(np.median(values[mask_sparse]) - np.median(values[mask_dense]))

    def permuted_control(values, n_perm=200):
        # shuffle which points are "sparse"/"dense" (same sizes) -> the gap should vanish
        idx = np.arange(len(values))
        gaps = []
        ns, nd = int(sparse.sum()), int(dense.sum())
        for _ in range(n_perm):
            perm = rng.permutation(idx)
            ms = np.zeros(len(values), bool); ms[perm[:ns]] = True
            md = np.zeros(len(values), bool); md[perm[ns:ns + nd]] = True
            gaps.append(strat_gap(values, ms, md))
        return float(np.median(np.abs(gaps))), float(np.quantile(np.abs(gaps), 0.95))

    cands = {
        "s1 error_vs_support": err,
        "s2 uncertainty_vs_support": u,
        "s3 overconfidence (err-u)": err - u,
        "s4 error_over_uncert (err/u)": err / u,
    }
    print("-" * 84)
    print(f"{'statistic':<30} {'test':>10} {'ctrl_med':>10} {'ctrl_p95':>10} {'holds?':>8}")
    rows = []
    for name, vals in cands.items():
        test = strat_gap(vals, sparse, dense)
        cmed, cp95 = permuted_control(vals)
        holds = abs(test) > max(cp95, 1e-9) and test > 0   # beats the permuted 95% AND right sign
        rows.append({"stat": name, "test": round(test, 5), "ctrl_med": round(cmed, 5),
                     "ctrl_p95": round(cp95, 5), "holds": holds})
        print(f"{name:<30} {test:>10.4f} {cmed:>10.4f} {cp95:>10.4f} {('HOLD' if holds else '-'):>8}")

    print("-" * 84)
    novel_holders = [r for r in rows if r["holds"] and r["stat"].startswith(("s3", "s4"))]
    sanity = next(r for r in rows if r["stat"].startswith("s1"))
    print(f"sanity (error rises in sparse regions): {'YES' if sanity['holds'] else 'NO'} "
          f"(test={sanity['test']})")
    print(f"NOVEL holders (overconfidence / calibration ratio): {[r['stat'] for r in novel_holders]}")
    print("=" * 84)
    if novel_holders:
        print("✅ A novel-AND-real candidate HOLDS: the model is OVERCONFIDENT in sparse composition "
              "regions (error rises MORE than its uncertainty). Take it to direction_framing_probe.py "
              "for the novelty gate, then port if it clears.")
        verdict = 0
    elif sanity["holds"]:
        print("🟡 The error-vs-support effect is real (sanity holds) but the UNCERTAINTY-based novel "
              "statistics did not separate from the permuted control. Try a different novel object "
              "(e.g. signed-bias / regression-to-mean, or a sharper UQ).")
        verdict = 2
    else:
        print("🔴 Even the sanity effect did not hold under this split/strata — re-check the support "
              "measure before trusting the candidates.")
        verdict = 1
    print(f"elapsed {time.time()-t0:.1f}s")
    print("HOLD_JSON " + json.dumps({"rows": rows, "sanity_holds": sanity["holds"],
                                     "novel_holders": [r["stat"] for r in novel_holders]}, default=str))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
