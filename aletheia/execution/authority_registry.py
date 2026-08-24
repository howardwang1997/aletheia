"""Read-only, signed registries for execution pricing and source-budget authority.

Registry trees are provisioned outside the application.  This module has no create, publish,
register, mint, or signing API: it only reopens pinned immutable bytes, verifies their canonical
content identity and detached Ed25519 signature, and returns existing execution contracts.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from aletheia.execution.authority_contracts import (
    AuthorityRegistryFilesystemPin,
    ExecutionRateCard,
    PricingAuthorityPin,
    SourceBudgetAuthorityPin,
    SourceBudgetAuthorization,
    SourceBudgetProjection,
    detached_signature_message,
)
from aletheia.execution.runtime_contracts import (
    ExecutionCostQuote,
    VerifiedBudgetAuthorizationResolution,
    VerifiedExecutionReceiptResolution,
)
from aletheia.execution.schemas import ExecutionModel, canonical_json_bytes

RATE_CARD_NAMESPACE = "rate_cards"
EXECUTION_COST_QUOTE_NAMESPACE = "execution_cost_quotes"
SOURCE_BUDGET_NAMESPACE = "source_budgets"
SOURCE_BUDGET_PROJECTION_NAMESPACE = "source_budget_projections"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PREFIX = re.compile(r"^[0-9a-f]{2}$")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_READ_CHUNK_BYTES = 64 * 1024


class AuthorityRegistryError(ValueError):
    """A registry or registered authority failed closed verification."""


class AuthorityRegistryCustodyError(AuthorityRegistryError):
    """Pinned filesystem custody changed or contains an unsafe object."""


class AuthorityRegistrySignatureError(AuthorityRegistryError):
    """A detached registry signature is absent, malformed, or invalid."""


class AuthorityRegistryConflictError(AuthorityRegistryError):
    """A supposedly exact registry contains duplicate or conflicting authority."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    links: int
    owner_uid: int
    owner_gid: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "_FileIdentity":
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            links=metadata.st_nlink,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )


@dataclass(frozen=True)
class _BlobPair:
    digest: str
    document_components: tuple[str, ...]
    signature_components: tuple[str, ...]
    document_identity: _FileIdentity
    signature_identity: _FileIdentity


ModelT = TypeVar("ModelT", bound=ExecutionModel)


def _require_digest(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AuthorityRegistryError(f"{label} must be a lowercase SHA-256 digest")


def _require_observed_at(observed_at: datetime) -> None:
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or observed_at.utcoffset().total_seconds() != 0
    ):
        raise AuthorityRegistryError("authority registry observed_at must be timezone-aware UTC")


def authority_document_paths(*, namespace: str, digest: str) -> tuple[Path, Path]:
    """Return the fixed read-only layout for a content document and its raw signature."""

    if namespace not in {
        RATE_CARD_NAMESPACE,
        EXECUTION_COST_QUOTE_NAMESPACE,
        SOURCE_BUDGET_NAMESPACE,
        SOURCE_BUDGET_PROJECTION_NAMESPACE,
    }:
        raise ValueError("unknown authority registry namespace")
    _require_digest(digest, label="authority document identity")
    parent = Path(namespace) / "sha256" / digest[:2]
    return parent / f"{digest}.json", parent / f"{digest}.sig"


class _ReadOnlyRegistryTree:
    """Descriptor-relative reader pinned to one pre-provisioned immutable tree."""

    def __init__(self, root: Path, pin: AuthorityRegistryFilesystemPin) -> None:
        self.root = Path(root).absolute()
        self.pin = AuthorityRegistryFilesystemPin.model_validate(pin.model_dump(mode="python"))
        try:
            root_lstat = os.lstat(self.root)
        except OSError as exc:
            raise AuthorityRegistryCustodyError("authority registry root is missing") from exc
        if stat.S_ISLNK(root_lstat.st_mode):
            raise AuthorityRegistryCustodyError("authority registry root cannot be a symlink")
        descriptor = self._open_root(expected=None)
        try:
            self._root_identity = _FileIdentity.from_stat(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def _validate_directory(self, metadata: os.stat_result, *, label: str) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.pin.owner_uid
            or metadata.st_dev != self.pin.device_id
            or stat.S_IMODE(metadata.st_mode) != self.pin.directory_mode
        ):
            raise AuthorityRegistryCustodyError(f"{label} differs from its owner/device/mode pin")

    def _validate_file(self, metadata: os.stat_result, *, label: str) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != self.pin.owner_uid
            or metadata.st_dev != self.pin.device_id
            or stat.S_IMODE(metadata.st_mode) != self.pin.file_mode
        ):
            raise AuthorityRegistryCustodyError(
                f"{label} must be a pinned regular nlink=1 registry file"
            )
        if metadata.st_size > self.pin.maximum_document_bytes:
            raise AuthorityRegistryCustodyError(f"{label} exceeds the registry byte limit")

    def _open_root(self, *, expected: _FileIdentity | None) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root, flags)
        except OSError as exc:
            raise AuthorityRegistryCustodyError(
                "authority registry root is missing or unsafe"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            self._validate_directory(metadata, label="authority registry root")
            observed = _FileIdentity.from_stat(metadata)
            if expected is not None and observed != expected:
                raise AuthorityRegistryCustodyError("authority registry root identity changed")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_directory_from(self, root_descriptor: int, components: tuple[str, ...]) -> int:
        descriptor = os.dup(root_descriptor)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for component in components:
                if _SAFE_COMPONENT.fullmatch(component) is None or component in {".", ".."}:
                    raise AuthorityRegistryCustodyError(
                        "authority registry path component is invalid"
                    )
                try:
                    path_metadata = os.stat(
                        component,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise AuthorityRegistryCustodyError(
                        "authority registry directory is missing or unsafe"
                    ) from exc
                self._validate_directory(path_metadata, label="authority registry directory")
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise AuthorityRegistryCustodyError(
                        "authority registry directory is missing or unsafe"
                    ) from exc
                child_metadata = os.fstat(child)
                try:
                    self._validate_directory(
                        child_metadata,
                        label="authority registry directory",
                    )
                    if _FileIdentity.from_stat(child_metadata) != _FileIdentity.from_stat(
                        path_metadata
                    ):
                        raise AuthorityRegistryCustodyError(
                            "authority registry directory changed while opening"
                        )
                except Exception:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _read_once(
        self,
        root_descriptor: int,
        *,
        components: tuple[str, ...],
        expected: _FileIdentity,
    ) -> bytes:
        parent = self._open_directory_from(root_descriptor, components[:-1])
        name = components[-1]
        try:
            try:
                path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise AuthorityRegistryCustodyError(
                    "authority registry file is missing or unsafe"
                ) from exc
            self._validate_file(path_metadata, label="authority registry file")
            if _FileIdentity.from_stat(path_metadata) != expected:
                raise AuthorityRegistryCustodyError("authority registry file identity changed")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                raise AuthorityRegistryCustodyError(
                    "authority registry file is missing or unsafe"
                ) from exc
            try:
                before = os.fstat(descriptor)
                self._validate_file(before, label="authority registry file")
                if _FileIdentity.from_stat(before) != expected:
                    raise AuthorityRegistryCustodyError(
                        "authority registry file changed before read"
                    )
                remaining = before.st_size
                chunks: list[bytes] = []
                while remaining:
                    chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
                    if not chunk:
                        raise AuthorityRegistryCustodyError(
                            "authority registry file ended while being read"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise AuthorityRegistryCustodyError(
                        "authority registry file grew while being read"
                    )
                after = os.fstat(descriptor)
                if _FileIdentity.from_stat(after) != expected:
                    raise AuthorityRegistryCustodyError(
                        "authority registry file changed while being read"
                    )
            finally:
                os.close(descriptor)
            try:
                final_path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise AuthorityRegistryCustodyError(
                    "authority registry file was renamed while being read"
                ) from exc
            if _FileIdentity.from_stat(final_path_metadata) != expected:
                raise AuthorityRegistryCustodyError(
                    "authority registry file was replaced while being read"
                )
            return b"".join(chunks)
        finally:
            os.close(parent)

    def read_reopened(self, *, components: tuple[str, ...], expected: _FileIdentity) -> bytes:
        """Read twice by pathname and require the originally indexed inode both times."""

        root = self._open_root(expected=self._root_identity)
        try:
            first = self._read_once(root, components=components, expected=expected)
            second = self._read_once(root, components=components, expected=expected)
            if first != second:
                raise AuthorityRegistryCustodyError(
                    "authority registry bytes changed across mandatory reopen"
                )
        finally:
            os.close(root)
        check = self._open_root(expected=self._root_identity)
        os.close(check)
        return first

    def index_namespace(self, namespace: str) -> dict[str, _BlobPair]:
        if _SAFE_COMPONENT.fullmatch(namespace) is None:
            raise AuthorityRegistryCustodyError("authority registry namespace is invalid")
        root = self._open_root(expected=self._root_identity)
        try:
            sha_root = self._open_directory_from(root, (namespace, "sha256"))
            try:
                try:
                    prefixes = tuple(sorted(os.listdir(sha_root)))
                except OSError as exc:
                    raise AuthorityRegistryCustodyError(
                        "authority registry namespace cannot be enumerated"
                    ) from exc
                pairs: dict[str, dict[str, tuple[tuple[str, ...], _FileIdentity]]] = {}
                for prefix in prefixes:
                    if _PREFIX.fullmatch(prefix) is None:
                        raise AuthorityRegistryCustodyError(
                            "authority registry contains a non-content-addressed directory"
                        )
                    prefix_descriptor = self._open_directory_from(sha_root, (prefix,))
                    try:
                        try:
                            names = tuple(sorted(os.listdir(prefix_descriptor)))
                        except OSError as exc:
                            raise AuthorityRegistryCustodyError(
                                "authority registry prefix cannot be enumerated"
                            ) from exc
                        for name in names:
                            match = re.fullmatch(r"([0-9a-f]{64})\.(json|sig)", name)
                            if match is None or not match.group(1).startswith(prefix):
                                raise AuthorityRegistryCustodyError(
                                    "authority registry contains a non-content-addressed file"
                                )
                            digest, suffix = match.groups()
                            try:
                                metadata = os.stat(
                                    name,
                                    dir_fd=prefix_descriptor,
                                    follow_symlinks=False,
                                )
                            except OSError as exc:
                                raise AuthorityRegistryCustodyError(
                                    "authority registry file changed during indexing"
                                ) from exc
                            self._validate_file(metadata, label="authority registry file")
                            slots = pairs.setdefault(digest, {})
                            if suffix in slots:
                                raise AuthorityRegistryConflictError(
                                    "authority registry contains a duplicate document component"
                                )
                            slots[suffix] = (
                                (namespace, "sha256", prefix, name),
                                _FileIdentity.from_stat(metadata),
                            )
                    finally:
                        os.close(prefix_descriptor)
            finally:
                os.close(sha_root)
        finally:
            os.close(root)
        check = self._open_root(expected=self._root_identity)
        os.close(check)

        indexed: dict[str, _BlobPair] = {}
        for digest, components in pairs.items():
            if set(components) != {"json", "sig"}:
                raise AuthorityRegistryCustodyError(
                    "authority registry document lacks its detached signature pair"
                )
            document_components, document_identity = components["json"]
            signature_components, signature_identity = components["sig"]
            indexed[digest] = _BlobPair(
                digest=digest,
                document_components=document_components,
                signature_components=signature_components,
                document_identity=document_identity,
                signature_identity=signature_identity,
            )
        return indexed

    def load_pair(
        self,
        pair: _BlobPair,
        *,
        model_type: type[ModelT],
        public_key: Ed25519PublicKey,
        signature_domain: str,
    ) -> tuple[ModelT, bytes, bytes]:
        payload = self.read_reopened(
            components=pair.document_components,
            expected=pair.document_identity,
        )
        signature = self.read_reopened(
            components=pair.signature_components,
            expected=pair.signature_identity,
        )
        if len(signature) != 64:
            raise AuthorityRegistrySignatureError(
                "authority registry detached signature must contain 64 raw bytes"
            )
        if hashlib.sha256(payload).hexdigest() != pair.digest:
            raise AuthorityRegistryCustodyError(
                "authority registry document differs from its content address"
            )
        try:
            model = model_type.model_validate_json(payload)
        except (UnicodeDecodeError, ValidationError) as exc:
            raise AuthorityRegistryError("authority registry document is invalid") from exc
        if canonical_json_bytes(model) != payload:
            raise AuthorityRegistryError("authority registry document is not canonical JSON")
        try:
            public_key.verify(
                signature,
                detached_signature_message(
                    signature_domain=signature_domain,
                    canonical_payload=payload,
                ),
            )
        except InvalidSignature as exc:
            raise AuthorityRegistrySignatureError(
                "authority registry detached signature is invalid"
            ) from exc
        return model, payload, signature


def _public_key(public_key_ed25519_hex: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_ed25519_hex))
    except ValueError as exc:  # pins validate first; retain a fail-closed boundary
        raise AuthorityRegistrySignatureError("authority registry public key is invalid") from exc


class ExactExecutionCostQuoteRegistry:
    """Resolve only exact, signed quotes covered by signed deployment-pinned rate cards."""

    def __init__(
        self,
        root: Path,
        *,
        filesystem_pin: AuthorityRegistryFilesystemPin,
        pricing_authority_pin: PricingAuthorityPin,
    ) -> None:
        self._tree = _ReadOnlyRegistryTree(root, filesystem_pin)
        self._pin = PricingAuthorityPin.model_validate(
            pricing_authority_pin.model_dump(mode="python")
        )
        self._public_key = _public_key(self._pin.public_key_ed25519_hex)
        self._rate_card_pairs = self._tree.index_namespace(RATE_CARD_NAMESPACE)
        self._quote_pairs = self._tree.index_namespace(EXECUTION_COST_QUOTE_NAMESPACE)
        self._rate_cards: dict[str, ExecutionRateCard] = {}
        self._quotes: dict[str, ExecutionCostQuote] = {}

        for digest, pair in self._rate_card_pairs.items():
            rate_card, _, _ = self._tree.load_pair(
                pair,
                model_type=ExecutionRateCard,
                public_key=self._public_key,
                signature_domain=self._pin.rate_card_signature_domain,
            )
            if rate_card.rate_card_sha256 != digest:
                raise AuthorityRegistryCustodyError("rate-card identity is inconsistent")
            self._verify_rate_card_authority(rate_card)
            self._rate_cards[digest] = rate_card

        attempts: dict[str, str] = {}
        for digest, pair in self._quote_pairs.items():
            quote, _, _ = self._tree.load_pair(
                pair,
                model_type=ExecutionCostQuote,
                public_key=self._public_key,
                signature_domain=self._pin.quote_signature_domain,
            )
            if quote.quote_sha256 != digest:
                raise AuthorityRegistryCustodyError("cost-quote identity is inconsistent")
            self._verify_quote_authority(quote)
            attempt_key = quote.infrastructure_attempt_id
            previous = attempts.setdefault(attempt_key, digest)
            if previous != digest:
                raise AuthorityRegistryConflictError(
                    "exact quote registry contains conflicting quotes for one attempt"
                )
            self._quotes[digest] = quote

    @property
    def pricing_authority_pin(self) -> PricingAuthorityPin:
        """Return the immutable deployment pin for composition role checks."""

        return self._pin

    def _verify_rate_card_authority(self, rate_card: ExecutionRateCard) -> None:
        if (
            rate_card.pricing_policy_sha256 != self._pin.policy_sha256
            or rate_card.issued_by_principal_id != self._pin.principal_id
            or rate_card.pricing_authority_key_id != self._pin.key_id
            or not self._pin.active_at(rate_card.valid_from)
            or rate_card.active_until > self._pin.active_until
        ):
            raise AuthorityRegistryError(
                "rate card is outside its deployment-pinned pricing authority"
            )

    def _verify_quote_authority(self, quote: ExecutionCostQuote) -> None:
        rate_card = self._rate_cards.get(quote.rate_card_sha256)
        if rate_card is None:
            raise AuthorityRegistryError("cost quote names an absent signed rate card")
        line = next(
            (
                candidate
                for candidate in rate_card.lines
                if candidate.accepted_resource_class_ids == quote.accepted_resource_class_ids
                and candidate.currency_code == quote.currency_code
            ),
            None,
        )
        if (
            quote.pricing_policy_sha256 != self._pin.policy_sha256
            or quote.quoted_by_principal_id != self._pin.principal_id
            or line is None
            or quote.fixed_charge_microunits != line.fixed_charge_microunits
            or quote.charge_per_second_microunits != line.charge_per_second_microunits
            or quote.maximum_lease_seconds > line.maximum_lease_seconds
            or not rate_card.active_at(quote.quoted_at)
            or not self._pin.active_at(quote.quoted_at)
            or quote.expires_at > min(rate_card.active_until, self._pin.active_until)
        ):
            raise AuthorityRegistryError(
                "cost quote differs from its deployment-pinned rate-card authority"
            )

    def resolve_execution_cost_quote(
        self,
        *,
        cost_quote_sha256: str,
        observed_at: datetime,
    ) -> ExecutionCostQuote | None:
        _require_digest(cost_quote_sha256, label="cost quote identity")
        _require_observed_at(observed_at)
        pair = self._quote_pairs.get(cost_quote_sha256)
        if pair is None:
            return None
        quote, _, _ = self._tree.load_pair(
            pair,
            model_type=ExecutionCostQuote,
            public_key=self._public_key,
            signature_domain=self._pin.quote_signature_domain,
        )
        if quote != self._quotes[cost_quote_sha256]:
            raise AuthorityRegistryCustodyError("registered cost quote changed after indexing")
        rate_pair = self._rate_card_pairs[quote.rate_card_sha256]
        rate_card, _, _ = self._tree.load_pair(
            rate_pair,
            model_type=ExecutionRateCard,
            public_key=self._public_key,
            signature_domain=self._pin.rate_card_signature_domain,
        )
        if rate_card != self._rate_cards[quote.rate_card_sha256]:
            raise AuthorityRegistryCustodyError("registered rate card changed after indexing")
        self._verify_rate_card_authority(rate_card)
        self._verify_quote_authority(quote)
        if (
            not self._pin.active_at(observed_at)
            or not rate_card.active_at(observed_at)
            or not quote.quoted_at <= observed_at < quote.expires_at
        ):
            raise AuthorityRegistryError("registered cost quote is inactive")
        return quote


class SourceBudgetProjectionRegistry:
    """Resolve one signed, exact projection for each existing signed source budget."""

    def __init__(
        self,
        root: Path,
        *,
        filesystem_pin: AuthorityRegistryFilesystemPin,
        source_budget_authority_pin: SourceBudgetAuthorityPin,
        resolved_by_principal_id: str = "execution-source-budget-registry",
    ) -> None:
        if (
            not resolved_by_principal_id
            or resolved_by_principal_id != resolved_by_principal_id.strip()
        ):
            raise ValueError("budget registry resolver principal must be canonical")
        self._tree = _ReadOnlyRegistryTree(root, filesystem_pin)
        self._pin = SourceBudgetAuthorityPin.model_validate(
            source_budget_authority_pin.model_dump(mode="python")
        )
        self._public_key = _public_key(self._pin.public_key_ed25519_hex)
        self._resolved_by_principal_id = resolved_by_principal_id
        self._source_pairs = self._tree.index_namespace(SOURCE_BUDGET_NAMESPACE)
        self._projection_pairs = self._tree.index_namespace(SOURCE_BUDGET_PROJECTION_NAMESPACE)
        self._sources: dict[str, SourceBudgetAuthorization] = {}
        self._projections: dict[str, tuple[str, SourceBudgetProjection]] = {}

        source_ids: dict[str, str] = {}
        for digest, pair in self._source_pairs.items():
            source, _, _ = self._tree.load_pair(
                pair,
                model_type=SourceBudgetAuthorization,
                public_key=self._public_key,
                signature_domain=self._pin.source_signature_domain,
            )
            if source.source_budget_authorization_sha256 != digest:
                raise AuthorityRegistryCustodyError("source-budget identity is inconsistent")
            self._verify_source_authority(source)
            previous = source_ids.setdefault(source.source_budget_id, digest)
            if previous != digest:
                raise AuthorityRegistryConflictError(
                    "source-budget registry contains conflicting bytes for one source id"
                )
            self._sources[digest] = source

        authorization_owners: dict[str, str] = {}
        resource_budget_owners: dict[str, str] = {}
        for projection_digest, pair in self._projection_pairs.items():
            projection, _, _ = self._tree.load_pair(
                pair,
                model_type=SourceBudgetProjection,
                public_key=self._public_key,
                signature_domain=self._pin.projection_signature_domain,
            )
            if projection.projection_sha256 != projection_digest:
                raise AuthorityRegistryCustodyError("budget projection identity is inconsistent")
            source = self._sources.get(projection.source_budget_authorization_sha256)
            if source is None:
                raise AuthorityRegistryError("budget projection names absent source bytes")
            self._verify_exact_projection(source=source, projection=projection)
            source_digest = projection.source_budget_authorization_sha256
            if source_digest in self._projections:
                raise AuthorityRegistryConflictError(
                    "source budget has a duplicate or conflicting projection"
                )
            authorization_digest = projection.budget_authorization_sha256
            if authorization_digest in authorization_owners:
                raise AuthorityRegistryConflictError(
                    "budget authorization is projected more than once"
                )
            resource_budget_digest = projection.budget_authorization.resource_budget_sha256
            if resource_budget_digest in resource_budget_owners:
                raise AuthorityRegistryConflictError("resource budget is projected more than once")
            authorization_owners[authorization_digest] = source_digest
            resource_budget_owners[resource_budget_digest] = source_digest
            self._projections[source_digest] = (projection_digest, projection)

    @property
    def source_budget_authority_pin(self) -> SourceBudgetAuthorityPin:
        """Return the immutable deployment pin for composition role checks."""

        return self._pin

    def _verify_source_authority(self, source: SourceBudgetAuthorization) -> None:
        if (
            source.source_authorization_policy_sha256 != self._pin.policy_sha256
            or source.authorized_by_principal_id != self._pin.principal_id
            or source.source_authority_key_id != self._pin.key_id
            or not self._pin.active_at(source.authorized_at)
            or source.active_until > self._pin.active_until
        ):
            raise AuthorityRegistryError("source budget is outside its deployment-pinned authority")

    def _verify_exact_projection(
        self,
        *,
        source: SourceBudgetAuthorization,
        projection: SourceBudgetProjection,
    ) -> None:
        authorization = projection.budget_authorization
        if (
            projection.source_authorization_policy_sha256 != self._pin.policy_sha256
            or projection.projected_by_principal_id != self._pin.principal_id
            or projection.source_authority_key_id != self._pin.key_id
            or not self._pin.active_at(projection.projected_at)
            or projection.projected_at >= source.active_until
            or authorization.quest_id != source.quest_id
            or authorization.currency_code != source.currency_code
            or authorization.maximum_cost_microunits != source.maximum_cost_microunits
            or authorization.deadline != source.deadline
            or authorization.authorized_by_principal_id != source.authorized_by_principal_id
            or authorization.authorized_at != source.authorized_at
            or authorization.expires_at != source.active_until
        ):
            raise AuthorityRegistryError(
                "budget projection differs from its exact signed source authority"
            )

    def resolve_budget_authorization(
        self,
        *,
        source_budget_authorization_sha256: str,
        observed_at: datetime,
    ) -> VerifiedBudgetAuthorizationResolution | None:
        _require_digest(
            source_budget_authorization_sha256,
            label="source budget authorization identity",
        )
        _require_observed_at(observed_at)
        indexed = self._projections.get(source_budget_authorization_sha256)
        if indexed is None:
            return None
        projection_digest, expected_projection = indexed
        source_pair = self._source_pairs[source_budget_authorization_sha256]
        source, source_payload, source_signature = self._tree.load_pair(
            source_pair,
            model_type=SourceBudgetAuthorization,
            public_key=self._public_key,
            signature_domain=self._pin.source_signature_domain,
        )
        projection_pair = self._projection_pairs[projection_digest]
        projection, _, _ = self._tree.load_pair(
            projection_pair,
            model_type=SourceBudgetProjection,
            public_key=self._public_key,
            signature_domain=self._pin.projection_signature_domain,
        )
        if (
            source != self._sources[source_budget_authorization_sha256]
            or projection != expected_projection
            or hashlib.sha256(source_payload).hexdigest() != source_budget_authorization_sha256
        ):
            raise AuthorityRegistryCustodyError("source-budget registry changed after indexing")
        self._verify_source_authority(source)
        self._verify_exact_projection(source=source, projection=projection)
        authorization = projection.budget_authorization
        if (
            not self._pin.active_at(observed_at)
            or not source.active_at(observed_at)
            or not authorization.authorized_at <= observed_at < authorization.expires_at
            or projection.projected_at > observed_at
        ):
            raise AuthorityRegistryError("registered source budget projection is inactive")
        return VerifiedBudgetAuthorizationResolution(
            source_budget_authorization_sha256=source_budget_authorization_sha256,
            source_authorization_canonical_bytes_sha256=(
                hashlib.sha256(source_payload).hexdigest()
            ),
            source_authorization_policy_sha256=(source.source_authorization_policy_sha256),
            source_authorization_signature_sha256=hashlib.sha256(source_signature).hexdigest(),
            budget_authorization_sha256=authorization.authorization_sha256,
            budget_authorization=authorization,
            resolved_by_principal_id=self._resolved_by_principal_id,
            resolved_at=observed_at,
        )


class CompositeExecutionAuthorityResolver:
    """Compose the two signed registries with an existing receipt custody resolver."""

    def __init__(
        self,
        *,
        quote_registry: ExactExecutionCostQuoteRegistry,
        budget_registry: SourceBudgetProjectionRegistry,
        execution_receipt_resolver: object,
    ) -> None:
        if not callable(getattr(execution_receipt_resolver, "resolve_execution_receipt", None)):
            raise TypeError("execution receipt resolver does not implement its read-only port")
        self._quote_registry = quote_registry
        self._budget_registry = budget_registry
        self._execution_receipt_resolver = execution_receipt_resolver
        pricing_pin = quote_registry.pricing_authority_pin
        budget_pin = budget_registry.source_budget_authority_pin
        if (
            pricing_pin.key_id == budget_pin.key_id
            or pricing_pin.principal_id == budget_pin.principal_id
        ):
            raise AuthorityRegistryConflictError(
                "pricing and source-budget authorities must use distinct keys and principals"
            )

    @property
    def pricing_authority_pin(self) -> PricingAuthorityPin:
        return self._quote_registry.pricing_authority_pin

    @property
    def source_budget_authority_pin(self) -> SourceBudgetAuthorityPin:
        return self._budget_registry.source_budget_authority_pin

    def resolve_execution_cost_quote(
        self,
        *,
        cost_quote_sha256: str,
        observed_at: datetime,
    ) -> ExecutionCostQuote | None:
        return self._quote_registry.resolve_execution_cost_quote(
            cost_quote_sha256=cost_quote_sha256,
            observed_at=observed_at,
        )

    def resolve_budget_authorization(
        self,
        *,
        source_budget_authorization_sha256: str,
        observed_at: datetime,
    ) -> VerifiedBudgetAuthorizationResolution | None:
        return self._budget_registry.resolve_budget_authorization(
            source_budget_authorization_sha256=source_budget_authorization_sha256,
            observed_at=observed_at,
        )

    def resolve_execution_receipt(
        self,
        *,
        execution_receipt_sha256: str,
        observed_at: datetime,
    ) -> VerifiedExecutionReceiptResolution | None:
        _require_digest(execution_receipt_sha256, label="execution receipt identity")
        _require_observed_at(observed_at)
        candidate = self._execution_receipt_resolver.resolve_execution_receipt(
            execution_receipt_sha256=execution_receipt_sha256,
            observed_at=observed_at,
        )
        if candidate is None:
            return None
        try:
            resolution = VerifiedExecutionReceiptResolution.model_validate(
                candidate.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise AuthorityRegistryError(
                "execution receipt resolver returned invalid custody"
            ) from exc
        if (
            resolution.execution_receipt_sha256 != execution_receipt_sha256
            or resolution.resolved_at != observed_at
        ):
            raise AuthorityRegistryError("execution receipt resolver returned divergent custody")
        return resolution


# Explicit descriptive aliases retained at the adapter boundary; none exposes mutation authority.
ReadOnlyPricingAuthorityRegistry = ExactExecutionCostQuoteRegistry
ReadOnlySourceBudgetProjectionRegistry = SourceBudgetProjectionRegistry
SignedExactQuoteRegistry = ExactExecutionCostQuoteRegistry
SignedSourceBudgetProjectionRegistry = SourceBudgetProjectionRegistry


__all__ = [
    "AuthorityRegistryConflictError",
    "AuthorityRegistryCustodyError",
    "AuthorityRegistryError",
    "AuthorityRegistrySignatureError",
    "CompositeExecutionAuthorityResolver",
    "EXECUTION_COST_QUOTE_NAMESPACE",
    "ExactExecutionCostQuoteRegistry",
    "RATE_CARD_NAMESPACE",
    "ReadOnlyPricingAuthorityRegistry",
    "ReadOnlySourceBudgetProjectionRegistry",
    "SOURCE_BUDGET_NAMESPACE",
    "SOURCE_BUDGET_PROJECTION_NAMESPACE",
    "SignedExactQuoteRegistry",
    "SignedSourceBudgetProjectionRegistry",
    "SourceBudgetProjectionRegistry",
    "authority_document_paths",
]
