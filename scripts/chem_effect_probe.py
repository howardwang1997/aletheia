"""CHEMISTRY-EFFECT hold-probe — test the physics-grounded hypotheses grok ideated, where the
DIFFICULTY STRATA are defined from CHEMISTRY (element identity / electronegativity / valence), NOT
from the target. This fixes the circularity that sank the cliff effect, and the mechanisms are
specific physics (novelty), so a holder is a strong novel-AND-holds candidate.

Hypotheses (each: chemistry-defined strata + a permuted-strata control that should give ~0):
  H1 pnictide_valence  (Tc)     : Fe + pnictogen(As/P/Sb), HIGH Magpie 'range NValence' -> SIGNED mean
                                  residual (carrier-density miscount). vs rest.
  H2 eneg_contrast     (bandgap): max pairwise Pauling electronegativity diff > 1.8 AND has
                                  halogen/chalcogen -> MAE vs the complement (ionic charge-transfer gap).
  H3 cuprate_doping    (Tc)     : Cu + O + >=2 distinct alkaline-earth (Ba/Sr/Ca/Mg) -> MAE vs rest
                                  (plane-specific hole doping Magpie can't encode).

A statistic HOLDS if |effect| beats the permuted-strata control's 95th percentile. Holders go to the
direction novelty probe; strata are chemistry-only so they should survive the circularity critique.

    conda run -n aletheia python scripts/chem_effect_probe.py
"""

from __future__ import annotations

import json
import time

import numpy as np

from aletheia.domains.materials.featurizers import magpie_features

SEED = 0
MAX_ROWS = 12000
GAP_CSV = None  # matbench_expt_gap loads from the matminer cache via the plugin path below
TC_CSV = "/Users/howardwang/Desktop/playground/aletheia/artifacts/datasets/superconduct_unique_m.csv"

_HALOGEN = {"F", "Cl", "Br", "I"}
_CHALCOGEN = {"O", "S", "Se", "Te"}
_PNICTOGEN = {"N", "P", "As", "Sb", "Bi"}
_ALK_EARTH = {"Be", "Mg", "Ca", "Sr", "Ba"}


def _load(dataset: str):
    """Return (X, y, comps, feat_names). comps = list of pymatgen Composition aligned with X."""
    import pandas as pd
    if dataset == "Tc":
        df = pd.read_csv(TC_CSV)
        comp_col, target = "material", "critical_temp"
    else:  # bandgap
        from aletheia.domains.materials.datasets import load_benchmark
        df = load_benchmark("matbench_expt_gap")
        comp_col, target = "composition", "gap expt"
    if len(df) > MAX_ROWS:
        df = df.sample(MAX_ROWS, random_state=SEED).reset_index(drop=True)
    X, names, work = magpie_features(df, composition_col=comp_col)
    y = work[target].to_numpy(dtype=float)
    comps = work["_comp_obj"].tolist()
    return np.asarray(X, dtype=float), y, comps, list(names)


def _descriptors(comps):
    """Per-composition chemistry descriptors (NOT using the target)."""
    out = {"maxEN": [], "has_hal_chal": [], "fe_pnictide": [], "cuprate_multiAE": []}
    for c in comps:
        syms = {el.symbol for el in c.elements}
        ens = [el.X for el in c.elements if el.X is not None and not np.isnan(el.X)]
        out["maxEN"].append((max(ens) - min(ens)) if len(ens) >= 2 else 0.0)
        out["has_hal_chal"].append(bool(syms & (_HALOGEN | _CHALCOGEN)))
        out["fe_pnictide"].append(("Fe" in syms) and bool(syms & _PNICTOGEN))
        out["cuprate_multiAE"].append(("Cu" in syms) and ("O" in syms) and len(syms & _ALK_EARTH) >= 2)
    return {k: np.array(v) for k, v in out.items()}


def _fit_resid(X, y):
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    tr, ho = train_test_split(np.arange(len(y)), test_size=0.3, random_state=SEED)
    rf = RandomForestRegressor(n_estimators=150, n_jobs=1, random_state=SEED).fit(X[tr], y[tr])
    pred = rf.predict(X[ho])
    return ho, pred, (pred - y[ho]), np.abs(pred - y[ho])


def _perm_p95(vals, mask, signed, n_perm=400, seed=SEED):
    """95th pct of |effect| under random masks of the same size (the null)."""
    rng = np.random.default_rng(seed)
    n_sub = int(mask.sum())
    rest = vals[~mask]
    base_rest = (np.mean if signed else np.median)
    g = []
    idx = np.arange(len(vals))
    for _ in range(n_perm):
        m = np.zeros(len(vals), bool); m[rng.permutation(idx)[:n_sub]] = True
        sub = (np.mean(vals[m]) if signed else np.median(vals[m]))
        oth = (np.mean(vals[~m]) if signed else np.median(vals[~m]))
        g.append(sub - oth)
    return float(np.quantile(np.abs(g), 0.95))


def _effect(vals, mask, signed):
    sub = (np.mean(vals[mask]) if signed else np.median(vals[mask]))
    oth = (np.mean(vals[~mask]) if signed else np.median(vals[~mask]))
    return float(sub - oth)


def main() -> int:
    print("=" * 92)
    print("CHEMISTRY-EFFECT HOLD-PROBE  (grok physics hypotheses; chemistry-defined strata; Claude-free)")
    print("=" * 92)
    t0 = time.time()
    rows = []

    # ---- band gap: H2 electronegativity contrast ----
    Xg, yg, cg, ng = _load("bandgap")
    dg = _descriptors(cg)
    ho, pred, resid, err = _fit_resid(Xg, yg)
    print(f"[bandgap] n={len(yg)}, holdout MAE={err.mean():.4f}  ({time.time()-t0:.1f}s)")
    mask = (dg["maxEN"][ho] > 1.8) & dg["has_hal_chal"][ho]
    if 30 < mask.sum() < len(mask) - 30:
        eff, p95 = _effect(err, mask, False), _perm_p95(err, mask, False)
        rows.append(("H2 eneg_contrast (bandgap, MAE)", int(mask.sum()), eff, p95, abs(eff) > p95 and eff > 0))
    else:
        print(f"  H2 skipped (stratum size {int(mask.sum())}/{len(mask)})")

    # ---- Tc: H1 pnictide valence (signed) + H3 cuprate doping (MAE) ----
    Xt, yt, ct, nt = _load("Tc")
    dt = _descriptors(ct)
    # Magpie 'range NValence' column for H1's high-valence-range restriction
    vcol = next((i for i, nm in enumerate(nt) if "NValence" in nm and ("range" in nm.lower() or "Dev" in nm)), None)
    hot, predt, residt, errt = _fit_resid(Xt, yt)
    print(f"[Tc] n={len(yt)}, holdout MAE={errt.mean():.3f}; NValence-range col={'yes' if vcol is not None else 'NO'}"
          f"  ({time.time()-t0:.1f}s)")

    fe_p = dt["fe_pnictide"][hot]
    if vcol is not None and fe_p.sum() > 30:
        vr = Xt[hot][:, vcol]
        hi_v = vr >= np.quantile(vr[fe_p], 0.5)        # high valence-range WITHIN Fe-pnictides
        mask1 = fe_p & hi_v
        if 20 < mask1.sum() < len(mask1) - 20:
            eff, p95 = _effect(residt, mask1, True), _perm_p95(residt, mask1, True)
            rows.append(("H1 pnictide_valence (Tc, signed resid)", int(mask1.sum()), eff, p95, abs(eff) > p95))
    else:
        print(f"  H1 skipped (Fe-pnictides={int(fe_p.sum())}, vcol={vcol})")

    mask3 = dt["cuprate_multiAE"][hot]
    if mask3.sum() > 30:
        eff, p95 = _effect(errt, mask3, False), _perm_p95(errt, mask3, False)
        rows.append(("H3 cuprate_doping (Tc, MAE)", int(mask3.sum()), eff, p95, abs(eff) > p95 and eff > 0))
    else:
        print(f"  H3 skipped (multi-AE cuprates={int(mask3.sum())})")

    print("-" * 92)
    print(f"{'hypothesis':<42} {'n_sub':>6} {'effect':>9} {'ctrl_p95':>9} {'holds?':>7}")
    for name, nsub, eff, p95, h in rows:
        print(f"{name:<42} {nsub:>6} {eff:>9.3f} {p95:>9.3f} {('HOLD' if h else '-'):>7}")
    print("-" * 92)
    holders = [r for r in rows if r[4]]
    print(f"chemistry-grounded holders: {[r[0] for r in holders]}")
    print("=" * 92)
    if holders:
        strongest = max(holders, key=lambda r: abs(r[2]) / max(r[3], 1e-9))
        print(f"✅ A PHYSICS-grounded, NON-circular effect HOLDS: '{strongest[0]}' "
              f"(effect {strongest[2]:+.3f} vs ctrl_p95 {strongest[3]:.3f}). Strata are chemistry-only "
              "(no target leakage) -> should survive the circularity critique. Take it to "
              "direction_framing_probe.py for the novelty gate.")
        verdict = 0
    else:
        print("🔴 None of grok's physics hypotheses separated from the permuted control on these data. "
              "Re-ideate or accept the loop-demo path.")
        verdict = 1
    print(f"elapsed {time.time()-t0:.1f}s")
    print("CHEM_JSON " + json.dumps({"rows": [{"h": r[0], "n_sub": r[1], "effect": round(r[2], 4),
                                               "ctrl_p95": round(r[3], 4), "holds": r[4]} for r in rows],
                                     "holders": [r[0] for r in holders]}, default=str))
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
