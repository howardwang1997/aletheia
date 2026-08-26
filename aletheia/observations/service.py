"""Durable DB-time services for the independent scientific-observation bridge."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.observations.coordinator import ObservationAdmissionVerificationContext
from aletheia.observations.scientific_bridge import (
    AdmissionIssuanceChallenge,
    CommittedObservationValidationReceipt,
    ObservationValidationReceipt,
    RawRunEnvelope,
    ScientificExecutionAuthorization,
    ValidationIssuanceChallenge,
    commit_observation_validation_receipt,
    issue_admission_issuance_challenge,
    issue_validation_issuance_challenge,
    verify_admission_issuance_challenge,
    verify_committed_observation_validation_receipt,
    verify_validation_issuance_challenge,
)
from aletheia.observations.store import (
    ObservationIdentityConflict,
    ObservationIssuanceChallengeWrite,
    ObservationValidationReceiptWrite,
    ScientificExecutionAuthorizationWrite,
    get_live_observation_issuance_challenge,
    get_observation_validation_receipt_by_slot,
    get_scientific_execution_authorization_by_slot,
    record_observation_issuance_challenge,
    record_observation_validation_receipt,
)


class ScientificBridgeServiceError(RuntimeError):
    """A durable scientific-bridge operation failed closed."""


@dataclass(frozen=True)
class ValidationChallengeRegistrationReceipt:
    challenge: ValidationIssuanceChallenge
    recorded_at: datetime
    created: bool


@dataclass(frozen=True)
class AdmissionChallengeRegistrationReceipt:
    challenge: AdmissionIssuanceChallenge
    recorded_at: datetime
    created: bool


@dataclass(frozen=True)
class ValidationCommitReceipt:
    committed_validation: CommittedObservationValidationReceipt
    created: bool


SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
DatabaseClock = Callable[[Session], datetime]
NonceFactory = Callable[[], str]


def _database_time(session: Session) -> datetime:
    observed = session.scalar(select(func.clock_timestamp()))
    if not isinstance(observed, datetime):  # pragma: no cover - PostgreSQL production path
        raise ScientificBridgeServiceError(
            "PostgreSQL did not provide the bridge linearization time"
        )
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0):
        raise ScientificBridgeServiceError("bridge database time must be UTC")
    return observed


def _nonce_sha256() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _quest_and_authorization(
    authorization: ScientificExecutionAuthorization,
) -> tuple[str, str, str]:
    message = authorization.message
    return (
        message.action_protocol_binding.action.quest_id,
        message.scientific_slot_id,
        authorization.authorization_sha256,
    )


class PostgreSQLScientificBridgeService:
    """Issue DB-signed challenge/validation proofs for an atomically registered SEA."""

    def __init__(
        self,
        *,
        verification: ObservationAdmissionVerificationContext,
        challenge_ttl: timedelta = timedelta(minutes=5),
        session_scope_factory: SessionScopeFactory = session_scope,
        database_clock: DatabaseClock = _database_time,
        nonce_factory: NonceFactory = _nonce_sha256,
    ) -> None:
        if challenge_ttl <= timedelta(0) or challenge_ttl > timedelta(hours=1):
            raise ValueError("observation challenge TTL must be in (0, 1 hour]")
        self._verification = verification
        self._challenge_ttl = challenge_ttl
        self._session_scope_factory = session_scope_factory
        self._database_clock = database_clock
        self._nonce_factory = nonce_factory

    def issue_validation_challenge(
        self,
        *,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
    ) -> ValidationChallengeRegistrationReceipt:
        authorization = raw_run.scientific_authorization
        quest_id, scientific_slot_id, authorization_sha256 = _quest_and_authorization(authorization)
        with self._session_scope_factory() as session:
            issued_at = self._database_clock(session)
            self._require_registered_authorization(
                session=session,
                authorization=authorization,
            )
            existing = get_live_observation_issuance_challenge(
                session,
                purpose="validation",
                quest_id=quest_id,
                scientific_slot_id=scientific_slot_id,
                authorization_sha256=authorization_sha256,
                observed_at=issued_at,
            )
            if existing is not None:
                return self._recover_validation_challenge(
                    write=existing,
                    raw_run=raw_run,
                    validation_campaign_sha256=validation_campaign_sha256,
                    observed_at=issued_at,
                )
            expires_at = min(
                issued_at + self._challenge_ttl,
                authorization.message.observation_admission_deadline,
                self._verification.database_authority_pin.active_until,
            )
            challenge = issue_validation_issuance_challenge(
                raw_run=raw_run,
                validation_campaign_sha256=validation_campaign_sha256,
                nonce_sha256=self._nonce_factory(),
                database_authority_pin=self._verification.database_authority_pin,
                private_key=self._verification.database_private_key,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            recorded_at = self._database_clock(session)
            write = ObservationIssuanceChallengeWrite.from_contract(
                challenge,
                quest_id=quest_id,
                authorization_sha256=authorization_sha256,
                recorded_at=recorded_at,
            )
            try:
                append = record_observation_issuance_challenge(session, write)
            except ObservationIdentityConflict:
                concurrent = get_live_observation_issuance_challenge(
                    session,
                    purpose="validation",
                    quest_id=quest_id,
                    scientific_slot_id=scientific_slot_id,
                    authorization_sha256=authorization_sha256,
                    observed_at=recorded_at,
                )
                if concurrent is None:
                    raise
                return self._recover_validation_challenge(
                    write=concurrent,
                    raw_run=raw_run,
                    validation_campaign_sha256=validation_campaign_sha256,
                    observed_at=recorded_at,
                )
            return ValidationChallengeRegistrationReceipt(
                challenge=challenge,
                recorded_at=recorded_at,
                created=append.created,
            )

    def commit_validation(
        self,
        receipt: ObservationValidationReceipt,
    ) -> ValidationCommitReceipt:
        receipt = ObservationValidationReceipt.model_validate(receipt.model_dump(mode="python"))
        raw_run = receipt.message.raw_run
        authorization = raw_run.scientific_authorization
        quest_id, scientific_slot_id, _ = _quest_and_authorization(authorization)
        with self._session_scope_factory() as session:
            observed_at = self._database_clock(session)
            self._require_registered_authorization(
                session=session,
                authorization=authorization,
            )
            existing = get_observation_validation_receipt_by_slot(
                session,
                quest_id=quest_id,
                scientific_slot_id=scientific_slot_id,
            )
            if existing is not None:
                committed = CommittedObservationValidationReceipt.model_validate(
                    existing.committed_receipt_json
                )
                if committed.message.receipt != receipt:
                    raise ObservationIdentityConflict(
                        "scientific slot is committed to another validation receipt"
                    )
                verify_committed_observation_validation_receipt(
                    committed_receipt=committed,
                    qualification_authority=self._verification.qualification_authority,
                    action_authority=self._verification.action_authority,
                    qualification_custody=self._verification.qualification_custody,
                    raw_run_custody=self._verification.raw_run_custody,
                    validation_campaign_custody=(self._verification.validation_campaign_custody),
                    execution_authority_pin=self._verification.execution_authority_pin,
                    validator_authority_pin=self._verification.validator_authority_pin,
                    admission_authority_pin=self._verification.admission_authority_pin,
                    database_authority_pin=self._verification.database_authority_pin,
                    observed_at=observed_at,
                )
                return ValidationCommitReceipt(
                    committed_validation=committed,
                    created=False,
                )

            registered_at = observed_at
            committed_at = self._database_clock(session)
            committed = commit_observation_validation_receipt(
                receipt=receipt,
                qualification_authority=self._verification.qualification_authority,
                action_authority=self._verification.action_authority,
                qualification_custody=self._verification.qualification_custody,
                raw_run_custody=self._verification.raw_run_custody,
                validation_campaign_custody=self._verification.validation_campaign_custody,
                execution_authority_pin=self._verification.execution_authority_pin,
                validator_authority_pin=self._verification.validator_authority_pin,
                admission_authority_pin=self._verification.admission_authority_pin,
                database_authority_pin=self._verification.database_authority_pin,
                private_key=self._verification.database_private_key,
                registered_at=registered_at,
                committed_at=committed_at,
            )
            append = record_observation_validation_receipt(
                session,
                ObservationValidationReceiptWrite.from_contract(
                    committed,
                    quest_id=quest_id,
                ),
            )
            return ValidationCommitReceipt(
                committed_validation=committed,
                created=append.created,
            )

    def issue_admission_challenge(
        self,
        committed_validation: CommittedObservationValidationReceipt,
    ) -> AdmissionChallengeRegistrationReceipt:
        committed_validation = CommittedObservationValidationReceipt.model_validate(
            committed_validation.model_dump(mode="python")
        )
        receipt_message = committed_validation.message.receipt.message
        authorization = receipt_message.raw_run.scientific_authorization
        quest_id, scientific_slot_id, authorization_sha256 = _quest_and_authorization(authorization)
        with self._session_scope_factory() as session:
            issued_at = self._database_clock(session)
            self._require_registered_authorization(
                session=session,
                authorization=authorization,
            )
            persisted = get_observation_validation_receipt_by_slot(
                session,
                quest_id=quest_id,
                scientific_slot_id=scientific_slot_id,
            )
            if (
                persisted is None
                or persisted.committed_receipt_sha256
                != committed_validation.committed_receipt_sha256
                or CommittedObservationValidationReceipt.model_validate(
                    persisted.committed_receipt_json
                )
                != committed_validation
            ):
                raise ScientificBridgeServiceError(
                    "admission challenge requires the exact durably committed validation"
                )
            existing = get_live_observation_issuance_challenge(
                session,
                purpose="admission",
                quest_id=quest_id,
                scientific_slot_id=scientific_slot_id,
                authorization_sha256=authorization_sha256,
                observed_at=issued_at,
            )
            if existing is not None:
                return self._recover_admission_challenge(
                    write=existing,
                    committed_validation=committed_validation,
                    observed_at=issued_at,
                )
            expires_at = min(
                issued_at + self._challenge_ttl,
                authorization.message.observation_admission_deadline,
                self._verification.database_authority_pin.active_until,
            )
            challenge = issue_admission_issuance_challenge(
                committed_validation_receipt=committed_validation,
                nonce_sha256=self._nonce_factory(),
                database_authority_pin=self._verification.database_authority_pin,
                private_key=self._verification.database_private_key,
                issued_at=issued_at,
                expires_at=expires_at,
            )
            recorded_at = self._database_clock(session)
            write = ObservationIssuanceChallengeWrite.from_contract(
                challenge,
                quest_id=quest_id,
                authorization_sha256=authorization_sha256,
                recorded_at=recorded_at,
            )
            try:
                append = record_observation_issuance_challenge(session, write)
            except ObservationIdentityConflict:
                concurrent = get_live_observation_issuance_challenge(
                    session,
                    purpose="admission",
                    quest_id=quest_id,
                    scientific_slot_id=scientific_slot_id,
                    authorization_sha256=authorization_sha256,
                    observed_at=recorded_at,
                )
                if concurrent is None:
                    raise
                return self._recover_admission_challenge(
                    write=concurrent,
                    committed_validation=committed_validation,
                    observed_at=recorded_at,
                )
            return AdmissionChallengeRegistrationReceipt(
                challenge=challenge,
                recorded_at=recorded_at,
                created=append.created,
            )

    @staticmethod
    def _require_registered_authorization(
        *,
        session: Session,
        authorization: ScientificExecutionAuthorization,
    ) -> ScientificExecutionAuthorizationWrite:
        quest_id, scientific_slot_id, authorization_sha256 = _quest_and_authorization(authorization)
        registered = get_scientific_execution_authorization_by_slot(
            session,
            quest_id=quest_id,
            scientific_slot_id=scientific_slot_id,
        )
        if (
            registered is None
            or registered.authorization_sha256 != authorization_sha256
            or ScientificExecutionAuthorization.model_validate(registered.authorization_json)
            != authorization
        ):
            raise ScientificBridgeServiceError(
                "bridge operation requires the exact preregistered scientific authorization"
            )
        return registered

    def _recover_validation_challenge(
        self,
        *,
        write: ObservationIssuanceChallengeWrite,
        raw_run: RawRunEnvelope,
        validation_campaign_sha256: str | None,
        observed_at: datetime,
    ) -> ValidationChallengeRegistrationReceipt:
        challenge = ValidationIssuanceChallenge.model_validate(write.challenge_json)
        if (
            challenge.message.raw_run_sha256 != raw_run.raw_run_sha256
            or challenge.message.validation_campaign_sha256 != validation_campaign_sha256
        ):
            raise ObservationIdentityConflict(
                "live validation challenge belongs to another raw run or campaign"
            )
        verify_validation_issuance_challenge(
            challenge=challenge,
            raw_run=raw_run,
            expected_validation_campaign_sha256=validation_campaign_sha256,
            database_authority_pin=self._verification.database_authority_pin,
            observed_at=observed_at,
        )
        return ValidationChallengeRegistrationReceipt(
            challenge=challenge,
            recorded_at=write.recorded_at,
            created=False,
        )

    def _recover_admission_challenge(
        self,
        *,
        write: ObservationIssuanceChallengeWrite,
        committed_validation: CommittedObservationValidationReceipt,
        observed_at: datetime,
    ) -> AdmissionChallengeRegistrationReceipt:
        challenge = AdmissionIssuanceChallenge.model_validate(write.challenge_json)
        if (
            challenge.message.committed_validation_receipt_sha256
            != committed_validation.committed_receipt_sha256
        ):
            raise ObservationIdentityConflict(
                "live admission challenge belongs to another validation receipt"
            )
        verify_admission_issuance_challenge(
            challenge=challenge,
            committed_validation_receipt=committed_validation,
            database_authority_pin=self._verification.database_authority_pin,
            observed_at=observed_at,
        )
        return AdmissionChallengeRegistrationReceipt(
            challenge=challenge,
            recorded_at=write.recorded_at,
            created=False,
        )


__all__ = [
    "AdmissionChallengeRegistrationReceipt",
    "PostgreSQLScientificBridgeService",
    "ScientificBridgeServiceError",
    "ValidationChallengeRegistrationReceipt",
    "ValidationCommitReceipt",
]
