"""Shared, domain-agnostic leakage-aware regression protocol.

Every domain that predicts a continuous property reuses the SAME honest harness:
RepeatedKFold (confidence) + grouped out-of-fold CV (the **headline** — generalize
to unseen groups, not interpolate within them) + a random holdout (comparison +
parity plot) + a baseline panel under the same grouped CV + an error breakdown by a
domain-supplied stratifier. The agent never grades its own homework: this code, not
the model author, computes every metric.

A plugin supplies only what is domain-specific — the model/baseline factories, the
group key (chemical system, molecular scaffold, …), the stratifier, and labels/units
— via ``grouped_regression_eval``; the protocol stays identical across domains.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from aletheia.domains.base import ExperimentResult


def _grouped_scores(estimator_factory: Callable[[], Any], X: Any, y: Any, groups: Any) -> dict[str, Any]:
    """Grouped out-of-fold scores via GroupKFold (each group sits entirely in one
    held-out fold). Falls back to plain KFold when no usable grouping is available."""
    import numpy as np
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import GroupKFold, KFold, cross_val_predict

    n_groups = int(len(np.unique(groups))) if groups is not None else len(y)
    n_splits = max(2, min(5, n_groups))
    if groups is not None and n_groups >= 2:
        cv = GroupKFold(n_splits=n_splits)
        oof = cross_val_predict(estimator_factory(), X, y, cv=cv, groups=groups, n_jobs=1)
    else:
        cv = KFold(n_splits=n_splits, shuffle=True, random_state=42)
        oof = cross_val_predict(estimator_factory(), X, y, cv=cv, n_jobs=1)
    return {
        "mae": float(mean_absolute_error(y, oof)),
        "r2": float(r2_score(y, oof)),
        "rmse": float(mean_squared_error(y, oof) ** 0.5),
        "n_groups": n_groups,
        "n_splits": n_splits,
    }


def _parity_plot(y_true: Any, y_pred: Any, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4, 4))
    ax.scatter(y_true, y_pred, s=10, alpha=0.6)
    lo = float(min(min(y_true), min(y_pred)))
    hi = float(max(max(y_true), max(y_pred)))
    ax.plot([lo, hi], [lo, hi], "r--", lw=1)
    ax.set_xlabel("Actual")
    ax.set_ylabel("Predicted")
    ax.set_title("Parity")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def grouped_regression_eval(
    *,
    model_factory: Callable[[], Any],
    baseline_models: dict[str, Any],
    X: Any,
    y: Any,
    groups: Any,
    design: dict[str, Any],
    workdir: Path,
    model_name: str,
    grouped_key: str = "grouped",          # metric-key suffix: "lcso" | "scaffold" | "grouped"
    headline_label: str = "grouped CV",    # summary prose, e.g. "LCSO(GroupKFold)"
    grouped_abbr: str = "grouped",         # short tag in the baselines line, e.g. "LCSO"
    group_strategy_desc: str = "GroupKFold(group)",
    units: str = "",
    stratifier: Callable[[Any, Any], list[dict[str, Any]]] | None = None,
    stratify_label: str = "value range",
) -> ExperimentResult:
    """Run the full leakage-aware protocol and return a populated ExperimentResult.

    The metrics dict always carries headline aliases ``mae``/``r2``/``rmse`` (= the
    grouped numbers), the explicit grouped keys ``{mae,r2,rmse}_{grouped_key}``, the
    RepeatedKFold ``mae_cv_mean``/``mae_cv_std``/``r2_cv_mean``, and the holdout
    ``{mae,r2,rmse}_holdout``. ``model_factory`` is called for every fit (a fresh
    estimator each time) so folds never leak a fitted model.
    """
    import joblib
    import numpy as np
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import RepeatedKFold, cross_validate, train_test_split

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    y_arr = np.asarray(y, dtype=float)
    random_state = int(design.get("random_state", 42))
    test_size = float(design.get("test_size", 0.2))
    usfx = f" {units}" if units else ""

    # 1. RepeatedKFold 5x5 -> confidence (mean ± std)
    rkf = RepeatedKFold(n_splits=5, n_repeats=5, random_state=random_state)
    cv = cross_validate(
        model_factory(), X, y_arr, cv=rkf,
        scoring=("neg_mean_absolute_error", "r2"), n_jobs=1,
    )
    mae_cv = -cv["test_neg_mean_absolute_error"]
    mae_cv_mean, mae_cv_std = float(mae_cv.mean()), float(mae_cv.std())
    r2_cv_mean = float(cv["test_r2"].mean())

    # 2. grouped out-of-fold CV -> HEADLINE
    grouped = _grouped_scores(model_factory, X, y_arr, groups)
    mae_g, r2_g, rmse_g = grouped["mae"], grouped["r2"], grouped["rmse"]

    # 3. single random holdout -> comparison + parity plot + stratification source
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y_arr, test_size=test_size, random_state=random_state
    )
    holdout = model_factory()
    holdout.fit(X_tr, y_tr)
    pred = holdout.predict(X_te)
    mae_ho = float(mean_absolute_error(y_te, pred))
    r2_ho = float(r2_score(y_te, pred))
    rmse_ho = float(mean_squared_error(y_te, pred) ** 0.5)

    # 4. baseline panel under the SAME grouped CV
    baselines_grouped = {
        name: _grouped_scores(lambda est=est: est, X, y_arr, groups)["mae"]
        for name, est in baseline_models.items()
    }
    baselines_grouped[model_name] = mae_g

    # 5. error stratified by a domain-supplied binning (on holdout predictions)
    error_by_range = stratifier(y_te, pred) if stratifier else []

    # final model fit on ALL data + artifacts
    final = model_factory()
    final.fit(X, y_arr)
    model_path = workdir / "model.joblib"
    joblib.dump(final, model_path)
    artifacts: list[dict[str, Any]] = [{"kind": "model", "uri": str(model_path)}]
    try:
        plot_path = workdir / "parity.png"
        _parity_plot(y_te, pred, plot_path)
        artifacts.append({"kind": "plot", "uri": str(plot_path)})
    except Exception:  # plotting is best-effort; never fail the run on it
        pass

    eval_payload = {
        "protocol": {
            "headline_metric": f"mae_{grouped_key}",
            "repeated_kfold": {"n_splits": 5, "n_repeats": 5},
            "grouped_cv": {
                "strategy": group_strategy_desc,
                "n_splits": grouped["n_splits"],
                "n_groups": int(grouped["n_groups"]),
            },
            "holdout": {"test_size": test_size, "random_state": random_state},
        },
        "headline": {"metric": f"mae_{grouped_key}", "value": mae_g},
        "splits": {
            "grouped_cv": {"mae": mae_g, "r2": r2_g, "rmse": rmse_g},
            "repeated_kfold_5x5": {"mae_mean": mae_cv_mean, "mae_std": mae_cv_std, "r2_mean": r2_cv_mean},
            "random_holdout": {"mae": mae_ho, "r2": r2_ho, "rmse": rmse_ho, "n_test": int(len(y_te))},
        },
        "baselines_grouped_mae": baselines_grouped,
        "error_by_range": error_by_range,
    }
    eval_path = workdir / "eval.json"
    eval_path.write_text(json.dumps(eval_payload, indent=2))
    artifacts.append({"kind": "eval", "uri": str(eval_path)})

    bl_str = ", ".join(f"{k} {v:.3f}" for k, v in baselines_grouped.items())
    gap_str = "; ".join(
        f"{r['range']} n={r['n']} MAE={r['mae']:.3f}"
        for r in error_by_range
        if r.get("mae") is not None
    )
    eval_summary = (
        f"{headline_label} MAE {mae_g:.3f}{usfx}, R² {r2_g:.3f}, RMSE {rmse_g:.3f} "
        f"(HEADLINE, all same split) | "
        f"RepeatedKFold 5x5 MAE {mae_cv_mean:.3f}±{mae_cv_std:.3f} | "
        f"random holdout MAE {mae_ho:.3f} (comparison only) | "
        f"baselines({grouped_abbr} MAE): {bl_str} | errors by {stratify_label}: {gap_str}"
    )

    metrics = {
        # headline aliases (OPTIMIZE compares + the report leads with these) = the
        # honest grouped numbers, not the optimistic holdout. All share provenance.
        "mae": mae_g,
        "r2": r2_g,
        "rmse": rmse_g,
        # explicit grouped keys (named per domain via grouped_key)
        f"mae_{grouped_key}": mae_g,
        f"r2_{grouped_key}": r2_g,
        f"rmse_{grouped_key}": rmse_g,
        "mae_cv_mean": mae_cv_mean,
        "mae_cv_std": mae_cv_std,
        "r2_cv_mean": r2_cv_mean,
        "mae_holdout": mae_ho,
        "r2_holdout": r2_ho,
        "rmse_holdout": rmse_ho,
    }
    return ExperimentResult(
        metrics=metrics,
        artifacts=artifacts,
        info={
            "n_train": int(len(X_tr)),
            "n_test": int(len(y_te)),
            "model": model_name,
            # the estimator class ACTUALLY fit + scored — so the write-up reports what
            # ran, not merely what the design requested (a coder-authored Pipeline, or
            # a fallback like RandomForestRegressor when the requested method is not
            # implementable on these features).
            "model_impl": type(final).__name__,
            "eval_summary": eval_summary,
        },
    )
