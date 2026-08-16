"""Transactional, receipt-backed scientific memory and task context projection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.jobs.persistence import ScientificCommandRecord
from aletheia.paths import research_memory_archive_dir
from aletheia.programs.memory_archive import ScientificMemoryArchive
from aletheia.programs.memory_schemas import (
    MemoryArtifactReceipt,
    MemoryCompactionArtifact,
    MemoryCompactionMember,
    MemoryCompactionSnapshot,
    MemoryContextRole,
    MemoryCoverageDisposition,
    MemoryFactSnapshot,
    MemoryMutationReceipt,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    NON_DROPPABLE_FACT_KINDS,
    ResearchMemoryFactSpec,
    ResearchMemorySnapshot,
    TaskContextPayload,
    TaskContextReceipt,
    TaskContextRequest,
    compaction_id,
    render_task_context,
    research_memory_snapshot_sha256,
    source_manifest_sha256,
)
from aletheia.programs.persistence import (
    ResearchGraphNodeRecord,
    ResearchMemoryCompactionMemberRecord,
    ResearchMemoryCompactionRecord,
    ResearchMemoryContextReceiptRecord,
    ResearchMemoryFactRecord,
    ResearchMemoryTaskBindingRecord,
)
from aletheia.programs.schemas import GraphCommandContext, GraphNodeType
from aletheia.reproducibility.manifest import content_sha256

_TASK_KEY_RE = re.compile(r"^(\*|[A-Za-z0-9][A-Za-z0-9._:/-]{0,127})$")


class ResearchMemoryError(RuntimeError):
    """Base error for the authoritative scientific-memory ledger."""


class ResearchMemoryNotFound(ResearchMemoryError):
    pass


class ResearchMemoryConflict(ResearchMemoryError):
    pass


class ResearchMemoryStale(ResearchMemoryError):
    pass


class ResearchMemoryInvariantError(ResearchMemoryError):
    pass


class ResearchMemoryContextOverflow(ResearchMemoryError):
    pass


def _record_id(prefix: str, projection: dict[str, Any]) -> str:
    return f"{prefix}_{content_sha256(projection)[:32]}"


def _task_key(value: str) -> str:
    value = value.strip()
    if _TASK_KEY_RE.fullmatch(value) is None:
        raise ValueError("memory task key is invalid")
    return value


def _command_key(context: GraphCommandContext, operation: str) -> str:
    return _record_id(
        "rm",
        {
            "schema": "aletheia.research_memory_command_key.v1",
            "operation": operation,
            "client_idempotency_key": context.idempotency_key,
        },
    )


def _source_key(context: GraphCommandContext) -> str | None:
    if context.source_event_key is None:
        return None
    return _record_id(
        "rms",
        {
            "schema": "aletheia.research_memory_source_key.v1",
            "source_event_key": context.source_event_key,
        },
    )


def _scientific_command(
    *,
    operation: str,
    object_id: str,
    payload: dict[str, Any],
    context: GraphCommandContext,
    command_type: ScientificCommandType,
    aggregate_type: str,
    event_type: str,
) -> ScientificCommandSpec:
    return ScientificCommandSpec(
        run_id=None,
        command_type=command_type.value,
        aggregate_type=aggregate_type,
        aggregate_id=object_id,
        idempotency_key=_command_key(context, operation),
        source_event_key=_source_key(context),
        input={"operation": operation, **payload},
        principal=context.principal,
        event_type=event_type,
    )


def _scope_or_error(session: Session, scope_node_id: str) -> ResearchGraphNodeRecord:
    scope = session.get(ResearchGraphNodeRecord, scope_node_id)
    if scope is None:
        raise ResearchMemoryNotFound(f"research graph scope not found: {scope_node_id}")
    return scope


def _lock_quest(session: Session, quest_id: str) -> None:
    quest = session.scalar(
        select(ResearchGraphNodeRecord)
        .where(ResearchGraphNodeRecord.node_id == quest_id)
        .with_for_update()
    )
    if quest is None or quest.node_type != GraphNodeType.QUEST.value:
        raise ResearchMemoryNotFound(f"research quest not found: {quest_id}")


def _scope_chain(session: Session, scope: ResearchGraphNodeRecord) -> tuple[str, ...]:
    if scope.node_type == GraphNodeType.QUEST.value:
        if scope.quest_id != scope.node_id or scope.parent_node_id is not None:
            raise ResearchMemoryInvariantError("quest memory scope has invalid hierarchy")
        return (scope.node_id,)
    if scope.node_type == GraphNodeType.PROGRAM.value:
        quest = session.get(ResearchGraphNodeRecord, scope.quest_id)
        if (
            quest is None
            or quest.node_type != GraphNodeType.QUEST.value
            or scope.parent_node_id != quest.node_id
        ):
            raise ResearchMemoryInvariantError("program memory scope has invalid hierarchy")
        return (quest.node_id, scope.node_id)
    if scope.node_type == GraphNodeType.CAMPAIGN.value:
        program = session.get(ResearchGraphNodeRecord, scope.parent_node_id)
        quest = session.get(ResearchGraphNodeRecord, scope.quest_id)
        if (
            program is None
            or program.node_type != GraphNodeType.PROGRAM.value
            or program.quest_id != scope.quest_id
            or quest is None
            or quest.node_type != GraphNodeType.QUEST.value
            or program.parent_node_id != quest.node_id
        ):
            raise ResearchMemoryInvariantError("campaign memory scope has invalid hierarchy")
        return (quest.node_id, program.node_id, scope.node_id)
    raise ResearchMemoryInvariantError(f"unknown memory scope node type: {scope.node_type}")


def _binding_rows(
    session: Session, fact_ids: Iterable[str]
) -> dict[str, tuple[MemoryTaskBindingSpec, ...]]:
    ids = tuple(sorted(set(fact_ids)))
    if not ids:
        return {}
    rows = session.scalars(
        select(ResearchMemoryTaskBindingRecord)
        .where(ResearchMemoryTaskBindingRecord.fact_id.in_(ids))
        .order_by(
            ResearchMemoryTaskBindingRecord.fact_id,
            ResearchMemoryTaskBindingRecord.task_key,
        )
    ).all()
    grouped: dict[str, list[MemoryTaskBindingSpec]] = {fact_id: [] for fact_id in ids}
    for row in rows:
        grouped.setdefault(row.fact_id, []).append(
            MemoryTaskBindingSpec(task_key=row.task_key, context_role=row.context_role)
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _fact_snapshot(
    row: ResearchMemoryFactRecord,
    bindings: tuple[MemoryTaskBindingSpec, ...],
) -> MemoryFactSnapshot:
    try:
        spec = ResearchMemoryFactSpec(
            scope_node_id=row.scope_node_id,
            kind=row.kind,
            statement=row.statement,
            detail=row.detail_json,
            task_bindings=bindings,
            sources=tuple(MemorySourceRef.model_validate(item) for item in row.source_refs_json),
        )
    except Exception as exc:
        raise ResearchMemoryInvariantError(f"invalid research memory fact: {row.fact_id}") from exc
    if spec.fact_id != row.fact_id or spec.fact_sha256 != row.fact_sha256:
        raise ResearchMemoryInvariantError(f"research memory fact identity changed: {row.fact_id}")
    return MemoryFactSnapshot(
        fact_id=row.fact_id,
        quest_id=row.quest_id,
        scope_node_id=row.scope_node_id,
        kind=row.kind,
        statement=row.statement,
        detail=row.detail_json,
        task_bindings=bindings,
        sources=spec.sources,
        fact_sha256=row.fact_sha256,
        command_id=row.command_id,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _effective_role(
    bindings: tuple[MemoryTaskBindingSpec, ...], task_key: str
) -> MemoryContextRole:
    matching = [item.context_role for item in bindings if item.task_key in {"*", task_key}]
    if not matching:
        raise ResearchMemoryInvariantError("eligible fact has no matching task binding")
    if MemoryContextRole.REQUIRED in matching:
        return MemoryContextRole.REQUIRED
    return MemoryContextRole.SUPPORTING


def _disposition(fact: MemoryFactSnapshot, role: MemoryContextRole) -> MemoryCoverageDisposition:
    if fact.kind in NON_DROPPABLE_FACT_KINDS:
        return MemoryCoverageDisposition.EXACT_NON_DROPPABLE
    if role == MemoryContextRole.REQUIRED:
        return MemoryCoverageDisposition.EXACT_REQUIRED
    return MemoryCoverageDisposition.SUMMARY


def _member(fact: MemoryFactSnapshot, task_key: str) -> MemoryCompactionMember:
    return MemoryCompactionMember(
        fact_id=fact.fact_id,
        fact_sha256=fact.fact_sha256,
        kind=fact.kind,
        disposition=_disposition(fact, _effective_role(fact.task_bindings, task_key)),
    )


def _latest_compaction_row(
    session: Session,
    *,
    scope_node_id: str,
    task_key: str,
) -> ResearchMemoryCompactionRecord | None:
    rows = session.scalars(
        select(ResearchMemoryCompactionRecord).where(
            ResearchMemoryCompactionRecord.scope_node_id == scope_node_id,
            ResearchMemoryCompactionRecord.task_key == task_key,
        )
    ).all()
    if not rows:
        return None
    by_id = {row.compaction_id: row for row in rows}
    if len(by_id) != len(rows):  # pragma: no cover - primary key already protects this
        raise ResearchMemoryInvariantError("memory compaction ledger has duplicate identities")
    referenced_parents = {
        row.parent_compaction_id for row in rows if row.parent_compaction_id is not None
    }
    if not referenced_parents.issubset(by_id):
        raise ResearchMemoryInvariantError("memory compaction parent left its scope/task")
    leaves = sorted(set(by_id) - referenced_parents)
    if len(leaves) != 1:
        raise ResearchMemoryInvariantError("memory compaction history is not one linear chain")
    return by_id[leaves[0]]


def build_research_memory_snapshot(
    *,
    quest_id: str,
    scope_node_id: str,
    task_key: str,
    facts: Iterable[MemoryFactSnapshot],
    compactions: Iterable[MemoryCompactionSnapshot],
) -> ResearchMemorySnapshot:
    """Canonicalize an independently verified projection, regardless of input order."""

    fact_items = tuple(sorted(facts, key=lambda item: item.fact_id))
    if len({item.fact_id for item in fact_items}) != len(fact_items):
        raise ResearchMemoryInvariantError("memory snapshot contains duplicate facts")
    compaction_items = tuple(
        sorted(compactions, key=lambda item: (item.created_at, item.compaction_id))
    )
    if len({item.compaction_id for item in compaction_items}) != len(compaction_items):
        raise ResearchMemoryInvariantError("memory snapshot contains duplicate compactions")
    if compaction_items:
        ids = {item.compaction_id for item in compaction_items}
        parent_ids = {
            item.parent_compaction_id
            for item in compaction_items
            if item.parent_compaction_id is not None
        }
        if not parent_ids.issubset(ids):
            raise ResearchMemoryInvariantError("memory snapshot parent left its projection")
        leaves = sorted(ids - parent_ids)
        if len(leaves) != 1:
            raise ResearchMemoryInvariantError("memory snapshot is not one compaction chain")
        latest = leaves[0]
    else:
        latest = None
    payload: dict[str, Any] = {
        "schema_version": 1,
        "quest_id": quest_id,
        "scope_node_id": scope_node_id,
        "task_key": task_key,
        "facts": fact_items,
        "compactions": compaction_items,
        "latest_compaction_id": latest,
        "rebuilt_at": None,
    }
    projection = {
        key: [item.model_dump(mode="json") for item in value] if isinstance(value, tuple) else value
        for key, value in payload.items()
        if key != "rebuilt_at"
    }
    payload["memory_sha256"] = research_memory_snapshot_sha256(projection)
    return ResearchMemorySnapshot(**payload)


class ResearchMemoryStore:
    """Authoritative memory ledger, artifact compactor, and minimal-context assembler."""

    def __init__(self, *, archive_root: Path | None = None) -> None:
        self._commands = ScientificTransitionStore()
        self._archive = ScientificMemoryArchive(archive_root or research_memory_archive_dir())

    @staticmethod
    def _session():
        from aletheia.db import session_scope

        return session_scope()

    def _execute(
        self,
        command: ScientificCommandSpec,
        apply,
        *,
        object_id: str,
        now: datetime | None = None,
    ) -> MemoryMutationReceipt:
        try:
            receipt = self._commands.execute(command, apply, now=now)
        except IntegrityError as exc:
            raise ResearchMemoryConflict(
                "research memory identity already has different content"
            ) from exc
        return MemoryMutationReceipt(object_id=object_id, command=receipt)

    @staticmethod
    def _verify_command(
        session: Session,
        *,
        command_id: str,
        object_id: str,
        command_type: ScientificCommandType,
        aggregate_type: str,
    ) -> ScientificCommandReceipt:
        row = session.get(ScientificCommandRecord, command_id)
        if row is None:
            raise ResearchMemoryInvariantError(f"research memory command is missing: {command_id}")
        try:
            ScientificTransitionStore._verify_event(session, row)
            receipt = ScientificTransitionStore._receipt(row, created=False)
        except Exception as exc:
            raise ResearchMemoryInvariantError(
                f"research memory command receipt is invalid: {command_id}"
            ) from exc
        if (
            row.command_type != command_type.value
            or row.aggregate_type != aggregate_type
            or row.aggregate_id != object_id
            or receipt.result.get("object_id") != object_id
        ):
            raise ResearchMemoryInvariantError(
                f"research memory command is rebound away from {object_id}: {command_id}"
            )
        return receipt

    def register_fact(
        self,
        spec: ResearchMemoryFactSpec,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> MemoryMutationReceipt:
        spec = ResearchMemoryFactSpec.model_validate(spec.model_dump(mode="python"))
        fact_id = spec.fact_id
        command = _scientific_command(
            operation="register_fact",
            object_id=fact_id,
            payload={
                "fact_id": fact_id,
                "fact": spec.model_dump(mode="json"),
                "fact_sha256": spec.fact_sha256,
            },
            context=context,
            command_type=ScientificCommandType.RESEARCH_MEMORY_MUTATION,
            aggregate_type="research_memory",
            event_type="research_memory_fact_registered",
        )

        def apply(session: Session) -> ScientificMutation:
            scope = _scope_or_error(session, spec.scope_node_id)
            _lock_quest(session, scope.quest_id)
            if session.get(ResearchMemoryFactRecord, fact_id) is not None:
                raise ResearchMemoryConflict(f"research memory fact already exists: {fact_id}")
            assert command.command_id is not None
            session.add(
                ResearchMemoryFactRecord(
                    fact_id=fact_id,
                    quest_id=scope.quest_id,
                    scope_node_id=scope.node_id,
                    kind=spec.kind.value,
                    statement=spec.statement,
                    detail_json=spec.detail,
                    source_refs_json=[item.model_dump(mode="json") for item in spec.sources],
                    fact_sha256=spec.fact_sha256,
                    command_id=command.command_id,
                    created_by=context.principal,
                    created_at=now,
                )
            )
            session.flush()
            for binding in spec.task_bindings:
                session.add(
                    ResearchMemoryTaskBindingRecord(
                        fact_id=fact_id,
                        task_key=binding.task_key,
                        context_role=binding.context_role.value,
                        command_id=command.command_id,
                    )
                )
            return ScientificMutation(
                result={"kind": "memory_fact", "object_id": fact_id},
                event_projection={
                    "quest_id": scope.quest_id,
                    "scope_node_id": scope.node_id,
                    "fact_id": fact_id,
                    "fact_kind": spec.kind.value,
                    "fact_sha256": spec.fact_sha256,
                    "task_keys": [item.task_key for item in spec.task_bindings],
                },
            )

        return self._execute(command, apply, object_id=fact_id, now=now)

    @staticmethod
    def _eligible_rows(
        session: Session,
        *,
        scope: ResearchGraphNodeRecord,
        task_key: str,
    ) -> tuple[list[ResearchMemoryFactRecord], dict[str, tuple[MemoryTaskBindingSpec, ...]]]:
        chain = _scope_chain(session, scope)
        rows = (
            session.scalars(
                select(ResearchMemoryFactRecord)
                .join(
                    ResearchMemoryTaskBindingRecord,
                    ResearchMemoryTaskBindingRecord.fact_id == ResearchMemoryFactRecord.fact_id,
                )
                .where(
                    ResearchMemoryFactRecord.scope_node_id.in_(chain),
                    or_(
                        ResearchMemoryTaskBindingRecord.task_key == task_key,
                        ResearchMemoryTaskBindingRecord.task_key == "*",
                    ),
                )
                .order_by(ResearchMemoryFactRecord.fact_id)
            )
            .unique()
            .all()
        )
        bindings = _binding_rows(session, (row.fact_id for row in rows))
        return rows, bindings

    def _eligible_facts(
        self,
        session: Session,
        *,
        scope: ResearchGraphNodeRecord,
        task_key: str,
        verify_commands: bool,
    ) -> tuple[MemoryFactSnapshot, ...]:
        rows, bindings = self._eligible_rows(session, scope=scope, task_key=task_key)
        snapshots: list[MemoryFactSnapshot] = []
        for row in rows:
            snapshot = _fact_snapshot(row, bindings.get(row.fact_id, ()))
            if row.quest_id != scope.quest_id:
                raise ResearchMemoryInvariantError(f"memory fact left its quest: {row.fact_id}")
            if verify_commands:
                self._verify_command(
                    session,
                    command_id=row.command_id,
                    object_id=row.fact_id,
                    command_type=ScientificCommandType.RESEARCH_MEMORY_MUTATION,
                    aggregate_type="research_memory",
                )
            snapshots.append(snapshot)
        return tuple(sorted(snapshots, key=lambda item: item.fact_id))

    def eligible_facts(self, scope_node_id: str, task_key: str) -> tuple[MemoryFactSnapshot, ...]:
        task_key = _task_key(task_key)
        with self._session() as session:
            scope = _scope_or_error(session, scope_node_id)
            return self._eligible_facts(
                session, scope=scope, task_key=task_key, verify_commands=True
            )

    def compact(
        self,
        *,
        scope_node_id: str,
        task_key: str,
        draft: MemorySummaryDraft,
        context: GraphCommandContext,
        parent_compaction_id: str | None = None,
        now: datetime | None = None,
    ) -> MemoryMutationReceipt:
        task_key = _task_key(task_key)
        draft = MemorySummaryDraft.model_validate(draft.model_dump(mode="python"))
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("memory compaction timestamp must be timezone-aware")

        with self._session() as session:
            scope = _scope_or_error(session, scope_node_id)
            facts = self._eligible_facts(
                session, scope=scope, task_key=task_key, verify_commands=True
            )
            if not facts:
                raise ResearchMemoryConflict("cannot compact an empty task memory")
            latest: ResearchMemoryCompactionRecord | None = None
            if parent_compaction_id is None:
                latest = _latest_compaction_row(
                    session,
                    scope_node_id=scope.node_id,
                    task_key=task_key,
                )
            elif session.get(ResearchMemoryCompactionRecord, parent_compaction_id) is None:
                raise ResearchMemoryNotFound(
                    f"parent memory compaction not found: {parent_compaction_id}"
                )
            quest_id = scope.quest_id

        members = tuple(
            sorted((_member(fact, task_key) for fact in facts), key=lambda item: item.fact_id)
        )
        expected_ids = tuple(item.fact_id for item in members)
        if draft.covered_fact_ids != expected_ids:
            missing = sorted(set(expected_ids) - set(draft.covered_fact_ids))
            extra = sorted(set(draft.covered_fact_ids) - set(expected_ids))
            raise ResearchMemoryConflict(
                f"summary coverage must exactly match eligible facts; missing={missing}, extra={extra}"
            )
        manifest_sha256 = source_manifest_sha256(members)
        if parent_compaction_id is None and latest is not None:
            # An exact redelivery must reconstruct the original parent, not make the just-written
            # compaction its own successor.  A genuinely new draft/snapshot extends the chain.
            if (
                latest.source_manifest_sha256 == manifest_sha256
                and latest.producer_draft_sha256 == draft.draft_sha256
            ):
                parent_compaction_id = latest.parent_compaction_id
            else:
                parent_compaction_id = latest.compaction_id
        object_id = compaction_id(
            scope_node_id=scope_node_id,
            task_key=task_key,
            source_manifest_sha256=manifest_sha256,
            draft_sha256=content_sha256(
                {
                    "draft_sha256": draft.draft_sha256,
                    "parent_compaction_id": parent_compaction_id,
                }
            ),
        )
        exact_ids = {
            item.fact_id
            for item in members
            if item.disposition != MemoryCoverageDisposition.SUMMARY
        }
        exact_facts = tuple(fact for fact in facts if fact.fact_id in exact_ids)
        artifact = MemoryCompactionArtifact(
            compaction_id=object_id,
            quest_id=quest_id,
            scope_node_id=scope_node_id,
            task_key=task_key,
            parent_compaction_id=parent_compaction_id,
            source_manifest_sha256=manifest_sha256,
            members=members,
            summary_text=draft.summary_text,
            exact_facts=exact_facts,
            producer_provider=draft.producer_provider,
            producer_model=draft.producer_model,
            producer_prompt_sha256=draft.prompt_sha256,
            producer_draft_sha256=draft.draft_sha256,
        )
        artifact_receipt = self._archive.store(artifact, archived_at=observed_at)
        command = _scientific_command(
            operation="compact",
            object_id=object_id,
            payload={
                "artifact": artifact.model_dump(mode="json"),
                # Archive time is operational metadata.  Excluding it makes an exact redelivery
                # reconstruct byte-identical command input even when the retry occurs later.
                "artifact_receipt": artifact_receipt.model_dump(
                    mode="json", exclude={"archived_at"}
                ),
            },
            context=context,
            command_type=ScientificCommandType.RESEARCH_MEMORY_MUTATION,
            aggregate_type="research_memory",
            event_type="research_memory_compacted",
        )

        def apply(session: Session) -> ScientificMutation:
            current_scope = _scope_or_error(session, scope_node_id)
            _lock_quest(session, current_scope.quest_id)
            current_facts = self._eligible_facts(
                session,
                scope=current_scope,
                task_key=task_key,
                verify_commands=True,
            )
            current_members = tuple(
                sorted(
                    (_member(fact, task_key) for fact in current_facts),
                    key=lambda item: item.fact_id,
                )
            )
            if current_members != members:
                raise ResearchMemoryStale(
                    "eligible task memory changed while compaction was being committed"
                )
            current_latest = _latest_compaction_row(
                session,
                scope_node_id=scope_node_id,
                task_key=task_key,
            )
            if (current_latest is None and parent_compaction_id is not None) or (
                current_latest is not None and current_latest.compaction_id != parent_compaction_id
            ):
                raise ResearchMemoryStale(
                    "memory compaction parent changed while compaction was being committed"
                )
            if parent_compaction_id is not None:
                parent = session.get(ResearchMemoryCompactionRecord, parent_compaction_id)
                if (
                    parent is None
                    or parent.scope_node_id != scope_node_id
                    or parent.task_key != task_key
                ):
                    raise ResearchMemoryConflict("memory compaction parent has another scope/task")
                parent_ids = set(
                    session.scalars(
                        select(ResearchMemoryCompactionMemberRecord.fact_id).where(
                            ResearchMemoryCompactionMemberRecord.compaction_id
                            == parent_compaction_id
                        )
                    ).all()
                )
                if not parent_ids.issubset(set(expected_ids)):
                    raise ResearchMemoryConflict(
                        "memory compaction cannot forget facts covered by its parent"
                    )
            assert command.command_id is not None
            session.add(
                ResearchMemoryCompactionRecord(
                    compaction_id=object_id,
                    quest_id=current_scope.quest_id,
                    scope_node_id=scope_node_id,
                    task_key=task_key,
                    parent_compaction_id=parent_compaction_id,
                    source_manifest_sha256=manifest_sha256,
                    source_count=len(members),
                    exact_count=len(exact_facts),
                    summary_text=draft.summary_text,
                    summary_sha256=content_sha256(draft.summary_text),
                    producer_provider=draft.producer_provider,
                    producer_model=draft.producer_model,
                    producer_prompt_sha256=draft.prompt_sha256,
                    producer_draft_sha256=draft.draft_sha256,
                    artifact_sha256=artifact_receipt.artifact_sha256,
                    artifact_bytes=artifact_receipt.artifact_bytes,
                    artifact_relative_path=artifact_receipt.relative_path,
                    artifact_object_sha256=artifact_receipt.object_sha256,
                    artifact_receipt_sha256=artifact_receipt.receipt_sha256,
                    command_id=command.command_id,
                    created_at=observed_at,
                )
            )
            session.flush()
            for member in members:
                session.add(
                    ResearchMemoryCompactionMemberRecord(
                        compaction_id=object_id,
                        fact_id=member.fact_id,
                        fact_sha256=member.fact_sha256,
                        fact_kind=member.kind.value,
                        disposition=member.disposition.value,
                    )
                )
            return ScientificMutation(
                result={"kind": "memory_compaction", "object_id": object_id},
                event_projection={
                    "quest_id": current_scope.quest_id,
                    "scope_node_id": scope_node_id,
                    "task_key": task_key,
                    "compaction_id": object_id,
                    "source_manifest_sha256": manifest_sha256,
                    "source_count": len(members),
                    "exact_count": len(exact_facts),
                    "artifact_sha256": artifact_receipt.artifact_sha256,
                    "artifact_receipt_sha256": artifact_receipt.receipt_sha256,
                },
            )

        return self._execute(command, apply, object_id=object_id, now=observed_at)

    @staticmethod
    def _artifact_receipt(row: ResearchMemoryCompactionRecord) -> MemoryArtifactReceipt:
        receipt = MemoryArtifactReceipt(
            artifact_sha256=row.artifact_sha256,
            artifact_bytes=row.artifact_bytes,
            relative_path=row.artifact_relative_path,
            object_sha256=row.artifact_object_sha256,
            archived_at=row.created_at,
        )
        if receipt.receipt_sha256 != row.artifact_receipt_sha256:
            raise ResearchMemoryInvariantError(
                f"memory artifact receipt changed: {row.compaction_id}"
            )
        return receipt

    def recover_compaction(self, compaction_id_value: str) -> MemoryCompactionArtifact:
        with self._session() as session:
            row = session.get(ResearchMemoryCompactionRecord, compaction_id_value)
            if row is None:
                raise ResearchMemoryNotFound(
                    f"research memory compaction not found: {compaction_id_value}"
                )
            receipt = self._artifact_receipt(row)
        try:
            artifact = self._archive.read(receipt)
        except Exception as exc:
            raise ResearchMemoryInvariantError(
                f"research memory artifact is missing or corrupt: {compaction_id_value}"
            ) from exc
        if artifact.compaction_id != compaction_id_value:
            raise ResearchMemoryInvariantError("memory artifact is rebound to another compaction")
        return artifact

    def rebuild_memory(self, scope_node_id: str, task_key: str) -> ResearchMemorySnapshot:
        task_key = _task_key(task_key)
        with self._session() as session:
            scope = _scope_or_error(session, scope_node_id)
            facts = self._eligible_facts(
                session, scope=scope, task_key=task_key, verify_commands=True
            )
            fact_by_id = {item.fact_id: item for item in facts}
            rows = session.scalars(
                select(ResearchMemoryCompactionRecord)
                .where(
                    ResearchMemoryCompactionRecord.scope_node_id == scope_node_id,
                    ResearchMemoryCompactionRecord.task_key == task_key,
                )
                .order_by(
                    ResearchMemoryCompactionRecord.created_at,
                    ResearchMemoryCompactionRecord.compaction_id,
                )
            ).all()
            snapshots: list[MemoryCompactionSnapshot] = []
            for row in rows:
                member_rows = session.scalars(
                    select(ResearchMemoryCompactionMemberRecord)
                    .where(ResearchMemoryCompactionMemberRecord.compaction_id == row.compaction_id)
                    .order_by(ResearchMemoryCompactionMemberRecord.fact_id)
                ).all()
                members = tuple(
                    MemoryCompactionMember(
                        fact_id=item.fact_id,
                        fact_sha256=item.fact_sha256,
                        kind=item.fact_kind,
                        disposition=item.disposition,
                    )
                    for item in member_rows
                )
                if (
                    len(members) != row.source_count
                    or source_manifest_sha256(members) != row.source_manifest_sha256
                ):
                    raise ResearchMemoryInvariantError(
                        f"memory compaction membership changed: {row.compaction_id}"
                    )
                if (
                    sum(item.disposition != MemoryCoverageDisposition.SUMMARY for item in members)
                    != row.exact_count
                ):
                    raise ResearchMemoryInvariantError(
                        f"memory compaction exact count changed: {row.compaction_id}"
                    )
                for member in members:
                    fact = fact_by_id.get(member.fact_id)
                    if fact is None:
                        raise ResearchMemoryInvariantError(
                            f"memory compaction references an ineligible fact: {member.fact_id}"
                        )
                    if member != _member(fact, task_key):
                        raise ResearchMemoryInvariantError(
                            f"memory compaction member identity changed: {member.fact_id}"
                        )
                if row.parent_compaction_id is not None:
                    parent = session.get(ResearchMemoryCompactionRecord, row.parent_compaction_id)
                    if (
                        parent is None
                        or parent.scope_node_id != scope_node_id
                        or parent.task_key != task_key
                    ):
                        raise ResearchMemoryInvariantError(
                            f"memory compaction parent left its task: {row.compaction_id}"
                        )
                    parent_ids = set(
                        session.scalars(
                            select(ResearchMemoryCompactionMemberRecord.fact_id).where(
                                ResearchMemoryCompactionMemberRecord.compaction_id
                                == row.parent_compaction_id
                            )
                        ).all()
                    )
                    if not parent_ids.issubset({item.fact_id for item in members}):
                        raise ResearchMemoryInvariantError(
                            f"memory compaction parent chain forgets facts: {row.compaction_id}"
                        )
                if row.summary_sha256 != content_sha256(row.summary_text):
                    raise ResearchMemoryInvariantError(
                        f"memory compaction summary changed: {row.compaction_id}"
                    )
                self._verify_command(
                    session,
                    command_id=row.command_id,
                    object_id=row.compaction_id,
                    command_type=ScientificCommandType.RESEARCH_MEMORY_MUTATION,
                    aggregate_type="research_memory",
                )
                receipt = self._artifact_receipt(row)
                try:
                    artifact = self._archive.read(receipt)
                except Exception as exc:
                    raise ResearchMemoryInvariantError(
                        f"memory compaction artifact is missing or corrupt: {row.compaction_id}"
                    ) from exc
                expected_exact = tuple(
                    fact_by_id[item.fact_id]
                    for item in members
                    if item.disposition != MemoryCoverageDisposition.SUMMARY
                )
                if (
                    artifact.compaction_id != row.compaction_id
                    or artifact.quest_id != row.quest_id
                    or artifact.scope_node_id != row.scope_node_id
                    or artifact.task_key != row.task_key
                    or artifact.parent_compaction_id != row.parent_compaction_id
                    or artifact.source_manifest_sha256 != row.source_manifest_sha256
                    or artifact.members != members
                    or artifact.summary_text != row.summary_text
                    or artifact.exact_facts != expected_exact
                    or artifact.producer_provider != row.producer_provider
                    or artifact.producer_model != row.producer_model
                    or artifact.producer_prompt_sha256 != row.producer_prompt_sha256
                    or artifact.producer_draft_sha256 != row.producer_draft_sha256
                ):
                    raise ResearchMemoryInvariantError(
                        f"memory compaction artifact differs from ledger: {row.compaction_id}"
                    )
                snapshots.append(
                    MemoryCompactionSnapshot(
                        compaction_id=row.compaction_id,
                        quest_id=row.quest_id,
                        scope_node_id=row.scope_node_id,
                        task_key=row.task_key,
                        parent_compaction_id=row.parent_compaction_id,
                        source_manifest_sha256=row.source_manifest_sha256,
                        members=members,
                        summary_sha256=row.summary_sha256,
                        producer_provider=row.producer_provider,
                        producer_model=row.producer_model,
                        producer_prompt_sha256=row.producer_prompt_sha256,
                        producer_draft_sha256=row.producer_draft_sha256,
                        artifact=receipt,
                        command_id=row.command_id,
                        created_at=row.created_at,
                    )
                )
            return build_research_memory_snapshot(
                quest_id=scope.quest_id,
                scope_node_id=scope.node_id,
                task_key=task_key,
                facts=facts,
                compactions=snapshots,
            )

    def build_task_context(
        self,
        request: TaskContextRequest,
        context: GraphCommandContext,
        *,
        now: datetime | None = None,
    ) -> TaskContextReceipt:
        request = TaskContextRequest.model_validate(request.model_dump(mode="python"))
        snapshot = self.rebuild_memory(request.scope_node_id, request.task_key)
        if not snapshot.compactions:
            raise ResearchMemoryStale("task memory has no committed compaction")
        compaction_snapshot = (
            next(
                (
                    item
                    for item in snapshot.compactions
                    if item.compaction_id == request.compaction_id
                ),
                None,
            )
            if request.compaction_id is not None
            else next(
                item
                for item in snapshot.compactions
                if item.compaction_id == snapshot.latest_compaction_id
            )
        )
        if compaction_snapshot is None:
            raise ResearchMemoryNotFound(
                f"memory compaction not found for task: {request.compaction_id}"
            )
        if compaction_snapshot.compaction_id != snapshot.latest_compaction_id:
            raise ResearchMemoryStale("task memory compaction is superseded by a newer compaction")
        current_members = tuple(_member(fact, request.task_key) for fact in snapshot.facts)
        if compaction_snapshot.members != current_members:
            raise ResearchMemoryStale(
                "task memory compaction is stale; new or rebound facts require recompaction"
            )
        artifact = self.recover_compaction(compaction_snapshot.compaction_id)
        prompt_text = render_task_context(
            quest_id=snapshot.quest_id,
            scope_node_id=snapshot.scope_node_id,
            task_key=snapshot.task_key,
            summary_text=artifact.summary_text,
            exact_facts=artifact.exact_facts,
        )
        if len(prompt_text) > request.max_chars:
            raise ResearchMemoryContextOverflow(
                "required/non-droppable scientific memory exceeds the prompt context budget"
            )
        payload_without_hash: dict[str, Any] = {
            "schema_version": 1,
            "quest_id": snapshot.quest_id,
            "scope_node_id": snapshot.scope_node_id,
            "task_key": snapshot.task_key,
            "compaction_id": artifact.compaction_id,
            "compaction_artifact_sha256": compaction_snapshot.artifact.artifact_sha256,
            "source_manifest_sha256": artifact.source_manifest_sha256,
            "source_fact_ids": tuple(item.fact_id for item in current_members),
            "summary_text": artifact.summary_text,
            "exact_facts": artifact.exact_facts,
            "prompt_text": prompt_text,
        }
        provider_neutral = {
            key: [item.model_dump(mode="json") for item in value]
            if key == "exact_facts"
            else list(value)
            if key == "source_fact_ids"
            else value
            for key, value in payload_without_hash.items()
        }
        payload = TaskContextPayload(
            **payload_without_hash,
            context_sha256=content_sha256(provider_neutral),
        )
        object_id = _record_id(
            "mctx",
            {
                "schema": "aletheia.research_memory_context_receipt.v1",
                "context_sha256": payload.context_sha256,
                "consumer_provider": request.consumer_provider,
                "consumer_model": request.consumer_model,
                "client_idempotency_key": context.idempotency_key,
            },
        )
        selected_manifest = content_sha256(
            [{"fact_id": item.fact_id, "fact_sha256": item.fact_sha256} for item in snapshot.facts]
        )
        command = _scientific_command(
            operation="build_context",
            object_id=object_id,
            payload={
                "request": request.model_dump(mode="json"),
                "context": payload.model_dump(mode="json"),
                "selected_manifest_sha256": selected_manifest,
            },
            context=context,
            command_type=ScientificCommandType.RESEARCH_MEMORY_CONTEXT,
            aggregate_type="research_memory_context",
            event_type="research_memory_context_built",
        )

        def apply(session: Session) -> ScientificMutation:
            scope = _scope_or_error(session, request.scope_node_id)
            _lock_quest(session, scope.quest_id)
            current_facts = self._eligible_facts(
                session,
                scope=scope,
                task_key=request.task_key,
                verify_commands=True,
            )
            if tuple(_member(fact, request.task_key) for fact in current_facts) != current_members:
                raise ResearchMemoryStale(
                    "eligible task memory changed while context was being committed"
                )
            assert command.command_id is not None
            session.add(
                ResearchMemoryContextReceiptRecord(
                    context_receipt_id=object_id,
                    compaction_id=artifact.compaction_id,
                    quest_id=scope.quest_id,
                    scope_node_id=scope.node_id,
                    task_key=request.task_key,
                    consumer_provider=request.consumer_provider,
                    consumer_model=request.consumer_model,
                    max_chars=request.max_chars,
                    prompt_chars=len(prompt_text),
                    selected_manifest_sha256=selected_manifest,
                    context_sha256=payload.context_sha256,
                    payload_json=payload.model_dump(mode="json"),
                    command_id=command.command_id,
                    created_at=now,
                )
            )
            return ScientificMutation(
                result={
                    "kind": "memory_context",
                    "object_id": object_id,
                    "context_sha256": payload.context_sha256,
                },
                event_projection={
                    "quest_id": scope.quest_id,
                    "scope_node_id": scope.node_id,
                    "task_key": request.task_key,
                    "compaction_id": artifact.compaction_id,
                    "context_receipt_id": object_id,
                    "context_sha256": payload.context_sha256,
                    "source_fact_count": len(current_facts),
                    "prompt_chars": len(prompt_text),
                    "consumer_provider": request.consumer_provider,
                    "consumer_model": request.consumer_model,
                },
            )

        mutation = self._execute(command, apply, object_id=object_id, now=now)
        return TaskContextReceipt(
            context_receipt_id=object_id,
            context=payload,
            consumer_provider=request.consumer_provider,
            consumer_model=request.consumer_model,
            max_chars=request.max_chars,
            command=mutation.command,
        )

    def load_task_context(self, context_receipt_id: str) -> TaskContextReceipt:
        """Rehydrate and re-verify a context receipt without contacting its producer."""

        with self._session() as session:
            row = session.get(ResearchMemoryContextReceiptRecord, context_receipt_id)
            if row is None:
                raise ResearchMemoryNotFound(
                    f"research memory context receipt not found: {context_receipt_id}"
                )
            try:
                payload = TaskContextPayload.model_validate(row.payload_json)
            except Exception as exc:
                raise ResearchMemoryInvariantError(
                    f"research memory context payload is invalid: {context_receipt_id}"
                ) from exc
            command = self._verify_command(
                session,
                command_id=row.command_id,
                object_id=row.context_receipt_id,
                command_type=ScientificCommandType.RESEARCH_MEMORY_CONTEXT,
                aggregate_type="research_memory_context",
            )
            if (
                payload.compaction_id != row.compaction_id
                or payload.quest_id != row.quest_id
                or payload.scope_node_id != row.scope_node_id
                or payload.task_key != row.task_key
                or payload.context_sha256 != row.context_sha256
                or len(payload.prompt_text) != row.prompt_chars
                or row.prompt_chars > row.max_chars
            ):
                raise ResearchMemoryInvariantError(
                    f"research memory context receipt changed: {context_receipt_id}"
                )
            consumer_provider = row.consumer_provider
            consumer_model = row.consumer_model
            max_chars = row.max_chars
            selected_manifest = row.selected_manifest_sha256

        snapshot = self.rebuild_memory(payload.scope_node_id, payload.task_key)
        compaction = next(
            (item for item in snapshot.compactions if item.compaction_id == payload.compaction_id),
            None,
        )
        if compaction is None:
            raise ResearchMemoryInvariantError("context compaction left its memory ledger")
        if compaction.compaction_id != snapshot.latest_compaction_id:
            raise ResearchMemoryStale("stored task context is superseded by a newer compaction")
        current_members = tuple(_member(fact, payload.task_key) for fact in snapshot.facts)
        if compaction.members != current_members:
            raise ResearchMemoryStale(
                "stored task context is stale; current scientific memory has changed"
            )
        artifact = self.recover_compaction(compaction.compaction_id)
        expected_prompt = render_task_context(
            quest_id=snapshot.quest_id,
            scope_node_id=snapshot.scope_node_id,
            task_key=snapshot.task_key,
            summary_text=artifact.summary_text,
            exact_facts=artifact.exact_facts,
        )
        expected_selected_manifest = content_sha256(
            [{"fact_id": item.fact_id, "fact_sha256": item.fact_sha256} for item in snapshot.facts]
        )
        if (
            payload.compaction_artifact_sha256 != compaction.artifact.artifact_sha256
            or payload.source_manifest_sha256 != artifact.source_manifest_sha256
            or payload.source_fact_ids != tuple(item.fact_id for item in snapshot.facts)
            or payload.summary_text != artifact.summary_text
            or payload.exact_facts != artifact.exact_facts
            or payload.prompt_text != expected_prompt
            or selected_manifest != expected_selected_manifest
        ):
            raise ResearchMemoryInvariantError(
                f"research memory context cannot be reconstructed: {context_receipt_id}"
            )
        return TaskContextReceipt(
            context_receipt_id=context_receipt_id,
            context=payload,
            consumer_provider=consumer_provider,
            consumer_model=consumer_model,
            max_chars=max_chars,
            command=command,
        )


__all__ = [
    "ResearchMemoryConflict",
    "ResearchMemoryContextOverflow",
    "ResearchMemoryError",
    "ResearchMemoryInvariantError",
    "ResearchMemoryNotFound",
    "ResearchMemoryStale",
    "ResearchMemoryStore",
    "build_research_memory_snapshot",
]
