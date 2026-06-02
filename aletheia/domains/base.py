"""DomainPlugin — the pluggable seam between Aletheia's reasoning loop and a
concrete scientific task. Phase 1 ships one: materials band-gap regression.

A plugin turns a *design* (the concrete spec the agent commits to) + a *data spec*
(a resolved ``DataAsset``) into metrics + artifacts, without the orchestrator ever
touching raw compute in its own context — the compute adapter runs the plugin in a
subprocess and the plugin reports back through this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExperimentResult:
    """What a single train/evaluate run produces. JSON-serializable via ``to_dict``."""

    metrics: dict[str, float] = field(default_factory=dict)  # {"mae":.., "r2":.., "rmse":..}
    artifacts: list[dict[str, Any]] = field(default_factory=list)  # [{kind, uri, ...}]
    info: dict[str, Any] = field(default_factory=dict)  # n_train/n_test/feature_count/model/...

    def to_dict(self) -> dict[str, Any]:
        return {"metrics": self.metrics, "artifacts": self.artifacts, "info": self.info}


@dataclass
class DomainProfile:
    """Everything the (domain-agnostic) reasoning loop needs to phrase a run in a
    domain's own terms — so the driver, compute dry-run, and coder contract carry NO
    materials-specific literals. Each plugin returns one via ``DomainPlugin.profile``.

    ``headline_metric`` is the metrics-dict key the loop optimizes + reports (e.g.
    ``mae_lcso`` for materials, ``rmse_scaffold`` for molecules). ``sota_reference``
    is the field's published benchmark number — it makes "beat the SOTA" concrete in
    the analysis + write-up. The ``dry_*`` fields are the canned, offline content the
    dry-run path uses (no network, no spend)."""

    task: str  # e.g. "composition→property regression"
    headline_metric: str  # metrics key the loop leads with / optimizes
    # whether a BETTER headline is lower (error metrics: mae/rmse) or higher (f1/recall):
    headline_goal: str = "min"  # "min" | "max"
    # whether the domain scores a subjective quality dimension via the cross-vendor
    # critic panel (a `faithfulness` metric), in addition to deterministic metrics:
    quality_via_critics: bool = False
    # whether the eval runs HOST-SIDE in the driver (trusted: keys + network for a real
    # LLM step) rather than in the compute-backend sandbox. Scoring stays deterministic.
    host_side_run: bool = False
    # the artifact kinds a REAL run must emit to be verifiable; the post-execution guard
    # pauses a real run missing any of them. Regression domains fit + score a model
    # (model + eval); host-side eval-only domains (e.g. RAG) emit just eval.
    required_artifacts: tuple[str, ...] = ("eval", "model")
    units: str = ""  # e.g. "eV"; "" for unitless targets
    protocol_desc: str = "grouped cross-validation + holdout + baseline panel"
    feature_desc: str = "a numeric feature matrix"
    sota_reference: str = "no comparable published benchmark"
    # structured form of ``sota_reference`` — published SOTA rows for this domain so
    # "beat the SOTA" is a row comparison, not prose. Each: {method, dataset, metric,
    # score, split_policy, source}.
    sota_rows: list[dict[str, Any]] = field(default_factory=list)
    dry_papers: list[Any] = field(default_factory=list)  # list[research.literature.Paper]
    # canned structured literature findings for the dry-run path (offline). Each:
    # {paper_id, method, dataset, metric, result, limitation, gap, relevance}.
    dry_literature_findings: list[dict[str, Any]] = field(default_factory=list)
    dry_gaps: list[str] = field(default_factory=list)
    dry_frontier_methods: list[dict[str, str]] = field(default_factory=list)  # [{name, why, source}]
    dry_hypothesis: dict[str, Any] = field(default_factory=dict)
    dry_next_hypothesis: dict[str, Any] = field(default_factory=dict)
    dry_metrics: dict[str, float] = field(default_factory=dict)
    dry_eval_summary: str = ""


class DomainPlugin(ABC):
    """Contract every domain implements. Steps are separable so they can be unit
    tested offline (featurize/train on a tiny in-memory frame) and also composed by
    ``run_experiment`` for subprocess execution."""

    name: str = "base"

    @abstractmethod
    def load_data(self, data_spec: dict[str, Any]) -> Any:
        """Resolve a data spec (source benchmark|upload|api) to a DataFrame."""

    @abstractmethod
    def featurize(self, df: Any, design: dict[str, Any]) -> tuple[Any, Any, list[str], Any]:
        """Return ``(X, y, feature_names, groups)`` for the given design.

        ``groups`` is a per-row grouping key (aligned with ``X``/``y``) used for
        leakage-aware grouped cross-validation; return ``None`` if the domain has no
        natural grouping.
        """

    @abstractmethod
    def train_evaluate(
        self, X: Any, y: Any, design: dict[str, Any], workdir: Path, groups: Any = None
    ) -> ExperimentResult:
        """Fit the design's model, score under a grouped/cross-validated protocol,
        save artifacts."""

    @abstractmethod
    def baselines(self) -> list[dict[str, Any]]:
        """Candidate model/feature specs the agent can choose among / compare."""

    @abstractmethod
    def profile(self) -> DomainProfile:
        """Domain vocabulary + canned dry-run content for the reasoning loop."""

    def run_experiment(
        self, design: dict[str, Any], data_spec: dict[str, Any], workdir: Path
    ) -> ExperimentResult:
        """End-to-end load -> featurize -> train/evaluate. Used by the subprocess
        training script generated by the compute adapter."""
        df = self.load_data(data_spec)
        X, y, feature_names, groups = self.featurize(df, design)
        result = self.train_evaluate(X, y, design, Path(workdir), groups=groups)
        result.info.setdefault("feature_count", len(feature_names))
        result.info.setdefault("n_rows", int(getattr(df, "shape", [0])[0]))
        return result

    def run_demonstration(
        self, demonstration: dict[str, Any], data_spec: dict[str, Any], workdir: Path
    ) -> dict[str, Any] | None:
        """Compute a PARADIGM contribution's discriminating demonstration DETERMINISTICALLY
        (harness-owned — never an LLM 'holds' assertion): does the new frame reveal/measure
        something the incumbent provably cannot? Return
        ``{form, holds: bool, statistic: float|None, detail: str}`` or ``None`` if the
        domain has no computable demonstration. Default: no demonstration available."""
        return None
