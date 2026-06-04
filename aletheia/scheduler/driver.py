"""The autonomous lifecycle driver.

Launched from a finalized, data-ready plan, it walks EXPERIMENT_DESIGN -> ARCHIVE
as a background task with zero per-action approvals: Opus reasons at each reasoning
stage, the critic gates design + results, and the local backend runs training in a
subprocess. Everything is recorded to the ledger and streamed to the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from aletheia.coder.sandbox import check_code, smoke_test_solution
from aletheia.coder.worker import CANNED_SOLUTION, CODER_SYSTEM, coder_prompt, extract_code
from aletheia.compute.base import JobSpec
from aletheia.compute.factory import get_compute_backend
from aletheia.compute.mcp_tools import resolve_data_spec
from aletheia.config import get_settings
from aletheia.critics.gateway import CriticGateway
from aletheia.domains.base import DomainProfile
from aletheia.domains.registry import get_domain_plugin
from aletheia.events.bus import get_bus, make_event
from aletheia.iam import policy
from aletheia.iam.github_app import GitHubBackend, RepoResult, get_github_backend
from aletheia.memory.service import (
    create_claim,
    create_experiment,
    get_run,
    list_claims,
    record_artifacts,
    record_literature_finding,
    record_metrics,
    record_scorecard,
    record_sota_result,
    set_experiment_hypothesis,
    set_experiment_repo,
    set_run_status,
    update_claim,
)
from aletheia.memory.vector import format_briefing, index_chunk, recall
from aletheia.orchestrator.gate import build_tool_gate
from aletheia.orchestrator.tools import build_search_literature_tool
from aletheia.research import literature
from aletheia.research.citations import numbered_references, to_bibtex
from aletheia.notify.feishu import notify_feishu
from aletheia.orchestrator.reasoner import reason_stage
from aletheia.orchestrator.worker import is_degraded, run_worker
from aletheia.paths import run_artifacts_dir
from aletheia.scheduler.budget import BudgetPaused, BudgetTracker
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
    def __init__(self, run_id: str, dry_run: bool = False) -> None:
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
        self.hypothesis: dict[str, Any] = {}  # the hypothesis chosen by IDEATE
        self.profile: DomainProfile | None = None  # the domain's vocabulary (set in _run)
        self.plugin: Any = None  # the resolved DomainPlugin (set in _run)
        self.domain: str | None = None  # the run's domain name (materials | molecules | …)
        self._claim_ids: dict[str, str] = {}  # role -> claim id, reset per experiment round
        self._last_scores: dict[str, float] = {}  # latest hypothesis scorecard scores (campaign EIG)

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
        approve. No demonstration executed → `speculative` (it stays a proposal)."""
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
            return "moderate" if has_sota else "weak"  # curated string, no structured row yet (Phase H)
        if kind == "mechanism":
            return "moderate" if gate_passed else "weak"
        if kind == "novelty":
            return "speculative"  # no structured literature/novelty check in v1
        if kind == "formulation":
            if demonstration_holds is None:
                return "speculative"  # no discriminating demonstration executed -> a proposal
            if not demonstration_holds or gate_passed is False:
                return "weak"  # demonstration did not hold, or the gate did not pass
            if reproduced and gate_verdict == "approve":
                return "strong"  # held + clean approve + STABLE across an independent re-run
            return "moderate"  # held + approved (approve_with_changes, or not reproduced)
        return "weak"

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
            self.profile = get_domain_plugin(self.domain).profile()
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
        from claude_agent_sdk import create_sdk_mcp_server

        server = create_sdk_mcp_server(
            name="research", version="0.0.1", tools=[build_search_literature_tool(self.run_id)]
        )
        allow = {"mcp__research__search_literature"}
        return await run_worker(
            self.run_id, f"survey:{subq[:24]}",
            f"Research this sub-question for the direction '{topic}':\n{subq}\n\n"
            "Call search_literature with focused queries, then give a ≤70-word finding that names the "
            "strongest / state-of-the-art METHODS the field uses, the prior work they come from, and any "
            "gap you notice.",
            system="You are a meticulous research librarian; ground claims in retrieved papers, never invent.",
            mcp_servers={"research": server}, allowed_tools=list(allow),
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

    async def _ideate(self, plan: dict, exp_id: str | None) -> dict:
        """IDEATE: from the survey + gaps, generate testable hypotheses and pick the
        most novel + feasible one. Persist it to the experiment. Mirrors the
        hypothesis-generation workflow."""
        await self._status("ideating", "forming a hypothesis")
        prompt = (
            f"PLAN:\n{json.dumps(plan, indent=2)}\n\n"
            + (self.survey_brief + "\n\n" if self.survey_brief else "")
            + ("RESEARCH GAPS:\n- " + "\n- ".join(self.survey_gaps) + "\n\n" if self.survey_gaps else "")
            + "Propose 2-3 TESTABLE hypotheses for this direction (each: statement, one-line rationale, "
            "concrete prediction). Then pick the ONE that is most NOVEL given the literature/gaps above "
            "and feasible on CPU within budget.\n"
            "Also classify the CONTRIBUTION TYPE of the chosen hypothesis:\n"
            "- \"performance\": the goal is to beat/match a benchmark on an established metric.\n"
            "- \"paradigm\": the goal is to change the QUESTION — a new problem formulation, "
            "representation, or evaluation metric — where beating the incumbent benchmark is NOT the "
            "point. Choose this ONLY if you can name a concrete discriminating demonstration the new "
            "frame would make (a case the incumbent provably cannot handle/distinguish).\n"
            "If (and ONLY if) paradigm, also give a \"demonstration\": "
            '{"form": "discriminating_instance | enablement | unification | impossibility", '
            '"claim": "the concrete, checkable case the incumbent frame provably cannot '
            'handle/distinguish"}. A paradigm hypothesis WITHOUT a concrete demonstration will be '
            "treated as a performance contribution.\n"
            "Return ONLY JSON for the chosen one: "
            '{"statement": "...", "rationale": "...", "prediction": "...", "novelty_note": "...", '
            '"contribution_type": "performance" | "paradigm", "demonstration": {"form": "...", "claim": "..."}}.'
        )
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
        self._claim_ids["novelty"] = await asyncio.to_thread(
            create_claim, self.run_id,
            claim_text=f"This direction is novel: {note or statement}",
            claim_type="novelty", strength=self._claim_strength("novelty"),
            status="unverified", experiment_id=exp_id, created_by="ideate", stage="ideate",
            evidence=evidence,
        )
        # a PARADIGM hypothesis gets a first-class `formulation` claim (the contribution is
        # the new frame, not a benchmark number). It stays `proposed`/`speculative` until a
        # reproducible discriminating demonstration grounds it (see PARADIGM_MODE_DESIGN.md);
        # this is the honest vocabulary so the contribution is not flattened into a metric.
        if self._contribution_type() == "paradigm":
            demo = self._paradigm_demonstration() or {}
            form_evidence = evidence + [{
                "evidence_kind": "demonstration",
                "evidence_ref": str(demo.get("form", "discriminating")),
                "note": str(demo.get("claim", "")),
            }]
            self._claim_ids["formulation"] = await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=f"New formulation proposed ({demo.get('form', 'discriminating')}): {note or statement}",
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
        """`performance` (beat a benchmark) or `paradigm` (change the question). Selects
        the results-gate review STANDARD and the write-up framing (see
        docs/PARADIGM_MODE_DESIGN.md). A `paradigm` claim is honored ONLY if it names a
        concrete discriminating demonstration — the fakeability guardrail; otherwise it
        falls back to the conservative `performance`."""
        ct = str(self.hypothesis.get("contribution_type", "")).strip().lower()
        if ct == "paradigm" and self._paradigm_demonstration() is not None:
            return "paradigm"
        return "performance"

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
        while True:
            content = {
                "hypothesis": self.hypothesis, "gaps": self.survey_gaps, "literature": self.survey_brief,
            }
            panel = await self.gateway.review(
                "direction", content, exp_id or self.run_id, run_id=self.run_id, dry_run=self.dry_run
            )
            if panel.gate_passed:
                return True
            n = self.guard.bump("direction")
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
        novelty + evaluation-clarity floors. Low on either → block."""
        s = get_settings()
        nov = float(scores.get("novelty", 0.0))
        clar = float(scores.get("evaluation_clarity", 0.0))
        if nov < s.hypothesis_min_novelty:
            return False, f"novelty {nov:.2f} below floor {s.hypothesis_min_novelty}"
        if clar < s.hypothesis_min_eval_clarity:
            return False, f"evaluation clarity {clar:.2f} below floor {s.hypothesis_min_eval_clarity}"
        return True, "scorecard cleared the novelty + evaluation floors"

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
        # fitted model — gate on the demonstration; a failed/timed-out performance eval is
        # a limitation (recorded in write-up), NOT a blocker.
        if self._contribution_type() == "paradigm":
            if not info.get("demonstration"):
                await self._block_run(
                    exp_id, "paradigm run produced no discriminating demonstration; "
                    "pausing before analysis",
                )
                return False
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
        hk = self.profile.headline_metric if self.profile else "mae"
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
            # a demonstration is REPRODUCED iff it still HOLDS on the seed-perturbed re-run
            # AND its statistic stays the same ORDER of magnitude (within 2x) — the right
            # check for an impossibility-style ratio (qualitatively robust, not bit-identical;
            # the strict metric tolerance would wrongly fail an honestly-noisy ratio).
            s1, s2 = orig_demo.get("statistic"), (repro_demo or {}).get("statistic")
            demo_repro = bool(orig_demo.get("holds")) and bool((repro_demo or {}).get("holds"))
            if demo_repro and s1 is not None and s2 is not None:
                lo, hi = sorted((abs(float(s1)), abs(float(s2))))
                demo_repro = hi <= 1e-9 or (lo / hi) >= 0.5  # within ~2x
            payload["demonstration_reproduced"] = demo_repro
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
        if self._claim_ids.get("metric"):
            await asyncio.to_thread(
                update_claim, self._claim_ids["metric"],
                strength=self._claim_strength(
                    "metric", protocol_status=protocol_status, gate_passed=passed,
                    gate_verdict=verdict, reproduced=reproduced,
                ),
                status=status,
            )
        if self._claim_ids.get("mechanism"):
            mech_status = "not_evaluated" if method_not_instantiated else status
            await asyncio.to_thread(
                update_claim, self._claim_ids["mechanism"],
                strength="weak" if method_not_instantiated else self._claim_strength("mechanism", gate_passed=passed),
                status=mech_status,
            )
        # a PARADIGM contribution's formulation claim is grounded by its reproducible
        # discriminating demonstration (NOT by SOTA-delta). No demonstration executed ->
        # `not_evaluated`/`speculative` (it stays an honest proposal, never a finding).
        if self._claim_ids.get("formulation"):
            demo = demonstration if isinstance(demonstration, dict) else {}
            holds = demo.get("holds")  # True | False | None(not executed)
            demo_repro = repro.get("demonstration_reproduced") if repro.get("attempted") else None
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
            await asyncio.to_thread(
                update_claim, self._claim_ids["formulation"],
                strength=self._claim_strength(
                    "formulation", gate_passed=passed, gate_verdict=verdict,
                    reproduced=demo_repro, demonstration_holds=holds,
                ),
                status=form_status,
            )
        # a reproducibility claim: did an independent re-run confirm the headline?
        if repro.get("attempted"):
            confirmed = bool(repro.get("reproduced"))
            await asyncio.to_thread(
                create_claim, self.run_id,
                claim_text=(
                    f"An independent re-run ({repro.get('mode', 'rerun')}) "
                    f"{'confirmed' if confirmed else 'did NOT confirm'} the headline "
                    f"{repro.get('metric', '')} (original={repro.get('original')}, "
                    f"reproduced={repro.get('repro')}, rel Δ {repro.get('delta')})."
                ),
                claim_type="reproducibility",
                strength="moderate" if confirmed else "weak",
                status="supported" if confirmed else "refuted",
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

    # --- entry point ---
    async def run(self) -> None:
        try:
            await self._run()
        except BudgetPaused:
            return  # already paused + notified; a clean stop, not a failure
        except Exception as exc:  # noqa: BLE001
            await get_bus().publish(
                make_event("error", run_id=self.run_id, payload={"error": str(exc)})
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
        self.profile = plugin.profile()  # the domain's vocabulary for every stage
        data_spec = resolve_data_spec(self.run_id)
        self.budget = await asyncio.to_thread(BudgetTracker, self.run_id)

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
        max_exps = max(1, get_settings().max_experiments_per_campaign)
        outcomes: list[dict[str, Any]] = []
        cur_exp_id = exp_id
        next_hint: dict[str, Any] | None = None
        round_idx = 1
        while True:
            # per-round guard isolation so design/direction revision counters don't
            # bleed across experiments
            self.guard = LoopGuard(get_settings().critics.consensus.max_design_iterations)
            # fresh claim set per round — reset HERE (before IDEATE) so the novelty +
            # formulation claims ideate creates survive into _finalize_claims.
            self._claim_ids = {}
            await get_bus().publish(
                make_event("experiment", run_id=self.run_id,
                           payload={"round": round_idx, "exp_id": cur_exp_id, "of": max_exps})
            )
            # IDEATE this round's hypothesis: round 1 from the survey; later rounds
            # adopt the go/no-go step's proposed next hypothesis.
            if round_idx == 1:
                await self._ideate(plan, cur_exp_id)
            else:
                await self._adopt_hypothesis(next_hint or {}, cur_exp_id)
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

            if round_idx >= max_exps:
                break
            decision = await self._campaign_step(plan, outcomes, round_idx, max_exps)
            if not decision.get("continue"):
                await get_bus().publish(
                    make_event("campaign", run_id=self.run_id,
                               payload={"decision": "stop", "rationale": decision.get("rationale", "")})
                )
                break
            next_hint = decision.get("next_hypothesis") or {}
            cur_exp_id = await asyncio.to_thread(
                create_experiment, self.run_id, plan, parent_experiment_id=cur_exp_id
            )
            round_idx += 1

        # campaign-level synthesis across the experiments (only when >1 ran)
        if len(outcomes) > 1:
            await self._campaign_synthesis(plan, outcomes, exp_id)

        # ARCHIVE -------------------------------------------------------------
        # A run whose concluding experiment's results gate REJECTED is distinguished
        # from a clean completion: it ran to the end and is archived, but peer review
        # did not endorse the result. The claims already carry the refutation; this
        # makes the run-level outcome legible too.
        last_exp_id = outcomes[-1]["exp_id"] if outcomes else cur_exp_id
        results_rejected = bool(outcomes) and outcomes[-1].get("verdict") == "reject"
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
                    "experiments": len(outcomes),
                    "metrics": outcomes[-1].get("metrics") if outcomes else None,
                },
            )
        )
        detail = (
            f"campaign complete ({len(outcomes)} experiment(s)) — results rejected by peer review"
            if results_rejected else f"campaign complete ({len(outcomes)} experiment(s))"
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
        else:
            solution_code = await self._code(design, data_spec, exp_id)
            if solution_code:
                design["solution_code"] = solution_code

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

        # 5) review_results gate — the critics see the CLAIMS + evidence (proposed in
        # analysis) + the protocol status + the reproduction, so they review evidence.
        info = result.get("info") or {}
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
        await record_transition(
            self.run_id, exp_id, "analysis", "optimize",
            f"results reviewed: {rpanel.consensus_verdict} (gate_passed={rpanel.gate_passed})",
            actor="critic",
        )
        # finalize the proposed claims now that the gate has spoken (harness-owned
        # strength: a metric claim only reaches `strong` with grouped CV + a clean
        # approve + a CONFIRMED reproduction).
        await self._finalize_claims(
            info.get("protocol_status"), rpanel, info.get("reproduction") or {}, exp_id,
            ablation=info.get("ablation"),
            method_not_instantiated=bool(info.get("method_drift")),
            demonstration=info.get("demonstration"),
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
        hk = self.profile.headline_metric if self.profile else "mae"
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
        trajectory = "\n".join(
            f"- round {o['round']} [{o.get('experiment_type') or '?'}]: '{o['hypothesis']}' -> "
            f"{o.get('headline_metric')} {o.get('headline')} [{o.get('model')}], verdict {o.get('verdict')}"
            for o in outcomes
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
            f"{gaps}{budget_line}\n"
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
        # deterministic selection: the best candidate that clears the EIG floor wins;
        # if none does, the program has converged. Also honor the backward-looking
        # floor on the just-run hypothesis (a low-gain last round signals convergence).
        viable = [c for c in candidates if float(c.get("expected_information_gain", 0.0) or 0.0) >= floor]
        last_eig = float(self._last_scores.get("expected_information_gain", 1.0))
        if not viable or last_eig < floor:
            reason = (
                f"no proposed experiment clears the EIG floor {floor}"
                if not viable else
                f"last experiment's expected information gain {last_eig:.2f} below floor {floor}"
            )
            decision = {"continue": False, "rationale": f"{reason}; program converged", "candidates": candidates}
        else:
            best = max(viable, key=lambda c: float(c.get("expected_information_gain", 0.0) or 0.0))
            nxt = dict(best.get("hypothesis") or {})
            nxt["experiment_type"] = best.get("experiment_type")
            nxt["open_question"] = best.get("open_question")
            decision = {
                "continue": True,
                "rationale": (
                    f"chose a {best.get('experiment_type')} (EIG "
                    f"{float(best.get('expected_information_gain', 0.0)):.2f}) to answer: "
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
                     "eig": c.get("expected_information_gain")}
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

    async def _campaign_synthesis(self, plan: dict, outcomes: list[dict], first_exp_id: str | None) -> None:
        """Write a campaign-level summary tying the experiments together: the
        trajectory, which experiment won, what the program learned, open gaps."""
        await self._status("synthesizing", "summarizing the campaign")
        hk = self.profile.headline_metric if self.profile else "mae"
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
        best = (
            (max if self._maximize() else min)(scored, key=lambda o: o["headline"])
            if scored else outcomes[-1]
        )
        prompt = (
            f"Summarize this research campaign (objective: {plan.get('objective', '')}) as a short markdown "
            f"brief. Experiments:\n{rows}\n\nThe best {hk} was {best.get('headline')} in round "
            f"{best.get('round')}. Write: the trajectory (how each experiment informed the next), which "
            "experiment won and why, what the program learned, and the most important still-open gap. "
            f"Ground every claim in the {hk} numbers above; do not invent results."
        )
        summary = await reason_stage(
            self.run_id, "campaign", prompt, dry_run=self.dry_run,
            dry_text=(
                f"# Campaign Summary\n\n**Objective:** {plan.get('objective', 'n/a')}\n\n"
                f"## Trajectory\n\n{rows}\n\n"
                f"## Outcome\n\nThe best {hk} was {best.get('headline')}{usfx} in "
                f"round {best.get('round')} ({best.get('model')}). Across {len(outcomes)} experiments the "
                f"program refined its hypothesis from the survey gaps toward the most informative test.\n\n"
                f"## Open gaps\n\n- " + "\n- ".join(self.survey_gaps or ["(none recorded)"])
            ),
        )
        path = run_artifacts_dir(self.run_id) / "campaign.md"
        path.write_text(summary)
        if first_exp_id:
            await asyncio.to_thread(
                record_artifacts, first_exp_id, [{"kind": "campaign", "uri": str(path)}]
            )
        await self._index(
            "conclusion",
            f"campaign ({len(outcomes)} experiments) best {hk} {best.get('headline')} "
            f"in round {best.get('round')} ({best.get('model')}).",
            first_exp_id, headline=best.get("headline"), experiments=len(outcomes),
        )
        await get_bus().publish(
            make_event("campaign_finished", run_id=self.run_id, payload={
                "uri": str(path), "experiments": len(outcomes),
                "best_round": best.get("round"), "best_headline": best.get("headline"),
            })
        )

    # --- stage implementations ---
    async def _design(self, plan, data_spec, plugin, exp_id) -> dict[str, Any]:
        fallback = dict(plugin.baselines()[0])
        fallback.setdefault("test_size", 0.2)
        fallback.setdefault("random_state", 42)
        if data_spec.get("target_column"):
            fallback["target_column"] = data_spec["target_column"]
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
        while True:
            content = (
                {"design": design, "literature": self.survey_brief} if self.survey_brief else design
            )
            panel = await self.gateway.review(
                "design", content, exp_id or self.run_id, run_id=self.run_id, dry_run=self.dry_run
            )
            if panel.gate_passed:
                return design
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
        paradigm = self._contribution_type() == "paradigm"
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
                                    "holds": demo.get("holds"), "statistic": demo.get("statistic")})
            )
        return demo

    _DEFAULT_RAG_PROMPT = (
        "Answer the question using ONLY the context. Be concise (a short phrase). "
        "If the context does not contain the answer, say 'unknown'.\n\n"
        "CONTEXT:\n{context}\n\nQUESTION: {question}\nANSWER:"
    )

    async def _rag_generate(self, question: str, contexts: list[str], prompt: str) -> str:
        """Generate one answer HOST-SIDE. Real run → a Claude call (trusted; the no-network
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
        model = get_settings().claude_model
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

    async def _write_up(self, plan, design, result, analysis, rpanel, exp_id) -> None:
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
        # SOTA claim now stands on STRUCTURED rows (Phase H): find the best comparable
        # published number on the headline metric family and compute a real win/loss.
        best_sota, comparable, beat = self._compare_to_sota(hk, hv)
        if not comparable:
            sota_text = f"No structured SOTA row comparable on {hk}; known reference: {sota}."
            sota_strength = "weak"
        else:
            rel = "beats" if beat else "does not beat"
            sota_text = (
                f"The headline {hk} ({hv}{usfx}) {rel} the best comparable published result "
                f"({best_sota.get('method')} {best_sota.get('score')} on {best_sota.get('dataset')})."
            )
            # only a grouped + gate-passed WIN earns 'strong'; otherwise comparable -> moderate
            sota_strength = (
                "strong" if (beat and protocol_status == "grouped" and rpanel.gate_passed) else "moderate"
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
            status=("supported" if comparable else "unverified"),
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
        if self._contribution_type() == "paradigm" and not info.get("demonstration"):
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
        # the claim table the writer must obey (only supported & ≥moderate claims may
        # be stated strongly; speculative / unverified / weak ones must be labeled).
        claims = await asyncio.to_thread(list_claims, self.run_id, exp_id)
        claim_table = "\n".join(
            f"- [{c['claim_type']}] strength={c['strength']} status={c['status']}: {c['claim_text']}"
            for c in claims
        ) or "- (no claims recorded)"
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
        paradigm_note = (
            "CONTRIBUTION TYPE = PARADIGM: this study's contribution is a NEW FORMULATION "
            "(a new question / representation / metric), NOT beating a benchmark. Frame the "
            "Results & Discussion around what the new frame reveals or enables; report the SOTA "
            "comparison as CONTEXT only — do NOT present trailing the incumbent benchmark as "
            "failure, and do NOT claim a performance win. The contribution stands on the "
            "formulation claim and its demonstration, not on the headline metric delta.\n"
            if self._contribution_type() == "paradigm" else ""
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
            "CLAIM RULES (obey strictly — the report must not imply more than the evidence): only claims with "
            "status=supported AND strength in {moderate, strong} may be stated as findings; mark weak claims as "
            "preliminary, and speculative/unverified claims (e.g. novelty) explicitly as such (e.g. 'we did not "
            "verify novelty against a structured literature search'). Do not assert SOTA superiority beyond the "
            "curated KNOWN SOTA string. A claim with status=not_evaluated must be reported as not evaluated, "
            "never as a finding or a refutation.\n"
            f"{degraded_note}{repro_note}{drift_note}{paradigm_note}"
            f"CLAIM TABLE:\n{claim_table}\n\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis)}\n"
            f"PLAN: {json.dumps(plan)}\nDESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
            f"REQUESTED METHOD: {requested or 'n/a'}\nEXECUTED IMPLEMENTATION: {impl or 'n/a'}\n"
            f"EVAL PROTOCOL SUMMARY: {eval_summary}\nKNOWN SOTA: {sota}\n"
            f"ANALYSIS: {analysis}\nCRITIC VERDICT: {rpanel.consensus_verdict}\n\n"
            f"CITABLE REFERENCES (cite inline as [n]):\n{cite_list or '(none retrieved)'}"
        )
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
                f"{analysis or 'The pipeline ran end-to-end under a leakage-aware protocol.'} Known SOTA: {sota}."
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
