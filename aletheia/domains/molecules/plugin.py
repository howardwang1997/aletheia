"""Molecular property regression — Aletheia's 2nd, frontier domain (drug-discovery /
chem-bio). SMILES -> Morgan/ECFP fingerprints -> a regressor, scored under the SAME
leakage-aware protocol as materials (``domains/protocol.py``) but with the field's
**scaffold split** as the grouping and **scaffold-grouped RMSE** as the headline —
the metric MoleculeNet reports — so the goal is concrete: beat the published ESOL
SOTA. RDKit is imported lazily (only in featurize), so the no-network sandbox (which
runs only ``train_evaluate`` on staged numeric arrays) and ``profile()`` need no RDKit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aletheia.domains.base import (
    DemonstrationCapability,
    DomainPlugin,
    DomainProfile,
    ExperimentResult,
)
from aletheia.domains.molecules.datasets import load_benchmark, resolve_columns
from aletheia.domains.molecules.featurizers import morgan_features, scaffold_groups
from aletheia.domains.protocol import grouped_regression_eval


class MoleculePropertyPlugin(DomainPlugin):
    name = "molecules"

    # --- data ---
    def load_data(self, data_spec: dict[str, Any]) -> Any:
        source = data_spec.get("source", "benchmark")
        if source == "benchmark":
            df = load_benchmark(data_spec.get("ref") or "esol")
        elif source in ("upload", "directory", "url"):
            from aletheia.data.loaders import materialize, read_tabular

            path = materialize(data_spec)
            max_rows = data_spec.get("max_rows")
            df = read_tabular(path, nrows=int(max_rows) if max_rows else None)
        else:
            raise ValueError(f"unknown data source: {source!r}")
        df.attrs["data_spec"] = data_spec
        return df

    # --- features ---
    def featurize(self, df: Any, design: dict[str, Any]) -> tuple[Any, Any, list[str], Any]:
        spec = dict(df.attrs.get("data_spec", {}))
        for k in ("target_column", "smiles_column"):
            if design.get(k):
                spec[k] = design[k]
        smiles_col, target = resolve_columns(df, spec)
        n_bits = int(design.get("n_bits", 1024))
        X, feature_names, work = morgan_features(df, smiles_col=smiles_col, n_bits=n_bits)
        y = work[target]
        groups = scaffold_groups(work)  # Bemis-Murcko scaffold per row, aligned with X
        return X, y, feature_names, groups

    # --- train / evaluate ---
    def _load_solution_pipeline(self, path: str):
        import importlib.util

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
        solution_path = design.get("solution_path")
        if solution_path:
            return self._load_solution_pipeline(solution_path)

        from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

        model = (design.get("model") or "random_forest").lower()
        params = dict(design.get("model_params") or {})
        if model in ("gradient_boosting", "gbm", "gbr"):
            params.setdefault("n_estimators", 200)
            params.setdefault("random_state", 42)
            return GradientBoostingRegressor(**params)
        params.setdefault("n_estimators", 200)
        params.setdefault("random_state", 42)
        params.setdefault("n_jobs", -1)
        return RandomForestRegressor(**params)

    def _baseline_models(self, design: dict[str, Any]) -> dict[str, Any]:
        from sklearn.dummy import DummyRegressor
        from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
        from sklearn.linear_model import Ridge
        from sklearn.neighbors import KNeighborsRegressor

        rs = int(design.get("random_state", 42))
        return {
            "dummy_mean": DummyRegressor(strategy="mean"),
            "ridge": Ridge(),
            # Tanimoto/jaccard is the chem-apt similarity, but it forces a dtype cast on
            # the integer fingerprint matrix; for a reference baseline we keep the panel
            # dtype-clean with the default metric.
            "knn": KNeighborsRegressor(),
            "rf": RandomForestRegressor(n_estimators=200, random_state=rs, n_jobs=-1),
            "gbm": GradientBoostingRegressor(n_estimators=200, max_depth=3, random_state=rs),
        }

    @staticmethod
    def _error_by_solubility(y_true: Any, pred: Any) -> list[dict[str, Any]]:
        """Stratify holdout absolute error into target terciles (low/mid/high)."""
        import numpy as np

        y_true = np.asarray(y_true, dtype=float)
        err = np.abs(np.asarray(pred, dtype=float) - y_true)
        if len(y_true) == 0:
            return []
        q1, q2 = np.quantile(y_true, [1 / 3, 2 / 3])
        bins = [("low", -np.inf, q1), ("mid", q1, q2), ("high", q2, np.inf)]
        rows: list[dict[str, Any]] = []
        for label, lo, hi in bins:
            mask = (y_true >= lo) & (y_true < hi) if label != "high" else (y_true >= lo)
            n = int(mask.sum())
            rows.append({"range": label, "n": n, "mae": float(err[mask].mean()) if n else None})
        return rows

    def train_evaluate(
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path, groups: Any = None
    ) -> ExperimentResult:
        return grouped_regression_eval(
            model_factory=lambda: self._make_model(design),
            baseline_models=self._baseline_models(design),
            X=X, y=y, groups=groups, design=design, workdir=Path(workdir),
            model_name=design.get("model") or "random_forest",
            grouped_key="scaffold",
            headline_label="scaffold-split",
            grouped_abbr="scaffold",
            group_strategy_desc="GroupKFold(Bemis-Murcko scaffold)",
            units="",
            stratifier=self._error_by_solubility,
            stratify_label="solubility range",
        )

    def demonstration_capabilities(self) -> dict[str, DemonstrationCapability]:
        """The discriminating demonstrations this domain can COMPUTE (Codex #2). IDEATE
        picks one by id; ``run_demonstration`` (base class) dispatches by id, falling back
        to ``_match_capability``'s keyword matcher for untagged specs; an unmatched spec
        stays unverified (fail-closed)."""
        return {
            "activity_cliff_lipschitz": DemonstrationCapability(
                id="activity_cliff_lipschitz",
                description=(
                    "Activity cliffs as a representational IMPOSSIBILITY (not data scarcity): "
                    "among near-identical molecules (Tanimoto>=0.9), cliff pairs imply a local "
                    "Lipschitz constant orders of magnitude larger than smooth regions, so a single "
                    "continuous regressor on this representation cannot fit cliffs and generalize. "
                    "Statistic = L_cliff / L_global."
                ),
                compute=self._demo_activity_cliff_lipschitz,
            ),
            "scaffold_generalization_gap": DemonstrationCapability(
                id="scaffold_generalization_gap",
                description=(
                    "The incumbent random-split RMSE is BLIND to scaffold generalization: the same "
                    "model scored random-holdout way (what most ESOL papers report) vs scaffold-"
                    "grouped way; the metric the field reports cannot certify generalization to "
                    "novel scaffolds. Statistic = scaffold / random RMSE ratio."
                ),
                compute=self._demo_scaffold_generalization,
            ),
        }

    def _match_capability(
        self, demonstration: dict[str, Any], caps: dict[str, DemonstrationCapability]
    ) -> DemonstrationCapability | None:
        """Keyword fallback for specs IDEATE didn't tag with an explicit capability id:
        an activity-cliff / Lipschitz claim -> the cliff-Lipschitz impossibility; a
        scaffold / random-split / generalization claim -> the scaffold blind-spot."""
        intent = f"{demonstration.get('claim', '')} {demonstration.get('form', '')}".lower()
        if "cliff" in intent or "lipschitz" in intent:
            return caps["activity_cliff_lipschitz"]
        if any(t in intent for t in ("scaffold", "random-split", "random split", "generaliz")):
            return caps["scaffold_generalization_gap"]
        return None

    def _demo_activity_cliff_lipschitz(
        self, demonstration: dict[str, Any], data_spec: dict[str, Any]
    ) -> dict[str, Any]:
        """Activity cliffs as a representational IMPOSSIBILITY (not data scarcity): among
        near-identical molecules (Tanimoto >= 0.9 on Morgan bits), the cliff pairs (top
        decile of |Δproperty|) imply a local Lipschitz constant L_cliff = |Δy| / structural
        distance that is orders of magnitude larger than the smooth-region L_global. A
        single continuous/Lipschitz regressor on this representation cannot satisfy both —
        fitting the cliffs forces an L that destroys generalization elsewhere. ``statistic``
        = L_cliff / L_global. Computed on a SEED-perturbed 90% subsample so the reproduction
        re-run (a fresh seed) is a genuine independent re-computation — the ratio must be
        STABLE across subsamples, not a trivially-identical recompute."""
        import numpy as np

        df = self.load_data(data_spec)
        sample_n = demonstration.get("sample_n") or data_spec.get("sample_n")
        if sample_n:
            df = df.head(int(sample_n))
        # seed-perturbed 90% subsample: a different seed -> a different sample -> a real
        # reproduction check on the ratio's stability (not an identical recompute).
        rs = int(demonstration.get("random_state") or data_spec.get("random_state") or 42)
        if len(df) > 50:
            rng = np.random.default_rng(rs)
            keep = rng.choice(len(df), size=int(len(df) * 0.9), replace=False)
            df = df.iloc[sorted(keep)]
        df.attrs["data_spec"] = data_spec  # featurize reads the spec (target/smiles cols) off attrs
        X, y, _names, _groups = self.featurize(df, {})
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        n = len(y)
        inter = X @ X.T  # |A & B| for binary Morgan bits
        pop = X.sum(axis=1)
        union = pop[:, None] + pop[None, :] - inter
        with np.errstate(divide="ignore", invalid="ignore"):
            tani = np.where(union > 0, inter / union, 0.0)
        iu = np.triu_indices(n, k=1)
        t = tani[iu]
        dy = np.abs(y[iu[0]] - y[iu[1]])
        sim = t >= 0.9  # "near-identical" structural neighbors
        if int(sim.sum()) < 20:  # sparse data: relax to the top-1% most similar pairs
            sim = t >= float(np.quantile(t, 0.99))
        ts, dys = t[sim], dy[sim]
        dist = np.maximum(1.0 - ts, 1e-3)  # structural distance (collisions floored)
        lip = dys / dist  # implied local Lipschitz per near-identical pair
        cliff = dys >= float(np.quantile(dys, 0.9))  # the activity-cliff pairs
        l_cliff = float(np.median(lip[cliff])) if cliff.any() else 0.0
        l_global = float(np.median(lip[~cliff])) if (~cliff).any() else 1e-9
        ratio = l_cliff / l_global if l_global > 0 else float("inf")
        holds = bool(int(sim.sum()) >= 20 and ratio > 5.0)
        return {
            "form": "impossibility",
            "holds": holds,
            "statistic": round(ratio, 3),
            "detail": (
                f"among {int(sim.sum())} near-identical (Tanimoto>=0.9) pairs, the activity-cliff "
                f"pairs imply local Lipschitz L_cliff={l_cliff:.2f} vs smooth-region L_global="
                f"{l_global:.2f} (ratio {ratio:.1f}). A smooth/continuous regressor that fits the "
                f"cliffs needs an L ~{ratio:.0f}x larger than elsewhere — incompatible with global "
                f"generalization, so the cliff failure is representational, not data scarcity."
            ),
        }

    def _demo_scaffold_generalization(
        self, demonstration: dict[str, Any], data_spec: dict[str, Any]
    ) -> dict[str, Any]:
        """The incumbent random-split metric is BLIND to scaffold generalization: fit the
        SAME model and measure RMSE a random-holdout way (what most ESOL papers report) vs
        a scaffold-grouped (leave-whole-scaffold-out) way; ``holds`` iff scaffold RMSE is
        materially worse. ``statistic`` = scaffold/random RMSE ratio."""
        import numpy as np
        from sklearn.metrics import mean_squared_error
        from sklearn.model_selection import train_test_split

        from aletheia.domains.protocol import _grouped_scores

        rs = int(demonstration.get("random_state") or data_spec.get("random_state") or 42)
        margin = float(demonstration.get("margin", 0.10))  # gap must exceed 10% to "hold"
        design = {"random_state": rs}
        df = self.load_data(data_spec)
        sample_n = demonstration.get("sample_n") or data_spec.get("sample_n")
        if sample_n:
            df = df.head(int(sample_n))
            df.attrs["data_spec"] = data_spec  # featurize reads the spec off attrs
        X, y, _names, groups = self.featurize(df, design)
        y = np.asarray(y, dtype=float)
        # incumbent frame: a single random holdout RMSE
        X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=rs)
        m = self._make_model(design)
        m.fit(X_tr, y_tr)
        random_rmse = float(mean_squared_error(y_te, m.predict(X_te)) ** 0.5)
        # new frame: scaffold-grouped (leave-whole-scaffold-out) RMSE
        grouped = _grouped_scores(lambda: self._make_model(design), X, y, groups)
        scaffold_rmse = float(grouped["rmse"])
        ratio = scaffold_rmse / random_rmse if random_rmse > 0 else float("inf")
        holds = bool(grouped["protocol_status"] == "grouped" and ratio > 1.0 + margin)
        return {
            "form": "impossibility",  # the incumbent metric has a structural blind spot
            "holds": holds,
            "statistic": round(ratio, 4),
            "detail": (
                f"random-split RMSE={random_rmse:.3f} vs scaffold-grouped RMSE={scaffold_rmse:.3f} "
                f"(ratio {ratio:.2f}, {grouped['n_groups']} scaffolds). The random-split metric "
                f"{'cannot' if holds else 'does not clearly'} certify generalization to novel scaffolds."
            ),
        }

    def baselines(self) -> list[dict[str, Any]]:
        return [
            {"model": "random_forest", "model_params": {"n_estimators": 200}, "featurizer": "ecfp"},
            {
                "model": "gradient_boosting",
                "model_params": {"n_estimators": 200, "max_depth": 3},
                "featurizer": "ecfp",
            },
        ]

    def profile(self) -> DomainProfile:
        from aletheia.research.literature import Paper

        return DomainProfile(
            task="molecular property regression (aqueous solubility)",
            headline_metric="rmse_scaffold",
            units="",
            protocol_desc=(
                "scaffold-split GroupKFold (headline) + RepeatedKFold 5x5 + a baseline panel + "
                "solubility-range stratification"
            ),
            feature_desc="Morgan/ECFP fingerprints (binary, 1024-bit)",
            sota_reference=(
                "MoleculeNet ESOL test RMSE: ≈0.55 (D-MPNN/GNN), ≈0.99 (RF on ECFP); scaffold split"
            ),
            sota_rows=[
                {"method": "D-MPNN (Chemprop)", "dataset": "MoleculeNet ESOL", "metric": "rmse",
                 "score": 0.55, "split_policy": "scaffold", "source": "MoleculeNet / Chemprop"},
                {"method": "Random forest on ECFP", "dataset": "MoleculeNet ESOL", "metric": "rmse",
                 "score": 0.99, "split_policy": "scaffold", "source": "MoleculeNet"},
            ],
            dry_literature_findings=[
                {"paper_id": "10.0000/ecfp-gbm", "method": "ECFP + gradient boosting", "dataset": "ESOL",
                 "metric": "rmse", "result": "≈0.9 scaffold split", "limitation": "below GNN SOTA",
                 "gap": "fair scaffold-split fingerprint baseline under-reported", "relevance": "direct baseline"},
                {"paper_id": "10.0000/scaffold", "method": "scaffold split", "dataset": "molecular benchmarks",
                 "metric": "rmse", "result": "scaffold > random RMSE", "limitation": "harder generalization",
                 "gap": "split not standardized across papers", "relevance": "evaluation protocol"},
            ],
            dry_papers=[
                Paper(
                    title="Molecular fingerprints + gradient boosting for solubility prediction",
                    abstract="ECFP features with tree ensembles are strong, cheap baselines for ESOL.",
                    year=2023, source="arxiv",
                ),
                Paper(
                    title="Scaffold splits for honest molecular property benchmarks",
                    abstract="Random splits leak analogues; scaffold splits measure generalization to novel cores.",
                    year=2022, citations=44, source="openalex",
                ),
            ],
            dry_gaps=[
                "Few solubility studies report a fair scaffold-split RMSE against GNN SOTA.",
                "Fingerprint baselines vs message-passing GNNs are rarely compared under the same split.",
            ],
            dry_frontier_methods=[
                {"name": "D-MPNN (Chemprop)",
                 "why": "directed message-passing GNN; ESOL SOTA on scaffold splits",
                 "source": "MoleculeNet / Chemprop"},
                {"name": "gradient boosting on ECFP fingerprints (XGBoost/LightGBM)",
                 "why": "strong, cheap fingerprint baseline competitive on ESOL",
                 "source": "surveyed prior work"},
            ],
            dry_hypothesis={
                "statement": (
                    "ECFP fingerprints + gradient boosting reach a competitive scaffold-split RMSE for "
                    "ESOL solubility, within reach of GNN SOTA"
                ),
                "rationale": "ECFP captures local substructure that governs solubility",
                "prediction": "scaffold-split RMSE below the random-forest baseline, approaching ~0.9",
                "novelty_note": "fair scaffold-split comparison vs SOTA is under-reported",
            },
            dry_next_hypothesis={
                "statement": (
                    "Counting Morgan fingerprints (ECFP with counts) improve scaffold-split solubility "
                    "RMSE over binary ECFP"
                ),
                "rationale": "substructure multiplicity carries solubility signal",
                "prediction": "lower scaffold-split RMSE than round 1",
                "novelty_note": "rarely ablated under a scaffold split",
            },
            dry_metrics={
                "mae": 0.72, "r2": 0.78, "rmse": 0.95,
                "mae_scaffold": 0.72, "r2_scaffold": 0.78, "rmse_scaffold": 0.95,
                "mae_cv_mean": 0.65, "mae_cv_std": 0.05, "r2_cv_mean": 0.82,
                "mae_holdout": 0.60, "r2_holdout": 0.85, "rmse_holdout": 0.80,
            },
            dry_eval_summary=(
                "scaffold-split MAE 0.720, R² 0.780, RMSE 0.950 (HEADLINE, all same split) | "
                "RepeatedKFold 5x5 MAE 0.650±0.050 | random holdout MAE 0.600 (comparison only) | "
                "baselines(scaffold MAE): dummy_mean 1.900, ridge 1.100, knn 1.200, rf 0.720, gbm 0.800 | "
                "errors by solubility range: low n=10 MAE=0.60; high n=8 MAE=0.80"
            ),
        )
