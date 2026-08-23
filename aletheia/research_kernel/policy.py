"""Pure, versioned cryptographic authority policy for research-kernel commands.

Command signers never establish their own authority.  A deployment-pinned trust root certifies one
Quest-scoped policy, and that policy delegates four deliberately disjoint roles.  The resulting
policy bytes are content addressed and can be persisted with the event stream for historical
verification; the trust-root decision remains an external deployment authority.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import AwareDatetime, Field, model_validator

from aletheia.research_kernel.schemas import (
    KernelModel,
    canonical_json_bytes,
    canonical_sha256,
)

AUTHORIZATION_POLICY_SCHEMA_VERSION = 1

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIGNATURE_PATTERN = r"^[0-9a-f]{128}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_POLICY_ID_PATTERN = r"^rap_[0-9a-f]{32}$"
_TRUST_ROOT_ID_PATTERN = r"^rat_[0-9a-f]{32}$"
_PRINCIPAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"


class ResearchAuthorizationError(ValueError):
    """Raised when cryptographic or delegated research authority fails closed."""


class ResearchAuthorizationRole(str, Enum):
    COMMISSIONING = "commissioning"
    ORDINARY = "ordinary"
    AMENDMENT = "amendment"
    EMERGENCY = "emergency"


def _public_key_bytes(private_key: bytes) -> bytes:
    if len(private_key) != 32:
        raise ResearchAuthorizationError("Ed25519 private keys must contain exactly 32 raw bytes")
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def ed25519_public_key_hex(private_key: bytes) -> str:
    """Return the raw public key without retaining private-key material."""

    return _public_key_bytes(private_key).hex()


def ed25519_key_id(public_key_ed25519_hex: str) -> str:
    """Derive the immutable key id from a raw Ed25519 public key."""

    try:
        public_key = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("Ed25519 public keys must be hexadecimal") from exc
    if len(public_key) != 32:
        raise ValueError("Ed25519 public keys must contain exactly 32 raw bytes")
    return hashlib.sha256(public_key).hexdigest()


class ResearchAuthorizationTrustKey(KernelModel):
    """One deployment-held key allowed to certify Quest authorization policies."""

    key_id: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _key_is_consistent(self) -> "ResearchAuthorizationTrustKey":
        if self.key_id != ed25519_key_id(self.public_key_ed25519_hex):
            raise ValueError("trust key id does not match its Ed25519 public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("trust key expiry must follow its validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("trust key revocation must fall within its validity window")
        return self

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.expires_at and (
            self.revoked_at is None or timestamp < self.revoked_at
        )


class ResearchAuthorizationTrustRootV1(KernelModel):
    """Frozen deployment trust input; commands and policies cannot choose this value."""

    schema_name: Literal["aletheia.research_authorization_trust_root"] = (
        "aletheia.research_authorization_trust_root"
    )
    schema_version: Literal[1] = AUTHORIZATION_POLICY_SCHEMA_VERSION
    trust_root_id: str = Field(pattern=_TRUST_ROOT_ID_PATTERN)
    frozen_at: AwareDatetime
    commissioning_keys: tuple[ResearchAuthorizationTrustKey, ...] = Field(
        min_length=1, max_length=64
    )

    @model_validator(mode="after")
    def _keys_are_canonical(self) -> "ResearchAuthorizationTrustRootV1":
        expected = tuple(sorted(set(self.commissioning_keys), key=lambda item: item.key_id))
        if self.commissioning_keys != expected:
            raise ValueError("trust-root keys must be unique and canonically ordered")
        if len({item.key_id for item in self.commissioning_keys}) != len(self.commissioning_keys):
            raise ValueError("trust-root key ids must be unique")
        return self

    @property
    def trust_root_sha256(self) -> str:
        return canonical_sha256(self)

    def key(self, key_id: str) -> ResearchAuthorizationTrustKey:
        for key in self.commissioning_keys:
            if key.key_id == key_id:
                return key
        raise ResearchAuthorizationError("policy certificate uses an untrusted root key")


class ResearchAuthorizationKey(KernelModel):
    """One Quest-scoped command key with exactly one non-overlapping role."""

    key_id: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    role: ResearchAuthorizationRole
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _key_is_consistent(self) -> "ResearchAuthorizationKey":
        if self.key_id != ed25519_key_id(self.public_key_ed25519_hex):
            raise ValueError("authorization key id does not match its Ed25519 public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("authorization key expiry must follow its validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("authorization key revocation must fall within its validity window")
        return self

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.expires_at and (
            self.revoked_at is None or timestamp < self.revoked_at
        )


class ResearchAuthorizationPolicyProposalV1(KernelModel):
    """Unsigned policy content that must be certified by the deployment trust root."""

    schema_name: Literal["aletheia.research_authorization_policy_proposal"] = (
        "aletheia.research_authorization_policy_proposal"
    )
    schema_version: Literal[1] = AUTHORIZATION_POLICY_SCHEMA_VERSION
    policy_id: str = Field(pattern=_POLICY_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    keys: tuple[ResearchAuthorizationKey, ...] = Field(min_length=4, max_length=256)

    @model_validator(mode="after")
    def _delegations_are_canonical_and_disjoint(
        self,
    ) -> "ResearchAuthorizationPolicyProposalV1":
        expected = tuple(sorted(set(self.keys), key=lambda item: item.key_id))
        if self.keys != expected:
            raise ValueError("authorization keys must be unique and canonically ordered")
        if len({item.key_id for item in self.keys}) != len(self.keys):
            raise ValueError("authorization key ids must be unique")
        principal_roles: dict[str, set[ResearchAuthorizationRole]] = {}
        for key in self.keys:
            principal_roles.setdefault(key.principal_id, set()).add(key.role)
        if any(len(roles) != 1 for roles in principal_roles.values()):
            raise ValueError("authorization principals cannot span disjoint roles")
        if {item.role for item in self.keys} != set(ResearchAuthorizationRole):
            raise ValueError("authorization policy must delegate every required role")
        return self

    @property
    def proposal_sha256(self) -> str:
        return canonical_sha256(self)


class ResearchAuthorizationPolicyV1(KernelModel):
    """Root-certified, Quest-scoped authorization policy persisted with its stream."""

    schema_name: Literal["aletheia.research_authorization_policy"] = (
        "aletheia.research_authorization_policy"
    )
    schema_version: Literal[1] = AUTHORIZATION_POLICY_SCHEMA_VERSION
    policy_id: str = Field(pattern=_POLICY_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    frozen_at: AwareDatetime
    keys: tuple[ResearchAuthorizationKey, ...] = Field(min_length=4, max_length=256)
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    certified_by_key_id: str = Field(pattern=_SHA256_PATTERN)
    certified_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    certified_at: AwareDatetime
    certification_signature_ed25519_hex: str = Field(pattern=_SIGNATURE_PATTERN)

    @model_validator(mode="after")
    def _policy_matches_its_unsigned_proposal(self) -> "ResearchAuthorizationPolicyV1":
        proposal = self.as_proposal()
        if self.proposal_sha256 != proposal.proposal_sha256:
            raise ValueError("policy proposal digest does not match its certified content")
        if self.certified_at < self.frozen_at:
            raise ValueError("policy cannot be certified before it is frozen")
        return self

    def as_proposal(self) -> ResearchAuthorizationPolicyProposalV1:
        return ResearchAuthorizationPolicyProposalV1(
            policy_id=self.policy_id,
            quest_id=self.quest_id,
            trust_root_sha256=self.trust_root_sha256,
            frozen_at=self.frozen_at,
            keys=self.keys,
        )

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)

    def key(self, key_id: str) -> ResearchAuthorizationKey:
        for key in self.keys:
            if key.key_id == key_id:
                return key
        raise ResearchAuthorizationError("command uses a key outside its frozen policy")

    def principals_for_role(self, role: ResearchAuthorizationRole) -> frozenset[str]:
        return frozenset(item.principal_id for item in self.keys if item.role is role)


class _ResearchAuthorizationPolicyCertificateMessage(KernelModel):
    schema_name: Literal["aletheia.research_authorization_policy_certificate_message"] = (
        "aletheia.research_authorization_policy_certificate_message"
    )
    schema_version: Literal[1] = AUTHORIZATION_POLICY_SCHEMA_VERSION
    algorithm: Literal["ed25519-canonical-json-v1"] = "ed25519-canonical-json-v1"
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN)
    trust_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    certified_by_key_id: str = Field(pattern=_SHA256_PATTERN)
    certified_by_principal_id: str = Field(pattern=_PRINCIPAL_ID_PATTERN)
    certified_at: AwareDatetime


def _policy_certificate_message(
    *,
    proposal: ResearchAuthorizationPolicyProposalV1,
    certified_by_key_id: str,
    certified_by_principal_id: str,
    certified_at: datetime,
) -> bytes:
    return canonical_json_bytes(
        _ResearchAuthorizationPolicyCertificateMessage(
            proposal_sha256=proposal.proposal_sha256,
            trust_root_sha256=proposal.trust_root_sha256,
            certified_by_key_id=certified_by_key_id,
            certified_by_principal_id=certified_by_principal_id,
            certified_at=certified_at,
        )
    )


def certify_research_authorization_policy(
    proposal: ResearchAuthorizationPolicyProposalV1,
    *,
    trust_root: ResearchAuthorizationTrustRootV1,
    root_key_id: str,
    private_key: bytes,
    certified_at: datetime,
) -> ResearchAuthorizationPolicyV1:
    """Certify a policy with a key from an independently supplied deployment trust root."""

    proposal = ResearchAuthorizationPolicyProposalV1.model_validate(
        proposal.model_dump(mode="python")
    )
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(
        trust_root.model_dump(mode="python")
    )
    if proposal.trust_root_sha256 != trust_root.trust_root_sha256:
        raise ResearchAuthorizationError("policy proposal is bound to another trust root")
    if not trust_root.frozen_at <= proposal.frozen_at <= certified_at:
        raise ResearchAuthorizationError("policy certification has an invalid time lineage")
    root_key = trust_root.key(root_key_id)
    if not root_key.active_at(certified_at):
        raise ResearchAuthorizationError("policy certification key is inactive")
    if _public_key_bytes(private_key).hex() != root_key.public_key_ed25519_hex:
        raise ResearchAuthorizationError("policy certification private key does not match")
    message = _policy_certificate_message(
        proposal=proposal,
        certified_by_key_id=root_key.key_id,
        certified_by_principal_id=root_key.principal_id,
        certified_at=certified_at,
    )
    policy = ResearchAuthorizationPolicyV1(
        policy_id=proposal.policy_id,
        quest_id=proposal.quest_id,
        trust_root_sha256=proposal.trust_root_sha256,
        frozen_at=proposal.frozen_at,
        keys=proposal.keys,
        proposal_sha256=proposal.proposal_sha256,
        certified_by_key_id=root_key.key_id,
        certified_by_principal_id=root_key.principal_id,
        certified_at=certified_at,
        certification_signature_ed25519_hex=(
            Ed25519PrivateKey.from_private_bytes(private_key).sign(message).hex()
        ),
    )
    verify_research_authorization_policy(policy=policy, trust_root=trust_root)
    return policy


def verify_research_authorization_policy(
    *,
    policy: ResearchAuthorizationPolicyV1,
    trust_root: ResearchAuthorizationTrustRootV1,
) -> None:
    """Verify a frozen policy against the deployment-pinned commissioning trust root."""

    policy = ResearchAuthorizationPolicyV1.model_validate(policy.model_dump(mode="python"))
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(
        trust_root.model_dump(mode="python")
    )
    if policy.trust_root_sha256 != trust_root.trust_root_sha256:
        raise ResearchAuthorizationError("authorization policy uses an untrusted root")
    if not trust_root.frozen_at <= policy.frozen_at <= policy.certified_at:
        raise ResearchAuthorizationError("authorization policy has an invalid time lineage")
    root_key = trust_root.key(policy.certified_by_key_id)
    if root_key.principal_id != policy.certified_by_principal_id:
        raise ResearchAuthorizationError("policy certificate principal does not match its key")
    if not root_key.active_at(policy.certified_at):
        raise ResearchAuthorizationError("policy certificate key is inactive")
    message = _policy_certificate_message(
        proposal=policy.as_proposal(),
        certified_by_key_id=policy.certified_by_key_id,
        certified_by_principal_id=policy.certified_by_principal_id,
        certified_at=policy.certified_at,
    )
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(root_key.public_key_ed25519_hex)).verify(
            bytes.fromhex(policy.certification_signature_ed25519_hex), message
        )
    except (InvalidSignature, ValueError) as exc:
        raise ResearchAuthorizationError("authorization policy certificate is invalid") from exc


def sign_authorization_message(
    *,
    policy: ResearchAuthorizationPolicyV1,
    key_id: str,
    private_key: bytes,
    principal_id: str,
    required_role: ResearchAuthorizationRole,
    authorized_at: datetime,
    message: bytes,
) -> str:
    """Sign one canonical command message after local key/role/time checks."""

    key = policy.key(key_id)
    if key.principal_id != principal_id or key.role is not required_role:
        raise ResearchAuthorizationError("command signer lacks the exact required role")
    if not key.active_at(authorized_at):
        raise ResearchAuthorizationError("command authorization key is inactive")
    if _public_key_bytes(private_key).hex() != key.public_key_ed25519_hex:
        raise ResearchAuthorizationError("command private key does not match its policy key")
    return Ed25519PrivateKey.from_private_bytes(private_key).sign(message).hex()


def verify_authorization_message(
    *,
    policy: ResearchAuthorizationPolicyV1,
    key_id: str,
    principal_id: str,
    required_role: ResearchAuthorizationRole,
    authorized_at: datetime,
    committed_at: datetime,
    message: bytes,
    signature_ed25519_hex: str,
) -> None:
    """Verify exact role, historical activity at authorization and commit, and signature bytes."""

    if authorized_at > committed_at:
        raise ResearchAuthorizationError("command cannot be authorized after it is committed")
    key = policy.key(key_id)
    if key.principal_id != principal_id or key.role is not required_role:
        raise ResearchAuthorizationError("command signer lacks the exact required role")
    if not key.active_at(authorized_at) or not key.active_at(committed_at):
        raise ResearchAuthorizationError("command authorization key is inactive")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_ed25519_hex)).verify(
            bytes.fromhex(signature_ed25519_hex), message
        )
    except (InvalidSignature, ValueError) as exc:
        raise ResearchAuthorizationError("research command signature is invalid") from exc


__all__ = [
    "AUTHORIZATION_POLICY_SCHEMA_VERSION",
    "ResearchAuthorizationError",
    "ResearchAuthorizationKey",
    "ResearchAuthorizationPolicyProposalV1",
    "ResearchAuthorizationPolicyV1",
    "ResearchAuthorizationRole",
    "ResearchAuthorizationTrustKey",
    "ResearchAuthorizationTrustRootV1",
    "certify_research_authorization_policy",
    "ed25519_key_id",
    "ed25519_public_key_hex",
    "sign_authorization_message",
    "verify_authorization_message",
    "verify_research_authorization_policy",
]
