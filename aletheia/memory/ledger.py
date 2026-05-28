"""The experiment ledger — Aletheia's source of truth and anti-chaos backbone.

Every agent reads/writes the same ledger; nothing consequential is "real" until
it lands here. Decisions carry rationale + actor + critic verdict so the whole
research reasoning chain is auditable and re-hydratable across sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aletheia.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


# --- lifecycle vocab (kept as plain strings for flexibility) ---
RUN_STATUSES = ("active", "paused", "completed", "failed", "archived")
STAGES = (
    "idea",
    "hypothesis",
    "experiment_design",
    "execution",
    "analysis",
    "optimize",
    "write_up",
    "archive",
)


class Run(Base):
    """A research campaign within a human-set domain/direction."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    domain: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")
    human_owner: Mapped[str | None] = mapped_column(String(128))
    budget_cap_usd: Mapped[float | None] = mapped_column(Float)
    gpu_hours_cap: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    experiments: Mapped[list["Experiment"]] = relationship(back_populates="run")


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    design_json: Mapped[dict | None] = mapped_column(JSONB)
    stage: Mapped[str] = mapped_column(String(32), default="idea")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    code_repo: Mapped[str | None] = mapped_column(String(256))
    code_branch: Mapped[str | None] = mapped_column(String(256))
    parent_experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="experiments")


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)
    split: Mapped[str | None] = mapped_column(String(32))
    step: Mapped[int | None] = mapped_column(BigInteger)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))  # model | dataset | plot | report | code
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Decision(Base):
    """Why the system transitioned between stages — the auditable reasoning chain."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    stage_from: Mapped[str | None] = mapped_column(String(32))
    stage_to: Mapped[str | None] = mapped_column(String(32))
    rationale: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(64))  # orchestrator | human | <subagent>
    critique_panel_id: Mapped[str | None] = mapped_column(ForeignKey("critique_panels.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CritiquePanel(Base):
    __tablename__ = "critique_panels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    target: Mapped[str] = mapped_column(String(32))  # design | direction | results
    target_ref: Mapped[str | None] = mapped_column(String(32))  # ledger id
    consensus_verdict: Mapped[str | None] = mapped_column(String(32))
    gate_passed: Mapped[bool | None] = mapped_column()
    raw_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BudgetEvent(Base):
    __tablename__ = "budget_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # usd | gpu_hours | agent_sdk_credit | tokens
    amount: Mapped[float] = mapped_column(Float)
    cumulative: Mapped[float | None] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ComputeJob(Base):
    __tablename__ = "compute_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    backend: Mapped[str] = mapped_column(String(32))  # local | docker | slurm | k8s
    ext_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    resources_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """Canonical event-bus sink: every SDK message + service event."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    agent: Mapped[str | None] = mapped_column(String(64))
    parent_tool_use_id: Mapped[str | None] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_events_run_ts", Event.run_id, Event.ts)
