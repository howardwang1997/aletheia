"""The experiment ledger — Aletheia's source of truth and anti-chaos backbone.

Every agent reads/writes the same ledger; nothing consequential is "real" until
it lands here. Decisions carry rationale + actor + critic verdict so the whole
research reasoning chain is auditable and re-hydratable across sessions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aletheia.config import get_settings
from aletheia.db import Base

# Embedding dimension is fixed at table-creation time (pgvector requires a
# concrete dim). Read it once from settings; changing it later means recreating
# the memory_chunks table.
_EMBED_DIM = get_settings().embedding_dim

# what kind of content a recall chunk holds:
MEMORY_KINDS = (
    "hypothesis",
    "design_rationale",
    "critique_summary",
    "conclusion",
    "note",
    "literature",  # external prior work (arXiv/OpenAlex), ingested by the SURVEY stage
    "method",  # a frontier method the SURVEY found the field using (name + why + source)
)


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
# --- evidence-ledger vocab (claims ↔ evidence; the anti-overclaiming layer) ---
# what a claim asserts:
CLAIM_TYPES = (
    "novelty",
    "sota",
    "metric",
    "mechanism",
    "limitation",
    "reproducibility",
    "comparison",  # a controlled ablation: method/config A vs B on the same eval
    "cost",
    "safety",
    # the load-bearing claim of a PARADIGM contribution (a new question / problem
    # formulation / representation / metric), as `metric`/`sota` are for a PERFORMANCE
    # contribution. Reaches >speculative only with a reproducible discriminating
    # demonstration (see docs/PARADIGM_MODE_DESIGN.md); SOTA-delta is irrelevant to it.
    "formulation",
)
# how strongly the evidence backs it (a deterministic harness rule sets this, never
# the LLM's self-assessment):
CLAIM_STRENGTHS = ("speculative", "weak", "moderate", "strong")
# where the claim stands relative to the evidence:
# "not_evaluated" = the claim's mechanism was never instantiated (e.g. the executed
# model did not match the hypothesized one), so it is untested — distinct from
# "refuted", which means it was tested and contradicted.
CLAIM_STATUSES = ("proposed", "supported", "refuted", "unverified", "not_evaluated")
# the kind of artifact a piece of evidence points at:
EVIDENCE_KINDS = (
    "paper",
    "metric",
    "artifact",
    "critique_panel",
    "experiment",
    "dataset",
    "code",
    "reproduction",
    "credence",  # K2: the campaign's calibrated Beta credence behind a formulation claim's strength
)

# how a dataset is connected:
#   benchmark  - a named public dataset (matminer/matbench), auto-downloaded
#   upload     - a single file the human uploaded
#   directory  - a local directory of files (multi-file / sharded datasets)
#   url        - an online file or archive (downloaded + extracted on connect)
#   api        - a keyed data source (e.g. Materials Project) — Phase 2
DATA_SOURCES = ("benchmark", "upload", "directory", "url", "api")
DATA_STATUSES = ("needed", "ready", "error")  # readiness gate vocab


class Run(Base):
    """A research campaign within a human-set domain/direction."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    domain: Mapped[str | None] = mapped_column(String(128))
    direction: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    # e.g. active | scoping | completed | results_rejected | paused | failed
    status: Mapped[str] = mapped_column(String(32), default="active")
    human_owner: Mapped[str | None] = mapped_column(String(128))
    budget_cap_usd: Mapped[float | None] = mapped_column(Float)
    gpu_hours_cap: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    experiments: Mapped[list["Experiment"]] = relationship(back_populates="run")


class RunManifestRecord(Base):
    """Immutable, content-addressed identity frozen before a formal run does science."""

    __tablename__ = "run_manifests"
    __table_args__ = (UniqueConstraint("run_id", name="uq_run_manifest_run"),)

    manifest_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    parent_manifest_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    uri: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default="frozen")
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # stable content identity so a resumed run REUSES this row instead of inserting a duplicate
    # (idempotent create). Computed from (run_id, parent, plan, stage). See create_experiment.
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
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
    scientific_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    commit_ordinal: Mapped[int | None] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(scientific_command_id IS NULL AND commit_ordinal IS NULL) OR "
            "(scientific_command_id IS NOT NULL AND commit_ordinal IS NOT NULL)",
            name="ck_artifacts_scientific_commit_pair",
        ),
        UniqueConstraint(
            "scientific_command_id",
            "commit_ordinal",
            name="uq_artifacts_scientific_commit_ordinal",
        ),
    )


class Decision(Base):
    """Why the system transitioned between stages — the auditable reasoning chain."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint(
            "scientific_command_id",
            name="uq_decisions_scientific_command_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    stage_from: Mapped[str | None] = mapped_column(String(32))
    stage_to: Mapped[str | None] = mapped_column(String(32))
    rationale: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(64))  # orchestrator | human | <subagent>
    critique_panel_id: Mapped[str | None] = mapped_column(ForeignKey("critique_panels.id"))
    scientific_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CritiquePanel(Base):
    __tablename__ = "critique_panels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    target: Mapped[str] = mapped_column(
        String(32)
    )  # design | direction | results | demonstration_audit
    target_ref: Mapped[str | None] = mapped_column(String(32))  # ledger id
    consensus_verdict: Mapped[str | None] = mapped_column(String(32))
    gate_passed: Mapped[bool | None] = mapped_column()
    raw_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Claim(Base):
    """A scientific assertion the lab makes (novelty / SOTA / metric / mechanism / …),
    with a strength + status set by the harness from the evidence — so a report can
    never imply stronger evidence than the ledger holds. Each claim links to the
    artifacts that back it via ``ClaimEvidence``."""

    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    claim_text: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(32))  # CLAIM_TYPES
    strength: Mapped[str] = mapped_column(String(16))  # CLAIM_STRENGTHS
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # CLAIM_STATUSES
    created_by: Mapped[str | None] = mapped_column(String(64))  # stage / actor
    stage: Mapped[str | None] = mapped_column(String(32))
    # stable content identity so a resumed run UPDATES this claim instead of inserting a duplicate
    # (idempotent create). Computed from (run_id, experiment_id, claim_type, claim_text).
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    evidence: Mapped[list["ClaimEvidence"]] = relationship(back_populates="claim")


class ClaimEvidence(Base):
    """A pointer from a claim to the concrete evidence that supports it (an eval
    metric key, an artifact uri, a critique-panel id, a paper ref, …)."""

    __tablename__ = "claim_evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    evidence_kind: Mapped[str] = mapped_column(String(32))  # EVIDENCE_KINDS
    evidence_ref: Mapped[str] = mapped_column(Text)  # metric key | uri | ledger id | doi
    note: Mapped[str | None] = mapped_column(Text)

    claim: Mapped[Claim] = relationship(back_populates="evidence")


class BeliefState(Base):
    """K2 (epistemic world model): the campaign's calibrated credence ``Beta(alpha, beta)`` for an
    open-question lineage — "will this line hold on held-out data?". Seeded as a WEAK prior from the
    scorecard; moved ONLY by a harness-verified confirm-split verdict. A planning aid + an honest
    progress signal, NEVER a verdict (it never sets ``holds``/``supported``/strength). One row per
    ``(run_id, question_key)``; the hot path is in-memory on the driver, this is the durable mirror
    + the cross-round audit trail (alongside the ``belief_prior``/``belief_update`` events)."""

    __tablename__ = "belief_states"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    question_key: Mapped[str] = mapped_column(String(96), index=True)
    alpha: Mapped[float] = mapped_column(Float)
    beta: Mapped[float] = mapped_column(Float)
    n_updates: Mapped[int] = mapped_column(
        default=0
    )  # harness-verified confirm-split updates folded in
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("run_id", "question_key", name="uq_belief_run_question"),)


class HypothesisScorecard(Base):
    """A structured pre-execution score of a hypothesis (novelty / feasibility / EIG /
    …). The LLM scores; a fixed harness rule turns the scores into a proceed/block
    decision — so compute is spent on questions worth answering, auditably."""

    __tablename__ = "hypothesis_scorecards"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    scores: Mapped[dict | None] = mapped_column(
        JSONB
    )  # {novelty, feasibility, expected_information_gain, …}
    decision: Mapped[str | None] = mapped_column(String(16))  # proceed | block
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiteratureFinding(Base):
    """A structured row distilled from the SURVEY's retrieved papers — so novelty +
    SOTA reasoning can stand on queryable prior work, not a prose briefing alone."""

    __tablename__ = "literature_findings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    paper_id: Mapped[str | None] = mapped_column(String(256))  # doi | url | title key
    query: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    dataset: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str | None] = mapped_column(Text)  # the reported number / outcome
    limitation: Mapped[str | None] = mapped_column(Text)
    gap: Mapped[str | None] = mapped_column(Text)
    relevance: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))  # arxiv | openalex | profile | …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SOTAResult(Base):
    """A published state-of-the-art number for a domain/task/dataset — the structured
    form of ``DomainProfile.sota_reference``, so "beat the SOTA" is a row comparison,
    not a prose claim. Rows come from the domain profile (curated) + survey extraction."""

    __tablename__ = "sota_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    domain: Mapped[str | None] = mapped_column(String(128), index=True)
    task: Mapped[str | None] = mapped_column(Text)
    dataset: Mapped[str | None] = mapped_column(Text)
    metric: Mapped[str | None] = mapped_column(String(128))
    score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)  # paper / leaderboard ref
    split_policy: Mapped[str | None] = mapped_column(String(128))  # e.g. "scaffold" | "random"
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BudgetEvent(Base):
    __tablename__ = "budget_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # usd | gpu_hours | agent_sdk_credit | tokens
    amount: Mapped[float] = mapped_column(Float)
    cumulative: Mapped[float | None] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerCache(Base):
    """A persisted LLM worker result, keyed by (run_id, content hash of the call). Lets a
    RESUMED run replay the driver and skip every already-completed Claude call (0 tokens,
    instant), fast-forwarding to the first call that never finished. Only successful results
    are stored, so a network-failed call re-runs on resume. See aletheia.orchestrator.worker."""

    __tablename__ = "worker_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64))  # sha256(provider|label|system|model|prompt)
    label: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("run_id", "cache_key", name="uq_worker_cache_run_key"),)


class ComputeJob(Base):
    __tablename__ = "compute_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    backend: Mapped[str] = mapped_column(String(32))  # local | docker | slurm | k8s
    ext_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    resources_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataAsset(Base):
    """A dataset connected to a run — the human's "connect data pipelines" role.

    Two ways it appears (both converge on status="ready", which the launch gate checks):
      - pull: Aletheia calls the `request_data` tool during scoping -> status="needed",
        requested_by="agent"; the human then satisfies it.
      - push: the human registers/uploads data up front -> profiled and seeded into
        the scoping conversation so Aletheia plans against the real columns/target.
    """

    __tablename__ = "data_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # ``primary`` is the only role visible to adaptive design/training.  A ready
    # ``external_validation`` asset is sealed up front but opened only after the
    # campaign and its one-time internal final holdout have finished.
    role: Mapped[str] = mapped_column(String(32), default="primary")
    source: Mapped[str] = mapped_column(String(16))  # benchmark | upload | api
    ref: Mapped[str | None] = mapped_column(Text)  # benchmark name | file path | api dataset id
    target_column: Mapped[str | None] = mapped_column(String(128))
    # explicit feature/composition column (e.g. UCI superconductivity 'material') so the domain
    # featurizer does NOT fall back to the "first non-numeric column" heuristic — auditable + it
    # reaches the AI authoring prompt via resolve_data_spec -> data_spec.
    composition_column: Mapped[str | None] = mapped_column(String(128))
    feature_kind: Mapped[str | None] = mapped_column(String(32))  # e.g. "composition"
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="needed")  # needed | ready | error
    uri: Mapped[str | None] = mapped_column(Text)  # stored path for uploads
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    profile_json: Mapped[dict | None] = mapped_column(JSONB)  # columns/dtypes/n_rows/stats
    requested_by: Mapped[str] = mapped_column(String(16), default="human")  # agent | human
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignSplitLedger(Base):
    """Immutable campaign-wide row-role allocation for Epistemic Seal v2."""

    __tablename__ = "campaign_split_ledgers"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_campaign_split_run"),
        UniqueConstraint(
            "final_action_id",
            name="uq_campaign_split_ledgers_final_action_id",
        ),
        ForeignKeyConstraint(
            ["final_action_id", "final_action_receipt_sha256"],
            [
                "external_action_receipts.action_id",
                "external_action_receipts.receipt_sha256",
            ],
            name="fk_campaign_split_ledgers_final_action_receipt",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    dataset_fingerprint: Mapped[str] = mapped_column(String(64))
    row_identity_hash: Mapped[str] = mapped_column(String(64))
    split_algo_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="sealed")
    plan_json: Mapped[dict] = mapped_column(JSONB)
    final_result_json: Mapped[dict | None] = mapped_column(JSONB)
    final_action_id: Mapped[str | None] = mapped_column(
        ForeignKey("one_time_external_actions.action_id"), index=True
    )
    final_action_receipt_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    final_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExternalValidationLedger(Base):
    """One-time, locked-code evaluation on a separately sourced dataset."""

    __tablename__ = "external_validation_ledgers"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_external_validation_run"),
        UniqueConstraint(
            "action_id",
            name="uq_external_validation_ledgers_action_id",
        ),
        ForeignKeyConstraint(
            ["action_id", "action_receipt_sha256"],
            [
                "external_action_receipts.action_id",
                "external_action_receipts.receipt_sha256",
            ],
            name="fk_external_validation_ledgers_action_receipt",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    data_asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    dataset_fingerprint: Mapped[str] = mapped_column(String(64))
    row_identity_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="sealed")
    provenance_json: Mapped[dict] = mapped_column(JSONB)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("one_time_external_actions.action_id"), index=True
    )
    action_receipt_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HypothesisAttempt(Base):
    """Family-level disclosure of every hypothesis assigned a confirm/final test."""

    __tablename__ = "hypothesis_attempts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "experiment_id", "phase", name="uq_hypothesis_attempt_run_exp_phase"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True)
    family_key: Mapped[str] = mapped_column(String(64), index=True)
    hypothesis_key: Mapped[str] = mapped_column(String(64))
    hypothesis_text: Mapped[str] = mapped_column(Text)
    round_index: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(
        String(24)
    )  # confirmation | final_holdout | external_replication
    confirmation_batch: Mapped[int | None] = mapped_column(Integer)
    split_hash: Mapped[str] = mapped_column(String(64))
    alpha_allocated: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="registered")
    outcome_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryChunk(Base):
    """Semantic-recall store: embedded fragments of research reasoning (hypotheses,
    design rationales, critique summaries, conclusions). Lets the orchestrator ask
    "have we tried X? what failed? what did critics say?" before designing — the
    anti-chaos backbone that stops the loop re-running dead ends.

    run_id is nullable so recall can be cross-run (portfolio memory); queries
    rank by cosine distance over ``embedding``.
    """

    __tablename__ = "memory_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBED_DIM))
    meta_json: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- platform IAM: who may operate the lab -------------------------------
AUTH_PROVIDERS = ("local", "github", "feishu", "phone")
USER_ROLES = ("owner", "operator", "viewer")


class User(Base):
    """A human operator of the platform. One user may have several linked logins
    (local password, GitHub, Feishu, phone) — see Identity."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    display_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256), index=True)
    role: Mapped[str] = mapped_column(String(16), default="owner")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Identity(Base):
    """A login method bound to a user. ``subject`` is the provider-scoped id
    (local: email; github: user id; feishu: open_id; phone: E.164). ``secret_hash``
    holds the pbkdf2 password for the local provider; null for OAuth/phone."""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(256))
    secret_hash: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """A server-side session. The cookie carries an opaque token; only its sha256
    is stored here, so sessions are revocable and the raw token never persists."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """Canonical event-bus sink: every SDK message + service event."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "event_key IS NULL OR event_sha256 IS NOT NULL",
            name="ck_events_key_has_sha256",
        ),
        UniqueConstraint("event_key", name="uq_events_event_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_key: Mapped[str | None] = mapped_column(String(128))
    event_sha256: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    agent: Mapped[str | None] = mapped_column(String(64))
    parent_tool_use_id: Mapped[str | None] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ix_events_run_ts", Event.run_id, Event.ts)
