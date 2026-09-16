"""Seeded pipeline regressions on a synthetic fixture (no Magpie network).

The real 12k-row runs happen only on the box (v100ts, env arl2-cuprate);
these tests pin the machinery: rng-stream determinism, outcome-bin
mapping, planted-stratum detection, and the fail-closed paths.  Magpie is
replaced by an offline composition featurizer with the same contract —
the featurizer seam is the only mocked piece.
"""

import numpy as np
import pytest
from pymatgen.core import Composition

from aletheia.execution.cuprate import diagnostics
from aletheia.execution.cuprate.diagnostics import (
    doping_deviation_stratification,
    matched_control_contrast,
    run_cuprate_diagnostic,
)
from aletheia.research_kernel.schemas import canonical_json_bytes


def synthetic_featurizer(frame, composition_col):
    """Offline stand-in for magpie_features with the same (X, names, work) contract."""

    comps = [Composition(formula) for formula in frame[composition_col]]
    symbols = sorted({el.symbol for comp in comps for el in comp.elements})
    rows = [
        [comp.get_el_amt_dict().get(symbol, 0.0) for symbol in symbols]
        + [len(comp.elements), float(sum(comp.values()))]
        for comp in comps
    ]
    work = frame.copy()
    work["_comp_obj"] = comps
    return (
        np.asarray(rows, dtype=float),
        [f"n_{symbol}" for symbol in symbols] + ["n_elements", "total_atoms"],
        work,
    )


@pytest.fixture
def offline_magpie(monkeypatch):
    from aletheia.execution.cuprate import featurization

    monkeypatch.setattr(featurization, "magpie_features", synthetic_featurizer)


def _fixture_batch():
    # Every family row carries two of {Ba,Sr,Ca,Mg}: the pinned stratum is
    # Cu+O+>=2 alkaline earths, so single-AE cuprates (YBCO, La214) stay in
    # the non-family pool.
    family = (
        tuple(f"Bi2Sr2CaCu2O{oxy:.1f}" for oxy in np.linspace(7.3, 8.2, 8))
        + tuple(f"Tl2Ba2CaCu3O{oxy:.1f}" for oxy in np.linspace(9.2, 10.0, 7))
        + tuple(f"HgBa2Ca2Cu3O{oxy:.1f}" for oxy in np.linspace(7.9, 8.5, 5))
    )
    non_family = (
        "SiO2",
        "Fe2O3",
        "GaAs",
        "PbTe",
        "Nb3Sn",
        "TiO2",
        "ZnO",
        "Al2O3",
        "SnSe",
        "CdTe",
        "InP",
        "NiO",
        "MgO",
        "CaO",
        "SrO",
        "BaO",
        "ZrO2",
        "HfO2",
        "MnO",
        "CoO",
    ) * 2
    rng = np.random.default_rng(0)
    targets = np.concatenate(
        [rng.normal(75.0, 4.0, len(family)), rng.normal(8.0, 4.0, len(non_family))]
    )
    return family + non_family, tuple(float(value) for value in targets)


def test_full_run_is_deterministic_and_bin_consistent(offline_magpie):
    formulas, targets = _fixture_batch()
    result = run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)
    again = run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)
    assert canonical_json_bytes(result) == canonical_json_bytes(again)

    assert set(result) == {"analyzed_rows", "d1_matched_control", "d2_doping_stratification"}
    assert result["analyzed_rows"] == len(formulas)
    d1 = result["d1_matched_control"]
    d2 = result["d2_doping_stratification"]
    assert set(d1) == {
        "mae_cuprate",
        "mae_matched",
        "mae_non_all",
        "excess_over_matched",
        "ci",
        "survives",
        "n_cuprate",
        "outcome_bin",
    }
    assert d1["survives"] == (d1["ci"][0] > 0)
    assert d1["outcome_bin"] == (
        "family_excess_ci_excludes_zero" if d1["survives"] else "family_excess_ci_includes_zero"
    )
    assert d2["concentrates"] == (d2["effect"] > 0 and abs(d2["effect"]) > d2["ctrl_p95"])
    assert d2["outcome_bin"] == (
        "error_concentrates_at_extremes" if d2["concentrates"] else "no_stratification_structure"
    )
    assert d1["n_cuprate"] >= 1
    assert d2["family_holdout_rows"] >= 1


def test_matched_control_contrast_reports_planted_family_excess():
    # Holdout gets 10 family rows (error ~8) and 10 non-family rows (error
    # ~2); the matcher pairs each family row with a distinct control.
    rng = np.random.default_rng(1)
    family = np.zeros(100, bool)
    family[30:40] = True
    train_index = np.concatenate([np.arange(30), np.arange(40, 90)])
    holdout_index = np.concatenate([np.arange(30, 40), np.arange(90, 100)])
    full_errors = np.where(family, rng.normal(8.0, 0.3, 100), rng.normal(2.0, 0.3, 100))
    result = matched_control_contrast(
        X=rng.normal(size=(100, 4)),
        n_elements=rng.integers(2, 6, 100),
        family=family,
        train_index=train_index,
        holdout_index=holdout_index,
        errors=full_errors[holdout_index],
    )
    assert result["survives"] is True
    assert result["outcome_bin"] == "family_excess_ci_excludes_zero"
    assert result["n_cuprate"] == int(family[holdout_index].sum()) == 10
    assert result["mae_cuprate"] > result["mae_matched"]


def test_matched_control_contrast_on_equal_errors_is_not_significant():
    rng = np.random.default_rng(2)
    family = np.zeros(60, bool)
    family[10:20] = True
    holdout_index = np.concatenate([np.arange(10, 20), np.arange(45, 60)])
    result = matched_control_contrast(
        X=rng.normal(size=(60, 4)),
        n_elements=rng.integers(2, 6, 60),
        family=family,
        train_index=np.concatenate([np.arange(10), np.arange(20, 45)]),
        holdout_index=holdout_index,
        errors=np.full(60, 2.0)[holdout_index],
    )
    assert result["survives"] is False
    assert result["outcome_bin"] == "family_excess_ci_includes_zero"
    assert result["excess_over_matched"] == 0.0


def test_doping_deviation_stratification_detects_minority_planted_stratum():
    # 24 family holdout rows: 6 far from the optimum (holes 0.60) with high
    # error, 18 at the optimum with low error.  The size-6 random masks of
    # the null rarely concentrate on the minority, so the effect beats p95.
    holes = np.concatenate([np.full(6, 0.60), np.full(18, 0.16), np.zeros(16)])
    family_mask = np.concatenate([np.full(24, True), np.full(16, False)])
    result = doping_deviation_stratification(
        holdout_index=np.arange(24),
        errors=np.concatenate([np.full(6, 9.0), np.full(18, 1.0)]),
        holes_per_copper=holes,
        family_mask=family_mask,
        doping_optimum=0.16,
    )
    assert result["concentrates"] is True
    assert result["outcome_bin"] == "error_concentrates_at_extremes"
    assert result["family_holdout_rows"] == 24


def test_doping_deviation_stratification_without_structure_does_not_concentrate():
    holes = np.concatenate([np.full(6, 0.60), np.full(18, 0.16), np.zeros(16)])
    family_mask = np.concatenate([np.full(24, True), np.full(16, False)])
    result = doping_deviation_stratification(
        holdout_index=np.arange(24),
        errors=np.full(24, 1.0),
        holes_per_copper=holes,
        family_mask=family_mask,
        doping_optimum=0.16,
    )
    assert result["concentrates"] is False
    assert result["outcome_bin"] == "no_stratification_structure"


def test_featurize_gates_doping_estimates_to_family_rows(offline_magpie):
    # Cu-free rows, untabled-element non-family rows, and single-AE cuprates
    # (YBCO: Y is not an alkaline earth) must not touch the frozen valence
    # table; only pinned-stratum rows get a doping estimate.
    formulas = (
        "Fe2O3",
        "Nb3Sn",
        "XeF2",
        "YBa2Cu3O7",
        "Bi2Sr2CaCu2O8",
        "Tl2Ba2CaCu3O10",
        "SiO2",
    )
    targets = (0.5, 0.2, 0.1, 93.0, 91.0, 88.0, 0.01)
    X, y, comps, family, holes = diagnostics._featurize(formulas, targets)
    assert family.tolist() == [False, False, False, False, True, True, False]
    assert np.isnan(holes[~family]).all()
    assert np.isfinite(holes[family]).all()
    assert len(X) == len(y) == len(comps)


def test_untabled_family_element_fails_the_whole_run(offline_magpie):
    formulas = ("Bi2Sr2CaCu2O8Xe0.1", "SiO2", "Fe2O3", "Nb3Sn")
    targets = (90.0, 0.1, 0.2, 0.3)
    with pytest.raises(ValueError, match="Xe"):
        run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)


def test_run_refuses_without_family_rows(offline_magpie):
    formulas = ("SiO2", "Fe2O3", "Nb3Sn")
    targets = (0.1, 0.2, 0.3)
    with pytest.raises(ValueError, match="no multi-alkaline-earth cuprate"):
        run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)


@pytest.mark.parametrize("optimum", [0.0, -0.16])
def test_run_refuses_non_positive_doping_optimum(offline_magpie, optimum):
    formulas, targets = _fixture_batch()
    with pytest.raises(ValueError, match="doping optimum must be positive"):
        run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=optimum)


def test_frozen_constants_mirror_the_source_scripts():
    # These values ARE the ported lineage (scripts/cuprate_matched_control.py
    # and chem_effect_probe.py).  Changing one is a lineage decision that must
    # be recorded, never a refactor.
    assert (
        diagnostics.SEED,
        diagnostics.MAX_ROWS,
        diagnostics.N_ESTIMATORS,
        diagnostics.TEST_SIZE,
        diagnostics.N_DENSITY_NEIGHBORS,
        diagnostics.MAX_MATCH_CANDIDATES,
        diagnostics.STD_GUARD,
        diagnostics.N_BOOTSTRAP,
        diagnostics.CI_QUANTILES,
        diagnostics.N_PERMUTATIONS,
        diagnostics.PERM_QUANTILE,
    ) == (0, 12000, 150, 0.3, 10, 20, 1e-9, 2000, (0.025, 0.975), 400, 0.95)


def test_featurize_caps_the_frame_at_max_rows_with_the_seeded_subsample(offline_magpie):
    # The cap is the one lineage divergence from the historical full-file
    # runs; it must subsample deterministically and report honestly.  Rows
    # repeat few distinct formulas so Composition parsing stays cheap; the
    # three family oxygen contents keep the D2 deviation split non-degenerate.
    cycle = ("Bi2Sr2CaCu2O7.3", "Bi2Sr2CaCu2O7.7", "Bi2Sr2CaCu2O8.1", "SiO2", "Fe2O3", "Nb3Sn")
    formulas = tuple(cycle * 2001)  # 12006 rows
    targets = tuple((91.0, 88.0, 85.0, 0.1, 0.2, 0.3)[i % 6] for i in range(len(formulas)))
    X, y, comps, family, holes = diagnostics._featurize(formulas, targets)
    assert len(X) == len(y) == len(comps) == diagnostics.MAX_ROWS

    import pandas as pd

    expected = (
        pd.DataFrame({"material": list(formulas), "critical_temp": list(targets)})
        .sample(diagnostics.MAX_ROWS, random_state=diagnostics.SEED)
        .reset_index(drop=True)
    )
    assert list(y) == expected["critical_temp"].tolist()
    assert (
        run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)[
            "analyzed_rows"
        ]
        == diagnostics.MAX_ROWS
    )


def test_doping_split_with_an_empty_half_fails_closed():
    # Every family holdout row at the same deviation: the median split has no
    # high half, so there is no observable — refuse rather than emit NaN.
    holes = np.concatenate([np.full(24, 0.16), np.zeros(16)])
    family_mask = np.concatenate([np.full(24, True), np.full(16, False)])
    with pytest.raises(ValueError, match="degenerate"):
        doping_deviation_stratification(
            holdout_index=np.arange(24),
            errors=np.full(24, 1.0),
            holes_per_copper=holes,
            family_mask=family_mask,
            doping_optimum=0.16,
        )


def test_run_fails_closed_when_the_holdout_cannot_support_the_contrast(offline_magpie):
    # Two rows: whichever lands in the seeded 30% holdout, the holdout lacks
    # either the family side or the control side of D1 (and the train side is
    # too small for the density neighborhood) — a domain refusal, never an
    # sklearn shape error.
    formulas = ("Bi2Sr2CaCu2O8", "SiO2")
    targets = (91.0, 0.1)
    with pytest.raises(ValueError, match="matched control contrast"):
        run_cuprate_diagnostic(formulas=formulas, targets=targets, doping_optimum=0.16)


def test_outcome_bin_constants_match_the_wire_literals():
    # The bin strings live twice: as module constants here and as wire
    # Literals in external_rpc.  Neither side can drift without failing this.
    from typing import get_args

    from aletheia.research_controller.external_rpc import (
        CuprateDopingStratificationOutcome,
        CuprateMatchedControlOutcome,
    )

    assert set(get_args(CuprateMatchedControlOutcome.model_fields["outcome_bin"].annotation)) == {
        diagnostics.FAMILY_EXCESS_CI_EXCLUDES_ZERO,
        diagnostics.FAMILY_EXCESS_CI_INCLUDES_ZERO,
    }
    assert set(
        get_args(CuprateDopingStratificationOutcome.model_fields["outcome_bin"].annotation)
    ) == {
        diagnostics.ERROR_CONCENTRATES_AT_EXTREMES,
        diagnostics.NO_STRATIFICATION_STRUCTURE,
    }
