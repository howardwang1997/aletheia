"""Phase 1 step 3: materials domain plugin trains + returns MAE/R² offline.

No dataset download and no API: a tiny hand-built composition frame exercises
Magpie featurization + the sklearn train/evaluate path end-to-end.
"""

from __future__ import annotations

import pandas as pd

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
        "mae_lcso", "r2_lcso", "mae_cv_mean", "mae_cv_std", "r2_cv_mean",
        "mae_holdout", "r2_holdout", "rmse_holdout",
    } <= set(result.metrics)
    assert result.metrics["mae"] == result.metrics["mae_lcso"]  # headline = honest LCSO
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


def test_baselines_listed():
    plug = MaterialsBandGapPlugin()
    bl = plug.baselines()
    assert len(bl) >= 2
    assert {b["model"] for b in bl} >= {"random_forest", "gradient_boosting"}
