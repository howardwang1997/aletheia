"""Pure contracts for encrypted, pull-based qualification assignments.

The allocator must never persist a raw lease token.  It seals the exact token and immutable
assignment scope to a deployment-pinned X25519 node transport key.  The node may replay the same
stored envelope after a delivery crash, but a different node, key, attempt, or authority window
cannot decrypt or reinterpret it.

These contracts do not authorize scientific execution.  They only transport an already admitted
``qualification_only`` attempt to its exact enrolled node.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ATTEMPT_ID_PATTERN = r"^iat_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_X25519_HEX_PATTERN = r"^[0-9a-f]{64}$"
_NONCE_HEX_PATTERN = r"^[0-9a-f]{24}$"
_BASE64_PATTERN = r"^[A-Za-z0-9+/]+={0,2}$"
_TRANSPORT_KEY_DOMAIN = b"ALETHEIA_NODE_ASSIGNMENT_X25519_KEY_V1\x00"
_AEAD_INFO = b"ALETHEIA_QUALIFICATION_ASSIGNMENT_AEAD_V1"


class AssignmentTransportError(ValueError):
    """An assignment envelope or pinned node transport key failed closed."""


def node_transport_key_id(public_key_x25519_hex: str) -> str:
    """Derive a domain-separated immutable identity for one raw X25519 public key."""

    try:
        public_key = bytes.fromhex(public_key_x25519_hex)
    except ValueError as exc:
        raise ValueError("X25519 public keys must be hexadecimal") from exc
    if len(public_key) != 32:
        raise ValueError("X25519 public keys must contain exactly 32 raw bytes")
    return hashlib.sha256(_TRANSPORT_KEY_DOMAIN + public_key).hexdigest()


def x25519_public_key_hex(private_key: bytes) -> str:
    """Derive the raw public key used by a node assignment transport pin."""

    if len(private_key) != 32:
        raise ValueError("X25519 private keys must contain exactly 32 raw bytes")
    return (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


class NodeAssignmentTransportPin(ExecutionModel):
    """Deployment-owned encryption pin for exactly one enrolled node manifest."""

    schema_name: Literal["aletheia.node_assignment_transport_pin"] = (
        "aletheia.node_assignment_transport_pin"
    )
    schema_version: Literal[1] = 1
    transport_domain: Literal["ALETHEIA_QUALIFICATION_ASSIGNMENT_AEAD_V1"] = (
        "ALETHEIA_QUALIFICATION_ASSIGNMENT_AEAD_V1"
    )
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    transport_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    transport_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    transport_key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_x25519_hex: str = Field(pattern=_X25519_HEX_PATTERN)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _pin_is_exact_and_finite(self) -> "NodeAssignmentTransportPin":
        if self.transport_key_id != node_transport_key_id(self.public_key_x25519_hex):
            raise ValueError("node transport key id does not match its X25519 public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("node transport key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("node transport key revocation is outside its validity")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until

    @property
    def pin_sha256(self) -> str:
        return canonical_sha256(self)


class QualificationAssignmentSecret(ExecutionModel):
    """Plaintext that exists only before sealing or after node-local decryption."""

    schema_name: Literal["aletheia.qualification_assignment_secret"] = (
        "aletheia.qualification_assignment_secret"
    )
    schema_version: Literal[1] = 1
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token: str = Field(min_length=43, max_length=1024, repr=False)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _secret_is_exact_and_finite(self) -> "QualificationAssignmentSecret":
        if self.expires_at <= self.issued_at:
            raise ValueError("assignment expiry must follow issuance")
        if hashlib.sha256(self.lease_token.encode("utf-8")).hexdigest() != (
            self.lease_token_sha256
        ):
            raise ValueError("assignment raw lease token differs from its frozen hash")
        return self

    @property
    def secret_sha256(self) -> str:
        return canonical_sha256(self)


class SealedQualificationAssignment(ExecutionModel):
    """Replayable ciphertext stored by the allocator; contains no plaintext credential."""

    schema_name: Literal["aletheia.sealed_qualification_assignment"] = (
        "aletheia.sealed_qualification_assignment"
    )
    schema_version: Literal[1] = 1
    algorithm: Literal["x25519-hkdf-sha256-chacha20poly1305-v1"] = (
        "x25519-hkdf-sha256-chacha20poly1305-v1"
    )
    infrastructure_attempt_id: str = Field(pattern=_ATTEMPT_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SHA256_PATTERN)
    grant_sha256: str = Field(pattern=_SHA256_PATTERN)
    bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    node_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    node_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    resource_lease_sha256: str = Field(pattern=_SHA256_PATTERN)
    fencing_epoch: int = Field(ge=1)
    lease_token_sha256: str = Field(pattern=_SHA256_PATTERN)
    assignment_secret_sha256: str = Field(pattern=_SHA256_PATTERN)
    transport_pin_sha256: str = Field(pattern=_SHA256_PATTERN)
    transport_key_id: str = Field(pattern=_SHA256_PATTERN)
    ephemeral_public_key_x25519_hex: str = Field(pattern=_X25519_HEX_PATTERN)
    nonce_hex: str = Field(pattern=_NONCE_HEX_PATTERN)
    aad_sha256: str = Field(pattern=_SHA256_PATTERN)
    ciphertext_base64: str = Field(pattern=_BASE64_PATTERN, min_length=24)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _envelope_encoding_is_canonical(self) -> "SealedQualificationAssignment":
        if self.expires_at <= self.issued_at:
            raise ValueError("sealed assignment expiry must follow issuance")
        try:
            ciphertext = base64.b64decode(self.ciphertext_base64, validate=True)
        except ValueError as exc:
            raise ValueError("assignment ciphertext is not valid base64") from exc
        if len(ciphertext) <= 16 or base64.b64encode(ciphertext).decode("ascii") != (
            self.ciphertext_base64
        ):
            raise ValueError("assignment ciphertext encoding is empty or noncanonical")
        if self.aad_sha256 != canonical_sha256(_assignment_aad(self)):
            raise ValueError("assignment AAD hash differs from its immutable envelope scope")
        return self

    @property
    def envelope_sha256(self) -> str:
        return canonical_sha256(self)


def _assignment_aad(value: SealedQualificationAssignment | dict[str, object]) -> dict[str, object]:
    if isinstance(value, SealedQualificationAssignment):

        def get(name: str) -> object:
            return getattr(value, name)
    else:
        get = value.__getitem__

    def json_value(name: str) -> object:
        item = get(name)
        return item.isoformat() if isinstance(item, datetime) else item

    return {
        "schema": "aletheia.qualification_assignment_aad.v1",
        "infrastructure_attempt_id": json_value("infrastructure_attempt_id"),
        "admission_sha256": json_value("admission_sha256"),
        "grant_sha256": json_value("grant_sha256"),
        "bundle_sha256": json_value("bundle_sha256"),
        "node_id": json_value("node_id"),
        "node_manifest_sha256": json_value("node_manifest_sha256"),
        "resource_lease_sha256": json_value("resource_lease_sha256"),
        "fencing_epoch": json_value("fencing_epoch"),
        "lease_token_sha256": json_value("lease_token_sha256"),
        "assignment_secret_sha256": json_value("assignment_secret_sha256"),
        "transport_pin_sha256": json_value("transport_pin_sha256"),
        "transport_key_id": json_value("transport_key_id"),
        "ephemeral_public_key_x25519_hex": json_value("ephemeral_public_key_x25519_hex"),
        "nonce_hex": json_value("nonce_hex"),
        "issued_at": json_value("issued_at"),
        "expires_at": json_value("expires_at"),
        "qualification_only": True,
        "scientific_admission_allowed": False,
    }


def _derive_aead_key(*, shared_secret: bytes, aad_sha256: str) -> bytes:
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=bytes.fromhex(aad_sha256),
        info=_AEAD_INFO,
    ).derive(shared_secret)


def seal_qualification_assignment(
    *,
    secret: QualificationAssignmentSecret,
    transport_pin: NodeAssignmentTransportPin,
) -> SealedQualificationAssignment:
    """Encrypt one exact assignment for its deployment-pinned node transport key."""

    secret = QualificationAssignmentSecret.model_validate(secret.model_dump(mode="python"))
    transport_pin = NodeAssignmentTransportPin.model_validate(
        transport_pin.model_dump(mode="python")
    )
    if (
        secret.node_id != transport_pin.node_id
        or secret.node_manifest_sha256 != transport_pin.node_manifest_sha256
    ):
        raise AssignmentTransportError("assignment differs from its exact node transport pin")
    if not transport_pin.active_at(secret.issued_at) or (
        secret.expires_at > transport_pin.active_until
    ):
        raise AssignmentTransportError("node transport pin does not cover the assignment window")

    ephemeral_private = X25519PrivateKey.generate()
    ephemeral_public_hex = (
        ephemeral_private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    nonce = os.urandom(12)
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
        "transport_pin_sha256": transport_pin.pin_sha256,
        "transport_key_id": transport_pin.transport_key_id,
        "ephemeral_public_key_x25519_hex": ephemeral_public_hex,
        "nonce_hex": nonce.hex(),
        "issued_at": secret.issued_at,
        "expires_at": secret.expires_at,
    }
    aad = canonical_json_bytes(_assignment_aad(metadata))
    aad_sha256 = hashlib.sha256(aad).hexdigest()
    recipient = X25519PublicKey.from_public_bytes(
        bytes.fromhex(transport_pin.public_key_x25519_hex)
    )
    key = _derive_aead_key(
        shared_secret=ephemeral_private.exchange(recipient),
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


def open_qualification_assignment(
    *,
    envelope: SealedQualificationAssignment,
    transport_pin: NodeAssignmentTransportPin,
    node_transport_private_key: bytes,
    observed_at: datetime,
) -> QualificationAssignmentSecret:
    """Decrypt and revalidate an exact assignment at the node authority boundary."""

    envelope = SealedQualificationAssignment.model_validate(envelope.model_dump(mode="python"))
    transport_pin = NodeAssignmentTransportPin.model_validate(
        transport_pin.model_dump(mode="python")
    )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise AssignmentTransportError("assignment observation time must be timezone-aware UTC")
    if observed_at.utcoffset().total_seconds() != 0:
        raise AssignmentTransportError("assignment observation time must be UTC")
    if (
        envelope.transport_pin_sha256 != transport_pin.pin_sha256
        or envelope.transport_key_id != transport_pin.transport_key_id
        or envelope.node_id != transport_pin.node_id
        or envelope.node_manifest_sha256 != transport_pin.node_manifest_sha256
    ):
        raise AssignmentTransportError("sealed assignment differs from the pinned recipient")
    if (
        not transport_pin.active_at(observed_at)
        or not transport_pin.active_at(envelope.issued_at)
        or envelope.expires_at > transport_pin.active_until
        or not envelope.issued_at <= observed_at < envelope.expires_at
    ):
        raise AssignmentTransportError("sealed assignment or node transport pin is inactive")
    try:
        private = X25519PrivateKey.from_private_bytes(node_transport_private_key)
    except ValueError as exc:
        raise AssignmentTransportError("node X25519 private key must contain 32 raw bytes") from exc
    public_hex = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    if public_hex != transport_pin.public_key_x25519_hex:
        raise AssignmentTransportError("node transport private key differs from the deployment pin")
    aad = canonical_json_bytes(_assignment_aad(envelope))
    key = _derive_aead_key(
        shared_secret=private.exchange(
            X25519PublicKey.from_public_bytes(
                bytes.fromhex(envelope.ephemeral_public_key_x25519_hex)
            )
        ),
        aad_sha256=envelope.aad_sha256,
    )
    try:
        plaintext = ChaCha20Poly1305(key).decrypt(
            bytes.fromhex(envelope.nonce_hex),
            base64.b64decode(envelope.ciphertext_base64, validate=True),
            aad,
        )
        secret = QualificationAssignmentSecret.model_validate_json(plaintext)
    except (InvalidTag, ValueError) as exc:
        raise AssignmentTransportError("sealed assignment authentication failed") from exc
    if (
        secret.secret_sha256 != envelope.assignment_secret_sha256
        or secret.infrastructure_attempt_id != envelope.infrastructure_attempt_id
        or secret.admission_sha256 != envelope.admission_sha256
        or secret.grant_sha256 != envelope.grant_sha256
        or secret.bundle_sha256 != envelope.bundle_sha256
        or secret.node_id != envelope.node_id
        or secret.node_manifest_sha256 != envelope.node_manifest_sha256
        or secret.resource_lease_sha256 != envelope.resource_lease_sha256
        or secret.fencing_epoch != envelope.fencing_epoch
        or secret.lease_token_sha256 != envelope.lease_token_sha256
        or secret.issued_at != envelope.issued_at
        or secret.expires_at != envelope.expires_at
    ):
        raise AssignmentTransportError("decrypted assignment differs from its sealed scope")
    return secret


__all__ = [
    "AssignmentTransportError",
    "NodeAssignmentTransportPin",
    "QualificationAssignmentSecret",
    "SealedQualificationAssignment",
    "node_transport_key_id",
    "open_qualification_assignment",
    "seal_qualification_assignment",
    "x25519_public_key_hex",
]
