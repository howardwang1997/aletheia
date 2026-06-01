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
    create_experiment,
    get_run,
    record_artifacts,
    set_experiment_hypothesis,
    set_experiment_repo,
    set_run_status,
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
        self.survey_papers: list[literature.Paper] = []  # citable refs for WRITE_UP
        self.hypothesis: dict[str, Any] = {}  # the hypothesis chosen by IDEATE
        self.profile: DomainProfile | None = None  # the domain's vocabulary (set in _run)

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
        yields an empty briefing, never breaks the loop."""
        if self.profile is None:  # standalone call (tests); _run sets it before SURVEY
            run = await asyncio.to_thread(get_run, self.run_id)
            self.profile = get_domain_plugin((run or {}).get("domain")).profile()
        await self._status("surveying", "researching the literature")
        topic = " ".join(
            str(plan.get(k, "")).strip() for k in ("objective", "direction", "hypothesis")
        ).strip() or (plan.get("domain") or "materials")
        briefing, gaps = "", []
        try:
            if self.dry_run:
                papers = list(self.profile.dry_papers) if self.profile else []
                await asyncio.to_thread(literature.ingest, papers, self.run_id, True)
                briefing = literature.briefing(papers)
                gaps = list(self.profile.dry_gaps) if self.profile else []
                self.survey_papers = papers
            else:
                subqs = await self._decompose(topic)
                findings = await asyncio.gather(*[self._librarian(topic, sq) for sq in subqs])
                findings = [f for f in findings if f and not is_degraded(f)]
                briefing, gaps = await self._synthesize(topic, findings)
                # The librarians ingested papers into the recall store; pull them back
                # as structured, citable references for the WRITE_UP stage.
                self.survey_papers = await asyncio.to_thread(self._recall_papers, topic)
        except Exception as exc:  # noqa: BLE001 - survey is best-effort
            await get_bus().publish(
                make_event("literature", run_id=self.run_id, payload={"error": str(exc)})
            )
        await record_transition(
            self.run_id, exp_id, None, "survey", f"literature surveyed; {len(gaps)} gap(s) found"
        )
        return briefing, gaps

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
            "Call search_literature with focused queries, then give a ≤60-word finding citing the "
            "strongest prior work and any gap you notice.",
            system="You are a meticulous research librarian; ground claims in retrieved papers, never invent.",
            mcp_servers={"research": server}, allowed_tools=list(allow),
            can_use_tool=build_tool_gate(allow, self.run_id), max_turns=5, dry_run=False,
        )

    async def _synthesize(self, topic: str, findings: list[str]) -> tuple[str, list[str]]:
        joined = "\n".join(f"- {f}" for f in findings) or "(no findings retrieved)"
        text = await run_worker(
            self.run_id, "survey:synthesize",
            f"Synthesize a literature briefing for '{topic}' from these findings:\n{joined}\n\n"
            'Return ONLY JSON: {"briefing": "<=150 words on prior work + strongest methods/results", '
            '"gaps": ["concrete unexplored gap", ...]}.',
            system="You synthesize literature reviews and surface concrete research gaps.", dry_run=False,
        )
        obj = _parse_json(text, _JSON, {})
        gaps = [str(g).strip() for g in (obj.get("gaps") or []) if str(g).strip()][:6]
        return str(obj.get("briefing", "")).strip(), gaps

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
        plugin = get_domain_plugin(domain)
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

        # 4) ANALYSIS
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "execution", "analysis", "training complete; analyzing")
        await self._status("analyzing")
        analysis = await self._analyze(design, result, exp_id)

        # 5) review_results gate
        rpanel = await self.gateway.review(
            "results",
            {
                "design": design,
                "metrics": result.get("metrics"),
                "eval_summary": (result.get("info") or {}).get("eval_summary", ""),
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

    async def _campaign_step(
        self, plan: dict, outcomes: list[dict], round_idx: int, max_exps: int
    ) -> dict[str, Any]:
        """Go/no-go after an experiment: read the campaign so far + the open gaps and
        decide whether another experiment is worthwhile — and what it should test.
        This is the campaign's re-ideation step. Returns
        {"continue": bool, "next_hypothesis": {...}, "rationale": str}."""
        await self._status("deciding", "choosing the next experiment")
        trajectory = "\n".join(
            f"- round {o['round']}: '{o['hypothesis']}' -> {o.get('headline_metric')} "
            f"{o.get('headline')} [{o.get('model')}], verdict {o.get('verdict')}"
            for o in outcomes
        )
        gaps = ("OPEN GAPS:\n- " + "\n- ".join(self.survey_gaps) + "\n\n") if self.survey_gaps else ""
        prompt = (
            f"You are steering a research campaign (objective: {plan.get('objective', '')}). "
            f"{round_idx} of at most {max_exps} experiments have run:\n{trajectory}\n\n{gaps}"
            "Decide whether one MORE experiment would be informative (a new angle, an ablation that would "
            "explain the result, or a gap still worth closing) — or whether the program has converged. "
            "If continuing, propose the next testable hypothesis, DISTINCT from the rounds above and still "
            "novel vs the literature. Return ONLY JSON: "
            '{"continue": true|false, "rationale": "...", "next_hypothesis": '
            '{"statement": "...", "rationale": "...", "prediction": "...", "novelty_note": "..."}}.'
        )
        text = await reason_stage(
            self.run_id, "campaign", prompt, dry_run=self.dry_run,
            dry_text=json.dumps({
                "continue": True,
                "rationale": "an ablation / extension would sharpen the result",
                "next_hypothesis": self.profile.dry_next_hypothesis if self.profile else {},
            }),
        )
        decision = _parse_json(text, _JSON, {"continue": False})
        await self._index(
            "design_rationale",
            f"campaign go/no-go after round {round_idx}: continue={decision.get('continue')} — "
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
            f"- round {o['round']} ({o.get('model')}): '{o['hypothesis']}' -> {hk} "
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
        prompt = (
            f"Turn this research plan into a concrete experiment design for a {task} on "
            f"{feature_desc}.\n\n"
            f"PLAN:\n{json.dumps(plan, indent=2)}\n\nDATA:\n{json.dumps(data_spec, indent=2)}\n\n"
            + (self.survey_brief + "\n\n" if self.survey_brief else "")
            + (briefing + "\n\n" if briefing else "")
            + "Return ONLY JSON with keys: model ('random_forest' or 'gradient_boosting'), "
            "model_params (object), test_size (0-1), random_state (int). Choose sensible values."
        )
        text = await reason_stage(
            self.run_id, "experiment_design", prompt,
            dry_run=self.dry_run,
            dry_text=f"[dry-run] Design: {fallback['model']} on {feature_desc}, 80/20 split.",
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
        cur_mae = result.get("metrics", {}).get("mae", float("inf"))
        alt_mae = alt_result.get("metrics", {}).get("mae", float("inf"))
        if alt_mae < cur_mae:
            await get_bus().publish(
                make_event(
                    "optimize", run_id=self.run_id,
                    payload={"kept": alt.get("model"), "mae": alt_mae, "previous_mae": cur_mae},
                )
            )
            return alt, alt_result
        await get_bus().publish(
            make_event(
                "optimize", run_id=self.run_id,
                payload={"kept": design.get("model"), "mae": cur_mae, "alt_mae": alt_mae},
            )
        )
        return design, result

    async def _write_up(self, plan, design, result, analysis, rpanel, exp_id) -> None:
        metrics = result.get("metrics", {})
        eval_summary = (result.get("info") or {}).get("eval_summary", "")
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
            "number. Do NOT write a References section — it is appended automatically.\n\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis)}\n"
            f"PLAN: {json.dumps(plan)}\nDESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
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
                f"{feat} feed a {design.get('model')}. Evaluation: {protocol}.\n\n"
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
