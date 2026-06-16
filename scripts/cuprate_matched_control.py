"""CUPRATE matched-control verification — does the cuprate plane-doping effect (H3) survive a
COMPLEXITY-MATCHED control, or is it just 'complex/rare compositions are harder' (the AD-confound)?

H3 held at 11x a permuted-strata null and PASSED the novelty gate with no objection. The one open
validity risk the critics named: multi-alkaline-earth cuprates may simply be more complex/rare. This
verifies the framing's committed safeguard: compare cuprate MAE to a non-cuprate control MATCHED on
composition complexity (element count) AND feature-space density. If cuprates stay significantly
harder than the matched control, the failure is cuprate-physics-specific, not generic complexity.

    conda run -n aletheia python scripts/cuprate_matched_control.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.featurizers import magpie_features

TC_CSV = "/Users/howardwang/Desktop/playground/aletheia/artifacts/datasets/superconduct_unique_m.csv"
SEED = 0
MAX_ROWS = 12000
_ALK_EARTH = {"Be", "Mg", "Ca", "Sr", "Ba"}


def main() -> int:
    print("=" * 86)
    print("CUPRATE MATCHED-CONTROL VERIFICATION  (does H3 survive complexity matching?)")
    print("=" * 86)
    t0 = time.time()
    import pandas as pd
    df = pd.read_csv(TC_CSV)
    if len(df) > MAX_ROWS:
        df = df.sample(MAX_ROWS, random_state=SEED).reset_index(drop=True)
    X, names, work = magpie_features(df, composition_col="material")
    X = np.asarray(X, dtype=float)
    y = work["critical_temp"].to_numpy(dtype=float)
    comps = work["_comp_obj"].tolist()
    n_el = np.array([len(c.elements) for c in comps])
    cuprate = np.array([("Cu" in {e.symbol for e in c.elements})
                        and ("O" in {e.symbol for e in c.elements})
                        and len({e.symbol for e in c.elements} & _ALK_EARTH) >= 2 for c in comps])

    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler

    tr, ho = train_test_split(np.arange(len(y)), test_size=0.3, random_state=SEED)
    sc = StandardScaler().fit(X[tr])
    rf = RandomForestRegressor(n_estimators=150, n_jobs=1, random_state=SEED).fit(X[tr], y[tr])
    err = np.abs(rf.predict(X[ho]) - y[ho])

    # complexity coordinates on the HOLDOUT: element count + feature-space density (mean kNN dist to train)
    nn = NearestNeighbors(n_neighbors=10).fit(sc.transform(X[tr]))
    dens = nn.kneighbors(sc.transform(X[ho]))[0].mean(axis=1)
    cup_ho = cuprate[ho]
    coord = np.column_stack([n_el[ho], dens])
    coord = (coord - coord.mean(0)) / (coord.std(0) + 1e-9)   # standardize the matching space

    cup_idx = np.where(cup_ho)[0]
    non_idx = np.where(~cup_ho)[0]
    print(f"holdout: {len(cup_idx)} multi-AE cuprates, {len(non_idx)} non-cuprates; "
          f"MAE all={err.mean():.3f}  ({time.time()-t0:.1f}s)")
    print(f"cuprate complexity: median n_elements={np.median(n_el[ho][cup_ho]):.0f} vs "
          f"non-cuprate {np.median(n_el[ho][~cup_ho]):.0f}")

    # greedy nearest-neighbor matching (without replacement) of each cuprate to a non-cuprate in coord
    rng = np.random.default_rng(SEED)
    nn_match = NearestNeighbors(n_neighbors=min(20, len(non_idx))).fit(coord[non_idx])
    order = cup_idx[rng.permutation(len(cup_idx))]
    used = set()
    matched = []
    cand = nn_match.kneighbors(coord[order])[1]
    for row, neigh in zip(order, cand):
        for j in neigh:
            gi = non_idx[j]
            if gi not in used:
                used.add(gi); matched.append(gi); break
    matched = np.array(matched)

    mae_cup = float(err[cup_idx].mean())
    mae_matched = float(err[matched].mean())
    mae_nonall = float(err[non_idx].mean())
    print("-" * 86)
    print(f"MAE multi-AE cuprates           : {mae_cup:.3f}")
    print(f"MAE complexity-matched control  : {mae_matched:.3f}   (matched on n_elements + density)")
    print(f"MAE all non-cuprates            : {mae_nonall:.3f}")
    print(f"excess over matched control     : {mae_cup - mae_matched:+.3f}")

    # bootstrap CI on (cuprate - matched) to test significance
    boot = []
    for _ in range(2000):
        bc = rng.choice(cup_idx, len(cup_idx), replace=True)
        bm = rng.choice(matched, len(matched), replace=True)
        boot.append(err[bc].mean() - err[bm].mean())
    lo, hi = np.quantile(boot, [0.025, 0.975])
    survives = lo > 0
    print(f"bootstrap 95% CI of excess      : [{lo:+.3f}, {hi:+.3f}]")
    print("=" * 86)
    if survives:
        print(f"✅ SURVIVES complexity matching: cuprates are {mae_cup-mae_matched:+.2f} K harder than "
              "non-cuprates MATCHED on element count + feature density, CI excludes 0. The failure is "
              "cuprate-PHYSICS-specific, NOT generic complexity/AD. H3 is a verified novel-AND-holds "
              "candidate -> ready to port into the K2 plan.")
        verdict = 0
    else:
        print(f"🟡 Does NOT clearly survive matching (CI includes 0): the cuprate excess is largely "
              "explained by composition complexity / density (the AD-confound). The effect is real but "
              "not cleanly cuprate-specific; sharpen the stratum or reconsider.")
        verdict = 2
    print(f"elapsed {time.time()-t0:.1f}s")
    print("MATCH_JSON " + json.dumps({"mae_cuprate": round(mae_cup, 3), "mae_matched": round(mae_matched, 3),
                                      "mae_non_all": round(mae_nonall, 3),
                                      "excess_over_matched": round(mae_cup - mae_matched, 3),
                                      "ci": [round(lo, 3), round(hi, 3)], "survives": survives,
                                      "n_cuprate": len(cup_idx)}, default=str))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
