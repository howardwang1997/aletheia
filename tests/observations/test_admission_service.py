from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations.admission_service import (
    IndependentAdmissionVerificationContext,
    IndependentObservationAdmissionError,
    IndependentObservationAdmissionService,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    ObservationAdmissionDisposition,
)

_TEST_ROOT = Path(__file__).resolve().parent
if str(_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_TEST_ROOT))

from test_scientific_bridge import (  # noqa: E402
    ADMISSION_PRIVATE_KEY,
    _bridge_case,
    _issue_admission_decision,
    _validated_receipt,
)


def _service(case, *, decided_at) -> IndependentObservationAdmissionService:
    return IndependentObservationAdmissionService(
        verification=IndependentAdmissionVerificationContext(
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
        ),
        admission_private_key=ADMISSION_PRIVATE_KEY,
        clock=lambda: decided_at,
    )


def test_independent_admitter_mechanically_admits_a_validated_observation() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case)
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )

    decided_at = expected.message.issuance_challenge.message.issued_at + timedelta(seconds=1)
    decision = _service(case, decided_at=decided_at).issue_admission_decision(
        committed_validation=committed,
        issuance_challenge=expected.message.issuance_challenge,
    )

    assert decision.message.committed_validation_receipt == committed
    assert decision.message.issuance_challenge == expected.message.issuance_challenge
    assert decision.message.decided_at == decided_at
    assert decision.message.disposition is ObservationAdmissionDisposition.ADMITTED
    assert decision.message.reason_codes == ()
    assert decision.message.persistence_committed is False
    assert decision.scientific_authority_conferred is False


def test_independent_admitter_preserves_validation_rejection_reasons() -> None:
    case = _bridge_case()
    blockers = ("protocol:material_deviation",)
    receipt = _validated_receipt(
        case,
        disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
        blocker_codes=blockers,
    )
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.REJECTED,
        reason_codes=blockers,
    )

    decision = _service(
        case,
        decided_at=expected.message.issuance_challenge.message.issued_at,
    ).issue_admission_decision(
        committed_validation=committed,
        issuance_challenge=expected.message.issuance_challenge,
    )

    assert decision == expected
    assert decision.message.disposition is ObservationAdmissionDisposition.REJECTED
    assert decision.message.admitted_observation_sha256 is None
    assert decision.message.reason_codes == blockers


def test_independent_admitter_reverifies_nested_custody_and_live_db_challenge() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case)
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    case.raw_run_custody.fail = True

    with pytest.raises(IndependentObservationAdmissionError, match="failed closed"):
        _service(
            case,
            decided_at=expected.message.issuance_challenge.message.issued_at,
        ).issue_admission_decision(
            committed_validation=committed,
            issuance_challenge=expected.message.issuance_challenge,
        )


def test_independent_admitter_rejects_another_private_key() -> None:
    case = _bridge_case()

    with pytest.raises(IndependentObservationAdmissionError, match="differs"):
        IndependentObservationAdmissionService(
            verification=IndependentAdmissionVerificationContext(
                qualification_authority=case.qualification_authority,
                action_authority=case.action_authority,
                qualification_custody=case.qualification_custody,
                raw_run_custody=case.raw_run_custody,
                validation_campaign_custody=case.validation_campaign_custody,
                execution_authority_pin=case.execution_pin,
                validator_authority_pin=case.validator_pin,
                admission_authority_pin=case.admission_pin,
                database_authority_pin=case.database_pin,
            ),
            admission_private_key=hashlib.sha256(b"another-admission-key").digest(),
            clock=lambda: case.authorization.message.authorized_at,
        )


def test_independent_admitter_rejects_an_expired_database_challenge() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case)
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    challenge = expected.message.issuance_challenge

    with pytest.raises(IndependentObservationAdmissionError, match="failed closed"):
        _service(case, decided_at=challenge.message.expires_at).issue_admission_decision(
            committed_validation=committed,
            issuance_challenge=challenge,
        )


def test_independent_admitter_rejects_database_time_rollback() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case)
    expected, committed = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    challenge = expected.message.issuance_challenge
    times = iter(
        (
            challenge.message.issued_at + timedelta(seconds=2),
            challenge.message.issued_at + timedelta(seconds=1),
        )
    )
    service = IndependentObservationAdmissionService(
        verification=IndependentAdmissionVerificationContext(
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
        ),
        admission_private_key=ADMISSION_PRIVATE_KEY,
        clock=lambda: next(times),
    )

    with pytest.raises(IndependentObservationAdmissionError, match="failed closed"):
        service.issue_admission_decision(
            committed_validation=committed,
            issuance_challenge=challenge,
        )
