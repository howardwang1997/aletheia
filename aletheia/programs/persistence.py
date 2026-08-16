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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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
    quest_id: Mapped[str] = mapped_column(
        ForeignKey("research_graph_nodes.node_id"), index=True
    )
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
        UniqueConstraint(
            "command_id", name="uq_research_graph_transitions_command"
        ),
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
        UniqueConstraint(
            "command_id", name="uq_research_scientific_families_command"
        ),
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
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_research_campaign_families_command"),
    )

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
        UniqueConstraint(
            "scope_node_id", "kind", name="uq_research_budget_allocations_scope_kind"
        ),
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


__all__ = [
    "ResearchBudgetAllocationRecord",
    "ResearchCampaignExperimentRecord",
    "ResearchCampaignFamilyRecord",
    "ResearchCampaignRunRecord",
    "ResearchDataRoleAllocationRecord",
    "ResearchGraphDependencyRecord",
    "ResearchGraphNodeRecord",
    "ResearchGraphTransitionRecord",
    "ResearchProgramQuestionRecord",
    "ResearchScientificFamilyRecord",
]
