"""Phase E: the shared, domain-agnostic leakage-aware regression protocol. Runs on
synthetic numeric data with no domain libraries — proving the harness is domain-free."""

from __future__ import annotations

import numpy as np

from aletheia.domains.protocol import grouped_regression_eval


def _quantile_stratifier(y_true, pred):
    y_true = np.asarray(y_true, dtype=float)
    err = np.abs(np.asarray(pred, dtype=float) - y_true)
    med = float(np.median(y_true))
    rows = []
    for label, mask in (("low", y_true < med), ("high", y_true >= med)):
        n = int(mask.sum())
        rows.append({"range": label, "n": n, "mae": float(err[mask].mean()) if n else None})
    return rows


def test_grouped_regression_eval_returns_full_panel(tmp_path):
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(0)
    X = rng.standard_normal((40, 4))
    y = X[:, 0] * 1.5 + rng.standard_normal(40) * 0.1
    groups = np.array([f"g{i % 8}" for i in range(40)], dtype=object)

    result = grouped_regression_eval(
        model_factory=lambda: Ridge(alpha=1.0),
        baseline_models={"ridge": Ridge()},
        X=X, y=y, groups=groups, design={"random_state": 0, "test_size": 0.25},
        workdir=tmp_path, model_name="ridge",
        grouped_key="grouped", headline_label="grouped CV", grouped_abbr="grouped",
        group_strategy_desc="GroupKFold(synthetic)", units="",
        stratifier=_quantile_stratifier, stratify_label="quantile",
    )

    # headline aliases + explicit grouped + cv + holdout keys all present
    assert {
        "mae", "r2", "rmse",
        "mae_grouped", "r2_grouped", "rmse_grouped",
        "mae_cv_mean", "mae_cv_std", "r2_cv_mean",
        "mae_holdout", "r2_holdout", "rmse_holdout",
    } <= set(result.metrics)
    assert result.metrics["mae"] == result.metrics["mae_grouped"]  # headline == grouped
    assert result.metrics["mae"] >= 0.0

    kinds = {a["kind"] for a in result.artifacts}
    assert {"model", "eval"} <= kinds
    assert (tmp_path / "model.joblib").exists()
    assert (tmp_path / "eval.json").exists()
    assert "grouped CV" in result.info["eval_summary"]
    assert result.info["n_test"] >= 1
    assert result.info["model_impl"] == "Ridge"  # records what ACTUALLY ran
    assert result.info["protocol_status"] == "grouped"  # real groups -> honest headline


def test_protocol_falls_back_to_kfold_without_groups(tmp_path):
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(1)
    X = rng.standard_normal((30, 3))
    y = X[:, 0] + rng.standard_normal(30) * 0.1
    result = grouped_regression_eval(
        model_factory=lambda: Ridge(),
        baseline_models={"ridge": Ridge()},
        X=X, y=y, groups=None, design={}, workdir=tmp_path, model_name="ridge",
    )
    assert "mae_grouped" in result.metrics  # KFold fallback still produces the headline
    assert result.info["protocol_status"] == "degraded_kfold"  # no groups -> degraded, marked
