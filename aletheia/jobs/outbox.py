"""Transactional scientific commands backed by the durable event ledger.

The queue deliberately provides at-least-once delivery.  This module is the corresponding
exact-commit boundary for scientific state: an idempotent command, the rows written by its
callback, its immutable result receipt, and one keyed event all commit in the same PostgreSQL
transaction.  The ``events`` table is therefore the outbox; SSE and later consumers resume from
its database cursor rather than depending on process-local notification.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.events.bus import make_event
from aletheia.events.store import persist_event
from aletheia.jobs.contracts import canonical_payload
from aletheia.jobs.persistence import ScientificCommandRecord
from aletheia.reproducibility.manifest import content_sha256

_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_COMMAND_ID_PATTERN = r"^scmd_[0-9a-f]{32}$"
_COMMAND_TYPE_PATTERN = r"^[a-z][a-z0-9_.-]{0,95}$"
_AGGREGATE_TYPE_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"


class ScientificTransitionError(RuntimeError):
    """Base error for a scientific command contract or persisted invariant violation."""


class ScientificIdempotencyConflict(ScientificTransitionError):
    """A command/source-event identity was rebound to different content."""


class ScientificTransitionInvariantError(ScientificTransitionError):
    """A committed command no longer has its exact result/event receipt."""


class ScientificCommandType(str, Enum):
    PREDICTION_COMMIT = "prediction.commit"
    OBSERVATION_VALIDATION_COMMIT = "observation_validation.commit"
    BELIEF_UPDATE_COMMIT = "belief_update.commit"
    ARTIFACT_COMMIT = "artifact.commit"
    STAGE_TRANSITION = "stage.transition"
    WORLD_MODEL_TRANSITION = "world_model.transition"
    RESEARCH_GRAPH_MUTATION = "research_graph.mutation"
    RESEARCH_MEMORY_MUTATION = "research_memory.mutation"
    RESEARCH_MEMORY_CONTEXT = "research_memory.context"
    RESEARCH_PORTFOLIO_MUTATION = "research_portfolio.mutation"
    GENERIC = "scientific.generic"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _transaction_time(session: Session, supplied: datetime | None) -> datetime:
    if supplied is not None:
        return _aware(supplied, label="scientific command timestamp")
    observed = session.scalar(select(func.now()))
    if observed is None:  # pragma: no cover - PostgreSQL always returns now()
        return datetime.now(timezone.utc)
    return _aware(observed, label="database transaction timestamp")


def scientific_command_id(command_type: str, idempotency_key: str) -> str:
    """Return the deterministic database identity for a logical scientific command."""

    digest = content_sha256(
        {
            "schema": "aletheia.scientific_command_identity.v1",
            "command_type": command_type,
            "idempotency_key": idempotency_key,
        }
    )
    return f"scmd_{digest[:32]}"


class ScientificCommandSpec(_FrozenModel):
    """Immutable input identity for one transactionally applied scientific mutation."""

    command_id: str | None = Field(default=None, pattern=_COMMAND_ID_PATTERN)
    # Quest/program mutations exist above any legacy ``Run``.  A missing run_id therefore means
    # portfolio-scoped scientific state, not an anonymous run mutation; aggregate identity remains
    # mandatory and the durable event is emitted without a run filter.
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    command_type: str = Field(pattern=_COMMAND_TYPE_PATTERN)
    aggregate_type: str = Field(pattern=_AGGREGATE_TYPE_PATTERN)
    aggregate_id: str = Field(min_length=1, max_length=192)
    idempotency_key: str = Field(pattern=_IDENTITY_PATTERN)
    source_event_key: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)
    input: dict[str, Any] = Field(default_factory=dict)
    principal: str = Field(min_length=1, max_length=128)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")

    @model_validator(mode="after")
    def _normalize_and_bind_identity(self) -> "ScientificCommandSpec":
        normalized = canonical_payload(self.input)
        expected = scientific_command_id(self.command_type, self.idempotency_key)
        if self.command_id is not None and self.command_id != expected:
            raise ValueError("scientific command id does not match its type/idempotency identity")
        object.__setattr__(self, "command_id", expected)
        object.__setattr__(self, "input", normalized)
        return self

    @property
    def input_sha256(self) -> str:
        return content_sha256(self.input)

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)

    @property
    def output_event_key(self) -> str:
        assert self.command_id is not None
        return f"scientific-command:{self.command_id}"


class ScientificMutation(_FrozenModel):
    """Callback output committed alongside the callback's database writes."""

    result: dict[str, Any] = Field(default_factory=dict)
    event_projection: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _canonicalize(self) -> "ScientificMutation":
        object.__setattr__(self, "result", canonical_payload(self.result))
        object.__setattr__(
            self,
            "event_projection",
            canonical_payload(self.event_projection),
        )
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self.result)


class ScientificCommandReceipt(_FrozenModel):
    command_id: str = Field(pattern=_COMMAND_ID_PATTERN)
    run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    command_type: str
    aggregate_type: str
    aggregate_id: str
    idempotency_key: str
    source_event_key: str | None
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: dict[str, Any]
    output_event_key: str
    output_event_id: int = Field(ge=1)
    committed_at: AwareDatetime
    created: bool


FaultHook = Callable[[str, Session], None]
ApplyScientificMutation = Callable[[Session], ScientificMutation]


class ScientificTransitionStore:
    """Execute and replay exact scientific mutations under one database transaction."""

    @staticmethod
    def _receipt(row: ScientificCommandRecord, *, created: bool) -> ScientificCommandReceipt:
        reconstructed = ScientificCommandSpec(
            command_id=row.command_id,
            run_id=row.run_id,
            command_type=row.command_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            idempotency_key=row.idempotency_key,
            source_event_key=row.source_event_key,
            input=row.input_json,
            principal=row.principal,
            event_type=row.event_type,
        )
        if (
            reconstructed.request_sha256 != row.request_sha256
            or reconstructed.input_sha256 != row.input_sha256
            or reconstructed.output_event_key != row.output_event_key
        ):
            raise ScientificTransitionInvariantError("scientific command request identity changed")
        if (
            row.status != "committed"
            or row.result_sha256 is None
            or row.result_json is None
            or row.event_payload_json is None
            or row.output_event_id is None
            or row.committed_at is None
        ):
            raise ScientificTransitionInvariantError(
                "scientific command is visible without a complete result/event receipt"
            )
        if content_sha256(row.result_json) != row.result_sha256:
            raise ScientificTransitionInvariantError("scientific command result hash changed")
        return ScientificCommandReceipt(
            command_id=row.command_id,
            run_id=row.run_id,
            command_type=row.command_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            idempotency_key=row.idempotency_key,
            source_event_key=row.source_event_key,
            request_sha256=row.request_sha256,
            input_sha256=row.input_sha256,
            result_sha256=row.result_sha256,
            result=row.result_json,
            output_event_key=row.output_event_key,
            output_event_id=row.output_event_id,
            committed_at=row.committed_at,
            created=created,
        )

    @staticmethod
    def _verify_request(row: ScientificCommandRecord, spec: ScientificCommandSpec) -> None:
        expected = {
            "command_id": spec.command_id,
            "run_id": spec.run_id,
            "command_type": spec.command_type,
            "aggregate_type": spec.aggregate_type,
            "aggregate_id": spec.aggregate_id,
            "idempotency_key": spec.idempotency_key,
            "source_event_key": spec.source_event_key,
            "request_sha256": spec.request_sha256,
            "input_sha256": spec.input_sha256,
            "input_json": spec.input,
            "principal": spec.principal,
            "event_type": spec.event_type,
            "output_event_key": spec.output_event_key,
        }
        if any(getattr(row, field) != value for field, value in expected.items()):
            raise ScientificIdempotencyConflict(
                "scientific command or source event is already bound to different content"
            )

    @staticmethod
    def _verify_event(session: Session, row: ScientificCommandRecord) -> None:
        assert row.event_payload_json is not None
        assert row.output_event_id is not None
        event_id = persist_event(
            make_event(
                row.event_type,
                run_id=row.run_id,
                agent=row.principal,
                payload=row.event_payload_json,
            ),
            event_key=row.output_event_key,
            session=session,
        )
        if event_id != row.output_event_id:
            raise ScientificTransitionInvariantError(
                "scientific command event id no longer matches its receipt"
            )

    def execute(
        self,
        spec: ScientificCommandSpec,
        apply: ApplyScientificMutation,
        *,
        now: datetime | None = None,
        fault_hook: FaultHook | None = None,
    ) -> ScientificCommandReceipt:
        """Apply one callback once; exact redelivery returns the committed receipt.

        The callback must use the supplied ``Session`` and must not commit it.  Raising from the
        callback, event write, or fault hook rolls back its state rows, command row, and event.
        """

        spec = ScientificCommandSpec.model_validate(spec.model_dump(mode="python"))
        with session_scope() as session:
            observed_at = _transaction_time(session, now)
            inserted = session.scalar(
                postgresql_insert(ScientificCommandRecord)
                .values(
                    command_id=spec.command_id,
                    run_id=spec.run_id,
                    command_type=spec.command_type,
                    aggregate_type=spec.aggregate_type,
                    aggregate_id=spec.aggregate_id,
                    idempotency_key=spec.idempotency_key,
                    source_event_key=spec.source_event_key,
                    request_sha256=spec.request_sha256,
                    input_sha256=spec.input_sha256,
                    input_json=spec.input,
                    principal=spec.principal,
                    status="applying",
                    result_sha256=None,
                    result_json=None,
                    event_type=spec.event_type,
                    event_payload_json=None,
                    output_event_key=spec.output_event_key,
                    output_event_id=None,
                    created_at=observed_at,
                    committed_at=None,
                )
                .on_conflict_do_nothing()
                .returning(ScientificCommandRecord.command_id)
            )
            session.flush()
            if inserted is None:
                predicates = [
                    ScientificCommandRecord.command_id == spec.command_id,
                    ScientificCommandRecord.idempotency_key == spec.idempotency_key,
                ]
                if spec.source_event_key is not None:
                    predicates.append(
                        ScientificCommandRecord.source_event_key == spec.source_event_key
                    )
                rows = session.scalars(
                    select(ScientificCommandRecord).where(or_(*predicates))
                ).all()
                unique = {row.command_id: row for row in rows}
                if len(unique) != 1:
                    raise ScientificIdempotencyConflict(
                        "scientific command identity conflicts with multiple persisted commands"
                    )
                row = next(iter(unique.values()))
                self._verify_request(row, spec)
                self._verify_event(session, row)
                return self._receipt(row, created=False)

            assert spec.command_id is not None
            row = session.get(ScientificCommandRecord, spec.command_id)
            if row is None:  # pragma: no cover - the insert just returned this key
                raise ScientificTransitionInvariantError("scientific command insert disappeared")

            mutation = apply(session)
            if not isinstance(mutation, ScientificMutation):
                mutation = ScientificMutation.model_validate(mutation)
            session.flush()
            if fault_hook is not None:
                fault_hook("after_state_before_event", session)

            event_payload = canonical_payload(
                {
                    "schema": "aletheia.scientific_transition",
                    "schema_version": 1,
                    "command_id": spec.command_id,
                    "command_type": spec.command_type,
                    "aggregate_type": spec.aggregate_type,
                    "aggregate_id": spec.aggregate_id,
                    "source_event_key": spec.source_event_key,
                    "input_sha256": spec.input_sha256,
                    "result_sha256": mutation.result_sha256,
                    "projection": mutation.event_projection,
                    "committed_at": observed_at.isoformat(),
                }
            )
            event_id = persist_event(
                make_event(
                    spec.event_type,
                    run_id=spec.run_id,
                    agent=spec.principal,
                    payload=event_payload,
                ),
                event_key=spec.output_event_key,
                session=session,
            )
            if fault_hook is not None:
                fault_hook("after_event_before_receipt", session)

            row.result_sha256 = mutation.result_sha256
            row.result_json = mutation.result
            row.event_payload_json = event_payload
            row.output_event_id = event_id
            row.status = "committed"
            row.committed_at = observed_at
            session.flush()
            if fault_hook is not None:
                fault_hook("before_commit", session)
            return self._receipt(row, created=True)

    def get(self, command_id: str) -> ScientificCommandReceipt:
        if re.fullmatch(_COMMAND_ID_PATTERN, command_id) is None:
            raise ValueError("invalid scientific command id")
        with session_scope() as session:
            row = session.get(ScientificCommandRecord, command_id)
            if row is None:
                raise ScientificTransitionError(f"scientific command not found: {command_id}")
            self._verify_event(session, row)
            return self._receipt(row, created=False)


__all__ = [
    "ScientificCommandReceipt",
    "ScientificCommandSpec",
    "ScientificCommandType",
    "ScientificIdempotencyConflict",
    "ScientificMutation",
    "ScientificTransitionError",
    "ScientificTransitionInvariantError",
    "ScientificTransitionStore",
    "scientific_command_id",
]
