"""Thin helpers over the ledger. Used by the API and by the ``memory.log`` MCP
tool so agents persist work-log entries to the single source of truth."""

from __future__ import annotations

from typing import Any

from aletheia.db import session_scope
from aletheia.memory.ledger import Decision, Experiment, Run


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
