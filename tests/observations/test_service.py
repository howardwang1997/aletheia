from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations import service as service_module
from aletheia.observations.coordinator import ObservationAdmissionVerificationContext
from aletheia.observations.scientific_bridge import ValidationIssuanceChallenge
from aletheia.observations.service import PostgreSQLScientificBridgeService
from aletheia.observations.store import (
    AppendReceipt,
    ObservationIssuanceChallengeWrite,
    ObservationValidationReceiptWrite,
    ScientificExecutionAuthorizationWrite,
)

_OBSERVATION_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_OBSERVATION_TESTS))
from test_scientific_bridge import (  # noqa: E402
    DATABASE_PRIVATE_KEY,
    _bridge_case,
    _digest,
    _raw_run,
    _validated_receipt,
)


class _Clock:
    def __init__(self, *values) -> None:
        self.values = list(values)

    def __call__(self, _session):
        if not self.values:
            raise AssertionError("test database clock was exhausted")
        return self.values.pop(0)


@contextmanager
def _session_scope():
    yield object()


def _verification(case) -> ObservationAdmissionVerificationContext:
    return ObservationAdmissionVerificationContext(
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        database_private_key=DATABASE_PRIVATE_KEY,
    )


def _install_memory_store(monkeypatch: pytest.MonkeyPatch):
    authorizations: dict[str, ScientificExecutionAuthorizationWrite] = {}
    challenges: list[ObservationIssuanceChallengeWrite] = []
    validations: dict[str, ObservationValidationReceiptWrite] = {}

    def get_authorization(_session, *, quest_id, scientific_slot_id):
        item = authorizations.get(scientific_slot_id)
        return item if item is not None and item.quest_id == quest_id else None

    def get_live_challenge(
        _session,
        *,
        purpose,
        quest_id,
        scientific_slot_id,
        authorization_sha256,
        observed_at,
    ):
        matches = tuple(
            item
            for item in challenges
            if item.purpose == purpose
            and item.quest_id == quest_id
            and item.scientific_slot_id == scientific_slot_id
            and item.authorization_sha256 == authorization_sha256
            and item.expires_at > observed_at
        )
        assert len(matches) <= 1
        return matches[0] if matches else None

    def get_challenge(_session, *, challenge_sha256):
        matches = tuple(item for item in challenges if item.challenge_sha256 == challenge_sha256)
        assert len(matches) <= 1
        return matches[0] if matches else None

    def record_challenge(_session, write):
        challenges.append(write)
        return AppendReceipt(identity_sha256=write.challenge_sha256, created=True)

    def get_validation(_session, *, quest_id, scientific_slot_id):
        item = validations.get(scientific_slot_id)
        return item if item is not None and item.quest_id == quest_id else None

    def record_validation(_session, write):
        validations[write.scientific_slot_id] = write
        return AppendReceipt(identity_sha256=write.committed_receipt_sha256, created=True)

    monkeypatch.setattr(
        service_module, "lock_scientific_execution_authorization_by_slot", get_authorization
    )
    monkeypatch.setattr(
        service_module, "get_live_observation_issuance_challenge", get_live_challenge
    )
    monkeypatch.setattr(
        service_module,
        "get_observation_issuance_challenge_by_sha256",
        get_challenge,
    )
    monkeypatch.setattr(service_module, "record_observation_issuance_challenge", record_challenge)
    monkeypatch.setattr(
        service_module, "get_observation_validation_receipt_by_slot", get_validation
    )
    monkeypatch.setattr(service_module, "record_observation_validation_receipt", record_validation)
    return authorizations, challenges, validations


def test_atomically_preregistered_sea_and_live_validation_challenge_resume_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    authorizations, challenges, _validations = _install_memory_store(monkeypatch)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    authorizations[case.authorization.message.scientific_slot_id] = (
        ScientificExecutionAuthorizationWrite.from_contract(
            case.authorization,
            registered_at=registered_at,
        )
    )

    raw_run = _raw_run(case)
    issued_at = raw_run.assembled_at + timedelta(minutes=1)
    challenge_service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(
            issued_at,
            issued_at + timedelta(seconds=1),
            issued_at + timedelta(seconds=2),
        ),
        nonce_factory=lambda: _digest("validation-service-nonce"),
    )
    campaign_sha256 = _digest("validation-service-campaign")

    first = challenge_service.issue_validation_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
    )
    recovered = challenge_service.issue_validation_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
    )

    assert recovered == first
    assert first.durably_registered is True
    assert len(challenges) == 1
    assert (
        ValidationIssuanceChallenge.model_validate(challenges[0].challenge_json) == first.challenge
    )


def test_validation_commit_and_admission_challenge_resume_from_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    authorizations, challenges, validations = _install_memory_store(monkeypatch)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    authorizations[case.authorization.message.scientific_slot_id] = (
        ScientificExecutionAuthorizationWrite.from_contract(
            case.authorization,
            registered_at=registered_at,
        )
    )
    validation = _validated_receipt(case)
    authorization = validation.message.raw_run.scientific_authorization
    challenge = validation.message.issuance_challenge
    challenges.append(
        ObservationIssuanceChallengeWrite.from_contract(
            challenge,
            quest_id=authorization.message.action_protocol_binding.action.quest_id,
            authorization_sha256=authorization.authorization_sha256,
            recorded_at=challenge.message.issued_at,
        )
    )
    commit_at = validation.message.validated_at + timedelta(seconds=1)
    validation_service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(
            commit_at,
            commit_at + timedelta(seconds=1),
            commit_at + timedelta(seconds=2),
            commit_at + timedelta(seconds=3),
        ),
        nonce_factory=lambda: _digest("admission-service-nonce"),
    )

    created = validation_service.commit_validation(validation)
    recovered = validation_service.commit_validation(validation)

    assert recovered == created
    assert created.durably_committed is True
    assert len(validations) == 1

    admission_issued_at = created.committed_validation.message.committed_at + timedelta(minutes=1)
    admission_service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(
            admission_issued_at,
            admission_issued_at + timedelta(seconds=1),
            admission_issued_at + timedelta(seconds=2),
        ),
        nonce_factory=lambda: _digest("admission-service-nonce"),
    )
    first = admission_service.issue_admission_challenge(created.committed_validation)
    replayed = admission_service.issue_admission_challenge(created.committed_validation)

    assert replayed == first
    assert first.durably_registered is True
    assert len(challenges) == 2


def test_service_refuses_validation_without_preregistered_sea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    _install_memory_store(monkeypatch)
    raw_run = _raw_run(case)
    observed_at = raw_run.assembled_at + timedelta(minutes=1)
    service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(observed_at),
    )

    with pytest.raises(RuntimeError, match="preregistered"):
        service.issue_validation_challenge(
            raw_run=raw_run,
            validation_campaign_sha256=_digest("unregistered-campaign"),
        )


def test_validation_commit_requires_the_exact_durably_registered_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    authorizations, _challenges, validations = _install_memory_store(monkeypatch)
    authorization = case.authorization
    authorizations[authorization.message.scientific_slot_id] = (
        ScientificExecutionAuthorizationWrite.from_contract(
            authorization,
            registered_at=authorization.message.authorized_at + timedelta(seconds=1),
        )
    )
    validation = _validated_receipt(case)
    observed_at = validation.message.validated_at + timedelta(seconds=1)
    service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(observed_at),
    )

    with pytest.raises(RuntimeError, match="durably registered challenge"):
        service.commit_validation(validation)

    assert validations == {}


def test_validation_commit_rechecks_database_time_after_expensive_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    authorizations, challenges, validations = _install_memory_store(monkeypatch)
    authorization = case.authorization
    authorizations[authorization.message.scientific_slot_id] = (
        ScientificExecutionAuthorizationWrite.from_contract(
            authorization,
            registered_at=authorization.message.authorized_at + timedelta(seconds=1),
        )
    )
    validation = _validated_receipt(case)
    challenge = validation.message.issuance_challenge
    challenges.append(
        ObservationIssuanceChallengeWrite.from_contract(
            challenge,
            quest_id=authorization.message.action_protocol_binding.action.quest_id,
            authorization_sha256=authorization.authorization_sha256,
            recorded_at=challenge.message.issued_at,
        )
    )
    observed_at = validation.message.validated_at + timedelta(seconds=1)
    service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(
            observed_at,
            observed_at + timedelta(seconds=1),
            challenge.message.expires_at,
        ),
    )

    with pytest.raises(RuntimeError, match="lost its live database challenge window"):
        service.commit_validation(validation)

    assert validations == {}


def test_database_rpc_receipts_have_stable_canonical_retry_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    authorizations, _challenges, _validations = _install_memory_store(monkeypatch)
    authorization = case.authorization
    authorizations[authorization.message.scientific_slot_id] = (
        ScientificExecutionAuthorizationWrite.from_contract(
            authorization,
            registered_at=authorization.message.authorized_at + timedelta(seconds=1),
        )
    )
    raw_run = _raw_run(case)
    issued_at = raw_run.assembled_at + timedelta(minutes=1)
    service = PostgreSQLScientificBridgeService(
        verification=_verification(case),
        session_scope_factory=_session_scope,
        database_clock=_Clock(
            issued_at,
            issued_at + timedelta(seconds=1),
            issued_at + timedelta(seconds=2),
        ),
        nonce_factory=lambda: _digest("stable-receipt-nonce"),
    )

    first = service.issue_validation_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=_digest("stable-receipt-campaign"),
    )
    replayed = service.issue_validation_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=_digest("stable-receipt-campaign"),
    )

    assert first.model_dump_json() == replayed.model_dump_json()
    assert type(first).model_validate_json(first.model_dump_json()) == first
