"""Independent admission-decision signer for verified scientific observations.

The service represented here can sign a proposal for one exact scientific slot, but it cannot
reserve that slot, commit an observation, or mutate the Research Kernel.  Its decision is derived
only from the already committed independent-validation disposition and the database-signed live
issuance challenge.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
from aletheia.observations.scientific_bridge import (
    AdmissionIssuanceChallenge,
    BridgeValidationDisposition,
    CommittedObservationValidationReceipt,
    EngineeringQualificationCustodyVerificationPort,
    ObservationAdmissionDecision,
    ObservationAdmissionDisposition,
    ObservationDatabaseAuthorityPin,
    ObservationValidationCampaignVerificationPort,
    RawRunCustodyVerificationPort,
    ResearchActionAuthorityVerificationPort,
    ScientificBridgeAuthorityPin,
    ScientificBridgeRole,
    issue_observation_admission_decision,
)


class IndependentObservationAdmissionError(RuntimeError):
    """An independent admission proposal failed closed."""


@dataclass(frozen=True)
class IndependentAdmissionVerificationContext:
    """Public authorities and custody ports available to the isolated admitter."""

    qualification_authority: QualificationAuthorityVerifier
    action_authority: ResearchActionAuthorityVerificationPort
    qualification_custody: EngineeringQualificationCustodyVerificationPort
    raw_run_custody: RawRunCustodyVerificationPort
    validation_campaign_custody: ObservationValidationCampaignVerificationPort
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin
    database_authority_pin: ObservationDatabaseAuthorityPin


class IndependentObservationAdmissionService:
    """Sign deterministic, non-authoritative admission proposals outside the controller worker."""

    def __init__(
        self,
        *,
        verification: IndependentAdmissionVerificationContext,
        admission_private_key: bytes,
        clock: Callable[[], datetime],
    ) -> None:
        _require_admission_private_key(
            private_key=admission_private_key,
            pin=verification.admission_authority_pin,
        )
        if not callable(clock) or (
            verification.execution_authority_pin.role
            is not ScientificBridgeRole.EXECUTION_AUTHORIZER
            or verification.validator_authority_pin.role
            is not ScientificBridgeRole.OBSERVATION_VALIDATOR
        ):
            raise IndependentObservationAdmissionError(
                "independent admission verification roles are not closed"
            )
        self._verification = verification
        self._admission_private_key = admission_private_key
        self._clock = clock

    def issue_admission_decision(
        self,
        *,
        committed_validation: CommittedObservationValidationReceipt,
        issuance_challenge: AdmissionIssuanceChallenge,
    ) -> ObservationAdmissionDecision:
        """Derive and sign exactly one proposal without conferring scientific authority."""

        try:
            receipt = committed_validation.message.receipt.message
            if receipt.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION:
                disposition = ObservationAdmissionDisposition.ADMITTED
                reason_codes: tuple[str, ...] = ()
            else:
                disposition = ObservationAdmissionDisposition.REJECTED
                reason_codes = receipt.blocker_codes
            context = self._verification
            return issue_observation_admission_decision(
                committed_validation_receipt=committed_validation,
                issuance_challenge=issuance_challenge,
                disposition=disposition,
                reason_codes=reason_codes,
                qualification_authority=context.qualification_authority,
                action_authority=context.action_authority,
                qualification_custody=context.qualification_custody,
                raw_run_custody=context.raw_run_custody,
                validation_campaign_custody=context.validation_campaign_custody,
                execution_authority_pin=context.execution_authority_pin,
                validator_authority_pin=context.validator_authority_pin,
                admission_authority_pin=context.admission_authority_pin,
                database_authority_pin=context.database_authority_pin,
                private_key=self._admission_private_key,
                decision_clock=self._clock,
            )
        except IndependentObservationAdmissionError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolated authority boundary fails closed
            raise IndependentObservationAdmissionError(
                "independent observation admission failed closed"
            ) from exc


def _require_admission_private_key(
    *,
    private_key: bytes,
    pin: ScientificBridgeAuthorityPin,
) -> None:
    if pin.role is not ScientificBridgeRole.OBSERVATION_ADMITTER:
        raise IndependentObservationAdmissionError(
            "independent admission signer is not an observation admitter"
        )
    try:
        key = Ed25519PrivateKey.from_private_bytes(private_key)
    except (TypeError, ValueError) as exc:
        raise IndependentObservationAdmissionError(
            "independent admission private key is invalid"
        ) from exc
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if public.hex() != pin.public_key_ed25519_hex:
        raise IndependentObservationAdmissionError(
            "independent admission private key differs from its deployment pin"
        )


__all__ = [
    "IndependentAdmissionVerificationContext",
    "IndependentObservationAdmissionError",
    "IndependentObservationAdmissionService",
]
