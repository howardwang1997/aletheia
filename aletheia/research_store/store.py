"""PostgreSQL adapter for the authoritative research-kernel event stream.

The archive boundary is declared here so persisted rows can remain metadata-only.  A concrete CAS
owns object and snapshot bytes; this adapter only verifies and indexes their content identities.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchScopeBinding,
    directly_referenced_object,
    verify_research_command_authorization,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationError,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationTrustRootV1,
    verify_research_authorization_policy,
)
from aletheia.research_kernel.reducer import (
    REDUCER_VERSION,
    ResearchStateGraph,
    empty_state,
    reduce_event,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    EVENT_SCHEMA_VERSION,
    KernelModel,
    KernelObject,
    KernelObjectKind,
    KernelObjectRef,
    ObservationIncorporatedPayload,
    ResearchActionProposal,
    ResearchCharterVersion,
    ResearchEvent,
    StopCommittedPayload,
    StopDirective,
    StopReason,
    canonical_json_bytes,
)
from aletheia.research_store.persistence import (
    ResearchKernelCommandReceiptRecord as _ResearchKernelCommandReceiptRecord,
    ResearchKernelEventRecord as _ResearchKernelEventRecord,
    ResearchKernelObjectRecord as _ResearchKernelObjectRecord,
    ResearchKernelOutboxRecord as _ResearchKernelOutboxRecord,
    ResearchKernelSnapshotRecord as _ResearchKernelSnapshotRecord,
    ResearchQuestAuthorityRecord as _ResearchQuestAuthorityRecord,
    ResearchQuestStreamRecord as _ResearchQuestStreamRecord,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_STORAGE_KEY_PATTERN = r"^sha256/[0-9a-f]{2}/[0-9a-f]{64}$"


class ArchivedObjectMetadata(KernelModel):
    """Frozen DB-safe metadata for one object whose payload already exists in CAS."""

    object_ref: KernelObjectRef
    object_version: int = Field(ge=1)
    object_schema_name: str = Field(min_length=1, max_length=128)
    object_schema_version: int = Field(ge=1)
    canonicalization: Literal["aletheia.canonical_json.v1"] = "aletheia.canonical_json.v1"
    media_type: Literal["application/json"] = "application/json"
    object_size_bytes: int = Field(gt=0)
    storage_key: str = Field(pattern=_STORAGE_KEY_PATTERN)

    @model_validator(mode="after")
    def _storage_is_content_addressed(self) -> "ArchivedObjectMetadata":
        expected = f"sha256/{self.object_ref.object_sha256[:2]}/{self.object_ref.object_sha256}"
        if self.storage_key != expected:
            raise ValueError("object storage key does not match its content identity")
        return self


class ArchivedKernelObject(KernelModel):
    """A verified object payload returned across the read-only CAS boundary."""

    metadata: ArchivedObjectMetadata
    payload: KernelObject

    @model_validator(mode="after")
    def _payload_matches_metadata(self) -> "ArchivedKernelObject":
        if self.payload.object_ref != self.metadata.object_ref:
            raise ValueError("archived object payload does not match its metadata reference")
        payload_bytes = canonical_json_bytes(self.payload)
        if len(payload_bytes) != self.metadata.object_size_bytes:
            raise ValueError("archived object payload size does not match its metadata")
        if self.payload.schema_name != self.metadata.object_schema_name:
            raise ValueError("archived object schema name does not match its metadata")
        if self.payload.schema_version != self.metadata.object_schema_version:
            raise ValueError("archived object schema version does not match its metadata")
        version = int(getattr(self.payload, "version", 1))
        if version != self.metadata.object_version:
            raise ValueError("archived object logical version does not match its metadata")
        return self


class ArchivedSnapshotMetadata(KernelModel):
    """Frozen DB-safe metadata for one replay-derived canonical graph snapshot."""

    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    stream_version: int = Field(ge=1)
    snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonicalization: Literal["aletheia.canonical_json.v1"] = "aletheia.canonical_json.v1"
    media_type: Literal["application/json"] = "application/json"
    snapshot_size_bytes: int = Field(gt=0)
    storage_key: str = Field(pattern=_STORAGE_KEY_PATTERN)

    @model_validator(mode="after")
    def _storage_is_content_addressed(self) -> "ArchivedSnapshotMetadata":
        expected = f"sha256/{self.snapshot_sha256[:2]}/{self.snapshot_sha256}"
        if self.storage_key != expected:
            raise ValueError("snapshot storage key does not match its content identity")
        return self


class ResearchObjectArchive(Protocol):
    """Minimal CAS port required by the authoritative store and replay audit."""

    def load_object(self, ref: KernelObjectRef) -> ArchivedKernelObject:
        """Load and revalidate the exact typed object version named by ``ref``."""

    def archive_snapshot(
        self,
        *,
        quest_id: str,
        stream_version: int,
        snapshot_sha256: str,
        payload: bytes,
    ) -> ArchivedSnapshotMetadata:
        """Write canonical snapshot bytes once and return metadata for the database transaction."""

    def load_snapshot(self, metadata: ArchivedSnapshotMetadata) -> bytes:
        """Read the exact snapshot bytes named by immutable metadata."""


class ResearchStoreError(RuntimeError):
    """Base error for an authoritative store contract or persisted invariant violation."""


class UncommittedProposalError(ResearchStoreError):
    """A model proposal was presented to the authority-only write boundary."""


class ResearchIdempotencyConflict(ResearchStoreError):
    """An idempotency or source-event identity was rebound to another request."""


class ResearchVersionConflict(ResearchStoreError):
    """A command expected a different Quest stream version."""


class ResearchOutboxConflict(ResearchStoreError):
    """An outbox publish request expected another immutable delivery identity or state."""


class ResearchQuestNotFound(ResearchStoreError):
    """No authoritative stream exists for the requested Quest."""


class ResearchStoreInvariantError(ResearchStoreError):
    """Persisted event, command, CAS, head, snapshot, or outbox custody is inconsistent."""


class ResearchCommandReceipt(KernelModel):
    """Immutable proof returned for a newly committed or exactly replayed command."""

    command_id: str
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    idempotency_key: str
    source_event_key: str | None = None
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_stream_version: int = Field(ge=0)
    expected_tail_event_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    result_stream_version: int = Field(ge=1)
    result_event_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_event_id: str
    result_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    outbox_id: str
    principal_id: str
    authorization_trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorization_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    committed_at: AwareDatetime
    created: bool


class ResearchKernelOutboxIdentity(KernelModel):
    """Immutable routing identity exposed to operational dispatchers."""

    outbox_id: str
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    sequence: int = Field(ge=1)
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    topic: Literal["research_kernel.event.v1"] = "research_kernel.event.v1"
    delivery_key: str
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _identity_is_exact(self) -> "ResearchKernelOutboxIdentity":
        if self.outbox_id != f"rko_{self.event_sha256[:32]}":
            raise ValueError("research outbox id differs from its event identity")
        if self.delivery_key != f"{self.quest_id}:{self.sequence}":
            raise ValueError("research outbox delivery key differs from its Quest sequence")
        if self.payload_sha256 != self.event_sha256:
            raise ValueError("research outbox payload identity differs from its event")
        return self


class ResearchKernelOutboxItem(ResearchKernelOutboxIdentity):
    """Closed dispatcher view of one Kernel outbox row, never an ORM record."""

    delivery_status: Literal["pending", "delivering", "published"]
    delivery_attempts: int = Field(ge=0)
    available_at: AwareDatetime
    last_attempt_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _delivery_state_is_consistent(self) -> "ResearchKernelOutboxItem":
        if (self.delivery_status == "published") != (self.published_at is not None):
            raise ValueError("research outbox published state differs from its timestamp")
        return self

    @property
    def identity(self) -> ResearchKernelOutboxIdentity:
        """Return the immutable compare-and-set identity used when publishing."""

        return ResearchKernelOutboxIdentity.model_validate(
            self.model_dump(
                mode="python",
                include={
                    "outbox_id",
                    "quest_id",
                    "sequence",
                    "event_sha256",
                    "topic",
                    "delivery_key",
                    "payload_sha256",
                },
            )
        )


class ResearchReplayAudit(KernelModel):
    """A replayed graph plus the exact event/snapshot custody that was verified."""

    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    scope_binding: ResearchScopeBinding
    events: tuple[ResearchEvent, ...]
    state: ResearchStateGraph
    verified_snapshot_sha256s: tuple[str, ...]


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _transaction_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if observed is None:  # pragma: no cover - PostgreSQL always returns clock_timestamp()
        raise ResearchStoreInvariantError("PostgreSQL did not provide a trusted authorization time")
    return _aware(observed, label="database authorization linearization timestamp")


def _json(model: KernelModel) -> dict[str, object]:
    return model.model_dump(mode="json", exclude_none=True)


def _registered_quest_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValueError("pending outbox reads require at least one registered Quest")
    if len(values) > 5_000:
        raise ValueError("pending outbox reads accept at most 5000 registered Quests")
    if values != tuple(sorted(set(values))):
        raise ValueError("registered Quest ids must be unique and canonically ordered")
    if any(re.fullmatch(_QUEST_ID_PATTERN, value) is None for value in values):
        raise ValueError("registered Quest id is invalid")
    return values


def _outbox_item(row: _ResearchKernelOutboxRecord) -> ResearchKernelOutboxItem:
    try:
        return ResearchKernelOutboxItem(
            outbox_id=row.outbox_id,
            quest_id=row.quest_id,
            sequence=row.sequence,
            event_sha256=row.event_sha256,
            topic=row.topic,
            delivery_key=row.delivery_key,
            payload_sha256=row.payload_sha256,
            delivery_status=row.delivery_status,
            delivery_attempts=row.delivery_attempts,
            available_at=row.available_at,
            last_attempt_at=row.last_attempt_at,
            published_at=row.published_at,
            created_at=row.created_at,
        )
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted research outbox row is invalid") from exc


def _scope_from_head(head: _ResearchQuestStreamRecord) -> ResearchScopeBinding:
    try:
        binding = ResearchScopeBinding(
            quest_id=head.quest_id,
            program_id=head.program_id,
            campaign_id=head.campaign_id,
        )
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted Quest scope binding is invalid") from exc
    if binding.binding_sha256 != head.scope_binding_sha256:
        raise ResearchStoreInvariantError("persisted Quest scope binding hash changed")
    return binding


def _policy_from_head(
    head: _ResearchQuestStreamRecord,
    *,
    trust_root: ResearchAuthorizationTrustRootV1,
    expected_policy: ResearchAuthorizationPolicyV1 | None = None,
) -> ResearchAuthorizationPolicyV1:
    """Reconstruct and verify the stream-frozen fixed policy from authoritative JSON."""

    try:
        policy = ResearchAuthorizationPolicyV1.model_validate(head.authorization_policy_json)
        verify_research_authorization_policy(policy=policy, trust_root=trust_root)
    except (ValueError, TypeError) as exc:
        raise ResearchStoreInvariantError(
            "persisted authorization policy is invalid or uses an untrusted root"
        ) from exc
    if (
        policy.quest_id != head.quest_id
        or policy.trust_root_sha256 != head.authorization_trust_root_sha256
        or trust_root.trust_root_sha256 != head.authorization_trust_root_sha256
        or policy.policy_sha256 != head.authorization_policy_sha256
        or _json(policy) != head.authorization_policy_json
    ):
        raise ResearchStoreInvariantError(
            "persisted authorization policy JSON and frozen head binding disagree"
        )
    if expected_policy is not None and policy != expected_policy:
        raise ResearchAuthorizationError(
            "persisted Quest policy differs from the configured policy pin"
        )
    return policy


def _active_charter(
    state: ResearchStateGraph,
    catalog: dict[str, KernelObject],
) -> ResearchCharterVersion | None:
    if state.charter_ref is None:
        return None
    charter = catalog.get(state.charter_ref.object_sha256)
    if not isinstance(charter, ResearchCharterVersion) or charter.object_ref != state.charter_ref:
        raise ResearchStoreInvariantError("active charter custody is missing or invalid")
    return charter


def _resolved_action(
    state: ResearchStateGraph,
    payload: object,
    catalog: dict[str, KernelObject],
) -> ResearchActionProposal | None:
    """Resolve an execution/authorization payload to the exact admitted action version."""

    if (
        isinstance(payload, StopCommittedPayload)
        and isinstance(payload.decision.directive, StopDirective)
        and payload.decision.directive.stop_reason is StopReason.EMERGENCY_STOP
    ):
        return None
    expected_ref: KernelObjectRef | None = None
    if isinstance(payload, (ActionAuthorizedPayload, ObservationIncorporatedPayload)):
        matches = tuple(
            action for action in state.actions if action.action_ref.object_id == payload.action_id
        )
        if len(matches) != 1:
            raise ResearchStoreInvariantError(
                "action authorization does not resolve to one admitted action"
            )
        expected_ref = matches[0].action_ref
    else:
        decision = getattr(payload, "decision", None)
        if decision is not None:
            expected_ref = decision.selected_action_ref
            if not any(action.action_ref == expected_ref for action in state.actions):
                raise ResearchStoreInvariantError(
                    "transition decision selects an action absent from current state"
                )
    if expected_ref is None:
        return None
    action = catalog.get(expected_ref.object_sha256)
    if not isinstance(action, ResearchActionProposal) or action.object_ref != expected_ref:
        raise ResearchStoreInvariantError(
            "authorized or applied action is missing from exact CAS custody"
        )
    return action


def _object_ref_from_row(row: _ResearchKernelObjectRecord) -> KernelObjectRef:
    try:
        return KernelObjectRef(
            object_kind=KernelObjectKind(row.object_kind),
            object_id=row.object_id,
            object_sha256=row.object_sha256,
            quest_id=row.quest_id,
        )
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted object metadata is invalid") from exc


def _archived_object(
    archive: ResearchObjectArchive,
    ref: KernelObjectRef,
) -> ArchivedKernelObject:
    try:
        value = archive.load_object(ref)
        raw = value.model_dump(mode="python") if isinstance(value, ArchivedKernelObject) else value
        archived = ArchivedKernelObject.model_validate(raw)
    except Exception as exc:
        raise ResearchStoreInvariantError(
            f"CAS could not prove object {ref.object_id}@{ref.object_sha256}"
        ) from exc
    if archived.metadata.object_ref != ref or archived.payload.object_ref != ref:
        raise ResearchStoreInvariantError("CAS returned a different object identity")
    return archived


def _verify_object_row(
    row: _ResearchKernelObjectRecord,
    archived: ArchivedKernelObject,
) -> None:
    metadata = archived.metadata
    expected = {
        "object_sha256": metadata.object_ref.object_sha256,
        "quest_id": metadata.object_ref.quest_id,
        "object_kind": metadata.object_ref.object_kind.value,
        "object_id": metadata.object_ref.object_id,
        "object_version": metadata.object_version,
        "object_schema_name": metadata.object_schema_name,
        "object_schema_version": metadata.object_schema_version,
        "canonicalization": metadata.canonicalization,
        "media_type": metadata.media_type,
        "object_size_bytes": metadata.object_size_bytes,
        "storage_key": metadata.storage_key,
    }
    if any(getattr(row, field) != value for field, value in expected.items()):
        raise ResearchStoreInvariantError("persisted object metadata changed from CAS custody")


def _load_catalog(
    session: Session,
    *,
    quest_id: str,
    archive: ResearchObjectArchive,
) -> dict[str, KernelObject]:
    rows = session.scalars(
        select(_ResearchKernelObjectRecord)
        .where(_ResearchKernelObjectRecord.quest_id == quest_id)
        .order_by(_ResearchKernelObjectRecord.object_sha256)
    ).all()
    catalog: dict[str, KernelObject] = {}
    for row in rows:
        ref = _object_ref_from_row(row)
        archived = _archived_object(archive, ref)
        _verify_object_row(row, archived)
        catalog[ref.object_sha256] = archived.payload
    return catalog


def _event_from_row(row: _ResearchKernelEventRecord) -> ResearchEvent:
    try:
        event = ResearchEvent.model_validate(row.event_json)
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted event JSON is not a ResearchEvent") from exc
    expected = {
        "event_sha256": event.event_sha256,
        "event_id": event.event_id,
        "quest_id": event.quest_id,
        "sequence": event.sequence,
        "parent_sequence": event.sequence - 1 if event.sequence > 1 else None,
        "parent_event_sha256": event.parent_event_sha256,
        "event_schema_version": event.event_schema_version,
        "reducer_version": event.reducer_version,
        "event_type": event.event_type.value,
        "command_sha256": event.command_sha256,
        "principal_id": event.principal_id,
        "authorization_receipt_sha256": event.authorization_receipt_sha256,
        "committed_at": event.committed_at,
    }
    if any(getattr(row, field) != value for field, value in expected.items()):
        raise ResearchStoreInvariantError("persisted event columns disagree with canonical JSON")
    ref = directly_referenced_object(event.payload)
    admitted = (
        row.admitted_object_sha256,
        row.admitted_object_kind,
        row.admitted_object_id,
    )
    expected_admitted = (
        (ref.object_sha256, ref.object_kind.value, ref.object_id)
        if ref is not None
        else (None, None, None)
    )
    if admitted != expected_admitted:
        raise ResearchStoreInvariantError("persisted event object index disagrees with payload")
    return event


def _command_from_row(
    row: _ResearchKernelCommandReceiptRecord,
    *,
    expected: AuthorizedResearchCommand | None = None,
) -> AuthorizedResearchCommand:
    try:
        command = AuthorizedResearchCommand.model_validate(row.command_json)
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted command JSON is invalid") from exc
    fields = {
        "command_id": command.command_id,
        "quest_id": command.quest_id,
        "idempotency_key": command.idempotency_key,
        "source_event_key": command.source_event_key,
        "command_sha256": command.command_sha256,
        "scope_binding_sha256": command.scope_binding.binding_sha256,
        "expected_stream_version": command.expected_stream_version,
        "expected_tail_event_sha256": command.expected_tail_event_sha256,
        "principal_id": command.principal_id,
        "authorization_trust_root_sha256": command.authorization_trust_root_sha256,
        "authorization_policy_sha256": command.authorization_policy_sha256,
        "authorization_receipt_sha256": command.authorization_receipt_sha256,
    }
    if any(getattr(row, field) != value for field, value in fields.items()):
        raise ResearchStoreInvariantError("persisted command columns disagree with canonical JSON")
    if row.result_stream_version != command.expected_stream_version + 1:
        raise ResearchStoreInvariantError("persisted command result version is invalid")
    if row.submitted_at != command.authorized_at or row.submitted_at > row.committed_at:
        raise ResearchStoreInvariantError("persisted command timestamps are causally invalid")
    if expected is not None and command != expected:
        raise ResearchIdempotencyConflict(
            "research command idempotency/source identity is bound to different content"
        )
    return command


def _snapshot_metadata(row: _ResearchKernelSnapshotRecord) -> ArchivedSnapshotMetadata:
    try:
        return ArchivedSnapshotMetadata(
            quest_id=row.quest_id,
            stream_version=row.sequence,
            snapshot_sha256=row.snapshot_sha256,
            canonicalization=row.canonicalization,
            media_type=row.media_type,
            snapshot_size_bytes=row.snapshot_size_bytes,
            storage_key=row.storage_key,
        )
    except ValueError as exc:
        raise ResearchStoreInvariantError("persisted snapshot metadata is invalid") from exc


def _verify_snapshot(
    row: _ResearchKernelSnapshotRecord,
    *,
    state: ResearchStateGraph,
    event: ResearchEvent,
    archive: ResearchObjectArchive,
) -> str:
    expected = {
        "snapshot_sha256": state.snapshot_sha256,
        "quest_id": event.quest_id,
        "sequence": event.sequence,
        "tail_event_sha256": event.event_sha256,
        "snapshot_schema_version": state.snapshot_schema_version,
        "reducer_version": state.reducer_version,
        "snapshot_size_bytes": len(state.canonical_bytes()),
    }
    if any(getattr(row, field) != value for field, value in expected.items()):
        raise ResearchStoreInvariantError("persisted snapshot metadata disagrees with replay")
    metadata = _snapshot_metadata(row)
    try:
        payload = archive.load_snapshot(metadata)
    except Exception as exc:
        raise ResearchStoreInvariantError("CAS could not prove persisted snapshot bytes") from exc
    if payload != state.canonical_bytes():
        raise ResearchStoreInvariantError("CAS snapshot bytes disagree with canonical replay")
    return row.snapshot_sha256


def _audit_stream(
    session: Session,
    *,
    head: _ResearchQuestStreamRecord,
    archive: ResearchObjectArchive,
    trust_root: ResearchAuthorizationTrustRootV1,
    expected_policy: ResearchAuthorizationPolicyV1 | None = None,
    allow_uncommitted_empty: bool = False,
) -> ResearchReplayAudit:
    authority = session.get(_ResearchQuestAuthorityRecord, head.quest_id)
    if authority is None or authority.authority_kind != "research_kernel_v1":
        raise ResearchStoreInvariantError(
            "Quest authority registry does not bind the stream to research_kernel_v1"
        )
    scope = _scope_from_head(head)
    authorization_policy = _policy_from_head(
        head,
        trust_root=trust_root,
        expected_policy=expected_policy,
    )
    event_rows = session.scalars(
        select(_ResearchKernelEventRecord)
        .where(_ResearchKernelEventRecord.quest_id == head.quest_id)
        .order_by(_ResearchKernelEventRecord.sequence)
    ).all()
    snapshot_rows = session.scalars(
        select(_ResearchKernelSnapshotRecord)
        .where(_ResearchKernelSnapshotRecord.quest_id == head.quest_id)
        .order_by(_ResearchKernelSnapshotRecord.sequence)
    ).all()
    snapshots = {row.sequence: row for row in snapshot_rows}
    if len(snapshots) != len(snapshot_rows):  # pragma: no cover - database unique constraint
        raise ResearchStoreInvariantError("duplicate snapshot sequence")
    catalog = _load_catalog(session, quest_id=head.quest_id, archive=archive)

    if head.stream_version == 0 and not allow_uncommitted_empty:
        raise ResearchStoreInvariantError("a persisted authoritative Quest cannot be empty")

    state = empty_state()
    events: list[ResearchEvent] = []
    verified_snapshots: list[str] = []
    referenced_objects: set[str] = set()
    for row in event_rows:
        event = _event_from_row(row)
        if event.sequence != len(events) + 1:
            raise ResearchStoreInvariantError("persisted event stream has a sequence gap")
        expected_parent = events[-1].event_sha256 if events else None
        if event.parent_event_sha256 != expected_parent:
            raise ResearchStoreInvariantError("persisted event stream has a parent hash gap")

        command_row = session.get(_ResearchKernelCommandReceiptRecord, row.command_id)
        if command_row is None:
            raise ResearchStoreInvariantError("persisted event has no command receipt")
        command = _command_from_row(command_row)
        if (
            command.scope_binding != scope
            or command.authorization_trust_root_sha256 != head.authorization_trust_root_sha256
            or command.authorization_policy_sha256 != head.authorization_policy_sha256
            or command.event_type is not event.event_type
            or command.payload != event.payload
            or command.command_sha256 != event.command_sha256
            or command.expected_stream_version != event.sequence - 1
            or command.expected_tail_event_sha256 != event.parent_event_sha256
            or command.principal_id != event.principal_id
            or command.authorization_receipt_sha256 != event.authorization_receipt_sha256
            or command_row.result_event_sha256 != event.event_sha256
            or command_row.result_stream_version != event.sequence
            or command_row.committed_at != event.committed_at
        ):
            raise ResearchStoreInvariantError("event is not the exact result of its command")

        outbox = session.scalar(
            select(_ResearchKernelOutboxRecord).where(
                _ResearchKernelOutboxRecord.event_sha256 == event.event_sha256
            )
        )
        if outbox is None:
            raise ResearchStoreInvariantError("persisted event has no transactional outbox row")
        if (
            outbox.outbox_id != f"rko_{event.event_sha256[:32]}"
            or outbox.quest_id != event.quest_id
            or outbox.sequence != event.sequence
            or outbox.payload_sha256 != event.event_sha256
            or outbox.delivery_key != f"{event.quest_id}:{event.sequence}"
            or outbox.topic != "research_kernel.event.v1"
        ):
            raise ResearchStoreInvariantError("transactional outbox is not bound to its event")

        direct_ref = directly_referenced_object(event.payload)
        direct_object: KernelObject | None = None
        if direct_ref is not None:
            referenced_objects.add(direct_ref.object_sha256)
            if direct_ref.object_sha256 not in catalog:
                raise ResearchStoreInvariantError("event references an unindexed CAS object")
            direct_object = catalog[direct_ref.object_sha256]
        verify_research_command_authorization(
            command,
            authorization_policy=authorization_policy,
            trust_root=trust_root,
            committed_at=event.committed_at,
            active_charter=_active_charter(state, catalog),
            admitted_object=direct_object,
            resolved_action=_resolved_action(state, event.payload, catalog),
        )
        try:
            state = reduce_event(state, event, catalog)
        except ValueError as exc:
            raise ResearchStoreInvariantError("persisted event stream does not reduce") from exc
        events.append(event)

        snapshot = snapshots.get(event.sequence)
        if snapshot is None:
            raise ResearchStoreInvariantError("persisted event has no replay snapshot metadata")
        verified_snapshots.append(
            _verify_snapshot(snapshot, state=state, event=event, archive=archive)
        )

    if set(catalog) != referenced_objects:
        raise ResearchStoreInvariantError("object metadata catalog contains unadmitted payloads")
    if len(snapshots) != len(events):
        raise ResearchStoreInvariantError("snapshot history and event history differ")
    if (
        head.reducer_version != REDUCER_VERSION
        or head.stream_version != state.stream_version
        or head.tail_event_sha256 != state.tail_event_sha256
        or head.stream_version != len(events)
    ):
        raise ResearchStoreInvariantError("Quest stream head disagrees with canonical replay")
    return ResearchReplayAudit(
        quest_id=head.quest_id,
        scope_binding=scope,
        events=tuple(events),
        state=state,
        verified_snapshot_sha256s=tuple(verified_snapshots),
    )


def _find_existing_command(
    session: Session,
    command: AuthorizedResearchCommand,
) -> _ResearchKernelCommandReceiptRecord | None:
    predicates = [
        _ResearchKernelCommandReceiptRecord.command_id == command.command_id,
        _ResearchKernelCommandReceiptRecord.idempotency_key == command.idempotency_key,
    ]
    if command.source_event_key is not None:
        predicates.append(
            _ResearchKernelCommandReceiptRecord.source_event_key == command.source_event_key
        )
    rows = session.scalars(
        select(_ResearchKernelCommandReceiptRecord).where(
            _ResearchKernelCommandReceiptRecord.quest_id == command.quest_id,
            or_(*predicates),
        )
    ).all()
    unique = {row.command_id: row for row in rows}
    if not unique:
        return None
    if len(unique) != 1:
        raise ResearchIdempotencyConflict(
            "research command identity conflicts with multiple persisted receipts"
        )
    row = next(iter(unique.values()))
    _command_from_row(row, expected=command)
    return row


def _receipt(
    session: Session,
    row: _ResearchKernelCommandReceiptRecord,
    *,
    created: bool,
) -> ResearchCommandReceipt:
    command = _command_from_row(row)
    head = session.get(_ResearchQuestStreamRecord, row.quest_id)
    event = session.get(_ResearchKernelEventRecord, row.result_event_sha256)
    snapshot = session.scalar(
        select(_ResearchKernelSnapshotRecord).where(
            _ResearchKernelSnapshotRecord.quest_id == row.quest_id,
            _ResearchKernelSnapshotRecord.sequence == row.result_stream_version,
        )
    )
    outbox = session.scalar(
        select(_ResearchKernelOutboxRecord).where(
            _ResearchKernelOutboxRecord.event_sha256 == row.result_event_sha256
        )
    )
    if head is None or event is None or snapshot is None or outbox is None:
        raise ResearchStoreInvariantError(
            "committed command is visible without its event/head/snapshot/outbox receipt"
        )
    scope = _scope_from_head(head)
    if command.scope_binding != scope:
        raise ResearchStoreInvariantError("committed command scope differs from its Quest head")
    return ResearchCommandReceipt(
        command_id=row.command_id,
        quest_id=row.quest_id,
        scope_binding=scope,
        idempotency_key=row.idempotency_key,
        source_event_key=row.source_event_key,
        command_sha256=row.command_sha256,
        expected_stream_version=row.expected_stream_version,
        expected_tail_event_sha256=row.expected_tail_event_sha256,
        result_stream_version=row.result_stream_version,
        result_event_sha256=row.result_event_sha256,
        result_event_id=event.event_id,
        result_snapshot_sha256=snapshot.snapshot_sha256,
        outbox_id=outbox.outbox_id,
        principal_id=row.principal_id,
        authorization_trust_root_sha256=row.authorization_trust_root_sha256,
        authorization_policy_sha256=row.authorization_policy_sha256,
        authorization_receipt_sha256=row.authorization_receipt_sha256,
        committed_at=row.committed_at,
        created=created,
    )


def _register_object(
    session: Session,
    archived: ArchivedKernelObject,
) -> None:
    metadata = archived.metadata
    existing = session.get(_ResearchKernelObjectRecord, metadata.object_ref.object_sha256)
    if existing is not None:
        _verify_object_row(existing, archived)
        return
    logical = session.scalar(
        select(_ResearchKernelObjectRecord).where(
            _ResearchKernelObjectRecord.quest_id == metadata.object_ref.quest_id,
            _ResearchKernelObjectRecord.object_kind == metadata.object_ref.object_kind.value,
            _ResearchKernelObjectRecord.object_id == metadata.object_ref.object_id,
            _ResearchKernelObjectRecord.object_version == metadata.object_version,
        )
    )
    if logical is not None:
        raise ResearchStoreInvariantError(
            "logical object version is already bound to different CAS content"
        )
    session.add(
        _ResearchKernelObjectRecord(
            object_sha256=metadata.object_ref.object_sha256,
            quest_id=metadata.object_ref.quest_id,
            object_kind=metadata.object_ref.object_kind.value,
            object_id=metadata.object_ref.object_id,
            object_version=metadata.object_version,
            object_schema_name=metadata.object_schema_name,
            object_schema_version=metadata.object_schema_version,
            canonicalization=metadata.canonicalization,
            media_type=metadata.media_type,
            object_size_bytes=metadata.object_size_bytes,
            storage_key=metadata.storage_key,
        )
    )


class ResearchKernelStore:
    """Commit, exactly replay, and audit Quest-local authoritative event streams."""

    def __init__(
        self,
        *,
        trust_root: ResearchAuthorizationTrustRootV1,
        archive: ResearchObjectArchive,
        genesis_policy: ResearchAuthorizationPolicyV1 | None = None,
    ) -> None:
        if type(trust_root) is not ResearchAuthorizationTrustRootV1:
            raise TypeError("ResearchKernelStore requires the fixed v1 trust-root type")
        self._trust_root = ResearchAuthorizationTrustRootV1.model_validate(
            trust_root.model_dump(mode="python")
        )
        if genesis_policy is not None:
            if type(genesis_policy) is not ResearchAuthorizationPolicyV1:
                raise TypeError("ResearchKernelStore requires the fixed v1 policy type")
            genesis_policy = ResearchAuthorizationPolicyV1.model_validate(
                genesis_policy.model_dump(mode="python")
            )
            verify_research_authorization_policy(
                policy=genesis_policy,
                trust_root=self._trust_root,
            )
        if not all(
            callable(getattr(archive, method, None))
            for method in ("load_object", "archive_snapshot", "load_snapshot")
        ):
            raise TypeError("ResearchKernelStore requires one pinned object archive")
        self._archive = archive
        self._genesis_policy = genesis_policy

    def _expected_policy(self, quest_id: str) -> ResearchAuthorizationPolicyV1 | None:
        if self._genesis_policy is not None and self._genesis_policy.quest_id == quest_id:
            return self._genesis_policy
        return None

    def _genesis_policy_for(
        self,
        command: AuthorizedResearchCommand,
    ) -> ResearchAuthorizationPolicyV1:
        policy = self._expected_policy(command.quest_id)
        if policy is None:
            raise ResearchAuthorizationError(
                "a deployment-pinned policy is required to commission a new Quest"
            )
        if (
            command.authorization_trust_root_sha256 != self._trust_root.trust_root_sha256
            or command.authorization_policy_sha256 != policy.policy_sha256
        ):
            raise ResearchAuthorizationError(
                "genesis command is bound to another deployment trust policy"
            )
        return policy

    @staticmethod
    def _inject_fault(_point: str) -> None:
        """Private test seam for process-crash simulation; never exposes the live transaction."""

    def _existing_receipt(
        self,
        session: Session,
        command: AuthorizedResearchCommand,
    ) -> ResearchCommandReceipt | None:
        row = _find_existing_command(session, command)
        if row is None:
            return None
        head = session.scalar(
            select(_ResearchQuestStreamRecord)
            .where(_ResearchQuestStreamRecord.quest_id == command.quest_id)
            .with_for_update()
        )
        if head is None:
            raise ResearchStoreInvariantError("command receipt exists without its Quest head")
        _audit_stream(
            session,
            head=head,
            archive=self._archive,
            trust_root=self._trust_root,
            expected_policy=self._expected_policy(command.quest_id),
        )
        return _receipt(session, row, created=False)

    def commit(
        self,
        command: AuthorizedResearchCommand | ResearchCommandProposal,
    ) -> ResearchCommandReceipt:
        """Atomically append one authorized event and all of its durable receipts.

        Object bytes must already exist in the constructor-pinned archive.  A snapshot is staged
        there before the database transaction commits; rollback may leave an unreachable CAS
        object, never a database event whose referenced bytes are absent.
        """

        return self._commit(command, caller_session=None)

    def commit_in_session(
        self,
        session: Session,
        command: AuthorizedResearchCommand | ResearchCommandProposal,
    ) -> ResearchCommandReceipt:
        """Stage one Kernel commit inside a caller-owned transaction.

        This is the atomic composition seam for an independently admitted observation and its
        ``observation_incorporated`` event.  The caller owns commit or rollback.  The returned
        receipt is authoritative only after that outer transaction commits; this method never
        invokes the post-commit fault seam.
        """

        if not isinstance(session, Session):
            raise TypeError("commit_in_session requires a SQLAlchemy Session")
        return self._commit(command, caller_session=session)

    def load_command_receipt_for_event(
        self,
        *,
        quest_id: str,
        result_event_sha256: str,
    ) -> ResearchCommandReceipt | None:
        """Load and fully audit the command receipt that produced one exact event."""

        with session_scope() as session:
            return self.load_command_receipt_for_event_in_session(
                session,
                quest_id=quest_id,
                result_event_sha256=result_event_sha256,
            )

    def load_command_receipt_for_event_in_session(
        self,
        session: Session,
        *,
        quest_id: str,
        result_event_sha256: str,
    ) -> ResearchCommandReceipt | None:
        """Load an event's receipt inside a caller-owned, audited transaction.

        Observation admission uses this seam to return the original Kernel receipt after a
        crash-after-commit exact retry.  No private ORM row escapes the store boundary.
        """

        if not isinstance(session, Session):
            raise TypeError(
                "load_command_receipt_for_event_in_session requires a SQLAlchemy Session"
            )
        if re.fullmatch(_QUEST_ID_PATTERN, quest_id) is None:
            raise ValueError("receipt lookup Quest id is invalid")
        if re.fullmatch(_SHA256_PATTERN, result_event_sha256) is None:
            raise ValueError("receipt lookup event identity is not SHA-256")
        row = session.scalar(
            select(_ResearchKernelCommandReceiptRecord).where(
                _ResearchKernelCommandReceiptRecord.quest_id == quest_id,
                _ResearchKernelCommandReceiptRecord.result_event_sha256 == result_event_sha256,
            )
        )
        if row is None:
            return None
        self.audit_in_session(session, quest_id)
        return _receipt(session, row, created=False)

    def _commit(
        self,
        command: AuthorizedResearchCommand | ResearchCommandProposal,
        *,
        caller_session: Session | None,
    ) -> ResearchCommandReceipt:
        """Run the common authoritative commit path in an owned or participating transaction."""

        if isinstance(command, ResearchCommandProposal):
            raise UncommittedProposalError(
                "a ResearchCommandProposal cannot mutate authoritative research state"
            )
        if not isinstance(command, AuthorizedResearchCommand):
            raise TypeError("commit requires an AuthorizedResearchCommand")
        command = AuthorizedResearchCommand.model_validate(command.model_dump(mode="python"))

        receipt: ResearchCommandReceipt
        created = False
        transaction = session_scope() if caller_session is None else nullcontext(caller_session)
        with transaction as session:
            # Exact retry wins before any current-time or head-version decision.  This is the
            # crash-after-commit path and must depend only on the original request identity.
            existing = self._existing_receipt(session, command)
            if existing is not None:
                receipt = existing
            else:
                if command.expected_stream_version == 0:
                    genesis_policy = self._genesis_policy_for(command)
                    provisional_at = _transaction_time(session)
                    session.execute(
                        postgresql_insert(_ResearchQuestStreamRecord)
                        .values(
                            quest_id=command.quest_id,
                            program_id=command.scope_binding.program_id,
                            campaign_id=command.scope_binding.campaign_id,
                            scope_binding_sha256=command.scope_binding.binding_sha256,
                            authorization_trust_root_sha256=(self._trust_root.trust_root_sha256),
                            authorization_policy_sha256=genesis_policy.policy_sha256,
                            authorization_policy_json=_json(genesis_policy),
                            stream_version=0,
                            tail_event_sha256=None,
                            reducer_version=REDUCER_VERSION,
                            created_at=provisional_at,
                            updated_at=provisional_at,
                        )
                        .on_conflict_do_nothing()
                    )
                    session.flush()
                head = session.scalar(
                    select(_ResearchQuestStreamRecord)
                    .where(_ResearchQuestStreamRecord.quest_id == command.quest_id)
                    .with_for_update()
                )
                if head is None:
                    raise ResearchQuestNotFound(
                        f"non-genesis command targets an unknown Quest: {command.quest_id}"
                    )

                # A concurrent first delivery can have committed while this transaction waited for
                # head creation/locking.  Recheck idempotency before comparing CAS version/tail.
                existing = self._existing_receipt(session, command)
                if existing is not None:
                    receipt = existing
                else:
                    # The authority time is sampled only after the serializing head lock and the
                    # lock-wait idempotency recheck.  A wait crossing key/charter expiry therefore
                    # cannot commit using a stale pre-lock timestamp.
                    observed_at = _transaction_time(session)
                    authorization_policy = _policy_from_head(
                        head,
                        trust_root=self._trust_root,
                        expected_policy=self._expected_policy(command.quest_id),
                    )
                    scope = _scope_from_head(head)
                    if scope != command.scope_binding:
                        raise ResearchStoreError(
                            "command scope binding differs from the frozen Quest stream"
                        )
                    if (
                        head.authorization_trust_root_sha256
                        != command.authorization_trust_root_sha256
                        or head.authorization_policy_sha256 != command.authorization_policy_sha256
                    ):
                        raise ResearchAuthorizationError(
                            "command trust policy differs from the frozen Quest stream"
                        )
                    if (
                        head.stream_version != command.expected_stream_version
                        or head.tail_event_sha256 != command.expected_tail_event_sha256
                    ):
                        raise ResearchVersionConflict(
                            "stale Quest head: expected "
                            f"{command.expected_stream_version}@"
                            f"{command.expected_tail_event_sha256}, observed "
                            f"{head.stream_version}@{head.tail_event_sha256}"
                        )

                    current = _audit_stream(
                        session,
                        head=head,
                        archive=self._archive,
                        trust_root=self._trust_root,
                        expected_policy=self._expected_policy(command.quest_id),
                        allow_uncommitted_empty=True,
                    )
                    catalog = _load_catalog(
                        session,
                        quest_id=command.quest_id,
                        archive=self._archive,
                    )
                    active_charter = _active_charter(current.state, catalog)
                    direct_ref = directly_referenced_object(command.payload)
                    archived: ArchivedKernelObject | None = None
                    if direct_ref is not None:
                        archived = _archived_object(self._archive, direct_ref)
                        catalog[direct_ref.object_sha256] = archived.payload
                    verify_research_command_authorization(
                        command,
                        authorization_policy=authorization_policy,
                        trust_root=self._trust_root,
                        committed_at=observed_at,
                        active_charter=active_charter,
                        admitted_object=archived.payload if archived is not None else None,
                        resolved_action=_resolved_action(
                            current.state,
                            command.payload,
                            catalog,
                        ),
                    )
                    event = command.to_event(
                        sequence=command.expected_stream_version + 1,
                        parent_event_sha256=command.expected_tail_event_sha256,
                        committed_at=observed_at,
                    )
                    try:
                        next_state = reduce_event(current.state, event, catalog)
                    except ValueError as exc:
                        raise ResearchStoreError(
                            "authorized command is not a valid research transition"
                        ) from exc

                    snapshot_bytes = next_state.canonical_bytes()
                    try:
                        raw_snapshot = self._archive.archive_snapshot(
                            quest_id=command.quest_id,
                            stream_version=event.sequence,
                            snapshot_sha256=next_state.snapshot_sha256,
                            payload=snapshot_bytes,
                        )
                        raw = (
                            raw_snapshot.model_dump(mode="python")
                            if isinstance(raw_snapshot, ArchivedSnapshotMetadata)
                            else raw_snapshot
                        )
                        snapshot_metadata = ArchivedSnapshotMetadata.model_validate(raw)
                        loaded_snapshot = self._archive.load_snapshot(snapshot_metadata)
                    except Exception as exc:
                        raise ResearchStoreError("snapshot CAS staging failed closed") from exc
                    if (
                        snapshot_metadata.quest_id != command.quest_id
                        or snapshot_metadata.stream_version != event.sequence
                        or snapshot_metadata.snapshot_sha256 != next_state.snapshot_sha256
                        or snapshot_metadata.snapshot_size_bytes != len(snapshot_bytes)
                        or loaded_snapshot != snapshot_bytes
                    ):
                        raise ResearchStoreError(
                            "snapshot CAS receipt does not match canonical reducer output"
                        )
                    self._inject_fault("after_snapshot_cas_before_db")

                    if archived is not None:
                        _register_object(session, archived)
                    command_row = _ResearchKernelCommandReceiptRecord(
                        command_id=command.command_id,
                        quest_id=command.quest_id,
                        idempotency_key=command.idempotency_key,
                        source_event_key=command.source_event_key,
                        command_sha256=command.command_sha256,
                        command_json=_json(command),
                        scope_binding_sha256=command.scope_binding.binding_sha256,
                        authorization_trust_root_sha256=(command.authorization_trust_root_sha256),
                        authorization_policy_sha256=command.authorization_policy_sha256,
                        expected_stream_version=command.expected_stream_version,
                        expected_tail_event_sha256=command.expected_tail_event_sha256,
                        principal_id=command.principal_id,
                        authorization_receipt_sha256=command.authorization_receipt_sha256,
                        result_stream_version=event.sequence,
                        result_event_sha256=event.event_sha256,
                        submitted_at=command.authorized_at,
                        committed_at=observed_at,
                    )
                    event_row = _ResearchKernelEventRecord(
                        event_sha256=event.event_sha256,
                        event_id=event.event_id,
                        quest_id=event.quest_id,
                        sequence=event.sequence,
                        parent_sequence=event.sequence - 1 if event.sequence > 1 else None,
                        parent_event_sha256=event.parent_event_sha256,
                        event_schema_version=EVENT_SCHEMA_VERSION,
                        reducer_version=REDUCER_VERSION,
                        event_type=event.event_type.value,
                        event_json=_json(event),
                        command_id=command.command_id,
                        command_sha256=command.command_sha256,
                        principal_id=command.principal_id,
                        authorization_receipt_sha256=command.authorization_receipt_sha256,
                        admitted_object_sha256=(
                            direct_ref.object_sha256 if direct_ref is not None else None
                        ),
                        admitted_object_kind=(
                            direct_ref.object_kind.value if direct_ref is not None else None
                        ),
                        admitted_object_id=(
                            direct_ref.object_id if direct_ref is not None else None
                        ),
                        committed_at=observed_at,
                    )
                    snapshot_row = _ResearchKernelSnapshotRecord(
                        snapshot_sha256=next_state.snapshot_sha256,
                        quest_id=command.quest_id,
                        sequence=event.sequence,
                        tail_event_sha256=event.event_sha256,
                        snapshot_schema_version=next_state.snapshot_schema_version,
                        reducer_version=next_state.reducer_version,
                        canonicalization=snapshot_metadata.canonicalization,
                        media_type=snapshot_metadata.media_type,
                        snapshot_size_bytes=snapshot_metadata.snapshot_size_bytes,
                        storage_key=snapshot_metadata.storage_key,
                        created_at=observed_at,
                    )
                    outbox_row = _ResearchKernelOutboxRecord(
                        outbox_id=f"rko_{event.event_sha256[:32]}",
                        quest_id=command.quest_id,
                        sequence=event.sequence,
                        event_sha256=event.event_sha256,
                        topic="research_kernel.event.v1",
                        delivery_key=f"{command.quest_id}:{event.sequence}",
                        payload_sha256=event.event_sha256,
                        delivery_status="pending",
                        delivery_attempts=0,
                        available_at=observed_at,
                        last_attempt_at=None,
                        published_at=None,
                        created_at=observed_at,
                    )
                    session.add_all((command_row, event_row, snapshot_row, outbox_row))
                    head.stream_version = event.sequence
                    head.tail_event_sha256 = event.event_sha256
                    head.updated_at = observed_at
                    session.flush()

                    # Re-read every authority and custody surface while still inside the same
                    # transaction.  Any mismatch aborts the event, head, command, snapshot, and
                    # outbox together.
                    _audit_stream(
                        session,
                        head=head,
                        archive=self._archive,
                        trust_root=self._trust_root,
                        expected_policy=self._expected_policy(command.quest_id),
                    )
                    self._inject_fault("before_commit")
                    receipt = _receipt(session, command_row, created=True)
                    created = True

        if created and caller_session is None:
            # Raising here models a process/client crash after PostgreSQL committed but before the
            # caller durably observed its receipt.  Exact retry returns created=False.
            self._inject_fault("after_commit")
        return receipt

    def list_pending_outbox(
        self,
        *,
        registered_quest_ids: tuple[str, ...],
        limit: int = 100,
    ) -> tuple[ResearchKernelOutboxItem, ...]:
        """Read ready Kernel delivery intents for an exact registered-Quest allowlist."""

        with session_scope() as session:
            return self.list_pending_outbox_in_session(
                session,
                registered_quest_ids=registered_quest_ids,
                limit=limit,
                lock_for_publish=False,
            )

    def list_pending_outbox_in_session(
        self,
        session: Session,
        *,
        registered_quest_ids: tuple[str, ...],
        limit: int = 100,
        lock_for_publish: bool = True,
    ) -> tuple[ResearchKernelOutboxItem, ...]:
        """Read ready intents in a caller-owned transaction, optionally locking for publish.

        A dispatcher can lock rows, enqueue its deterministic task and delivery receipt using the
        same ``session``, then call :meth:`mark_outbox_published_in_session`.  The caller owns the
        final commit or rollback and must not expose a delivery until that transaction commits.
        """

        if not isinstance(session, Session):
            raise TypeError("list_pending_outbox_in_session requires a SQLAlchemy Session")
        quest_ids = _registered_quest_ids(registered_quest_ids)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("pending outbox limit must be between 1 and 1000")
        if not isinstance(lock_for_publish, bool):
            raise TypeError("lock_for_publish must be a boolean")
        observed_at = _transaction_time(session)
        statement = (
            select(_ResearchKernelOutboxRecord)
            .where(
                _ResearchKernelOutboxRecord.quest_id.in_(quest_ids),
                _ResearchKernelOutboxRecord.delivery_status == "pending",
                _ResearchKernelOutboxRecord.available_at <= observed_at,
            )
            .order_by(
                _ResearchKernelOutboxRecord.available_at,
                _ResearchKernelOutboxRecord.created_at,
                _ResearchKernelOutboxRecord.quest_id,
                _ResearchKernelOutboxRecord.sequence,
            )
            .limit(limit)
        )
        if lock_for_publish:
            statement = statement.with_for_update(skip_locked=True)
        return tuple(_outbox_item(row) for row in session.scalars(statement).all())

    def mark_outbox_published(
        self,
        expected: ResearchKernelOutboxIdentity | ResearchKernelOutboxItem,
    ) -> ResearchKernelOutboxItem:
        """Atomically publish one intent only if its immutable identity is still exact."""

        with session_scope() as session:
            return self.mark_outbox_published_in_session(session, expected)

    def mark_outbox_published_in_session(
        self,
        session: Session,
        expected: ResearchKernelOutboxIdentity | ResearchKernelOutboxItem,
    ) -> ResearchKernelOutboxItem:
        """Mark an exact outbox intent published inside a caller-owned transaction.

        The operation is compare-and-set on the full immutable routing identity.  An exact retry
        of an already-published item returns the current projection without increasing attempts.
        """

        if not isinstance(session, Session):
            raise TypeError("mark_outbox_published_in_session requires a SQLAlchemy Session")
        if isinstance(expected, ResearchKernelOutboxItem):
            identity = expected.identity
        elif isinstance(expected, ResearchKernelOutboxIdentity):
            identity = ResearchKernelOutboxIdentity.model_validate(
                expected.model_dump(mode="python")
            )
        else:
            raise TypeError("mark_outbox_published requires a research outbox identity")
        row = session.scalar(
            select(_ResearchKernelOutboxRecord)
            .where(_ResearchKernelOutboxRecord.outbox_id == identity.outbox_id)
            .with_for_update()
        )
        if row is None:
            raise ResearchOutboxConflict("research outbox item does not exist")
        current = _outbox_item(row)
        if current.identity != identity:
            raise ResearchOutboxConflict(
                "research outbox item differs from the expected immutable identity"
            )
        if current.delivery_status == "published":
            return current
        if current.delivery_status != "pending":
            raise ResearchOutboxConflict("research outbox item is not pending or already published")
        observed_at = _transaction_time(session)
        if current.available_at > observed_at:
            raise ResearchOutboxConflict("research outbox item is not yet available")
        row.delivery_status = "published"
        row.delivery_attempts += 1
        row.last_attempt_at = observed_at
        row.published_at = observed_at
        session.flush()
        return _outbox_item(row)

    def audit(
        self,
        quest_id: str,
        *,
        expected_scope_binding: ResearchScopeBinding | None = None,
    ) -> ResearchReplayAudit:
        """Recompute and verify an entire Quest chain, CAS catalog, snapshots, and receipts."""

        with session_scope() as session:
            return self.audit_in_session(
                session,
                quest_id,
                expected_scope_binding=expected_scope_binding,
            )

    def audit_in_session(
        self,
        session: Session,
        quest_id: str,
        *,
        expected_scope_binding: ResearchScopeBinding | None = None,
    ) -> ResearchReplayAudit:
        """Fully audit and lock one Quest inside a caller-owned transaction."""

        if not isinstance(session, Session):
            raise TypeError("audit_in_session requires a SQLAlchemy Session")
        head = session.scalar(
            select(_ResearchQuestStreamRecord)
            .where(_ResearchQuestStreamRecord.quest_id == quest_id)
            .with_for_update()
        )
        if head is None:
            raise ResearchQuestNotFound(f"unknown Quest stream: {quest_id}")
        if expected_scope_binding is not None and _scope_from_head(head) != expected_scope_binding:
            raise ResearchStoreError("requested scope does not match the frozen Quest binding")
        return _audit_stream(
            session,
            head=head,
            archive=self._archive,
            trust_root=self._trust_root,
            expected_policy=self._expected_policy(quest_id),
        )

    def replay(
        self,
        quest_id: str,
        *,
        expected_scope_binding: ResearchScopeBinding | None = None,
    ) -> ResearchStateGraph:
        """Return only the canonical graph after performing the full fail-closed audit."""

        return self.audit(
            quest_id,
            expected_scope_binding=expected_scope_binding,
        ).state


__all__ = [
    "ArchivedKernelObject",
    "ArchivedObjectMetadata",
    "ArchivedSnapshotMetadata",
    "ResearchAuthorizationError",
    "ResearchCommandReceipt",
    "ResearchIdempotencyConflict",
    "ResearchKernelOutboxIdentity",
    "ResearchKernelOutboxItem",
    "ResearchKernelStore",
    "ResearchObjectArchive",
    "ResearchOutboxConflict",
    "ResearchQuestNotFound",
    "ResearchReplayAudit",
    "ResearchStoreError",
    "ResearchStoreInvariantError",
    "ResearchVersionConflict",
    "UncommittedProposalError",
]
