"""Thin helpers over the ledger. Used by the API and by the ``memory.log`` MCP
tool so agents persist work-log entries to the single source of truth."""

from __future__ import annotations

from typing import Any

from aletheia.db import session_scope
from aletheia.memory.ledger import (
    Artifact,
    BudgetEvent,
    Claim,
    ClaimEvidence,
    ComputeJob,
    CritiquePanel,
    Decision,
    Experiment,
    HypothesisScorecard,
    LiteratureFinding,
    Metric,
    Run,
    SOTAResult,
)


def create_run(
    goal: str,
    domain: str | None = None,
    direction: str | None = None,
    owner: str | None = None,
    budget_cap_usd: float | None = None,
    gpu_hours_cap: float | None = None,
    status: str = "active",
) -> str:
    with session_scope() as s:
        run = Run(
            goal=goal,
            domain=domain,
            direction=direction,
            human_owner=owner,
            budget_cap_usd=budget_cap_usd,
            gpu_hours_cap=gpu_hours_cap,
            status=status,
        )
        s.add(run)
        s.flush()
        return run.id


def get_run(run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        r = s.get(Run, run_id)
        if r is None:
            return None
        plan = (
            s.query(Experiment)
            .filter(Experiment.run_id == run_id, Experiment.stage == "experiment_design")
            .order_by(Experiment.created_at.desc())
            .first()
        )
        return {
            "id": r.id,
            "goal": r.goal,
            "domain": r.domain,
            "direction": r.direction,
            "status": r.status,
            "budget_cap_usd": r.budget_cap_usd,
            "gpu_hours_cap": r.gpu_hours_cap,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "plan": plan.design_json if plan else None,
            "plan_experiment_id": plan.id if plan else None,
        }


def finalize_plan(run_id: str, plan: dict[str, Any]) -> str:
    """Record the agreed experiment plan: mark the run planned and create an
    EXPERIMENT_DESIGN experiment carrying the structured plan. Returns its id.
    This is the hand-off artifact for Phase-1 execution."""
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = "planned"
            if plan.get("domain"):
                run.domain = plan["domain"]
            if plan.get("direction"):
                run.direction = plan["direction"]
            if plan.get("objective"):
                run.goal = plan["objective"]
        exp = Experiment(
            run_id=run_id,
            hypothesis=plan.get("hypothesis"),
            design_json=plan,
            stage="experiment_design",
            status="planned",
        )
        s.add(exp)
        s.flush()
        return exp.id


def create_experiment(
    run_id: str,
    plan: dict[str, Any],
    *,
    hypothesis: str | None = None,
    parent_experiment_id: str | None = None,
    stage: str = "experiment_design",
    status: str = "planned",
) -> str:
    """Create an additional Experiment for a campaign round, linked to the prior
    round via ``parent_experiment_id``. Round 1 reuses ``finalize_plan``'s
    experiment; rounds 2..N use this. Returns the new experiment id."""
    with session_scope() as s:
        exp = Experiment(
            run_id=run_id,
            hypothesis=hypothesis or plan.get("hypothesis"),
            design_json=plan,
            stage=stage,
            status=status,
            parent_experiment_id=parent_experiment_id,
        )
        s.add(exp)
        s.flush()
        return exp.id


def set_experiment_hypothesis(experiment_id: str, hypothesis: str) -> None:
    """Record the hypothesis the IDEATE stage chose for this experiment."""
    with session_scope() as s:
        exp = s.get(Experiment, experiment_id)
        if exp is not None:
            exp.hypothesis = hypothesis


def set_experiment_repo(
    experiment_id: str, code_repo: str | None = None, code_branch: str | None = None
) -> None:
    """Record the GitHub repo/branch this experiment lives in (IAM audit trail)."""
    with session_scope() as s:
        exp = s.get(Experiment, experiment_id)
        if exp is not None:
            if code_repo is not None:
                exp.code_repo = code_repo
            if code_branch is not None:
                exp.code_branch = code_branch


def set_run_status(run_id: str, status: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is not None:
            run.status = status


def list_runs() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Run).order_by(Run.created_at.desc()).all()
        return [
            {
                "id": r.id,
                "goal": r.goal,
                "domain": r.domain,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def log_note(
    run_id: str,
    note: str,
    actor: str = "orchestrator",
    stage_from: str | None = None,
    stage_to: str | None = None,
    experiment_id: str | None = None,
) -> int:
    """Append a work-log / decision entry. Returns the decision row id."""
    with session_scope() as s:
        d = Decision(
            run_id=run_id,
            experiment_id=experiment_id,
            stage_from=stage_from,
            stage_to=stage_to,
            rationale=note,
            actor=actor,
        )
        s.add(d)
        s.flush()
        return d.id


# --- compute / metrics / artifacts (Phase 1) -------------------------------


def create_compute_job(
    experiment_id: str | None,
    backend: str,
    resources: dict[str, Any] | None = None,
    status: str = "queued",
) -> str:
    with session_scope() as s:
        job = ComputeJob(
            experiment_id=experiment_id,
            backend=backend,
            status=status,
            resources_json=resources,
        )
        s.add(job)
        s.flush()
        return job.id


def set_compute_job_status(job_id: str, status: str, ext_id: str | None = None) -> None:
    with session_scope() as s:
        job = s.get(ComputeJob, job_id)
        if job is not None:
            job.status = status
            if ext_id is not None:
                job.ext_id = ext_id


def get_compute_job(job_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        job = s.get(ComputeJob, job_id)
        if job is None:
            return None
        return {
            "id": job.id,
            "experiment_id": job.experiment_id,
            "backend": job.backend,
            "status": job.status,
            "ext_id": job.ext_id,
            "resources": job.resources_json,
        }


def record_metrics(
    experiment_id: str, metrics: dict[str, float], split: str | None = "test", step: int | None = None
) -> None:
    with session_scope() as s:
        for name, value in metrics.items():
            s.add(
                Metric(
                    experiment_id=experiment_id,
                    name=name,
                    value=float(value),
                    split=split,
                    step=step,
                )
            )


def record_artifacts(experiment_id: str, artifacts: list[dict[str, Any]]) -> None:
    with session_scope() as s:
        for a in artifacts:
            s.add(
                Artifact(
                    experiment_id=experiment_id,
                    kind=a.get("kind", "model"),
                    uri=a.get("uri", ""),
                    sha256=a.get("sha256"),
                    bytes=a.get("bytes"),
                )
            )


def list_metrics(experiment_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Metric).filter(Metric.experiment_id == experiment_id).all()
        return [
            {"name": m.name, "value": m.value, "split": m.split, "step": m.step} for m in rows
        ]


def list_artifacts(experiment_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.query(Artifact).filter(Artifact.experiment_id == experiment_id).all()
        return [{"kind": a.kind, "uri": a.uri, "sha256": a.sha256, "bytes": a.bytes} for a in rows]


# --- budget events (Phase 1) ----------------------------------------------


def record_budget_event(run_id: str, kind: str, amount: float) -> float:
    """Append a budget charge of ``kind`` and return the new running total for that
    kind (cumulative = previous cumulative + amount)."""
    with session_scope() as s:
        last = (
            s.query(BudgetEvent)
            .filter(BudgetEvent.run_id == run_id, BudgetEvent.kind == kind)
            .order_by(BudgetEvent.id.desc())
            .first()
        )
        prev = (last.cumulative if last and last.cumulative is not None else 0.0)
        cumulative = prev + float(amount)
        s.add(BudgetEvent(run_id=run_id, kind=kind, amount=float(amount), cumulative=cumulative))
        return cumulative


def budget_spent(run_id: str, kind: str = "usd") -> float:
    with session_scope() as s:
        last = (
            s.query(BudgetEvent)
            .filter(BudgetEvent.run_id == run_id, BudgetEvent.kind == kind)
            .order_by(BudgetEvent.id.desc())
            .first()
        )
        return float(last.cumulative) if last and last.cumulative is not None else 0.0


# --- critic panels (Phase 1) ----------------------------------------------


def record_critique_panel(
    target: str,
    target_ref: str | None,
    consensus_verdict: str | None,
    gate_passed: bool | None,
    raw_json: dict[str, Any] | None,
) -> str:
    with session_scope() as s:
        panel = CritiquePanel(
            target=target,
            target_ref=target_ref,
            consensus_verdict=consensus_verdict,
            gate_passed=gate_passed,
            raw_json=raw_json,
        )
        s.add(panel)
        s.flush()
        return panel.id


# --- evidence ledger: claims ↔ evidence (Phase G) -------------------------


def create_claim(
    run_id: str,
    *,
    claim_text: str,
    claim_type: str,
    strength: str,
    status: str = "proposed",
    experiment_id: str | None = None,
    created_by: str | None = None,
    stage: str | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> str:
    """Record a scientific claim + (optionally) attach its evidence in one call.
    ``evidence`` items: {evidence_kind, evidence_ref, note?}."""
    with session_scope() as s:
        claim = Claim(
            run_id=run_id,
            experiment_id=experiment_id,
            claim_text=claim_text,
            claim_type=claim_type,
            strength=strength,
            status=status,
            created_by=created_by,
            stage=stage,
        )
        s.add(claim)
        s.flush()
        for e in evidence or []:
            s.add(
                ClaimEvidence(
                    claim_id=claim.id,
                    evidence_kind=e.get("evidence_kind", "artifact"),
                    evidence_ref=str(e.get("evidence_ref", "")),
                    note=e.get("note"),
                )
            )
        return claim.id


def update_claim(
    claim_id: str,
    *,
    strength: str | None = None,
    status: str | None = None,
    claim_text: str | None = None,
) -> None:
    """Finalize a claim once more evidence is in (e.g. after the results gate)."""
    with session_scope() as s:
        claim = s.get(Claim, claim_id)
        if claim is None:
            return
        if strength is not None:
            claim.strength = strength
        if status is not None:
            claim.status = status
        if claim_text is not None:
            claim.claim_text = claim_text


def attach_claim_evidence(
    claim_id: str, evidence_kind: str, evidence_ref: str, note: str | None = None
) -> None:
    with session_scope() as s:
        s.add(
            ClaimEvidence(
                claim_id=claim_id,
                evidence_kind=evidence_kind,
                evidence_ref=str(evidence_ref),
                note=note,
            )
        )


def list_claims(run_id: str, experiment_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Claim).filter(Claim.run_id == run_id)
        if experiment_id is not None:
            q = q.filter(Claim.experiment_id == experiment_id)
        rows = q.order_by(Claim.created_at).all()
        out: list[dict[str, Any]] = []
        for c in rows:
            ev = s.query(ClaimEvidence).filter(ClaimEvidence.claim_id == c.id).all()
            out.append(
                {
                    "id": c.id,
                    "claim_text": c.claim_text,
                    "claim_type": c.claim_type,
                    "strength": c.strength,
                    "status": c.status,
                    "created_by": c.created_by,
                    "stage": c.stage,
                    "experiment_id": c.experiment_id,
                    "evidence": [
                        {"evidence_kind": e.evidence_kind, "evidence_ref": e.evidence_ref, "note": e.note}
                        for e in ev
                    ],
                }
            )
        return out


# --- structured literature + SOTA (Phase H) -------------------------------

_LIT_FIELDS = ("paper_id", "query", "method", "dataset", "metric", "result", "limitation", "gap", "relevance", "source")
_SOTA_FIELDS = ("task", "dataset", "metric", "score", "method", "source", "split_policy", "notes")


def record_literature_finding(run_id: str, **fields: Any) -> int:
    with session_scope() as s:
        row = LiteratureFinding(run_id=run_id, **{k: fields.get(k) for k in _LIT_FIELDS})
        s.add(row)
        s.flush()
        return row.id


def list_literature_findings(run_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (
            s.query(LiteratureFinding)
            .filter(LiteratureFinding.run_id == run_id)
            .order_by(LiteratureFinding.id)
            .all()
        )
        return [
            {"id": r.id, **{k: getattr(r, k) for k in _LIT_FIELDS}} for r in rows
        ]


def record_scorecard(
    run_id: str,
    *,
    experiment_id: str | None,
    scores: dict[str, Any],
    decision: str,
    rationale: str | None = None,
) -> str:
    with session_scope() as s:
        row = HypothesisScorecard(
            run_id=run_id, experiment_id=experiment_id,
            scores=scores, decision=decision, rationale=rationale,
        )
        s.add(row)
        s.flush()
        return row.id


def list_scorecards(run_id: str, experiment_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(HypothesisScorecard).filter(HypothesisScorecard.run_id == run_id)
        if experiment_id is not None:
            q = q.filter(HypothesisScorecard.experiment_id == experiment_id)
        rows = q.order_by(HypothesisScorecard.created_at).all()
        return [
            {
                "id": r.id, "experiment_id": r.experiment_id, "scores": r.scores,
                "decision": r.decision, "rationale": r.rationale,
            }
            for r in rows
        ]


def record_sota_result(run_id: str, domain: str | None, **fields: Any) -> int:
    with session_scope() as s:
        score = fields.get("score")
        row = SOTAResult(
            run_id=run_id, domain=domain,
            score=(float(score) if score is not None else None),
            **{k: fields.get(k) for k in _SOTA_FIELDS if k != "score"},
        )
        s.add(row)
        s.flush()
        return row.id


def list_sota_results(run_id: str, domain: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(SOTAResult).filter(SOTAResult.run_id == run_id)
        if domain is not None:
            q = q.filter(SOTAResult.domain == domain)
        rows = q.order_by(SOTAResult.id).all()
        return [
            {"id": r.id, "domain": r.domain, **{k: getattr(r, k) for k in _SOTA_FIELDS}} for r in rows
        ]
