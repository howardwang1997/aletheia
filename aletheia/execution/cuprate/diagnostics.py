"""The ported cuprate diagnostic pipeline (SEED=0 lineage preserved).

Two analyses over the bound batch's registered rows, frozen at the source
scripts' constants: subsample ``MAX_ROWS`` via ``df.sample(random_state=
SEED)`` after the bound-batch restriction (a new lineage — the historical
full-file numbers are provenance, not expected output), Magpie
featurization, 70/30 ``train_test_split(random_state=0)`` over position
indices, ``RandomForestRegressor(n_estimators=150, n_jobs=1,
random_state=0)`` on unscaled features.

D1 (port of ``scripts/cuprate_matched_control.py``): holdout |err|,
complexity coordinates (element count + mean 10-NN distance to train in
StandardScaler-scaled space, standardized with the +1e-9 std guard), greedy
without-replacement matching of each multi-AE cuprate holdout row to a
non-cuprate in ``default_rng(0)``-permuted order, then a 2000-draw
bootstrap CI on the cuprate-minus-matched MAE from the same rng stream
(cuprate draw first, matched draw second, per iteration).  Result keys
mirror the source's MATCH_JSON line.  Outcome bin:
``family_excess_ci_excludes_zero`` iff the CI's lower bound is positive.

D2 (the permutation-null machinery of ``scripts/chem_effect_probe.py`` —
fresh ``default_rng(0)``, 400 same-size random masks, p95 over |effect| —
applied to the KEYSTONE_A round-1 observable): within the family holdout,
split rows at the median absolute doping deviation from the
protocol-pinned optimum; statistic = median err of the high-deviation
half minus median err of the low-deviation half.  Holds iff |effect| >
p95 and effect > 0, the source H3 rule.  Outcome bin:
``error_concentrates_at_extremes`` / ``no_stratification_structure``.

Floats are rounded to three decimals (D1) and four (D2), the source
scripts' JSON conventions; the exact-content catalog pins the emitted
bytes, so any surviving ULP drift fails closed at validation.
"""

from __future__ import annotations

from typing import Any

import numpy as np

SEED = 0
MAX_ROWS = 12000
N_ESTIMATORS = 150
TEST_SIZE = 0.3
N_DENSITY_NEIGHBORS = 10
MAX_MATCH_CANDIDATES = 20
STD_GUARD = 1e-9
N_BOOTSTRAP = 2000
CI_QUANTILES = (0.025, 0.975)
N_PERMUTATIONS = 400
PERM_QUANTILE = 0.95

FAMILY_EXCESS_CI_EXCLUDES_ZERO = "family_excess_ci_excludes_zero"
FAMILY_EXCESS_CI_INCLUDES_ZERO = "family_excess_ci_includes_zero"
ERROR_CONCENTRATES_AT_EXTREMES = "error_concentrates_at_extremes"
NO_STRATIFICATION_STRUCTURE = "no_stratification_structure"

# The pinned family stratum is Cu + O + >=2 of exactly {Ba,Sr,Ca,Mg}
# (KEYSTONE_A data audit: 4,009 rows, 18.85% of the registered card).  Y and
# the lanthanides are not alkaline earths, so YBCO- and La-series rows fall
# outside the stratum despite being cuprates.
FAMILY_AE = frozenset({"Ba", "Sr", "Ca", "Mg"})


def _is_family(symbols: frozenset[str]) -> bool:
    return "Cu" in symbols and "O" in symbols and len(symbols & FAMILY_AE) >= 2


def _featurize(formulas: tuple[str, ...], targets: tuple[float, ...]):
    """Featurize the batch; returns (X, y, family, holes) with failed rows dropped.

    Holes-per-Cu are only computed for family rows: the frozen valence
    table covers the family stratum's elements, and a non-family formula
    lacking Cu or carrying an untabled element must not fail the run.
    """

    import pandas as pd

    from aletheia.domains.materials.featurizers import magpie_features
    from aletheia.execution.cuprate.doping import stoichiometric_holes_per_copper

    frame = pd.DataFrame({"material": list(formulas), "critical_temp": list(targets)})
    if len(frame) > MAX_ROWS:
        frame = frame.sample(MAX_ROWS, random_state=SEED).reset_index(drop=True)
    X, _names, work = magpie_features(frame, composition_col="material")
    X = np.asarray(X, dtype=float)
    y = work["critical_temp"].to_numpy(dtype=float)
    comps = work["_comp_obj"].tolist()

    family = np.array([_is_family(frozenset(e.symbol for e in c.elements)) for c in comps])
    holes = np.full(len(comps), np.nan)
    for i, comp in enumerate(comps):
        if family[i]:
            holes[i] = stoichiometric_holes_per_copper(comp)
    return X, y, comps, family, holes


def _fit_holdout(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """70/30 split, seeded RF on unscaled features; returns (train, holdout, |err|)."""

    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split

    tr, ho = train_test_split(np.arange(len(y)), test_size=TEST_SIZE, random_state=SEED)
    forest = RandomForestRegressor(n_estimators=N_ESTIMATORS, n_jobs=1, random_state=SEED).fit(
        X[tr], y[tr]
    )
    err = np.abs(forest.predict(X[ho]) - y[ho])
    return tr, ho, err


def matched_control_contrast(
    *,
    X: np.ndarray,
    n_elements: np.ndarray,
    family: np.ndarray,
    train_index: np.ndarray,
    holdout_index: np.ndarray,
    errors: np.ndarray,
) -> dict[str, Any]:
    """D1: does the family excess survive complexity matching?

    Callers pass the already-fit holdout errors; this function owns only
    the D1-specific machinery (coordinates, matching, bootstrap) so the RF
    is trained exactly once per diagnostic.
    """

    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler

    if len(train_index) < N_DENSITY_NEIGHBORS:
        raise ValueError(
            "matched control contrast needs at least "
            f"{N_DENSITY_NEIGHBORS} train rows for the density neighborhood"
        )
    scaler = StandardScaler().fit(X[train_index])
    density_nn = NearestNeighbors(n_neighbors=N_DENSITY_NEIGHBORS).fit(
        scaler.transform(X[train_index])
    )
    density = density_nn.kneighbors(scaler.transform(X[holdout_index]))[0].mean(axis=1)
    coords = np.column_stack([n_elements[holdout_index], density])
    coords = (coords - coords.mean(axis=0)) / (coords.std(axis=0) + STD_GUARD)

    family_ho = family[holdout_index]
    fam_idx = np.where(family_ho)[0]
    non_idx = np.where(~family_ho)[0]
    if not len(fam_idx):
        raise ValueError("matched control contrast needs family rows in the holdout")
    if not len(non_idx):
        raise ValueError("matched control contrast needs non-family control rows in the holdout")

    rng = np.random.default_rng(SEED)
    match_nn = NearestNeighbors(n_neighbors=min(MAX_MATCH_CANDIDATES, len(non_idx))).fit(
        coords[non_idx]
    )
    order = fam_idx[rng.permutation(len(fam_idx))]
    used: set[int] = set()
    matched: list[int] = []
    for row, neighbors in zip(order, match_nn.kneighbors(coords[order])[1]):
        for j in neighbors:
            global_index = int(non_idx[j])
            if global_index not in used:
                used.add(global_index)
                matched.append(global_index)
                break
    matched_idx = np.array(matched)

    mae_family = float(errors[fam_idx].mean())
    mae_matched = float(errors[matched_idx].mean())
    mae_non_all = float(errors[non_idx].mean())

    boot = []
    for _ in range(N_BOOTSTRAP):
        resampled_family = rng.choice(fam_idx, len(fam_idx), replace=True)
        resampled_matched = rng.choice(matched_idx, len(matched_idx), replace=True)
        boot.append(errors[resampled_family].mean() - errors[resampled_matched].mean())
    lo, hi = np.quantile(boot, CI_QUANTILES)
    survives = bool(lo > 0)
    return {
        "mae_cuprate": round(mae_family, 3),
        "mae_matched": round(mae_matched, 3),
        "mae_non_all": round(mae_non_all, 3),
        "excess_over_matched": round(mae_family - mae_matched, 3),
        "ci": [round(float(lo), 3), round(float(hi), 3)],
        "survives": survives,
        "n_cuprate": int(len(fam_idx)),
        "outcome_bin": (
            FAMILY_EXCESS_CI_EXCLUDES_ZERO if survives else FAMILY_EXCESS_CI_INCLUDES_ZERO
        ),
    }


def doping_deviation_stratification(
    *,
    holdout_index: np.ndarray,
    errors: np.ndarray,
    holes_per_copper: np.ndarray,
    family_mask: np.ndarray,
    doping_optimum: float,
) -> dict[str, Any]:
    """D2: does within-family error stratify by doping deviation from the optimum?

    ``errors`` is holdout-aligned (the single fit's |err| vector, the same
    convention as D1); ``holes_per_copper`` and ``family_mask`` are
    full-length arrays aligned with the featurized frame, of which only the
    holdout entries are used.  The permutation null is the source's
    ``_perm_p95`` verbatim: fresh ``default_rng(SEED)``, 400 same-size
    random masks over the family holdout rows, p95 of |median effect|.
    """

    family_ho = family_mask[holdout_index]
    fam_err = errors[family_ho]
    deviation = np.abs(holes_per_copper[holdout_index][family_ho] - doping_optimum)
    threshold = np.quantile(deviation, 0.5)
    high = deviation > threshold
    if not high.any() or high.all():
        # An empty half (one family holdout row, or ties putting every row on
        # one side of the median) has no split to test; NaNs must never reach
        # the wire result.
        raise ValueError(
            "family doping deviations are degenerate: the median split has an empty half"
        )

    effect = float(np.median(fam_err[high]) - np.median(fam_err[~high]))

    rng = np.random.default_rng(SEED)
    n_sub = int(high.sum())
    index = np.arange(len(fam_err))
    null = []
    for _ in range(N_PERMUTATIONS):
        permuted = np.zeros(len(fam_err), bool)
        permuted[rng.permutation(index)[:n_sub]] = True
        null.append(np.median(fam_err[permuted]) - np.median(fam_err[~permuted]))
    ctrl_p95 = float(np.quantile(np.abs(null), PERM_QUANTILE))
    concentrates = bool(abs(effect) > ctrl_p95 and effect > 0)
    return {
        "family_holdout_rows": int(len(fam_err)),
        "deviation_threshold": round(float(threshold), 4),
        "effect": round(effect, 4),
        "ctrl_p95": round(ctrl_p95, 4),
        "concentrates": concentrates,
        "outcome_bin": (
            ERROR_CONCENTRATES_AT_EXTREMES if concentrates else NO_STRATIFICATION_STRUCTURE
        ),
    }


def run_cuprate_diagnostic(
    *,
    formulas: tuple[str, ...],
    targets: tuple[float, ...],
    doping_optimum: float,
) -> dict[str, Any]:
    """Execute the full diagnostic over the bound batch's rows.

    Deterministic given identical rows, library versions and hardware:
    every stochastic component is seeded from ``SEED`` and the result is
    rounded to the source conventions before emission.
    """

    if doping_optimum <= 0:
        raise ValueError("doping optimum must be positive")
    X, y, comps, family, holes = _featurize(formulas, targets)
    if not family.any():
        raise ValueError("bound batch contains no multi-alkaline-earth cuprate rows")
    n_elements = np.array([len(c.elements) for c in comps])
    train_index, holdout_index, errors = _fit_holdout(X, y)
    return {
        # Rows that actually reached the pipeline: _featurize caps the frame
        # at MAX_ROWS via a seeded subsample, so the count is post-cap.
        "analyzed_rows": min(len(formulas), MAX_ROWS),
        "d1_matched_control": matched_control_contrast(
            X=X,
            n_elements=n_elements,
            family=family,
            train_index=train_index,
            holdout_index=holdout_index,
            errors=errors,
        ),
        "d2_doping_stratification": doping_deviation_stratification(
            holdout_index=holdout_index,
            errors=errors,
            holes_per_copper=holes,
            family_mask=family,
            doping_optimum=doping_optimum,
        ),
    }
