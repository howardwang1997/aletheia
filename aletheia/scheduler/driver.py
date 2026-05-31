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
from aletheia.domains.registry import get_domain_plugin
from aletheia.events.bus import get_bus, make_event
from aletheia.iam import policy
from aletheia.iam.github_app import GitHubBackend, RepoResult, get_github_backend
from aletheia.memory.service import (
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
        self.hypothesis: dict[str, Any] = {}  # the hypothesis chosen by IDEATE

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
        await self._status("surveying", "researching the literature")
        topic = " ".join(
            str(plan.get(k, "")).strip() for k in ("objective", "direction", "hypothesis")
        ).strip() or (plan.get("domain") or "materials")
        briefing, gaps = "", []
        try:
            if self.dry_run:
                papers = [
                    literature.Paper(
                        title="Magpie composition features for band-gap regression",
                        abstract="Composition-only descriptors predict band gaps; tree models are strong baselines.",
                        year=2024, source="arxiv",
                    ),
                    literature.Paper(
                        title="Leakage-aware evaluation for materials ML",
                        abstract="Random splits overstate performance; leave-chemical-system-out is the honest metric.",
                        year=2023, citations=31, source="openalex",
                    ),
                ]
                await asyncio.to_thread(literature.ingest, papers, self.run_id, True)
                briefing = literature.briefing(papers)
                gaps = [
                    "Few works report a fair leave-chemical-system-out baseline.",
                    "Composition-only vs structure-aware features are rarely compared honestly.",
                ]
            else:
                subqs = await self._decompose(topic)
                findings = await asyncio.gather(*[self._librarian(topic, sq) for sq in subqs])
                findings = [f for f in findings if f and not is_degraded(f)]
                briefing, gaps = await self._synthesize(topic, findings)
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
            dry_text=(
                '{"statement": "Magpie features + gradient boosting beat a leakage-aware LCSO '
                'baseline for experimental band-gap regression", "rationale": "GBM captures nonlinear '
                'composition-property relations", "prediction": "LCSO MAE below the random-forest baseline", '
                '"novelty_note": "Few prior works report a fair LCSO comparison"}'
            ),
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
            self.run_id, "coder", coder_prompt(design, data_spec),
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

    async def _iam_finalize(self, report: str, rpanel, exp_id: str | None) -> None:
        """Commit the experiment report to its branch and open a PR carrying the
        critic verdict (PR-per-experiment). Best-effort."""
        if not (self.repo and self.branch and exp_id):
            return
        try:
            await asyncio.to_thread(
                self.gh.put_file, self.repo.full_name, "report.md", report,
                message=f"experiment {exp_id}: report", branch=self.branch,
            )
            body = (
                f"Autonomous experiment `{exp_id}`.\n\n"
                f"**Critic consensus:** {rpanel.consensus_verdict} "
                f"(gate {'passed' if rpanel.gate_passed else 'failed'}).\n\n"
                "Report committed on this branch."
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

        # 0) SURVEY (deep research) → IDEATE → novelty/feasibility gate
        self.survey_brief, self.survey_gaps = await self._survey(plan, exp_id)
        await self._ideate(plan, exp_id)
        if not await self._direction_gate(plan, exp_id):
            return  # direction rejected past the loop limit -> paused + escalated

        # 1) EXPERIMENT_DESIGN ------------------------------------------------
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await self._status("designing")
        design = await self._design(plan, data_spec, plugin, exp_id)

        # 2) critique_design gate (with bounded revision loop) ----------------
        design = await self._design_gate(design, plan, plugin, exp_id)
        if design is None:
            return  # rejected past the loop limit -> paused + escalated

        # design approved -> set up the project repo + experiment branch (IAM)
        await self._iam_setup(plan, domain, exp_id)

        # 2b) CODE: the coder authors the model (gated); falls back to the design model
        solution_code = await self._code(design, data_spec, exp_id)
        if solution_code:
            design["solution_code"] = solution_code

        # 3) EXECUTION --------------------------------------------------------
        await record_transition(self.run_id, exp_id, "experiment_design", "execution", "design approved; running")
        await self._status("executing")
        result = await self._execute(design, data_spec, domain, exp_id)

        # 4) ANALYSIS ---------------------------------------------------------
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "execution", "analysis", "training complete; analyzing")
        await self._status("analyzing")
        analysis = await self._analyze(design, result, exp_id)

        # 5) review_results gate ---------------------------------------------
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

        # 6) OPTIMIZE (<=1 iteration) ----------------------------------------
        await self._status("optimizing")
        best_design, best_result = await self._optimize(design, result, data_spec, domain, exp_id, plugin)

        # 7) WRITE_UP --------------------------------------------------------
        await self._guard_budget("usd", get_settings().est_stage_cost_usd)
        await record_transition(self.run_id, exp_id, "optimize", "write_up", "writing report")
        await self._status("writing")
        await self._write_up(plan, best_design, best_result, analysis, rpanel, exp_id)

        # 8) ARCHIVE ---------------------------------------------------------
        await record_transition(self.run_id, exp_id, "write_up", "archive", "run complete")
        await asyncio.to_thread(set_run_status, self.run_id, "completed")
        await get_bus().publish(
            make_event(
                "run_finished",
                run_id=self.run_id,
                payload={"status": "completed", "metrics": best_result.get("metrics")},
            )
        )
        await self._status("archived", "run complete")

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
        prompt = (
            "Turn this research plan into a concrete experiment design for a "
            "composition->property regression.\n\n"
            f"PLAN:\n{json.dumps(plan, indent=2)}\n\nDATA:\n{json.dumps(data_spec, indent=2)}\n\n"
            + (self.survey_brief + "\n\n" if self.survey_brief else "")
            + (briefing + "\n\n" if briefing else "")
            + "Return ONLY JSON with keys: model ('random_forest' or 'gradient_boosting'), "
            "model_params (object), test_size (0-1), random_state (int). Choose sensible values."
        )
        text = await reason_stage(
            self.run_id, "experiment_design", prompt,
            dry_run=self.dry_run,
            dry_text=f"[dry-run] Design: {fallback['model']} on Magpie features, 80/20 split.",
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
        lit = f"LITERATURE (prior work):\n{self.survey_brief}\n" if self.survey_brief else ""
        evidence = (
            f"HYPOTHESIS: {json.dumps(self.hypothesis) if self.hypothesis else 'n/a'}\n"
            f"DESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
            f"EVAL PROTOCOL SUMMARY: {eval_summary}\n{lit}"
        )
        # one focused, isolated sub-check per concern — independent, so parallel
        subchecks = {
            "leakage": "Assess DATA LEAKAGE risk only (train/test contamination, grouped split adequacy).",
            "overfit": "Assess OVERFITTING only (holdout vs LCSO vs RepeatedKFold spread; CV std).",
            "baseline": "Assess BASELINE ADEQUACY only (does the model beat dummy/ridge/knn/gbm meaningfully?).",
            "stats": "Assess STATISTICAL VALIDITY only (CI width, error stratification by gap range, sample sizes).",
            "sota": "Assess PRIOR WORK / SOTA only: is the headline LCSO result competitive with the "
                    "literature above, and does it support or refute the hypothesis? If no comparable "
                    "published number is available, say so explicitly — do not invent one.",
        }
        header = (
            "Interpret these regression results. The HEADLINE metric is "
            "leave-chemical-system-out MAE (GroupKFold); the random holdout is optimistic. "
        )
        findings = await asyncio.gather(
            *[
                run_worker(
                    self.run_id, f"analysis:{name}",
                    f"{header}{focus}\nBe concise (2-3 sentences).\n\n{evidence}",
                    dry_run=self.dry_run,
                    dry_value=f"[dry-run] {name}: nominal; LCSO MAE={metrics.get('mae_lcso')}.",
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
            "3) ABLATION READING — what the baseline panel + gap-range stratification reveal.\n"
            "4) SOTA COMPARISON — how the LCSO headline sits vs the prior work (or 'no comparable number').\n"
            "5) LIMITATIONS. Lead with the LCSO headline.\n\n"
            f"HYPOTHESIS: {json.dumps(self.hypothesis) if self.hypothesis else 'n/a'}\n"
            f"SUB-REVIEWS:\n{sub_text}\n\nMETRICS: {json.dumps(metrics)}\n{lit}",
            dry_run=self.dry_run,
            dry_value=(
                f"[dry-run] Analysis — Hypothesis verdict: supported (LCSO MAE={metrics.get('mae_lcso')} "
                f"< baselines). Claim: composition features carry band-gap signal -> tree model captures "
                f"it -> usable screen. Ablation: beats dummy/ridge/knn/gbm; worst on wide-gap bin. "
                f"SOTA: comparable to prior work in the briefing. Limitations: composition-only, one dataset. "
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
        prompt = (
            "Write a concise (≤300 words) experiment report in markdown with sections "
            "Objective, Method, Results, Critic Review, Conclusion. In Results, lead "
            "with the leave-chemical-system-out MAE (the honest headline), then give the "
            "RepeatedKFold mean±std and the baseline comparison, and note the gap-range "
            "error breakdown. Do not present the random-holdout number as the headline.\n\n"
            f"PLAN: {json.dumps(plan)}\nDESIGN: {json.dumps(design)}\nMETRICS: {json.dumps(metrics)}\n"
            f"EVAL PROTOCOL SUMMARY: {eval_summary}\n"
            f"ANALYSIS: {analysis}\nCRITIC: {rpanel.consensus_verdict}"
        )
        report = await reason_stage(
            self.run_id, "write_up", prompt,
            dry_run=self.dry_run,
            dry_text=(
                f"# Experiment Report (dry-run)\n\n"
                f"**Objective:** {plan.get('objective', 'n/a')}\n\n"
                f"**Method:** {design.get('model')} on Magpie composition features; "
                f"leave-chemical-system-out GroupKFold (headline) + RepeatedKFold 5x5 + baselines.\n\n"
                f"**Results:** LCSO MAE={metrics.get('mae_lcso')} eV, R²={metrics.get('r2_lcso')} "
                f"(headline); RepeatedKFold MAE={metrics.get('mae_cv_mean')}±{metrics.get('mae_cv_std')}; "
                f"holdout MAE={metrics.get('mae_holdout')}, RMSE={metrics.get('rmse_holdout')}.\n\n"
                f"**Eval protocol:** {eval_summary}\n\n"
                f"**Critic Review:** {rpanel.consensus_verdict}.\n\n"
                f"**Conclusion:** Pipeline ran end-to-end under a leakage-aware protocol; see metrics."
            ),
        )
        path = run_artifacts_dir(self.run_id) / "report.md"
        path.write_text(report)
        if exp_id:
            await asyncio.to_thread(record_artifacts, exp_id, [{"kind": "report", "uri": str(path)}])
        await get_bus().publish(
            make_event("report", run_id=self.run_id, payload={"uri": str(path), "preview": report[:400]})
        )
        # index the headline result so future runs recall what this campaign found
        await self._index(
            "conclusion",
            f"{plan.get('objective', '')}: {design.get('model')} -> LCSO MAE "
            f"{metrics.get('mae_lcso')} eV, R² {metrics.get('r2_lcso')}; verdict "
            f"{rpanel.consensus_verdict}.",
            exp_id, mae_lcso=metrics.get("mae_lcso"), model=design.get("model"),
        )
        # commit the report to the experiment branch + open the PR (IAM)
        await self._iam_finalize(report, rpanel, exp_id)


# --- launch / task tracking ---
_DRIVER_TASKS: set[asyncio.Task] = set()


def launch_driver(run_id: str, dry_run: bool = False) -> asyncio.Task:
    driver = ExperimentDriver(run_id, dry_run=dry_run)
    task = asyncio.create_task(driver.run())
    _DRIVER_TASKS.add(task)
    task.add_done_callback(_DRIVER_TASKS.discard)
    return task
