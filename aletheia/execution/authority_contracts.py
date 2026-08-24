"""Pure contracts for deployment-pinned, read-only execution authority registries.

The contracts deliberately contain no registration or signing helpers.  Deployment tooling may
prepare canonical JSON and detached Ed25519 signatures, while runtime code can only verify and
resolve already-published content-addressed authority bytes.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from aletheia.execution.runtime_contracts import BudgetAuthorization, ExecutionCostQuote
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes, canonical_sha256

AUTHORITY_REGISTRY_SCHEMA_VERSION = 1

PRICING_RATE_CARD_SIGNATURE_DOMAIN = "ALETHEIA_EXECUTION_RATE_CARD_V1"
PRICING_QUOTE_SIGNATURE_DOMAIN = "ALETHEIA_EXECUTION_COST_QUOTE_V1"
SOURCE_BUDGET_SIGNATURE_DOMAIN = "ALETHEIA_SOURCE_BUDGET_AUTHORIZATION_V1"
SOURCE_BUDGET_PROJECTION_SIGNATURE_DOMAIN = "ALETHEIA_SOURCE_BUDGET_PROJECTION_V1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUEST_ID_PATTERN = r"^qst_[0-9a-f]{32}$"
_SYMBOLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$"
_SIGNATURE_DOMAIN_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def authority_key_id(public_key_ed25519_hex: str) -> str:
    """Return the content identity of one raw Ed25519 public key."""

    try:
        public_key = bytes.fromhex(public_key_ed25519_hex)
    except ValueError as exc:
        raise ValueError("Ed25519 public keys must be lowercase hexadecimal") from exc
    if public_key.hex() != public_key_ed25519_hex or len(public_key) != 32:
        raise ValueError("Ed25519 public keys must contain 32 lowercase-hex raw bytes")
    # Parsing rejects malformed encodings even if the byte length is correct.
    Ed25519PublicKey.from_public_bytes(public_key).public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(public_key).hexdigest()


def detached_signature_message(*, signature_domain: str, canonical_payload: bytes) -> bytes:
    """Domain-separate exact canonical bytes for a detached Ed25519 signature."""

    if _SIGNATURE_DOMAIN_PATTERN.fullmatch(signature_domain) is None:
        raise ValueError("authority signature domain is not canonical")
    if not isinstance(canonical_payload, bytes):
        raise TypeError("detached signature payload must be exact bytes")
    return signature_domain.encode("ascii") + b"\x00" + canonical_payload


class AuthorityRegistryFilesystemPin(ExecutionModel):
    """Deployment-pinned POSIX custody identity for an immutable registry tree."""

    schema_name: Literal["aletheia.authority_registry_filesystem_pin"] = (
        "aletheia.authority_registry_filesystem_pin"
    )
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    registry_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    owner_uid: int = Field(ge=0)
    device_id: int = Field(ge=0)
    directory_mode: int = Field(ge=0, le=0o7777)
    file_mode: int = Field(ge=0, le=0o7777)
    maximum_document_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def _custody_is_read_only(self) -> "AuthorityRegistryFilesystemPin":
        if self.directory_mode & 0o222 or self.file_mode & 0o222:
            raise ValueError("authority registry modes must not contain write permission bits")
        if self.directory_mode & 0o7000 or self.file_mode & 0o7000:
            raise ValueError("authority registry modes must not contain special permission bits")
        if not self.directory_mode & 0o111:
            raise ValueError("authority registry directory mode must be searchable")
        if not self.file_mode & 0o444 or self.file_mode & 0o111:
            raise ValueError("authority registry files must be readable and non-executable")
        return self


class _Ed25519AuthorityPin(ExecutionModel):
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    key_id: str = Field(pattern=_SHA256_PATTERN)
    public_key_ed25519_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _authority_key_is_pinned(self) -> "_Ed25519AuthorityPin":
        if self.key_id != authority_key_id(self.public_key_ed25519_hex):
            raise ValueError("authority key id does not match its Ed25519 public key")
        if self.expires_at <= self.valid_from:
            raise ValueError("authority key expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("authority key revocation is outside its validity window")
        return self

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class PricingAuthorityPin(_Ed25519AuthorityPin):
    """Deployment-owned key and policy for rate cards and exact cost quotes."""

    schema_name: Literal["aletheia.pricing_authority_pin"] = "aletheia.pricing_authority_pin"
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    rate_card_signature_domain: Literal["ALETHEIA_EXECUTION_RATE_CARD_V1"] = (
        PRICING_RATE_CARD_SIGNATURE_DOMAIN
    )
    quote_signature_domain: Literal["ALETHEIA_EXECUTION_COST_QUOTE_V1"] = (
        PRICING_QUOTE_SIGNATURE_DOMAIN
    )


class SourceBudgetAuthorityPin(_Ed25519AuthorityPin):
    """Deployment-owned key and policy for source budgets and their exact projections."""

    schema_name: Literal["aletheia.source_budget_authority_pin"] = (
        "aletheia.source_budget_authority_pin"
    )
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    source_signature_domain: Literal["ALETHEIA_SOURCE_BUDGET_AUTHORIZATION_V1"] = (
        SOURCE_BUDGET_SIGNATURE_DOMAIN
    )
    projection_signature_domain: Literal["ALETHEIA_SOURCE_BUDGET_PROJECTION_V1"] = (
        SOURCE_BUDGET_PROJECTION_SIGNATURE_DOMAIN
    )


class ExecutionRateCardLine(ExecutionModel):
    """One exact pricing line selected by a quote's canonical resource-class envelope."""

    accepted_resource_class_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    fixed_charge_microunits: int = Field(ge=0)
    charge_per_second_microunits: int = Field(ge=0)
    maximum_lease_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _line_is_canonical(self) -> "ExecutionRateCardLine":
        values = self.accepted_resource_class_ids
        if any(
            re.fullmatch(_SYMBOLIC_ID_PATTERN, item) is None for item in values
        ) or values != tuple(sorted(set(values))):
            raise ValueError("rate-card resource classes must be canonical and unique")
        return self

    @property
    def selection_key(self) -> tuple[tuple[str, ...], str]:
        return self.accepted_resource_class_ids, self.currency_code


class ExecutionRateCard(ExecutionModel):
    """Content-addressed pricing policy signed by the deployment-pinned pricing key."""

    schema_name: Literal["aletheia.execution_rate_card"] = "aletheia.execution_rate_card"
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    pricing_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    issued_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    pricing_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    lines: tuple[ExecutionRateCardLine, ...] = Field(min_length=1, max_length=1024)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _rate_card_is_canonical(self) -> "ExecutionRateCard":
        if self.expires_at <= self.valid_from:
            raise ValueError("rate-card expiry must follow validity start")
        if self.revoked_at is not None and not (
            self.valid_from <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("rate-card revocation is outside its validity window")
        keys = tuple(line.selection_key for line in self.lines)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("rate-card lines must be unique and canonically ordered")
        return self

    @property
    def rate_card_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp < self.active_until


class SourceBudgetAuthorization(ExecutionModel):
    """Canonical source bytes that precede protocol/resource-budget materialization.

    This source intentionally has no protocol, work-order, resource-budget, or self-hash field.
    Its canonical byte hash can therefore be embedded in ``ResourceBudgetContract`` without a
    source-budget/resource-budget hash cycle.  Those later scope identities live only in the
    separately signed :class:`SourceBudgetProjection`.
    """

    schema_name: Literal["aletheia.source_budget_authorization"] = (
        "aletheia.source_budget_authorization"
    )
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    source_budget_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    quest_id: str = Field(pattern=_QUEST_ID_PATTERN)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    maximum_cost_microunits: int = Field(ge=0)
    deadline: AwareDatetime
    authorized_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    source_authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _source_window_is_valid(self) -> "SourceBudgetAuthorization":
        if not self.authorized_at < self.expires_at <= self.deadline:
            raise ValueError("source budget must expire inside its deadline")
        if self.revoked_at is not None and not (
            self.authorized_at <= self.revoked_at <= self.expires_at
        ):
            raise ValueError("source-budget revocation is outside its validity window")
        return self

    @property
    def source_budget_authorization_sha256(self) -> str:
        return canonical_sha256(self)

    @property
    def active_until(self) -> datetime:
        return min(self.expires_at, self.revoked_at or self.expires_at)

    def active_at(self, timestamp: datetime) -> bool:
        return self.authorized_at <= timestamp < self.active_until


class SourceBudgetProjection(ExecutionModel):
    """A signed one-to-one projection of existing source bytes into execution scope."""

    schema_name: Literal["aletheia.source_budget_projection"] = "aletheia.source_budget_projection"
    schema_version: Literal[1] = AUTHORITY_REGISTRY_SCHEMA_VERSION
    source_budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_authorization_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    budget_authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    budget_authorization: BudgetAuthorization
    projected_by_principal_id: str = Field(pattern=_SYMBOLIC_ID_PATTERN)
    source_authority_key_id: str = Field(pattern=_SHA256_PATTERN)
    projected_at: AwareDatetime
    qualification_only: Literal[True] = True
    scientific_admission_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _projection_is_hash_bound(self) -> "SourceBudgetProjection":
        authorization = self.budget_authorization
        if (
            authorization.source_budget_authorization_sha256
            != self.source_budget_authorization_sha256
            or authorization.authorization_sha256 != self.budget_authorization_sha256
            or self.projected_at < authorization.authorized_at
            or self.projected_at >= authorization.expires_at
        ):
            raise ValueError("budget projection is not exactly bound to its authorization")
        return self

    @property
    def projection_sha256(self) -> str:
        return canonical_sha256(self)


def rate_card_signature_message(rate_card: ExecutionRateCard) -> bytes:
    """Return the exact detached-signature message for a canonical rate card."""

    validated = ExecutionRateCard.model_validate(rate_card.model_dump(mode="python"))
    return detached_signature_message(
        signature_domain=PRICING_RATE_CARD_SIGNATURE_DOMAIN,
        canonical_payload=canonical_json_bytes(validated),
    )


def execution_cost_quote_signature_message(quote: ExecutionCostQuote) -> bytes:
    """Return the exact detached-signature message for a canonical cost quote."""

    validated = ExecutionCostQuote.model_validate(quote.model_dump(mode="python"))
    return detached_signature_message(
        signature_domain=PRICING_QUOTE_SIGNATURE_DOMAIN,
        canonical_payload=canonical_json_bytes(validated),
    )


def source_budget_signature_message(source: SourceBudgetAuthorization) -> bytes:
    """Return the exact detached-signature message for canonical source-budget bytes."""

    validated = SourceBudgetAuthorization.model_validate(source.model_dump(mode="python"))
    return detached_signature_message(
        signature_domain=SOURCE_BUDGET_SIGNATURE_DOMAIN,
        canonical_payload=canonical_json_bytes(validated),
    )


def source_budget_projection_signature_message(projection: SourceBudgetProjection) -> bytes:
    """Return the exact detached-signature message for one canonical projection."""

    validated = SourceBudgetProjection.model_validate(projection.model_dump(mode="python"))
    return detached_signature_message(
        signature_domain=SOURCE_BUDGET_PROJECTION_SIGNATURE_DOMAIN,
        canonical_payload=canonical_json_bytes(validated),
    )


__all__ = [
    "AUTHORITY_REGISTRY_SCHEMA_VERSION",
    "AuthorityRegistryFilesystemPin",
    "ExecutionRateCard",
    "ExecutionRateCardLine",
    "PRICING_QUOTE_SIGNATURE_DOMAIN",
    "PRICING_RATE_CARD_SIGNATURE_DOMAIN",
    "PricingAuthorityPin",
    "SOURCE_BUDGET_PROJECTION_SIGNATURE_DOMAIN",
    "SOURCE_BUDGET_SIGNATURE_DOMAIN",
    "SourceBudgetAuthorityPin",
    "SourceBudgetAuthorization",
    "SourceBudgetProjection",
    "authority_key_id",
    "detached_signature_message",
    "execution_cost_quote_signature_message",
    "rate_card_signature_message",
    "source_budget_projection_signature_message",
    "source_budget_signature_message",
]
