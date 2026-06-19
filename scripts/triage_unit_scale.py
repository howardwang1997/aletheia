"""Triage divergent candidate #3 — does a composition model's RELIABILITY MAP reorder under a
non-affine (physically-equivalent) re-expression of the target, while an AFFINE rescale leaves it
unchanged? If so: "reliably predicted" is an artifact of the reporting units, not the material.

Design (band gap, cached): train the SAME RF on y, on an AFFINE rescale (c*y), and on a NON-affine
monotone transform (log1p y). Per-point reliability = -|residual| (in each model's own target scale).
Compare reliability RANKINGS to the y-model:
  - affine control should preserve the ranking  (Spearman ~ 1),
  - non-affine (log) should reorder it          (Spearman < 1)  IF the claim is real.
Effect = corr(y, affine) - corr(y, log); bootstrap 95% CI. Holds if the CI excludes 0.

    conda run -n aletheia python scripts/triage_unit_scale.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.datasets import load_benchmark
from aletheia.domains.materials.featurizers import magpie_features

SEED = 0


def main() -> int:
    print("=" * 84)
    print("TRIAGE #3 — unit re-expression reorders the reliability map?  (band gap; Claude-free)")
    print("=" * 84)
    t0 = time.time()
    df = load_benchmark("matbench_expt_gap")
    X, _names, work = magpie_features(df, composition_col="composition")
    X = np.asarray(X, dtype=float)
    y = work["gap expt"].to_numpy(dtype=float)
    n = len(y)

    from scipy.stats import spearmanr
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split

    tr, ho = train_test_split(np.arange(n), test_size=0.3, random_state=SEED)

    def rel(ytr_transform, yho_transform):
        """per-point reliability (-|resid|) of an RF trained on a transformed target."""
        m = RandomForestRegressor(n_estimators=200, n_jobs=1, random_state=SEED)
        m.fit(X[tr], ytr_transform(y[tr]))
        return -np.abs(m.predict(X[ho]) - yho_transform(y[ho]))

    rel_y = rel(lambda v: v, lambda v: v)                       # native units
    rel_aff = rel(lambda v: 3.0 * v + 5.0, lambda v: 3.0 * v + 5.0)   # AFFINE rescale (control)
    rel_log = rel(lambda v: np.log1p(v), lambda v: np.log1p(v))       # NON-affine monotone

    c_aff = float(spearmanr(rel_y, rel_aff).correlation)
    c_log = float(spearmanr(rel_y, rel_log).correlation)
    effect = c_aff - c_log
    print(f"staged n={n}; holdout n={len(ho)}  ({time.time()-t0:.1f}s)")
    print(f"reliability-rank corr  affine vs native : {c_aff:+.3f}   (expect ~1: affine preserves)")
    print(f"reliability-rank corr  log    vs native : {c_log:+.3f}   (lower => reordered by units)")
    print(f"EFFECT (affine_corr - log_corr)         : {effect:+.3f}")

    # bootstrap CI on the effect (resample holdout points)
    rng = np.random.default_rng(SEED)
    boots = []
    idx = np.arange(len(ho))
    for _ in range(400):
        b = rng.choice(idx, len(idx), replace=True)
        ca = spearmanr(rel_y[b], rel_aff[b]).correlation
        cl = spearmanr(rel_y[b], rel_log[b]).correlation
        boots.append(ca - cl)
    lo, hi = np.quantile(boots, [0.025, 0.975])
    holds = lo > 0.0
    print(f"bootstrap 95% CI of effect              : [{lo:+.3f}, {hi:+.3f}]")
    print("=" * 84)
    if holds and effect > 0.05:
        print(f"✅ HOLDS — a non-affine unit change REORDERS the reliability map (effect {effect:+.3f}, "
              "CI excludes 0) while an affine rescale does not. 'Reliably predicted' is unit-dependent, "
              "not a property of the material. A novel-AND-feasible candidate -> direction novelty-gate.")
        v = 0
    else:
        print(f"🔴 Weak/NULL — the reliability map does not reorder materially under a non-affine unit "
              f"change (effect {effect:+.3f}, CI [{lo:+.3f},{hi:+.3f}]); not worth carrying forward.")
        v = 1
    print(f"elapsed {time.time()-t0:.1f}s")
    print("TRIAGE_JSON " + json.dumps({"c_affine": round(c_aff, 4), "c_log": round(c_log, 4),
                                       "effect": round(effect, 4), "ci": [round(lo, 4), round(hi, 4)],
                                       "holds": holds}, default=str))
    return v


if __name__ == "__main__":
    raise SystemExit(main())
