"""Materials band-gap regression — Aletheia's Phase-1 thin-slice domain.

Composition -> Magpie features -> tree regressor, scored under a leakage-aware
protocol: the **headline** number is leave-chemical-system-out MAE (GroupKFold by
chemical system), reported alongside a RepeatedKFold mean±std, a single random
holdout, a baseline panel (Dummy/Ridge/KNN/GBM under the same grouped CV), and an
error breakdown by gap range. CPU-only, offline-capable (the unit test never
downloads), and canonical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aletheia.domains.base import DomainPlugin, ExperimentResult
from aletheia.domains.materials.datasets import load_benchmark, resolve_columns
from aletheia.domains.materials.featurizers import composition_groups, magpie_features

# Band-gap bins (eV) for error stratification. The first bin isolates metals /
# near-zero-gap entries, whose abundance can inflate the aggregate MAE.
_GAP_BINS: list[tuple[str, float, float]] = [
    ("metal~0", -1e-9, 0.05),
    ("[0.05,0.5)", 0.05, 0.5),
    ("[0.5,2)", 0.5, 2.0),
    ("[2,4)", 2.0, 4.0),
    ("[4,inf)", 4.0, float("inf")),
]


class MaterialsBandGapPlugin(DomainPlugin):
    name = "materials"

    # --- data ---
    def load_data(self, data_spec: dict[str, Any]) -> Any:
        source = data_spec.get("source", "benchmark")
        if source == "benchmark":
            df = load_benchmark(data_spec.get("ref") or "matbench_expt_gap")
        elif source in ("upload", "directory", "url"):
            # file, directory of files, or an online URL — all via the loaders.
            from aletheia.data.loaders import materialize, read_tabular

            path = materialize(data_spec)
            max_rows = data_spec.get("max_rows")
            df = read_tabular(path, nrows=int(max_rows) if max_rows else None)
        elif source == "api":
            raise NotImplementedError(
                "Materials Project ('api') fetch is a Phase-2 adapter; provide a "
                "benchmark name, file, directory, or URL for Phase 1."
            )
        else:
            raise ValueError(f"unknown data source: {source!r}")
        df.attrs["data_spec"] = data_spec
        return df

    # --- features ---
    def featurize(self, df: Any, design: dict[str, Any]) -> tuple[Any, Any, list[str], Any]:
        spec = dict(df.attrs.get("data_spec", {}))
        for k in ("target_column", "composition_column"):
            if design.get(k):
                spec[k] = design[k]
        comp_col, target = resolve_columns(df, spec)
        X, feature_names, work = magpie_features(df, composition_col=comp_col)
        y = work[target]
        groups = composition_groups(work)  # chemical-system key per row, aligned with X
        return X, y, feature_names, groups

    # --- train / evaluate ---
    def _make_model(self, design: dict[str, Any]):
        from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

        model = (design.get("model") or "random_forest").lower()
        params = dict(design.get("model_params") or {})
        if model in ("gradient_boosting", "gbm", "gbr"):
            params.setdefault("n_estimators", 200)
            params.setdefault("random_state", 42)
            return GradientBoostingRegressor(**params)
        params.setdefault("n_estimators", 100)
        params.setdefault("random_state", 42)
        params.setdefault("n_jobs", -1)
        return RandomForestRegressor(**params)

    def train_evaluate(
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path, groups: Any = None
    ) -> ExperimentResult:
        import json

        import joblib
        import numpy as np
        from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
        from sklearn.model_selection import RepeatedKFold, cross_validate, train_test_split

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        y_arr = np.asarray(y, dtype=float)
        random_state = int(design.get("random_state", 42))
        test_size = float(design.get("test_size", 0.2))
        model_name = design.get("model") or "random_forest"

        # 1. RepeatedKFold 5x5 -> confidence (mean ± std)
        rkf = RepeatedKFold(n_splits=5, n_repeats=5, random_state=random_state)
        cv = cross_validate(
            self._make_model(design), X, y_arr, cv=rkf,
            scoring=("neg_mean_absolute_error", "r2"), n_jobs=1,
        )
        mae_cv = -cv["test_neg_mean_absolute_error"]
        mae_cv_mean, mae_cv_std = float(mae_cv.mean()), float(mae_cv.std())
        r2_cv_mean = float(cv["test_r2"].mean())

        # 2. leave-chemical-system-out (GroupKFold) -> HEADLINE
        lcso = self._lcso_scores(self._make_model(design), X, y_arr, groups)
        mae_lcso, r2_lcso = lcso["mae"], lcso["r2"]

        # 3. single random holdout -> comparison + parity plot + stratification source
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y_arr, test_size=test_size, random_state=random_state
        )
        model = self._make_model(design)
        model.fit(X_tr, y_tr)
        pred = model.predict(X_te)
        mae_holdout = float(mean_absolute_error(y_te, pred))
        r2_holdout = float(r2_score(y_te, pred))
        rmse_holdout = float(mean_squared_error(y_te, pred) ** 0.5)

        # 4. baseline panel under the SAME grouped CV (Ridge/KNN scaled within folds)
        baselines_lcso = {
            name: self._lcso_scores(est, X, y_arr, groups)["mae"]
            for name, est in self._baseline_models(design).items()
        }
        baselines_lcso[model_name] = mae_lcso  # the chosen model, for reference

        # 5. error stratified by gap range (on the holdout predictions)
        error_by_gap = self._error_by_gap(y_te, pred)

        # final model fit on ALL data + artifacts
        final = self._make_model(design)
        final.fit(X, y_arr)
        model_path = workdir / "model.joblib"
        joblib.dump(final, model_path)
        artifacts: list[dict[str, Any]] = [{"kind": "model", "uri": str(model_path)}]
        try:
            plot_path = workdir / "parity.png"
            self._parity_plot(y_te, pred, plot_path)
            artifacts.append({"kind": "plot", "uri": str(plot_path)})
        except Exception:  # plotting is best-effort; never fail the run on it
            pass

        eval_payload = {
            "protocol": {
                "headline_metric": "mae_lcso",
                "repeated_kfold": {"n_splits": 5, "n_repeats": 5},
                "grouped_cv": {
                    "strategy": "GroupKFold(chemical_system)",
                    "n_splits": lcso["n_splits"],
                    "n_groups": int(lcso["n_groups"]),
                },
                "holdout": {"test_size": test_size, "random_state": random_state},
                "leakage_controls": (
                    "grouped by chemical system; Ridge/KNN scaling fit within each fold"
                ),
            },
            "headline": {"metric": "mae_lcso", "value": mae_lcso},
            "splits": {
                "lcso_groupkfold": {"mae": mae_lcso, "r2": r2_lcso},
                "repeated_kfold_5x5": {
                    "mae_mean": mae_cv_mean, "mae_std": mae_cv_std, "r2_mean": r2_cv_mean,
                },
                "random_holdout": {
                    "mae": mae_holdout, "r2": r2_holdout, "rmse": rmse_holdout,
                    "n_test": int(len(y_te)),
                },
            },
            "baselines_lcso_mae": baselines_lcso,
            "error_by_gap_range": error_by_gap,
        }
        eval_path = workdir / "eval.json"
        eval_path.write_text(json.dumps(eval_payload, indent=2))
        artifacts.append({"kind": "eval", "uri": str(eval_path)})

        metrics = {
            # headline aliases (OPTIMIZE compares + the report leads with these) =
            # the honest leave-chemical-system-out numbers, not the optimistic holdout
            "mae": mae_lcso,
            "r2": r2_lcso,
            "rmse": rmse_holdout,
            # explicit protocol metrics (each persisted as its own ledger row)
            "mae_lcso": mae_lcso,
            "r2_lcso": r2_lcso,
            "mae_cv_mean": mae_cv_mean,
            "mae_cv_std": mae_cv_std,
            "r2_cv_mean": r2_cv_mean,
            "mae_holdout": mae_holdout,
            "r2_holdout": r2_holdout,
            "rmse_holdout": rmse_holdout,
        }
        return ExperimentResult(
            metrics=metrics,
            artifacts=artifacts,
            info={
                "n_train": int(len(X_tr)),
                "n_test": int(len(y_te)),
                "model": model_name,
                "eval_summary": self._eval_summary(eval_payload),
            },
        )

    # --- evaluation helpers ---
    def _lcso_scores(self, estimator: Any, X: Any, y: Any, groups: Any) -> dict[str, Any]:
        """Leave-chemical-system-out scores via GroupKFold out-of-fold predictions.

        Each chemical system sits entirely in one held-out fold, so the score reflects
        generalization to unseen systems rather than interpolation within known ones.
        Falls back to plain KFold when no usable grouping is available.
        """
        import numpy as np
        from sklearn.metrics import mean_absolute_error, r2_score
        from sklearn.model_selection import GroupKFold, KFold, cross_val_predict

        n_groups = int(len(np.unique(groups))) if groups is not None else len(y)
        n_splits = max(2, min(5, n_groups))
        if groups is not None and n_groups >= 2:
            cv = GroupKFold(n_splits=n_splits)
            oof = cross_val_predict(estimator, X, y, cv=cv, groups=groups, n_jobs=1)
        else:
            cv = KFold(n_splits=n_splits, shuffle=True, random_state=42)
            oof = cross_val_predict(estimator, X, y, cv=cv, n_jobs=1)
        return {
            "mae": float(mean_absolute_error(y, oof)),
            "r2": float(r2_score(y, oof)),
            "n_groups": n_groups,
            "n_splits": n_splits,
        }

    def _baseline_models(self, design: dict[str, Any]) -> dict[str, Any]:
        from sklearn.dummy import DummyRegressor
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.linear_model import Ridge
        from sklearn.neighbors import KNeighborsRegressor
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        rs = int(design.get("random_state", 42))
        return {
            "dummy_mean": DummyRegressor(strategy="mean"),
            "ridge": make_pipeline(StandardScaler(), Ridge()),
            "knn": make_pipeline(StandardScaler(), KNeighborsRegressor()),
            "gbm": GradientBoostingRegressor(n_estimators=200, max_depth=3, random_state=rs),
        }

    @staticmethod
    def _error_by_gap(y_true: Any, pred: Any) -> list[dict[str, Any]]:
        import numpy as np

        y_true = np.asarray(y_true, dtype=float)
        err = np.abs(np.asarray(pred, dtype=float) - y_true)
        rows: list[dict[str, Any]] = []
        for label, lo, hi in _GAP_BINS:
            mask = (y_true >= lo) & (y_true < hi)
            n = int(mask.sum())
            rows.append({"range": label, "n": n, "mae": float(err[mask].mean()) if n else None})
        return rows

    @staticmethod
    def _eval_summary(payload: dict[str, Any]) -> str:
        s = payload["splits"]
        lc, rk, ho = s["lcso_groupkfold"], s["repeated_kfold_5x5"], s["random_holdout"]
        bl_str = ", ".join(f"{k} {v:.3f}" for k, v in payload["baselines_lcso_mae"].items())
        gap_str = "; ".join(
            f"{r['range']} n={r['n']} MAE={r['mae']:.3f}"
            for r in payload["error_by_gap_range"]
            if r["mae"] is not None
        )
        return (
            f"LCSO(GroupKFold) MAE {lc['mae']:.3f} eV, R² {lc['r2']:.3f} (HEADLINE) | "
            f"RepeatedKFold 5x5 MAE {rk['mae_mean']:.3f}±{rk['mae_std']:.3f} | "
            f"random holdout MAE {ho['mae']:.3f}, RMSE {ho['rmse']:.3f} | "
            f"baselines(LCSO MAE): {bl_str} | errors by gap: {gap_str}"
        )

    @staticmethod
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

    def baselines(self) -> list[dict[str, Any]]:
        return [
            {"model": "random_forest", "model_params": {"n_estimators": 100}, "featurizer": "magpie"},
            {
                "model": "gradient_boosting",
                "model_params": {"n_estimators": 200, "max_depth": 3},
                "featurizer": "magpie",
            },
        ]
