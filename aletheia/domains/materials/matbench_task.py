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

from aletheia.domains.base import DomainPlugin, DomainProfile, ExperimentResult
from aletheia.domains.materials.datasets import load_benchmark, resolve_columns
from aletheia.domains.materials.featurizers import composition_groups, magpie_features
from aletheia.domains.protocol import grouped_regression_eval

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
    def _load_solution_pipeline(self, path: str):
        """Load a coder-authored ``build_pipeline()`` estimator, re-checking the
        AST gate (defense in depth — the driver already gated it before submit)."""
        import importlib.util
        from pathlib import Path

        from aletheia.coder.sandbox import check_code

        src = Path(path).read_text()
        ok, reasons = check_code(src)
        if not ok:
            raise RuntimeError(f"solution failed the code gate: {reasons}")
        spec = importlib.util.spec_from_file_location("aletheia_solution", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.build_pipeline()

    def _make_model(self, design: dict[str, Any]):
        # a coder-authored solution overrides the fixed model; it runs through the
        # identical LCSO/CV/holdout/baseline protocol below (no special-casing).
        solution_path = design.get("solution_path")
        if solution_path:
            # harness-owned seed: override the solution's hardcoded random_state so the
            # reproduction re-run (design random_state+1) is a REAL re-computation.
            from aletheia.domains.base import reseed_estimator

            return reseed_estimator(
                self._load_solution_pipeline(solution_path),
                int(design.get("random_state", 42)),
            )

        from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

        from aletheia.domains.base import filter_estimator_params

        model = (design.get("model") or "random_forest").lower()
        params = dict(design.get("model_params") or {})
        rs = int(design.get("random_state", 42))  # harness seed (reproduction sets rs+1)
        if model in ("gradient_boosting", "gbm", "gbr"):
            params = filter_estimator_params(GradientBoostingRegressor, params)
            params.setdefault("n_estimators", 200)
            params.setdefault("random_state", rs)
            return GradientBoostingRegressor(**params)
        params = filter_estimator_params(RandomForestRegressor, params)
        params.setdefault("n_estimators", 100)
        params.setdefault("random_state", rs)
        params.setdefault("n_jobs", -1)
        return RandomForestRegressor(**params)

    def train_evaluate(
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path, groups: Any = None
    ) -> ExperimentResult:
        # The leakage-aware protocol is shared across domains (domains/protocol.py);
        # materials supplies only the model/baseline factories, the chemical-system
        # grouping (passed in via ``groups``), the gap-range stratifier, and labels.
        return grouped_regression_eval(
            model_factory=lambda: self._make_model(design),
            baseline_models=self._baseline_models(design),
            X=X, y=y, groups=groups, design=design, workdir=Path(workdir),
            model_name=design.get("model") or "random_forest",
            grouped_key="lcso",
            headline_label="LCSO(GroupKFold)",
            grouped_abbr="LCSO",
            group_strategy_desc="GroupKFold(chemical_system)",
            units="eV",
            stratifier=self._error_by_gap,
            stratify_label="gap",
        )

    # --- evaluation helpers ---
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

    def baselines(self) -> list[dict[str, Any]]:
        return [
            {"model": "random_forest", "model_params": {"n_estimators": 100}, "featurizer": "magpie"},
            {
                "model": "gradient_boosting",
                "model_params": {"n_estimators": 200, "max_depth": 3},
                "featurizer": "magpie",
            },
        ]

    def profile(self) -> DomainProfile:
        from aletheia.research.literature import Paper

        return DomainProfile(
            task="composition→property regression",
            headline_metric="mae_lcso",
            units="eV",
            protocol_desc=(
                "leave-chemical-system-out GroupKFold (headline) + RepeatedKFold 5x5 + a "
                "baseline panel + gap-range stratification"
            ),
            feature_desc="Magpie composition features (numeric, ~130 features)",
            sota_reference="matbench_expt_gap leaderboard: best test MAE ≈ 0.33 eV",
            sota_rows=[
                {"method": "MODNet (structure/composition)", "dataset": "matbench_expt_gap",
                 "metric": "mae", "score": 0.33, "split_policy": "matbench 5-fold",
                 "source": "Matbench leaderboard"},
                {"method": "CrabNet (composition transformer)", "dataset": "matbench_expt_gap",
                 "metric": "mae", "score": 0.35, "split_policy": "matbench 5-fold",
                 "source": "Matbench leaderboard"},
            ],
            dry_literature_findings=[
                {"paper_id": "10.0000/magpie", "method": "Magpie + tree models", "dataset": "matbench_expt_gap",
                 "metric": "mae", "result": "≈0.45 eV random split", "limitation": "random split is optimistic",
                 "gap": "no fair LCSO baseline reported", "relevance": "direct baseline"},
                {"paper_id": "10.0000/leakage", "method": "leave-chemical-system-out eval", "dataset": "materials ML",
                 "metric": "mae", "result": "LCSO > random by ~0.1 eV", "limitation": "few datasets",
                 "gap": "rarely adopted as headline", "relevance": "evaluation protocol"},
            ],
            dry_papers=[
                Paper(
                    title="Magpie composition features for band-gap regression",
                    abstract="Composition-only descriptors predict band gaps; tree models are strong baselines.",
                    year=2024, source="arxiv",
                ),
                Paper(
                    title="Leakage-aware evaluation for materials ML",
                    abstract="Random splits overstate performance; leave-chemical-system-out is the honest metric.",
                    year=2023, citations=31, source="openalex",
                ),
            ],
            dry_gaps=[
                "Few works report a fair leave-chemical-system-out baseline.",
                "Composition-only vs structure-aware features are rarely compared honestly.",
            ],
            dry_frontier_methods=[
                {"name": "structure GNNs (CGCNN / MEGNet / M3GNet)",
                 "why": "structure-aware message passing sets the Matbench SOTA for band gaps",
                 "source": "Matbench leaderboard"},
                {"name": "gradient boosting on composition descriptors (Magpie + XGBoost/LightGBM)",
                 "why": "strong composition-only baseline when structure is unavailable",
                 "source": "surveyed prior work"},
            ],
            dry_hypothesis={
                "statement": (
                    "Magpie features + gradient boosting beat a leakage-aware LCSO baseline for "
                    "experimental band-gap regression"
                ),
                "rationale": "GBM captures nonlinear composition-property relations",
                "prediction": "LCSO MAE below the random-forest baseline",
                "novelty_note": "Few prior works report a fair LCSO comparison",
            },
            dry_next_hypothesis={
                "statement": (
                    "Adding oxidation-state features improves LCSO band-gap accuracy over "
                    "composition-only Magpie features"
                ),
                "rationale": "valence governs gap size",
                "prediction": "lower LCSO MAE than round 1",
                "novelty_note": "rarely tested under LCSO",
            },
            dry_metrics={
                "mae": 0.63, "r2": 0.41, "rmse": 0.84,
                "mae_lcso": 0.63, "r2_lcso": 0.41, "rmse_lcso": 0.84,
                "mae_cv_mean": 0.49, "mae_cv_std": 0.03, "r2_cv_mean": 0.65,
                "mae_holdout": 0.49, "r2_holdout": 0.65, "rmse_holdout": 0.61,
            },
            dry_eval_summary=(
                "LCSO(GroupKFold) MAE 0.630 eV, R² 0.410, RMSE 0.840 (HEADLINE, all same split) | "
                "RepeatedKFold 5x5 MAE 0.490±0.030 | random holdout MAE 0.490 (comparison only) | "
                "baselines(LCSO MAE): dummy_mean 1.100, ridge 0.850, knn 0.780, gbm 0.610, "
                "random_forest 0.630 | errors by gap: metal~0 n=10 MAE=0.20; [0.5,2) n=8 MAE=0.55"
            ),
        )
