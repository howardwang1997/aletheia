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

from aletheia.coder.sandbox import check_code
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
    ) -> str:
        """Deterministic, harness-owned claim strength — never the LLM's self-rating.
        Keeps a report from implying stronger evidence than the ledger holds.

        ``reproduced``: True = an independent re-run confirmed the headline; False =
        it contradicted it; None = not attempted. A metric claim reaches `strong` ONLY
        when a reproduction confirmed it."""
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
        return "weak"

    def _compare_to_sota(
        self, headline_metric: str, headline_value: float | None
    ) -> tuple[dict | None, list[dict], bool]:
        """Compare the headline against the structured SOTA rows on the same metric
        FAMILY (mae/rmse → lower is better; r2 → higher). Returns
        (best_comparable_row, all_comparable_rows, did_we_beat_it)."""
        fam = headline_metric.split("_")[0].lower()  # "mae_lcso" -> "mae"
        comparable = [
            r for r in self.survey_sota
            if str(r.get("metric", "")).lower().startswith(fam) and r.get("score") is not None
        ]
        if not comparable or headline_value is None:
            return None, comparable, False
        if fam == "r2":
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
            "and feasible on CPU within budget. Return ONLY JSON for the chosen one: "
            '{"statement": "...", "rationale": "...", "prediction": "...", "novelty_note": "..."}.'
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
        if statement and exp_id:
            await asyncio.to_thread(set_experiment_hypothesis, exp_id, statement)
        await self._index("hypothesis", statement, exp_id)
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
        await record_transition(self.run_id, exp_id, "survey", "ideate", f"hypothesis: {statement[:80]}")
        return self.hypothesis

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
                'hypothesis. Return ONLY JSON: {"statement","rationale","prediction","novelty_note"}.'
            )
            text = await reason_stage(
                self.run_id, "ideate", prompt, dry_run=self.dry_run,
                dry_text='{"statement": "revised hypothesis", "rationale": "r", "prediction": "p", "novelty_note": "n"}',
            )
            self.hypothesis = _parse_json(text, _JSON, self.hypothesis)
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
            'evaluation. Return ONLY JSON: {"statement","rationale","prediction","novelty_note"}.'
        )
        text = await reason_stage(
            self.run_id, "ideate", prompt, dry_run=self.dry_run,
            dry_text='{"statement": "revised, more novel hypothesis", "rationale": "r", "prediction": "p", "novelty_note": "n"}',
        )
        self.hypothesis = _parse_json(text, _JSON, self.hypothesis)
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
        missing = {"eval", "model"} - kinds
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
            repro_result = await self._execute(repro_design, data_spec, domain, None)
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
        await get_bus().publish(make_event("reproduction", run_id=self.run_id, payload=payload))
        await record_transition(
            self.run_id, exp_id, "analysis", "analysis",
            f"reproduction ({hk}): original={original} repro={repro_v} -> "
            f"{'confirmed' if reproduced else 'NOT confirmed'} (rel Δ {delta})",
        )
        return payload

    async def _finalize_claims(
        self, protocol_status: str | None, rpanel, reproduction: dict | None = None,
        exp_id: str | None = None,
    ) -> None:
        """After the results gate, set the final strength + status of the proposed
        metric/mechanism claims from the (harness-owned) evidence rule, and record a
        reproducibility claim from the independent re-run."""
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
            await asyncio.to_thread(
                update_claim, self._claim_ids["mechanism"],
                strength=self._claim_strength("mechanism", gate_passed=passed),
                status=status,
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
        last_exp_id = outcomes[-1]["exp_id"] if outcomes else cur_exp_id
        await record_transition(self.run_id, last_exp_id, "write_up", "archive", "campaign complete")
        await asyncio.to_thread(set_run_status, self.run_id, "completed")
        await get_bus().publish(
            make_event(
                "run_finished",
                run_id=self.run_id,
                payload={
                    "status": "completed",
                    "experiments": len(outcomes),
                    "metrics": outcomes[-1].get("metrics") if outcomes else None,
                },
            )
        )
        await self._status("archived", f"campaign complete ({len(outcomes)} experiment(s))")

    async def _run_experiment(
        self, plan, data_spec, plugin, domain, exp_id, round_idx
    ) -> dict[str, Any] | None:
        """Run ONE experiment end-to-end (DESIGN → WRITE_UP) for the given experiment
        id. Returns a compact outcome for the campaign, or None if a hard gate
        rejected past its loop limit (the run is already paused + escalated)."""
        self._claim_ids = {}  # fresh claim set per experiment round
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

        # 2b) CODE: the coder authors the model (gated); falls back to the design model
        solution_code = await self._code(design, data_spec, exp_id)
        if solution_code:
            design["solution_code"] = solution_code

        # 3) EXECUTION
        await record_transition(self.run_id, exp_id, "experiment_design", "execution", "design approved; running")
        await self._status("executing")
        result = await self._execute(design, data_spec, domain, exp_id)

        # 3b) fail-closed: a real run must produce verifiable artifacts; a degraded
        # (KFold-fallback) headline is surfaced, not hidden.
        if not await self._post_execution_guards(result, exp_id):
            return None

        # 3c) REPRODUCTION: an independent re-run (locked code, new seed) — a metric
        # claim can only reach `strong` if this confirms the headline within tolerance.
        result.setdefault("info", {})["reproduction"] = await self._reproduce(
            design, data_spec, domain, result, exp_id
        )

        # 4) ANALYSIS
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "execution", "analysis", "training complete; analyzing")
        await self._status("analyzing")
        analysis = await self._analyze(design, result, exp_id)

        # 5) review_results gate — the critics see the CLAIMS + evidence (proposed in
        # analysis) + the protocol status + the reproduction, so they review evidence.
        info = result.get("info") or {}
        rpanel = await self.gateway.review(
            "results",
            {
                "design": design,
                "metrics": result.get("metrics"),
                "eval_summary": info.get("eval_summary", ""),
                "protocol_status": info.get("protocol_status"),
                "degraded": info.get("protocol_status") == "degraded_kfold",
                "reproduction": info.get("reproduction"),
                "claims": await asyncio.to_thread(list_claims, self.run_id, exp_id),
                "analysis": analysis,
                # the coder's actual model code, so an adversarial reviewer can check
                # for leakage / metric-gaming, not just trust the reported numbers
                "solution_code": design.get("solution_code", ""),
            },
            exp_id or self.run_id,
            run_id=self.run_id,
            dry_run=self.dry_run,
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
            info.get("protocol_status"), rpanel, info.get("reproduction") or {}, exp_id
        )

        # 6) OPTIMIZE (<=1 iteration)
        await self._status("optimizing")
        best_design, best_result = await self._optimize(design, result, data_spec, domain, exp_id, plugin)

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
        best = min(
            (o for o in outcomes if o.get("headline") is not None),
            key=lambda o: o["headline"],
            default=outcomes[-1],
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

    async def _optimize(self, design, result, data_spec, domain, exp_id, plugin):
        # try the alternate baseline model once; keep whichever has lower MAE
        alt = dict(plugin.baselines()[-1])
        alt.setdefault("test_size", design.get("test_size", 0.2))
        alt.setdefault("random_state", design.get("random_state", 42))
        if data_spec.get("target_column"):
            alt["target_column"] = data_spec["target_column"]
        if alt.get("model") == design.get("model"):
            return design, result  # nothing distinct to try
        alt_result = await self._execute(alt, data_spec, domain, exp_id)
        # compare on the domain's declared headline metric (lower is better for the
        # error metrics both domains lead with), not always "mae".
        hk = self.profile.headline_metric if self.profile else "mae"
        cur_score = result.get("metrics", {}).get(hk, float("inf"))
        alt_score = alt_result.get("metrics", {}).get(hk, float("inf"))
        if alt_score < cur_score:
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
            "curated KNOWN SOTA string.\n"
            f"{degraded_note}{repro_note}"
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
