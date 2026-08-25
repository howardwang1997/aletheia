"""Atomic SEA preregistration and qualification-only execution reservation.

The external execution authority signs a complete scientific execution authorization before this
module is called.  This registrar owns no signing key: it verifies that authorization and commits
its append-only registration in the same PostgreSQL transaction as the PR-4 qualification
admission and resource reservation.  The returned receipt intentionally omits the lease token and
grants neither launch nor scientific-observation admission authority.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.execution.allocator import PostgreSQLExecutionAllocator, ReservationClaim
from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
from aletheia.observations.scientific_bridge import (
    EngineeringQualificationCustodyVerificationPort,
    ResearchActionAuthorityVerificationPort,
    ScientificBridgeAuthorityPin,
    ScientificBridgeModel,
    ScientificExecutionAuthorization,
    verify_scientific_execution_authorization,
    verify_scientific_execution_authorization_historical,
)
from aletheia.observations.store import (
    ObservationIdentityConflict,
    ScientificExecutionAuthorizationWrite,
    get_scientific_execution_authorization_by_slot,
    register_scientific_execution_authorization,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_PATTERN = r"^qst_[0-9a-f]{32}$"
_SLOT_PATTERN = r"^sos_[0-9a-f]{32}$"
_EXECUTION_PATTERN = r"^exe_[0-9a-f]{32}$"
_ATTEMPT_PATTERN = r"^iat_[0-9a-f]{32}$"


class ScientificExecutionRegistrationError(RuntimeError):
    """The SEA and qualification reservation could not commit as one exact transaction."""


@dataclass(frozen=True)
class ScientificExecutionRegistrationVerificationContext:
    """Public verification authorities required before the reservation is admitted."""

    qualification_authority: QualificationAuthorityVerifier
    action_authority: ResearchActionAuthorityVerificationPort
    qualification_custody: EngineeringQualificationCustodyVerificationPort
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin


class AtomicScientificExecutionRegistrationReceipt(ScientificBridgeModel):
    """Non-secret proof that SEA registration and PR-4 reservation committed together."""

    schema_name: Literal["aletheia.atomic_scientific_execution_registration_receipt"] = (
        "aletheia.atomic_scientific_execution_registration_receipt"
    )
    schema_version: Literal[1] = 1
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    quest_id: str = Field(pattern=_QUEST_PATTERN)
    scientific_slot_id: str = Field(pattern=_SLOT_PATTERN)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_id: str = Field(pattern=_EXECUTION_PATTERN)
    attempt_id: str = Field(pattern=_ATTEMPT_PATTERN)
    qualification_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    registered_at: AwareDatetime
    qualification_admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_reservation_sha256: str = Field(pattern=_SHA256_PATTERN)
    reserved_at: AwareDatetime
    authorization_registration_created: bool
    qualification_reservation_created: bool
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _chronology_is_strict(self) -> "AtomicScientificExecutionRegistrationReceipt":
        if not self.registered_at < self.reserved_at:
            raise ValueError("scientific authorization was not registered before reservation")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
DatabaseClock = Callable[[Session], datetime]


def _database_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL production path
        raise ScientificExecutionRegistrationError(
            "PostgreSQL did not provide the execution-registration linearization time"
        )
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
        raise ScientificExecutionRegistrationError(
            "execution-registration database time must be timezone-aware UTC"
        )
    return observed


def _lock_scientific_slot(session: Session, scientific_slot_id: str) -> None:
    session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"scientific-execution-registration:{scientific_slot_id}", 0)
            )
        )
    )


class PostgreSQLAtomicScientificExecutionRegistrar:
    """Commit one signed SEA and its exact PR-4 reservation without a crash gap."""

    def __init__(
        self,
        *,
        verification: ScientificExecutionRegistrationVerificationContext,
        allocator: PostgreSQLExecutionAllocator,
        session_scope_factory: SessionScopeFactory = session_scope,
        database_clock: DatabaseClock = _database_time,
    ) -> None:
        if not isinstance(allocator, PostgreSQLExecutionAllocator):
            raise TypeError("scientific execution registration requires the PR-4 allocator")
        if allocator.runtime_control_issuance_enabled:
            raise ValueError("execution-registration worker cannot load a runtime-control signer")
        if not allocator.runtime_control_verification_enabled:
            raise ValueError("execution registration requires public-key runtime verification")
        if not callable(session_scope_factory) or not callable(database_clock):
            raise TypeError("execution registration requires callable PostgreSQL seams")
        self._verification = verification
        self._allocator = allocator
        self._session_scope_factory = session_scope_factory
        self._database_clock = database_clock

    def register_and_reserve(
        self,
        authorization: ScientificExecutionAuthorization,
    ) -> AtomicScientificExecutionRegistrationReceipt:
        try:
            authorization = ScientificExecutionAuthorization.model_validate(
                authorization.model_dump(mode="python")
            )
            message = authorization.message
            binding = message.action_protocol_binding
            quest_id = binding.action.quest_id
            scientific_slot_id = message.scientific_slot_id
            with self._session_scope_factory() as session:
                if not isinstance(session, Session):
                    raise ScientificExecutionRegistrationError(
                        "execution registration requires one SQLAlchemy Session transaction"
                    )
                _lock_scientific_slot(session, scientific_slot_id)
                if not session.in_transaction():  # pragma: no cover - lock always autobegins
                    raise ScientificExecutionRegistrationError(
                        "execution registration failed to begin its PostgreSQL transaction"
                    )
                observed_at = self._database_clock(session)
                existing = get_scientific_execution_authorization_by_slot(
                    session,
                    quest_id=quest_id,
                    scientific_slot_id=scientific_slot_id,
                )
                if existing is None:
                    verify_scientific_execution_authorization(
                        authorization=authorization,
                        qualification_authority=self._verification.qualification_authority,
                        action_authority=self._verification.action_authority,
                        qualification_custody=self._verification.qualification_custody,
                        execution_authority_pin=self._verification.execution_authority_pin,
                        validator_authority_pin=self._verification.validator_authority_pin,
                        admission_authority_pin=self._verification.admission_authority_pin,
                        observed_at=observed_at,
                    )
                    append = register_scientific_execution_authorization(
                        session,
                        ScientificExecutionAuthorizationWrite.from_contract(
                            authorization,
                            registered_at=observed_at,
                        ),
                    )
                    registered_at = observed_at
                    registration_created = append.created
                else:
                    persisted = ScientificExecutionAuthorization.model_validate(
                        existing.authorization_json
                    )
                    if persisted != authorization:
                        raise ObservationIdentityConflict(
                            "scientific slot is registered to another execution authorization"
                        )
                    verify_scientific_execution_authorization_historical(
                        authorization=persisted,
                        qualification_authority=self._verification.qualification_authority,
                        execution_authority_pin=self._verification.execution_authority_pin,
                        validator_authority_pin=self._verification.validator_authority_pin,
                        admission_authority_pin=self._verification.admission_authority_pin,
                        observed_at=observed_at,
                    )
                    registered_at = existing.registered_at
                    registration_created = False

                claim = self._allocator.admit_and_reserve_in_session(
                    session,
                    bundle=message.qualification_bundle,
                    grant=message.qualification_grant,
                )
                return self._receipt(
                    authorization=authorization,
                    registered_at=registered_at,
                    registration_created=registration_created,
                    claim=claim,
                )
        except ScientificExecutionRegistrationError:
            raise
        except Exception as exc:  # noqa: BLE001 - rollback and fail closed across both authorities
            raise ScientificExecutionRegistrationError(
                "scientific authorization and qualification reservation failed atomic registration"
            ) from exc

    @staticmethod
    def _receipt(
        *,
        authorization: ScientificExecutionAuthorization,
        registered_at: datetime,
        registration_created: bool,
        claim: ReservationClaim,
    ) -> AtomicScientificExecutionRegistrationReceipt:
        message = authorization.message
        binding = message.action_protocol_binding
        intent = message.qualification_bundle.intent
        snapshot = claim.snapshot
        attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
        if (
            snapshot.execution_id != intent.execution_id
            or snapshot.attempt_id != attempt_id
            or snapshot.intent_sha256 != intent.intent_sha256
            or snapshot.bundle_sha256 != message.qualification_bundle.bundle_sha256
            or snapshot.grant_sha256 != message.qualification_grant.grant_sha256
            or snapshot.status != "reserved"
        ):
            raise ScientificExecutionRegistrationError(
                "allocator reservation differs from the signed scientific authorization"
            )
        return AtomicScientificExecutionRegistrationReceipt(
            authorization_sha256=authorization.authorization_sha256,
            quest_id=binding.action.quest_id,
            scientific_slot_id=message.scientific_slot_id,
            action_sha256=binding.action.object_sha256,
            execution_id=intent.execution_id,
            attempt_id=attempt_id,
            qualification_bundle_sha256=message.qualification_bundle.bundle_sha256,
            qualification_grant_sha256=message.qualification_grant.grant_sha256,
            registered_at=registered_at,
            qualification_admission_sha256=snapshot.admission_sha256,
            resource_reservation_sha256=snapshot.resource_lease_sha256,
            reserved_at=snapshot.reserved_at,
            authorization_registration_created=registration_created,
            qualification_reservation_created=claim.created,
        )


__all__ = [
    "AtomicScientificExecutionRegistrationReceipt",
    "PostgreSQLAtomicScientificExecutionRegistrar",
    "ScientificExecutionRegistrationError",
    "ScientificExecutionRegistrationVerificationContext",
]
