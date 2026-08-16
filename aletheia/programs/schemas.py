"""Frozen contracts for the scientific hierarchy and its replayable ledger view.

The graph deliberately does not model durable task dependencies.  Its edges state scientific
prerequisites between programs or between campaigns; engineering work remains in ``aletheia.jobs``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.jobs.outbox import ScientificCommandReceipt
from aletheia.reproducibility.manifest import content_sha256

_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_NODE_ID_PATTERN = r"^(qst|prg|cmp)_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_PROGRAM_ID_PATTERN = r"^prg_[0-9a-f]{32}$"
_CAMPAIGN_ID_PATTERN = r"^cmp_[0-9a-f]{32}$"
_FAMILY_ID_PATTERN = r"^fam_[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphNodeType(str, Enum):
    QUEST = "quest"
    PROGRAM = "program"
    CAMPAIGN = "campaign"


class GraphNodeState(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    ARCHIVED = "archived"


class DataRole(str, Enum):
    EXPLORATION = "exploration"
    TRAINING = "training"
    CONFIRMATION = "confirmation"
    EXTERNAL_VALIDATION = "external_validation"
    REPLICATION = "replication"
    SAFETY = "safety"


class BudgetKind(str, Enum):
    USD = "usd"
    GPU_HOURS = "gpu_hours"
    AGENT_SDK_CREDIT = "agent_sdk_credit"
    TOKENS = "tokens"
    WALL_CLOCK_HOURS = "wall_clock_hours"
    EXPERIMENT_COUNT = "experiment_count"


def _stable_id(prefix: str, projection: dict[str, Any]) -> str:
    return f"{prefix}_{content_sha256(projection)[:32]}"


class GraphCommandContext(FrozenModel):
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    principal: str = Field(min_length=1, max_length=128)
    source_event_key: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)


class QuestSpec(FrozenModel):
    identity_key: str = Field(pattern=_IDENTITY_PATTERN)
    title: str = Field(min_length=1, max_length=240)
    direction: str = Field(min_length=1, max_length=4_000)
    value_boundary: str = Field(min_length=1, max_length=4_000)
    safety_boundary: tuple[str, ...] = Field(min_length=1, max_length=64)
    resource_boundary: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return _stable_id(
            "qst",
            {
                "schema": "aletheia.quest_identity.v1",
                "identity_key": self.identity_key,
            },
        )

    @property
    def spec_sha256(self) -> str:
        return content_sha256(self)


class ResearchProgramSpec(FrozenModel):
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    identity_key: str = Field(pattern=_IDENTITY_PATTERN)
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=4_000)
    problem_domain: str = Field(min_length=1, max_length=256)
    knowledge_boundary: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return _stable_id(
            "prg",
            {
                "schema": "aletheia.research_program_identity.v1",
                "quest_id": self.quest_id,
                "identity_key": self.identity_key,
            },
        )

    @property
    def spec_sha256(self) -> str:
        return content_sha256(self)


class ScientificFamilySpec(FrozenModel):
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    family_key: str = Field(pattern=_IDENTITY_PATTERN, max_length=64)
    title: str = Field(min_length=1, max_length=240)
    scientific_scope: str = Field(min_length=1, max_length=4_000)
    multiplicity_policy: dict[str, Any] = Field(default_factory=dict)

    @property
    def family_id(self) -> str:
        return _stable_id(
            "fam",
            {
                "schema": "aletheia.scientific_family_identity.v1",
                "program_id": self.program_id,
                "family_key": self.family_key,
            },
        )

    @property
    def semantic_sha256(self) -> str:
        return content_sha256(
            {
                "schema": "aletheia.scientific_family_semantics.v1",
                "scientific_scope": self.scientific_scope,
                "multiplicity_policy": self.multiplicity_policy,
            }
        )


class CampaignSpec(FrozenModel):
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    family_id: str = Field(pattern=_FAMILY_ID_PATTERN)
    identity_key: str = Field(pattern=_IDENTITY_PATTERN)
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=4_000)
    stopping_boundary: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return _stable_id(
            "cmp",
            {
                "schema": "aletheia.research_campaign_identity.v1",
                "program_id": self.program_id,
                "identity_key": self.identity_key,
            },
        )

    @property
    def spec_sha256(self) -> str:
        return content_sha256(self)


class NodeTransitionSpec(FrozenModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    expected_version: int = Field(ge=1)
    to_state: GraphNodeState
    reason: str = Field(min_length=1, max_length=4_000)


class DependencySpec(FrozenModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    dependency_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    rationale: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def _not_self(self) -> "DependencySpec":
        if self.node_id == self.dependency_node_id:
            raise ValueError("a scientific node cannot depend on itself")
        return self


class CampaignRunBindingSpec(FrozenModel):
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    role: str = Field(default="primary", pattern=r"^[a-z][a-z0-9_]{0,31}$")


class CampaignExperimentBindingSpec(FrozenModel):
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    experiment_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    role: str = Field(default="scientific_attempt", pattern=r"^[a-z][a-z0-9_]{0,31}$")


class ProgramQuestionBindingSpec(FrozenModel):
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    role: str = Field(default="primary", pattern=r"^[a-z][a-z0-9_]{0,31}$")


class DataRoleAllocationSpec(FrozenModel):
    scope_node_id: str = Field(pattern=r"^(qst|prg)_[0-9a-f]{32}$")
    data_asset_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    role: DataRole
    exclusive: bool = True
    policy: dict[str, Any] = Field(default_factory=dict)


class BudgetAllocationSpec(FrozenModel):
    scope_node_id: str = Field(pattern=r"^(qst|prg)_[0-9a-f]{32}$")
    parent_allocation_id: str | None = Field(
        default=None, pattern=r"^bga_[0-9a-f]{32}$"
    )
    kind: BudgetKind
    cap_microunits: int = Field(gt=0)
    policy: dict[str, Any] = Field(default_factory=dict)


class GraphMutationReceipt(FrozenModel):
    object_id: str
    command: ScientificCommandReceipt

    @property
    def created(self) -> bool:
        return self.command.created


class ResearchNodeSnapshot(FrozenModel):
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    parent_node_id: str | None = Field(default=None, pattern=_NODE_ID_PATTERN)
    node_type: GraphNodeType
    identity_key: str
    spec_sha256: str = Field(pattern=_SHA256_PATTERN)
    spec: dict[str, Any]
    state: GraphNodeState
    state_version: int = Field(ge=1)
    created_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ResearchTransitionSnapshot(FrozenModel):
    transition_id: str
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    command_id: str
    from_state: GraphNodeState | None
    to_state: GraphNodeState
    from_version: int = Field(ge=0)
    to_version: int = Field(ge=1)
    reason: str
    principal: str
    created_at: AwareDatetime


class ResearchDependencySnapshot(FrozenModel):
    edge_id: str
    node_id: str = Field(pattern=_NODE_ID_PATTERN)
    dependency_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    rationale: str
    command_id: str
    created_at: AwareDatetime


class ScientificFamilySnapshot(FrozenModel):
    family_id: str = Field(pattern=_FAMILY_ID_PATTERN)
    program_id: str = Field(pattern=_PROGRAM_ID_PATTERN)
    family_key: str
    semantic_sha256: str = Field(pattern=_SHA256_PATTERN)
    spec: dict[str, Any]
    command_id: str
    created_at: AwareDatetime


class CampaignFamilySnapshot(FrozenModel):
    campaign_id: str = Field(pattern=_CAMPAIGN_ID_PATTERN)
    family_id: str = Field(pattern=_FAMILY_ID_PATTERN)
    command_id: str


class ExternalBindingSnapshot(FrozenModel):
    binding_id: str
    binding_type: str
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    external_id: str
    role: str
    command_id: str
    created_at: AwareDatetime


class DataRoleAllocationSnapshot(FrozenModel):
    allocation_id: str
    scope_node_id: str
    data_asset_id: str
    role: DataRole
    exclusive: bool
    data_asset_scope_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy: dict[str, Any]
    command_id: str
    created_at: AwareDatetime


class BudgetAllocationSnapshot(FrozenModel):
    allocation_id: str
    scope_node_id: str
    parent_allocation_id: str | None
    kind: BudgetKind
    cap_microunits: int
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy: dict[str, Any]
    command_id: str
    created_at: AwareDatetime


class QuestGraphSnapshot(FrozenModel):
    schema_version: int = Field(default=1, ge=1)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    nodes: tuple[ResearchNodeSnapshot, ...]
    transitions: tuple[ResearchTransitionSnapshot, ...]
    dependencies: tuple[ResearchDependencySnapshot, ...]
    scientific_families: tuple[ScientificFamilySnapshot, ...]
    campaign_families: tuple[CampaignFamilySnapshot, ...]
    external_bindings: tuple[ExternalBindingSnapshot, ...]
    data_allocations: tuple[DataRoleAllocationSnapshot, ...]
    budget_allocations: tuple[BudgetAllocationSnapshot, ...]
    rebuilt_at: datetime | None = None
    graph_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _verify_content_hash(self) -> "QuestGraphSnapshot":
        projection = self.model_dump(
            mode="json",
            exclude={"graph_sha256", "rebuilt_at"},
        )
        expected = content_sha256(projection)
        if self.graph_sha256 != expected:
            raise ValueError("quest graph snapshot hash does not match reconstructed ledger")
        return self


def graph_snapshot_sha256(payload: dict[str, Any]) -> str:
    """Hash a snapshot constructor payload, excluding operational rebuild time."""

    return content_sha256(
        {
            key: value
            for key, value in payload.items()
            if key not in {"graph_sha256", "rebuilt_at"}
        }
    )


__all__ = [
    "BudgetAllocationSnapshot",
    "BudgetAllocationSpec",
    "BudgetKind",
    "CampaignExperimentBindingSpec",
    "CampaignFamilySnapshot",
    "CampaignRunBindingSpec",
    "CampaignSpec",
    "DataRole",
    "DataRoleAllocationSnapshot",
    "DataRoleAllocationSpec",
    "DependencySpec",
    "ExternalBindingSnapshot",
    "GraphCommandContext",
    "GraphMutationReceipt",
    "GraphNodeState",
    "GraphNodeType",
    "NodeTransitionSpec",
    "ProgramQuestionBindingSpec",
    "QuestGraphSnapshot",
    "QuestSpec",
    "ResearchDependencySnapshot",
    "ResearchNodeSnapshot",
    "ResearchProgramSpec",
    "ResearchTransitionSnapshot",
    "ScientificFamilySnapshot",
    "ScientificFamilySpec",
    "graph_snapshot_sha256",
]
