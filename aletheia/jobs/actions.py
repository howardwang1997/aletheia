"""At-most-once outward-action intents, claims, receipts, and reconciliation.

No database can make an arbitrary remote side effect and a local commit globally atomic.  The
safe contract is therefore explicit: persist and claim an intent before revealing data or calling
a provider, return a stable provider idempotency key, accept one token-bound receipt, and never
automatically reissue a claimed action.  A stale claim becomes ``reconciliation_required``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.events.bus import make_event
from aletheia.events.store import persist_event
from aletheia.jobs.contracts import canonical_payload
from aletheia.jobs.persistence import (
    ExternalActionReceiptRecord,
    OneTimeExternalActionRecord,
)
from aletheia.memory.ledger import Event
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
_ACTION_ID_PATTERN = r"^act_[0-9a-f]{32}$"
_ACTION_TYPE_PATTERN = r"^[a-z][a-z0-9_.-]{0,95}$"
_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class ExternalActionError(RuntimeError):
    """Base error for a one-time external action contract."""


class ExternalActionIdentityConflict(ExternalActionError):
    """An action scope or identity was rebound to different immutable content."""


class ExternalActionTokenMismatch(ExternalActionError):
    """A completion callback did not prove possession of the one returned claim token."""


class InvalidExternalActionTransition(ExternalActionError):
    """An external action was completed or recovered from an invalid state."""


class ExternalActionInvariantError(ExternalActionError):
    """Persisted intent, receipt, or keyed event no longer agrees with its hash."""


class ExternalActionStatus(str, Enum):
    CLAIMED = "claimed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETED = "completed"


class ExternalActionType(str, Enum):
    FINAL_HOLDOUT_OPEN = "final_holdout.open"
    EXTERNAL_VALIDATION_OPEN = "external_validation.open"
    DATASET_ACCESS = "dataset.access"
    PROVIDER_REQUEST = "provider.request"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _transaction_time(session: Session, supplied: datetime | None) -> datetime:
    if supplied is not None:
        return _aware(supplied, label="external action timestamp")
    observed = session.scalar(select(func.now()))
    if observed is None:  # pragma: no cover - PostgreSQL always returns now()
        return datetime.now(timezone.utc)
    return _aware(observed, label="database transaction timestamp")


def one_time_action_id(action_type: str, scope_key: str) -> str:
    digest = content_sha256(
        {
            "schema": "aletheia.one_time_external_action_identity.v1",
            "action_type": action_type,
            "scope_key": scope_key,
        }
    )
    return f"act_{digest[:32]}"


def _provider_key(action_id: str, request: dict[str, Any]) -> str:
    digest = content_sha256(
        {
            "schema": "aletheia.external_provider_idempotency.v1",
            "action_id": action_id,
            "request": request,
        }
    )
    return f"aletheia:{digest}"


class OneTimeExternalActionSpec(_FrozenModel):
    action_id: str | None = Field(default=None, pattern=_ACTION_ID_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    action_type: str = Field(pattern=_ACTION_TYPE_PATTERN)
    scope_key: str = Field(pattern=_KEY_PATTERN)
    request: dict[str, Any] = Field(default_factory=dict)
    principal: str = Field(min_length=1, max_length=128)
    provider_idempotency_key: str | None = Field(default=None, pattern=_KEY_PATTERN)
    claim_ttl_seconds: int = Field(default=3600, ge=1, le=604_800)

    @model_validator(mode="after")
    def _normalize_and_bind(self) -> "OneTimeExternalActionSpec":
        request = canonical_payload(self.request)
        action_id = one_time_action_id(self.action_type, self.scope_key)
        if self.action_id is not None and self.action_id != action_id:
            raise ValueError("external action id does not match its type/scope identity")
        provider_key = _provider_key(action_id, request)
        if (
            self.provider_idempotency_key is not None
            and self.provider_idempotency_key != provider_key
        ):
            raise ValueError("external provider idempotency key does not match action content")
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "provider_idempotency_key", provider_key)
        return self

    @property
    def request_sha256(self) -> str:
        return content_sha256(self)


class ExternalActionReceipt(_FrozenModel):
    schema_version: Literal[1] = 1
    action_id: str = Field(pattern=_ACTION_ID_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    action_type: str
    scope_key: str
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_idempotency_key: str
    outcome_sha256: str = Field(pattern=_SHA256_PATTERN)
    outcome: dict[str, Any]
    provider_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider_receipt: dict[str, Any]
    completed_by: str = Field(min_length=1, max_length=128)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _hashes_bind_content(self) -> "ExternalActionReceipt":
        outcome = canonical_payload(self.outcome)
        provider = canonical_payload(self.provider_receipt)
        if content_sha256(outcome) != self.outcome_sha256:
            raise ValueError("external action outcome hash does not match")
        if content_sha256(provider) != self.provider_receipt_sha256:
            raise ValueError("external provider receipt hash does not match")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "provider_receipt", provider)
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ExternalActionSnapshot(_FrozenModel):
    action_id: str = Field(pattern=_ACTION_ID_PATTERN)
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    action_type: str
    scope_key: str
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    request: dict[str, Any]
    principal: str
    provider_idempotency_key: str
    status: ExternalActionStatus
    state_version: int = Field(ge=1)
    claim_owner: str
    claimed_at: AwareDatetime
    reconcile_after: AwareDatetime
    receipt: ExternalActionReceipt | None
    last_event_id: int = Field(ge=1)
    completed_at: AwareDatetime | None


class ExternalActionClaim(_FrozenModel):
    action: ExternalActionSnapshot
    execution_token: str | None = Field(default=None, min_length=32, repr=False)
    created: bool


class ExternalActionCompletion(_FrozenModel):
    action: ExternalActionSnapshot
    receipt: ExternalActionReceipt
    replayed: bool


class ExternalActionRecoveryReceipt(_FrozenModel):
    action_ids: tuple[str, ...]
    recovered_at: AwareDatetime


ClaimMutation = Callable[[Session, str, datetime], None]
CompleteMutation = Callable[[Session, ExternalActionReceipt], None]
FaultHook = Callable[[str, Session], None]


class OneTimeExternalActionStore:
    """Persist claims before effects and accept a single immutable completion receipt."""

    @staticmethod
    def _token_sha256(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt(session: Session, action_id: str) -> ExternalActionReceipt | None:
        row = session.scalar(
            select(ExternalActionReceiptRecord).where(
                ExternalActionReceiptRecord.action_id == action_id
            )
        )
        if row is None:
            return None
        receipt = ExternalActionReceipt.model_validate(row.payload_json)
        if (
            receipt.receipt_sha256 != row.receipt_sha256
            or receipt.request_sha256 != row.request_sha256
            or receipt.outcome_sha256 != row.outcome_sha256
            or receipt.provider_receipt_sha256 != row.provider_receipt_sha256
            or receipt.completed_at != row.completed_at
        ):
            raise ExternalActionInvariantError("external action receipt columns changed")
        event_id = persist_event(
            make_event(
                "external_action_completed",
                run_id=receipt.run_id,
                agent=receipt.completed_by,
                payload=row.event_payload_json,
            ),
            event_key=row.completion_event_key,
            session=session,
        )
        if event_id != row.completion_event_id:
            raise ExternalActionInvariantError("external action completion event changed")
        return receipt

    @classmethod
    def _snapshot(
        cls, session: Session, row: OneTimeExternalActionRecord
    ) -> ExternalActionSnapshot:
        reconstructed = OneTimeExternalActionSpec(
            action_id=row.action_id,
            run_id=row.run_id,
            action_type=row.action_type,
            scope_key=row.scope_key,
            request=row.request_json,
            principal=row.principal,
            provider_idempotency_key=row.provider_idempotency_key,
            claim_ttl_seconds=row.claim_ttl_seconds,
        )
        if reconstructed.request_sha256 != row.request_sha256:
            raise ExternalActionInvariantError("external action request identity changed")
        if row.reconcile_after != row.claimed_at + timedelta(seconds=row.claim_ttl_seconds):
            raise ExternalActionInvariantError("external action reconciliation deadline changed")
        receipt = cls._receipt(session, row.action_id)
        if (row.status == ExternalActionStatus.COMPLETED.value) != (receipt is not None):
            raise ExternalActionInvariantError(
                "external action completion state and receipt disagree"
            )
        if receipt is not None and row.receipt_sha256 != receipt.receipt_sha256:
            raise ExternalActionInvariantError("external action receipt binding changed")
        if row.last_event_id is None:
            raise ExternalActionInvariantError("external action has no durable state event")
        event = session.get(Event, row.last_event_id)
        expected_event_key = f"external-action:{row.action_id}:{row.state_version}"
        expected_event_type = {
            ExternalActionStatus.CLAIMED.value: "external_action_claimed",
            ExternalActionStatus.RECONCILIATION_REQUIRED.value: (
                "external_action_reconciliation_required"
            ),
            ExternalActionStatus.COMPLETED.value: "external_action_completed",
        }[row.status]
        if (
            event is None
            or event.event_key != expected_event_key
            or event.type != expected_event_type
            or event.run_id != row.run_id
            or not isinstance(event.payload, dict)
            or event.payload.get("action_id") != row.action_id
            or event.payload.get("request_sha256") != row.request_sha256
            or event.payload.get("status") != row.status
            or event.payload.get("state_version") != row.state_version
        ):
            raise ExternalActionInvariantError("external action state event binding changed")
        event_projection = {
            "run_id": event.run_id,
            "agent": event.agent,
            "parent_tool_use_id": event.parent_tool_use_id,
            "type": event.type,
            "payload": event.payload,
        }
        if event.event_sha256 != content_sha256(event_projection):
            raise ExternalActionInvariantError("external action state event hash changed")
        if row.status != ExternalActionStatus.COMPLETED.value:
            expected_payload = cls._state_event_payload(
                row,
                transitioned_at=row.updated_at,
            )
            if event.payload != expected_payload:
                raise ExternalActionInvariantError("external action state event content changed")
        return ExternalActionSnapshot(
            action_id=row.action_id,
            run_id=row.run_id,
            action_type=row.action_type,
            scope_key=row.scope_key,
            request_sha256=row.request_sha256,
            request=row.request_json,
            principal=row.principal,
            provider_idempotency_key=row.provider_idempotency_key,
            status=ExternalActionStatus(row.status),
            state_version=row.state_version,
            claim_owner=row.claim_owner,
            claimed_at=row.claimed_at,
            reconcile_after=row.reconcile_after,
            receipt=receipt,
            last_event_id=row.last_event_id,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _verify_spec(row: OneTimeExternalActionRecord, spec: OneTimeExternalActionSpec) -> None:
        expected = {
            "action_id": spec.action_id,
            "run_id": spec.run_id,
            "action_type": spec.action_type,
            "scope_key": spec.scope_key,
            "request_sha256": spec.request_sha256,
            "request_json": spec.request,
            "principal": spec.principal,
            "provider_idempotency_key": spec.provider_idempotency_key,
            "claim_ttl_seconds": spec.claim_ttl_seconds,
        }
        if any(getattr(row, field) != value for field, value in expected.items()):
            raise ExternalActionIdentityConflict(
                "external action identity or scope is already bound to different content"
            )

    @staticmethod
    def _state_event_payload(
        row: OneTimeExternalActionRecord,
        *,
        transitioned_at: datetime,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema": "aletheia.one_time_external_action",
            "schema_version": 1,
            "action_id": row.action_id,
            "action_type": row.action_type,
            "scope_key": row.scope_key,
            "request_sha256": row.request_sha256,
            "provider_idempotency_key": row.provider_idempotency_key,
            "status": row.status,
            "state_version": row.state_version,
            "claim_owner": row.claim_owner,
            "claimed_at": row.claimed_at.isoformat(),
            "reconcile_after": row.reconcile_after.isoformat(),
            "receipt_sha256": row.receipt_sha256,
            "transitioned_at": transitioned_at.isoformat(),
        }
        payload.update(extra or {})
        return canonical_payload(payload)

    @classmethod
    def _emit_state(
        cls,
        session: Session,
        row: OneTimeExternalActionRecord,
        *,
        event_type: str,
        transitioned_at: datetime,
        extra: dict[str, Any] | None = None,
        agent: str | None = None,
    ) -> tuple[int, dict[str, Any], str]:
        payload = cls._state_event_payload(
            row,
            transitioned_at=transitioned_at,
            extra=extra,
        )
        event_key = f"external-action:{row.action_id}:{row.state_version}"
        # ``persist_event`` executes an INSERT and would otherwise trigger ORM autoflush while the
        # action still points at its previous event. Keep the state/version/event-id change in one
        # row update so the database transition trigger can verify it atomically.
        with session.no_autoflush:
            event_id = persist_event(
                make_event(
                    event_type,
                    run_id=row.run_id,
                    agent=agent or row.principal,
                    payload=payload,
                ),
                event_key=event_key,
                session=session,
                flush_pending=False,
            )
        row.last_event_id = event_id
        return event_id, payload, event_key

    def claim(
        self,
        spec: OneTimeExternalActionSpec,
        *,
        claim_owner: str,
        now: datetime | None = None,
        on_claim: ClaimMutation | None = None,
        fault_hook: FaultHook | None = None,
    ) -> ExternalActionClaim:
        """Return a raw execution token only to the transaction that first claims the scope."""

        if not claim_owner or len(claim_owner) > 128:
            raise ValueError("external action claim owner must contain 1-128 characters")
        spec = OneTimeExternalActionSpec.model_validate(spec.model_dump(mode="python"))
        raw_token = secrets.token_urlsafe(32)
        token_sha256 = self._token_sha256(raw_token)
        with session_scope() as session:
            claimed_at = _transaction_time(session, now)
            reconcile_after = claimed_at + timedelta(seconds=spec.claim_ttl_seconds)
            inserted = session.scalar(
                postgresql_insert(OneTimeExternalActionRecord)
                .values(
                    action_id=spec.action_id,
                    run_id=spec.run_id,
                    action_type=spec.action_type,
                    scope_key=spec.scope_key,
                    request_sha256=spec.request_sha256,
                    request_json=spec.request,
                    principal=spec.principal,
                    provider_idempotency_key=spec.provider_idempotency_key,
                    claim_ttl_seconds=spec.claim_ttl_seconds,
                    status=ExternalActionStatus.CLAIMED.value,
                    state_version=1,
                    claim_owner=claim_owner,
                    execution_token_sha256=token_sha256,
                    claimed_at=claimed_at,
                    reconcile_after=reconcile_after,
                    receipt_sha256=None,
                    last_event_id=None,
                    created_at=claimed_at,
                    updated_at=claimed_at,
                    completed_at=None,
                )
                .on_conflict_do_nothing()
                .returning(OneTimeExternalActionRecord.action_id)
            )
            session.flush()
            if inserted is None:
                rows = session.scalars(
                    select(OneTimeExternalActionRecord).where(
                        or_(
                            OneTimeExternalActionRecord.action_id == spec.action_id,
                            OneTimeExternalActionRecord.scope_key == spec.scope_key,
                            OneTimeExternalActionRecord.provider_idempotency_key
                            == spec.provider_idempotency_key,
                        )
                    )
                ).all()
                unique = {row.action_id: row for row in rows}
                if len(unique) != 1:
                    raise ExternalActionIdentityConflict(
                        "external action identity conflicts with multiple intents"
                    )
                row = next(iter(unique.values()))
                self._verify_spec(row, spec)
                return ExternalActionClaim(
                    action=self._snapshot(session, row),
                    execution_token=None,
                    created=False,
                )

            assert spec.action_id is not None
            row = session.get(OneTimeExternalActionRecord, spec.action_id)
            if row is None:  # pragma: no cover - insert just returned this identity
                raise ExternalActionInvariantError("external action claim disappeared")
            if on_claim is not None:
                on_claim(session, row.action_id, claimed_at)
            session.flush()
            if fault_hook is not None:
                fault_hook("after_domain_claim_before_event", session)
            self._emit_state(
                session,
                row,
                event_type="external_action_claimed",
                transitioned_at=claimed_at,
            )
            session.flush()
            if fault_hook is not None:
                fault_hook("before_claim_commit", session)
            return ExternalActionClaim(
                action=self._snapshot(session, row),
                execution_token=raw_token,
                created=True,
            )

    def complete(
        self,
        *,
        action_id: str,
        execution_token: str,
        outcome: dict[str, Any],
        provider_receipt: dict[str, Any],
        completed_by: str,
        now: datetime | None = None,
        event_projection: dict[str, Any] | None = None,
        on_complete: CompleteMutation | None = None,
        fault_hook: FaultHook | None = None,
    ) -> ExternalActionCompletion:
        """Commit one token-bound result, provider receipt, domain state, and keyed event."""

        if not completed_by or len(completed_by) > 128:
            raise ValueError("external action completion principal must contain 1-128 characters")
        outcome = canonical_payload(outcome)
        provider_receipt = canonical_payload(provider_receipt)
        projection = canonical_payload(event_projection or {})
        with session_scope() as session:
            row = session.scalar(
                select(OneTimeExternalActionRecord)
                .where(OneTimeExternalActionRecord.action_id == action_id)
                .with_for_update()
            )
            if row is None:
                raise ExternalActionError(f"external action not found: {action_id}")
            if not hmac.compare_digest(
                row.execution_token_sha256,
                self._token_sha256(execution_token),
            ):
                raise ExternalActionTokenMismatch("external action completion token is invalid")

            existing = self._receipt(session, action_id)
            if existing is not None:
                if existing.outcome != outcome or existing.provider_receipt != provider_receipt:
                    raise InvalidExternalActionTransition(
                        "completed external action cannot be rebound to a different receipt"
                    )
                return ExternalActionCompletion(
                    action=self._snapshot(session, row),
                    receipt=existing,
                    replayed=True,
                )
            if row.status not in {
                ExternalActionStatus.CLAIMED.value,
                ExternalActionStatus.RECONCILIATION_REQUIRED.value,
            }:
                raise InvalidExternalActionTransition(
                    f"cannot complete external action from state {row.status!r}"
                )

            completed_at = _transaction_time(session, now)
            receipt = ExternalActionReceipt(
                action_id=row.action_id,
                run_id=row.run_id,
                action_type=row.action_type,
                scope_key=row.scope_key,
                request_sha256=row.request_sha256,
                provider_idempotency_key=row.provider_idempotency_key,
                outcome_sha256=content_sha256(outcome),
                outcome=outcome,
                provider_receipt_sha256=content_sha256(provider_receipt),
                provider_receipt=provider_receipt,
                completed_by=completed_by,
                completed_at=completed_at,
            )
            row.status = ExternalActionStatus.COMPLETED.value
            row.state_version += 1
            row.receipt_sha256 = receipt.receipt_sha256
            row.updated_at = completed_at
            row.completed_at = completed_at
            event_extra = {
                "receipt_sha256": receipt.receipt_sha256,
                "outcome_sha256": receipt.outcome_sha256,
                "provider_receipt_sha256": receipt.provider_receipt_sha256,
                "projection": projection,
            }
            event_id, event_payload, event_key = self._emit_state(
                session,
                row,
                event_type="external_action_completed",
                transitioned_at=completed_at,
                extra=event_extra,
                agent=completed_by,
            )
            session.add(
                ExternalActionReceiptRecord(
                    receipt_sha256=receipt.receipt_sha256,
                    action_id=row.action_id,
                    request_sha256=row.request_sha256,
                    outcome_sha256=receipt.outcome_sha256,
                    provider_receipt_sha256=receipt.provider_receipt_sha256,
                    payload_json=receipt.model_dump(mode="json"),
                    event_payload_json=event_payload,
                    completion_event_key=event_key,
                    completion_event_id=event_id,
                    completed_at=completed_at,
                )
            )
            # Flush the referenced immutable receipt before a domain ledger stores its composite
            # (action_id, receipt_sha256) foreign key. Everything is still inside this transaction,
            # so a later callback/fault rolls the action, event, receipt, and domain state back.
            session.flush()
            if fault_hook is not None:
                fault_hook("after_receipt_before_domain_result", session)
            if on_complete is not None:
                on_complete(session, receipt)
            session.flush()
            if fault_hook is not None:
                fault_hook("after_domain_result_before_commit", session)
                fault_hook("before_completion_commit", session)
            return ExternalActionCompletion(
                action=self._snapshot(session, row),
                receipt=receipt,
                replayed=False,
            )

    def recover_stale(
        self,
        *,
        now: datetime | None = None,
        principal: str = "external-action-recovery",
        limit: int = 100,
    ) -> ExternalActionRecoveryReceipt:
        """Require reconciliation for expired claims without ever issuing another token."""

        if not 1 <= limit <= 10_000:
            raise ValueError("external action recovery limit must be between 1 and 10000")
        if not principal or len(principal) > 128:
            raise ValueError("external action recovery principal must contain 1-128 characters")
        with session_scope() as session:
            recovered_at = _transaction_time(session, now)
            rows = session.scalars(
                select(OneTimeExternalActionRecord)
                .where(
                    OneTimeExternalActionRecord.status == ExternalActionStatus.CLAIMED.value,
                    OneTimeExternalActionRecord.reconcile_after <= recovered_at,
                )
                .order_by(OneTimeExternalActionRecord.reconcile_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            action_ids: list[str] = []
            for row in rows:
                row.status = ExternalActionStatus.RECONCILIATION_REQUIRED.value
                row.state_version += 1
                row.updated_at = recovered_at
                self._emit_state(
                    session,
                    row,
                    event_type="external_action_reconciliation_required",
                    transitioned_at=recovered_at,
                    agent=principal,
                )
                action_ids.append(row.action_id)
            return ExternalActionRecoveryReceipt(
                action_ids=tuple(action_ids),
                recovered_at=recovered_at,
            )

    def get(self, action_id: str) -> ExternalActionSnapshot:
        with session_scope() as session:
            row = session.get(OneTimeExternalActionRecord, action_id)
            if row is None:
                raise ExternalActionError(f"external action not found: {action_id}")
            return self._snapshot(session, row)


__all__ = [
    "ExternalActionClaim",
    "ExternalActionCompletion",
    "ExternalActionError",
    "ExternalActionIdentityConflict",
    "ExternalActionInvariantError",
    "ExternalActionReceipt",
    "ExternalActionRecoveryReceipt",
    "ExternalActionSnapshot",
    "ExternalActionStatus",
    "ExternalActionTokenMismatch",
    "ExternalActionType",
    "InvalidExternalActionTransition",
    "OneTimeExternalActionSpec",
    "OneTimeExternalActionStore",
    "one_time_action_id",
]
