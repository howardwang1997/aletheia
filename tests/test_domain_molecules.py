"""Phase E: the molecules domain (drug-discovery / chem-bio). SMILES -> ECFP
fingerprints -> a regressor scored under the shared leakage-aware protocol with a
Bemis-Murcko scaffold split and a scaffold-grouped RMSE headline. Offline (a tiny
in-memory SMILES frame; real RDKit in-env — never downloads)."""

from __future__ import annotations

import pandas as pd

from aletheia.domains.molecules.plugin import MoleculePropertyPlugin

# tiny in-memory solubility frame (SMILES + a numeric target); diverse scaffolds
_MOLS = [
    ("CCO", -0.77), ("CCCCO", -0.30), ("CC(C)O", -0.18), ("OCCO", 1.10),
    ("c1ccccc1", -2.10), ("c1ccccc1C", -2.80), ("c1ccccc1O", -0.40), ("c1ccccc1CCO", -1.10),
    ("c1ccncc1", 0.80), ("c1ccncc1C", 0.30), ("CC(=O)O", 0.60), ("CCN", 1.20),
    ("CCOCC", -0.20), ("CCCCCC", -3.80), ("c1ccc2ccccc2c1", -3.50), ("c1ccc2ccccc2c1C", -4.10),
]


def test_molecule_plugin_trains_and_scores(tmp_path):
    plug = MoleculePropertyPlugin()
    df = pd.DataFrame(_MOLS, columns=["smiles", "solubility"])
    design = {
        "model": "random_forest", "model_params": {"n_estimators": 30},
        "target_column": "solubility", "smiles_column": "smiles",
        "n_bits": 256, "test_size": 0.25, "random_state": 0,
    }

    # 1. featurize offline (real RDKit, no downloads)
    X, y, feature_names, groups = plug.featurize(df, design)
    assert len(feature_names) == 256  # ECFP bit columns
    assert len(X) == len(y) == len(groups)
    assert len(set(groups)) >= 2  # multiple distinct scaffolds -> grouped CV

    # 2. train/evaluate through the shared protocol
    result = plug.train_evaluate(X, y, design, tmp_path, groups=groups)
    assert {
        "mae", "r2", "rmse",
        "mae_scaffold", "r2_scaffold", "rmse_scaffold",
        "mae_cv_mean", "mae_holdout",
    } <= set(result.metrics)
    assert result.metrics["rmse"] == result.metrics["rmse_scaffold"]  # headline provenance
    kinds = {a["kind"] for a in result.artifacts}
    assert {"model", "eval"} <= kinds
    assert (tmp_path / "eval.json").exists()
    assert "scaffold-split" in result.info["eval_summary"]


def test_molecule_baselines_and_profile():
    plug = MoleculePropertyPlugin()
    bl = plug.baselines()
    assert len(bl) >= 2 and {b["model"] for b in bl} >= {"random_forest", "gradient_boosting"}
    prof = plug.profile()  # must NOT need RDKit
    assert prof.headline_metric == "rmse_scaffold"
    assert "ESOL" in prof.sota_reference and prof.dry_metrics.get("rmse_scaffold")
