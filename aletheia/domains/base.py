"""DomainPlugin — the pluggable seam between Aletheia's reasoning loop and a
concrete scientific task. Phase 1 ships one: materials band-gap regression.

A plugin turns a *design* (the concrete spec the agent commits to) + a *data spec*
(a resolved ``DataAsset``) into metrics + artifacts, without the orchestrator ever
touching raw compute in its own context — the compute adapter runs the plugin in a
subprocess and the plugin reports back through this contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
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


@dataclass
class DemonstrationCapability:
    """A registered, harness-IMPLEMENTED discriminating demonstration a domain can compute
    deterministically (Codex #2). Paradigm grounding dispatches to these by ``id`` — an
    explicit, enumerable contract — instead of free-text keyword guessing, so what the
    harness can actually demonstrate is knowable up front (IDEATE chooses from this menu)
    and an unrecognized request stays UNVERIFIED rather than being silently matched to an
    unrelated computation.

    ``compute(demonstration, data_spec) -> {form, holds, statistic, detail, ...} | None``
    is harness-owned (never an LLM 'holds' assertion). ``reproduce_factor`` is the
    per-capability reproduction tolerance (Codex #5): the demonstration's statistic must
    stay within this multiplicative factor across the seed-perturbed re-run (default 2x —
    order-of-magnitude stability, the right check for an impossibility-style ratio; a
    capability with a tighter, well-behaved statistic can lower it)."""

    id: str
    description: str  # what it computes + which incumbent frame it discriminates against
    compute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]
    reproduce_factor: float = 2.0


def filter_estimator_params(cls: type, params: dict[str, Any]) -> dict[str, Any]:
    """Keep only the kwargs ``cls.__init__`` actually accepts. The DESIGN stage sometimes
    packs featurization keys (``fingerprint``, ``radius``, ``n_bits``, …) into
    ``model_params``; splatting those into an sklearn constructor is a ``TypeError`` that
    kills the whole training job. Featurization keys are consumed from the design by
    ``featurize`` — dropping them here loses nothing."""
    import inspect

    try:
        valid = set(inspect.signature(cls.__init__).parameters) - {"self"}
    except (TypeError, ValueError):  # pragma: no cover - C-level __init__
        return dict(params)
    return {k: v for k, v in params.items() if k in valid}


def reseed_estimator(est: Any, random_state: Any) -> Any:
    """Override every ``random_state`` hyperparameter on a built (possibly nested) sklearn
    estimator. The HARNESS owns the seed: a coder-authored ``build_pipeline()`` hardcodes its
    own random_state, so without this the locked-code reproduction re-run (``random_state+1``)
    is bit-identical to the original — "confirmed (rel Δ 0.0)" would be a VACUOUS check, and a
    metric claim could reach ``strong`` on a reproduction that never re-computed anything.
    Best-effort: an estimator without ``get_params``/seed params is returned unchanged."""
    if random_state is None:
        return est
    try:
        params = est.get_params(deep=True)
        seeds = {
            k: int(random_state)
            for k in params
            if k == "random_state" or k.endswith("__random_state")
        }
        if seeds:
            est.set_params(**seeds)
    except Exception:  # noqa: BLE001 - reseeding is best-effort, never break model build
        pass
    return est


class DomainPlugin(ABC):
    """Contract every domain implements. Steps are separable so they can be unit
    tested offline (featurize/train on a tiny in-memory frame) and also composed by
    ``run_experiment`` for subprocess execution."""

    name: str = "base"

    # The AI-AUTHORED demonstration capability (the frontier path): registered for EVERY
    # domain, reachable by explicit id ONLY (never keyword-matched), so the AI can author the
    # discriminating computation itself while the harness still owns the verdict.
    AI_AUTHORED_CAPABILITY_ID = "ai_authored_demonstration"

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

    def demonstration_capabilities(self) -> dict[str, DemonstrationCapability]:
        """The discriminating demonstrations this domain can actually COMPUTE, keyed by
        capability id (Codex #2). IDEATE chooses a paradigm's demonstration from this menu;
        ``run_demonstration`` dispatches to it by id; an unmatched request stays unverified.

        Every domain ALWAYS gets the AI-AUTHORED capability (the frontier path): the AI writes
        the discriminating computation as sandboxed code and the harness applies the AI's
        PRE-REGISTERED decision rule + negative control (still never an LLM 'holds'). Domains
        add their own hand-built capabilities by MERGING into this dict
        (``{**super().demonstration_capabilities(), ...}``). A domain that adds none can still
        ground a paradigm claim via an AI-authored demonstration — but only when the driver
        supplies authored code + a committed pre-registration; otherwise it fails closed."""
        return {
            self.AI_AUTHORED_CAPABILITY_ID: DemonstrationCapability(
                id=self.AI_AUTHORED_CAPABILITY_ID,
                description=(
                    "AI-AUTHORED discriminating demonstration: the AI writes sandboxed code that "
                    "computes a TEST statistic and a CONTROL statistic; the harness applies the "
                    "AI's PRE-REGISTERED decision rule (supported_if / control_silent_if) plus "
                    "leakage/degeneracy probes to decide `holds` (never an LLM assertion). Reachable "
                    "by explicit id only; requires driver-supplied code + committed pre-registration."
                ),
                compute=self._compute_ai_authored_demonstration,
                reproduce_factor=2.0,
            )
        }

    def run_demonstration(
        self, demonstration: dict[str, Any], data_spec: dict[str, Any], workdir: Path
    ) -> dict[str, Any] | None:
        """Compute a PARADIGM contribution's discriminating demonstration DETERMINISTICALLY
        (harness-owned — never an LLM 'holds' assertion): does the new frame reveal/measure
        something the incumbent provably cannot? Dispatches to a registered
        ``demonstration_capabilities()`` entry — by explicit ``demonstration["capability"]``
        id, else by best-effort keyword match — and stamps the chosen ``capability`` id +
        ``reproduce_factor`` onto the result. Returns
        ``{form, holds: bool, statistic: float|None, detail: str, capability, reproduce_factor}``
        or ``None`` (FAIL CLOSED) when no registered capability matches — never grounds the
        formulation with an unrelated computation. Default: no capabilities -> ``None``."""
        caps = self.demonstration_capabilities()
        cap = self._select_capability(demonstration, caps)
        if cap is None:
            return None
        result = cap.compute(demonstration, data_spec)
        if isinstance(result, dict):
            result.setdefault("capability", cap.id)
            result.setdefault("reproduce_factor", cap.reproduce_factor)
        return result

    def _select_capability(
        self, demonstration: dict[str, Any], caps: dict[str, DemonstrationCapability]
    ) -> DemonstrationCapability | None:
        """Resolve a demonstration spec to a registered capability: an explicit
        ``demonstration["capability"]`` id wins; otherwise fall back to the domain's
        keyword matcher (legacy specs / specs IDEATE didn't tag). No match -> ``None``."""
        if not caps:
            return None
        cap_id = str(demonstration.get("capability") or "").strip()
        if cap_id in caps:
            return caps[cap_id]
        return self._match_capability(demonstration, caps)

    def _match_capability(
        self, demonstration: dict[str, Any], caps: dict[str, DemonstrationCapability]
    ) -> DemonstrationCapability | None:
        """Domain-specific keyword fallback when a spec carries no explicit ``capability``
        id. Default: no fuzzy match — fail closed. Override to map intent -> capability. The
        AI-authored capability is intentionally NOT keyword-matched (explicit id only)."""
        return None

    # --- AI-authored demonstration: harness owns the verdict (the frontier path) -------
    @staticmethod
    def _apply_rule(value: float, rule: dict[str, Any]) -> bool:
        """Apply a pre-registered comparison rule ``{op, threshold}`` to ``value``. A
        malformed rule -> False (fail closed: an un-appliable rule never 'triggers')."""
        try:
            op = str(rule["op"]).strip()
            thr = float(rule["threshold"])
        except (KeyError, TypeError, ValueError):
            return False
        v = float(value)
        return {">=": v >= thr, ">": v > thr, "<=": v <= thr, "<": v < thr}.get(op, False)

    @staticmethod
    def _valid_decision_rules(prereg: dict[str, Any]) -> bool:
        """A usable pre-registration must carry both decision rules as ``{op, threshold}`` with
        a recognized op and a finite threshold; otherwise the harness cannot derive ``holds``."""
        import math

        if not isinstance(prereg, dict):
            return False
        for key, ops in (("supported_if", {">=", ">", "<=", "<"}),
                         ("control_silent_if", {"<", "<="})):
            r = prereg.get(key)
            if not isinstance(r, dict) or str(r.get("op")) not in ops:
                return False
            try:
                if not math.isfinite(float(r["threshold"])):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        return True

    def _demonstration_probes(self, res: dict[str, Any]) -> dict[str, Any]:
        """Harness-side leakage/degeneracy probes (anti-fakeability #4). Deterministic checks
        that the statistic isn't trivially gamed: adequate sample sizes on BOTH the test and
        control sides (a sham empty/degenerate control fails here), and finite statistics. Any
        flag -> ``clean=False`` -> the demonstration is REFUTED, not silently passed."""
        import math

        try:
            from aletheia.config import get_settings
            min_n = int(getattr(get_settings(), "demonstration_min_samples", 20))
        except Exception:  # noqa: BLE001 - config best-effort; fall back to a safe default
            min_n = 20
        flags: list[str] = []
        n_test, n_control = int(res.get("n_test", 0)), int(res.get("n_control", 0))
        if n_test < min_n:
            flags.append(f"test sample too small (n_test={n_test} < {min_n})")
        if n_control < min_n:
            flags.append(f"control sample too small/degenerate (n_control={n_control} < {min_n})")
        if not (math.isfinite(float(res.get("test_statistic", float("nan"))))
                and math.isfinite(float(res.get("control_statistic", float("nan"))))):
            flags.append("non-finite statistic")
        clean = not flags
        note = "" if clean else " [probe flags: " + "; ".join(flags) + "]"
        return {"clean": clean, "note": note, "flags": flags,
                "min_samples": min_n, "n_test": n_test, "n_control": n_control}

    def _compute_ai_authored_demonstration(
        self, demonstration: dict[str, Any], data_spec: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Run an AI-AUTHORED ``compute_demonstration`` and derive ``holds`` from the AI's
        COMMITTED pre-registration — the harness owns the verdict, the AI only computes the
        statistic. The driver injects ``demonstration_code`` (sandboxed) + ``preregistration``
        (read back from the ledger, immutable) onto the spec.

        ``holds = test_triggers AND control_silent AND probes_clean`` — all harness-applied.
        Never reads an AI-provided 'holds'. FAIL CLOSED everywhere: missing code/rule -> None
        (not_evaluated); gate reject / control fires / probe trips -> holds False (refuted);
        sandbox failure -> holds None (not_evaluated). Never raises."""
        from aletheia.coder.demonstration_runner import run_authored_demonstration
        from aletheia.coder.sandbox import DEMO_REQUIRED_FUNCTION, check_code

        code = demonstration.get("demonstration_code")
        prereg = demonstration.get("preregistration")
        form = demonstration.get("form") or "discriminating_instance"
        if not isinstance(code, str) or not code.strip() or not self._valid_decision_rules(prereg):
            return None  # no authored demonstration / no usable rule -> not_evaluated (fail closed)

        ok, reasons = check_code(code, required_function=DEMO_REQUIRED_FUNCTION)
        if not ok:
            return {"form": form, "holds": False, "statistic": None,
                    "detail": f"AI-authored demonstration rejected by code gate: {reasons[:3]}",
                    "preregistration": prereg}

        rs = int(demonstration.get("random_state") or data_spec.get("random_state") or 42)
        try:
            df = self.load_data(data_spec)
            sample_n = demonstration.get("sample_n") or data_spec.get("sample_n")
            if sample_n:
                df = df.head(int(sample_n))
                try:
                    df.attrs["data_spec"] = data_spec  # plugins read target/feature cols off attrs
                except Exception:  # noqa: BLE001 - not all frames carry attrs
                    pass
            X, y, _names, groups = self.featurize(df, {"random_state": rs})
        except Exception as exc:  # noqa: BLE001 - featurization failure -> not_evaluated
            return {"form": form, "holds": None, "statistic": None,
                    "detail": f"could not stage data for the AI demonstration: {exc}",
                    "preregistration": prereg}

        meta = {"random_state": rs, "preregistration": prereg}
        res = run_authored_demonstration(code, X, y, groups, meta)
        if res is None:
            return {"form": form, "holds": None, "statistic": None,
                    "detail": "AI-authored demonstration did not run (sandbox timeout/error)",
                    "preregistration": prereg}

        probes = self._demonstration_probes(res)
        test_triggers = self._apply_rule(res["test_statistic"], prereg["supported_if"])
        control_silent = self._apply_rule(res["control_statistic"], prereg["control_silent_if"])
        holds = bool(test_triggers and control_silent and probes["clean"])  # harness, not the AI
        verdict = ("holds" if holds else
                   "control fired" if not control_silent else
                   "test did not trigger" if not test_triggers else
                   "probe flagged")
        return {
            "form": form,
            "holds": holds,
            "statistic": round(float(res["test_statistic"]), 4),
            "detail": (
                f"AI-authored demonstration ({prereg.get('statistic_name', 'statistic')}): "
                f"test={res['test_statistic']:.4g} (supported_if {prereg['supported_if']}), "
                f"control={res['control_statistic']:.4g} (silent_if {prereg['control_silent_if']}) "
                f"-> {verdict}. {res.get('detail', '')}{probes['note']}"
            ),
            "test_statistic": float(res["test_statistic"]),
            "control_statistic": float(res["control_statistic"]),
            "test_triggers": test_triggers,
            "control_silent": control_silent,
            "preregistration": prereg,
            "probes": probes,
            "components": res.get("components", {}),
        }
