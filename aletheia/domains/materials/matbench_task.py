"""Materials band-gap regression — Aletheia's Phase-1 thin-slice domain.

Composition -> Magpie features -> tree regressor -> MAE/R²/RMSE on a held-out
split. CPU-only, offline-capable (the unit test never downloads), and canonical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aletheia.domains.base import DomainPlugin, ExperimentResult
from aletheia.domains.materials.datasets import load_benchmark, resolve_columns
from aletheia.domains.materials.featurizers import magpie_features


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
    def featurize(self, df: Any, design: dict[str, Any]) -> tuple[Any, Any, list[str]]:
        spec = dict(df.attrs.get("data_spec", {}))
        for k in ("target_column", "composition_column"):
            if design.get(k):
                spec[k] = design[k]
        comp_col, target = resolve_columns(df, spec)
        X, feature_names, work = magpie_features(df, composition_col=comp_col)
        y = work[target]
        return X, y, feature_names

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
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path
    ) -> ExperimentResult:
        import joblib
        from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
        from sklearn.model_selection import train_test_split

        test_size = float(design.get("test_size", 0.2))
        random_state = int(design.get("random_state", 42))
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=test_size, random_state=random_state
        )
        model = self._make_model(design)
        model.fit(X_tr, y_tr)
        pred = model.predict(X_te)

        mae = float(mean_absolute_error(y_te, pred))
        r2 = float(r2_score(y_te, pred))
        rmse = float(mean_squared_error(y_te, pred) ** 0.5)

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, Any]] = []
        model_path = workdir / "model.joblib"
        joblib.dump(model, model_path)
        artifacts.append({"kind": "model", "uri": str(model_path)})
        try:
            plot_path = workdir / "parity.png"
            self._parity_plot(y_te, pred, plot_path)
            artifacts.append({"kind": "plot", "uri": str(plot_path)})
        except Exception:  # plotting is best-effort; never fail the run on it
            pass

        return ExperimentResult(
            metrics={"mae": mae, "r2": r2, "rmse": rmse},
            artifacts=artifacts,
            info={
                "n_train": int(len(X_tr)),
                "n_test": int(len(X_te)),
                "model": (design.get("model") or "random_forest"),
            },
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
