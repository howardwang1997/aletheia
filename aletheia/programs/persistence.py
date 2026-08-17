"""SQLAlchemy records for the immutable scientific program graph ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aletheia.db import Base


class ResearchGraphNodeRecord(Base):
    __tablename__ = "research_graph_nodes"
    __table_args__ = (
        CheckConstraint(
            "node_type IN ('quest','program','campaign')",
            name="ck_research_graph_nodes_type",
        ),
        CheckConstraint("state_version >= 1", name="ck_research_graph_nodes_state_version"),
        CheckConstraint(
            "(node_type = 'quest' AND parent_node_id IS NULL AND quest_id = node_id) OR "
            "(node_type IN ('program','campaign') AND parent_node_id IS NOT NULL "
            "AND quest_id <> node_id)",
            name="ck_research_graph_nodes_hierarchy_shape",
        ),
        UniqueConstraint(
            "quest_id",
            "node_type",
            "identity_key",
            name="uq_research_graph_nodes_scoped_identity",
        ),
        Index("ix_research_graph_nodes_quest_type", "quest_id", "node_type"),
    )

    node_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    parent_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    node_type: Mapped[str] = mapped_column(String(16), index=True)
    identity_key: Mapped[str] = mapped_column(String(128))
    spec_sha256: Mapped[str] = mapped_column(String(64), index=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    current_state: Mapped[str] = mapped_column(String(24), index=True)
    state_version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchGraphTransitionRecord(Base):
    __tablename__ = "research_graph_transitions"
    __table_args__ = (
        CheckConstraint(
            "to_version = from_version + 1",
            name="ck_research_graph_transitions_version_step",
        ),
        CheckConstraint(
            "(from_version = 0 AND from_state IS NULL) OR "
            "(from_version >= 1 AND from_state IS NOT NULL)",
            name="ck_research_graph_transitions_source_shape",
        ),
        UniqueConstraint(
            "node_id", "to_version", name="uq_research_graph_transitions_node_version"
        ),
        UniqueConstraint("command_id", name="uq_research_graph_transitions_command"),
    )

    transition_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), index=True)
    from_version: Mapped[int] = mapped_column(Integer)
    to_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    principal: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchScientificFamilyRecord(Base):
    __tablename__ = "research_scientific_families"
    __table_args__ = (
        UniqueConstraint(
            "program_node_id",
            "family_key",
            name="uq_research_scientific_families_program_key",
        ),
        UniqueConstraint("command_id", name="uq_research_scientific_families_command"),
    )

    family_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    program_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    family_key: Mapped[str] = mapped_column(String(64), index=True)
    semantic_sha256: Mapped[str] = mapped_column(String(64), index=True)
    spec_sha256: Mapped[str] = mapped_column(String(64))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchCampaignFamilyRecord(Base):
    __tablename__ = "research_campaign_families"
    __table_args__ = (UniqueConstraint("command_id", name="uq_research_campaign_families_command"),)

    campaign_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), primary_key=True
    )
    family_id: Mapped[str] = mapped_column(
        ForeignKey("research_scientific_families.family_id"), index=True
    )
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchGraphDependencyRecord(Base):
    __tablename__ = "research_graph_dependencies"
    __table_args__ = (
        CheckConstraint(
            "node_id <> dependency_node_id",
            name="ck_research_graph_dependencies_not_self",
        ),
        UniqueConstraint(
            "node_id",
            "dependency_node_id",
            name="uq_research_graph_dependencies_pair",
        ),
        UniqueConstraint("command_id", name="uq_research_graph_dependencies_command"),
    )

    edge_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    dependency_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    rationale: Mapped[str] = mapped_column(Text)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchProgramQuestionRecord(Base):
    __tablename__ = "research_program_questions"
    __table_args__ = (
        UniqueConstraint(
            "program_node_id",
            "question_sha256",
            name="uq_research_program_questions_pair",
        ),
        UniqueConstraint("command_id", name="uq_research_program_questions_command"),
    )

    binding_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    program_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    question_sha256: Mapped[str] = mapped_column(
        ForeignKey("epistemic_research_questions.question_sha256"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchCampaignRunRecord(Base):
    __tablename__ = "research_campaign_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_campaign_runs_run"),
        UniqueConstraint("command_id", name="uq_research_campaign_runs_command"),
    )

    binding_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    campaign_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchCampaignExperimentRecord(Base):
    __tablename__ = "research_campaign_experiments"
    __table_args__ = (
        UniqueConstraint("experiment_id", name="uq_research_campaign_experiments_experiment"),
        UniqueConstraint("command_id", name="uq_research_campaign_experiments_command"),
    )

    binding_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    campaign_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchDataRoleAllocationRecord(Base):
    __tablename__ = "research_data_role_allocations"
    __table_args__ = (
        CheckConstraint(
            "role IN ('exploration','training','confirmation','external_validation',"
            "'replication','safety')",
            name="ck_research_data_role_allocations_role",
        ),
        UniqueConstraint(
            "scope_node_id",
            "data_asset_id",
            "role",
            name="uq_research_data_role_allocations_scope_asset_role",
        ),
        UniqueConstraint("command_id", name="uq_research_data_role_allocations_command"),
    )

    allocation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    scope_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    data_asset_id: Mapped[str] = mapped_column(ForeignKey("data_assets.id"), index=True)
    data_asset_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    source_role: Mapped[str] = mapped_column(String(32))
    data_asset_scope_sha256: Mapped[str] = mapped_column(String(64))
    data_asset_scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    role: Mapped[str] = mapped_column(String(32), index=True)
    exclusive: Mapped[bool] = mapped_column(Boolean)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchBudgetAllocationRecord(Base):
    __tablename__ = "research_budget_allocations"
    __table_args__ = (
        CheckConstraint("cap_microunits > 0", name="ck_research_budget_allocations_cap"),
        CheckConstraint(
            "kind IN ('usd','gpu_hours','agent_sdk_credit','tokens','wall_clock_hours',"
            "'experiment_count')",
            name="ck_research_budget_allocations_kind",
        ),
        UniqueConstraint("scope_node_id", "kind", name="uq_research_budget_allocations_scope_kind"),
        UniqueConstraint("command_id", name="uq_research_budget_allocations_command"),
    )

    allocation_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    scope_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    parent_allocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_budget_allocations.allocation_id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    cap_microunits: Mapped[int] = mapped_column(BigInteger)
    policy_sha256: Mapped[str] = mapped_column(String(64))
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchMemoryFactRecord(Base):
    """One immutable scientific fact; summaries are derived from these rows."""

    __tablename__ = "research_memory_facts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('goal','hypothesis','assumption','prediction','observation','evidence',"
            "'decision','method','result','negative_result','contradiction','limitation',"
            "'failed_hypothesis','open_question','safety_boundary')",
            name="ck_research_memory_facts_kind",
        ),
        UniqueConstraint("command_id", name="uq_research_memory_facts_command"),
        Index("ix_research_memory_facts_scope_kind", "scope_node_id", "kind"),
    )

    fact_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    scope_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    statement: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    fact_sha256: Mapped[str] = mapped_column(String(64), index=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchMemoryTaskBindingRecord(Base):
    """Explicit task relevance; no semantic search is allowed to expand prompt authority."""

    __tablename__ = "research_memory_task_bindings"
    __table_args__ = (
        CheckConstraint(
            "context_role IN ('required','supporting')",
            name="ck_research_memory_task_bindings_role",
        ),
    )

    fact_id: Mapped[str] = mapped_column(
        ForeignKey("research_memory_facts.fact_id"), primary_key=True
    )
    task_key: Mapped[str] = mapped_column(String(128), primary_key=True, index=True)
    context_role: Mapped[str] = mapped_column(String(24), index=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )


class ResearchMemoryCompactionRecord(Base):
    """Immutable receipt for one complete task/scope memory projection."""

    __tablename__ = "research_memory_compactions"
    __table_args__ = (
        CheckConstraint("source_count > 0", name="ck_research_memory_compactions_source_count"),
        CheckConstraint(
            "exact_count >= 0 AND exact_count <= source_count",
            name="ck_research_memory_compactions_exact_count",
        ),
        UniqueConstraint("command_id", name="uq_research_memory_compactions_command"),
        Index(
            "ix_research_memory_compactions_scope_task",
            "scope_node_id",
            "task_key",
            "created_at",
        ),
        Index(
            "uq_research_memory_compactions_root",
            "scope_node_id",
            "task_key",
            unique=True,
            postgresql_where=text("parent_compaction_id IS NULL"),
        ),
        Index(
            "uq_research_memory_compactions_parent",
            "parent_compaction_id",
            unique=True,
            postgresql_where=text("parent_compaction_id IS NOT NULL"),
        ),
    )

    compaction_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    scope_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    task_key: Mapped[str] = mapped_column(String(128), index=True)
    parent_compaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_memory_compactions.compaction_id"), index=True
    )
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_count: Mapped[int] = mapped_column(Integer)
    exact_count: Mapped[int] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text)
    summary_sha256: Mapped[str] = mapped_column(String(64))
    producer_provider: Mapped[str] = mapped_column(String(64))
    producer_model: Mapped[str] = mapped_column(String(256))
    producer_prompt_sha256: Mapped[str] = mapped_column(String(64))
    producer_draft_sha256: Mapped[str] = mapped_column(String(64))
    artifact_sha256: Mapped[str] = mapped_column(String(64), index=True)
    artifact_bytes: Mapped[int] = mapped_column(BigInteger)
    artifact_relative_path: Mapped[str] = mapped_column(Text)
    artifact_object_sha256: Mapped[str] = mapped_column(String(64))
    artifact_receipt_sha256: Mapped[str] = mapped_column(String(64), index=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchMemoryCompactionMemberRecord(Base):
    __tablename__ = "research_memory_compaction_members"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('summary','exact_required','exact_non_droppable')",
            name="ck_research_memory_compaction_members_disposition",
        ),
    )

    compaction_id: Mapped[str] = mapped_column(
        ForeignKey("research_memory_compactions.compaction_id"), primary_key=True
    )
    fact_id: Mapped[str] = mapped_column(
        ForeignKey("research_memory_facts.fact_id"), primary_key=True, index=True
    )
    fact_sha256: Mapped[str] = mapped_column(String(64))
    fact_kind: Mapped[str] = mapped_column(String(32))
    disposition: Mapped[str] = mapped_column(String(32), index=True)


class ResearchMemoryContextReceiptRecord(Base):
    """Append-only proof of the provider-neutral context delivered to one consumer."""

    __tablename__ = "research_memory_context_receipts"
    __table_args__ = (
        CheckConstraint("max_chars >= 512", name="ck_research_memory_context_max_chars"),
        CheckConstraint(
            "prompt_chars > 0 AND prompt_chars <= max_chars",
            name="ck_research_memory_context_prompt_chars",
        ),
        UniqueConstraint("command_id", name="uq_research_memory_context_command"),
        Index(
            "ix_research_memory_context_scope_task",
            "scope_node_id",
            "task_key",
            "created_at",
        ),
    )

    context_receipt_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    compaction_id: Mapped[str] = mapped_column(
        ForeignKey("research_memory_compactions.compaction_id"), index=True
    )
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    scope_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    task_key: Mapped[str] = mapped_column(String(128), index=True)
    consumer_provider: Mapped[str] = mapped_column(String(64))
    consumer_model: Mapped[str] = mapped_column(String(256))
    max_chars: Mapped[int] = mapped_column(Integer)
    prompt_chars: Mapped[int] = mapped_column(Integer)
    selected_manifest_sha256: Mapped[str] = mapped_column(String(64))
    context_sha256: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchPortfolioSlateRecord(Base):
    """Frozen proposal, independent assessment, graph, budget, and memory boundary."""

    __tablename__ = "research_portfolio_slates"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_research_portfolio_slates_command"),
        Index("ix_research_portfolio_slates_quest_created", "quest_id", "created_at"),
    )

    slate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    memory_context_receipt_id: Mapped[str] = mapped_column(
        ForeignKey("research_memory_context_receipts.context_receipt_id"), index=True
    )
    policy_sha256: Mapped[str] = mapped_column(String(64), index=True)
    proposal_sha256: Mapped[str] = mapped_column(String(64), index=True)
    assessment_batch_sha256: Mapped[str] = mapped_column(String(64), index=True)
    spec_sha256: Mapped[str] = mapped_column(String(64), index=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    graph_sha256: Mapped[str] = mapped_column(String(64), index=True)
    graph_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    budget_state_sha256: Mapped[str] = mapped_column(String(64), index=True)
    budget_state_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchPortfolioCandidateRecord(Base):
    """One action and its independent inputs as frozen within a portfolio slate."""

    __tablename__ = "research_portfolio_candidates"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('advance_campaign','discriminating_experiment','replication',"
            "'mechanism_test','acquire_data','repair_capability','start_campaign',"
            "'pause_program','stop_program')",
            name="ck_research_portfolio_candidates_action_type",
        ),
        ForeignKeyConstraint(
            ["slate_id"],
            ["research_portfolio_slates.slate_id"],
            name="fk_research_portfolio_candidates_slate",
        ),
        UniqueConstraint(
            "slate_id",
            "action_sha256",
            name="uq_research_portfolio_candidates_slate_action",
        ),
        Index(
            "ix_research_portfolio_candidates_program",
            "program_id",
            "slate_id",
        ),
    )

    slate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    program_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    family_id: Mapped[str | None] = mapped_column(
        ForeignKey("research_scientific_families.family_id"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(48), index=True)
    target_node_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
    action_sha256: Mapped[str] = mapped_column(String(64), index=True)
    action_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    assessment_sha256: Mapped[str] = mapped_column(String(64), index=True)
    assessment_json: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ResearchPortfolioHumanPlanRecord(Base):
    """A human plan committed before planner scores are materialized."""

    __tablename__ = "research_portfolio_human_plans"
    __table_args__ = (
        UniqueConstraint("slate_id", name="uq_research_portfolio_human_plans_slate"),
        UniqueConstraint("command_id", name="uq_research_portfolio_human_plans_command"),
    )

    human_plan_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    slate_id: Mapped[str] = mapped_column(
        ForeignKey("research_portfolio_slates.slate_id"), index=True
    )
    plan_sha256: Mapped[str] = mapped_column(String(64), index=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchPortfolioEpochRecord(Base):
    """One immutable shadow evaluation; it cannot enqueue work or reserve budget."""

    __tablename__ = "research_portfolio_epochs"
    __table_args__ = (
        CheckConstraint("score_count >= 2", name="ck_research_portfolio_epochs_score_count"),
        CheckConstraint(
            "shadow_only IS TRUE AND actions_enqueued IS FALSE",
            name="ck_research_portfolio_epochs_shadow_only",
        ),
        UniqueConstraint("slate_id", name="uq_research_portfolio_epochs_slate"),
        UniqueConstraint("human_plan_id", name="uq_research_portfolio_epochs_human_plan"),
        UniqueConstraint("command_id", name="uq_research_portfolio_epochs_command"),
        Index("ix_research_portfolio_epochs_quest_evaluated", "quest_id", "evaluated_at"),
    )

    epoch_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    slate_id: Mapped[str] = mapped_column(
        ForeignKey("research_portfolio_slates.slate_id"), index=True
    )
    quest_id: Mapped[str] = mapped_column(ForeignKey("research_graph_nodes.node_id"), index=True)
    human_plan_id: Mapped[str] = mapped_column(
        ForeignKey("research_portfolio_human_plans.human_plan_id"), index=True
    )
    score_count: Mapped[int] = mapped_column(Integer)
    decision_sha256: Mapped[str] = mapped_column(String(64), index=True)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    comparison_sha256: Mapped[str] = mapped_column(String(64), index=True)
    comparison_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    epoch_sha256: Mapped[str] = mapped_column(String(64), index=True)
    shadow_only: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    actions_enqueued: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("scientific_commands.command_id"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ResearchPortfolioScoreRecord(Base):
    """Harness-derived candidate score within one frozen shadow epoch."""

    __tablename__ = "research_portfolio_scores"
    __table_args__ = (
        ForeignKeyConstraint(
            ["slate_id", "candidate_id"],
            [
                "research_portfolio_candidates.slate_id",
                "research_portfolio_candidates.candidate_id",
            ],
            name="fk_research_portfolio_scores_candidate",
        ),
        UniqueConstraint(
            "epoch_id",
            "rank",
            name="uq_research_portfolio_scores_epoch_rank",
        ),
        CheckConstraint("rank >= 1", name="ck_research_portfolio_scores_rank"),
        Index("ix_research_portfolio_scores_epoch_selected", "epoch_id", "selected"),
    )

    epoch_id: Mapped[str] = mapped_column(
        ForeignKey("research_portfolio_epochs.epoch_id"), primary_key=True
    )
    slate_id: Mapped[str] = mapped_column(String(96))
    candidate_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    score_sha256: Mapped[str] = mapped_column(String(64), index=True)
    score_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    feasible: Mapped[bool] = mapped_column(Boolean, index=True)
    base_utility_microscore: Mapped[int] = mapped_column(BigInteger)
    selected: Mapped[bool] = mapped_column(Boolean, index=True)
    rank: Mapped[int] = mapped_column(Integer)


__all__ = [
    "ResearchBudgetAllocationRecord",
    "ResearchCampaignExperimentRecord",
    "ResearchCampaignFamilyRecord",
    "ResearchCampaignRunRecord",
    "ResearchDataRoleAllocationRecord",
    "ResearchGraphDependencyRecord",
    "ResearchGraphNodeRecord",
    "ResearchGraphTransitionRecord",
    "ResearchMemoryCompactionMemberRecord",
    "ResearchMemoryCompactionRecord",
    "ResearchMemoryContextReceiptRecord",
    "ResearchMemoryFactRecord",
    "ResearchMemoryTaskBindingRecord",
    "ResearchPortfolioCandidateRecord",
    "ResearchPortfolioEpochRecord",
    "ResearchPortfolioHumanPlanRecord",
    "ResearchPortfolioScoreRecord",
    "ResearchPortfolioSlateRecord",
    "ResearchProgramQuestionRecord",
    "ResearchScientificFamilyRecord",
]
