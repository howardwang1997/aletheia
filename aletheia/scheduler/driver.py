"""The autonomous lifecycle driver.

Launched from a finalized, data-ready plan, it walks EXPERIMENT_DESIGN -> ARCHIVE
as a background task with zero per-action approvals: Opus reasons at each reasoning
stage, the critic gates design + results, and the local backend runs training in a
subprocess. Everything is recorded to the ledger and streamed to the dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from typing import Any

from aletheia.coder.demonstration import (
    CANNED_EXPLORATION,
    CANNED_PREREGISTRATION,
    CANNED_DEMO,
    DEMO_SYSTEM,
    EXPLORE_SYSTEM,
    MAX_DEMONSTRATION_CODE_CHARS,
    demonstration_prompt,
    exploration_prompt,
    extract_preregistration,
)
from aletheia.coder.demonstration_runner import run_authored_exploration
from aletheia.coder.executor import hard_sandbox_preflight
from aletheia.coder.sandbox import (
    DEMO_REQUIRED_FUNCTION,
    EXPLORE_REQUIRED_FUNCTION,
    check_code,
    smoke_test_demonstration,
    smoke_test_solution,
)
from aletheia.coder.worker import CANNED_SOLUTION, CODER_SYSTEM, coder_prompt, extract_code
from aletheia.compute.base import JobSpec
from aletheia.compute.factory import get_compute_backend
from aletheia.compute.mcp_tools import resolve_data_spec, resolve_external_data_spec
from aletheia.config import get_settings
from aletheia.critics.gateway import CriticGateway
from aletheia.domains.base import DomainPlugin, DomainProfile
from aletheia.domains.registry import get_domain_plugin
from aletheia.events.bus import get_bus, make_event
from aletheia.events.store import list_run_events
from aletheia.iam import policy
from aletheia.iam.github_app import GitHubBackend, RepoResult, get_github_backend
from aletheia.memory.service import (
    attach_claim_evidence,
    claim_external_validation,
    claim_final_holdout,
    create_claim,
    create_experiment,
    finish_hypothesis_attempt,
    get_run,
    list_claims,
    record_artifacts,
    record_external_validation_result,
    record_literature_finding,
    record_metrics,
    record_final_holdout_result,
    record_scorecard,
    record_sota_result,
    register_hypothesis_attempt,
    seal_campaign_splits,
    seal_external_validation,
    set_experiment_hypothesis,
    set_experiment_repo,
    set_run_status,
    update_claim,
    upsert_credence,
)
from aletheia.memory.vector import format_briefing, index_chunk, recall
from aletheia.memory import belief
from aletheia.orchestrator.gate import build_tool_gate
from aletheia.orchestrator.tools import build_search_literature_tool
from aletheia.research import literature
from aletheia.research.citations import numbered_references, to_bibtex
from aletheia.research.split_ledger import (
    allocate_campaign_splits,
    public_split_summary,
    staged_data_identity,
)
from aletheia.reproducibility import ManifestCompatibilityError, freeze_run_manifest
from aletheia.notify.feishu import notify_feishu
from aletheia.orchestrator.reasoner import reason_stage
from aletheia.orchestrator.worker import is_degraded, run_worker
from aletheia.paths import run_artifacts_dir
from aletheia.scheduler.budget import BudgetPaused, BudgetTracker
from aletheia.scheduler.outcome import classify_outcome
from aletheia.scheduler.statemachine import LoopGuard, record_transition

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARR = re.compile(r"\[.*\]", re.DOTALL)


def _parse_json(text: str, pattern: re.Pattern, default: Any) -> Any:
    """Extract the first JSON object/array matching ``pattern``; ``default`` on failure."""
    m = pattern.search(text or "")
    if not m:
        return default
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return default


def _parse_design(text: str, fallback: dict[str, Any]) -> dict[str, Any]:
    m = _JSON.search(text or "")
    if not m:
        return dict(fallback)
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return dict(fallback)
    design = dict(fallback)
    design.update({k: v for k, v in parsed.items() if v is not None})
    return design


# Model families for plan↔execution drift detection: if the hypothesis explicitly
# names one family but a DIFFERENT family was executed, the hypothesis mechanism was
# never instantiated and cannot be tested (it is "not evaluated", not "refuted").
_METHOD_FAMILIES: dict[str, tuple[str, ...]] = {
    "random forest": ("random forest", "randomforest", "random-forest"),
    "gradient boosting": ("gradient boost", "gradient-boost", "xgboost", "xgbregressor",
                          "lightgbm", "lgbm", "histgradientboosting", "gradientboosting",
                          "gbm", "gbt", "gbrt", "boosted tree"),
    "neural network": ("neural network", "mlp", "deep learning", "transformer", "cnn",
                       "rnn", "lstm", "perceptron"),
    "linear model": ("linear regression", "ridge", "lasso", "elasticnet", "logistic regression"),
    "support vector": ("support vector", "svm", "svr"),
    "nearest neighbor": ("nearest neighbor", "nearest-neighbor", "knn", "k-nn"),
    "gaussian process": ("gaussian process", "gpr", "kriging"),
}


def _method_family(text: str) -> str | None:
    """The model family named in a free-text string, or None if none is recognized.
    First match wins (longest, most-specific keywords are listed per family)."""
    t = (text or "").lower()
    for family, keywords in _METHOD_FAMILIES.items():
        if any(kw in t for kw in keywords):
            return family
    return None


def detect_method_drift(hypothesis_text: str, requested: str, executed_impl: str) -> tuple[bool, str]:
    """True + a message when the hypothesis names a model family that differs from the
    one actually executed (so the hypothesis mechanism was never instantiated). Returns
    (False, "") when the hypothesis names no family, or the families agree, or the
    executed family is unknown — i.e. only flag a CONFIDENT mismatch."""
    hypo_fam = _method_family(hypothesis_text)
    exec_fam = _method_family(executed_impl) or _method_family(requested)
    if hypo_fam and exec_fam and hypo_fam != exec_fam:
        return True, (
            f"the hypothesis names a {hypo_fam} but the executed model was a {exec_fam} "
            f"({executed_impl or requested}); the hypothesis mechanism was not instantiated "
            f"and could not be tested"
        )
    return False, ""


class ExperimentDriver:
    # Frozen PR-0 compatibility controller; new science belongs in the research kernel.
    COMPATIBILITY_API = True
    MIGRATION_STATUS = "legacy_protocol_executor"
    NEW_SCIENTIFIC_EXTENSIONS_ALLOWED = False

    def __init__(
        self,
        run_id: str,
        dry_run: bool = False,
        auditable_sota_campaign_fn=None,
    ) -> None:
        self.run_id = run_id
        self.dry_run = dry_run
        self.gateway = CriticGateway()
        self.backend = get_compute_backend()
        settings = get_settings()
        self.guard = LoopGuard(settings.critics.consensus.max_design_iterations)
        self.budget: BudgetTracker | None = None  # set in _run (cap depends on the run)
        self.gh: GitHubBackend = get_github_backend(dry_run=dry_run)
        self.repo: RepoResult | None = None  # set by _iam_setup once a repo exists
        self.branch: str | None = None  # this experiment's branch
        self.survey_brief: str = ""  # literature briefing from the SURVEY stage
        self.survey_gaps: list[str] = []  # research gaps surfaced by SURVEY
        self.survey_methods: list[dict[str, str]] = []  # frontier methods the SURVEY found the field using
        self.survey_papers: list[literature.Paper] = []  # citable refs for WRITE_UP
        self.survey_findings: list[dict[str, Any]] = []  # structured LiteratureFinding rows
        self.survey_sota: list[dict[str, Any]] = []  # structured SOTAResult rows (curated + extracted)
        # Optional F8-S6 evaluator boundary. It receives only current-result identities and must
        # return ``(SOTAEvaluationCampaign, receipt_verification_key)``. When present, WRITE_UP
        # bypasses the legacy caller-score shortcut and fails closed on every binding error.
        self.auditable_sota_campaign_fn = auditable_sota_campaign_fn
        self.survey_weak: bool = False  # retrieval health: too few citable papers to ground novelty
        self.hypothesis: dict[str, Any] = {}  # the hypothesis chosen by IDEATE
        self._discovered_demo: dict[str, Any] | None = None  # {code, prereg} from the discovery stage
        self._discovery_split_meta: dict[str, Any] | None = None  # immutable explore/confirm seal
        self.profile: DomainProfile | None = None  # the domain's vocabulary (set in _run)
        self.plugin: Any = None  # the resolved DomainPlugin (set in _run)
        self.domain: str | None = None  # the run's domain name (materials | molecules | …)
        self._claim_ids: dict[str, str] = {}  # role -> claim id, reset per experiment round
        self._last_scores: dict[str, float] = {}  # latest hypothesis scorecard scores (campaign EIG)
        # K2 (epistemic world model): per open-question lineage, a calibrated credence Beta(α,β) for
        # "will this line hold on held-out data?". Seeded as a WEAK prior from the scorecard; moved
        # ONLY by a harness-verified confirm-split verdict (S4). A planning aid, never a verdict.
        self._belief: dict[str, belief.Credence] = {}
        # K2-S4: per lineage, the forward P(holds) prediction committed BEFORE a round runs (a
        # pre-registration) so the predicted−realized surprise can't be back-fitted.
        self._pending_prediction: dict[str, float] = {}
        # Epistemic Seal v2: one immutable allocation shared by every adaptive round.
        self._campaign_split_plan: dict[str, Any] | None = None
        self._campaign_dataset_identity: dict[str, Any] | None = None
        self._external_validation_spec: dict[str, Any] | None = None
        self._external_dataset_identity: dict[str, Any] | None = None
        self._current_round_idx: int = 1
        self._current_attempt_sequence: int = 0
        self._current_attempt_id: int | None = None
        self._family_key: str = ""
        # Source of the successful AI-authored exploration probe for the current
        # attempt.  It is code-only (no confirmation rows/results) and lets the
        # confirmatory author reuse the already-executed estimand instead of writing
        # a second long implementation from scratch over a flaky model connection.
        self._exploration_code: str | None = None
        self._demonstration_smoke_sample: tuple[Any, Any, Any] | None = None
        self._demonstration_design_contract: dict[str, Any] | None = None
        # First independently non-refuted artifact for an operator-locked diagnostic.
        # Fresh campaign batches reuse this exact source + scientific preregistration;
        # per-batch split/alpha live in the harness ledger, not in a rewritten rule.
        self._locked_diagnostic_artifact: dict[str, Any] | None = None
        # A locked scientific direction/design is reviewed once, before any result is
        # observed.  Later confirmation batches must not turn an unchanged artifact
        # into a repeated, stochastic approval lottery.  These durable receipts are
        # keyed by gate and bind approval to canonical bytes of the scientific
        # artifact (procedural continuation metadata is deliberately excluded).
        self._locked_gate_receipts: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _locked_scientific_hypothesis(hypothesis: dict[str, Any]) -> dict[str, Any]:
        """Return only the immutable scientific fields reviewed by locked gates.

        ``experiment_type``, ``open_question`` and ``continuation_rationale`` describe
        why another fresh batch was opened; they do not alter the pre-registered
        question, estimand, prediction, or demonstration.  Including them in the
        approval identity would make identical science look different each round.
        """
        keys = (
            "statement",
            "rationale",
            "prediction",
            "novelty_note",
            "contribution_type",
            "demonstration",
            "hypothesis_locked",
        )
        return {key: hypothesis.get(key) for key in keys if key in hypothesis}

    @staticmethod
    def _locked_gate_fingerprint(gate: str, artifact: dict[str, Any]) -> str:
        blob = json.dumps(
            {"gate": gate, "artifact": artifact},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
        return hashlib.sha256(blob).hexdigest()

    def _restore_locked_gate_receipts(self) -> None:
        """Restore persisted, hash-bound approvals after a process restart."""
        for event in list_run_events(self.run_id):
            if event.get("type") != "locked_gate_approved":
                continue
            payload = event.get("payload") or {}
            gate = str(payload.get("gate") or "")
            fingerprint = str(payload.get("artifact_sha256") or "")
            if gate not in {"direction", "design"} or len(fingerprint) != 64:
                continue
            self._locked_gate_receipts[gate] = {
                "artifact_sha256": fingerprint,
                "approval_event_id": event.get("id"),
                "target_ref": payload.get("target_ref"),
                "verdict": payload.get("verdict"),
                "reviewers": payload.get("reviewers") or [],
            }

    async def _record_locked_gate_approval(
        self,
        gate: str,
        fingerprint: str,
        panel: Any,
        target_ref: str,
    ) -> bool:
        """Persist a gate receipt before treating a locked approval as reusable."""
        reviewers = sorted({
            str(c.critic_id)
            for c in getattr(panel, "critiques", [])
            if str(getattr(c, "critic_id", ""))
        })
        min_reviewers = int(get_settings().min_review_vendors)
        if len(reviewers) < min_reviewers:
            await get_bus().publish(make_event(
                "locked_gate_approval_rejected",
                run_id=self.run_id,
                payload={
                    "gate": gate,
                    "artifact_sha256": fingerprint,
                    "target_ref": target_ref,
                    "reviewers": reviewers,
                    "min_reviewers": min_reviewers,
                    "reason": "cross-vendor reviewer floor was not met",
                },
            ))
            return False
        event = await get_bus().publish(make_event(
            "locked_gate_approved",
            run_id=self.run_id,
            payload={
                "gate": gate,
                "artifact_sha256": fingerprint,
                "target_ref": target_ref,
                "verdict": getattr(panel, "consensus_verdict", None),
                "reviewers": reviewers,
                "timing": "before_confirmation_execution",
            },
        ))
        if event.get("id") is None:
            return False
        self._locked_gate_receipts[gate] = {
            "artifact_sha256": fingerprint,
            "approval_event_id": event.get("id"),
            "target_ref": target_ref,
            "verdict": getattr(panel, "consensus_verdict", None),
            "reviewers": reviewers,
        }
        return True

    async def _reuse_locked_gate_if_approved(
        self,
        gate: str,
        fingerprint: str,
        target_ref: str,
    ) -> bool:
        receipt = self._locked_gate_receipts.get(gate) or {}
        if receipt.get("artifact_sha256") != fingerprint:
            return False
        event = await get_bus().publish(make_event(
            f"{gate}_gate_reused",
            run_id=self.run_id,
            payload={
                "artifact_sha256": fingerprint,
                "approval_event_id": receipt.get("approval_event_id"),
                "approval_target_ref": receipt.get("target_ref"),
                "target_ref": target_ref,
                "verdict": receipt.get("verdict"),
                "reviewers": receipt.get("reviewers") or [],
                "reason": "unchanged operator-locked scientific artifact",
            },
        ))
        # A reused approval is part of the evidence chain.  If it cannot be
        # persisted, do not silently proceed on an in-memory assertion.
        return event.get("id") is not None

    def _restore_locked_diagnostic_artifact(self) -> None:
        """Restore a pre-confirmation-locked artifact after a process restart.

        Files are only storage; the durable event's hashes are the trust anchor.
        Any missing/mutated bytes fail closed instead of silently re-authoring.
        """
        if self._locked_diagnostic_artifact is not None:
            return
        locks = [
            e.get("payload") or {}
            for e in list_run_events(self.run_id)
            if e.get("type") == "diagnostic_artifact_locked"
        ]
        if not locks:
            return
        lock = locks[-1]
        locked_dir = run_artifacts_dir(self.run_id) / "locked_candidate"
        code_path = locked_dir / "demonstration.py"
        prereg_path = locked_dir / "preregistration.json"
        exploration_path = locked_dir / "exploration.json"
        if not code_path.is_file() or not prereg_path.is_file():
            raise RuntimeError("locked diagnostic artifact bytes are missing on resume")
        code = code_path.read_text()
        prereg_blob = prereg_path.read_text()
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        prereg_hash = hashlib.sha256(prereg_blob.encode()).hexdigest()
        if code_hash != lock.get("code_sha256") or prereg_hash != lock.get(
            "preregistration_sha256"
        ):
            raise RuntimeError("locked diagnostic artifact hash mismatch on resume")
        try:
            prereg = json.loads(prereg_blob)
            exploration = (
                json.loads(exploration_path.read_text())
                if exploration_path.is_file()
                else None
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"locked diagnostic artifact is corrupt: {exc}") from exc
        audit_events = [
            e.get("payload") or {}
            for e in list_run_events(self.run_id)
            if e.get("type") == "demonstration_audit"
        ]
        eligible = any(
            a.get("gate_passed") is True
            and len(set(a.get("auditors") or [])) >= int(get_settings().min_review_vendors)
            for a in audit_events
        )
        if any(a.get("gate_passed") is False for a in audit_events):
            eligible = False
        self._locked_diagnostic_artifact = {
            "code": code,
            "preregistration": prereg,
            "exploration": exploration,
            "eligible": eligible,
        }

    @staticmethod
    def _claim_strength(
        kind: str,
        *,
        protocol_status: str | None = None,
        gate_passed: bool | None = None,
        gate_verdict: str | None = None,
        has_sota: bool = False,
        method_match: bool = True,
        reproduced: bool | None = None,
        demonstration_holds: bool | None = None,
        cross_vendor: bool = True,
        audit_passed: bool | None = None,
        audit_error: bool = False,
        exploration_missing: bool = False,
        weak_prior: bool = False,
    ) -> str:
        """Deterministic, harness-owned claim strength — never the LLM's self-rating.
        Keeps a report from implying stronger evidence than the ledger holds.

        ``reproduced``: True = an independent re-run confirmed the headline; False =
        it contradicted it; None = not attempted. A metric claim reaches `strong` ONLY
        when a reproduction confirmed it.

        ``demonstration_holds`` (paradigm contributions): True = the discriminating
        demonstration was executed and held; False = executed and did not hold; None =
        not executed. A `formulation` claim is grounded by this — NOT by SOTA-delta — and
        reaches `strong` only when the demonstration held AND reproduced under a clean
        approve. No demonstration executed → `speculative` (it stays a proposal).

        ``cross_vendor``: were >=2 DISTINCT critic vendors actually in the final review? A
        gate-derived claim cannot exceed `weak` on single-vendor (degraded) review — the
        cross-vendor adversarial panel is the load-bearing guarantee for moderate/strong,
        so a gate that survived on one vendor (e.g. same-vendor self-review) must not yield
        a strong claim.

        ``audit_passed`` / ``audit_error`` keep two DIFFERENT failure modes apart for a
        formulation claim: ``audit_passed is False`` means the independent audit RAN and
        refuted/failed the demonstration (caps to `weak`); ``audit_error is True`` means the
        audit could NOT run (infrastructure error) so there is no independent verification —
        also caps to `weak`, but this is missing evidence, NOT a refutation, and must not be
        reported as one (the same not_evaluated/refuted distinction used elsewhere).

        ``exploration_missing`` (K1): the AI-authored demonstration ran WITHOUT the explore->confirm
        seal (blind fallback) — there is no verification its threshold wasn't fit to noise, so the
        formulation claim is capped at `moderate` (never `strong`), even when held + reproduced.

        ``weak_prior`` (K2): the campaign's calibrated credence for this line is still a weak prior
        (too few harness-verified confirm-split rounds to trust). A single confirm-split hold IS a
        weak prior, so it caps the formulation claim at `moderate` — a `strong` formulation must rest
        on accumulated, replicated belief, not one round. Couples claim strength to the world model
        (the epistemic state the spine actually earned), mirroring the ``exploration_missing`` cap."""

        def _s() -> str:
            if kind == "metric":
                if protocol_status == "degraded_kfold" or not method_match:
                    return "weak"  # headline rests on a degraded protocol / a fallback method
                if gate_passed is None:
                    return "moderate"  # pre-gate: a grouped number, not yet peer-reviewed
                if not gate_passed:
                    return "weak"  # gate did not pass
                if reproduced is False:
                    return "weak"  # an independent re-run contradicted the headline
                if reproduced and gate_verdict == "approve":
                    return "strong"  # grouped + clean approve + CONFIRMED reproduction
                return "moderate"  # approve_with_changes, or reproduction not attempted/confirmed
            if kind == "sota":
                return "moderate" if has_sota else "weak"  # curated string, no structured row yet
            if kind == "mechanism":
                return "moderate" if gate_passed else "weak"
            if kind == "novelty":
                return "speculative"  # no structured literature/novelty check in v1
            if kind == "formulation":
                if demonstration_holds is None:
                    return "speculative"  # no discriminating demonstration executed -> a proposal
                if not demonstration_holds or gate_passed is False or audit_passed is False:
                    return "weak"  # didn't hold, gate failed, OR the independent audit refuted it
                if audit_error:
                    return "weak"  # audit could NOT run -> no independent verification (not a refutation)
                if reproduced and gate_verdict == "approve" and not exploration_missing and not weak_prior:
                    return "strong"  # held + clean approve + STABLE re-run + seal + accumulated belief
                # Capped at `moderate` when: the explore->confirm seal was NOT applied (K1, no
                # verification the threshold wasn't fit to noise), OR the credence is still a weak
                # prior (K2, one confirm-split hold isn't yet accumulated belief), OR review was only
                # approve_with_changes / reproduction wasn't confirmed.
                return "moderate"  # held + approved, but short of the `strong` bar
            return "weak"

        s = _s()
        # single-vendor (degraded) review caps any gate-derived claim at `weak`
        if not cross_vendor and {"speculative": 0, "weak": 1, "moderate": 2, "strong": 3}[s] > 1:
            return "weak"
        return s

    @staticmethod
    def _slug(text: str) -> str:
        """Slugify free text into a stable lineage key (K2 belief state)."""
        return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")[:80] or "q"

    def _question_key(self, plan: dict) -> str:
        """A stable key for the open-question lineage a round belongs to (K2 belief state).

        Continuations/pivots carry an ``open_question`` from the go/no-go step; round 1 falls back
        to the objective. Slugified so the same lineage maps to the same credence across rounds —
        the seam that lets a refute in round N move the SAME belief the next round reads.
        """
        return self._slug(self.hypothesis.get("open_question") or plan.get("objective") or "")

    def _fold_verdict_into_belief(
        self, qkey: str, demo: dict, audit_error: bool
    ) -> tuple[belief.Credence, dict[str, Any]]:
        """K2-S4: fold a harness verdict into the lineage credence and return ``(post_cred, signal)``.

        The credence moves ONLY on a harness-verified confirm-split verdict (``belief.update`` no-ops
        otherwise), so a not_evaluated / exploration-only / audit-degraded round produces NO update —
        fail-closed. ``signal`` carries the predicted−realized surprise (against the pre-registered
        forward prediction) + the realized / remaining information gain for the event, the outcome
        dict, and the calibration metric. The belief NEVER set this verdict; it only learns from it.
        """
        prior_cred = self._belief.get(qkey) or belief.prior_from_scorecard(self._last_scores)
        confirm_split = bool(demo.get("exploration_applied"))
        post_cred = belief.update(
            prior_cred, holds=demo.get("holds"), confirm_split=confirm_split, audit_error=audit_error,
        )
        self._belief[qkey] = post_cred
        updated = post_cred != prior_cred
        predicted = self._pending_prediction.get(qkey)
        realized = (1.0 if demo.get("holds") else 0.0) if updated else None
        surprise = abs(predicted - realized) if (updated and predicted is not None) else None
        realized_eig = belief.entropy(prior_cred) - belief.entropy(post_cred) if updated else 0.0
        # remaining information in THIS lineage after the update (normalized) — the convergence signal
        remaining_eig = belief.normalized_information_gain(post_cred, belief.mean(post_cred))
        return post_cred, {
            "updated": updated, "predicted": predicted, "realized": realized,
            "surprise": surprise, "realized_eig": realized_eig, "remaining_eig": remaining_eig,
        }

    def _maximize(self) -> bool:
        """Does a BETTER headline mean a HIGHER value (F1/recall) vs lower (MAE/RMSE)?"""
        return (self.profile.headline_goal if self.profile else "min") == "max"

    def _is_better(self, new: float | None, cur: float | None) -> bool:
        """Is ``new`` a better headline than ``cur`` under the domain's goal?"""
        if new is None:
            return False
        if cur is None:
            return True
        return new > cur if self._maximize() else new < cur

    def _worst(self) -> float:
        """The 'no score yet' sentinel under the domain's goal."""
        return float("-inf") if self._maximize() else float("inf")

    def _compare_to_sota(
        self, headline_metric: str, headline_value: float | None
    ) -> tuple[dict | None, list[dict], bool]:
        """Compare the headline against the structured SOTA rows on the same metric
        FAMILY, honoring the domain's goal (max for f1/recall/r2, min for mae/rmse).
        Returns (best_comparable_row, all_comparable_rows, did_we_beat_it)."""
        fam = headline_metric.split("_")[0].lower()  # "mae_lcso" -> "mae"; "answer_f1" -> "answer"
        comparable = [
            r for r in self.survey_sota
            if str(r.get("metric", "")).lower().startswith(fam) and r.get("score") is not None
        ]
        if not comparable or headline_value is None:
            return None, comparable, False
        if self._maximize():
            best = max(comparable, key=lambda r: r["score"])
            beat = headline_value > best["score"]
        else:  # error metrics: lower is better
            best = min(comparable, key=lambda r: r["score"])
            beat = headline_value < best["score"]
        return best, comparable, beat

    async def _index(self, kind: str, text: str, exp_id: str | None, **meta: Any) -> None:
        """Best-effort: embed a reasoning fragment into the recall store (off-thread)."""
        await asyncio.to_thread(
            index_chunk, kind, text,
            run_id=self.run_id, experiment_id=exp_id, meta=meta or None, dry_run=self.dry_run,
        )

    async def _survey(self, plan: dict, exp_id: str | None) -> tuple[str, list[str]]:
        """Deep-research SURVEY: decompose the direction into sub-questions, fan out
        parallel isolated librarian workers (each drives search_literature + ingests),
        then synthesize a thematic briefing + explicit research gaps. Mirrors the
        literature-review workflow. Returns (briefing, gaps). Best-effort — any failure
        yields an empty briefing, never breaks the loop. Sets ``self.survey_methods``
        (the frontier methods the field uses) and records a ``survey.md`` artifact."""
        if self.profile is None:  # standalone call (tests); _run sets it before SURVEY
            run = await asyncio.to_thread(get_run, self.run_id)
            self.domain = (run or {}).get("domain")
            _tgt = resolve_data_spec(self.run_id).get("target_column")
            self.profile = get_domain_plugin(self.domain).profile(target_column=_tgt)
        await self._status("surveying", "researching the literature")
        topic = " ".join(
            str(plan.get(k, "")).strip() for k in ("objective", "direction", "hypothesis")
        ).strip() or (plan.get("domain") or "materials")
        briefing, gaps, methods = "", [], []
        lit_findings: list[dict[str, Any]] = []  # structured prior-work rows
        extracted_sota: list[dict[str, Any]] = []  # SOTA rows the survey pulled from the literature
        try:
            if self.dry_run:
                papers = list(self.profile.dry_papers) if self.profile else []
                await asyncio.to_thread(literature.ingest, papers, self.run_id, True)
                briefing = literature.briefing(papers)
                gaps = list(self.profile.dry_gaps) if self.profile else []
                methods = list(self.profile.dry_frontier_methods) if self.profile else []
                lit_findings = list(self.profile.dry_literature_findings) if self.profile else []
                self.survey_papers = papers
            else:
                subqs = await self._decompose(topic)
                prose = await asyncio.gather(*[self._librarian(topic, sq) for sq in subqs])
                prose = [f for f in prose if f and not is_degraded(f)]
                if prose:
                    briefing, gaps, methods, lit_findings, extracted_sota = await self._synthesize(topic, prose)
                else:
                    await get_bus().publish(
                        make_event(
                            "literature",
                            run_id=self.run_id,
                            payload={"error": "no usable literature findings returned"},
                        )
                    )
                # The librarians ingested papers into the recall store; pull them back
                # as structured, citable references for the WRITE_UP stage.
                self.survey_papers = await asyncio.to_thread(self._recall_papers, topic)
        except Exception as exc:  # noqa: BLE001 - survey is best-effort
            await get_bus().publish(
                make_event("literature", run_id=self.run_id, payload={"error": str(exc)})
            )
        self.survey_methods = methods
        self.survey_findings = lit_findings
        # the domain's curated published SOTA (always recorded) + any survey-extracted rows
        curated_sota = list(self.profile.sota_rows) if self.profile else []
        self.survey_sota = curated_sota + extracted_sota
        # retrieval health: a near-empty literature search is WEAK evidence, not novelty.
        # (Zero papers is fail-closed upstream; this flags the few-papers case so the
        # novelty claim is honestly qualified.)
        n_papers = len(self.survey_papers)
        self.survey_weak = (not self.dry_run) and 0 < n_papers < int(get_settings().min_survey_papers)
        await get_bus().publish(make_event(
            "survey_health", run_id=self.run_id,
            payload={"papers": n_papers, "findings": len(self.survey_findings),
                     "gaps": len(gaps), "weak_grounding": self.survey_weak}))
        await self._record_structured_literature(topic)
        await self._record_survey(topic, briefing, gaps, methods, exp_id)
        await record_transition(
            self.run_id, exp_id, None, "survey",
            f"literature surveyed; {len(gaps)} gap(s), {len(methods)} frontier method(s) found",
        )
        return briefing, gaps

    async def _record_survey(
        self, topic: str, briefing: str, gaps: list[str], methods: list[dict], exp_id: str | None
    ) -> None:
        """Persist this survey's result each run: a survey.md artifact (briefing +
        gaps + the frontier methods the field uses + the retrieved papers) and a
        recall chunk per method, so 'what the field is doing' is durable + recallable."""
        method_lines = "\n".join(
            f"- **{m.get('name','')}** — {m.get('why','')} _(source: {m.get('source','')})_"
            for m in methods
        ) or "- (none extracted)"
        gap_lines = "\n".join(f"- {g}" for g in gaps) or "- (none)"
        paper_lines = "\n".join(
            f"- {p.title}{f' ({p.year})' if p.year else ''}" for p in self.survey_papers[:8]
        ) or "- (none retrieved)"
        md = (
            f"# Literature Survey\n\n**Topic:** {topic}\n\n"
            f"## Briefing\n\n{briefing or '(none)'}\n\n"
            f"## Frontier methods (the field's state of the art)\n\n{method_lines}\n\n"
            f"## Open gaps\n\n{gap_lines}\n\n"
            f"## Papers retrieved\n\n{paper_lines}\n"
        )
        try:
            path = run_artifacts_dir(self.run_id) / "survey.md"
            path.write_text(md)
            if exp_id:
                await asyncio.to_thread(
                    record_artifacts, exp_id, [{"kind": "survey", "uri": str(path)}]
                )
            for m in methods:
                await self._index(
                    "method", f"{m.get('name','')}: {m.get('why','')} (source: {m.get('source','')})",
                    exp_id, source=m.get("source"),
                )
            await get_bus().publish(
                make_event("survey_recorded", run_id=self.run_id,
                           payload={"methods": len(methods), "gaps": len(gaps),
                                    "findings": len(self.survey_findings), "sota": len(self.survey_sota),
                                    "uri": str(path)})
            )
        except Exception as exc:  # noqa: BLE001 - recording is best-effort
            await get_bus().publish(
                make_event("literature", run_id=self.run_id, payload={"error": f"record_survey: {exc}"})
            )

    async def _record_structured_literature(self, topic: str) -> None:
        """Persist the structured prior-work + SOTA rows so novelty/SOTA claims can
        point at queryable evidence (not a prose briefing). Best-effort."""
        try:
            for f in self.survey_findings:
                await asyncio.to_thread(
                    record_literature_finding, self.run_id,
                    paper_id=f.get("paper_id"), query=f.get("query") or topic, method=f.get("method"),
                    dataset=f.get("dataset"), metric=f.get("metric"), result=f.get("result"),
                    limitation=f.get("limitation"), gap=f.get("gap"), relevance=f.get("relevance"),
                    source=f.get("source") or ("profile" if self.dry_run else "survey"),
                )
            domain = self.domain
            for r in self.survey_sota:
                await asyncio.to_thread(
                    record_sota_result, self.run_id, domain,
                    task=domain, dataset=r.get("dataset"), metric=r.get("metric"), score=r.get("score"),
                    method=r.get("method"), source=r.get("source"), split_policy=r.get("split_policy"),
                    notes=r.get("notes"),
                )
        except Exception as exc:  # noqa: BLE001 - recording is best-effort
            await get_bus().publish(
                make_event("literature", run_id=self.run_id, payload={"error": f"record_structured: {exc}"})
            )

    async def _decompose(self, topic: str) -> list[str]:
        text = await run_worker(
            self.run_id, "survey:decompose",
            f"Break this research direction into 3-4 focused literature sub-questions that "
            f"together map the prior work:\n{topic}\n\nReturn ONLY a JSON array of short query strings.",
            system="You scope systematic literature reviews.", dry_run=False,
        )
        subqs = _parse_json(text, _JSON_ARR, [])
        subqs = [str(s).strip() for s in subqs if str(s).strip()][:4]
        return subqs or [topic]

    async def _librarian(self, topic: str, subq: str) -> str:
        allow = {"mcp__research__search_literature"}
        return await run_worker(
            self.run_id, f"survey:{subq[:24]}",
            f"Research this sub-question for the direction '{topic}':\n{subq}\n\n"
            "Call search_literature with focused queries (the tool now includes a DOI-backed Crossref "
            "fallback), then give a ≤70-word finding that names the "
            "strongest / state-of-the-art METHODS the field uses, the prior work they come from, and any "
            "gap you notice.",
            system="You are a meticulous research librarian; ground claims in retrieved papers, never invent.",
            tools=[build_search_literature_tool(self.run_id)], tool_namespace="research",
            allowed_tools=list(allow),
            can_use_tool=build_tool_gate(allow, self.run_id), max_turns=5, dry_run=False,
        )

    async def _synthesize(
        self, topic: str, findings: list[str]
    ) -> tuple[str, list[str], list[dict], list[dict], list[dict]]:
        joined = "\n".join(f"- {f}" for f in findings) or "(no findings retrieved)"
        text = await run_worker(
            self.run_id, "survey:synthesize",
            f"Synthesize a literature briefing for '{topic}' from these findings:\n{joined}\n\n"
            'Return ONLY JSON: {"briefing": "<=150 words on prior work + strongest methods/results", '
            '"gaps": ["concrete unexplored gap", ...], '
            '"methods": [{"name": "the frontier/SOTA method the field uses", "why": "one line", '
            '"source": "the prior work it comes from"}, ...], '
            '"findings": [{"paper_id": "doi/url/title", "method": "...", "dataset": "...", "metric": "...", '
            '"result": "the reported number", "limitation": "...", "gap": "...", "relevance": "..."}, ...], '
            '"sota": [{"method": "...", "dataset": "...", "metric": "...", "score": <number or null>, '
            '"split_policy": "...", "source": "the paper/leaderboard"}, ...]}. '
            "Only include findings/sota you can ground in the retrieved papers — never invent a number.",
            system="You synthesize literature reviews, surface concrete gaps, name the field's "
            "state-of-the-art methods, and extract structured prior-work rows (never invent — ground "
            "everything in the findings).", dry_run=False,
        )
        obj = _parse_json(text, _JSON, {})
        gaps = [str(g).strip() for g in (obj.get("gaps") or []) if str(g).strip()][:6]
        methods = [
            {"name": str(m.get("name", "")).strip(), "why": str(m.get("why", "")).strip(),
             "source": str(m.get("source", "")).strip()}
            for m in (obj.get("methods") or [])
            if isinstance(m, dict) and str(m.get("name", "")).strip()
        ][:6]
        lit_findings = [m for m in (obj.get("findings") or []) if isinstance(m, dict)][:12]
        sota = [m for m in (obj.get("sota") or []) if isinstance(m, dict)][:8]
        return str(obj.get("briefing", "")).strip(), gaps, methods, lit_findings, sota

    def _recall_papers(self, topic: str, k: int = 12) -> list[literature.Paper]:
        """Reconstruct citable Papers from the literature chunks ingested by the
        survey librarians (chunk text = 'title\\n\\nabstract'; meta carries
        doi/year/venue/citations/authors). Best-effort — [] on any failure."""
        papers: list[literature.Paper] = []
        try:
            chunks = recall(topic, k=k, kinds=["literature"], run_id=self.run_id, dry_run=self.dry_run)
        except Exception:  # noqa: BLE001 - recall is best-effort
            return []
        for c in chunks:
            text = (c.get("text") or "").strip()
            if not text:
                continue
            title, _, abstract = text.partition("\n\n")
            meta = c.get("meta") or {}
            papers.append(
                literature.Paper(
                    title=title.strip(),
                    authors=list(meta.get("authors") or []),
                    year=meta.get("year"),
                    doi=meta.get("doi"),
                    venue=meta.get("venue"),
                    abstract=abstract.strip(),
                    url=meta.get("url"),
                    citations=meta.get("citations"),
                    source=str(meta.get("source") or ""),
                )
            )
        return papers

    def _capability_menu(self) -> str:
        """The domain's registered demonstration capabilities (Codex #2), as a prompt menu
        so IDEATE picks a paradigm demonstration the harness can ACTUALLY compute (by id)
        rather than inventing one nothing can ground. Empty when the domain has none (then
        any paradigm demonstration stays unverified — honest)."""
        try:
            plug = self.plugin or (get_domain_plugin(self.domain) if self.domain else None)
            caps = plug.demonstration_capabilities() if plug is not None else {}
            # the AI-authored capability is NOT an IDEATE-selectable menu item: the driver
            # decides to author a demonstration (when no registered capability fits), so it
            # never appears here. IDEATE only picks among hand-built, registered computations.
            caps = {k: v for k, v in caps.items() if k != getattr(plug, "AI_AUTHORED_CAPABILITY_ID", None)}
        except Exception:  # noqa: BLE001 - menu is best-effort context, never fatal
            caps = {}
        if not caps:
            return ""
        lines = "\n".join(f'- "{cid}": {cap.description}' for cid, cap in caps.items())
        return (
            "DEMONSTRATION CAPABILITIES (the harness can COMPUTE exactly these discriminating "
            "demonstrations; a paradigm claim is only grounded by one of them):\n"
            f"{lines}\n\n"
        )

    async def _discover(self, plan: dict, exp_id: str | None) -> bool:
        """Autonomous discovery STAGE (gated by ``discovery_enabled``): run the divergent ideate ->
        self-triage loop (aletheia.research.discovery). On a survivor, ADOPT its hypothesis + its
        already-screened demonstration (set ``self.hypothesis`` + ``self._discovered_demo``) and return
        True; the direction gate is then skipped (the loop already cleared the cross-vendor novelty
        gate) and ``_demonstration_code`` reuses the survivor's code. Best-effort: ANY failure -> False
        (the caller falls back to single-shot ideation)."""
        import numpy as _np

        from aletheia.research.discovery import (
            discover, make_coauthor_ideator, make_vendor_ideator, make_worker_code_author,
            materials_angle_context, materials_ideate_context,
        )

        s = get_settings()
        try:
            await self._status("discovering", "generating + self-screening candidates")
            plugin = self.plugin or (get_domain_plugin(self.domain) if self.domain else None)
            if plugin is None:
                return False
            # The discovery prompt/code contract is currently materials-specific. Refuse to run it
            # against another plugin instead of silently applying the wrong scientific semantics.
            if plugin.name != "materials":
                await get_bus().publish(make_event(
                    "discovery", run_id=self.run_id,
                    payload={"status": "unsupported_domain", "domain": plugin.name},
                ))
                return False
            if not s.demonstration_explore_confirm_enabled:
                await get_bus().publish(make_event(
                    "discovery", run_id=self.run_id,
                    payload={"status": "seal_required", "reason": "explore/confirm is disabled"},
                ))
                return False
            data_spec = resolve_data_spec(self.run_id)
            # Discovery may inspect ONLY EXPLORE. The CONFIRM rows are sealed before the first
            # candidate is generated or screened, so selection cannot contaminate confirmation.
            staged = await asyncio.to_thread(
                self._stage_explore_arrays, plugin, {}, data_spec, 42,
            )
            if staged is None:
                await get_bus().publish(make_event(
                    "discovery", run_id=self.run_id,
                    payload={"status": "no_honest_split", "reason": "data cannot support sealed discovery"},
                ))
                return False
            X, y, groups, _confirm_index, split_meta = staged
            X = _np.asarray(X, dtype=float)
            y = _np.asarray(y, dtype=float)
            self._discovery_split_meta = dict(split_meta)
            vid = s.discovery_ideator_vendor
            vc = next((c for c in s.critics.active if c.id == vid and c.transport == "api"), None)
            key = s.vendor_key(vid) if vc else None
            if not vc or not key:
                await get_bus().publish(make_event("discovery", run_id=self.run_id,
                                                   payload={"status": "no_ideator", "vendor": vid}))
                return False
            target = str(data_spec.get("target_column") or "the target")
            novelty_exclude = {vid}
            if s.discovery_coauthor:
                # CO-AUTHOR: one vendor proposes the angle and the configured orchestrator writes
                # the code. The novelty panel excludes BOTH author vendors.
                angle_ideate = make_vendor_ideator(
                    vc.model, s.vendor_base_url(vid) or vc.base_url, key,
                    context=materials_angle_context(6, target, gaps=getattr(self, "survey_gaps", None)))
                author = make_worker_code_author(
                    self.run_id, target_desc=target, dry_run=self.dry_run,
                    loop=asyncio.get_running_loop(),
                )
                ideate = make_coauthor_ideator(angle_ideate, author, log=print)
                novelty_exclude.add(s.orchestrator_vendor)
            else:
                ideate = make_vendor_ideator(
                    vc.model, s.vendor_base_url(vid) or vc.base_url, key,
                    context=materials_ideate_context(6, target))
            survivors, rows = await asyncio.to_thread(
                discover, ideate_fn=ideate, plugin=plugin, X=X, y=y, groups=groups,
                gateway=self.gateway, run_id=self.run_id,
                k_survivors=int(s.discovery_k_survivors), max_rounds=int(s.discovery_max_rounds),
                novelty_exclude=novelty_exclude, log=lambda *_a: None)
            # Surface WHY each candidate died (the driver used to discard the per-row detail):
            # kill_tally is small + console-visible; breakdown carries the full per-candidate record
            # (stage it died at, why, test/control statistics, grounding + novelty verdict) to the jsonl.
            from collections import Counter as _Counter
            kill_tally = dict(_Counter(("SURVIVED" if r["survives"] else r["stage"]) for r in rows))
            breakdown = [{"title": r["title"][:64], "stage": r["stage"], "survives": r["survives"],
                          "why": r.get("why", ""), "test": r.get("test"), "control": r.get("control"),
                          "holds": r.get("holds"), "grounded": r.get("grounded"),
                          "n_papers": r.get("n_papers"), "consensus": r.get("consensus"),
                          "novelty_objection": r.get("novelty_objection")} for r in rows]
            await get_bus().publish(make_event("discovery", run_id=self.run_id, payload={
                "status": "done", "screened": len(rows), "n_survivors": len(survivors),
                "kill_tally": kill_tally, "survivors": [r["title"] for r in survivors],
                "breakdown": breakdown, "split": split_meta}))
            if not survivors:
                return False
            cand = survivors[0]["candidate"]
            claim = str(cand.get("claim") or cand.get("title") or "discovered effect")
            self.hypothesis = {
                "statement": claim, "rationale": str(cand.get("insight", "")),
                "novelty_note": str(cand.get("insight", "")), "contribution_type": "paradigm",
                "demonstration": {"form": "discriminating_instance", "claim": claim, "capability": "ai_authored"},
                "discovery_sourced": True,
            }
            self._discovered_demo = {"code": cand["code"], "prereg": cand["prereg"]}
            if exp_id:
                await asyncio.to_thread(set_experiment_hypothesis, exp_id, claim)
            await self._index("hypothesis", claim, exp_id)
            return True
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort; fall back to ideation
            await get_bus().publish(make_event("discovery", run_id=self.run_id,
                                               payload={"status": "error", "error": str(exc)[:200]}))
            return False

    async def _ideate(self, plan: dict, exp_id: str | None) -> dict:
        """IDEATE: from the survey + gaps, generate testable hypotheses and pick the
        most novel + feasible one. Persist it to the experiment. Mirrors the
        hypothesis-generation workflow.

        When ``discovery_enabled``, run the autonomous discovery loop (divergent ideate -> self-triage)
        IN PLACE OF single-shot ideation; if it banks a survivor, adopt its hypothesis + verified
        demonstration and skip the normal path (the loop already cleared the novelty gate). If discovery
        yields nothing, fall through to single-shot ideation."""
        if get_settings().discovery_enabled and await self._discover(plan, exp_id):
            return self.hypothesis
        await self._status("ideating", "forming a hypothesis")
        locked = bool(plan.get("hypothesis_locked"))
        prompt = (
            f"PLAN:\n{json.dumps(plan, indent=2)}\n\n"
            + (self.survey_brief + "\n\n" if self.survey_brief else "")
            + ("RESEARCH GAPS:\n- " + "\n- ".join(self.survey_gaps) + "\n\n" if self.survey_gaps else "")
            + self._capability_menu()
            + "Propose 2-3 TESTABLE hypotheses for this direction (each: statement, one-line rationale, "
            "concrete prediction). Then pick the ONE that is most NOVEL given the literature/gaps above "
            "and feasible on CPU within budget.\n"
            "Also classify the CONTRIBUTION TYPE of the chosen hypothesis:\n"
            "- \"performance\": the goal is to beat/match a benchmark on an established metric.\n"
            "- \"diagnostic\": the goal is to pre-specify and quantify a narrow model failure "
            "without claiming methodological novelty or a benchmark win. It still requires a "
            "concrete discriminating demonstration and negative control.\n"
            "- \"paradigm\": the goal is to change the QUESTION — a new problem formulation, "
            "representation, or evaluation metric — where beating the incumbent benchmark is NOT the "
            "point. Choose this ONLY if you can name a concrete discriminating demonstration the new "
            "frame would make (a case the incumbent provably cannot handle/distinguish).\n"
            "If diagnostic or paradigm, also give a \"demonstration\": "
            '{"form": "discriminating_instance | enablement | unification | impossibility", '
            '"claim": "the concrete, checkable case the incumbent frame provably cannot '
            'handle/distinguish", "capability": "<one of the DEMONSTRATION CAPABILITIES ids '
            "above, if your demonstration matches it — the harness will then COMPUTE exactly "
            "that; omit/leave blank to propose a NEW demonstration the harness cannot yet "
            'compute (it will stay UNVERIFIED)"}. A diagnostic/paradigm hypothesis WITHOUT a concrete '
            "demonstration will be treated as a performance contribution.\n"
            "Return ONLY JSON for the chosen one: "
            '{"statement": "...", "rationale": "...", "prediction": "...", "novelty_note": "...", '
            '"contribution_type": "performance" | "diagnostic" | "paradigm", '
            '"demonstration": {"form": "...", "claim": "...", "capability": "..."}}.'
        )
        if locked:
            # An operator may deliberately pre-register a narrow confirmatory/diagnostic
            # question. Preserve that scope instead of letting novelty-seeking ideation
            # inflate it into an unsupported causal or paradigm claim. Independent
            # scorecard + critic gates still review it below.
            hypo = {
                "statement": str(plan.get("hypothesis") or plan.get("objective") or "").strip(),
                "rationale": str(plan.get("direction") or plan.get("objective") or "").strip(),
                "prediction": str(plan.get("prediction") or plan.get("success_criteria") or "").strip(),
                "novelty_note": str(
                    plan.get("novelty_note")
                    or "Pre-specified diagnostic; no broad methodological novelty is asserted."
                ).strip(),
                "contribution_type": plan.get("contribution_type") or "diagnostic",
                "demonstration": plan.get("demonstration"),
                "hypothesis_locked": True,
            }
            await get_bus().publish(
                make_event(
                    "hypothesis_locked",
                    run_id=self.run_id,
                    payload={
                        "scope": "operator_pre_registered",
                        "contribution_type": hypo["contribution_type"],
                    },
                )
            )
        else:
            text = await reason_stage(
                self.run_id, "ideate", prompt, dry_run=self.dry_run,
                dry_text=json.dumps(self.profile.dry_hypothesis if self.profile else {}),
            )
            hypo = _parse_json(text, _JSON, {})
        statement = str(
            hypo.get("statement") or plan.get("hypothesis") or plan.get("objective") or ""
        ).strip()
        self.hypothesis = hypo if hypo else {"statement": statement}
        # operator-declared framing: when the PLAN pins a paradigm contribution (+ its
        # discriminating demonstration), carry it into the hypothesis if ideation didn't
        # emit it — so a paradigm study is reproducible, not at the mercy of whether the
        # LLM happened to include the structured fields. (The demonstration is still
        # COMPUTED by the harness, never asserted, so the fakeability guardrail holds.)
        for k in ("contribution_type", "demonstration"):
            if not self.hypothesis.get(k) and plan.get(k):
                self.hypothesis[k] = plan[k]
        if statement and exp_id:
            await asyncio.to_thread(set_experiment_hypothesis, exp_id, statement)
        await self._index("hypothesis", statement, exp_id)
        # scrutinize + REFINE the idea through a bounded adversarial debate BEFORE it is
        # committed (so a weak/known proposal is strengthened, not just gated later).
        if not locked:
            await self._debate_hypothesis(exp_id)
        statement = str(self.hypothesis.get("statement", "")).strip() or statement
        # evidence-ledger: a novelty claim per hypothesis, now GROUNDED in concrete
        # prior-work rows (Phase H). The full novelty *judgment* (scorecard) is Phase I,
        # so it stays speculative — but its evidence points at the structured findings
        # it was checked against, not just gaps.
        note = str(self.hypothesis.get("novelty_note", "")).strip()
        prior_work = [
            {"evidence_kind": "paper", "evidence_ref": str(f.get("paper_id") or f.get("method") or "prior work"),
             "note": f"{f.get('method', '')} on {f.get('dataset', '')}".strip(" on ")}
            for f in self.survey_findings[:4]
        ]
        evidence = prior_work or [
            {"evidence_kind": "experiment", "evidence_ref": exp_id or self.run_id,
             "note": "no structured prior work retrieved; novelty unchecked"}
        ]
        diagnostic = self._contribution_type() == "diagnostic"
        novelty_text = (
            f"No broad methodological novelty is asserted for this diagnostic: {note or statement}"
            if diagnostic else f"This direction is novel: {note or statement}"
        )
        self._claim_ids["novelty"] = await asyncio.to_thread(
            create_claim, self.run_id,
            claim_text=novelty_text,
            claim_type="novelty", strength=self._claim_strength("novelty"),
            status="unverified", experiment_id=exp_id, created_by="ideate", stage="ideate",
            evidence=evidence,
        )
        # weak retrieval health: too few papers to support a novelty judgment -> record it
        # as a limitation so "barely found prior work" is never read as confirmed novelty.
        if self.survey_weak:
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=(f"Novelty is unconfirmed: the literature search returned only "
                            f"{len(self.survey_papers)} citable paper(s) (< {get_settings().min_survey_papers}); "
                            "treat the novelty claim as speculative, not established."),
                claim_type="limitation", strength="moderate", status="supported",
                experiment_id=exp_id, created_by="ideate", stage="ideate",
                evidence=[{"evidence_kind": "experiment", "evidence_ref": exp_id or self.run_id,
                           "note": "weak prior-work retrieval"}],
            )
        # A demonstration-based diagnostic/paradigm hypothesis gets a first-class
        # formulation claim. It stays proposed until the locked computation grounds it.
        if self._uses_discriminating_demonstration():
            demo = self._paradigm_demonstration() or {}
            form_evidence = evidence + [{
                "evidence_kind": "demonstration",
                "evidence_ref": str(demo.get("form", "discriminating")),
                "note": str(demo.get("claim", "")),
            }]
            self._claim_ids["formulation"] = await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=(
                    f"Diagnostic proposed ({demo.get('form', 'discriminating')}): {statement}"
                    if diagnostic
                    else f"New formulation proposed ({demo.get('form', 'discriminating')}): {note or statement}"
                ),
                claim_type="formulation", strength="speculative",
                status="proposed", experiment_id=exp_id, created_by="ideate", stage="ideate",
                evidence=form_evidence,
            )
        await record_transition(self.run_id, exp_id, "survey", "ideate", f"hypothesis: {statement[:80]}")
        return self.hypothesis

    async def _debate_hypothesis(self, exp_id: str | None) -> None:
        """Scrutinize + REFINE the proposed hypothesis through a bounded adversarial debate
        BEFORE it is committed: a skeptic attacks NOVELTY (vs the surveyed prior work),
        RIGOR, and well-posedness — and, for a paradigm, whether the demonstration is
        genuinely discriminating — then the proposer revises. Strengthens the idea and
        weeds out known/repackaged ones before the (cross-vendor) direction gate sees it.
        Bounded; stops early once the skeptic has no major objection. Offline-safe: the
        dry-run skeptic returns 'solid', so this no-ops with no spend."""
        rounds = int(get_settings().ideation_debate_rounds)
        brief = (self.survey_brief + "\n\n") if self.survey_brief else ""
        for i in range(rounds):
            skeptic_prompt = (
                f"{brief}PROPOSED HYPOTHESIS:\n{json.dumps(self.hypothesis)}\n\n"
                "You are a SKEPTICAL, adversarial reviewer of the IDEA itself. Find the STRONGEST "
                "objections, focusing on:\n"
                "1) NOVELTY: given the literature above, is this already standard/known (name what)? "
                "Call out a repackaged commonplace.\n"
                "2) RIGOR + WELL-POSEDNESS: is the claim precise, testable, not vague or circular?\n"
                "3) PARADIGM only: is the 'demonstration' genuinely DISCRIMINATING (a case the "
                "incumbent provably cannot handle), or trivial/known?\n"
                'Return ONLY JSON: {"objections": ["..."], "verdict": "solid" | "revise"}. '
                'Use "solid" ONLY if there is no major novelty/rigor objection.'
            )
            raw = await reason_stage(
                self.run_id, "ideate", skeptic_prompt, dry_run=self.dry_run,
                dry_text='{"objections": [], "verdict": "solid"}',
            )
            if is_degraded(raw):  # the skeptic reasoning call failed — surface that
                await get_bus().publish(make_event(  # honestly, don't imply it was 'solid'
                    "ideation_debate", run_id=self.run_id,
                    payload={"round": i + 1, "verdict": "degraded", "objections": []}))
                break
            crit = _parse_json(raw, _JSON, {})
            objections = [str(o).strip() for o in (crit.get("objections") or []) if str(o).strip()]
            verdict = str(crit.get("verdict", "")).strip().lower()
            await get_bus().publish(make_event(
                "ideation_debate", run_id=self.run_id,
                payload={"round": i + 1, "verdict": verdict, "objections": objections[:4]}))
            if verdict == "solid" or not objections:
                break
            revise_prompt = (
                f"{brief}CURRENT HYPOTHESIS:\n{json.dumps(self.hypothesis)}\n\n"
                "A skeptic raised these objections:\n- " + "\n- ".join(objections) + "\n\n"
                "Revise into a STRONGER hypothesis that answers them — more genuinely NOVEL vs the "
                "prior work and more rigorous/well-posed; do NOT retreat to a known commonplace. Keep "
                "contribution_type and (if paradigm) demonstration unless you deliberately change the "
                'framing.\nReturn ONLY JSON: {"statement","rationale","prediction","novelty_note",'
                '"contribution_type","demonstration"}.'
            )
            revised = await reason_stage(
                self.run_id, "ideate", revise_prompt, dry_run=self.dry_run,
                dry_text=json.dumps(self.hypothesis),
            )
            self.hypothesis = self._carry_framing(_parse_json(revised, _JSON, self.hypothesis))
        stmt = str(self.hypothesis.get("statement", "")).strip()
        if stmt and exp_id:
            await asyncio.to_thread(set_experiment_hypothesis, exp_id, stmt)

    def _contribution_type(self) -> str:
        """Return the evidence/review mode, guarded by a concrete demonstration.

        ``performance`` beats a benchmark, ``diagnostic`` characterizes a narrow
        pre-specified failure without a novelty claim, and ``paradigm`` changes the question. Selects
        the results-gate review STANDARD and the write-up framing (see
        docs/PARADIGM_MODE_DESIGN.md). A `paradigm` claim is honored ONLY if it names a
        concrete discriminating demonstration — the fakeability guardrail; otherwise it
        falls back to the conservative `performance`."""
        ct = str(self.hypothesis.get("contribution_type", "")).strip().lower()
        if ct in {"diagnostic", "paradigm"} and self._paradigm_demonstration() is not None:
            return ct
        return "performance"

    def _uses_discriminating_demonstration(self) -> bool:
        return self._contribution_type() in {"diagnostic", "paradigm"}

    def _paradigm_demonstration(self) -> dict | None:
        """The discriminating demonstration a paradigm hypothesis must name: a concrete
        case the incumbent frame provably cannot handle/distinguish. Returns ``{form,
        claim}`` when present, else None. Accepts a bare string (some ideation outputs /
        plans give prose) and normalizes it to a discriminating_instance."""
        d = self.hypothesis.get("demonstration")
        if isinstance(d, dict) and str(d.get("claim", "")).strip():
            return d
        if isinstance(d, str) and d.strip():
            return {"form": "discriminating_instance", "claim": d.strip()}
        return None

    def _carry_framing(self, revised: dict) -> dict:
        """Preserve the paradigm framing (contribution_type + demonstration) across ANY
        hypothesis revision that omits them — so a re-ideation (direction gate, scorecard)
        never silently downgrades a paradigm contribution to performance."""
        for k in ("contribution_type", "demonstration"):
            if not revised.get(k) and self.hypothesis.get(k):
                revised[k] = self.hypothesis[k]
        return revised

    async def _direction_gate(self, plan: dict, exp_id: str | None) -> bool:
        """Novelty + feasibility gate on the chosen hypothesis (cross-model peer review,
        target='direction'). Bounded re-ideation on reject; escalate + pause past the limit."""
        if self.hypothesis.get("discovery_sourced"):
            # the autonomous discovery loop already cleared the cross-vendor novelty gate (author
            # excluded) on this hypothesis — don't re-gate it. (Only reachable when discovery_enabled.)
            await get_bus().publish(make_event("direction_gate_skipped", run_id=self.run_id, payload={
                "reason": "discovery-sourced hypothesis already cleared the cross-vendor novelty gate"}))
            return True
        locked = bool(self.hypothesis.get("hypothesis_locked"))
        locked_fingerprint = self._locked_gate_fingerprint(
            "direction", self._locked_scientific_hypothesis(self.hypothesis)
        )
        if locked:
            receipt = self._locked_gate_receipts.get("direction") or {}
            if receipt.get("artifact_sha256") == locked_fingerprint:
                if await self._reuse_locked_gate_if_approved(
                    "direction", locked_fingerprint, exp_id or self.run_id
                ):
                    return True
                await self._block_run(
                    exp_id,
                    "could not persist reuse of the locked direction approval; refusing "
                    "an unauditable continuation",
                )
                return False
            if receipt:
                await self._block_run(
                    exp_id,
                    "locked scientific direction differs from its pre-result approval; "
                    "refusing post-result scope drift",
                )
                return False
        while True:
            content = {
                "hypothesis": self.hypothesis, "gaps": self.survey_gaps, "literature": self.survey_brief,
                "artifact_locked": locked,
            }
            panel = await self.gateway.review(
                "direction",
                content,
                exp_id or self.run_id,
                run_id=self.run_id,
                dry_run=self.dry_run,
                mode=self._contribution_type(),
            )
            if panel.gate_passed:
                if locked and not await self._record_locked_gate_approval(
                    "direction", locked_fingerprint, panel, exp_id or self.run_id
                ):
                    await self._block_run(
                        exp_id,
                        "could not persist the locked direction approval before execution",
                    )
                    return False
                return True
            n = self.guard.bump("direction")
            if self.hypothesis.get("hypothesis_locked"):
                await self._block_run(
                    exp_id,
                    "locked pre-registration rejected by the direction gate; refusing post-review scope drift",
                )
                return False
            if self.guard.exceeded("direction"):
                await get_bus().publish(
                    make_event("escalation", run_id=self.run_id, payload={
                        "reason": "direction rejected past loop limit", "verdict": panel.consensus_verdict})
                )
                await asyncio.to_thread(set_run_status, self.run_id, "paused")
                await self._status("paused", "direction gate could not be satisfied")
                return False
            findings = [
                f"{f['severity']}/{f['category']}: {f['claim']} -> {f['suggestion']}"
                for c in panel.model_dump()["critiques"]
                for f in c["findings"]
            ]
            prompt = (
                f"The critic REJECTED this research direction (revision {n}):\n{json.dumps(self.hypothesis)}\n\n"
                "Findings:\n" + "\n".join(findings) + "\n\nPropose a revised, more novel/feasible "
                'hypothesis. Return ONLY JSON: {"statement","rationale","prediction","novelty_note",'
                '"contribution_type","demonstration"}. KEEP the contribution_type and (if paradigm) the '
                "demonstration unless your revision deliberately changes the framing."
            )
            text = await reason_stage(
                self.run_id, "ideate", prompt, dry_run=self.dry_run,
                dry_text='{"statement": "revised hypothesis", "rationale": "r", "prediction": "p", "novelty_note": "n"}',
            )
            # preserve the paradigm framing across re-ideation (see _carry_framing): a
            # revision that omits contribution_type/demonstration must NOT silently revert
            # a paradigm contribution to performance (skipping its demonstration entirely).
            self.hypothesis = self._carry_framing(_parse_json(text, _JSON, self.hypothesis))
            stmt = str(self.hypothesis.get("statement", "")).strip()
            if stmt and exp_id:
                await asyncio.to_thread(set_experiment_hypothesis, exp_id, stmt)

    async def _resolve_auditable_sota_claim(
        self,
        *,
        headline_metric: str,
        headline_score: float | None,
        candidate_protocol_sha256: str,
        experiment_id: str | None,
    ) -> dict[str, Any]:
        """Turn an evaluator-owned F8-S6 campaign into exact claim-ledger fields."""
        from aletheia.research.sota_claims import (
            blocked_sota_writeup_decision,
            screen_auditable_sota_campaign,
        )

        request = {
            "run_id": self.run_id,
            "experiment_id": experiment_id,
            "headline_metric": headline_metric,
            "headline_score": headline_score,
            "candidate_protocol_sha256": candidate_protocol_sha256,
            "contribution_type": self._contribution_type(),
        }
        try:
            provided = self.auditable_sota_campaign_fn(request)
            if inspect.isawaitable(provided):
                provided = await provided
            if not isinstance(provided, tuple) or len(provided) != 2:
                raise TypeError("campaign provider must return (campaign, receipt_key)")
            campaign, receipt_key = provided
            if not isinstance(receipt_key, bytes):
                raise TypeError("campaign receipt verification key must be bytes")
            decision = screen_auditable_sota_campaign(
                campaign=campaign,
                receipt_key=receipt_key,
                expected_candidate_protocol_sha256=candidate_protocol_sha256,
                headline_metric=headline_metric,
                headline_score=headline_score,
                contribution_type=self._contribution_type(),
            )
        except Exception as exc:  # noqa: BLE001 - manuscript SOTA claims fail closed
            decision = blocked_sota_writeup_decision(
                headline_metric=headline_metric,
                headline_score=headline_score,
                reason_code=f"auditable_sota_provider_error:{type(exc).__name__}",
            )

        evidence: list[dict[str, str]] = []
        if decision.campaign_sha256:
            evidence.append({
                "evidence_kind": "artifact",
                "evidence_ref": decision.campaign_sha256,
                "note": f"F8-S6 campaign; verdict={decision.campaign_verdict.value}",
            })
        if decision.candidate_result_receipt_sha256:
            evidence.append({
                "evidence_kind": "metric",
                "evidence_ref": decision.candidate_result_receipt_sha256,
                "note": "signed candidate benchmark result receipt",
            })
        evidence.extend(
            {
                "evidence_kind": "artifact",
                "evidence_ref": row_sha256,
                "note": "derived protocol/statistical SOTA comparison row",
            }
            for row_sha256 in decision.comparison_row_sha256s
        )
        if not evidence:
            evidence.append({
                "evidence_kind": "metric",
                "evidence_ref": headline_metric,
                "note": ",".join(decision.reason_codes),
            })
        return {
            "claim_text": decision.claim_text,
            "strength": decision.claim_strength,
            "status": decision.claim_status,
            "evidence": evidence,
            "decision_sha256": decision.decision_sha256,
            "headline_authorized": decision.headline_authorized,
        }

    @staticmethod
    def _writeup_claim_policy(claims: list[dict[str, Any]]) -> dict[str, str]:
        """Classify ledger claims for WRITE_UP.

        The report is a view over the ledger. Supported moderate/strong non-limitation claims are
        the only positive findings; supported limitations are required limitations; everything else
        must be downgraded in prose with its exact status/strength.
        """
        rank = {"speculative": 0, "weak": 1, "moderate": 2, "strong": 3}
        allowed: list[str] = []
        limitations: list[str] = []
        restricted: list[str] = []
        table: list[str] = []

        for c in claims:
            ctype = str(c.get("claim_type") or "claim")
            status = str(c.get("status") or "proposed")
            strength = str(c.get("strength") or "speculative")
            text = str(c.get("claim_text") or "").strip()
            prefix = f"[{ctype}] status={status} strength={strength}"
            row = f"- {prefix}: {text}"
            if status == "supported" and rank.get(strength, 0) >= 2 and ctype != "limitation":
                allowed.append(row)
                tag = "FINDING_ALLOWED"
            elif ctype == "limitation" and status == "supported":
                limitations.append(row)
                tag = "REQUIRED_LIMITATION"
            else:
                restricted.append(row)
                tag = "NOT_FINDING"
            table.append(f"- {tag} {prefix}: {text}")

        return {
            "allowed": "\n".join(allowed) or "- (none; no claim reached finding-grade support)",
            "limitations": "\n".join(limitations) or "- (none recorded)",
            "restricted": "\n".join(restricted) or "- (none)",
            "table": "\n".join(table) or "- (no claims recorded)",
        }

    @staticmethod
    def _dry_writeup_ledger_text(claim_policy: dict[str, str]) -> str:
        """Dry-run report paragraph derived from the claim policy, not free-form analysis."""
        allowed = claim_policy.get("allowed", "")
        limitations = claim_policy.get("limitations", "")
        restricted = claim_policy.get("restricted", "")
        parts: list[str] = []
        if "(none;" in allowed:
            parts.append("No claim reached finding-grade support in the claim ledger.")
        else:
            parts.append("Finding-grade claims from the ledger: " + allowed.replace("\n", " "))
        if "(none recorded)" not in limitations:
            parts.append("Required limitations: " + limitations.replace("\n", " "))
        if "(none)" not in restricted:
            parts.append("Claims not eligible as findings: " + restricted.replace("\n", " "))
        return " ".join(parts)

    # --- hypothesis scorecard (gate low-value experiments before spending compute) ---
    _SCORE_DIMS = (
        "novelty", "feasibility", "expected_information_gain", "sota_relevance",
        "dataset_fit", "evaluation_clarity", "cost_risk", "failure_interpretability",
    )

    async def _score_hypothesis(self, plan: dict) -> dict[str, float]:
        """Score the chosen hypothesis on 8 dimensions (0..1), grounded in the
        structured prior work + SOTA from the survey. The LLM scores; a fixed rule
        (``_scorecard_decision``) gates. Dry-run → canned high scores (proceeds)."""
        findings = "\n".join(
            f"- {f.get('method','')} on {f.get('dataset','')}: {f.get('result','')} (gap: {f.get('gap','')})"
            for f in self.survey_findings[:8]
        ) or "(no structured prior work)"
        sota = "\n".join(
            f"- {r.get('method','')} {r.get('metric','')}={r.get('score','')} on {r.get('dataset','')}"
            for r in self.survey_sota[:8]
        ) or "(no SOTA rows)"
        gaps = ("\n- " + "\n- ".join(self.survey_gaps)) if self.survey_gaps else " (none)"
        prompt = (
            f"Score this research HYPOTHESIS before we spend compute on it.\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis)}\n"
            f"OBJECTIVE: {plan.get('objective', '')}\n"
            f"OPEN GAPS:{gaps}\n"
            f"PRIOR WORK (structured):\n{findings}\n"
            f"KNOWN SOTA (structured):\n{sota}\n\n"
            "Score each 0..1 (cost_risk: higher = costlier/riskier), grounded in the prior work above — "
            "novelty is LOW if a listed prior-work row already does essentially this. Return ONLY JSON: "
            '{"novelty": .., "feasibility": .., "expected_information_gain": .., "sota_relevance": .., '
            '"dataset_fit": .., "evaluation_clarity": .., "cost_risk": .., "failure_interpretability": .., '
            '"rationale": "one line"}.'
        )
        text = await reason_stage(
            self.run_id, "scorecard", prompt, dry_run=self.dry_run,
            dry_text=json.dumps({
                "novelty": 0.7, "feasibility": 0.8, "expected_information_gain": 0.7,
                "sota_relevance": 0.7, "dataset_fit": 0.8, "evaluation_clarity": 0.8,
                "cost_risk": 0.2, "failure_interpretability": 0.7,
                "rationale": "novel + feasible + clearly evaluable on the available data",
            }),
        )
        obj = _parse_json(text, _JSON, {})
        scores: dict[str, float] = {}
        for d in self._SCORE_DIMS:
            try:
                scores[d] = max(0.0, min(1.0, float(obj.get(d))))
            except (TypeError, ValueError):
                scores[d] = 0.0
        scores["rationale"] = str(obj.get("rationale", "")).strip()
        return scores

    def _scorecard_decision(self, scores: dict[str, float]) -> tuple[bool, str]:
        """Fixed harness rule: an experiment is worth running only if it clears the
        mode-appropriate value + evaluation-clarity floors. A diagnostic deliberately
        makes no novelty claim, so expected information gain replaces novelty."""
        s = get_settings()
        nov = float(scores.get("novelty", 0.0))
        eig = float(scores.get("expected_information_gain", 0.0))
        clar = float(scores.get("evaluation_clarity", 0.0))
        if self._contribution_type() == "diagnostic" and eig < s.campaign_min_eig:
            return False, f"information gain {eig:.2f} below floor {s.campaign_min_eig}"
        if self._contribution_type() != "diagnostic" and nov < s.hypothesis_min_novelty:
            return False, f"novelty {nov:.2f} below floor {s.hypothesis_min_novelty}"
        if clar < s.hypothesis_min_eval_clarity:
            return False, f"evaluation clarity {clar:.2f} below floor {s.hypothesis_min_eval_clarity}"
        axis = "information-gain" if self._contribution_type() == "diagnostic" else "novelty"
        return True, f"scorecard cleared the {axis} + evaluation floors"

    async def _scorecard_gate(self, plan: dict, exp_id: str | None) -> bool:
        """Score the hypothesis, persist the scorecard, and gate on it (cheap +
        deterministic, BEFORE the cross-model direction gate). Bounded re-ideation on
        block; escalate + pause past the limit. Returns True to proceed."""
        while True:
            scores = await self._score_hypothesis(plan)
            proceed, reason = self._scorecard_decision(scores)
            await asyncio.to_thread(
                record_scorecard, self.run_id, experiment_id=exp_id, scores=scores,
                decision=("proceed" if proceed else "block"),
                rationale=scores.get("rationale") or reason,
            )
            await get_bus().publish(
                make_event("scorecard", run_id=self.run_id, payload={
                    "decision": "proceed" if proceed else "block", "reason": reason,
                    "scores": {k: scores.get(k) for k in self._SCORE_DIMS}})
            )
            if proceed:
                self._last_scores = scores
                # K2-S1: seed this lineage's belief as a WEAK prior from the scorecard (once per
                # open-question lineage). The credence only ever moves on a harness-verified
                # confirm-split verdict (S4); the prior never asserts belief, it just sizes it.
                qkey = self._question_key(plan)
                if qkey not in self._belief:
                    cred = belief.prior_from_scorecard(scores)
                    self._belief[qkey] = cred
                    await asyncio.to_thread(
                        upsert_credence, self.run_id, qkey, cred.alpha, cred.beta, cred.n_updates)
                    await get_bus().publish(make_event(
                        "belief_prior", run_id=self.run_id, payload={
                            "question_key": qkey, "alpha": cred.alpha, "beta": cred.beta,
                            "mean": belief.mean(cred), "weak_prior": belief.is_weak_prior(cred)}))
                # K2-S4: commit the forward P(holds) prediction NOW — before EXECUTE — as an
                # immutable pre-registration. A continuation reads its accumulated credence (so the
                # prediction reflects prior learning); a fresh lineage reads its weak prior. The
                # predicted−realized surprise (computed post-verdict) can't be back-fitted.
                predicted = belief.mean(self._belief[qkey])
                self._pending_prediction[qkey] = predicted
                await get_bus().publish(make_event(
                    "belief_prediction", run_id=self.run_id, payload={
                        "question_key": qkey, "predicted_p_holds": predicted}))
                # the novelty claim (from IDEATE) is now SCORED + grounded in structured
                # prior work — upgrade it from speculative (still LLM-judged → only weak).
                if (
                    self._claim_ids.get("novelty")
                    and scores.get("novelty", 0.0) >= get_settings().hypothesis_min_novelty
                    and self.survey_findings
                ):
                    await asyncio.to_thread(
                        update_claim, self._claim_ids["novelty"], strength="weak",
                    )
                return True
            self.guard.bump("scorecard")
            if self.guard.exceeded("scorecard"):
                await get_bus().publish(
                    make_event("escalation", run_id=self.run_id, payload={
                        "reason": "hypothesis scorecard could not clear the floors", "detail": reason})
                )
                await self._block_run(exp_id, f"hypothesis blocked by scorecard: {reason}")
                return False
            if self.hypothesis.get("hypothesis_locked"):
                await self._block_run(
                    exp_id,
                    f"locked pre-registration blocked by scorecard: {reason}; refusing to rewrite it",
                )
                return False
            # re-ideate for a more novel / clearly-evaluable hypothesis
            await self._reideate_for_scorecard(plan, exp_id, reason)

    async def _reideate_for_scorecard(self, plan: dict, exp_id: str | None, reason: str) -> None:
        prompt = (
            f"The hypothesis was BLOCKED by the pre-execution scorecard: {reason}.\n"
            f"Current hypothesis: {json.dumps(self.hypothesis)}\n\n"
            "Propose a revised hypothesis that is MORE NOVEL vs the surveyed prior work and has a CLEARER "
            'evaluation. Return ONLY JSON: {"statement","rationale","prediction","novelty_note",'
            '"contribution_type","demonstration"}. KEEP the contribution_type and (if paradigm) the '
            "demonstration unless you deliberately change the framing."
        )
        text = await reason_stage(
            self.run_id, "ideate", prompt, dry_run=self.dry_run,
            dry_text='{"statement": "revised, more novel hypothesis", "rationale": "r", "prediction": "p", "novelty_note": "n"}',
        )
        self.hypothesis = self._carry_framing(_parse_json(text, _JSON, self.hypothesis))
        stmt = str(self.hypothesis.get("statement", "")).strip()
        if stmt and exp_id:
            await asyncio.to_thread(set_experiment_hypothesis, exp_id, stmt)

    async def _code(self, design: dict, data_spec: dict, exp_id: str | None) -> str | None:
        """CODE stage: the coder authors a constrained build_pipeline() solution.
        It is statically gated; a passing solution is used as the candidate model,
        a failing one is rejected and the run falls back to the fixed design model."""
        if not get_settings().coder_enabled:
            return None
        await self._status("coding", "authoring model code")
        text = await run_worker(
            self.run_id, "coder",
            coder_prompt(
                design, data_spec,
                feature_desc=self.profile.feature_desc if self.profile else "a dense numeric feature matrix",
                protocol=self.profile.protocol_desc if self.profile
                else "grouped cross-validation (headline) + RepeatedKFold + a baseline panel",
                methods=self.survey_methods or None,
            ),
            system=CODER_SYSTEM, dry_run=self.dry_run, dry_value=CANNED_SOLUTION,
        )
        if is_degraded(text):
            return None  # worker unavailable -> fall back to the fixed model
        code = extract_code(text)
        ok, reasons = check_code(code)
        if ok:  # the AST gate can't catch a runtime import error (a real symbol from the
            # WRONG module) or a build error — smoke-test the import + build so a coder
            # slip falls back to the fixed model instead of crashing the training run.
            smoke_ok, smoke_err = await asyncio.to_thread(smoke_test_solution, code)
            if not smoke_ok:
                ok = False
                reasons = [*reasons, f"import/build failed: {smoke_err}"]
        await get_bus().publish(
            make_event(
                "code", run_id=self.run_id,
                payload={"accepted": ok, "lines": code.count("\n") + 1, "reasons": reasons[:5]},
            )
        )
        if not ok:
            await record_transition(
                self.run_id, exp_id, "experiment_design", "experiment_design",
                f"coder solution rejected by gate: {reasons[:3]}; using fixed model",
            )
            return None
        await self._index("design_rationale", f"coder solution:\n{code[:400]}", exp_id)
        return code

    @staticmethod
    def _valid_preregistration(prereg: Any) -> bool:
        """A committable pre-registration carries all descriptive keys AND both decision rules
        well-formed (op + finite threshold) — the structured rule is what lets the harness derive
        ``holds`` with NO LLM assertion. Reuses the base-class rule validator."""
        if not isinstance(prereg, dict):
            return False
        for k in ("statistic_name", "computation", "control_description", "expected_control"):
            if not str(prereg.get(k, "")).strip():
                return False
        return DomainPlugin._valid_decision_rules(prereg)

    def _read_committed_preregistration(self) -> dict | None:
        """Read the IMMUTABLE pre-registration back from the LEDGER (not driver memory), so the
        rule applied at compute time is provably the one committed BEFORE results existed — the
        central anti-fakeability guard. Returns None if none was committed."""
        fid = self._claim_ids.get("formulation")
        if not fid:
            return None
        try:
            for c in list_claims(self.run_id):
                if c.get("id") != fid:
                    continue
                for e in c.get("evidence", []):
                    if e.get("evidence_kind") == "preregistration" and e.get("note"):
                        return json.loads(e["note"])
        except Exception:  # noqa: BLE001 - ledger read best-effort; missing -> fail closed
            return None
        return None

    @staticmethod
    def _prefer_authored_demonstration(demo: dict) -> bool:
        """The frontier OVERRIDE to registered-first routing: author the demonstration even when
        a registered capability keyword-matches. True when the global ``demonstration_prefer_authored``
        setting is on, OR the demonstration spec is explicitly tagged (``authoring == "ai"`` or a
        truthy ``ai_authored``). Default False -> registered-first stands."""
        if getattr(get_settings(), "demonstration_prefer_authored", False):
            return True
        if not isinstance(demo, dict):
            return False
        return str(demo.get("authoring", "")).strip().lower() == "ai" or bool(demo.get("ai_authored"))

    async def _demonstration_code(self, design: dict, data_spec: dict, exp_id: str | None) -> None:
        """DEMONSTRATION CODE stage (paradigm frontier): the orchestrator authors ``compute_demonstration``
        + a STRUCTURED pre-registration. Both are statically gated + smoke-tested; the
        pre-registration is committed IMMUTABLY to the formulation claim BEFORE execution (so it
        cannot be tuned to the observed statistic). On success, stashes the code on ``design`` so
        ``_compute_demonstration`` routes to the AI-authored capability. ANY failure -> no commit,
        fall back to the registered-capability path (fail closed). Never raises."""
        if not get_settings().coder_enabled or not getattr(
            get_settings(), "ai_demonstration_enabled", True
        ):
            return
        fid = self._claim_ids.get("formulation")
        demo = self._paradigm_demonstration()
        if not fid or not demo:
            return
        # Registered-first (default): if a trusted, hand-built capability already grounds this
        # claim, use it (deterministic + already audited) — the AI-authored path is for claims
        # NOTHING can currently ground (Codex gap #3: "arbitrary AI-proposed demonstrations out of
        # reach"). The frontier OVERRIDE: a ``demonstration_prefer_authored`` setting or a spec
        # tagged ``authoring="ai"`` forces the AI to author even when a registered capability fits
        # (the full anti-fakeability spine — prereg + control + audit + probes — still applies).
        plugin = self.plugin or (get_domain_plugin(self.domain) if self.domain else None)
        if plugin is not None and not self._prefer_authored_demonstration(demo):
            caps = plugin.demonstration_capabilities()
            registered = {k: v for k, v in caps.items() if k != plugin.AI_AUTHORED_CAPABILITY_ID}
            if plugin._select_capability(demo, registered) is not None:
                return  # a registered capability fits; don't AI-author
        feature_desc = self.profile.feature_desc if self.profile else "a dense numeric feature matrix"
        min_n = int(getattr(get_settings(), "demonstration_min_samples", 20))
        rs = int(design.get("random_state", 42))
        self._exploration_code = None
        self._demonstration_smoke_sample = None
        self._demonstration_design_contract = design

        locked_artifact = self._locked_diagnostic_artifact
        if (
            locked_artifact is not None
            and self._campaign_split_plan is not None
            and self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
        ):
            # Resolve + identity-check the new sealed batch without exposing any
            # row or result to an author. `_stage_explore_arrays` is trusted staging;
            # its exploration arrays are discarded here.
            try:
                staged = await asyncio.to_thread(
                    self._stage_explore_arrays, plugin, demo, data_spec, rs
                )
            except Exception as exc:  # noqa: BLE001 - fail closed below
                staged = None
                reuse_error = str(exc)
            else:
                reuse_error = "sealed batch could not be staged"
            if staged is None:
                await get_bus().publish(make_event(
                    "demonstration_rejected",
                    run_id=self.run_id,
                    payload={"reason": f"locked artifact reuse failed: {reuse_error}"},
                ))
                return
            _xe, _ye, _ge, confirm_index, split_meta = staged
            code = str(locked_artifact["code"])
            prereg = dict(locked_artifact["preregistration"])
            exploration_obs = locked_artifact.get("exploration")
            await asyncio.to_thread(
                attach_claim_evidence,
                fid,
                "preregistration",
                str(prereg.get("statistic_name", "statistic")),
                json.dumps(prereg),
            )
            design["demonstration_code"] = code
            design["demonstration_confirm_index"] = confirm_index
            design["demonstration_split_meta"] = split_meta
            if exploration_obs is not None:
                design["demonstration_exploration"] = exploration_obs
            await asyncio.to_thread(
                attach_claim_evidence,
                fid,
                "split",
                f"locked artifact on fresh confirmation batch "
                f"{split_meta.get('confirmation_batch')}",
                json.dumps(split_meta),
            )
            await get_bus().publish(make_event(
                "demonstration_code_reused",
                run_id=self.run_id,
                payload={
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    "preregistration_sha256": hashlib.sha256(
                        (json.dumps(prereg, indent=2, sort_keys=True) + "\n").encode()
                    ).hexdigest(),
                    "confirmation_batch": split_meta.get("confirmation_batch"),
                    "confirmation_index_hash": split_meta.get("confirmation_index_hash"),
                    "family_alpha": split_meta.get("family_alpha"),
                },
            ))
            return

        # K1 EXPLORE phase (the explore->confirm seal): author + run an exploration probe on a
        # DISJOINT explore subset so the threshold is CALIBRATED to observed data, then have the
        # harness CONFIRM on a held-out subset the authoring never saw. Best-effort: any failure
        # (disabled, dry-run, infeasible split, worker/sandbox error) -> blind authoring, with the
        # formulation claim capped below `strong` (no seal = no verification it isn't fit to noise).
        exploration_obs: dict | None = None
        confirm_index: list[int] | None = None
        split_meta: dict | None = None
        if (getattr(get_settings(), "demonstration_explore_confirm_enabled", True)
                and plugin is not None and not self.dry_run):
            exploration_obs, confirm_index, split_meta = await self._explore_for_demonstration(
                plugin, demo, data_spec, feature_desc, rs, exp_id
            )
        if (self.hypothesis.get("discovery_sourced") or self._campaign_split_plan) and confirm_index is None:
            # Under either discovery or the campaign ledger, running on full data would mix
            # exploration/previous/final rows into the verdict. Never degrade to blind/full.
            await get_bus().publish(make_event(
                "demonstration_rejected", run_id=self.run_id,
                payload={"reason": "campaign requires an intact fresh confirmation batch"},
            ))
            return

        await self._status("coding", "authoring the discriminating demonstration")
        base_prompt = demonstration_prompt(
            self.hypothesis, demo, data_spec,
            feature_desc=feature_desc, min_samples=min_n, exploration=exploration_obs,
            exploration_code=self._exploration_code,
            design=design,
        )
        retry_note = ""
        code, prereg = "", None
        ok, valid_prereg, consistent = False, False, True
        reasons: list[str] = []
        consistency_reason = ""
        accepted = False
        # bounded informed retry: each attempt re-authors with the PREVIOUS rejection reason
        # (control-not-silent / threshold-doomed / runtime error / degenerate pre-flight sample)
        # fed back. More rounds = more chances to fix a flagged DESIGN flaw within one campaign
        # round (configurable for the frontier campaign, which leans hard on the AI-authored path).
        auth_rounds = max(1, int(get_settings().demonstration_authoring_rounds))
        for attempt in range(auth_rounds):
            disc = getattr(self, "_discovered_demo", None) if attempt == 0 else None
            if disc:
                # autonomous-discovery STAGE: use the survivor's already-screened demonstration code
                # instead of authoring one. It STILL goes through the same gates below (code-gate,
                # smoke, valid-prereg, explore/confirm seal, commit) — just sourced from discovery.
                code, prereg = disc["code"], disc["prereg"]
            else:
                text = await run_worker(
                    self.run_id, "coder", base_prompt + retry_note,
                    system=DEMO_SYSTEM, dry_run=self.dry_run,
                    max_attempts=get_settings().authoring_max_attempts,  # the long stream gets patient retries
                    dry_value="```python\n" + CANNED_DEMO + "```\n```json\n"
                    + json.dumps(CANNED_PREREGISTRATION) + "\n```",
                )
                if is_degraded(text):
                    return  # worker unavailable -> fall back to registered capability
                code = extract_code(text)
                prereg = extract_preregistration(text)
                # When a successful exploration implementation was supplied, the
                # final author is asked for only a compact adapter.  Concatenate the
                # exact AI-authored source that produced the archived observations;
                # no trusted code changes its statistic or matching algorithm.
                if (
                    self._exploration_code
                    and "explore_demonstration(" in code
                    and "def explore_demonstration" not in code
                ):
                    code = self._exploration_code.rstrip() + "\n\n" + code.lstrip()
            if len(code) > MAX_DEMONSTRATION_CODE_CHARS:
                ok, reasons = False, [
                    f"demonstration source exceeds the auditable limit "
                    f"({len(code)} > {MAX_DEMONSTRATION_CODE_CHARS} characters)"
                ]
            else:
                ok, reasons = check_code(code, required_function=DEMO_REQUIRED_FUNCTION)
            if ok:
                smoke_ok, smoke_err = await asyncio.to_thread(
                    smoke_test_demonstration,
                    code,
                    float(get_settings().demonstration_timeout_s),
                    self._demonstration_smoke_sample,
                )
                if not smoke_ok:
                    ok, reasons = False, [*reasons, f"import/run failed: {smoke_err}"]
            valid_prereg = self._valid_preregistration(prereg)
            expected_rules = design.get("decision_rules") or {}
            if valid_prereg and expected_rules:
                try:
                    rule_match = all(
                        str((prereg.get(key) or {}).get("op"))
                        == str((expected_rules.get(key) or {}).get("op"))
                        and float((prereg.get(key) or {}).get("threshold"))
                        == float((expected_rules.get(key) or {}).get("threshold"))
                        for key in ("supported_if", "control_silent_if")
                    )
                except (TypeError, ValueError):
                    rule_match = False
                if not rule_match:
                    valid_prereg = False
                    reasons = [
                        *reasons,
                        "pre-registration decision rules differ from the locked executable "
                        f"contract: expected {expected_rules}",
                    ]
            # K1 seal #5 (DETERMINISTIC, pre-commit): a threshold inconsistent with what the AI
            # observed on the explore subset (doom-to-zero / control-not-silent / trivially-easy)
            # is NOT committed.
            consistent, consistency_reason = True, ""
            if exploration_obs is not None and valid_prereg:
                consistent, consistency_reason = self._prereg_consistent_with_exploration(
                    prereg, exploration_obs)
                if not consistent:
                    reasons = [*reasons, consistency_reason]
            accepted = bool(ok and valid_prereg and consistent)
            # K1 v1.2: ONE bounded INFORMED retry on ANY actionable authoring rejection — a code-gate
            # reject, a smoke-test runtime error, a malformed pre-registration, OR a threshold
            # inconsistent with the AI's OWN exploration. The retried output goes through the EXACT
            # SAME gates (nothing relaxed); it just gives one informed second attempt before falling
            # back. On a domain with NO registered fallback (materials), that is produce-a-verifiable-
            # demonstration vs produce-nothing. Seen live: run b1993c7f died on a threshold
            # inconsistency (v1.1 covered it); run 5cdb9d60 died on a COMPOUND failure (the code
            # raised AND the control threshold was off) that v1.1's consistency-only retry missed.
            # break on acceptance OR after the LAST configured round. (Previously hard-capped at
            # `attempt == 1`, which silently pinned the loop to 2 attempts and made
            # `demonstration_authoring_rounds` > 2 a no-op — the informed retries it was bumped to
            # buy never actually ran.)
            if accepted or attempt >= auth_rounds - 1:
                break
            await get_bus().publish(
                make_event("demonstration_code", run_id=self.run_id, payload={
                    "accepted": False, "lines": code.count("\n") + 1, "reasons": reasons[:5],
                    "preregistration_valid": valid_prereg,
                    "exploration_applied": confirm_index is not None,
                    "exploration_consistent": consistent, "retrying": True})
            )
            await self._status("coding", "revising the demonstration (bounded retry)")
            retry_note = (
                "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED PRE-COMMIT by the harness. Fix ALL of these "
                "and resubmit the SAME two fenced blocks (python + json), keeping the same "
                "discriminating idea:\n"
                + "\n".join(f"  - {r}" for r in reasons[:5])
                + "\nIf a threshold was inconsistent with your exploration observations, recalibrate "
                "it to what those observations support (a supported_if your expected_test_statistic "
                "meets, a control_silent_if your expected_control_statistic satisfies, and thresholds "
                "that still DISCRIMINATE test from control). If the code raised at import/run, fix the "
                "runtime error. If the pre-registration was malformed, return both decision rules as "
                "{op, threshold}."
            )
        if accepted and isinstance(prereg, dict) and split_meta is not None:
            # Harness-owned family-wise metadata is committed with the decision rule. The model
            # cannot choose a looser alpha after seeing which campaign round it occupies. The
            # actual role/batch/alpha are intentionally kept in split_meta + the attempt ledger,
            # leaving this scientific preregistration byte-identical across later phases.
            prereg = {
                **prereg,
                "familywise_error": {
                    "family_key": self._family_key,
                    "alpha_source": "harness_runtime_family_alpha",
                    "method": "bonferroni_fixed_sequence",
                    "decision_rules_invariant": True,
                },
            }
        await get_bus().publish(
            make_event("demonstration_code", run_id=self.run_id, payload={
                "accepted": accepted, "lines": code.count("\n") + 1, "reasons": reasons[:5],
                "preregistration_valid": valid_prereg,
                "exploration_applied": confirm_index is not None,
                "exploration_consistent": consistent,
                "family_alpha": (split_meta or {}).get("family_alpha")})
        )
        if not accepted:
            await record_transition(
                self.run_id, exp_id, "experiment_design", "experiment_design",
                f"AI demonstration not accepted (code_ok={ok}, prereg_ok={valid_prereg}, "
                f"explore_consistent={consistent}); falling back to a registered capability",
            )
            return
        # immutable commit BEFORE execution — the pre-registration cannot be revised post-hoc.
        await asyncio.to_thread(
            attach_claim_evidence, fid, "preregistration",
            str(prereg.get("statistic_name", "statistic")), json.dumps(prereg),
        )
        await get_bus().publish(
            make_event("preregistration", run_id=self.run_id, payload={
                "statistic": prereg.get("statistic_name"),
                "supported_if": prereg.get("supported_if"),
                "control_silent_if": prereg.get("control_silent_if"),
                "familywise_error": prereg.get("familywise_error"),
                "allocated_alpha": (split_meta or {}).get("family_alpha"),
                "confirmation_batch": (split_meta or {}).get("confirmation_batch")})
        )
        design["demonstration_code"] = code
        # K1: carry the held-out CONFIRM partition (+ its audit meta) so the compute step runs the
        # demonstration ONLY on data the authoring never saw — the partition is evidence, like the
        # prereg. Absent -> the compute path stays full-data (backward compatible) and the claim is
        # capped below `strong` (the seal could not be applied).
        if confirm_index is not None and split_meta is not None:
            design["demonstration_confirm_index"] = confirm_index
            design["demonstration_split_meta"] = split_meta
            design["demonstration_exploration"] = exploration_obs  # for the audit's seal-#5 check
            await asyncio.to_thread(
                attach_claim_evidence, fid, "split",
                f"explore/confirm seal (algo v{split_meta.get('split_algo_version')})",
                json.dumps(split_meta),
            )
        if (
            self._locked_diagnostic_artifact is None
            and self._campaign_split_plan is not None
            and self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
        ):
            # Freeze at PRE-COMMIT, before confirmation execution or any verdict.
            # Later batches may validate/refute this artifact but cannot rewrite it.
            self._locked_diagnostic_artifact = {
                "code": code,
                "preregistration": dict(prereg),
                "exploration": exploration_obs,
                "eligible": False,
            }
            locked_dir = run_artifacts_dir(self.run_id) / "locked_candidate"
            locked_dir.mkdir(parents=True, exist_ok=True)
            locked_code = locked_dir / "demonstration.py"
            locked_prereg = locked_dir / "preregistration.json"
            locked_exploration = locked_dir / "exploration.json"
            prereg_blob = json.dumps(prereg, indent=2, sort_keys=True) + "\n"
            locked_code.write_text(code)
            locked_prereg.write_text(prereg_blob)
            locked_exploration.write_text(
                json.dumps(exploration_obs, indent=2, sort_keys=True, default=str) + "\n"
            )
            lock_event = await get_bus().publish(make_event(
                "diagnostic_artifact_locked",
                run_id=self.run_id,
                payload={
                    "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                    "preregistration_sha256": hashlib.sha256(prereg_blob.encode()).hexdigest(),
                    "code_uri": str(locked_code),
                    "preregistration_uri": str(locked_prereg),
                    "exploration_uri": str(locked_exploration),
                    "timing": "before_confirmation_execution",
                },
            ))
            if lock_event.get("id") is None:
                raise RuntimeError(
                    "locked diagnostic artifact event was not durably persisted; "
                    "refusing to open confirmation data"
                )
        await self._index("design_rationale", f"AI-authored demonstration:\n{code[:400]}", exp_id)

    async def _explore_for_demonstration(
        self, plugin, demo: dict, data_spec: dict, feature_desc: str, rs: int, exp_id: str | None,
    ) -> tuple[dict | None, list[int] | None, dict | None]:
        """K1 EXPLORE phase: author + run an ``explore_demonstration`` probe on a DISJOINT explore
        subset and return ``(observations, confirm_index, split_meta)`` for calibrating + sealing
        the confirmatory demonstration. Returns ``(None, None, None)`` on any failure (infeasible
        split, worker degraded, gate reject, sandbox error) so the caller degrades to blind
        authoring. The exploration output is DESCRIPTIVE only and never enters the verdict."""
        expected_split = self._discovery_split_meta if self.hypothesis.get("discovery_sourced") else None
        split_seed = int(expected_split.get("seed", rs)) if expected_split else rs
        try:
            staged = await asyncio.to_thread(
                self._stage_explore_arrays, plugin, demo, data_spec, split_seed,
            )
        except Exception:  # noqa: BLE001 - staging best-effort -> blind fallback
            staged = None
        if staged is None:
            return None, None, None
        x_explore, y_explore, groups_explore, confirm_index, split_meta = staged
        if expected_split and split_meta.get("index_hash") != expected_split.get("index_hash"):
            await get_bus().publish(make_event(
                "demonstration_split_mismatch", run_id=self.run_id,
                payload={"discovery": expected_split, "demonstration": split_meta},
            ))
            return None, None, None

        await self._status("coding", "authoring the exploration probe")
        text = await run_worker(
            self.run_id, "coder",
            exploration_prompt(
                self.hypothesis,
                demo,
                data_spec,
                feature_desc=feature_desc,
                design=self._demonstration_design_contract,
            ),
            system=EXPLORE_SYSTEM, dry_run=self.dry_run,
            dry_value="```python\n" + CANNED_EXPLORATION + "```",
        )
        if is_degraded(text):
            return None, None, None
        code = extract_code(text)
        ok, _reasons = check_code(code, required_function=EXPLORE_REQUIRED_FUNCTION)
        if not ok:
            return None, None, None
        obs = await asyncio.to_thread(
            run_authored_exploration,
            code,
            x_explore,
            y_explore,
            groups_explore,
            {
                "random_state": rs,
                "family_alpha": float(split_meta.get("family_alpha") or 0.05),
            },
        )
        if obs is None:
            return None, None, None
        self._exploration_code = code
        # Runnability smoke-testing on random integer groups falsely rejects
        # materials code whose documented input is a chemical-system string. Keep
        # a bounded deterministic slice of the already-exposed exploration pool;
        # confirmation rows remain sealed and unseen.
        import numpy as np

        smoke_n = min(3000, int(len(y_explore)))
        smoke_idx = np.linspace(0, len(y_explore) - 1, smoke_n, dtype=int)
        self._demonstration_smoke_sample = (
            np.asarray(x_explore)[smoke_idx],
            np.asarray(y_explore)[smoke_idx],
            np.asarray(groups_explore, dtype=object)[smoke_idx]
            if groups_explore is not None else None,
        )
        await get_bus().publish(make_event("demonstration_exploration", run_id=self.run_id, payload={
            "observations": obs.get("observations"), "n_explore": obs.get("n"),
            "n_confirm": split_meta.get("n_confirm"), "split": split_meta,
            "sandbox_image_id": obs.get("sandbox_image_id")}))
        return obs, confirm_index, split_meta

    def _stage_explore_arrays(self, plugin, demo: dict, data_spec: dict, rs: int):
        """Load + featurize (the SAME way the compute step does) and return the EXPLORE-subset
        arrays + the held-out confirm index + split meta, or ``None`` when the data cannot support
        an honest 4-way split. Pure/sync (run in a thread)."""
        import numpy as np

        df = plugin.load_data(data_spec)
        sample_n = demo.get("sample_n") or data_spec.get("sample_n")
        if sample_n:
            df = df.head(int(sample_n))
            try:
                df.attrs["data_spec"] = data_spec
            except Exception:  # noqa: BLE001 - not all frames carry attrs
                pass
        X, y, names, groups = plugin.featurize(df, {"random_state": rs})
        if self._campaign_split_plan:
            identity = staged_data_identity(X, y, groups, list(names) if names is not None else [])
            expected = self._campaign_dataset_identity or {}
            if identity.get("dataset_fingerprint") != expected.get("dataset_fingerprint"):
                raise RuntimeError("staged data changed after campaign split sealing")
            if identity.get("row_identity_hash") != expected.get("row_identity_hash"):
                raise RuntimeError("row identities changed after campaign split sealing")
            explore = dict(self._campaign_split_plan.get("explore") or {})
            confirm = self._confirmation_for_attempt()
            ex = np.asarray(explore.get("indices") or [], dtype=int)
            cf = np.asarray(confirm.get("indices") or [], dtype=int)
            if ex.size == 0 or cf.size == 0:
                raise RuntimeError("sealed campaign allocation is empty")
            combined_hash = hashlib.sha256(
                f"{explore.get('index_hash')}|{confirm.get('index_hash')}".encode()
            ).hexdigest()[:16]
            split = {
                "explore_idx": ex,
                "confirm_idx": cf,
                "meta": {
                    "seed": self._campaign_split_plan.get("seed"),
                    "n_explore": int(ex.size),
                    "n_confirm": int(cf.size),
                    "split_algo_version": self._campaign_split_plan.get("split_algo_version"),
                    "group_disjoint": self._campaign_split_plan.get("group_disjoint"),
                    "index_hash": combined_hash,
                    "explore_index_hash": explore.get("index_hash"),
                    "confirmation_index_hash": confirm.get("index_hash"),
                    "confirmation_batch": confirm.get("batch"),
                    "family_alpha": confirm.get("alpha"),
                    "membership_hash": self._campaign_split_plan.get("membership_hash"),
                },
            }
        else:
            split = plugin._split_explore_confirm(groups, len(y), rs)
        if split is None:
            return None
        ex = split["explore_idx"]
        X = np.asarray(X)
        y = np.asarray(y)
        groups_explore = (np.asarray(groups, dtype=object)[ex] if groups is not None else None)
        confirm_index = [int(i) for i in split["confirm_idx"].tolist()]
        return X[ex], y[ex], groups_explore, confirm_index, split["meta"]

    @staticmethod
    def _prereg_consistent_with_exploration(prereg: dict, exploration: dict) -> tuple[bool, str]:
        """K1 seal #5 (DETERMINISTIC, harness-owned): is the committed threshold consistent with what
        the AI observed on the explore subset? Fails closed on a threshold that is doom-to-zero (its
        own explore estimate of the test statistic does not satisfy ``supported_if``), a control that
        is already not silent on explore, or a trivially-easy threshold (the control estimate ALSO
        satisfies ``supported_if`` — not discriminating). Lenient (passes) when the exploration did
        not report the expected statistics — the cross-vendor audit is then the only seal-#5 layer.
        This runs FIRST; the LLM auditor is the second, softer layer (never the only guard)."""
        obs = (exploration or {}).get("observations") or {}
        sup = prereg.get("supported_if") or {}
        sil = prereg.get("control_silent_if") or {}
        et = obs.get("expected_test_statistic")
        ec = obs.get("expected_control_statistic")
        try:
            if et is not None and not DomainPlugin._apply_rule(float(et), sup):
                return False, (f"threshold doom-to-zero: supported_if {sup} unmet by the explore-"
                               f"estimated test statistic ({et})")
            if ec is not None and not DomainPlugin._apply_rule(float(ec), sil):
                return False, (f"control not silent on explore: control_silent_if {sil} violated by "
                               f"the explore-estimated control statistic ({ec})")
            if ec is not None and DomainPlugin._apply_rule(float(ec), sup):
                return False, (f"threshold trivially easy: supported_if {sup} is also met by the "
                               f"control statistic ({ec}) — not discriminating")
        except (TypeError, ValueError):
            return True, ""  # un-parseable estimates -> defer to the LLM auditor
        return True, ""

    async def _code_rag(self, design: dict, exp_id: str | None) -> None:
        """RAG-aware coder: author the GENERATION STRATEGY (answer prompt + retrieval
        depth k) for a host-side-LLM domain, instead of build_pipeline() code. Mutates
        ``design`` with a validated ``answer_prompt`` + ``k``; falls back to the default
        prompt on any problem. The fixed harness still scores deterministically."""
        if not get_settings().coder_enabled:
            return
        await self._status("coding", "authoring the RAG generation strategy")
        methods = "\n".join(f"- {m.get('name','')}: {m.get('why','')}" for m in (self.survey_methods or [])) \
            or "- (none surveyed)"
        prompt = (
            "Design the GENERATION STRATEGY for a retrieval-augmented QA system. A fixed harness retrieves "
            "passages and scores answers against gold (recall@k + token-F1 + exact-match) — you do NOT score. "
            "Choose: a RETRIEVER ('dense' = semantic embeddings, stronger on paraphrase; or 'lexical' = "
            "token-overlap baseline); a retrieval depth k (1-10); and a concise answer PROMPT (it MUST use the "
            "placeholders {context} and {question}) that yields short, grounded answers.\n\n"
            f"PLAN/DESIGN: {json.dumps(design)}\nSURVEYED METHODS:\n{methods}\n\n"
            'Return ONLY JSON: {"retriever": "dense"|"lexical", "k": <int 1-10>, '
            '"answer_prompt": "...{context}...{question}..."}.'
        )
        text = await run_worker(
            self.run_id, "coder", prompt,
            system="You design concise, grounded RAG answer prompts; you never fabricate facts.",
            dry_run=self.dry_run,
            dry_value=json.dumps({"answer_prompt": self._DEFAULT_RAG_PROMPT, "k": int(design.get("k", 5))}),
        )
        strategy = _parse_json(text, _JSON, {}) if not is_degraded(text) else {}
        ap = str(strategy.get("answer_prompt", "")).strip()
        accepted = bool(ap) and "{context}" in ap and "{question}" in ap
        if accepted:
            design["answer_prompt"] = ap
        try:
            kk = int(strategy.get("k", design.get("k", 5)))
            design["k"] = max(1, min(10, kk))
        except (TypeError, ValueError):
            pass
        rtype = str(strategy.get("retriever", "")).lower()
        design["retriever"] = rtype if rtype in ("dense", "lexical") else design.get("retriever", "lexical")
        await get_bus().publish(
            make_event("code", run_id=self.run_id, payload={
                "accepted": accepted, "kind": "rag_strategy", "k": design.get("k"),
                "retriever": design.get("retriever"),
                "reasons": [] if accepted else ["invalid/missing answer_prompt; using default"]})
        )
        await self._index(
            "design_rationale",
            f"RAG generation strategy: retriever={design.get('retriever')}, k={design.get('k')}, "
            f"prompt={'custom' if accepted else 'default'}",
            exp_id,
        )

    async def _iam_setup(self, plan: dict, domain: str, exp_id: str | None) -> None:
        """Create (or reuse) this project's repo and the experiment's branch, behind
        the policy gate. Best-effort: any failure is logged, never breaks the run."""
        if not exp_id:
            return
        try:
            slug = plan.get("direction") or plan.get("objective") or "experiment"
            name = policy.repo_name(domain, slug)
            created_today = await asyncio.to_thread(policy.created_repos_last_24h)
            verdict = policy.check_repo_create(name, created_today)
            if not verdict.allow:
                await get_bus().publish(
                    make_event("iam", run_id=self.run_id, payload={"op": "repo_denied", "name": name, "reason": verdict.reason})
                )
                return
            repo = await asyncio.to_thread(
                self.gh.ensure_repo, name,
                description=str(plan.get("objective", ""))[:200],
                private=(get_settings().iam_repo_visibility == "private"),
            )
            self.repo = repo
            if repo.created:
                await get_bus().publish(
                    make_event("iam_repo_created", run_id=self.run_id, payload={"repo": repo.full_name, "url": repo.html_url})
                )
            branch = policy.branch_name(exp_id, plan.get("objective") or "exp")
            if policy.check_push(repo.name).allow:
                self.branch = await asyncio.to_thread(self.gh.ensure_branch, repo.full_name, branch)
            await asyncio.to_thread(set_experiment_repo, exp_id, repo.full_name, self.branch)
            await get_bus().publish(
                make_event("iam", run_id=self.run_id, payload={"op": "branch_ready", "repo": repo.full_name, "branch": self.branch, "backend": self.gh.name})
            )
        except Exception as exc:  # noqa: BLE001 - IAM is best-effort
            await get_bus().publish(
                make_event("iam", run_id=self.run_id, payload={"op": "error", "error": str(exc)})
            )

    async def _iam_finalize(self, report: str, bib: str, rpanel, exp_id: str | None) -> None:
        """Commit the cited paper (+ bibliography) to its branch and open a PR carrying
        the critic verdict (PR-per-experiment). Best-effort."""
        if not (self.repo and self.branch and exp_id):
            return
        try:
            files = {"report.md": report}
            if bib:
                files["references.bib"] = bib
            for fname, content in files.items():
                await asyncio.to_thread(
                    self.gh.put_file, self.repo.full_name, fname, content,
                    message=f"experiment {exp_id}: {fname}", branch=self.branch,
                )
            body = (
                f"Autonomous experiment `{exp_id}`.\n\n"
                f"**Critic consensus:** {rpanel.consensus_verdict} "
                f"(gate {'passed' if rpanel.gate_passed else 'failed'}).\n\n"
                f"Cited paper (`report.md`{', `references.bib`' if bib else ''}) committed on this branch."
            )
            pr = await asyncio.to_thread(
                self.gh.open_pr, self.repo.full_name,
                head=self.branch, base="main",
                title=f"Experiment {exp_id}: {rpanel.consensus_verdict}", body=body,
            )
            await asyncio.to_thread(record_artifacts, exp_id, [{"kind": "pr", "uri": pr.html_url}])
            await get_bus().publish(
                make_event("iam_pr_opened", run_id=self.run_id, payload={"repo": self.repo.full_name, "url": pr.html_url, "number": pr.number})
            )
        except Exception as exc:  # noqa: BLE001 - IAM is best-effort
            await get_bus().publish(
                make_event("iam", run_id=self.run_id, payload={"op": "error", "error": str(exc)})
            )

    async def _status(self, state: str, detail: str | None = None) -> None:
        await get_bus().publish(
            make_event("status", run_id=self.run_id, payload={"state": state, "detail": detail})
        )

    async def _block_run(self, exp_id: str | None, reason: str) -> None:
        """Pause a real run when required scientific grounding is missing."""
        await get_bus().publish(
            make_event("research_blocked", run_id=self.run_id, payload={"reason": reason})
        )
        await record_transition(self.run_id, exp_id, None, "paused", reason)
        await asyncio.to_thread(set_run_status, self.run_id, "paused")
        await self._status("paused", reason)

    async def _post_execution_guards(self, result: dict[str, Any], exp_id: str | None) -> bool:
        """Fail-closed checks after EXECUTION (real runs only). Returns True to
        continue, False if the run was blocked (paused). Dry runs always continue.

        - Artifact completeness: the honest harness must have produced the eval
          record + the fitted model; a result missing them is unverifiable.
        - Protocol degradation: a `degraded_kfold` headline (no usable grouping) is
          surfaced as a `research_degraded` state, not hidden — the claim-strength
          rule then caps any metric claim resting on it (it does NOT pause).
        """
        if self.dry_run:
            return True
        info = result.get("info") or {}
        kinds = {a.get("kind") for a in (result.get("artifacts") or [])}
        # a PARADIGM run is verifiable through its discriminating DEMONSTRATION, not a
        # fitted model — a failed/timed-out performance eval is a limitation (recorded in
        # write-up), NOT a blocker. A MISSING demonstration is no longer hard-paused here:
        # K2 S3.5 turns it into a bounded campaign PIVOT (an informative negative), decided
        # in the campaign loop — _run_experiment surfaces it as an "undemonstrated" outcome.
        if self._uses_discriminating_demonstration():
            pass  # skip the fitted-artifact requirement; the loop handles a missing demonstration
        else:
            required = set(self.profile.required_artifacts) if self.profile else {"eval", "model"}
            missing = required - kinds
            if missing or not result.get("metrics"):
                await self._block_run(
                    exp_id,
                    f"execution did not produce verifiable artifacts (missing: "
                    f"{', '.join(sorted(missing)) or 'metrics'}); pausing before analysis",
                )
                return False
        if info.get("protocol_status") == "degraded_kfold":
            await get_bus().publish(
                make_event(
                    "research_degraded",
                    run_id=self.run_id,
                    payload={
                        "reason": "headline used plain KFold (no usable grouping); "
                        "grouped-CV claim downgraded",
                    },
                )
            )
            await self._status("degraded", "headline protocol degraded to KFold")
        return True

    @staticmethod
    def _repro_match(original: float | None, repro: float | None, tol: float) -> tuple[bool, float | None]:
        """Did the re-run reproduce the headline within a relative tolerance?"""
        if original is None or repro is None:
            return False, None
        denom = max(abs(float(original)), 1e-9)
        rel = abs(float(original) - float(repro)) / denom
        return rel <= tol, round(rel, 4)

    async def _reproduce(
        self, design: dict, data_spec: dict, domain: str | None, result: dict, exp_id: str | None
    ) -> dict[str, Any]:
        """Independent re-run (locked code + a new seed) through the FIXED harness, to
        confirm the headline before it can be claimed `strong`. Re-runs with
        ``experiment_id=None`` so it lands in its own workdir and never clobbers the
        original's metrics/artifacts. Best-effort — a failed re-run is recorded as a
        non-reproduction, never crashes the experiment."""
        if not get_settings().reproduction_enabled:
            return {"attempted": False}
        hk = (
            "diagnostic_test_statistic"
            if self._contribution_type() == "diagnostic"
            else (self.profile.headline_metric if self.profile else "mae")
        )
        original = (result.get("metrics") or {}).get(hk)
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await self._status("reproducing", "independent re-run (locked code, new seed)")
        repro_design = {**design, "random_state": int(design.get("random_state", 42)) + 1}
        try:
            repro_result = await self._run_eval(repro_design, data_spec, domain, None)
        except Exception as exc:  # noqa: BLE001 - reproduction is best-effort
            await get_bus().publish(
                make_event("reproduction", run_id=self.run_id,
                           payload={"attempted": True, "reproduced": False, "error": str(exc)})
            )
            return {"attempted": True, "reproduced": False, "error": str(exc),
                    "mode": "locked-code reseed", "metric": hk, "original": original}
        repro_v = (repro_result.get("metrics") or {}).get(hk)
        reproduced, delta = self._repro_match(original, repro_v, get_settings().reproduction_rel_tol)
        payload = {"attempted": True, "reproduced": reproduced, "mode": "locked-code reseed",
                   "metric": hk, "original": original, "repro": repro_v, "delta": delta}
        # paradigm contributions: reproduce the DISCRIMINATING DEMONSTRATION, not a metric —
        # it is reproduced only if it held on BOTH runs and its statistic is stable.
        orig_demo = (result.get("info") or {}).get("demonstration")
        repro_demo = (repro_result.get("info") or {}).get("demonstration")
        if isinstance(orig_demo, dict):
            # DECOMPOSED stability (review Finding 5 — one opaque bool hid WHICH kind of
            # reproduction was achieved; seen live: the statistic swung 20x across seeds and
            # the payload couldn't show it):
            #   verdict_stable    — QUALITATIVE: the harness verdict (holds) is the same on the
            #                       seed-perturbed re-run (True for a twice-refuted demo too);
            #   statistic_stable  — STATISTICAL: the statistic stays within the capability's
            #                       own ``reproduce_factor`` (Codex #5, default 2x; None when
            #                       either run produced no statistic).
            # ``demonstration_reproduced`` (the strength-escalation gate to `strong`) stays
            # STRICT: held on BOTH runs AND the statistic did not swing beyond tolerance.
            # Both samples + seeds are persisted so a reviewer can audit the swing directly.
            s1, s2 = orig_demo.get("statistic"), (repro_demo or {}).get("statistic")
            h1, h2 = orig_demo.get("holds"), (repro_demo or {}).get("holds")
            verdict_stable = (h2 is not None) and (bool(h1) == bool(h2))
            factor = max(1.0, float(orig_demo.get("reproduce_factor", 2.0)))
            statistic_stable: bool | None = None
            if s1 is not None and s2 is not None:
                lo, hi = sorted((abs(float(s1)), abs(float(s2))))
                statistic_stable = hi <= 1e-9 or (lo / hi) >= 1.0 / factor
            # K1: the re-run must recompute on the SAME held-out CONFIRM partition (same
            # explore->confirm seal). A missing confirm statistic (s2 None -> statistic_stable not
            # True) or a mismatched split (different index_hash) is NOT a reproduction.
            sm1 = orig_demo.get("split_meta") or {}
            sm2 = (repro_demo or {}).get("split_meta") or {}
            split_match = (not sm1) or (sm1.get("index_hash") == sm2.get("index_hash"))
            payload["demonstration_reproduced"] = (
                bool(h1) and bool(h2) and statistic_stable is True and split_match
            )
            payload["demonstration_verdict_stable"] = verdict_stable
            payload["demonstration_statistic_stable"] = statistic_stable
            payload["demonstration_split_match"] = split_match
            payload["demonstration_original_statistic"] = s1
            payload["demonstration_repro_statistic"] = s2
            payload["demonstration_reproduce_factor"] = factor
            payload["demonstration_seeds"] = [
                int(design.get("random_state", 42)), int(repro_design["random_state"]),
            ]
        await get_bus().publish(make_event("reproduction", run_id=self.run_id, payload=payload))
        await record_transition(
            self.run_id, exp_id, "analysis", "analysis",
            f"reproduction ({hk}): original={original} repro={repro_v} -> "
            f"{'confirmed' if reproduced else 'NOT confirmed'} (rel Δ {delta})",
        )
        return payload

    @staticmethod
    def _results_review_payload(
        design: dict, result: dict, analysis: Any, claims: Any,
        contribution_type: str = "performance", demonstration: dict | None = None,
    ) -> dict:
        """The evidence package the critic panel reviews at the results gate. Includes
        the harness's own ``method_drift`` finding (+ requested/executed method) so the
        critic evaluates plan↔execution drift directly, not by inferring it from code.
        For a ``paradigm`` contribution it also carries the discriminating demonstration
        the critic must judge (SOTA-delta is irrelevant in that mode)."""
        info = result.get("info") or {}
        return {
            "artifact_locked": True,
            "design": design,
            "metrics": result.get("metrics"),
            "eval_summary": info.get("eval_summary", ""),
            "protocol_status": info.get("protocol_status"),
            "degraded": info.get("protocol_status") == "degraded_kfold",
            "method_drift": bool(info.get("method_drift")),
            "method_drift_msg": info.get("method_drift_msg", ""),
            "requested_method": info.get("requested_method") or design.get("model", ""),
            "executed_impl": info.get("model_impl", ""),
            "reproduction": info.get("reproduction"),
            "contribution_type": contribution_type,
            "demonstration": demonstration,  # the PROPOSED discriminating demonstration
            # the COMPUTED, harness-owned result {form, holds, statistic, detail} — so the
            # critic judges what was actually computed (and whether it matches the claim),
            # not just the proposal. This is what makes the paradigm gate adversarially real.
            "demonstration_result": info.get("demonstration"),
            "claims": claims,
            "analysis": analysis,
            # the coder's actual model code, so an adversarial reviewer can check
            # for leakage / metric-gaming, not just trust the reported numbers. Capped so
            # the CLI critics (which pass content as a process arg) never approach ARG_MAX.
            "solution_code": str(design.get("solution_code", ""))[:20000],
        }

    async def _finalize_claims(
        self, protocol_status: str | None, rpanel, reproduction: dict | None = None,
        exp_id: str | None = None, ablation: dict | None = None,
        method_not_instantiated: bool = False, demonstration: dict | None = None,
        audit_passed: bool | None = None, audit_error: bool = False,
        credence: belief.Credence | None = None,
    ) -> None:
        """After the results gate, set the final strength + status of the proposed
        metric/mechanism claims from the (harness-owned) evidence rule, and record a
        reproducibility claim from the independent re-run.

        ``method_not_instantiated``: the executed model did not match the family the
        hypothesis names, so the mechanism was never tested — its claim is marked
        ``not_evaluated`` (NOT ``refuted``: refuted would wrongly imply it was tried
        and failed)."""
        verdict = rpanel.consensus_verdict
        passed = bool(rpanel.gate_passed)
        status = "supported" if passed else ("refuted" if verdict == "reject" else "unverified")
        repro = reproduction or {}
        # reproduced: True confirmed / False contradicted / None not attempted
        reproduced = repro.get("reproduced") if repro.get("attempted") else None
        # cross-vendor review is the load-bearing guarantee: count DISTINCT vendors that
        # actually reviewed; below the floor the gate is `degraded_review` and gate-derived
        # claims are capped at `weak` (a gate that survived on one vendor — e.g. same-vendor
        # self-review — is not strong evidence).
        n_vendors = len({c.critic_id for c in (getattr(rpanel, "critiques", None) or [])})
        cross_vendor = n_vendors >= int(get_settings().min_review_vendors)
        if passed and not cross_vendor:
            await get_bus().publish(make_event(
                "degraded_review", run_id=self.run_id,
                payload={"target": "results", "n_vendors": n_vendors,
                         "min_vendors": int(get_settings().min_review_vendors),
                         "reason": "gate passed on single-vendor review; claims capped at weak"}))
        if self._claim_ids.get("metric"):
            await asyncio.to_thread(
                update_claim, self._claim_ids["metric"],
                strength=self._claim_strength(
                    "metric", protocol_status=protocol_status, gate_passed=passed,
                    gate_verdict=verdict, reproduced=reproduced, cross_vendor=cross_vendor,
                ),
                status=status,
            )
        if self._claim_ids.get("mechanism"):
            mech_status = "not_evaluated" if method_not_instantiated else status
            await asyncio.to_thread(
                update_claim, self._claim_ids["mechanism"],
                strength="weak" if method_not_instantiated else self._claim_strength(
                    "mechanism", gate_passed=passed, cross_vendor=cross_vendor),
                status=mech_status,
            )
        # a PARADIGM contribution's formulation claim is grounded by its reproducible
        # discriminating demonstration (NOT by SOTA-delta). No demonstration executed ->
        # `not_evaluated`/`speculative` (it stays an honest proposal, never a finding).
        if self._claim_ids.get("formulation"):
            demo = demonstration if isinstance(demonstration, dict) else {}
            holds = demo.get("holds")  # True | False | None(not executed)
            demo_repro = repro.get("demonstration_reproduced") if repro.get("attempted") else None
            # K1: an AI-authored demonstration that ran WITHOUT the explore->confirm seal (blind
            # fallback) cannot reach `strong` — see _claim_strength. Registered capabilities (a
            # different `capability`) are unaffected.
            exploration_missing = (
                demo.get("capability") == DomainPlugin.AI_AUTHORED_CAPABILITY_ID
                and not demo.get("exploration_applied")
            )
            # `refuted` is reserved for a demonstration that was TESTED and did NOT hold —
            # NOT for one that held but the gate didn't endorse (that is `unverified`),
            # mirroring the not_evaluated/refuted distinction elsewhere.
            if holds is None:
                form_status = "not_evaluated"
            elif holds is False:
                form_status = "refuted"  # the discriminating demonstration was contradicted
            elif passed:
                form_status = "supported"
            else:
                form_status = "unverified"  # demonstration held, but peer review didn't endorse it
            # K2-S5: a credence still a weak prior (too few harness-verified confirm-split rounds)
            # cannot yield a `strong` formulation claim — strong requires accumulated, replicated
            # belief, not one round. Fail-closed: no credence supplied => treated as weak.
            weak_prior = belief.is_weak_prior(credence) if credence is not None else True
            await asyncio.to_thread(
                update_claim, self._claim_ids["formulation"],
                strength=self._claim_strength(
                    "formulation", gate_passed=passed, gate_verdict=verdict,
                    reproduced=demo_repro, demonstration_holds=holds, cross_vendor=cross_vendor,
                    audit_passed=audit_passed, audit_error=audit_error,
                    exploration_missing=exploration_missing, weak_prior=weak_prior,
                ),
                status=form_status,
            )
            # record the calibrated credence behind that strength, so the cap is auditable.
            if credence is not None:
                await asyncio.to_thread(
                    attach_claim_evidence, self._claim_ids["formulation"], "credence",
                    f"Beta(alpha={credence.alpha:.2f}, beta={credence.beta:.2f})",
                    note=(f"mean P(holds)={belief.mean(credence):.3f}; n_updates={credence.n_updates}; "
                          f"weak_prior={weak_prior}"),
                )
        # a reproducibility claim: did an independent re-run confirm the headline?
        if repro.get("attempted"):
            confirmed = bool(repro.get("reproduced"))
            # rel Δ None means NO comparison ever happened (no headline value on the
            # original and/or the re-run, or the re-run errored). That is ``not_evaluated``
            # — ``refuted`` is reserved for a comparison that RAN and contradicted the
            # headline (the same refuted/not-evaluated distinction as elsewhere).
            compared = repro.get("delta") is not None
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=(
                    f"An independent re-run ({repro.get('mode', 'rerun')}) "
                    + (
                        f"{'confirmed' if confirmed else 'did NOT confirm'} the headline "
                        f"{repro.get('metric', '')} (original={repro.get('original')}, "
                        f"reproduced={repro.get('repro')}, rel Δ {repro.get('delta')})."
                        if compared
                        else f"could not evaluate the headline {repro.get('metric', '')} "
                        f"(original={repro.get('original')}, reproduced={repro.get('repro')} "
                        f"— no comparable value on both runs)."
                    )
                ),
                claim_type="reproducibility",
                strength="moderate" if confirmed else "weak",
                status=("supported" if confirmed else ("refuted" if compared else "not_evaluated")),
                experiment_id=exp_id, created_by="reproduction", stage="analysis",
                evidence=[
                    {"evidence_kind": "reproduction", "evidence_ref": repro.get("mode", "rerun"),
                     "note": f"original={repro.get('original')} reproduced={repro.get('repro')}"}
                ],
            )
        # a comparison claim from a controlled retriever ablation (dense vs lexical)
        if ablation:
            d = ablation.get("delta", {})
            df1 = float(d.get("answer_f1", 0.0) or 0.0)
            drec = float(d.get("recall_at_k", 0.0) or 0.0)
            beats = df1 > 0 or drec > 0
            reproduced = bool(repro.get("reproduced")) if repro.get("attempted") else None
            strength = ("strong" if (beats and reproduced) else "moderate") if beats else "weak"
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=(
                    f"Retriever ablation (answerer fixed): dense "
                    f"{'beats' if beats else 'does not beat'} lexical "
                    f"(Δrecall@k {drec:+.3f}, ΔF1 {df1:+.3f})."
                ),
                claim_type="comparison",
                strength=strength,
                status="supported" if beats else "refuted",
                experiment_id=exp_id, created_by="ablation", stage="analysis",
                evidence=[
                    {"evidence_kind": "metric", "evidence_ref": "recall_at_k_delta", "note": f"{drec:+.3f}"},
                    {"evidence_kind": "metric", "evidence_ref": "answer_f1_delta", "note": f"{df1:+.3f}"},
                    {"evidence_kind": "artifact", "evidence_ref": "ablation.json", "note": "controlled comparison"},
                ],
            )
        # surface the verification spine's outcome: the final claim ledger (type/status/strength
        # + which evidence kinds grounded each) so the UI can show what was actually established,
        # refuted, or left unverified — not just the headline metric.
        try:
            ledger = await asyncio.to_thread(list_claims, self.run_id)
            await get_bus().publish(make_event(
                "claims", run_id=self.run_id,
                payload={"claims": [
                    {"claim_type": c.get("claim_type"), "status": c.get("status"),
                     "strength": c.get("strength"),
                     "evidence_kinds": sorted({e.get("evidence_kind")
                                               for e in (c.get("evidence") or [])
                                               if e.get("evidence_kind")}),
                     "claim_text": str(c.get("claim_text", ""))[:240]}
                    for c in ledger]}))
        except Exception:  # noqa: BLE001 - the claims summary is a best-effort UI signal
            pass

    async def _guard_budget(self, kind: str, amount: float) -> None:
        """Charge an estimated cost and auto-pause + notify if a cap is breached."""
        if self.budget is None:
            return
        cum = await asyncio.to_thread(self.budget.charge, kind, amount)
        await get_bus().publish(
            make_event(
                "budget",
                run_id=self.run_id,
                payload={"kind": kind, "amount": amount, "cumulative": cum, "cap": self.budget.cap_usd},
            )
        )
        breaches = self.budget.breaches()
        if breaches:
            await get_bus().publish(
                make_event("budget_breach", run_id=self.run_id, payload={"breaches": breaches})
            )
            await asyncio.to_thread(set_run_status, self.run_id, "paused")
            await notify_feishu(
                f"Aletheia run {self.run_id} auto-paused: budget breach {breaches}",
                run_id=self.run_id,
            )
            await self._status("paused", "budget cap reached")
            raise BudgetPaused()

    @staticmethod
    def _build_campaign_split(plugin, data_spec: dict[str, Any], batches: int):
        """Trusted host-side staging used once, before the first adaptive decision."""
        settings = get_settings()
        df = plugin.load_data(data_spec)
        X, y, names, groups = plugin.featurize(
            df, {"random_state": int(settings.campaign_split_seed)}
        )
        identity = staged_data_identity(X, y, groups, list(names) if names is not None else [])
        split = allocate_campaign_splits(
            groups,
            len(y),
            seed=int(settings.campaign_split_seed),
            confirmation_batches=int(batches),
            explore_fraction=float(settings.campaign_explore_fraction),
            final_holdout_fraction=float(settings.campaign_final_holdout_fraction),
            min_confirmation_n=2 * int(settings.demonstration_min_samples),
            family_alpha=float(settings.campaign_family_alpha),
            final_alpha=float(settings.campaign_final_alpha),
        )
        return identity, split

    async def _seal_campaign_data(
        self, plan: dict[str, Any], plugin: Any, data_spec: dict[str, Any], batches: int
    ) -> bool:
        """Seal all campaign row roles before literature synthesis or hypothesis ideation."""
        try:
            identity, proposed = await asyncio.to_thread(
                self._build_campaign_split, plugin, data_spec, batches
            )
            sealed = await asyncio.to_thread(
                seal_campaign_splits,
                self.run_id,
                dataset_fingerprint=identity["dataset_fingerprint"],
                row_identity_hash=identity["row_identity_hash"],
                plan=proposed,
            )
        except Exception as exc:
            await self._block_run(
                (await asyncio.to_thread(get_run, self.run_id) or {}).get("plan_experiment_id"),
                f"could not create immutable campaign split ledger: {exc}",
            )
            return False
        self._campaign_dataset_identity = identity
        self._campaign_split_plan = dict(sealed["plan"])
        family_material = "|".join(
            [
                str(plan.get("objective") or plan.get("direction") or "campaign"),
                identity["dataset_fingerprint"],
            ]
        )
        self._family_key = hashlib.sha256(family_material.encode()).hexdigest()
        await get_bus().publish(
            make_event(
                "campaign_split_reused" if sealed.get("reused") else "campaign_split_sealed",
                run_id=self.run_id,
                payload={
                    **public_split_summary(self._campaign_split_plan),
                    "dataset_fingerprint": identity["dataset_fingerprint"],
                    "row_identity_hash": identity["row_identity_hash"],
                    "family_key": self._family_key,
                    "reused": bool(sealed.get("reused")),
                },
            )
        )
        return True

    @staticmethod
    def _build_external_identity(plugin: Any, data_spec: dict[str, Any]):
        """Trusted staging pass; no authored/model code or adaptive logic sees these values."""
        from pathlib import Path

        from aletheia.data.external_supercon2 import file_sha256

        declared_sha = str(data_spec.get("content_sha256") or "").strip()
        uri = data_spec.get("uri")
        if not declared_sha:
            raise RuntimeError("external-validation asset requires a declared content SHA-256")
        if uri and Path(str(uri)).is_file():
            actual_sha = file_sha256(Path(str(uri)))
            if actual_sha != declared_sha:
                raise RuntimeError(
                    f"external-validation content hash mismatch: {actual_sha} != {declared_sha}"
                )
        df = plugin.load_data(data_spec)
        X, y, names, groups = plugin.featurize(
            df, {"random_state": int(get_settings().campaign_split_seed)}
        )
        identity = staged_data_identity(X, y, groups, list(names) if names is not None else [])
        identity["n_rows"] = int(len(y))
        return identity

    async def _seal_external_data(self, plugin: Any) -> bool:
        """Seal a separately sourced validation asset before the adaptive campaign starts."""
        try:
            spec = await asyncio.to_thread(resolve_external_data_spec, self.run_id)
        except Exception as exc:
            await self._block_run(None, f"could not resolve external-validation asset: {exc}")
            return False
        required = bool(get_settings().campaign_external_validation_required)
        if spec is None:
            if required:
                await self._block_run(
                    None, "campaign requires a ready external_validation data asset"
                )
                return False
            return True
        try:
            identity = await asyncio.to_thread(self._build_external_identity, plugin, spec)
            provenance = {
                "asset_id": spec.get("asset_id"),
                "source": spec.get("source"),
                "ref": spec.get("ref"),
                "content_sha256": spec.get("content_sha256"),
                "profile": spec.get("profile") or {},
                "n_featurized_rows": identity["n_rows"],
            }
            sealed = await asyncio.to_thread(
                seal_external_validation,
                self.run_id,
                data_asset_id=str(spec.get("asset_id") or ""),
                dataset_fingerprint=identity["dataset_fingerprint"],
                row_identity_hash=identity["row_identity_hash"],
                provenance=provenance,
            )
        except Exception as exc:
            await self._block_run(None, f"could not seal external-validation asset: {exc}")
            return False
        self._external_validation_spec = spec
        self._external_dataset_identity = identity
        await get_bus().publish(
            make_event(
                "external_validation_reused" if sealed.get("reused") else "external_validation_sealed",
                run_id=self.run_id,
                payload={
                    "asset_id": spec.get("asset_id"),
                    "dataset_fingerprint": identity["dataset_fingerprint"],
                    "row_identity_hash": identity["row_identity_hash"],
                    "n_rows": identity["n_rows"],
                    "content_sha256": spec.get("content_sha256"),
                    "reused": bool(sealed.get("reused")),
                },
            )
        )
        return True

    def _confirmation_for_attempt(self, attempt_sequence: int | None = None) -> dict[str, Any]:
        plan = self._campaign_split_plan or {}
        confirmations = list(plan.get("confirmations") or [])
        idx = int(attempt_sequence or self._current_attempt_sequence) - 1
        if idx < 0 or idx >= len(confirmations):
            raise RuntimeError(
                "fresh confirmation data exhausted; campaign must stop rather than reshuffle"
            )
        return dict(confirmations[idx])

    async def _register_family_attempt(
        self, exp_id: str | None, round_idx: int
    ) -> bool:
        if not self._campaign_split_plan:
            return True
        try:
            batch = self._confirmation_for_attempt()
            statement = str(self.hypothesis.get("statement") or "").strip() or "(unnamed hypothesis)"
            attempt_id = await asyncio.to_thread(
                register_hypothesis_attempt,
                self.run_id,
                experiment_id=exp_id,
                family_key=self._family_key,
                hypothesis_text=statement,
                round_index=round_idx,
                phase="confirmation",
                confirmation_batch=int(batch["batch"]),
                split_hash=str(batch["index_hash"]),
                alpha_allocated=float(batch["alpha"]),
            )
            self._current_attempt_id = attempt_id
            await get_bus().publish(
                make_event(
                    "hypothesis_attempt",
                    run_id=self.run_id,
                    payload={
                        "attempt_id": attempt_id,
                        "round": round_idx,
                        "phase": "confirmation",
                        "batch": batch["batch"],
                        "split_hash": batch["index_hash"],
                        "alpha": batch["alpha"],
                        "family_key": self._family_key,
                        "status": "registered",
                    },
                )
            )
            return True
        except Exception as exc:
            await self._block_run(exp_id, str(exc))
            return False

    async def _finish_family_attempt(self, outcome: dict[str, Any]) -> None:
        if self._current_attempt_id is None:
            return
        compact = {
            "reason": outcome.get("reason"),
            "verdict": outcome.get("verdict"),
            "demonstration_holds": outcome.get("demonstration_holds"),
            "undemonstrated": bool(outcome.get("undemonstrated")),
            "headline_metric": outcome.get("headline_metric"),
            "headline": outcome.get("headline"),
        }
        status = "not_evaluated" if outcome.get("undemonstrated") else "evaluated"
        await asyncio.to_thread(
            finish_hypothesis_attempt,
            self._current_attempt_id,
            status=status,
            outcome=compact,
        )
        await get_bus().publish(
            make_event(
                "hypothesis_attempt",
                run_id=self.run_id,
                payload={
                    "attempt_id": self._current_attempt_id,
                    "round": outcome.get("round"),
                    "phase": "confirmation",
                    "status": status,
                    "outcome": compact,
                },
            )
        )

    def _select_final_candidate(self, outcomes: list[dict[str, Any]]) -> dict[str, Any] | None:
        if (
            self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
            and self._locked_diagnostic_artifact is not None
            and not self._locked_diagnostic_artifact.get("eligible")
        ):
            return None
        candidates = [o for o in outcomes if o.get("_final_candidate")]
        if not candidates:
            return None
        if (
            self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
        ):
            # Predeclared anti-winner's-curse policy: lock the first committable
            # implementation. Later batches measure replication; they do not permit
            # choosing the largest observed effect after several looks.
            return candidates[0]
        scored = [o for o in candidates if o.get("headline") is not None]
        return (
            (max if self._maximize() else min)(scored, key=lambda o: o["headline"])
            if scored
            else candidates[-1]
        )

    async def _validate_final_holdout(
        self, outcomes: list[dict[str, Any]], data_spec: dict[str, Any], plugin: Any
    ) -> dict[str, Any]:
        """Select on confirmation evidence, then open the reserved holdout exactly once."""
        if not self._campaign_split_plan:
            return {"evaluated": False, "reason": "campaign split ledger disabled"}
        selected = self._select_final_candidate(outcomes)
        if selected is None:
            return {"evaluated": False, "reason": "no demonstrated candidate for final holdout"}
        final = dict(self._campaign_split_plan.get("final_holdout") or {})
        candidate = dict(selected["_final_candidate"])
        code_text = str(candidate["code"])
        prereg_text = json.dumps(candidate["preregistration"], indent=2, sort_keys=True)
        code_sha256 = hashlib.sha256(code_text.encode()).hexdigest()
        prereg_blob = prereg_text + "\n"
        prereg_sha256 = hashlib.sha256(prereg_blob.encode()).hexdigest()
        locked_dir = run_artifacts_dir(self.run_id) / "locked_validation"
        locked_dir.mkdir(parents=True, exist_ok=True)
        locked_code = locked_dir / "demonstration.py"
        locked_prereg = locked_dir / "preregistration.json"
        locked_code.write_text(code_text)
        locked_prereg.write_text(prereg_blob)
        attempt_id = await asyncio.to_thread(
            register_hypothesis_attempt,
            self.run_id,
            experiment_id=selected.get("exp_id"),
            family_key=self._family_key,
            hypothesis_text=str(selected.get("hypothesis") or "(unnamed hypothesis)"),
            round_index=int(selected.get("round") or 0),
            phase="final_holdout",
            confirmation_batch=None,
            split_hash=str(final.get("index_hash") or ""),
            alpha_allocated=float(final.get("alpha") or 0.0),
        )
        claim = await asyncio.to_thread(
            claim_final_holdout,
            self.run_id,
            claim_owner=f"experiment-driver:{self.run_id}",
        )
        if not claim.get("claimed"):
            if claim.get("result") is not None:
                return dict(claim["result"])
            raise RuntimeError(
                "final holdout was already claimed without a completed receipt; "
                "reconciliation is required and a second look is forbidden"
            )
        action_id = str(claim["action_id"])
        execution_token = str(claim["execution_token"])

        split_meta = {
            "split_algo_version": self._campaign_split_plan.get("split_algo_version"),
            "role": "final_holdout",
            "index_hash": final.get("index_hash"),
            "n_confirm": final.get("n"),
            "group_disjoint": self._campaign_split_plan.get("group_disjoint"),
            "alpha": final.get("alpha"),
        }
        spec = {
            **dict(candidate.get("demonstration") or {}),
            "capability": plugin.AI_AUTHORED_CAPABILITY_ID,
            "demonstration_code": candidate["code"],
            "preregistration": candidate["preregistration"],
            "random_state": int(candidate.get("random_state", 42)),
            "confirm_index": list(final.get("indices") or []),
            "split_meta": split_meta,
            "family_alpha": float(final.get("alpha") or 0.0),
            "external_action": {
                "action_id": action_id,
                "provider_idempotency_key": claim["provider_idempotency_key"],
                "delivery": "at_most_once_reconcile_on_unknown",
            },
        }
        execution_status = "returned"
        try:
            demo = await asyncio.to_thread(
                plugin.run_demonstration,
                spec,
                data_spec,
                run_artifacts_dir(self.run_id) / "final_holdout",
            )
            result = {
                "evaluated": isinstance(demo, dict) and demo.get("holds") is not None,
                "selected_round": selected.get("round"),
                "selected_experiment_id": selected.get("exp_id"),
                "hypothesis": selected.get("hypothesis"),
                "holds": demo.get("holds") if isinstance(demo, dict) else None,
                "test_statistic": demo.get("test_statistic") if isinstance(demo, dict) else None,
                "control_statistic": demo.get("control_statistic") if isinstance(demo, dict) else None,
                "detail": demo.get("detail") if isinstance(demo, dict) else "demonstration failed",
                "split_hash": final.get("index_hash"),
                "n": final.get("n"),
                "alpha": final.get("alpha"),
                "sandbox_image_id": demo.get("sandbox_image_id") if isinstance(demo, dict) else None,
                "locked_code_sha256": code_sha256,
                "locked_preregistration_sha256": prereg_sha256,
            }
        except Exception as exc:
            execution_status = "raised"
            result = {
                "evaluated": False,
                "selected_round": selected.get("round"),
                "selected_experiment_id": selected.get("exp_id"),
                "hypothesis": selected.get("hypothesis"),
                "holds": None,
                "detail": f"final holdout execution failed: {exc}",
                "split_hash": final.get("index_hash"),
                "n": final.get("n"),
                "alpha": final.get("alpha"),
                "locked_code_sha256": code_sha256,
                "locked_preregistration_sha256": prereg_sha256,
            }
        await asyncio.to_thread(
            record_final_holdout_result,
            self.run_id,
            result,
            action_id=action_id,
            execution_token=execution_token,
            provider_receipt={
                "executor": "plugin.run_demonstration",
                "execution_status": execution_status,
                "sandbox_image_id": result.get("sandbox_image_id"),
            },
            completed_by=f"experiment-driver:{self.run_id}",
        )
        await asyncio.to_thread(
            finish_hypothesis_attempt,
            attempt_id,
            status="evaluated" if result["evaluated"] else "not_evaluated",
            outcome=result,
        )
        artifact = run_artifacts_dir(self.run_id) / "final_holdout.json"
        artifact.write_text(json.dumps(result, indent=2, default=str))
        if selected.get("exp_id"):
            await asyncio.to_thread(
                record_artifacts,
                selected["exp_id"],
                [
                    {"kind": "final_holdout", "uri": str(artifact)},
                    {"kind": "locked_demonstration_code", "uri": str(locked_code)},
                    {"kind": "locked_preregistration", "uri": str(locked_prereg)},
                ],
            )
        await get_bus().publish(make_event("final_holdout", run_id=self.run_id, payload=result))
        return result

    async def _validate_external_replication(
        self, outcomes: list[dict[str, Any]], plugin: Any
    ) -> dict[str, Any] | None:
        """Run the locked winning code/rule once on the pre-sealed external dataset."""
        if self._external_validation_spec is None:
            return None
        selected = self._select_final_candidate(outcomes)
        if selected is None:
            return {"evaluated": False, "reason": "no demonstrated candidate for external replication"}
        candidate = dict(selected["_final_candidate"])
        code_sha256 = hashlib.sha256(str(candidate["code"]).encode()).hexdigest()
        prereg_blob = json.dumps(candidate["preregistration"], indent=2, sort_keys=True) + "\n"
        prereg_sha256 = hashlib.sha256(prereg_blob.encode()).hexdigest()
        identity = await asyncio.to_thread(
            self._build_external_identity, plugin, self._external_validation_spec
        )
        expected = self._external_dataset_identity or {}
        if identity.get("dataset_fingerprint") != expected.get("dataset_fingerprint"):
            raise RuntimeError("external dataset changed after pre-campaign sealing")
        if identity.get("row_identity_hash") != expected.get("row_identity_hash"):
            raise RuntimeError("external row identities changed after pre-campaign sealing")

        alpha = float(get_settings().campaign_external_alpha)
        attempt_id = await asyncio.to_thread(
            register_hypothesis_attempt,
            self.run_id,
            experiment_id=selected.get("exp_id"),
            family_key=self._family_key,
            hypothesis_text=str(selected.get("hypothesis") or "(unnamed hypothesis)"),
            round_index=int(selected.get("round") or 0),
            phase="external_replication",
            confirmation_batch=None,
            split_hash=str(identity["row_identity_hash"]),
            alpha_allocated=alpha,
        )
        claim = await asyncio.to_thread(
            claim_external_validation,
            self.run_id,
            claim_owner=f"experiment-driver:{self.run_id}",
            claim_ttl_seconds=max(
                60,
                int(get_settings().campaign_external_timeout_s) + 300,
            ),
        )
        if not claim.get("claimed"):
            if claim.get("result") is not None:
                return dict(claim["result"])
            raise RuntimeError(
                "external-validation action was already claimed without a completed receipt; "
                "reconciliation is required and a second look is forbidden"
            )
        action_id = str(claim["action_id"])
        execution_token = str(claim["execution_token"])

        split_meta = {
            "role": "external_replication",
            "dataset_fingerprint": identity["dataset_fingerprint"],
            "row_identity_hash": identity["row_identity_hash"],
            "n_confirm": identity["n_rows"],
            "alpha": alpha,
            "locked_after_round": selected.get("round"),
        }
        spec = {
            **dict(candidate.get("demonstration") or {}),
            "capability": plugin.AI_AUTHORED_CAPABILITY_ID,
            "demonstration_code": candidate["code"],
            "preregistration": candidate["preregistration"],
            "random_state": int(candidate.get("random_state", 42)),
            "confirm_index": list(range(int(identity["n_rows"]))),
            "split_meta": split_meta,
            "family_alpha": alpha,
            "execution_timeout_s": float(get_settings().campaign_external_timeout_s),
            "external_action": {
                "action_id": action_id,
                "provider_idempotency_key": claim["provider_idempotency_key"],
                "delivery": "at_most_once_reconcile_on_unknown",
            },
        }
        execution_status = "returned"
        try:
            demo = await asyncio.to_thread(
                plugin.run_demonstration,
                spec,
                self._external_validation_spec,
                run_artifacts_dir(self.run_id) / "external_replication",
            )
            result = {
                "evaluated": isinstance(demo, dict) and demo.get("holds") is not None,
                "selected_round": selected.get("round"),
                "selected_experiment_id": selected.get("exp_id"),
                "hypothesis": selected.get("hypothesis"),
                "holds": demo.get("holds") if isinstance(demo, dict) else None,
                "test_statistic": demo.get("test_statistic") if isinstance(demo, dict) else None,
                "control_statistic": demo.get("control_statistic") if isinstance(demo, dict) else None,
                "detail": demo.get("detail") if isinstance(demo, dict) else "demonstration failed",
                "dataset_fingerprint": identity["dataset_fingerprint"],
                "row_identity_hash": identity["row_identity_hash"],
                "n": identity["n_rows"],
                "alpha": alpha,
                "data_asset_id": self._external_validation_spec.get("asset_id"),
                "content_sha256": self._external_validation_spec.get("content_sha256"),
                "sandbox_image_id": demo.get("sandbox_image_id") if isinstance(demo, dict) else None,
                "independence_class": (
                    (self._external_validation_spec.get("profile") or {})
                    .get("external_provenance", {})
                    .get("independence_class")
                ),
                "locked_code_sha256": code_sha256,
                "locked_preregistration_sha256": prereg_sha256,
            }
        except Exception as exc:
            execution_status = "raised"
            result = {
                "evaluated": False,
                "selected_round": selected.get("round"),
                "selected_experiment_id": selected.get("exp_id"),
                "hypothesis": selected.get("hypothesis"),
                "holds": None,
                "detail": f"external replication execution failed: {exc}",
                "dataset_fingerprint": identity["dataset_fingerprint"],
                "row_identity_hash": identity["row_identity_hash"],
                "n": identity["n_rows"],
                "alpha": alpha,
                "data_asset_id": self._external_validation_spec.get("asset_id"),
                "content_sha256": self._external_validation_spec.get("content_sha256"),
                "locked_code_sha256": code_sha256,
                "locked_preregistration_sha256": prereg_sha256,
            }
        await asyncio.to_thread(
            record_external_validation_result,
            self.run_id,
            result,
            action_id=action_id,
            execution_token=execution_token,
            provider_receipt={
                "executor": "plugin.run_demonstration",
                "execution_status": execution_status,
                "sandbox_image_id": result.get("sandbox_image_id"),
            },
            completed_by=f"experiment-driver:{self.run_id}",
        )
        await asyncio.to_thread(
            finish_hypothesis_attempt,
            attempt_id,
            status="evaluated" if result["evaluated"] else "not_evaluated",
            outcome=result,
        )
        artifact = run_artifacts_dir(self.run_id) / "external_replication.json"
        artifact.write_text(json.dumps(result, indent=2, default=str))
        if selected.get("exp_id"):
            await asyncio.to_thread(
                record_artifacts,
                selected["exp_id"],
                [{"kind": "external_replication", "uri": str(artifact)}],
            )
        await get_bus().publish(
            make_event("external_replication", run_id=self.run_id, payload=result)
        )
        return result

    # --- entry point ---
    async def run(self) -> None:
        try:
            await self._run()
        except BudgetPaused:
            return  # already paused + notified; a clean stop, not a failure
        except Exception as exc:  # noqa: BLE001
            import traceback

            tb = traceback.format_exc()
            await get_bus().publish(
                make_event("error", run_id=self.run_id,
                           payload={"error": str(exc), "traceback": tb[-1500:]})
            )
            await asyncio.to_thread(set_run_status, self.run_id, "failed")
            await self._status("failed", str(exc))

    async def _run(self) -> None:
        run = await asyncio.to_thread(get_run, self.run_id)
        if run is None:
            raise RuntimeError("run not found")
        plan = run.get("plan") or {}
        exp_id = run.get("plan_experiment_id")
        domain = run.get("domain") or "materials"
        self.domain = domain
        try:
            plugin = get_domain_plugin(domain, strict=not self.dry_run)
        except ValueError as exc:
            await self._block_run(exp_id, str(exc))
            return
        # surface a domain we don't have a plugin for (it silently ran the default) so
        # the operator sees the run is NOT the science they asked for — not stderr-only.
        if plugin.name != (domain or "").strip().lower():
            await get_bus().publish(
                make_event("domain_fallback", run_id=self.run_id,
                           payload={"requested": domain, "ran": plugin.name})
            )
        self.plugin = plugin
        data_spec = resolve_data_spec(self.run_id)
        # target-aware vocabulary: a Tc run reports K / Tc-range, not band-gap eV / 'gap'.
        self.profile = plugin.profile(target_column=data_spec.get("target_column"))
        self.budget = await asyncio.to_thread(BudgetTracker, self.run_id)

        # Production safety gate: every path that can execute model-authored Python must have the
        # same hard, no-network container boundary.  This runs before survey/model spend.  A local
        # subprocess remains useful for explicit unit/developer work, but an unattended real run
        # may never silently downgrade to it.
        if not self.dry_run and (
            get_settings().coder_enabled
            or get_settings().ai_demonstration_enabled
            or get_settings().discovery_enabled
        ):
            unsafe_reasons: list[str] = []
            if get_settings().authored_code_backend != "docker":
                unsafe_reasons.append("authored_code_backend is not docker")
            if not self.profile.host_side_run and get_settings().compute_backend != "docker":
                unsafe_reasons.append("compute_backend is not docker for authored model training")
            if get_settings().sandbox_allow_network:
                unsafe_reasons.append("sandbox network escape hatch is enabled")
            if unsafe_reasons:
                await self._block_run(
                    exp_id,
                    "unattended real run requires the hard sandbox: " + "; ".join(unsafe_reasons),
                )
                return
            preflight = await asyncio.to_thread(hard_sandbox_preflight)
            await get_bus().publish(
                make_event("sandbox_preflight", run_id=self.run_id, payload=preflight)
            )
            if not preflight.get("ok"):
                await self._block_run(
                    exp_id,
                    f"hard sandbox unavailable; refusing authored-code execution: "
                    f"{preflight.get('error')}",
                )
                return

        max_exps = max(1, get_settings().max_experiments_per_campaign)
        max_pivots = max(0, int(get_settings().campaign_max_pivots))
        max_hypothesis_attempts = max_exps + max_pivots
        if (
            not self.dry_run
            and get_settings().campaign_seal_v2_enabled
            and not self.profile.host_side_run
        ):
            if not await self._seal_campaign_data(
                plan, plugin, data_spec, max_hypothesis_attempts
            ):
                return
            if not await self._seal_external_data(plugin):
                return
            if plan.get("hypothesis_locked") and str(
                plan.get("contribution_type") or ""
            ).strip().lower() == "diagnostic":
                try:
                    await asyncio.to_thread(self._restore_locked_gate_receipts)
                    await asyncio.to_thread(self._restore_locked_diagnostic_artifact)
                except Exception as exc:  # noqa: BLE001 - resume integrity is fail-closed
                    await self._block_run(
                        exp_id,
                        f"could not restore locked campaign approvals/artifact safely: {exc}",
                    )
                    return
        elif not self.dry_run and get_settings().campaign_external_validation_required:
            await self._block_run(
                exp_id, "external validation requires Epistemic Seal v2 on a non-host-side domain"
            )
            return

        # Freeze exact code/model/data/split/policy identity after deterministic protocol sealing
        # and before SURVEY, model calls, or scientific observations. Dry runs permit a dirty
        # development tree; real release runs require a clean tree unless explicitly overridden.
        try:
            manifest = await asyncio.to_thread(
                freeze_run_manifest,
                self.run_id,
                allow_dirty=True if self.dry_run else None,
            )
        except ManifestCompatibilityError as exc:
            await self._block_run(exp_id, f"run manifest gate failed: {exc}")
            return
        await get_bus().publish(
            make_event(
                "run_manifest_frozen",
                run_id=self.run_id,
                payload={
                    "manifest_sha256": manifest.manifest_sha256,
                    "schema_version": manifest.payload.schema_version,
                    "dirty": manifest.payload.git.dirty,
                    "split_ledgers": len(manifest.payload.split_ledgers),
                },
            )
        )

        await asyncio.to_thread(set_run_status, self.run_id, "active")
        await get_bus().publish(
            make_event("run_started", run_id=self.run_id, payload={"mode": "dry_run" if self.dry_run else "real"})
        )

        # index the campaign's hypothesis so future runs can recall it
        hypo = " ".join(
            str(plan.get(k, "")).strip()
            for k in ("objective", "direction", "hypothesis")
        ).strip()
        if hypo:
            await self._index("hypothesis", hypo, exp_id, domain=domain)

        # 0) SURVEY once — the campaign-level literature map
        self.survey_brief, self.survey_gaps = await self._survey(plan, exp_id)
        if not self.dry_run and (not self.survey_brief or not self.survey_papers):
            await self._block_run(
                exp_id,
                "literature survey did not produce citable grounding; pausing before ideation",
            )
            return

        # CAMPAIGN LOOP: one Run -> up to N linked experiments. Each round poses a
        # hypothesis, runs the full experiment, then a go/no-go step decides whether
        # the next experiment is worth running (and what it should test).
        outcomes: list[dict[str, Any]] = []
        cur_exp_id = exp_id
        next_hint: dict[str, Any] | None = None
        round_idx = 1
        min_exps = max(
            1,
            min(int(get_settings().min_experiments_per_campaign), int(max_exps)),
        )
        pivots_used = 0  # K2 S3.5: undemonstrated rounds that triggered a bounded pivot
        while True:
            self._current_round_idx = round_idx
            self._current_attempt_sequence += 1
            self._current_attempt_id = None
            # per-round guard isolation so design/direction revision counters don't
            # bleed across experiments
            self.guard = LoopGuard(get_settings().critics.consensus.max_design_iterations)
            # fresh claim set per round — reset HERE (before IDEATE) so the novelty +
            # formulation claims ideate creates survive into _finalize_claims.
            self._claim_ids = {}
            await get_bus().publish(
                make_event(
                    "experiment",
                    run_id=self.run_id,
                    payload={
                        "round": round_idx,
                        "attempt_sequence": self._current_attempt_sequence,
                        "exp_id": cur_exp_id,
                        "of": max_exps,
                    },
                )
            )
            # IDEATE the first hypothesis from the survey; every later attempt — a campaign
            # CONTINUATION or a bounded PIVOT (K2 S3.5) — adopts the go/no-go step's proposal.
            if next_hint is None:
                await self._ideate(plan, cur_exp_id)
            elif plan.get("hypothesis_locked"):
                # An operator-locked confirmatory diagnostic remains the SAME scientific
                # question across fresh batches.  The campaign planner may decide that a
                # replication is informative, but it may not replace the preregistered
                # stratum/statistic after seeing an earlier outcome (or an infrastructure
                # failure). Rebuild the exact locked hypothesis and retain only procedural
                # provenance from the planning step.
                await self._ideate(plan, cur_exp_id)
                self.hypothesis["experiment_type"] = "reproduction"
                self.hypothesis["open_question"] = str(plan.get("objective") or "").strip()
                self.hypothesis["continuation_rationale"] = str(
                    next_hint.get("rationale") or next_hint.get("open_question") or ""
                ).strip()
                await get_bus().publish(make_event(
                    "hypothesis_locked_continuation", run_id=self.run_id, payload={
                        "round": round_idx,
                        "scope": "same_preregistered_question",
                        "fresh_confirmation_batch": self._current_attempt_sequence,
                    }))
            else:
                await self._adopt_hypothesis(next_hint or {}, cur_exp_id)
            if not await self._register_family_attempt(cur_exp_id, round_idx):
                return
            # SCORECARD gate (cheap + deterministic) BEFORE the cross-model direction
            # gate: block low-novelty / unclear-evaluation hypotheses before spending
            # peer-review + compute on them.
            await self._status("scoring", "scoring the hypothesis")
            if not await self._scorecard_gate(plan, cur_exp_id):
                return  # scorecard blocked past the loop limit -> paused + escalated
            if not await self._direction_gate(plan, cur_exp_id):
                return  # direction rejected past the loop limit -> paused + escalated

            outcome = await self._run_experiment(plan, data_spec, plugin, domain, cur_exp_id, round_idx)
            if outcome is None:
                return  # a gate rejected past the loop limit -> paused + escalated
            outcomes.append(outcome)
            await self._finish_family_attempt(outcome)

            # K2 S3.5: a PARADIGM round that produced no demonstration is an informative negative.
            # PIVOT to a different hypothesis (bounded by campaign_max_pivots) instead of aborting;
            # fail CLOSED (pause) only when the pivot budget is spent or the planner finds no viable
            # alternative. A pivot does NOT consume the max_exps budget (round_idx is unchanged).
            if outcome.get("undemonstrated"):
                if pivots_used >= max_pivots:
                    await self._block_run(
                        cur_exp_id,
                        f"paradigm run produced no discriminating demonstration after "
                        f"{pivots_used} pivot(s); pausing before analysis",
                    )
                    return
                decision = await self._campaign_step(plan, outcomes, round_idx, max_exps)
                if not decision.get("continue"):
                    await self._block_run(
                        cur_exp_id,
                        "paradigm run produced no discriminating demonstration and the campaign "
                        "found no viable pivot; pausing before analysis",
                    )
                    return
                pivots_used += 1
                next_hint = decision.get("next_hypothesis") or {}
                cur_exp_id = await asyncio.to_thread(
                    create_experiment, self.run_id, plan, parent_experiment_id=cur_exp_id
                )
                await get_bus().publish(
                    make_event("campaign", run_id=self.run_id, payload={
                        "decision": "pivot", "pivots_used": pivots_used, "max_pivots": max_pivots,
                        "rationale": decision.get("rationale", "")})
                )
                continue  # re-attempt at the same round_idx with a different hypothesis

            if round_idx >= max_exps:
                break
            decision = await self._campaign_step(plan, outcomes, round_idx, max_exps)
            if not decision.get("continue"):
                if round_idx >= min_exps:
                    await get_bus().publish(
                        make_event("campaign", run_id=self.run_id,
                                   payload={"decision": "stop", "rationale": decision.get("rationale", "")})
                    )
                    break
                # A live validation may explicitly require that the cross-round thesis is actually
                # exercised.  Continue with a conservative fresh-batch replication if the planner
                # would otherwise stop after round one; no verdict or threshold is relaxed.
                decision = {
                    **decision,
                    "continue": True,
                    "next_hypothesis": decision.get("next_hypothesis") or {
                        "statement": str(self.hypothesis.get("statement") or "").strip(),
                        "rationale": (
                            "Fresh-batch replication required by the predeclared minimum-round "
                            "validation protocol."
                        ),
                        "prediction": str(self.hypothesis.get("prediction") or "").strip(),
                        "novelty_note": (
                            "This is a confirmatory replication round, not a new novelty claim."
                        ),
                    },
                    "rationale": (
                        f"minimum campaign length is {min_exps}; run a locked fresh-batch "
                        "replication before stopping"
                    ),
                }
                await get_bus().publish(
                    make_event(
                        "campaign",
                        run_id=self.run_id,
                        payload={
                            "decision": "minimum_round_replication",
                            "round": round_idx,
                            "minimum": min_exps,
                            "rationale": decision["rationale"],
                        },
                    )
                )
                await get_bus().publish(
                    make_event(
                        "campaign_plan",
                        run_id=self.run_id,
                        payload={
                            "round": round_idx,
                            "continue": True,
                            "next_hypothesis": decision["next_hypothesis"],
                            "rationale": decision["rationale"],
                            "protocol_override": "minimum_round_replication",
                        },
                    )
                )
            next_hint = decision.get("next_hypothesis") or {}
            cur_exp_id = await asyncio.to_thread(
                create_experiment, self.run_id, plan, parent_experiment_id=cur_exp_id
            )
            round_idx += 1

        # synthesis / archive consider only DEMONSTRATED rounds — an undemonstrated pivot carries no
        # metrics and finalized no claims, so it must not drive the run-level verdict or synthesis.
        demonstrated = [o for o in outcomes if not o.get("undemonstrated")]

        final_validation: dict[str, Any] | None = None
        external_validation: dict[str, Any] | None = None
        if self._campaign_split_plan:
            try:
                final_validation = await self._validate_final_holdout(
                    demonstrated, data_spec, plugin
                )
            except Exception as exc:
                await self._block_run(cur_exp_id, f"final holdout could not be opened safely: {exc}")
                return
            if not final_validation.get("evaluated"):
                await self._block_run(
                    cur_exp_id,
                    "final holdout did not produce a harness verdict; headline remains unverified",
                )
                return

        if self._external_validation_spec is not None:
            try:
                external_validation = await self._validate_external_replication(
                    demonstrated, plugin
                )
            except Exception as exc:
                await self._block_run(
                    cur_exp_id, f"external replication could not be opened safely: {exc}"
                )
                return
            if not external_validation or not external_validation.get("evaluated"):
                await self._block_run(
                    cur_exp_id,
                    "external replication did not produce a harness verdict; result is not portable",
                )
                return

        # campaign-level synthesis across the experiments (only when >1 demonstrated)
        if len(demonstrated) > 1:
            await self._campaign_synthesis(
                plan,
                demonstrated,
                exp_id,
                final_validation=final_validation,
                external_validation=external_validation,
            )

        # ARCHIVE -------------------------------------------------------------
        # A run whose concluding experiment's results gate REJECTED is distinguished
        # from a clean completion: it ran to the end and is archived, but peer review
        # did not endorse the result. The claims already carry the refutation; this
        # makes the run-level outcome legible too.
        last_exp_id = demonstrated[-1]["exp_id"] if demonstrated else cur_exp_id
        results_rejected = (
            bool(demonstrated)
            and (
                demonstrated[-1].get("verdict") == "reject"
                or (final_validation is not None and final_validation.get("holds") is not True)
                or (external_validation is not None and external_validation.get("holds") is not True)
            )
        )
        final_status = "results_rejected" if results_rejected else "completed"
        await record_transition(self.run_id, last_exp_id, "write_up", "archive", "campaign complete")
        await asyncio.to_thread(set_run_status, self.run_id, final_status)
        await get_bus().publish(
            make_event(
                "run_finished",
                run_id=self.run_id,
                payload={
                    "status": final_status,
                    "results_gate": "rejected" if results_rejected else "passed",
                    "experiments": len(demonstrated),
                    "pivots": pivots_used,
                    "metrics": demonstrated[-1].get("metrics") if demonstrated else None,
                    "final_holdout": final_validation,
                    "external_replication": external_validation,
                },
            )
        )
        detail = (
            f"campaign complete ({len(demonstrated)} experiment(s)) — results rejected by peer review"
            if results_rejected else f"campaign complete ({len(demonstrated)} experiment(s))"
        )
        await self._status("archived", detail)

    async def _run_experiment(
        self, plan, data_spec, plugin, domain, exp_id, round_idx
    ) -> dict[str, Any] | None:
        """Run ONE experiment end-to-end (DESIGN → WRITE_UP) for the given experiment
        id. Returns a compact outcome for the campaign, or None if a hard gate
        rejected past its loop limit (the run is already paused + escalated)."""
        # NB: _claim_ids is reset at the campaign-loop top (before IDEATE), NOT here —
        # ideate's novelty + formulation claim ids must survive into _finalize_claims.
        # 1) EXPERIMENT_DESIGN
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await self._status("designing")
        design = await self._design(plan, data_spec, plugin, exp_id)

        # 2) critique_design gate (with bounded revision loop)
        design = await self._design_gate(design, plan, plugin, exp_id)
        if design is None:
            return None  # rejected past the loop limit -> paused + escalated

        # design approved -> set up (or reuse) the project repo + this experiment's branch
        await self._iam_setup(plan, domain, exp_id)

        # 2b) CODE: the coder authors the method. Host-side-LLM domains (RAG) get a
        # generation STRATEGY (answer prompt + k); regression domains get build_pipeline().
        if self.profile and self.profile.host_side_run:
            await self._code_rag(design, exp_id)
        elif self._contribution_type() == "diagnostic":
            # The authored discriminating demonstration below contains and executes
            # the diagnostic model/statistic. A second authored build_pipeline plus
            # the generic 5x5/grouped/baseline benchmark is not evidence for this
            # question and previously consumed the full sandbox wall clock before the
            # primary statistic could run.
            await get_bus().publish(make_event(
                "code", run_id=self.run_id, payload={
                    "accepted": None,
                    "skipped": True,
                    "reason": "diagnostic is evaluated by the authored preregistered demonstration",
                }))
        else:
            solution_code = await self._code(design, data_spec, exp_id)
            if solution_code:
                design["solution_code"] = solution_code

        # 2c) DEMONSTRATION CODE (paradigm only, the frontier path): the AI authors the
        # DISCRIMINATING COMPUTATION itself + pre-registers its decision rule, committed
        # immutably BEFORE execution so it can't be tuned to the result.
        if self._uses_discriminating_demonstration():
            await self._demonstration_code(design, data_spec, exp_id)

        # 3) EXECUTION
        await record_transition(self.run_id, exp_id, "experiment_design", "execution", "design approved; running")
        await self._status("executing")
        result = await self._run_eval(design, data_spec, domain, exp_id)

        # the eval itself may fail closed (e.g. an ablation with no real embedder); the
        # run is already paused, so abort this experiment.
        if result.get("blocked"):
            return None

        # 3b) fail-closed: a real run must produce verifiable artifacts; a degraded
        # (KFold-fallback) headline is surfaced, not hidden.
        if not await self._post_execution_guards(result, exp_id):
            return None

        # 3b') K2 S3.5: a PARADIGM round that produced NO discriminating demonstration (e.g. the
        # AI could not author a threshold consistent with its own exploration — seal #5 doom-to-
        # zero) is an informative negative, not a dead end. Surface it as an "undemonstrated"
        # outcome so the campaign loop can PIVOT to a different hypothesis (bounded) rather than
        # abort. The round is honestly NOT evaluated (no claims finalized, no metrics) — the spine
        # is unchanged; only the response to it (pivot vs hard-pause) moved up to the loop.
        if (not self.dry_run and self._uses_discriminating_demonstration()
                and not (result.get("info") or {}).get("demonstration")):
            return await self._undemonstrated_outcome(round_idx, exp_id)

        # plan↔execution drift: did the executed model match the family the hypothesis
        # names? If not, the hypothesis mechanism was never instantiated (so its claim
        # is "not evaluated", not "refuted"). Computed once, consumed by claims + write-up.
        _info = result.setdefault("info", {})
        hypo_text = " ".join(
            str(self.hypothesis.get(k, "")) for k in ("statement", "prediction", "rationale")
        )
        drift, drift_msg = detect_method_drift(
            hypo_text, str(design.get("model", "")), str(_info.get("model_impl", ""))
        )
        if _info.get("dense_fallback"):
            # a requested dense retriever that fell back to lexical is also a
            # not-instantiated mechanism (the family detector doesn't model retrievers).
            drift = True
            drift_msg = drift_msg or (
                "requested dense retrieval but no real embedder available; evaluated with "
                "the lexical baseline — mechanism not instantiated"
            )
        _info["method_drift"] = drift
        _info["method_drift_msg"] = drift_msg
        if drift:
            await get_bus().publish(
                make_event("method_drift", run_id=self.run_id,
                           payload={"detail": drift_msg, "exp_id": exp_id})
            )

        # 3c) REPRODUCTION: an independent re-run (locked code, new seed) — a metric
        # claim can only reach `strong` if this confirms the headline within tolerance.
        result.setdefault("info", {})["reproduction"] = await self._reproduce(
            design, data_spec, domain, result, exp_id
        )

        # 3d) FAITHFULNESS (domains that declare quality_via_critics): an independent
        # CROSS-VENDOR groundedness score — a separate, clearly-labeled metric, never
        # the headline (the honest deterministic metric stays the headline).
        if self.profile and self.profile.quality_via_critics:
            cases = (result.get("info") or {}).get("faithfulness_cases") or []
            score = await self.gateway.score_faithfulness(cases, run_id=self.run_id, dry_run=self.dry_run)
            if score is not None:
                result.setdefault("metrics", {})["faithfulness"] = score
            await get_bus().publish(
                make_event("faithfulness", run_id=self.run_id,
                           payload={"score": score, "n_cases": len(cases), "scorer": "cross-vendor panel"})
            )

        # 4) ANALYSIS
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "execution", "analysis", "training complete; analyzing")
        await self._status("analyzing")
        analysis = await self._analyze(design, result, exp_id)

        # 4b) INDEPENDENT AUDIT of an AI-authored demonstration (author vendor excluded) —
        # runs before the results gate so a refuting audit (forced holds=False) is reflected
        # in the demonstration result the gate + claim finalization read.
        info = result.get("info") or {}
        audit_passed, audit_error = await self._audit_demonstration(design, info.get("demonstration"))

        # 5) review_results gate — the critics see the CLAIMS + evidence (proposed in
        # analysis) + the protocol status + the reproduction, so they review evidence.
        claims = await asyncio.to_thread(list_claims, self.run_id, exp_id)
        contribution_type = self._contribution_type()
        rpanel = await self.gateway.review(
            "results",
            self._results_review_payload(
                design, result, analysis, claims,
                contribution_type=contribution_type, demonstration=self._paradigm_demonstration(),
            ),
            exp_id or self.run_id,
            run_id=self.run_id,
            dry_run=self.dry_run,
            mode=contribution_type,  # paradigm work is judged on the right axes, not SOTA-delta
        )
        if (
            self._locked_diagnostic_artifact is not None
            and design.get("demonstration_code")
            == self._locked_diagnostic_artifact.get("code")
        ):
            # Candidate eligibility is earned only by a completed independent audit
            # of the executed source. It is deliberately independent of whether the
            # observed scientific result held or whether a results reviewer liked the
            # direction of that result: selecting only positive outcomes would be a
            # winner's curse. A missing audit never upgrades; a substantive code-audit
            # rejection on any fresh recomputation revokes eligibility.
            produced_verdict = isinstance(
                (info.get("demonstration") or {}).get("holds"), bool
            )
            if produced_verdict and audit_passed is True and not audit_error:
                self._locked_diagnostic_artifact["eligible"] = True
            elif audit_passed is False and not audit_error:
                self._locked_diagnostic_artifact["eligible"] = False
        await record_transition(
            self.run_id, exp_id, "analysis", "optimize",
            f"results reviewed: {rpanel.consensus_verdict} (gate_passed={rpanel.gate_passed})",
            actor="critic",
        )
        # K2-S4: fold THIS round's harness verdict into the lineage credence BEFORE finalizing claims
        # (the only thing that moves the belief). A confirm-split hold/refute updates it; a
        # not_evaluated / exploration-only / audit-degraded round folds in nothing (fail-closed). The
        # belief is a planning aid — it never set this verdict, it only learns from it.
        qkey = self._question_key(plan)
        post_cred, sig = self._fold_verdict_into_belief(qkey, info.get("demonstration") or {}, audit_error)
        if sig["updated"]:
            await asyncio.to_thread(
                upsert_credence, self.run_id, qkey, post_cred.alpha, post_cred.beta, post_cred.n_updates)
            await get_bus().publish(make_event(
                "belief_update", run_id=self.run_id, payload={
                    "round": round_idx, "exp_id": exp_id, "question_key": qkey,
                    "alpha": post_cred.alpha, "beta": post_cred.beta, "mean": belief.mean(post_cred),
                    "n_updates": post_cred.n_updates, "weak_prior": belief.is_weak_prior(post_cred),
                    "predicted_p_holds": sig["predicted"], "realized": sig["realized"],
                    "surprise": sig["surprise"], "realized_eig_bits": sig["realized_eig"],
                    "remaining_eig": sig["remaining_eig"]}))
        # finalize the proposed claims now that the gate has spoken (harness-owned strength: a metric
        # claim only reaches `strong` with grouped CV + a clean approve + a CONFIRMED reproduction; a
        # formulation claim additionally needs the explore->confirm seal AND a non-weak credence, K2-S5).
        await self._finalize_claims(
            info.get("protocol_status"), rpanel, info.get("reproduction") or {}, exp_id,
            ablation=info.get("ablation"),
            method_not_instantiated=bool(info.get("method_drift")),
            demonstration=info.get("demonstration"),
            audit_passed=audit_passed,
            audit_error=audit_error,
            credence=post_cred,
        )

        # 6) OPTIMIZE (<=1 iteration) — skipped when the results gate rejected, since
        # tuning a baseline cannot answer a critique-level rejection (and wastes a stage).
        await self._status("optimizing")
        best_design, best_result = await self._optimize(
            design, result, data_spec, domain, exp_id, plugin, gate_passed=bool(rpanel.gate_passed)
        )

        # 7) WRITE_UP
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "optimize", "write_up", "writing report")
        await self._status("writing")
        await self._write_up(plan, best_design, best_result, analysis, rpanel, exp_id)

        metrics = best_result.get("metrics", {})
        hk = (
            "diagnostic_test_statistic"
            if self._contribution_type() == "diagnostic"
            else (self.profile.headline_metric if self.profile else "mae")
        )
        # K2: classify WHY the demonstration held/failed (deterministic, harness-owned) so the
        # next campaign round is shaped by this round's reason, not a fresh blind guess. Read the
        # POST-audit demonstration in ``info`` (the same dict the audit mutated + claims read);
        # it never decides holds/strength.
        demo_outcome = classify_outcome(
            info.get("demonstration"),
            info.get("reproduction"),
            audit_passed=audit_passed,
            audit_error=audit_error,
            gate_passed=rpanel.gate_passed,
            verdict=rpanel.consensus_verdict,
            min_confirm_n=2 * int(getattr(get_settings(), "demonstration_min_samples", 20)),
        )
        await get_bus().publish(
            make_event("campaign_reason", run_id=self.run_id, payload={
                "round": round_idx, "exp_id": exp_id, "reason": demo_outcome["reason"],
                "recoverable": demo_outcome["recoverable"], "detail": demo_outcome["detail"]})
        )
        # the belief was already folded (above, before claim finalization); reuse post_cred/sig.
        final_candidate = None
        demo_result = info.get("demonstration") or {}
        locked_eligible = (
            self._locked_diagnostic_artifact is None
            or bool(self._locked_diagnostic_artifact.get("eligible"))
        )
        if (
            design.get("demonstration_code")
            and demo_result.get("preregistration")
            and locked_eligible
        ):
            final_candidate = {
                "code": design["demonstration_code"],
                "preregistration": demo_result["preregistration"],
                "demonstration": self._paradigm_demonstration() or {},
                "random_state": int(design.get("random_state", 42)),
            }
        return {
            "round": round_idx,
            "exp_id": exp_id,
            "model": best_design.get("model"),
            "metrics": metrics,
            "headline_metric": hk,
            "headline": metrics.get(hk),
            "units": self.profile.units if self.profile else "",
            "analysis": analysis,
            "verdict": rpanel.consensus_verdict,
            "hypothesis": str(self.hypothesis.get("statement", "")).strip(),
            # what the planner intended this round to be (round 1 = the baseline)
            "experiment_type": str(self.hypothesis.get("experiment_type") or ("baseline" if round_idx == 1 else "")),
            "open_question": str(self.hypothesis.get("open_question") or ""),
            # K2 outcome-reason classification (feeds _campaign_step's reasoned trajectory)
            "reason": demo_outcome["reason"],
            "narrowing_hint": demo_outcome["narrowing_hint"],
            "recoverable": demo_outcome["recoverable"],
            "outcome_detail": demo_outcome["detail"],
            # K2-S4: the belief signal this round produced (read by _campaign_step + the synthesis)
            "question_key": qkey,
            "belief_mean": belief.mean(post_cred),
            "belief_weak_prior": belief.is_weak_prior(post_cred),
            "belief_updated": sig["updated"],
            "belief_surprise": sig["surprise"],
            "belief_remaining_eig": sig["remaining_eig"],
            "belief_predicted_p_holds": sig["predicted"],
            "demonstration_holds": demo_result.get("holds"),
            # Internal-only payload consumed after adaptive selection, when the one-time final
            # holdout opens. It is never sent to the campaign planner or synthesis prompt.
            "_final_candidate": final_candidate,
        }

    async def _undemonstrated_outcome(self, round_idx: int, exp_id: str | None) -> dict[str, Any]:
        """K2 S3.5: build the campaign outcome for a paradigm round that produced no committable
        demonstration. Carries reason=authoring_failed + a PIVOT hint so the campaign loop steers
        the next attempt to a different effect/statistic/question. ``undemonstrated`` marks it so
        the loop pivots (bounded) rather than treating it as a normal completed round, and so it is
        excluded from the campaign synthesis / archive (it has no metrics, no finalized claims)."""
        hint = (
            "the AI could not author a demonstration whose pre-registered threshold was consistent "
            "with its OWN exploration (e.g. the explore-estimated effect was ~0, so any positive "
            "threshold is doom-to-zero, or the authored code failed the gate twice). Do NOT re-tune "
            "the same threshold — PIVOT: choose a different effect/statistic the exploration "
            "actually reveals, or a different open question."
        )
        await get_bus().publish(make_event("campaign_reason", run_id=self.run_id, payload={
            "round": round_idx, "exp_id": exp_id, "reason": "authoring_failed",
            "recoverable": True, "detail": "paradigm round produced no discriminating demonstration"}))
        return {
            "round": round_idx, "exp_id": exp_id, "undemonstrated": True,
            "model": None, "metrics": {}, "headline_metric": (self.profile.headline_metric if self.profile else "mae"),
            "headline": None, "units": self.profile.units if self.profile else "",
            "analysis": "", "verdict": "blocked",
            "hypothesis": str(self.hypothesis.get("statement", "")).strip(),
            "experiment_type": str(self.hypothesis.get("experiment_type") or ("baseline" if round_idx == 1 else "")),
            "open_question": str(self.hypothesis.get("open_question") or ""),
            "reason": "authoring_failed", "narrowing_hint": hint,
            "recoverable": True, "outcome_detail": "no discriminating demonstration produced",
        }

    async def _adopt_hypothesis(self, hypo: dict[str, Any], exp_id: str | None) -> None:
        """Adopt the next hypothesis proposed by the campaign go/no-go step for a new
        experiment round (persist + index), then let the direction gate vet it."""
        if hypo:
            self.hypothesis = hypo
        statement = str(self.hypothesis.get("statement", "")).strip()
        if statement and exp_id:
            await asyncio.to_thread(set_experiment_hypothesis, exp_id, statement)
        await self._index("hypothesis", statement, exp_id)
        await record_transition(
            self.run_id, exp_id, None, "ideate", f"campaign round hypothesis: {statement[:80]}"
        )

    # the kinds of experiment the planner may propose for the next round
    EXPERIMENT_TYPES = (
        "baseline", "ablation", "method_comparison", "data_scaling",
        "robustness", "failure_analysis", "reproduction", "sota_attempt",
    )

    async def _campaign_step(
        self, plan: dict, outcomes: list[dict], round_idx: int, max_exps: int
    ) -> dict[str, Any]:
        """Experiment-search planner: propose several TYPED candidate next experiments,
        each tied to a named open question + an expected-information-gain (EIG) estimate,
        then deterministically pick the highest-EIG candidate that clears the floor — or
        stop when the program has converged. Returns
        {"continue": bool, "next_hypothesis": {...}, "rationale": str, "candidates": [...]}.
        """
        await self._status("planning", "planning the next experiment")

        # K2: a REASONED trajectory — each round carries WHY its demonstration held/failed (the
        # deterministic outcome-reason classification) + a concrete narrowing hint, not just a
        # metric + an approve/reject token. This is the seam that makes the next round provably
        # shaped by the last round's reason instead of a fresh blind guess.
        def _line(o: dict[str, Any]) -> str:
            base = (
                f"- round {o['round']} [{o.get('experiment_type') or '?'}]: '{o['hypothesis']}' -> "
                f"{o.get('headline_metric')} {o.get('headline')} [{o.get('model')}], "
                f"verdict {o.get('verdict')}"
            )
            reason = o.get("reason")
            if reason and reason != "no_demonstration":
                hint = str(o.get("narrowing_hint") or "").strip()
                base += f"\n    WHY [{reason}]: {hint}"
            return base

        trajectory = "\n".join(_line(o) for o in outcomes)

        # A hard directive built from the MOST RECENT round's reason: the next candidates must act
        # on it. When the effect itself did not appear on held-out data (recoverable is False),
        # re-tuning the same effect is forbidden — pivot to a different open question.
        last = outcomes[-1] if outcomes else {}
        last_reason = str(last.get("reason") or "")
        learning = ""
        if last_reason and last_reason != "no_demonstration":
            hint = str(last.get("narrowing_hint") or "").strip()
            pivot = (
                " The effect did NOT hold on held-out data, so re-tuning the SAME effect/threshold "
                "is unacceptable — at least one candidate must pivot to a DIFFERENT open question."
                if last.get("recoverable") is False else
                " At least one candidate must directly act on this hint rather than re-guess blind."
            )
            learning = (
                f"WHAT THE LAST ROUND LEARNED (reason: {last_reason}):\n  {hint}\n"
                f"  REQUIREMENT:{pivot}\n\n"
            )

        gaps = ("OPEN GAPS:\n- " + "\n- ".join(self.survey_gaps) + "\n\n") if self.survey_gaps else ""
        budget_line = ""
        if self.budget is not None:
            budget_line = (
                f"BUDGET: ${self.budget.spent_usd:.2f} of ${self.budget.cap_usd:.2f} used. "
            )
        types = ", ".join(self.EXPERIMENT_TYPES)
        prompt = (
            f"You are PLANNING the next experiment in a research campaign (objective: "
            f"{plan.get('objective', '')}). {round_idx} of at most {max_exps} have run:\n{trajectory}\n\n"
            f"{learning}{gaps}{budget_line}\n"
            "Propose 2-3 candidate NEXT experiments. Each must answer a NAMED open question, be a "
            f"distinct angle from the rounds above, and have a type from: {types}. Estimate each "
            "candidate's expected_information_gain (0..1) honestly — an experiment that would barely "
            "change our beliefs scores low. Return ONLY JSON: "
            '{"candidates": [{"experiment_type": "...", "open_question": "...", '
            '"expected_information_gain": 0.0, "rationale": "...", "hypothesis": '
            '{"statement": "...", "rationale": "...", "prediction": "...", "novelty_note": "..."}}, ...]}.'
        )
        text = await reason_stage(
            self.run_id, "campaign", prompt, dry_run=self.dry_run,
            dry_text=json.dumps({"candidates": [{
                "experiment_type": "ablation",
                "open_question": "which features drive the headline result?",
                "expected_information_gain": 0.7,
                "rationale": "an ablation would explain the result",
                "hypothesis": self.profile.dry_next_hypothesis if self.profile else {},
            }]}),
        )
        parsed = _parse_json(text, _JSON, {"candidates": []})
        candidates = [c for c in (parsed.get("candidates") or []) if isinstance(c, dict)]
        floor = get_settings().campaign_min_eig
        # K2-S4: each candidate's information gain is the harness-MEASURED expected entropy reduction
        # of its lineage credence (normalized 0..1); the LLM's self-reported number can only LOSE to
        # it (effective = min(llm, measured)). A fresh/weak lineage measures ~1.0 (no cap → today's
        # behavior, fail-closed); an ACCUMULATED belief measures low, so re-confirming a near-certain
        # line scores low and the program converges. The harness recomputes EIG — the LLM can't
        # inflate a candidate above what its belief state says there is to learn.
        for c in candidates:
            llm = float(c.get("expected_information_gain", 0.0) or 0.0)
            cred = self._belief.get(self._slug(c.get("open_question"))) or belief.prior_from_scorecard({})
            measured = belief.normalized_information_gain(cred, belief.mean(cred))
            c["measured_eig"] = measured
            c["effective_eig"] = min(llm, measured)
        viable = [c for c in candidates if float(c.get("effective_eig", 0.0)) >= floor]
        # backward-looking convergence on the just-run hypothesis. When its belief actually updated
        # this round, use the MEASURED remaining information of that lineage (a near-saturated belief
        # => converge); otherwise fall back to the LLM scorecard EIG (fail-closed on no belief signal,
        # so non-paradigm / dry-run campaigns behave exactly as before).
        if last.get("belief_updated"):
            last_gain = float(last.get("belief_remaining_eig", 1.0))
            backward_desc = f"belief on the last line is near-saturated (remaining EIG {last_gain:.2f} < {floor})"
        else:
            last_gain = float(self._last_scores.get("expected_information_gain", 1.0))
            backward_desc = f"last experiment's expected information gain {last_gain:.2f} below floor {floor}"
        if not viable or last_gain < floor:
            reason = (
                f"no proposed experiment clears the measured EIG floor {floor}"
                if not viable else backward_desc
            )
            decision = {"continue": False, "rationale": f"{reason}; program converged", "candidates": candidates}
        else:
            best = max(viable, key=lambda c: float(c.get("effective_eig", 0.0)))
            nxt = dict(best.get("hypothesis") or {})
            nxt["experiment_type"] = best.get("experiment_type")
            nxt["open_question"] = best.get("open_question")
            decision = {
                "continue": True,
                "rationale": (
                    f"chose a {best.get('experiment_type')} (measured EIG "
                    f"{float(best.get('effective_eig', 0.0)):.2f}) to answer: "
                    f"{best.get('open_question')}"
                ),
                "next_hypothesis": nxt,
                "candidates": candidates,
                "chosen": best,
            }
        await get_bus().publish(
            make_event("campaign_plan", run_id=self.run_id, payload={
                "round": round_idx, "continue": decision["continue"],
                "rationale": decision.get("rationale", ""),
                "candidates": [
                    {"experiment_type": c.get("experiment_type"), "open_question": c.get("open_question"),
                     "eig": c.get("expected_information_gain"),
                     "measured_eig": c.get("measured_eig"), "effective_eig": c.get("effective_eig")}
                    for c in candidates
                ],
            })
        )
        await self._index(
            "design_rationale",
            f"campaign plan after round {round_idx}: continue={decision['continue']} — "
            f"{decision.get('rationale', '')}",
            outcomes[-1]["exp_id"],
        )
        return decision

    async def _campaign_synthesis(
        self,
        plan: dict,
        outcomes: list[dict],
        first_exp_id: str | None,
        *,
        final_validation: dict[str, Any] | None = None,
        external_validation: dict[str, Any] | None = None,
    ) -> None:
        """Write a campaign-level summary tying the experiments together: the
        trajectory, which experiment won, what the program learned, open gaps."""
        await self._status("synthesizing", "summarizing the campaign")
        hk = (
            "diagnostic_test_statistic"
            if self._contribution_type() == "diagnostic"
            else (self.profile.headline_metric if self.profile else "mae")
        )
        units = self.profile.units if self.profile else ""
        usfx = f" {units}" if units else ""
        rows = "\n".join(
            f"- round {o['round']} [{o.get('experiment_type') or '?'}"
            + (f", Q: {o.get('open_question')}" if o.get("open_question") else "")
            + f"] ({o.get('model')}): '{o['hypothesis']}' -> {hk} "
            f"{o.get('headline')}, verdict {o.get('verdict')}"
            for o in outcomes
        )
        scored = [o for o in outcomes if o.get("headline") is not None]
        locked_diagnostic = (
            self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
        )
        best = (
            outcomes[0]
            if locked_diagnostic
            else (
                (max if self._maximize() else min)(scored, key=lambda o: o["headline"])
                if scored else outcomes[-1]
            )
        )
        # K2-S5: the BELIEF trajectory + calibration — the north-star progress signal, surfaced
        # HONESTLY as early/weak. A credence is calibrated only by harness-verified confirm-split
        # verdicts, so few updates => a weak prior, never a hard probability. Calibration =
        # mean |predicted − realized| surprise over the rounds that actually moved the belief.
        belief_rows = [o for o in outcomes if o.get("belief_mean") is not None]
        n_updates = sum(1 for o in outcomes if o.get("belief_updated"))
        surprises = [o["belief_surprise"] for o in outcomes if o.get("belief_surprise") is not None]
        calibration = (sum(surprises) / len(surprises)) if surprises else None
        belief_block = ""
        if belief_rows:
            traj = "\n".join(
                f"- round {o['round']}: P(holds)={o['belief_mean']:.2f}"
                + (f", surprise {o['belief_surprise']:.2f}" if o.get("belief_surprise") is not None else "")
                + (f" [{o.get('reason')}]" if o.get("reason") and o.get("reason") != "no_demonstration" else "")
                for o in belief_rows
            )
            cal = (
                f"mean predicted−realized surprise {calibration:.2f} over {n_updates} harness-verified "
                f"update(s) — EARLY/WEAK, not a calibrated probability"
                if calibration is not None else
                "no harness-verified confirm-split update yet — credences are weak priors"
            )
            belief_block = (
                "\n\n## Belief trajectory (calibrated only by held-out verdicts; early/weak)\n\n"
                f"{traj}\n\n**Calibration:** {cal}\n"
            )
        validation_block = ""
        if final_validation is not None or external_validation is not None:
            final = final_validation or {}
            external = external_validation or {}
            validation_block = (
                "\n\n## Locked validation\n\n"
                "The selected implementation was fixed before either validation set was opened. "
                "These are one-time harness results, not inputs to candidate selection.\n\n"
                "### Internal final holdout\n\n"
                f"- evaluated: `{final.get('evaluated')}`; holds: `{final.get('holds')}`\n"
                f"- test statistic: `{final.get('test_statistic')}`; control statistic: "
                f"`{final.get('control_statistic')}`; alpha: `{final.get('alpha')}`; n: "
                f"`{final.get('n')}`\n"
                f"- split hash: `{final.get('split_hash')}`\n"
                f"- locked code SHA-256: `{final.get('locked_code_sha256')}`\n"
                f"- locked preregistration SHA-256: "
                f"`{final.get('locked_preregistration_sha256')}`\n\n"
                "### External replication\n\n"
                f"- evaluated: `{external.get('evaluated')}`; holds: `{external.get('holds')}`; "
                f"independence class: `{external.get('independence_class')}`\n"
                f"- test statistic: `{external.get('test_statistic')}`; control statistic: "
                f"`{external.get('control_statistic')}`; alpha: `{external.get('alpha')}`; n: "
                f"`{external.get('n')}`\n"
                f"- dataset fingerprint: `{external.get('dataset_fingerprint')}`\n"
                f"- row-identity hash: `{external.get('row_identity_hash')}`\n"
                f"- pinned content SHA-256: `{external.get('content_sha256')}`\n"
                f"- locked code SHA-256: `{external.get('locked_code_sha256')}`\n"
                f"- locked preregistration SHA-256: "
                f"`{external.get('locked_preregistration_sha256')}`\n"
            )
        prompt = (
            f"Summarize this research campaign (objective: {plan.get('objective', '')}) as a short markdown "
            f"brief. Experiments:\n{rows}\n{belief_block}\nThe best {hk} was {best.get('headline')} in round "
            f"{best.get('round')}. Write: the trajectory (how each experiment informed the next), which "
            "experiment won and why, what the program learned, and the most important still-open gap. "
            f"Ground every claim in the {hk} numbers above; do not invent results. If a belief trajectory "
            "is shown, report it + its calibration as given and label credences as early/weak — never as "
            "hard probabilities."
        )
        deterministic_summary = (
            "# Campaign Summary\n\n"
            f"**Objective:** {plan.get('objective', 'n/a')}\n\n"
            "## Trajectory\n\n"
            f"{rows}{belief_block}{validation_block}\n"
            "## Outcome\n\n"
            "The operator-locked diagnostic was repeated on fresh, disjoint confirmation "
            "batches. The first committable code and preregistration were selected a priori for "
            "the one-time final holdout; later batches were replication evidence, not a "
            "winner-selection pool.\n\n"
            "## Open gaps\n\n- " + "\n- ".join(self.survey_gaps or ["(none recorded)"])
        )
        summary = (
            deterministic_summary
            if locked_diagnostic
            else await reason_stage(
                self.run_id, "campaign", prompt, dry_run=self.dry_run,
                dry_text=(
                f"# Campaign Summary\n\n**Objective:** {plan.get('objective', 'n/a')}\n\n"
                f"## Trajectory\n\n{rows}\n{belief_block}\n"
                f"## Outcome\n\nThe best {hk} was {best.get('headline')}{usfx} in "
                f"round {best.get('round')} ({best.get('model')}). Across {len(outcomes)} experiments the "
                f"program refined its hypothesis from the survey gaps toward the most informative test.\n\n"
                f"## Open gaps\n\n- " + "\n- ".join(self.survey_gaps or ["(none recorded)"])
                ),
            )
        )
        path = run_artifacts_dir(self.run_id) / "campaign.md"
        path.write_text(summary)
        if first_exp_id:
            await asyncio.to_thread(
                record_artifacts, first_exp_id, [{"kind": "campaign", "uri": str(path)}]
            )
        await self._index(
            "conclusion",
            f"campaign ({len(outcomes)} experiments) selected {hk} {best.get('headline')} "
            f"from round {best.get('round')} ({best.get('model')}).",
            first_exp_id, headline=best.get("headline"), experiments=len(outcomes),
        )
        await get_bus().publish(
            make_event("campaign_finished", run_id=self.run_id, payload={
                "uri": str(path), "experiments": len(outcomes),
                "best_round": best.get("round"), "best_headline": best.get("headline"),
                # K2-S5: the belief trajectory + calibration (honest north-star progress signal)
                "belief_trajectory": [
                    {"round": o["round"], "mean": o["belief_mean"],
                     "surprise": o.get("belief_surprise"), "reason": o.get("reason")}
                    for o in belief_rows
                ],
                "calibration": calibration, "n_belief_updates": n_updates,
                "final_holdout": final_validation,
                "external_replication": external_validation,
            })
        )

    # --- stage implementations ---
    async def _design(self, plan, data_spec, plugin, exp_id) -> dict[str, Any]:
        fallback = dict(plugin.baselines()[0])
        fallback.setdefault("test_size", 0.2)
        fallback.setdefault("random_state", 42)
        if data_spec.get("target_column"):
            fallback["target_column"] = data_spec["target_column"]
        if (
            self._contribution_type() == "diagnostic"
            and self.hypothesis.get("hypothesis_locked")
        ):
            # Full executable contract for the locked endpoint. A generic design
            # model JSON cannot faithfully carry leakage, matching, multiplicity,
            # and interpretation constraints, so the harness materializes them.
            design = {
                **fallback,
                "model": "random_forest",
                "model_params": {
                    "n_estimators": 300,
                    "max_features": 1.0,
                    "min_samples_leaf": 1,
                    "n_jobs": 1,
                },
                "featurizer": "magpie",
                "test_size": 0.5,
                "random_state": int(fallback.get("random_state", 42)),
                "contribution_type": "diagnostic",
                "estimand": (
                    "one-sided family-alpha bootstrap lower confidence bound for the mean "
                    "paired absolute-error excess: multi-alkaline-earth cuprate minus a "
                    "non-cuprate matched on element count and training-derived Magpie density"
                ),
                "stratum": (
                    "target-blind: group contains Cu and O and at least two of Ba/Sr/Ca/Mg"
                ),
                "role_internal_split": (
                    "GroupShuffleSplit(n_splits=1, test_size=0.5, random_state=runtime seed) "
                    "by chemical-system group. RandomForestRegressor uses the same runtime seed. "
                    "Training-column median imputation, StandardScaler, the RF, and the density "
                    "neighbor index are fit only on the training side; evaluation rows never "
                    "enter any fitted preprocessing or model."
                ),
                "control_pool": (
                    "all evaluation rows that do not satisfy the target-blind case definition: "
                    "not (Cu and O and at least two of Ba/Sr/Ca/Mg); rows may contain Cu or O "
                    "individually. No target/Tc value is used to form either pool."
                ),
                "density_descriptor": (
                    "after training-only median imputation and StandardScaler, choose at most "
                    "2048 training rows without replacement with seed+17; density for every "
                    "evaluation row is -log(mean Euclidean distance to its 10 nearest reference "
                    "rows + 1e-12). Standardize density using the reference-density mean/std. "
                    "This scalar is only a declared matching descriptor, not a claim that "
                    "high-dimensional distance exhausts applicability-domain differences."
                ),
                "matching": (
                    "use every available case up to min(n_case,n_control). Standardize element "
                    "count by its training-row mean/std; combine it with the standardized density "
                    "as a two-dimensional vector. Process cases in ascending original evaluation "
                    "row index and greedily choose the unused control with minimum Euclidean "
                    "distance; exact ties go to the smallest original evaluation row index. "
                    "Matching is 1:1 without replacement and never uses the target/Tc."
                ),
                "sample_accounting": (
                    "minimum_pairs=20 is an execution/probe floor calibrated from an archived "
                    "prior campaign, not a power guarantee. Report n_case_available, "
                    "n_control_available, n_pairs, paired SD and SE. Fewer than 20 pairs is "
                    "not_evaluated/sample_starved, never a scientific refutation."
                ),
                "test_statistic": (
                    "with 10000 repetitions and seed+307, resample the fixed matched-pair rows "
                    "with replacement and compute mean absolute-error excess each time; the "
                    "statistic is quantile family_alpha of those means. Authored code reports "
                    "family_alpha_used and bootstrap_repetitions."
                ),
                "negative_control": (
                    "keep the fixed, unique pair membership unchanged; with seed+211 flip the "
                    "sign of exactly floor(n_pairs/2) pair differences. Apply the same 10000-row "
                    "index bootstrap and family-alpha quantile to the signed differences. The "
                    "control is silent when that lower bound is <= 0."
                ),
                "multiplicity": (
                    "harness-supplied per-attempt Bonferroni alpha; no hard-coded CI level"
                ),
                "decision_rules": {
                    "supported_if": {"op": ">", "threshold": 0.0},
                    "control_silent_if": {"op": "<=", "threshold": 0.0},
                },
                "interpretation": (
                    "descriptive conditional error contrast only; no causal mechanism, "
                    "irreducible-error, SOTA, or universal-generalization claim"
                ),
                "method_note": (
                    "operator-locked diagnostic; generic performance benchmarking and "
                    "post-confirmation tuning are outside the primary endpoint"
                ),
            }
            await record_transition(
                self.run_id,
                exp_id,
                "experiment_design",
                "experiment_design",
                "locked diagnostic design contract generated by the harness",
            )
            await self._index(
                "design_rationale",
                f"locked diagnostic contract: {json.dumps(design, default=str)[:1200]}",
                exp_id,
                model=design["model"],
            )
            return design
        # recall-before-design: pull similar prior work from OTHER runs so the
        # design avoids repeating approaches that already failed / converged.
        query = " ".join(
            str(plan.get(k, "")).strip() for k in ("objective", "direction", "hypothesis")
        ).strip()
        prior = await asyncio.to_thread(
            recall, query, run_id=None, exclude_run_id=self.run_id, dry_run=self.dry_run
        )
        briefing = format_briefing(prior)
        task = self.profile.task if self.profile else "property regression"
        feature_desc = self.profile.feature_desc if self.profile else "a numeric feature matrix"
        # Method choice is DISCOVERED from the survey's frontier methods, not a fixed
        # menu — the designer picks the most promising one feasible on these features,
        # and the coder implements it (the harness still scores it).
        methods_block = ""
        if self.survey_methods:
            methods_block = (
                "FRONTIER METHODS the survey found this field using (choose the most promising one that is "
                "feasible on the features above; the coder will implement it):\n"
                + "\n".join(f"- {m.get('name','')}: {m.get('why','')}" for m in self.survey_methods)
                + "\n\n"
            )
        prompt = (
            f"Turn this research plan into a concrete experiment design for a {task} on "
            f"{feature_desc}.\n\n"
            f"PLAN:\n{json.dumps(plan, indent=2)}\n\nDATA:\n{json.dumps(data_spec, indent=2)}\n\n"
            + (self.survey_brief + "\n\n" if self.survey_brief else "")
            + methods_block
            + (briefing + "\n\n" if briefing else "")
            + "Choose the most promising method grounded in the frontier methods above (do NOT default to a "
            "fixed model). Return ONLY JSON with keys: model (a short method name), model_params (object), "
            "method_note (one line: which surveyed method + why), test_size (0-1), random_state (int)."
        )
        dry_model = (
            self.survey_methods[0]["name"] if self.survey_methods else fallback["model"]
        )
        text = await reason_stage(
            self.run_id, "experiment_design", prompt,
            dry_run=self.dry_run,
            dry_text=json.dumps({
                "model": dry_model, "model_params": {}, "test_size": 0.2, "random_state": 42,
                "method_note": f"adopt the surveyed frontier method '{dry_model}' for {task}",
            }),
        )
        design = _parse_design(text, fallback)
        await record_transition(
            self.run_id, exp_id, "experiment_design", "experiment_design",
            f"concrete design: {design.get('model')} {design.get('model_params')}",
        )
        await self._index(
            "design_rationale",
            f"{design.get('model')} with {design.get('model_params')} on {query}",
            exp_id, model=design.get("model"),
        )
        return design

    async def _design_gate(self, design, plan, plugin, exp_id) -> dict[str, Any] | None:
        locked = bool(self.hypothesis.get("hypothesis_locked"))
        locked_artifact = {
            "design": design,
            "hypothesis": self._locked_scientific_hypothesis(self.hypothesis),
            "demonstration": self._paradigm_demonstration(),
            "plan_objective": plan.get("objective"),
        }
        locked_fingerprint = self._locked_gate_fingerprint("design", locked_artifact)
        if locked:
            receipt = self._locked_gate_receipts.get("design") or {}
            if receipt.get("artifact_sha256") == locked_fingerprint:
                if await self._reuse_locked_gate_if_approved(
                    "design", locked_fingerprint, exp_id or self.run_id
                ):
                    return design
                await self._block_run(
                    exp_id,
                    "could not persist reuse of the locked design approval; refusing "
                    "an unauditable continuation",
                )
                return None
            if receipt:
                await self._block_run(
                    exp_id,
                    "locked executable design differs from its pre-result approval; "
                    "refusing post-result design drift",
                )
                return None
        while True:
            # Review the artifact that will actually run, in the scientific context that
            # defines its estimand.  Reviewing the small model-parameter JSON alone caused
            # diagnostic reviewers to infer a causal claim that the locked hypothesis did
            # not make, while an author rebuttal could promise changes to fields the parser
            # never persisted.  Rebuttals may clarify existing text; only this outer loop can
            # submit a materially revised artifact.
            content = {
                "artifact_locked": locked,
                "design": design,
                "hypothesis": self.hypothesis,
                "demonstration": self._paradigm_demonstration(),
                "plan_objective": plan.get("objective"),
            }
            if self.survey_brief:
                content["literature"] = self.survey_brief
            panel = await self.gateway.review(
                "design", content, exp_id or self.run_id, run_id=self.run_id,
                dry_run=self.dry_run, mode=self._contribution_type(),
            )
            if panel.gate_passed:
                if locked and not await self._record_locked_gate_approval(
                    "design", locked_fingerprint, panel, exp_id or self.run_id
                ):
                    await self._block_run(
                        exp_id,
                        "could not persist the locked design approval before execution",
                    )
                    return None
                return design
            if self.hypothesis.get("hypothesis_locked"):
                await self._block_run(
                    exp_id,
                    "locked pre-registration rejected by the design gate; refusing to treat "
                    "an unimplemented rebuttal or post-review estimand change as approval",
                )
                return None
            n = self.guard.bump("design")
            if self.guard.exceeded("design"):
                await get_bus().publish(
                    make_event(
                        "escalation",
                        run_id=self.run_id,
                        payload={"reason": "design rejected past loop limit", "verdict": panel.consensus_verdict},
                    )
                )
                await asyncio.to_thread(set_run_status, self.run_id, "paused")
                await self._status("paused", "design gate could not be satisfied")
                return None
            # revise the design given the critic findings
            findings = [
                f"{f['severity']}/{f['category']}: {f['claim']} -> {f['suggestion']}"
                for c in panel.model_dump()["critiques"]
                for f in c["findings"]
            ]
            prompt = (
                f"The critic REJECTED this design (revision {n}):\n{json.dumps(design)}\n\n"
                f"Findings:\n" + "\n".join(findings) + "\n\nReturn ONLY a revised design JSON "
                "addressing the blockers (same keys)."
            )
            text = await reason_stage(
                self.run_id, "experiment_design", prompt,
                dry_run=self.dry_run,
                dry_text="[dry-run] revised design",
            )
            design = _parse_design(text, design)

    async def _run_eval(self, design, data_spec, domain, exp_id) -> dict[str, Any]:
        """Dispatch the evaluation: host-side (trusted, real LLM step) for domains that
        declare it, else the compute-backend sandbox. Scoring stays the fixed harness
        either way. For a PARADIGM contribution, also compute the domain's discriminating
        demonstration (deterministic, harness-owned) into ``info["demonstration"]`` — done
        here so the reproduction re-run (which re-enters this method) recomputes it too.

        The paradigm demonstration IS the contribution, so it is computed even when the
        (secondary) performance eval fails/times out — a failed performance model must not
        sink a paradigm run."""
        paradigm = self._uses_discriminating_demonstration()
        diagnostic_primary = self._contribution_type() == "diagnostic"
        if diagnostic_primary and not self.dry_run:
            # A diagnostic's preregistered demonstration is the primary evaluation.
            # Do not run the unrelated full benchmark protocol (25 repeated folds +
            # grouped folds + baseline panel + final fit) before it. This is not a
            # relaxed path: the demonstration still runs in the immutable hard
            # sandbox, on the sealed fresh batch, under the committed rule and audit.
            result = {
                "job_id": "diagnostic-demonstration",
                "metrics": {},
                "artifacts": [],
                "info": {
                    "performance_eval_skipped": True,
                    "protocol_status": "demonstration_only",
                    "model": design.get("model"),
                    "model_impl": "AI-authored preregistered diagnostic",
                    "eval_summary": (
                        "Primary evaluation is the sealed confirm-batch discriminating "
                        "demonstration; the generic benchmark suite was not run."
                    ),
                },
            }
            await get_bus().publish(make_event(
                "compute_status", run_id=self.run_id, payload={
                    "job_id": "diagnostic-demonstration",
                    "status": "skipped_secondary_benchmark",
                    "metrics": None,
                }))
        else:
            try:
                result = await self._dispatch_eval(design, data_spec, domain, exp_id)
            except Exception as exc:
                if not paradigm:
                    raise
                await get_bus().publish(make_event(
                    "compute_status", run_id=self.run_id,
                    payload={"job_id": "eval-failed", "status": "failed", "error": str(exc),
                             "note": "performance eval failed; paradigm demonstration still computed"}))
                result = {"job_id": "eval-failed", "metrics": {}, "artifacts": [],
                          "info": {"eval_error": str(exc)}}
        if paradigm and not result.get("blocked"):
            demo = await self._compute_demonstration(design, data_spec, domain)
            if demo is not None:
                result.setdefault("info", {})["demonstration"] = demo
                # a first-class artifact so the run has a verifiable record of its contribution
                result.setdefault("artifacts", []).append(
                    {"kind": "demonstration", "uri": f"demonstration:{demo.get('form', 'discriminating')}"})
                if diagnostic_primary and demo.get("test_statistic") is not None:
                    diagnostic_metrics = {
                        "diagnostic_test_statistic": float(demo["test_statistic"]),
                        "diagnostic_control_statistic": float(demo["control_statistic"]),
                    }
                    result.setdefault("metrics", {}).update(diagnostic_metrics)
                    if exp_id:
                        await asyncio.to_thread(
                            record_metrics, exp_id, diagnostic_metrics, "confirmation"
                        )
        return result

    async def _dispatch_eval(self, design, data_spec, domain, exp_id) -> dict[str, Any]:
        if self.profile and self.profile.host_side_run:
            # a planner-proposed ablation round runs the CONTROLLED retriever comparison
            if str(self.hypothesis.get("experiment_type", "")).lower() in ("ablation", "method_comparison"):
                return await self._rag_ablation(design, data_spec, domain, exp_id)
            return await self._rag_eval_hostside(design, data_spec, domain, exp_id)
        return await self._execute(design, data_spec, domain, exp_id)

    async def _compute_demonstration(self, design, data_spec, domain) -> dict[str, Any] | None:
        """Run the domain's deterministic discriminating demonstration for a paradigm
        contribution. Best-effort + dry-run-skipped (it needs real featurization/data);
        a domain without one returns None, leaving the formulation claim a proposal."""
        if self.dry_run:
            return None
        plugin = self.plugin or get_domain_plugin(domain)
        # carry the design's random_state into the demonstration so the reproduction
        # re-run (which reseeds the design) genuinely RE-COMPUTES the demonstration on a
        # perturbed sample — making `demonstration_reproduced` a real stability check, not
        # an identical recompute (see _demo_activity_cliff_lipschitz).
        spec = {**(self._paradigm_demonstration() or {}),
                "random_state": int(design.get("random_state", 42))}
        # FRONTIER PATH: if the AI authored a demonstration this experiment, route to the
        # AI-authored capability by EXPLICIT id and attach the sandboxed code + the pre-
        # registration READ BACK FROM THE LEDGER (the committed-before-results version — the
        # anti-fakeability guard). If authoring didn't happen, the spec is untagged and dispatch
        # falls back to the domain's registered capabilities exactly as before.
        if design.get("demonstration_code"):
            prereg = self._read_committed_preregistration()
            if prereg:
                spec["capability"] = plugin.AI_AUTHORED_CAPABILITY_ID
                spec["demonstration_code"] = design["demonstration_code"]
                spec["preregistration"] = prereg
                # K1: route the demonstration onto the held-out CONFIRM partition (the
                # explore->confirm seal). Reproduction reseeds the design but reuses this exact
                # index, so the re-run is a genuine recompute on the same held-out data.
                if design.get("demonstration_confirm_index") is not None:
                    spec["confirm_index"] = design["demonstration_confirm_index"]
                    spec["split_meta"] = design.get("demonstration_split_meta")
                    spec["family_alpha"] = (
                        (design.get("demonstration_split_meta") or {}).get("family_alpha")
                    )
        try:
            demo = await asyncio.to_thread(
                plugin.run_demonstration, spec, data_spec,
                run_artifacts_dir(self.run_id) / "demonstration",
            )
        except Exception as exc:  # noqa: BLE001 - demonstration is best-effort
            await get_bus().publish(
                make_event("demonstration", run_id=self.run_id,
                           payload={"computed": False, "error": str(exc)})
            )
            return None
        if demo is not None:
            await get_bus().publish(
                make_event("demonstration", run_id=self.run_id,
                           payload={"computed": True, "form": demo.get("form"),
                                    "holds": demo.get("holds"), "statistic": demo.get("statistic"),
                                    "capability": demo.get("capability"),
                                    # the negative-control split (AI-authored path) — what makes
                                    # `holds` honest: the effect must fire on test AND vanish on control
                                    "test_statistic": demo.get("test_statistic"),
                                    "control_statistic": demo.get("control_statistic"),
                                    "audit_refuted": demo.get("audit_refuted"),
                                    # K1 explore->confirm seal: was it applied, and on what split?
                                    "exploration_applied": demo.get("exploration_applied"),
                                    "n_confirm": demo.get("n_confirm"),
                                    "sandbox_image_id": demo.get("sandbox_image_id"),
                                    "split_meta": demo.get("split_meta")})
            )
        return demo

    async def _audit_demonstration(
        self, design: dict, demo_result: dict | None
    ) -> tuple[bool | None, bool]:
        """Independently AUDIT an AI-authored demonstration with the AUTHOR vendor EXCLUDED
        (genuine cross-vendor independence, not the orchestrator reviewing its own code). A refuting audit
        (gate did not pass) invalidates an existing boolean verdict and forces ``holds=False``;
        it cannot turn ``holds=None`` (no scientific verdict) into counter-evidence.

        Returns ``(audit_passed, audit_error)``, keeping the distinct states apart:
        - ``(True, False)``  — audit ran and PASSED with >= the vendor floor of auditors;
        - ``(False, False)`` — audit ran and REFUTED / was not independent (forces an existing
          boolean verdict false, while preserving not_evaluated as None);
        - ``(None, True)``   — NO USABLE independent verification: the audit infrastructure
          errored, OR it approved with fewer distinct auditors than ``min_review_vendors``
          (a one-survivor approval is not adequate independent verification). ``holds`` is
          left untouched (missing verification is NOT a refutation), strength caps at `weak`;
        - ``(None, False)``  — no audit applicable (not an AI-authored demo, disabled, dry-run,
          or no formulation claim).

        A refuting audit keeps its fail-closed force regardless of panel size — one adversarial
        finding is enough to block, but one approval is not enough to verify.

        Mutates ``demo_result`` in place (the same dict ``_finalize_claims`` reads)."""
        if self.dry_run:
            return None, False
        if not isinstance(demo_result, dict) or not demo_result.get("preregistration"):
            return None, False  # only AI-authored demonstrations carry a pre-registration
        produced_scientific_verdict = isinstance(demo_result.get("holds"), bool)
        fid = self._claim_ids.get("formulation")
        # K1 seal #5 (DETERMINISTIC, runs FIRST, harness-owned): refute a pre-registered threshold
        # that is inconsistent with the AI's OWN exploration observations (doom-to-zero /
        # control-not-silent / trivially-easy) WITHOUT the LLM. The cross-vendor auditor below is
        # the second, softer layer — never the only seal-#5 guard.
        exploration = design.get("demonstration_exploration")
        prereg_rule = demo_result.get("preregistration") or {}
        if exploration and prereg_rule:
            consistent, reason = self._prereg_consistent_with_exploration(prereg_rule, exploration)
            if not consistent:
                # Reject the artifact definition, but never manufacture a scientific
                # refutation when execution itself was not evaluable.
                if produced_scientific_verdict:
                    demo_result["holds"] = False
                demo_result["audit_refuted"] = True
                if fid:
                    await asyncio.to_thread(
                        attach_claim_evidence, fid, "audit", "reject",
                        f"deterministic seal #5 refutation: {reason}",
                    )
                await get_bus().publish(make_event("demonstration_audit", run_id=self.run_id, payload={
                    "verdict": "reject", "gate_passed": False, "auditors": [],
                    "deterministic": True, "reason": reason}))
                await get_bus().publish(make_event("demonstration", run_id=self.run_id, payload={
                    "computed": True, "form": demo_result.get("form"),
                    "holds": demo_result.get("holds"),
                    "statistic": demo_result.get("statistic"),
                    "capability": demo_result.get("capability"),
                    "test_statistic": demo_result.get("test_statistic"),
                    "control_statistic": demo_result.get("control_statistic"),
                    "audit_refuted": True,
                    "exploration_applied": demo_result.get("exploration_applied"),
                    "n_confirm": demo_result.get("n_confirm"),
                    "sandbox_image_id": demo_result.get("sandbox_image_id"),
                    "split_meta": demo_result.get("split_meta")}))
                return False, False
        if not getattr(get_settings(), "demonstration_audit_enabled", True):
            # AI-authored demonstration, but audit explicitly disabled: not a refutation, but
            # also not independently verified. Cap strength the same way as an audit infra error.
            if fid:
                await asyncio.to_thread(
                    attach_claim_evidence, fid, "audit", "disabled",
                    "independent demonstration audit disabled; strength capped",
                )
            await get_bus().publish(make_event("demonstration_audit", run_id=self.run_id, payload={
                "verdict": "disabled", "gate_passed": None, "auditors": []}))
            return None, True
        payload = {
            "artifact_locked": True,
            # Full source: code size was bounded before immutable pre-registration.
            # An audit of a truncated prefix is not an audit of the executed artifact.
            "demonstration_code": str(design.get("demonstration_code", "")),
            "test_statistic": demo_result.get("test_statistic"),
            "control_statistic": demo_result.get("control_statistic"),
            "preregistration": demo_result.get("preregistration"),
            "probes": demo_result.get("probes"),
            "detail": demo_result.get("detail"),
        }
        author_vendor = get_settings().orchestrator_vendor
        try:
            panel = await self.gateway.review(
                "demonstration_audit", payload, self.run_id, run_id=self.run_id,
                dry_run=self.dry_run, exclude_vendors={author_vendor},
            )
        except Exception as exc:  # noqa: BLE001 - the audit must never kill the run
            # The audit INFRASTRUCTURE errored — the demonstration was never independently
            # reviewed, which is NOT a refutation (the refuted/not-evaluated distinction):
            # leave ``holds`` untouched, but fail closed on STRENGTH via ``audit_error=True``
            # (``_claim_strength`` caps the formulation claim to ``weak`` — an unaudited
            # AI-authored demonstration must not carry independent-verification strength —
            # yet the claim is NOT marked ``refuted``, since the audit never ran).
            await get_bus().publish(make_event("demonstration_audit", run_id=self.run_id, payload={
                "verdict": "error", "gate_passed": None, "auditors": [],
                "error": str(exc)[:200]}))
            if fid:
                await asyncio.to_thread(
                    attach_claim_evidence, fid, "audit", "error",
                    f"independent demonstration audit could not run: {str(exc)[:200]}",
                )
            return None, True
        audit_passed = bool(panel.gate_passed)
        # Independence guard: if the selected author vendor somehow appears in the panel, treat the
        # audit as NOT independent -> fail closed (cannot be trusted as a real audit).
        auditors = {c.critic_id for c in (getattr(panel, "critiques", None) or [])}
        if author_vendor in auditors:
            audit_passed = False
        # vendor floor (mirrors the results gate's min_review_vendors): a PASSING audit on
        # fewer distinct auditors — e.g. every provider but one errored out — is NOT adequate
        # independent verification. Degrade (strength caps at `weak` via audit_error) WITHOUT
        # refuting; only a genuine adversarial rejection forces holds=False.
        min_vendors = int(getattr(get_settings(), "min_review_vendors", 2))
        if audit_passed and len(auditors) < min_vendors:
            if fid:
                await asyncio.to_thread(
                    attach_claim_evidence, fid, "audit", "degraded",
                    f"audit approved but with only {len(auditors)} independent auditor(s) "
                    f"(< {min_vendors}) — not adequate verification; strength capped",
                )
            await get_bus().publish(make_event("demonstration_audit", run_id=self.run_id, payload={
                "verdict": "degraded", "gate_passed": None, "auditors": sorted(auditors),
                "n_auditors": len(auditors), "min_vendors": min_vendors}))
            return None, True
        if not audit_passed:
            # A substantive audit can invalidate an existing confirm verdict, but it
            # cannot turn not_evaluated (sample/infra failure) into counter-evidence.
            if produced_scientific_verdict:
                demo_result["holds"] = False
            demo_result["audit_refuted"] = True
            # re-publish the demonstration with the audit's mutation applied, so the event
            # stream / e2e summary / UI reflect the POST-audit verdict instead of the stale
            # snapshot emitted at compute time (seen live: summary showed audit_refuted=null
            # while the audit verdict was reject).
            await get_bus().publish(make_event("demonstration", run_id=self.run_id, payload={
                "computed": True, "form": demo_result.get("form"),
                "holds": demo_result.get("holds"), "statistic": demo_result.get("statistic"),
                "capability": demo_result.get("capability"),
                "test_statistic": demo_result.get("test_statistic"),
                "control_statistic": demo_result.get("control_statistic"),
                "audit_refuted": True,
                "exploration_applied": demo_result.get("exploration_applied"),
                "n_confirm": demo_result.get("n_confirm"),
                "sandbox_image_id": demo_result.get("sandbox_image_id"),
                "split_meta": demo_result.get("split_meta")}))
        if fid:
            await asyncio.to_thread(
                attach_claim_evidence, fid, "audit", str(panel.consensus_verdict),
                f"independent demonstration audit (author-excluded): {panel.consensus_verdict}",
            )
        await get_bus().publish(make_event("demonstration_audit", run_id=self.run_id, payload={
            "verdict": panel.consensus_verdict, "gate_passed": panel.gate_passed,
            "auditors": sorted(auditors)}))
        return audit_passed, False

    _DEFAULT_RAG_PROMPT = (
        "Answer the question using ONLY the context. Be concise (a short phrase). "
        "If the context does not contain the answer, say 'unknown'.\n\n"
        "CONTEXT:\n{context}\n\nQUESTION: {question}\nANSWER:"
    )

    async def _rag_generate(self, question: str, contexts: list[str], prompt: str) -> str:
        """Generate one answer HOST-SIDE. Real run → an orchestrator-model call (trusted; the no-network
        sandbox is untouched). Dry-run / no creds → the offline extractive baseline."""
        from aletheia.domains.rag.retriever import extractive_answerer

        if self.dry_run:
            return extractive_answerer(question, contexts)
        ctx = "\n".join(contexts)
        filled = prompt.replace("{context}", ctx).replace("{question}", question)
        text = await run_worker(
            self.run_id, "rag_generate", filled,
            system="You answer questions strictly from the provided context; never invent facts.",
            dry_run=False, dry_value=extractive_answerer(question, contexts),
        )
        # a degraded worker (API hiccup) falls back to the offline answerer, never crashes the eval
        return extractive_answerer(question, contexts) if is_degraded(text) else text.strip()

    def _make_retriever(self, design: dict) -> tuple[Any, str]:
        """Resolve the retriever the design selected. ``dense`` → a real embedding
        retriever (host-side, trusted, offline once the model is cached); anything else
        — and ANY dry-run (the hash-stub embedder is random, so dense is meaningless
        offline) — → the deterministic lexical baseline.

        If a real run requests ``dense`` but the real embedding backend is unavailable,
        we DO NOT silently rank with random hash vectors and call it "dense"; we fall
        back to the honest lexical baseline and label it as such (the caller's
        ``retriever`` event then reports ``fallback: true``)."""
        from aletheia.domains.rag.retriever import dense_retrieve, retrieve

        if str(design.get("retriever", "lexical")).lower() == "dense" and not self.dry_run:
            from aletheia.memory.embedder import EmbedderUnavailableError, get_embedder

            try:
                embed = get_embedder(dry_run=False, require_real=True).embed
            except EmbedderUnavailableError:
                return retrieve, "lexical token-overlap (dense embedder unavailable)"
            return (lambda corpus, q, k: dense_retrieve(corpus, q, k, embed=embed)), "dense (embeddings)"
        return retrieve, "lexical token-overlap"

    async def _rag_eval_hostside(self, design, data_spec, domain, exp_id) -> dict[str, Any]:
        """Run the RAG eval host-side: retrieve → generate (host-side LLM) → score (fixed
        deterministic harness). The retriever + the LLM author only retrieval order +
        answer text; recall@k / F1 / EM are computed by the harness."""
        from aletheia.domains.rag.retriever import extractive_answerer

        await self._status("executing", "host-side RAG generation + scoring")
        plugin = self.plugin or get_domain_plugin(domain)
        data = await asyncio.to_thread(plugin.load_data, data_spec)
        corpus, cases = data["corpus"], data["cases"]
        k = int(design.get("k", 5))
        prompt = str(design.get("answer_prompt") or self._DEFAULT_RAG_PROMPT)
        model = get_settings().orchestrator_model
        retriever_fn, retriever_label = self._make_retriever(design)
        requested_dense = str(design.get("retriever", "lexical")).lower() == "dense"
        dense_fellback = requested_dense and retriever_label.startswith("lexical")
        await get_bus().publish(
            make_event("retriever", run_id=self.run_id, payload={
                "requested": "dense" if requested_dense else "lexical", "used": retriever_label,
                "fallback": dense_fellback})
        )

        if not self.dry_run:
            await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        # pre-generate all answers (async, host-side), then hand a sync lookup to the
        # deterministic scorer.
        answers: dict[str, str] = {}
        for case in cases:
            q = str(case.get("question", ""))
            contexts = [r["text"] for r in retriever_fn(corpus, q, k)]
            answers[q] = await self._rag_generate(q, contexts, prompt)

        def _answerer(q: str, contexts: list[str]) -> str:
            return answers.get(q, extractive_answerer(q, contexts))

        label = "extractive (dry-run)" if self.dry_run else f"host-side LLM ({model})"
        per_call_cost = 0.0 if self.dry_run else 0.001  # estimate; trued up later
        result = await asyncio.to_thread(
            plugin.evaluate, corpus, cases, k,
            answerer=_answerer, retriever=retriever_fn, retriever_label=retriever_label,
            workdir=run_artifacts_dir(self.run_id) / "rag_eval",
            design=design, answerer_label=label, cost_per_answer=per_call_cost,
        )
        if exp_id:  # mirror the compute backend's ledger recording (skip for reproduction)
            await asyncio.to_thread(record_metrics, exp_id, result.metrics, "test")
            await asyncio.to_thread(record_artifacts, exp_id, result.artifacts)
        await get_bus().publish(
            make_event("compute_status", run_id=self.run_id,
                       payload={"job_id": "host-side", "status": "done", "metrics": result.metrics})
        )
        info = dict(result.info)
        if dense_fellback:
            # the requested scientific method (dense retrieval) was NOT instantiated;
            # the eval ran on the lexical baseline. Flag it so the mechanism claim is
            # finalized as `not_evaluated` (reuses the method-drift machinery downstream).
            info["dense_fallback"] = True
            info["requested_method"] = "dense (embeddings)"
        return {"job_id": "host-side", "metrics": result.metrics,
                "artifacts": result.artifacts, "info": info}

    async def _rag_ablation(self, design, data_spec, domain, exp_id) -> dict[str, Any]:
        """A CONTROLLED retriever ablation: run the eval with the lexical AND the dense
        retriever, holding the answerer fixed (extractive) so the ONLY variable is the
        retriever. Embeddings are free + offline, so this runs even in dry-run (it
        degrades to the random hash stub only if sentence-transformers is absent).
        Headline = the dense retriever's F1; the deltas vs lexical drive a comparison
        claim.

        Fail-closed: a dense-vs-lexical comparison whose "dense" arm is the random hash
        stub is meaningless. A real run therefore REQUIRES a real embedding backend and
        pauses if one is unavailable, rather than reporting a random-vector ablation."""
        from aletheia.domains.rag.compare import compare_retrievers
        from aletheia.memory.embedder import EmbedderUnavailableError, get_embedder

        await self._status("executing", "retriever ablation (lexical vs dense)")
        if not self.dry_run:
            await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        plugin = self.plugin or get_domain_plugin(domain)
        data = await asyncio.to_thread(plugin.load_data, data_spec)
        corpus, cases = data["corpus"], data["cases"]
        k = int(design.get("k", 3))
        # dry-run: keep the offline hash stub (plumbing test). real run: require the
        # real backend — never report a random-vector "dense beats lexical" result.
        try:
            embed = get_embedder(dry_run=False, require_real=not self.dry_run).embed
        except EmbedderUnavailableError as exc:
            await self._block_run(
                exp_id,
                f"dense-vs-lexical ablation needs a real embedding backend, but it is "
                f"unavailable ({exc}); pausing rather than reporting a random-vector comparison",
            )
            return {"job_id": "ablation", "blocked": True, "metrics": {}, "artifacts": [], "info": {}}
        cmp = await asyncio.to_thread(
            compare_retrievers, corpus, cases, k,
            embed=embed, workdir=run_artifacts_dir(self.run_id) / "rag_ablation",
        )
        lex, dense, delta = cmp["lexical"], cmp["dense"], cmp["delta"]
        metrics = {
            "answer_f1": dense["answer_f1"],  # HEADLINE (the candidate method)
            "recall_at_k": dense["recall_at_k"],
            "answer_f1_dense": dense["answer_f1"], "recall_at_k_dense": dense["recall_at_k"],
            "answer_f1_lexical": lex["answer_f1"], "recall_at_k_lexical": lex["recall_at_k"],
            "answer_f1_delta": delta["answer_f1"], "recall_at_k_delta": delta["recall_at_k"],
            "exact_match": dense["exact_match"], "latency_ms": dense["latency_ms"], "cost_usd": 0.0,
        }
        path = run_artifacts_dir(self.run_id) / "ablation.json"
        path.write_text(json.dumps(cmp, indent=2))
        eval_summary = (
            f"Retriever ablation (answerer fixed) on {cmp['n']} cases, k={k}: "
            f"answer-F1 lexical {lex['answer_f1']:.3f} -> dense {dense['answer_f1']:.3f} "
            f"({delta['answer_f1']:+.3f}); recall@{k} {lex['recall_at_k']:.3f} -> "
            f"{dense['recall_at_k']:.3f} ({delta['recall_at_k']:+.3f})"
        )
        artifacts = [{"kind": "eval", "uri": str(path)}]
        if exp_id:
            await asyncio.to_thread(record_metrics, exp_id, metrics, "test")
            await asyncio.to_thread(record_artifacts, exp_id, artifacts)
        await get_bus().publish(
            make_event("compute_status", run_id=self.run_id,
                       payload={"job_id": "ablation", "status": "done", "metrics": metrics})
        )
        return {"job_id": "ablation", "metrics": metrics, "artifacts": artifacts,
                "info": {"n_eval": cmp["n"], "model": "retriever ablation",
                         "model_impl": "retriever ablation (lexical vs dense)",
                         "protocol_status": "eval_set", "eval_summary": eval_summary,
                         "ablation": cmp}}

    async def _execute(self, design, data_spec, domain, exp_id) -> dict[str, Any]:
        spec = JobSpec(
            run_id=self.run_id, domain=domain, design=design, data_spec=data_spec,
            experiment_id=exp_id, dry_run=self.dry_run,
        )
        job_id = await asyncio.to_thread(self.backend.submit, spec)
        await get_bus().publish(
            make_event("compute_submitted", run_id=self.run_id, payload={"job_id": job_id, "design": design})
        )
        deadline = asyncio.get_event_loop().time() + 600
        st = await asyncio.to_thread(self.backend.status, job_id)
        while not st.finished and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1.0)
            st = await asyncio.to_thread(self.backend.status, job_id)
        await get_bus().publish(
            make_event(
                "compute_status", run_id=self.run_id,
                payload={"job_id": job_id, "status": st.status, "metrics": st.metrics},
            )
        )
        if st.status != "done":
            raise RuntimeError(f"training job failed: {st.error}")
        return {"job_id": job_id, "metrics": st.metrics or {}, "artifacts": st.artifacts, "info": st.info}

    async def _analyze(self, design, result, exp_id) -> str:
        """Decomposed, scientific analysis: independent concerns reviewed in parallel
        isolated worker contexts, then synthesized into a scientific reading —
        hypothesis verdict, claims (finding→mechanism→implication), an ablation reading
        of the baseline panel, a comparison to the surveyed literature/SOTA, and
        limitations. The main loop keeps only the merged text, never the sub-turns."""
        if self._contribution_type() == "diagnostic":
            demo = (result.get("info") or {}).get("demonstration") or {}
            holds = demo.get("holds")
            verdict = (
                "met the preregistered rule" if holds is True else
                "did not meet the preregistered rule" if holds is False else
                "was not evaluable"
            )
            prereg = demo.get("preregistration") or {}
            probes = demo.get("probes") or {}
            return (
                "Harness-owned diagnostic analysis: the sealed fresh-confirmation evaluation "
                f"{verdict}. The prespecified test statistic was "
                f"{demo.get('test_statistic')} K under supported_if={prereg.get('supported_if')}; "
                f"the matched/permuted negative-control statistic was "
                f"{demo.get('control_statistic')} K under "
                f"control_silent_if={prereg.get('control_silent_if')}. "
                f"The confirmation sample count was {demo.get('n_confirm')}; deterministic "
                f"degeneracy probes were clean={probes.get('clean')}. This is a descriptive, "
                "chemistry-stratified model-error audit only: it does not identify a causal "
                "materials mechanism, establish irreducible error, or claim benchmark novelty."
            )
        metrics = result.get("metrics", {})
        eval_summary = (result.get("info") or {}).get("eval_summary", "")
        hm = self.profile.headline_metric if self.profile else "mae"
        hv = metrics.get(hm)
        protocol = self.profile.protocol_desc if self.profile else "grouped CV + holdout + baselines"
        sota = self.profile.sota_reference if self.profile else "no comparable published benchmark"
        lit = f"LITERATURE (prior work):\n{self.survey_brief}\n" if self.survey_brief else ""
        evidence = (
            f"HYPOTHESIS: {json.dumps(self.hypothesis) if self.hypothesis else 'n/a'}\n"
            f"DESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
            f"EVAL PROTOCOL SUMMARY: {eval_summary}\n"
            f"KNOWN SOTA: {sota}\n{lit}"
        )
        # one focused, isolated sub-check per concern — independent, so parallel
        subchecks = {
            "leakage": "Assess DATA LEAKAGE risk only (train/test contamination, grouped split adequacy).",
            "overfit": "Assess OVERFITTING only (holdout vs grouped CV vs RepeatedKFold spread; CV std).",
            "baseline": "Assess BASELINE ADEQUACY only (does the model beat dummy/ridge/knn/gbm meaningfully?).",
            "stats": "Assess STATISTICAL VALIDITY only (CI width, error stratification by range, sample sizes).",
            "sota": "Assess PRIOR WORK / SOTA only: is the headline grouped-CV result competitive with the "
                    "literature above / the KNOWN SOTA, and does it support or refute the hypothesis? If no "
                    "comparable published number is available, say so explicitly — do not invent one.",
        }
        header = (
            f"Interpret these regression results. The HEADLINE metric is the grouped-CV score "
            f"({protocol}); the random holdout is optimistic. "
        )
        findings = await asyncio.gather(
            *[
                run_worker(
                    self.run_id, f"analysis:{name}",
                    f"{header}{focus}\nBe concise (2-3 sentences).\n\n{evidence}",
                    dry_run=self.dry_run,
                    dry_value=f"[dry-run] {name}: nominal; headline {hm}={hv}.",
                )
                for name, focus in subchecks.items()
            ]
        )
        # drop sub-checks that degraded (transient API failure) so error text never
        # pollutes the synthesis; note which were unavailable instead.
        ok = [(n, f) for n, f in zip(subchecks, findings) if not is_degraded(f)]
        sub_text = "\n".join(f"- {n}: {f}" for n, f in ok)
        unavailable = [n for n, f in zip(subchecks, findings) if is_degraded(f)]
        if unavailable:
            sub_text += f"\n- (unavailable sub-checks, excluded: {', '.join(unavailable)})"
        synthesis = await run_worker(
            self.run_id, "analysis",
            "Synthesize these independent sub-reviews into a SCIENTIFIC analysis, grounded in "
            "the metrics + literature (do not overclaim). Produce:\n"
            "1) HYPOTHESIS VERDICT — supported or refuted, judged against its prediction.\n"
            "2) CLAIMS — 1-2, each as finding -> mechanism -> implication.\n"
            "3) ABLATION READING — what the baseline panel + error stratification reveal.\n"
            "4) SOTA COMPARISON — how the grouped-CV headline sits vs the prior work / known SOTA "
            "(or 'no comparable number').\n"
            "5) LIMITATIONS. Lead with the grouped-CV headline.\n\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis) if self.hypothesis else 'n/a'}\n"
            f"SUB-REVIEWS:\n{sub_text}\n\nMETRICS: {json.dumps(metrics)}\nKNOWN SOTA: {sota}\n{lit}",
            dry_run=self.dry_run,
            dry_value=(
                f"[dry-run] Analysis — Hypothesis verdict: supported (headline {hm}={hv} < baselines). "
                f"Claim: the chosen features carry signal for {self.profile.task if self.profile else 'the task'} "
                f"-> the model captures it -> a usable predictor. Ablation: beats dummy/ridge/knn/gbm. "
                f"SOTA: {sota}. Limitations: one dataset, single split family. "
                f"Sub-checks: {sub_text}"
            ),
        )
        # evidence-ledger: a metric claim (the headline result) + a mechanism claim
        # (the hypothesis verdict). Created PROPOSED here so the results gate reviews
        # them; finalized after the gate in _run_experiment. Strength is pre-gate.
        info = result.get("info") or {}
        protocol_status = info.get("protocol_status")
        eval_uri = next(
            (a.get("uri") for a in (result.get("artifacts") or []) if a.get("kind") == "eval"), ""
        )
        units = self.profile.units if self.profile else ""
        usfx = f" {units}" if units else ""
        # only assert a metric claim if the performance eval actually produced a headline
        # value (a paradigm run whose performance eval failed/timed-out has none — its
        # contribution rests on the demonstration, not a metric).
        if hv is not None:
            self._claim_ids["metric"] = await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=f"Under grouped CV, {design.get('model')} attains {hm}={hv}{usfx}.",
                claim_type="metric",
                strength=self._claim_strength("metric", protocol_status=protocol_status, gate_passed=None),
                status="proposed", experiment_id=exp_id, created_by="analysis", stage="analysis",
                evidence=[
                    {"evidence_kind": "metric", "evidence_ref": hm, "note": f"value={hv}"},
                    *([{"evidence_kind": "artifact", "evidence_ref": eval_uri, "note": "eval.json"}] if eval_uri else []),
                ],
            )
        self._claim_ids["mechanism"] = await asyncio.to_thread(
            create_claim, self.run_id,
            claim_text=f"Hypothesis verdict (per analysis): {str(self.hypothesis.get('statement', '')).strip()}",
            claim_type="mechanism",
            strength=self._claim_strength("mechanism", gate_passed=None),
            status="proposed", experiment_id=exp_id, created_by="analysis", stage="analysis",
            evidence=[{"evidence_kind": "experiment", "evidence_ref": exp_id or self.run_id, "note": "analysis synthesis"}],
        )
        return synthesis

    async def _optimize(self, design, result, data_spec, domain, exp_id, plugin, *, gate_passed: bool = True):
        if self._uses_discriminating_demonstration():
            # Tuning a secondary benchmark after seeing the confirm-batch diagnostic
            # cannot strengthen the preregistered statistic and risks adaptive drift.
            await get_bus().publish(make_event(
                "optimize", run_id=self.run_id, payload={
                    "skipped": True,
                    "reason": "locked demonstration is the primary endpoint",
                }))
            return design, result
        # the results gate rejected: optimizing the metric won't address the critique,
        # so skip the extra training stage and keep the reviewed result as-is.
        if not gate_passed:
            await get_bus().publish(
                make_event("optimize", run_id=self.run_id,
                           payload={"skipped": True, "reason": "results gate rejected; tuning will not address the critique"})
            )
            return design, result
        # try the alternate baseline model once; keep whichever has lower MAE
        alt = dict(plugin.baselines()[-1])
        alt.setdefault("test_size", design.get("test_size", 0.2))
        alt.setdefault("random_state", design.get("random_state", 42))
        if data_spec.get("target_column"):
            alt["target_column"] = data_spec["target_column"]
        if alt.get("model") == design.get("model"):
            return design, result  # nothing distinct to try
        alt_result = await self._run_eval(alt, data_spec, domain, exp_id)
        # compare on the domain's declared headline metric, honoring its goal
        # (min for error metrics like MAE; max for F1/recall).
        hk = self.profile.headline_metric if self.profile else "mae"
        cur_score = result.get("metrics", {}).get(hk, self._worst())
        alt_score = alt_result.get("metrics", {}).get(hk, self._worst())
        if self._is_better(alt_score, cur_score):
            await get_bus().publish(
                make_event(
                    "optimize", run_id=self.run_id,
                    payload={"kept": alt.get("model"), "metric": hk, "score": alt_score, "previous": cur_score},
                )
            )
            return alt, alt_result
        await get_bus().publish(
            make_event(
                "optimize", run_id=self.run_id,
                payload={"kept": design.get("model"), "metric": hk, "score": cur_score, "alt": alt_score},
            )
        )
        return design, result

    async def _write_up_diagnostic(
        self, plan, design, result, analysis, rpanel, exp_id
    ) -> None:
        """Write a deterministic report for a pre-registered diagnostic.

        The generic writer assumes a fitted benchmark model, grouped-CV headline,
        baseline panel and SOTA comparison.  A diagnostic intentionally skips that
        unrelated suite: its evidence is the sealed demonstration, negative control,
        fixed alpha and locked-code recomputation.  Keeping this report harness-owned
        prevents a language model from filling absent benchmark fields with a stronger
        story than the ledger supports.
        """
        info = result.get("info") or {}
        demo = info.get("demonstration") or {}
        prereg = demo.get("preregistration") or {}
        probes = demo.get("probes") or {}
        split_meta = demo.get("split_meta") or {}
        components = demo.get("components") or {}
        repro = info.get("reproduction") or {}
        claims = await asyncio.to_thread(list_claims, self.run_id, exp_id)
        claim_policy = self._writeup_claim_policy(claims)
        refs, references_md = numbered_references(self.survey_papers)
        cite = " ".join(f"[{i}]" for i in range(1, min(3, len(refs)) + 1))
        prior_sentence = (
            f"The pre-run literature survey supplied the study context {cite}."
            if cite else
            "The literature survey did not yield a citable reference for this report."
        )
        holds = demo.get("holds")
        verdict = (
            "met" if holds is True else "did not meet" if holds is False else "did not produce"
        )
        alpha_used = components.get("family_alpha_used")
        component_text = json.dumps(components, indent=2, sort_keys=True, default=str)[:6000]
        prereg_text = json.dumps(prereg, indent=2, sort_keys=True, default=str)[:6000]
        report = (
            "# A sealed chemistry-stratified error audit of a composition-only Tc model\n\n"
            "## Abstract\n\n"
            "We evaluated a pre-specified descriptive diagnostic on a fresh, sealed "
            f"confirmation batch. The harness {verdict} a confirmatory verdict: the locked "
            f"test statistic was {demo.get('test_statistic')} K and the matched/permuted "
            f"negative-control statistic was {demo.get('control_statistic')} K at allocated "
            f"alpha {split_meta.get('family_alpha', alpha_used)}. The result was derived by "
            "sandboxed authored code under a rule committed before confirmation data were "
            "opened. This is a conditional model-error audit, not evidence for a causal "
            "materials mechanism, irreducible error, benchmark superiority, or universal "
            "generality.\n\n"
            "## 1. Introduction\n\n"
            f"{prior_sentence} The locked question was: "
            f"{self.hypothesis.get('statement', plan.get('objective', 'n/a'))}\n\n"
            "## 2. Related Work\n\n"
            f"The survey retrieved {len(refs)} citable work(s). It was used to motivate and "
            "bound the question, not to assert novelty or import a published benchmark number.\n\n"
            "## 3. Method\n\n"
            "The primary endpoint was the AI-authored discriminating demonstration; the "
            "generic repeated/grouped-CV benchmark and post-confirmation optimization suite "
            "were intentionally not run. The chemistry stratum and matching descriptors were "
            "defined without the target. Authored code ran inside the immutable hard sandbox "
            f"image `{demo.get('sandbox_image_id')}`. The confirmation role was "
            f"`{split_meta.get('role')}`, batch `{split_meta.get('confirmation_batch')}`, "
            f"with n={demo.get('n_confirm')} and split hash "
            f"`{split_meta.get('confirmation_index_hash') or split_meta.get('index_hash')}`. "
            f"The code reported use of harness alpha `{alpha_used}`; the harness rejected a "
            "missing or mismatched report. The exact committed rule was:\n\n"
            f"```json\n{prereg_text}\n```\n\n"
            "## 4. Results\n\n"
            f"The test statistic was `{demo.get('test_statistic')}` K and its pre-registered "
            f"rule was `{prereg.get('supported_if')}`. The negative-control statistic was "
            f"`{demo.get('control_statistic')}` K and its silence rule was "
            f"`{prereg.get('control_silent_if')}`. The harness verdict was `holds={holds}`; "
            f"test trigger=`{demo.get('test_triggers')}`, control silent="
            f"`{demo.get('control_silent')}`, deterministic probes clean="
            f"`{probes.get('clean')}`. Supporting components were:\n\n"
            f"```json\n{component_text}\n```\n\n"
            "The same locked code was recomputed on the same confirmation batch with a "
            f"different seed: verdict stable=`{repro.get('demonstration_verdict_stable')}`, "
            f"statistic stable=`{repro.get('demonstration_statistic_stable')}`, and strict "
            f"demonstration reproduction=`{repro.get('demonstration_reproduced')}` "
            f"(original `{repro.get('demonstration_original_statistic')}`, recomputation "
            f"`{repro.get('demonstration_repro_statistic')}`). This is a robustness "
            "recomputation, not a fresh-data replication.\n\n"
            "## 5. Discussion & Limitations\n\n"
            f"The results-review panel returned `{rpanel.consensus_verdict}`. {analysis}\n\n"
            "The comparison is descriptive and conditional on the declared matching "
            "variables. It does not show that chemistry caused the error gap, that the "
            "matching variables exhaust all generic differences, or that the same fitted "
            "model transfers between datasets. Internal final-holdout and external "
            "locked-procedure results occur only after campaign selection and are therefore "
            "reported in the campaign artifact, not retrofitted into this round report.\n\n"
            "### Claim-ledger view\n\n"
            f"Allowed findings:\n\n{claim_policy['allowed']}\n\n"
            f"Required limitations:\n\n{claim_policy['limitations']}\n\n"
            f"Not findings / must remain qualified:\n\n{claim_policy['restricted']}"
        )
        if references_md:
            report += "\n\n" + references_md
        adir = run_artifacts_dir(self.run_id)
        path = adir / "report.md"
        path.write_text(report)
        # Keep a per-experiment immutable copy: `report.md` is the convenient
        # latest view, but a multi-round campaign must not overwrite round 1's
        # evidence when round 2 writes its report.
        versioned_path = adir / f"report_{exp_id}.md" if exp_id else path
        if versioned_path != path:
            versioned_path.write_text(report)
        artifacts: list[dict[str, str]] = [
            {"kind": "report", "uri": str(versioned_path)}
        ]
        bib = to_bibtex(self.survey_papers)
        if bib:
            bib_path = adir / "references.bib"
            bib_path.write_text(bib)
            artifacts.append({"kind": "bibliography", "uri": str(bib_path)})
        if exp_id:
            await asyncio.to_thread(record_artifacts, exp_id, artifacts)
        await get_bus().publish(
            make_event("report", run_id=self.run_id, payload={"uri": str(path), "preview": report[:400]})
        )
        await self._index(
            "conclusion",
            f"{plan.get('objective', '')}: sealed diagnostic test={demo.get('test_statistic')} K, "
            f"control={demo.get('control_statistic')} K, holds={holds}; verdict "
            f"{rpanel.consensus_verdict}.",
            exp_id,
            headline=demo.get("test_statistic"),
            model=design.get("model"),
        )
        await self._iam_finalize(report, bib, rpanel, exp_id)

    async def _write_up(self, plan, design, result, analysis, rpanel, exp_id) -> None:
        if self._contribution_type() == "diagnostic":
            await self._write_up_diagnostic(plan, design, result, analysis, rpanel, exp_id)
            return
        metrics = result.get("metrics", {})
        info = result.get("info") or {}
        eval_summary = info.get("eval_summary", "")
        impl = info.get("model_impl", "")  # the estimator class actually fit + scored
        requested = str(design.get("model", "")).strip()
        hk = self.profile.headline_metric if self.profile else "mae"
        units = self.profile.units if self.profile else ""
        usfx = f" {units}" if units else ""
        task = self.profile.task if self.profile else "property regression"
        protocol = self.profile.protocol_desc if self.profile else "grouped CV + holdout + baselines"
        feat = self.profile.feature_desc if self.profile else "a numeric feature matrix"
        sota = self.profile.sota_reference if self.profile else "no comparable published benchmark"
        hv = metrics.get(hk)
        # Real references retrieved by the SURVEY — the writer may cite ONLY these.
        refs, references_md = numbered_references(self.survey_papers)
        cite_list = "\n".join(
            f"[{i}] {p.title}{f' ({p.year})' if p.year else ''}" for i, p in enumerate(refs, start=1)
        )
        # evidence-ledger: finalize this experiment's claims before the writer sees them.
        protocol_status = info.get("protocol_status")
        # An injected F8-S6 evaluator campaign replaces the legacy caller-score shortcut. The
        # current result must expose its exact candidate ProtocolSignature identity; absence or
        # mismatch becomes an unverified weak claim, never a fallback to the legacy comparison.
        if self.auditable_sota_campaign_fn is not None:
            audited_sota = await self._resolve_auditable_sota_claim(
                headline_metric=hk,
                headline_score=hv,
                candidate_protocol_sha256=str(
                    info.get("candidate_protocol_sha256") or ""
                ),
                experiment_id=exp_id,
            )
            sota_text = audited_sota["claim_text"]
            sota_strength = audited_sota["strength"]
            sota_status = audited_sota["status"]
            sota_evidence = audited_sota["evidence"]
        else:
            # Backward-compatible Phase-H path for deployments that have not yet materialized an
            # evaluator-owned F8-S6 campaign.
            best_sota, comparable, beat = self._compare_to_sota(hk, hv)
            if not comparable or best_sota is None:
                # best_sota is None with comparable rows when there is NO headline value to
                # compare (the performance eval produced no metrics) — no comparison happened.
                sota_text = (
                    f"No structured SOTA row comparable on {hk}; known reference: {sota}."
                    if not comparable
                    else f"No headline {hk} was measured (performance eval produced no metrics); "
                    f"{len(comparable)} comparable SOTA row(s) exist but no comparison is possible."
                )
                sota_strength = "weak"
            else:
                rel = "beats" if beat else "does not beat"
                sota_text = (
                    f"The headline {hk} ({hv}{usfx}) {rel} the best comparable published result "
                    f"({best_sota.get('method')} {best_sota.get('score')} on {best_sota.get('dataset')})."
                )
                # only a grouped + gate-passed WIN earns 'strong'; otherwise comparable -> moderate.
                # single-vendor (degraded) review caps it at weak (see _claim_strength rationale).
                _xv = len({c.critic_id for c in (rpanel.critiques or [])}) >= int(get_settings().min_review_vendors)
                sota_strength = (
                    "strong" if (beat and protocol_status == "grouped" and rpanel.gate_passed) else "moderate"
                )
                if not _xv and sota_strength in ("moderate", "strong"):
                    sota_strength = "weak"
            sota_status = (
                "supported" if (comparable and best_sota is not None) else "unverified"
            )
            sota_evidence = [
                {"evidence_kind": "paper",
                 "evidence_ref": f"{r.get('method')} ({r.get('source')})",
                 "note": f"{r.get('metric')}={r.get('score')} on {r.get('dataset')} [{r.get('split_policy')}]"}
                for r in comparable[:4]
            ] or [{"evidence_kind": "metric", "evidence_ref": hk, "note": f"curated SOTA string: {sota}"}]
        await asyncio.to_thread(
            create_claim, self.run_id,
            claim_text=sota_text, claim_type="sota", strength=sota_strength,
            status=sota_status,
            experiment_id=exp_id, created_by="write_up", stage="write_up",
            evidence=sota_evidence,
        )
        # method-provenance: if the executed estimator is the RF fallback while a
        # different method was requested, record it as a limitation (and the metric
        # claim about THAT method is only weakly supported).
        fallback_ran = (
            impl == "RandomForestRegressor"
            and "random" not in requested.lower()
            and "forest" not in requested.lower()
            and bool(requested)
        )
        if fallback_ran:
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=f"The requested method ({requested}) was not executed; a {impl} fallback ran instead.",
                claim_type="limitation", strength="moderate", status="supported",
                experiment_id=exp_id, created_by="write_up", stage="write_up",
                evidence=[{"evidence_kind": "code", "evidence_ref": impl, "note": "executed implementation"}],
            )
            if self._claim_ids.get("metric"):
                await asyncio.to_thread(update_claim, self._claim_ids["metric"], strength="weak")
        # plan↔execution drift: the hypothesis named a model family that was not the one
        # executed, so its mechanism was never instantiated — record it as a limitation
        # (the mechanism claim is already marked not_evaluated in _finalize_claims).
        if info.get("method_drift"):
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=f"Hypothesis not evaluated: {info.get('method_drift_msg', 'executed method differs from the hypothesis')}.",
                claim_type="limitation", strength="moderate", status="supported",
                experiment_id=exp_id, created_by="write_up", stage="write_up",
                evidence=[{"evidence_kind": "code", "evidence_ref": impl or requested,
                           "note": "executed implementation differs from the hypothesized method"}],
            )
        # paradigm contribution whose discriminating demonstration was never executed:
        # record it as a limitation so the report states the formulation is a PROPOSAL
        # (the formulation claim is already not_evaluated/speculative in _finalize_claims).
        if self._uses_discriminating_demonstration() and not info.get("demonstration"):
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=("The proposed new formulation's discriminating demonstration was not "
                            "executed; the formulation remains a PROPOSAL (not evaluated), not a finding."),
                claim_type="limitation", strength="moderate", status="supported",
                experiment_id=exp_id, created_by="write_up", stage="write_up",
                evidence=[{"evidence_kind": "demonstration",
                           "evidence_ref": str((self._paradigm_demonstration() or {}).get("form", "n/a")),
                           "note": "no demonstration result produced by execution"}],
            )
        # a paradigm run whose (secondary) performance eval failed/timed-out: record it as
        # a limitation — the contribution rests on the demonstration, not the model.
        if info.get("eval_error"):
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=("The performance eval did not complete (the contribution rests on the "
                            "discriminating demonstration, not a fitted model)."),
                claim_type="limitation", strength="moderate", status="supported",
                experiment_id=exp_id, created_by="write_up", stage="write_up",
                evidence=[{"evidence_kind": "code", "evidence_ref": "performance-eval",
                           "note": str(info.get("eval_error"))[:200]}],
            )
        # refresh the metric claim to the headline actually reported (OPTIMIZE may have
        # swapped in the alternate result).
        if self._claim_ids.get("metric"):
            await asyncio.to_thread(
                update_claim, self._claim_ids["metric"],
                claim_text=f"Under grouped CV, {design.get('model')} attains {hk}={hv}{usfx}.",
            )
        # the claim table the writer must obey. The ledger is the source of truth: analysis text
        # can suggest interpretations, but only ledger-supported, sufficiently strong claims can
        # be written as findings.
        claims = await asyncio.to_thread(list_claims, self.run_id, exp_id)
        claim_policy = self._writeup_claim_policy(claims)
        claim_table = claim_policy["table"]
        degraded_note = (
            "The headline rests on a DEGRADED (plain-KFold) protocol — do NOT state the grouped-CV "
            "result as a strong/headline generalization claim; describe it as preliminary.\n"
            if protocol_status == "degraded_kfold" else ""
        )
        repro = info.get("reproduction") or {}
        if repro.get("attempted"):
            repro_note = (
                f"REPRODUCTION: an independent re-run ({repro.get('mode', 'rerun')}) "
                f"{'CONFIRMED' if repro.get('reproduced') else 'did NOT confirm'} the headline "
                f"(original={repro.get('original')}, reproduced={repro.get('repro')}, rel Δ {repro.get('delta')}). "
                "Report BOTH numbers; you may call the result reproduced ONLY if it was confirmed.\n"
            )
        else:
            repro_note = "REPRODUCTION: not attempted — do not call the result reproduced.\n"
        drift_note = (
            f"HYPOTHESIS NOT EVALUATED: {info.get('method_drift_msg')}. State the verdict as "
            "'not evaluated' (NOT refuted) and explain why; do not present the hypothesis as "
            "tested-and-failed.\n"
            if info.get("method_drift") else ""
        )
        mode = self._contribution_type()
        paradigm_note = (
            "CONTRIBUTION TYPE = PARADIGM: this study's contribution is a NEW FORMULATION "
            "(a new question / representation / metric), NOT beating a benchmark. Frame the "
            "Results & Discussion around what the new frame reveals or enables; report the SOTA "
            "comparison as CONTEXT only — do NOT present trailing the incumbent benchmark as "
            "failure, and do NOT claim a performance win. The contribution stands on the "
            "formulation claim and its demonstration, not on the headline metric delta.\n"
            if mode == "paradigm"
            else (
                "CONTRIBUTION TYPE = DIAGNOSTIC: this pre-specified study characterizes a narrow "
                "model failure. Do NOT claim methodological novelty, a causal mechanism, universal "
                "generality, or a benchmark win. Lead with the locked statistic, negative control, "
                "uncertainty, reproduction, and external-validation result.\n"
                if mode == "diagnostic" else ""
            )
        )
        prompt = (
            "Write a structured scientific paper in markdown (IMRAD), grounded ONLY in the numbers and "
            "the literature below. Use flowing prose (no bullet points outside the method/baseline list). "
            "Sections, in order:\n"
            "# <concise, specific title>\n"
            "## Abstract — one flowing paragraph (~150 words), no labels.\n"
            "## 1. Introduction — motivation and the research question; cite prior work as [n].\n"
            "## 2. Related Work — summarize the surveyed literature and the gap this study targets; cite [n].\n"
            f"## 3. Method — the {task} on {feat}, and the leakage-aware protocol ({protocol}).\n"
            f"## 4. Results — LEAD with the headline grouped-CV metric ({hk}), then the RepeatedKFold "
            "mean±std, the baseline comparison, and the error breakdown; refer to the parity plot at "
            "`figures/parity.png`. Do NOT present the random-holdout number as the headline.\n"
            f"## 5. Discussion & Limitations — the hypothesis verdict, the comparison to known SOTA ({sota}), "
            "and honest limitations, taken from the analysis.\n\n"
            "Cite ONLY using the [n] keys under CITABLE REFERENCES; never invent a reference or a published "
            "number. Do NOT write a References section — it is appended automatically.\n"
            "In the Method, state the model that ACTUALLY ran (EXECUTED IMPLEMENTATION below); if it differs "
            "from the requested method, say so plainly — never claim a method that was not executed.\n\n"
            "CLAIM LEDGER IS AUTHORITATIVE: the prose must be a view over the claim ledger, not over the "
            "analysis narrative alone. State as research findings ONLY the claims listed under ALLOWED FINDINGS. "
            "If ALLOWED FINDINGS is empty, explicitly say no claim reached finding-grade support. Claims under "
            "REQUIRED LIMITATIONS must appear as limitations. Claims under NOT FINDINGS may be mentioned only "
            "with their ledger status/strength: weak=say preliminary, unverified=say unverified, not_evaluated="
            "say not evaluated, refuted=say refuted. Never convert a NOT FINDING into a positive result.\n"
            f"{degraded_note}{repro_note}{drift_note}{paradigm_note}"
            f"ALLOWED FINDINGS:\n{claim_policy['allowed']}\n\n"
            f"REQUIRED LIMITATIONS:\n{claim_policy['limitations']}\n\n"
            f"NOT FINDINGS / MUST DOWNGRADE:\n{claim_policy['restricted']}\n\n"
            f"FULL CLAIM TABLE:\n{claim_table}\n\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis)}\n"
            f"PLAN: {json.dumps(plan)}\nDESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
            f"REQUESTED METHOD: {requested or 'n/a'}\nEXECUTED IMPLEMENTATION: {impl or 'n/a'}\n"
            f"EVAL PROTOCOL SUMMARY: {eval_summary}\nKNOWN SOTA: {sota}\n"
            f"ANALYSIS (subordinate to the claim ledger): {analysis}\nCRITIC VERDICT: {rpanel.consensus_verdict}\n\n"
            f"CITABLE REFERENCES (cite inline as [n]):\n{cite_list or '(none retrieved)'}"
        )
        dry_ledger = self._dry_writeup_ledger_text(claim_policy)
        cite_a = "[1]" if refs else ""
        cite_b = "[2]" if len(refs) > 1 else cite_a
        body = await reason_stage(
            self.run_id, "write_up", prompt,
            dry_run=self.dry_run,
            dry_text=(
                f"# A leakage-aware study of {task}\n\n"
                f"## Abstract\n\n"
                f"We study {plan.get('objective', task)} using {feat} and a {design.get('model')} model, "
                f"evaluated under a leakage-aware protocol. Prior work {cite_a} reports strong baselines, but "
                f"few studies {cite_b} report a fair grouped-CV number. Under grouped cross-validation the "
                f"model attains {hk}={hv}{usfx} (R²={metrics.get('r2')}), with RepeatedKFold "
                f"MAE={metrics.get('mae_cv_mean')}±{metrics.get('mae_cv_std')}; the random holdout "
                f"(MAE={metrics.get('mae_holdout')}) is optimistic by comparison. Known SOTA: {sota}. The "
                f"critic panel returned {rpanel.consensus_verdict}.\n\n"
                f"## 1. Introduction\n\n"
                f"This study targets {task}. Prior work {cite_a} establishes strong baselines we build on.\n\n"
                f"## 2. Related Work\n\n"
                f"The surveyed literature {cite_a}{(' ' + cite_b) if cite_b != cite_a else ''} shows random "
                f"splits overstate accuracy; a fair grouped-CV comparison remains under-reported — the gap "
                f"this study targets.\n\n"
                f"## 3. Method\n\n"
                f"{feat} feed a {design.get('model')}"
                + (f" (executed implementation: {impl})" if impl else "")
                + f". Evaluation: {protocol}.\n\n"
                f"## 4. Results\n\n"
                f"The headline {hk} is {hv}{usfx} (R²={metrics.get('r2')}); RepeatedKFold "
                f"MAE={metrics.get('mae_cv_mean')}±{metrics.get('mae_cv_std')}; holdout "
                f"MAE={metrics.get('mae_holdout')}, RMSE={metrics.get('rmse_holdout')}. See "
                f"`figures/parity.png`. Protocol: {eval_summary}\n\n"
                f"## 5. Discussion & Limitations\n\n"
                f"{dry_ledger} Known SOTA: {sota}."
            ),
        )
        report = body.rstrip() + (("\n\n" + references_md) if references_md else "")
        adir = run_artifacts_dir(self.run_id)
        path = adir / "report.md"
        path.write_text(report)
        artifacts: list[dict[str, str]] = [{"kind": "report", "uri": str(path)}]
        bib = to_bibtex(self.survey_papers)
        if bib:
            bib_path = adir / "references.bib"
            bib_path.write_text(bib)
            artifacts.append({"kind": "bibliography", "uri": str(bib_path)})
        if exp_id:
            await asyncio.to_thread(record_artifacts, exp_id, artifacts)
        await get_bus().publish(
            make_event("report", run_id=self.run_id, payload={"uri": str(path), "preview": report[:400]})
        )
        # index the headline result so future runs recall what this campaign found
        await self._index(
            "conclusion",
            f"{plan.get('objective', '')}: {design.get('model')} -> {hk} "
            f"{hv}{usfx}, R² {metrics.get('r2')}; verdict "
            f"{rpanel.consensus_verdict}.",
            exp_id, headline=hv, model=design.get("model"),
        )
        # commit the paper + bibliography to the experiment branch + open the PR (IAM)
        await self._iam_finalize(report, bib, rpanel, exp_id)


# --- launch / task tracking ---
_DRIVER_TASKS: set[asyncio.Task] = set()


def launch_driver(run_id: str, dry_run: bool = False) -> asyncio.Task:
    driver = ExperimentDriver(run_id, dry_run=dry_run)
    task = asyncio.create_task(driver.run())
    _DRIVER_TASKS.add(task)
    task.add_done_callback(_DRIVER_TASKS.discard)
    return task
