"""Phase 1 step 3: materials domain plugin trains + returns MAE/R² offline.

No dataset download and no API: a tiny hand-built composition frame exercises
Magpie featurization + the sklearn train/evaluate path end-to-end.
"""

from __future__ import annotations

import pandas as pd

from aletheia.domains.materials.datasets import resolve_columns
from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin

# A small, real composition set with plausible band gaps (eV) — enough rows for
# Magpie features (~132 dims) + a held-out split.
_COMPS = [
    ("Si", 1.12), ("Ge", 0.67), ("GaAs", 1.42), ("GaN", 3.4), ("ZnO", 3.37),
    ("ZnS", 3.6), ("CdTe", 1.49), ("CdS", 2.42), ("NaCl", 8.5), ("MgO", 7.8),
    ("TiO2", 3.2), ("SiC", 3.0), ("AlN", 6.2), ("InP", 1.35), ("InAs", 0.36),
    ("PbS", 0.37), ("Cu2O", 2.1), ("Fe2O3", 2.2), ("SnO2", 3.6), ("WO3", 2.7),
]


def test_materials_plugin_trains_and_scores(tmp_path):
    plug = MaterialsBandGapPlugin()
    df = pd.DataFrame(_COMPS, columns=["composition", "band_gap"])

    design = {
        "model": "random_forest",
        "model_params": {"n_estimators": 50},
        "target_column": "band_gap",
        "composition_column": "composition",
        "test_size": 0.25,
        "random_state": 0,
    }

    X, y, feature_names, groups = plug.featurize(df, design)
    assert len(feature_names) > 100  # Magpie ~132 descriptors
    assert len(X) == len(y) == len(df)  # all toy compositions featurize cleanly
    assert len(groups) == len(df)  # one chemical-system key per row
    assert "Ga-N" in set(groups)  # GaN -> chemical system Ga-N

    result = plug.train_evaluate(X, y, design, tmp_path, groups=groups)
    # headline aliases + explicit protocol metrics are all present
    assert {
        "mae", "r2", "rmse",
        "mae_lcso", "r2_lcso", "rmse_lcso", "mae_cv_mean", "mae_cv_std", "r2_cv_mean",
        "mae_holdout", "r2_holdout", "rmse_holdout",
    } <= set(result.metrics)
    # the headline triple shares one (LCSO) provenance — internally consistent
    assert result.metrics["mae"] == result.metrics["mae_lcso"]
    assert result.metrics["r2"] == result.metrics["r2_lcso"]
    assert result.metrics["rmse"] == result.metrics["rmse_lcso"]
    assert isinstance(result.metrics["mae"], float)
    assert result.metrics["mae"] >= 0.0
    assert result.metrics["mae_cv_std"] >= 0.0
    # model + eval-panel artifacts written to the workdir
    kinds = {a["kind"] for a in result.artifacts}
    assert {"model", "eval"} <= kinds
    assert (tmp_path / "model.joblib").exists()
    assert (tmp_path / "eval.json").exists()
    assert "eval_summary" in result.info and "LCSO" in result.info["eval_summary"]
    assert result.info["n_test"] >= 1


def test_explicit_composition_column_overrides_heuristic():
    """Step 1a: an explicit composition_column resolves deterministically; without it the
    first-non-numeric-column heuristic picks the wrong (decoy) column."""
    df = pd.DataFrame({"label": ["x", "y"], "material": ["GaAs", "Si"], "critical_temp": [1.0, 2.0]})
    comp, _ = resolve_columns(df, {})                      # heuristic alone -> the hazard
    assert comp == "label"
    comp2, tgt2 = resolve_columns(df, {"composition_column": "material", "target_column": "critical_temp"})
    assert comp2 == "material" and tgt2 == "critical_temp"


def test_featurize_uses_explicit_composition_column_from_data_spec():
    """Mirrors the live demonstration path: load_data stamps df.attrs['data_spec'] (now carrying
    composition_column from resolve_data_spec) and featurize(df, {}) reads it -> the UCI
    superconductivity shape (material/critical_temp) featurizes via the NAMED column, not 'composition'."""
    plug = MaterialsBandGapPlugin()
    df = pd.DataFrame({
        "note": ["m"] * len(_COMPS),                       # decoy non-numeric FIRST column
        "material": [c for c, _ in _COMPS],                 # the real formula column (UCI name)
        "critical_temp": [g for _, g in _COMPS],
    })
    df.attrs["data_spec"] = {"composition_column": "material", "target_column": "critical_temp"}
    X, y, names, groups = plug.featurize(df, {})           # design={} -> spec comes from attrs
    assert X.shape[0] == len(y) == len(groups) == len(_COMPS)
    assert X.shape[1] > 100 and len(names) == X.shape[1]    # Magpie dims, from 'material'
    assert len(set(groups)) >= 2                            # chemical-system groups (element sets)
    assert "As-Ga" in set(groups)                           # GaAs -> chemical system As-Ga (alphabetical)


def test_profile_is_target_aware():
    """Step 1b: profile() selects target-aware semantics — band-gap (eV/gap) by default, Tc (K) for a
    superconductivity run — so band-gap labels never leak into a Tc run's vocabulary."""
    plug = MaterialsBandGapPlugin()
    gap = plug.profile()                                   # default -> band gap
    assert gap.units == "eV" and "gap" in gap.protocol_desc.lower()

    tc = plug.profile(target_column="critical_temp")       # superconductivity Tc
    assert tc.units == "K"
    assert "tc" in tc.task.lower() or "superconduct" in tc.task.lower()
    assert "gap" not in tc.protocol_desc.lower()            # no band-gap leakage
    assert "eV" not in tc.dry_eval_summary and "K" in tc.dry_eval_summary
    assert "superconduct" in tc.sota_reference.lower() or "Tc" in tc.sota_reference


def test_train_evaluate_tc_reports_kelvin_not_ev(tmp_path):
    """A Tc-target design routes train_evaluate through the K / Tc-range semantics (units + stratifier),
    not the hardcoded eV / gap path."""
    plug = MaterialsBandGapPlugin()
    df = pd.DataFrame(_COMPS, columns=["composition", "critical_temp"])  # reuse toy compositions
    design = {"model": "random_forest", "model_params": {"n_estimators": 30},
              "target_column": "critical_temp", "composition_column": "composition",
              "test_size": 0.25, "random_state": 0}
    X, y, _names, groups = plug.featurize(df, design)
    result = plug.train_evaluate(X, y, design, tmp_path, groups=groups)
    summ = result.info["eval_summary"]
    assert "eV" not in summ and " K," in summ               # Kelvin units, no band-gap leak
    assert "errors by Tc" in summ and "errors by gap" not in summ   # Tc-range stratifier, not gap


def test_baselines_listed():
    plug = MaterialsBandGapPlugin()
    bl = plug.baselines()
    assert len(bl) >= 2
    assert {b["model"] for b in bl} >= {"random_forest", "gradient_boosting"}


def test_make_model_forces_single_thread_even_if_design_requests_all_cores():
    # the sandbox caps RLIMIT_CPU (CPU-seconds summed across threads); a design asking n_jobs=-1
    # would blow the budget and die with SIGXCPU (return code -24) before the wall-clock backstop.
    # _make_model must clamp n_jobs to 1 regardless of what the design requests.
    plug = MaterialsBandGapPlugin()
    est = plug._make_model({"model": "random_forest",
                            "model_params": {"n_jobs": -1, "n_estimators": 10}})
    assert est.n_jobs == 1


def test_reseed_estimator_clamps_njobs_and_reseeds():
    # the harness adopts a coder-authored pipeline: it must own BOTH the seed AND the thread budget
    # (n_jobs=1) so the sandboxed solution_path estimator can't blow RLIMIT_CPU with n_jobs=-1.
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    from aletheia.domains.base import reseed_estimator

    pipe = Pipeline([("sc", StandardScaler()),
                     ("rf", RandomForestRegressor(n_jobs=-1, random_state=0))])
    out = reseed_estimator(pipe, 7)
    p = out.get_params(deep=True)
    assert p["rf__n_jobs"] == 1        # thread budget clamped (sandbox CPU-seconds safety)
    assert p["rf__random_state"] == 7  # harness owns the seed


def test_reseed_estimator_clamps_njobs_even_without_seed():
    from sklearn.ensemble import RandomForestRegressor

    from aletheia.domains.base import reseed_estimator

    est = reseed_estimator(RandomForestRegressor(n_jobs=-1, random_state=3), None)
    assert est.n_jobs == 1       # clamped even when no reseed is requested
    assert est.random_state == 3  # seed untouched when random_state is None
