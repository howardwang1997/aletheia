"""Frozen contracts for receipt-backed scientific memory compaction.

The semantic vector store remains a best-effort retrieval aid.  These models describe the
authoritative, append-only memory ledger used to resume scientific work without trusting one
provider's hidden conversation state or allowing compression to erase negative evidence.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aletheia.jobs.outbox import ScientificCommandReceipt
from aletheia.reproducibility.manifest import content_sha256

_NODE_ID_PATTERN = r"^(qst|prg|cmp)_[0-9a-f]{32}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_FACT_ID_PATTERN = r"^mem_[0-9a-f]{32}$"
_COMPACTION_ID_PATTERN = r"^mcp_[0-9a-f]{32}$"
_CONTEXT_RECEIPT_ID_PATTERN = r"^mctx_[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TASK_KEY_PATTERN = r"^(\*|[A-Za-z0-9][A-Za-z0-9._:/-]{0,127})$"


class FrozenMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryFactKind(str, Enum):
    GOAL = "goal"
    HYPOTHESIS = "hypothesis"
    ASSUMPTION = "assumption"
    PREDICTION = "prediction"
    OBSERVATION = "observation"
    EVIDENCE = "evidence"
    DECISION = "decision"
    METHOD = "method"
    RESULT = "result"
    NEGATIVE_RESULT = "negative_result"
    CONTRADICTION = "contradiction"
    LIMITATION = "limitation"
    FAILED_HYPOTHESIS = "failed_hypothesis"
    OPEN_QUESTION = "open_question"
    SAFETY_BOUNDARY = "safety_boundary"


# These facts are copied verbatim into every eligible compaction artifact and prompt context.
# They may be summarized additionally, but a summary is never allowed to replace them.
NON_DROPPABLE_FACT_KINDS = frozenset(
    {
        MemoryFactKind.NEGATIVE_RESULT,
        MemoryFactKind.CONTRADICTION,
        MemoryFactKind.LIMITATION,
        MemoryFactKind.FAILED_HYPOTHESIS,
        MemoryFactKind.SAFETY_BOUNDARY,
    }
)


class MemoryContextRole(str, Enum):
    REQUIRED = "required"
    SUPPORTING = "supporting"


class MemoryCoverageDisposition(str, Enum):
    SUMMARY = "summary"
    EXACT_REQUIRED = "exact_required"
    EXACT_NON_DROPPABLE = "exact_non_droppable"


class MemorySourceKind(str, Enum):
    ARTIFACT = "artifact"
    EVENT = "event"
    LEDGER = "ledger"
    HUMAN = "human"


class MemorySourceRef(FrozenMemoryModel):
    kind: MemorySourceKind
    source_id: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    uri: str | None = Field(default=None, min_length=1, max_length=2_048)


class MemoryTaskBindingSpec(FrozenMemoryModel):
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    context_role: MemoryContextRole


class ResearchMemoryFactSpec(FrozenMemoryModel):
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    kind: MemoryFactKind
    statement: str = Field(min_length=1, max_length=4_000)
    detail: dict[str, Any] = Field(default_factory=dict)
    task_bindings: tuple[MemoryTaskBindingSpec, ...] = Field(min_length=1, max_length=64)
    sources: tuple[MemorySourceRef, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _canonical_members(self) -> "ResearchMemoryFactSpec":
        bindings = sorted(self.task_bindings, key=lambda item: item.task_key)
        if len({item.task_key for item in bindings}) != len(bindings):
            raise ValueError("memory fact task bindings must have unique task keys")
        sources = sorted(
            self.sources,
            key=lambda item: (item.kind.value, item.source_id, item.sha256, item.uri or ""),
        )
        identities = {(item.kind.value, item.source_id) for item in sources}
        if len(identities) != len(sources):
            raise ValueError("memory fact sources must have unique kind/source identities")
        object.__setattr__(self, "statement", self.statement.strip())
        object.__setattr__(self, "task_bindings", tuple(bindings))
        object.__setattr__(self, "sources", tuple(sources))
        return self

    @property
    def fact_id(self) -> str:
        return (
            "mem_"
            + content_sha256(
                {
                    "schema": "aletheia.research_memory_fact.v1",
                    "fact": self.model_dump(mode="json"),
                }
            )[:32]
        )

    @property
    def fact_sha256(self) -> str:
        return content_sha256(self)


class MemoryFactSnapshot(FrozenMemoryModel):
    fact_id: str = Field(pattern=_FACT_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    kind: MemoryFactKind
    statement: str
    detail: dict[str, Any]
    task_bindings: tuple[MemoryTaskBindingSpec, ...]
    sources: tuple[MemorySourceRef, ...]
    fact_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_id: str
    created_by: str
    created_at: AwareDatetime


class MemorySummaryDraft(FrozenMemoryModel):
    """Untrusted producer output plus an explicit claim of source coverage.

    The harness verifies the coverage set and mechanically injects exact non-droppable facts.  A
    model therefore cannot make a compaction valid merely by claiming that it remembered them.
    """

    producer_provider: str = Field(min_length=1, max_length=64)
    producer_model: str = Field(min_length=1, max_length=256)
    prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    summary_text: str = Field(min_length=1, max_length=16_000)
    covered_fact_ids: tuple[str, ...] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def _canonical_coverage(self) -> "MemorySummaryDraft":
        covered = tuple(sorted(set(self.covered_fact_ids)))
        if len(covered) != len(self.covered_fact_ids):
            raise ValueError("summary coverage contains duplicate fact ids")
        if any(not item.startswith("mem_") or len(item) != 36 for item in covered):
            raise ValueError("summary coverage contains an invalid fact id")
        object.__setattr__(self, "summary_text", self.summary_text.strip())
        object.__setattr__(self, "covered_fact_ids", covered)
        return self

    @property
    def draft_sha256(self) -> str:
        return content_sha256(self)


class MemoryCompactionMember(FrozenMemoryModel):
    fact_id: str = Field(pattern=_FACT_ID_PATTERN)
    fact_sha256: str = Field(pattern=_SHA256_PATTERN)
    kind: MemoryFactKind
    disposition: MemoryCoverageDisposition


class MemoryArtifactReceipt(FrozenMemoryModel):
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    relative_path: str = Field(pattern=r"^ledgers/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.json$")
    object_sha256: str = Field(pattern=_SHA256_PATTERN)
    archived_at: AwareDatetime

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class MemoryCompactionArtifact(FrozenMemoryModel):
    schema_version: int = Field(default=1, ge=1)
    compaction_id: str = Field(pattern=_COMPACTION_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    parent_compaction_id: str | None = Field(default=None, pattern=_COMPACTION_ID_PATTERN)
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    members: tuple[MemoryCompactionMember, ...] = Field(min_length=1)
    summary_text: str = Field(min_length=1, max_length=16_000)
    exact_facts: tuple[MemoryFactSnapshot, ...]
    producer_provider: str
    producer_model: str
    producer_prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_draft_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _verify_members(self) -> "MemoryCompactionArtifact":
        members = tuple(sorted(self.members, key=lambda item: item.fact_id))
        if members != self.members or len({item.fact_id for item in members}) != len(members):
            raise ValueError("compaction members must be unique and sorted")
        expected_manifest = content_sha256(
            [
                {
                    "fact_id": item.fact_id,
                    "fact_sha256": item.fact_sha256,
                    "kind": item.kind.value,
                    "disposition": item.disposition.value,
                }
                for item in members
            ]
        )
        if self.source_manifest_sha256 != expected_manifest:
            raise ValueError("compaction source manifest hash does not match its members")
        exact = tuple(sorted(self.exact_facts, key=lambda item: item.fact_id))
        if exact != self.exact_facts or len({item.fact_id for item in exact}) != len(exact):
            raise ValueError("compaction exact facts must be unique and sorted")
        expected_exact = {
            item.fact_id
            for item in members
            if item.disposition
            in {
                MemoryCoverageDisposition.EXACT_REQUIRED,
                MemoryCoverageDisposition.EXACT_NON_DROPPABLE,
            }
        }
        if {item.fact_id for item in exact} != expected_exact:
            raise ValueError("compaction exact facts do not match exact coverage dispositions")
        by_id = {item.fact_id: item for item in members}
        if any(
            fact.fact_sha256 != by_id[fact.fact_id].fact_sha256
            or fact.kind != by_id[fact.fact_id].kind
            for fact in exact
        ):
            raise ValueError("compaction exact facts differ from their member identities")
        return self

    @property
    def object_sha256(self) -> str:
        return content_sha256(self)


class MemoryCompactionSnapshot(FrozenMemoryModel):
    compaction_id: str = Field(pattern=_COMPACTION_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    parent_compaction_id: str | None = Field(default=None, pattern=_COMPACTION_ID_PATTERN)
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    members: tuple[MemoryCompactionMember, ...]
    summary_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_provider: str
    producer_model: str
    producer_prompt_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_draft_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact: MemoryArtifactReceipt
    command_id: str
    created_at: AwareDatetime


class ResearchMemorySnapshot(FrozenMemoryModel):
    schema_version: int = Field(default=1, ge=1)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    facts: tuple[MemoryFactSnapshot, ...]
    compactions: tuple[MemoryCompactionSnapshot, ...]
    latest_compaction_id: str | None = Field(default=None, pattern=_COMPACTION_ID_PATTERN)
    rebuilt_at: datetime | None = None
    memory_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _verify_hash(self) -> "ResearchMemorySnapshot":
        expected = content_sha256(
            self.model_dump(mode="json", exclude={"memory_sha256", "rebuilt_at"})
        )
        if self.memory_sha256 != expected:
            raise ValueError("research memory snapshot hash does not match its ledger projection")
        return self


class TaskContextRequest(FrozenMemoryModel):
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    compaction_id: str | None = Field(default=None, pattern=_COMPACTION_ID_PATTERN)
    max_chars: int = Field(default=12_000, ge=512, le=200_000)
    consumer_provider: str = Field(min_length=1, max_length=64)
    consumer_model: str = Field(min_length=1, max_length=256)


class TaskContextPayload(FrozenMemoryModel):
    schema_version: int = Field(default=1, ge=1)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_node_id: str = Field(pattern=_NODE_ID_PATTERN)
    task_key: str = Field(pattern=_TASK_KEY_PATTERN)
    compaction_id: str = Field(pattern=_COMPACTION_ID_PATTERN)
    compaction_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_fact_ids: tuple[str, ...]
    summary_text: str
    exact_facts: tuple[MemoryFactSnapshot, ...]
    prompt_text: str
    context_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _verify_context_hash(self) -> "TaskContextPayload":
        expected = content_sha256(self.model_dump(mode="json", exclude={"context_sha256"}))
        if self.context_sha256 != expected:
            raise ValueError("task context hash does not match its provider-neutral payload")
        return self


class TaskContextReceipt(FrozenMemoryModel):
    context_receipt_id: str = Field(pattern=_CONTEXT_RECEIPT_ID_PATTERN)
    context: TaskContextPayload
    consumer_provider: str
    consumer_model: str
    max_chars: int
    command: ScientificCommandReceipt


class MemoryMutationReceipt(FrozenMemoryModel):
    object_id: str
    command: ScientificCommandReceipt

    @property
    def created(self) -> bool:
        return self.command.created


def compaction_id(
    *,
    scope_node_id: str,
    task_key: str,
    source_manifest_sha256: str,
    draft_sha256: str,
) -> str:
    return (
        "mcp_"
        + content_sha256(
            {
                "schema": "aletheia.research_memory_compaction_identity.v1",
                "scope_node_id": scope_node_id,
                "task_key": task_key,
                "source_manifest_sha256": source_manifest_sha256,
                "draft_sha256": draft_sha256,
            }
        )[:32]
    )


def source_manifest_sha256(members: tuple[MemoryCompactionMember, ...]) -> str:
    return content_sha256(
        [
            {
                "fact_id": item.fact_id,
                "fact_sha256": item.fact_sha256,
                "kind": item.kind.value,
                "disposition": item.disposition.value,
            }
            for item in members
        ]
    )


def render_task_context(
    *,
    quest_id: str,
    scope_node_id: str,
    task_key: str,
    summary_text: str,
    exact_facts: tuple[MemoryFactSnapshot, ...],
) -> str:
    """Render the exact provider-neutral text whose identity is stored in the receipt."""

    lines = [
        "RECEIPT-VERIFIED RESEARCH MEMORY",
        f"quest={quest_id} scope={scope_node_id} task={task_key}",
        "",
        "DERIVED SUMMARY (navigation only; exact facts below take precedence):",
        summary_text.strip(),
        "",
        "EXACT REQUIRED / NON-DROPPABLE FACTS:",
    ]
    if not exact_facts:
        lines.append("(none)")
    for fact in exact_facts:
        lines.append(f"- [{fact.kind.value}] {fact.fact_id}: {fact.statement}")
        if fact.detail:
            lines.append(
                "  detail="
                + json.dumps(fact.detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            )
        lines.append("  sources=" + ",".join(item.sha256 for item in fact.sources))
    return "\n".join(lines)


def research_memory_snapshot_sha256(payload: dict[str, Any]) -> str:
    return content_sha256(
        {key: value for key, value in payload.items() if key not in {"memory_sha256", "rebuilt_at"}}
    )


__all__ = [
    "MemoryArtifactReceipt",
    "MemoryCompactionArtifact",
    "MemoryCompactionMember",
    "MemoryCompactionSnapshot",
    "MemoryContextRole",
    "MemoryCoverageDisposition",
    "MemoryFactKind",
    "MemoryFactSnapshot",
    "MemoryMutationReceipt",
    "MemorySourceKind",
    "MemorySourceRef",
    "MemorySummaryDraft",
    "MemoryTaskBindingSpec",
    "NON_DROPPABLE_FACT_KINDS",
    "ResearchMemoryFactSpec",
    "ResearchMemorySnapshot",
    "TaskContextPayload",
    "TaskContextReceipt",
    "TaskContextRequest",
    "compaction_id",
    "render_task_context",
    "research_memory_snapshot_sha256",
    "source_manifest_sha256",
]
