from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pydantic import ValidationError

import aletheia.execution.assignment_contracts as assignment_module
from aletheia.execution.assignment_contracts import (
    AssignmentTransportError,
    NodeAssignmentTransportPin,
    QualificationAssignmentSecret,
    SealedQualificationAssignment,
    node_transport_key_id,
    open_qualification_assignment,
    seal_qualification_assignment,
    x25519_public_key_hex,
)
from aletheia.execution.schemas import canonical_json_bytes

NOW = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
PRIVATE_KEY = bytes.fromhex("11" * 32)
OTHER_PRIVATE_KEY = bytes.fromhex("22" * 32)


def _pin(
    *,
    private_key: bytes = PRIVATE_KEY,
    revoked_at: datetime | None = None,
) -> NodeAssignmentTransportPin:
    public_key = x25519_public_key_hex(private_key)
    return NodeAssignmentTransportPin(
        node_id="node.local.01",
        node_manifest_sha256="a" * 64,
        transport_policy_sha256="b" * 64,
        transport_principal_id="principal:node_transport",
        transport_key_id=node_transport_key_id(public_key),
        public_key_x25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        revoked_at=revoked_at,
    )


def _secret(**updates: object) -> QualificationAssignmentSecret:
    token = "lease-token-" + "x" * 48
    values: dict[str, object] = {
        "infrastructure_attempt_id": "iat_" + "1" * 32,
        "admission_sha256": "c" * 64,
        "grant_sha256": "d" * 64,
        "bundle_sha256": "e" * 64,
        "node_id": "node.local.01",
        "node_manifest_sha256": "a" * 64,
        "resource_lease_sha256": "f" * 64,
        "fencing_epoch": 7,
        "lease_token": token,
        "lease_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "issued_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(updates)
    return QualificationAssignmentSecret(**values)


def _sealed() -> tuple[
    NodeAssignmentTransportPin,
    QualificationAssignmentSecret,
    SealedQualificationAssignment,
]:
    pin = _pin()
    secret = _secret()
    return pin, secret, seal_qualification_assignment(secret=secret, transport_pin=pin)


def _forge_authenticated_envelope(
    *, secret: QualificationAssignmentSecret, pin: NodeAssignmentTransportPin
) -> SealedQualificationAssignment:
    """Construct valid recipient-authenticated bytes without using the guarded seal helper."""

    ephemeral = X25519PrivateKey.from_private_bytes(bytes.fromhex("33" * 32))
    ephemeral_public_hex = (
        ephemeral.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    nonce = bytes.fromhex("44" * 12)
    metadata: dict[str, object] = {
        "infrastructure_attempt_id": secret.infrastructure_attempt_id,
        "admission_sha256": secret.admission_sha256,
        "grant_sha256": secret.grant_sha256,
        "bundle_sha256": secret.bundle_sha256,
        "node_id": secret.node_id,
        "node_manifest_sha256": secret.node_manifest_sha256,
        "resource_lease_sha256": secret.resource_lease_sha256,
        "fencing_epoch": secret.fencing_epoch,
        "lease_token_sha256": secret.lease_token_sha256,
        "assignment_secret_sha256": secret.secret_sha256,
        "transport_pin_sha256": pin.pin_sha256,
        "transport_key_id": pin.transport_key_id,
        "ephemeral_public_key_x25519_hex": ephemeral_public_hex,
        "nonce_hex": nonce.hex(),
        "issued_at": secret.issued_at,
        "expires_at": secret.expires_at,
    }
    aad = canonical_json_bytes(assignment_module._assignment_aad(metadata))
    aad_sha256 = hashlib.sha256(aad).hexdigest()
    recipient = assignment_module.X25519PublicKey.from_public_bytes(
        bytes.fromhex(pin.public_key_x25519_hex)
    )
    key = assignment_module._derive_aead_key(
        shared_secret=ephemeral.exchange(recipient),
        aad_sha256=aad_sha256,
    )
    ciphertext = ChaCha20Poly1305(key).encrypt(
        nonce,
        canonical_json_bytes(secret),
        aad,
    )
    return SealedQualificationAssignment(
        **metadata,
        aad_sha256=aad_sha256,
        ciphertext_base64=base64.b64encode(ciphertext).decode("ascii"),
    )


def test_assignment_round_trip_binds_exact_secret_and_recipient() -> None:
    pin, secret, envelope = _sealed()

    opened = open_qualification_assignment(
        envelope=envelope,
        transport_pin=pin,
        node_transport_private_key=PRIVATE_KEY,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert opened == secret
    assert PRIVATE_KEY.hex() not in envelope.model_dump_json()
    assert secret.lease_token not in envelope.model_dump_json()
    assert envelope.envelope_sha256 == envelope.envelope_sha256


def test_assignment_wrong_private_key_is_rejected() -> None:
    pin, _secret_value, envelope = _sealed()

    with pytest.raises(AssignmentTransportError, match="differs from the deployment pin"):
        open_qualification_assignment(
            envelope=envelope,
            transport_pin=pin,
            node_transport_private_key=OTHER_PRIVATE_KEY,
            observed_at=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fencing_epoch", 8),
        ("resource_lease_sha256", "0" * 64),
        ("lease_token_sha256", "1" * 64),
        ("node_manifest_sha256", "2" * 64),
    ],
)
def test_assignment_scope_tamper_is_rejected(field: str, value: object) -> None:
    pin, _secret_value, envelope = _sealed()
    payload = envelope.model_dump(mode="python")
    payload[field] = value
    payload["aad_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="AAD hash"):
        SealedQualificationAssignment(**payload)

    tampered = envelope.model_copy(update={field: value})
    with pytest.raises((AssignmentTransportError, ValidationError)):
        open_qualification_assignment(
            envelope=tampered,
            transport_pin=pin,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=NOW + timedelta(minutes=1),
        )


def test_assignment_ciphertext_tamper_is_rejected() -> None:
    pin, _secret_value, envelope = _sealed()
    ciphertext = bytearray(base64.b64decode(envelope.ciphertext_base64))
    ciphertext[-1] ^= 1
    tampered = envelope.model_copy(
        update={"ciphertext_base64": base64.b64encode(ciphertext).decode()}
    )

    with pytest.raises(AssignmentTransportError, match="authentication failed"):
        open_qualification_assignment(
            envelope=tampered,
            transport_pin=pin,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=NOW + timedelta(minutes=1),
        )


def test_assignment_inactive_or_revoked_transport_pin_is_rejected() -> None:
    secret = _secret()
    pin = _pin(revoked_at=NOW + timedelta(minutes=30))

    with pytest.raises(AssignmentTransportError, match="does not cover"):
        seal_qualification_assignment(secret=secret, transport_pin=pin)

    good_pin, _secret_value, envelope = _sealed()
    revoked = good_pin.model_copy(update={"revoked_at": NOW + timedelta(seconds=30)})
    with pytest.raises(AssignmentTransportError, match="pinned recipient|inactive"):
        open_qualification_assignment(
            envelope=envelope,
            transport_pin=revoked,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("pin_updates", "secret_updates"),
    [
        (
            {
                "valid_from": NOW + timedelta(minutes=10),
                "expires_at": NOW + timedelta(hours=2),
            },
            {"issued_at": NOW, "expires_at": NOW + timedelta(hours=1)},
        ),
        (
            {
                "valid_from": NOW - timedelta(hours=1),
                "expires_at": NOW + timedelta(minutes=30),
            },
            {"issued_at": NOW, "expires_at": NOW + timedelta(hours=1)},
        ),
    ],
)
def test_assignment_open_rechecks_the_complete_transport_pin_window(
    pin_updates: dict[str, object], secret_updates: dict[str, object]
) -> None:
    pin = _pin().model_copy(update=pin_updates)
    pin = NodeAssignmentTransportPin.model_validate(pin.model_dump(mode="python"))
    secret = _secret(**secret_updates)
    envelope = _forge_authenticated_envelope(secret=secret, pin=pin)

    with pytest.raises(AssignmentTransportError, match="inactive"):
        open_qualification_assignment(
            envelope=envelope,
            transport_pin=pin,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=NOW + timedelta(minutes=15),
        )


def test_assignment_expiry_and_non_utc_observation_are_rejected() -> None:
    pin, _secret_value, envelope = _sealed()

    with pytest.raises(AssignmentTransportError, match="inactive"):
        open_qualification_assignment(
            envelope=envelope,
            transport_pin=pin,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=envelope.expires_at,
        )
    with pytest.raises(AssignmentTransportError, match="timezone-aware UTC"):
        open_qualification_assignment(
            envelope=envelope,
            transport_pin=pin,
            node_transport_private_key=PRIVATE_KEY,
            observed_at=NOW.replace(tzinfo=None),
        )


def test_assignment_token_hash_and_node_scope_are_closed() -> None:
    with pytest.raises(ValidationError, match="raw lease token"):
        _secret(lease_token_sha256="0" * 64)

    with pytest.raises(AssignmentTransportError, match="exact node"):
        seal_qualification_assignment(
            secret=_secret(node_id="node.other"),
            transport_pin=_pin(),
        )


def test_x25519_key_id_is_domain_bound_and_exact() -> None:
    public_key = (
        X25519PrivateKey.from_private_bytes(PRIVATE_KEY)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    assert public_key == x25519_public_key_hex(PRIVATE_KEY)
    assert (
        node_transport_key_id(public_key) != hashlib.sha256(bytes.fromhex(public_key)).hexdigest()
    )
    with pytest.raises(ValueError, match="does not match"):
        _pin().model_copy(update={"transport_key_id": "0" * 64}).model_validate(
            _pin().model_copy(update={"transport_key_id": "0" * 64}).model_dump(mode="python")
        )
