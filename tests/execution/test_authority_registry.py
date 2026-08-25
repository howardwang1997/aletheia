from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.execution.authority_contracts import (
    AuthorityRegistryFilesystemPin,
    ExecutionRateCard,
    ExecutionRateCardLine,
    PricingAuthorityPin,
    SourceBudgetAuthorityPin,
    SourceBudgetAuthorization,
    SourceBudgetProjection,
    authority_key_id,
    detached_signature_message,
)
from aletheia.execution.authority_registry import (
    EXECUTION_COST_QUOTE_NAMESPACE,
    RATE_CARD_NAMESPACE,
    SOURCE_BUDGET_NAMESPACE,
    SOURCE_BUDGET_PROJECTION_NAMESPACE,
    AuthorityRegistryConflictError,
    AuthorityRegistryCustodyError,
    AuthorityRegistryError,
    AuthorityRegistrySignatureError,
    CompositeExecutionAuthorityResolver,
    ExactExecutionCostQuoteRegistry,
    SourceBudgetProjectionRegistry,
    authority_document_paths,
)
from aletheia.execution.ports import ExecutionAuthorityResolverPort
from aletheia.execution.runtime_contracts import BudgetAuthorization, ExecutionCostQuote
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.protocols.schemas import ResourceBudgetContract

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
QUEST_ID = "qst_" + "a" * 32
RESOURCE_CLASS_ID = "rsc_" + "b" * 32
H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64


@pytest.fixture(autouse=True)
def _restore_writable_tmp_tree(tmp_path: Path):
    """Let pytest remove fixture trees after tests exercise immutable POSIX modes."""

    yield
    for path in sorted(tmp_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)
        except FileNotFoundError:
            pass
    tmp_path.chmod(0o700)


def _private_key(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _public_hex(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _pricing_pin(
    private_key: Ed25519PrivateKey,
    *,
    revoked_at: datetime | None = None,
) -> PricingAuthorityPin:
    public_hex = _public_hex(private_key)
    return PricingAuthorityPin(
        policy_sha256=H0,
        principal_id="principal:pricing-authority",
        key_id=authority_key_id(public_hex),
        public_key_ed25519_hex=public_hex,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        revoked_at=revoked_at,
    )


def _budget_pin(
    private_key: Ed25519PrivateKey,
    *,
    revoked_at: datetime | None = None,
) -> SourceBudgetAuthorityPin:
    public_hex = _public_hex(private_key)
    return SourceBudgetAuthorityPin(
        policy_sha256=H1,
        principal_id="principal:source-budget-authority",
        key_id=authority_key_id(public_hex),
        public_key_ed25519_hex=public_hex,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        revoked_at=revoked_at,
    )


def _rate_card(pin: PricingAuthorityPin, **updates: object) -> ExecutionRateCard:
    card = ExecutionRateCard(
        pricing_policy_sha256=pin.policy_sha256,
        issued_by_principal_id=pin.principal_id,
        pricing_authority_key_id=pin.key_id,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        lines=(
            ExecutionRateCardLine(
                accepted_resource_class_ids=(RESOURCE_CLASS_ID,),
                currency_code="USD",
                fixed_charge_microunits=100,
                charge_per_second_microunits=5,
                maximum_lease_seconds=600,
            ),
        ),
    )
    return card.model_copy(update=updates)


def _quote(card: ExecutionRateCard, **updates: object) -> ExecutionCostQuote:
    quote = ExecutionCostQuote(
        quest_id=QUEST_ID,
        protocol_sha256=H0,
        work_order_sha256=H1,
        intent_sha256=H2,
        execution_id="exe_" + "c" * 32,
        infrastructure_attempt_id="iat_" + "d" * 32,
        accepted_resource_class_ids=(RESOURCE_CLASS_ID,),
        permitted_node_manifest_sha256s=(H3,),
        selected_node_manifest_sha256=H3,
        selected_resource_ids=("cpu.socket-0",),
        currency_code="USD",
        rate_card_sha256=card.rate_card_sha256,
        fixed_charge_microunits=100,
        charge_per_second_microunits=5,
        maximum_lease_seconds=60,
        maximum_charge_microunits=400,
        pricing_policy_sha256=H0,
        quoted_by_principal_id="principal:pricing-authority",
        quoted_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=20),
    )
    return quote.model_copy(update=updates)


@dataclass(frozen=True)
class _BudgetCase:
    source: SourceBudgetAuthorization
    resource_budget: ResourceBudgetContract
    authorization: BudgetAuthorization
    projection: SourceBudgetProjection


def _budget_case(pin: SourceBudgetAuthorityPin, **source_updates: object) -> _BudgetCase:
    source = SourceBudgetAuthorization(
        source_budget_id="budget:quest-a:2026-08",
        quest_id=QUEST_ID,
        currency_code="USD",
        maximum_cost_microunits=10_000,
        deadline=NOW + timedelta(hours=1),
        authorized_by_principal_id=pin.principal_id,
        authorized_at=NOW - timedelta(minutes=30),
        expires_at=NOW + timedelta(minutes=30),
        source_authorization_policy_sha256=pin.policy_sha256,
        source_authority_key_id=pin.key_id,
    ).model_copy(update=source_updates)
    resource_budget = ResourceBudgetContract(
        currency_code=source.currency_code,
        maximum_cost_microunits=source.maximum_cost_microunits,
        maximum_total_artifact_bytes=1024,
        deadline=source.deadline,
        budget_authorization_sha256=source.source_budget_authorization_sha256,
        checkpoint_policy_sha256=H2,
        permitted_retention_policy_sha256s=(H3,),
    )
    authorization = BudgetAuthorization(
        quest_id=source.quest_id,
        protocol_sha256=H4,
        work_order_sha256=H5,
        resource_budget_sha256=resource_budget.resource_budget_sha256,
        source_budget_authorization_sha256=source.source_budget_authorization_sha256,
        currency_code=source.currency_code,
        maximum_cost_microunits=source.maximum_cost_microunits,
        deadline=source.deadline,
        authorized_by_principal_id=source.authorized_by_principal_id,
        authorized_at=source.authorized_at,
        expires_at=source.expires_at,
    )
    projection = SourceBudgetProjection(
        source_budget_authorization_sha256=source.source_budget_authorization_sha256,
        source_authorization_policy_sha256=source.source_authorization_policy_sha256,
        budget_authorization_sha256=authorization.authorization_sha256,
        budget_authorization=authorization,
        projected_by_principal_id=pin.principal_id,
        source_authority_key_id=pin.key_id,
        projected_at=NOW - timedelta(minutes=20),
    )
    return _BudgetCase(source, resource_budget, authorization, projection)


def _write_document(
    root: Path,
    *,
    namespace: str,
    model: object,
    signature_domain: str,
    private_key: Ed25519PrivateKey,
) -> tuple[str, Path, Path]:
    payload = canonical_json_bytes(model)
    digest = hashlib.sha256(payload).hexdigest()
    document_relative, signature_relative = authority_document_paths(
        namespace=namespace,
        digest=digest,
    )
    document = root / document_relative
    signature = root / signature_relative
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_bytes(payload)
    signature.write_bytes(
        private_key.sign(
            detached_signature_message(
                signature_domain=signature_domain,
                canonical_payload=payload,
            )
        )
    )
    return digest, document, signature


def _install_pricing(
    root: Path,
    *,
    pin: PricingAuthorityPin,
    private_key: Ed25519PrivateKey,
    card: ExecutionRateCard,
    quotes: tuple[ExecutionCostQuote, ...],
) -> dict[str, tuple[Path, Path]]:
    installed: dict[str, tuple[Path, Path]] = {}
    digest, document, signature = _write_document(
        root,
        namespace=RATE_CARD_NAMESPACE,
        model=card,
        signature_domain=pin.rate_card_signature_domain,
        private_key=private_key,
    )
    installed[digest] = (document, signature)
    for quote in quotes:
        digest, document, signature = _write_document(
            root,
            namespace=EXECUTION_COST_QUOTE_NAMESPACE,
            model=quote,
            signature_domain=pin.quote_signature_domain,
            private_key=private_key,
        )
        installed[digest] = (document, signature)
    return installed


def _install_budget(
    root: Path,
    *,
    pin: SourceBudgetAuthorityPin,
    private_key: Ed25519PrivateKey,
    sources: tuple[SourceBudgetAuthorization, ...],
    projections: tuple[SourceBudgetProjection, ...],
) -> dict[str, tuple[Path, Path]]:
    installed: dict[str, tuple[Path, Path]] = {}
    for source in sources:
        digest, document, signature = _write_document(
            root,
            namespace=SOURCE_BUDGET_NAMESPACE,
            model=source,
            signature_domain=pin.source_signature_domain,
            private_key=private_key,
        )
        installed[digest] = (document, signature)
    for projection in projections:
        digest, document, signature = _write_document(
            root,
            namespace=SOURCE_BUDGET_PROJECTION_NAMESPACE,
            model=projection,
            signature_domain=pin.projection_signature_domain,
            private_key=private_key,
        )
        installed[digest] = (document, signature)
    return installed


def _freeze(root: Path) -> AuthorityRegistryFilesystemPin:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)
    metadata = root.stat()
    return AuthorityRegistryFilesystemPin(
        registry_id="registry:test-authority",
        owner_uid=metadata.st_uid,
        device_id=metadata.st_dev,
        directory_mode=0o500,
        file_mode=0o400,
    )


def _pricing_registry(
    root: Path,
    *,
    private_key: Ed25519PrivateKey | None = None,
    pin: PricingAuthorityPin | None = None,
    card: ExecutionRateCard | None = None,
    quotes: tuple[ExecutionCostQuote, ...] | None = None,
) -> tuple[
    ExactExecutionCostQuoteRegistry, PricingAuthorityPin, ExecutionRateCard, ExecutionCostQuote
]:
    private_key = private_key or _private_key(1)
    pin = pin or _pricing_pin(private_key)
    card = card or _rate_card(pin)
    default_quote = _quote(card)
    _install_pricing(
        root,
        pin=pin,
        private_key=private_key,
        card=card,
        quotes=quotes or (default_quote,),
    )
    filesystem_pin = _freeze(root)
    return (
        ExactExecutionCostQuoteRegistry(
            root,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        ),
        pin,
        card,
        (quotes or (default_quote,))[0],
    )


def _budget_registry(
    root: Path,
    *,
    private_key: Ed25519PrivateKey | None = None,
    pin: SourceBudgetAuthorityPin | None = None,
    case: _BudgetCase | None = None,
) -> tuple[SourceBudgetProjectionRegistry, SourceBudgetAuthorityPin, _BudgetCase]:
    private_key = private_key or _private_key(2)
    pin = pin or _budget_pin(private_key)
    case = case or _budget_case(pin)
    _install_budget(
        root,
        pin=pin,
        private_key=private_key,
        sources=(case.source,),
        projections=(case.projection,),
    )
    filesystem_pin = _freeze(root)
    return (
        SourceBudgetProjectionRegistry(
            root,
            filesystem_pin=filesystem_pin,
            source_budget_authority_pin=pin,
        ),
        pin,
        case,
    )


class _NullReceiptResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    def resolve_execution_receipt(
        self,
        *,
        execution_receipt_sha256: str,
        observed_at: datetime,
    ) -> None:
        self.calls.append((execution_receipt_sha256, observed_at))
        return None


def test_composite_roundtrip_is_exact_read_only_and_port_compatible(tmp_path: Path) -> None:
    pricing, _, _, quote = _pricing_registry(tmp_path / "pricing")
    budget, _, case = _budget_registry(tmp_path / "budget")
    receipt_resolver = _NullReceiptResolver()
    composite = CompositeExecutionAuthorityResolver(
        quote_registry=pricing,
        budget_registry=budget,
        execution_receipt_resolver=receipt_resolver,
    )

    assert isinstance(composite, ExecutionAuthorityResolverPort)
    assert (
        composite.resolve_execution_cost_quote(
            cost_quote_sha256=quote.quote_sha256,
            observed_at=NOW,
        )
        == quote
    )
    resolution = composite.resolve_budget_authorization(
        source_budget_authorization_sha256=(case.source.source_budget_authorization_sha256),
        observed_at=NOW,
    )
    assert resolution is not None
    assert resolution.budget_authorization == case.authorization
    assert resolution.source_authorization_canonical_bytes_sha256 == (
        case.source.source_budget_authorization_sha256
    )
    assert (
        composite.resolve_execution_receipt(
            execution_receipt_sha256=H0,
            observed_at=NOW,
        )
        is None
    )
    assert receipt_resolver.calls == [(H0, NOW)]
    assert pricing.resolve_execution_cost_quote(cost_quote_sha256=H5, observed_at=NOW) is None
    assert (
        budget.resolve_budget_authorization(
            source_budget_authorization_sha256=H5,
            observed_at=NOW,
        )
        is None
    )

    public_pricing = {name for name in dir(pricing) if not name.startswith("_")}
    public_budget = {name for name in dir(budget) if not name.startswith("_")}
    assert public_pricing == {
        "pricing_authority_pin",
        "resolve_execution_cost_quote",
    }
    assert public_budget == {
        "resolve_budget_authorization",
        "source_budget_authority_pin",
    }


def test_composite_requires_distinct_pricing_and_budget_authorities(tmp_path: Path) -> None:
    shared_key = _private_key(1)
    pricing, pricing_pin, _, _ = _pricing_registry(
        tmp_path / "pricing",
        private_key=shared_key,
    )
    budget_pin = _budget_pin(shared_key).model_copy(
        update={"principal_id": pricing_pin.principal_id}
    )
    budget, _, _ = _budget_registry(
        tmp_path / "budget",
        private_key=shared_key,
        pin=budget_pin,
    )

    with pytest.raises(AuthorityRegistryConflictError, match="distinct keys and principals"):
        CompositeExecutionAuthorityResolver(
            quote_registry=pricing,
            budget_registry=budget,
            execution_receipt_resolver=_NullReceiptResolver(),
        )


def test_source_budget_precedes_resource_budget_without_a_hash_cycle() -> None:
    case = _budget_case(_budget_pin(_private_key(2)))
    forbidden = {
        "protocol_sha256",
        "work_order_sha256",
        "resource_budget_sha256",
        "source_budget_authorization_sha256",
    }
    assert forbidden.isdisjoint(SourceBudgetAuthorization.model_fields)
    assert case.resource_budget.budget_authorization_sha256 == (
        case.source.source_budget_authorization_sha256
    )
    assert case.authorization.resource_budget_sha256 == (
        case.resource_budget.resource_budget_sha256
    )
    assert case.authorization.source_budget_authorization_sha256 == (
        case.source.source_budget_authorization_sha256
    )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "directory"])
def test_registry_rejects_symlink_and_nonregular_documents(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    key = _private_key(1)
    pin = _pricing_pin(key)
    card = _rate_card(pin)
    quote = _quote(card)
    installed = _install_pricing(
        tmp_path / "registry",
        pin=pin,
        private_key=key,
        card=card,
        quotes=(quote,),
    )
    document, _ = installed[quote.quote_sha256]
    payload = document.read_bytes()
    document.unlink()
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(payload)
        document.symlink_to(outside)
    elif unsafe_kind == "fifo":
        os.mkfifo(document)
    else:
        document.mkdir()
    filesystem_pin = _freeze(tmp_path / "registry")

    with pytest.raises(AuthorityRegistryCustodyError, match="regular|content-addressed"):
        ExactExecutionCostQuoteRegistry(
            tmp_path / "registry",
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )


def test_registry_rejects_symlink_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    real_root.chmod(0o500)
    link = tmp_path / "link"
    link.symlink_to(real_root, target_is_directory=True)
    metadata = real_root.stat()
    filesystem_pin = AuthorityRegistryFilesystemPin(
        registry_id="registry:symlink-test",
        owner_uid=metadata.st_uid,
        device_id=metadata.st_dev,
        directory_mode=0o500,
        file_mode=0o400,
    )
    with pytest.raises(AuthorityRegistryCustodyError, match="symlink"):
        ExactExecutionCostQuoteRegistry(
            link,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=_pricing_pin(_private_key(1)),
        )


@pytest.mark.parametrize("pin_change", ["owner", "device", "mode"])
def test_registry_enforces_owner_device_and_mode_pins(
    tmp_path: Path,
    pin_change: str,
) -> None:
    key = _private_key(1)
    pricing_pin = _pricing_pin(key)
    card = _rate_card(pricing_pin)
    quote = _quote(card)
    _install_pricing(
        tmp_path / "registry",
        pin=pricing_pin,
        private_key=key,
        card=card,
        quotes=(quote,),
    )
    filesystem_pin = _freeze(tmp_path / "registry")
    updates = {
        "owner": {"owner_uid": filesystem_pin.owner_uid + 1},
        "device": {"device_id": filesystem_pin.device_id + 1},
        "mode": {"directory_mode": 0o555},
    }[pin_change]
    divergent = filesystem_pin.model_copy(update=updates)
    with pytest.raises(AuthorityRegistryCustodyError, match="owner/device/mode"):
        ExactExecutionCostQuoteRegistry(
            tmp_path / "registry",
            filesystem_pin=divergent,
            pricing_authority_pin=pricing_pin,
        )


def test_registry_detects_rename_replacement_after_indexing(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry, _, _, quote = _pricing_registry(root)
    document_relative, _ = authority_document_paths(
        namespace=EXECUTION_COST_QUOTE_NAMESPACE,
        digest=quote.quote_sha256,
    )
    document = root / document_relative
    parent = document.parent
    payload = document.read_bytes()
    parent.chmod(0o700)
    document.rename(parent / "replaced-away.json")
    document.write_bytes(payload)
    document.chmod(0o400)
    parent.chmod(0o500)

    with pytest.raises(AuthorityRegistryCustodyError, match="identity changed"):
        registry.resolve_execution_cost_quote(
            cost_quote_sha256=quote.quote_sha256,
            observed_at=NOW,
        )


def test_registry_detects_in_place_tamper_after_indexing(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    registry, _, _, quote = _pricing_registry(root)
    document_relative, _ = authority_document_paths(
        namespace=EXECUTION_COST_QUOTE_NAMESPACE,
        digest=quote.quote_sha256,
    )
    document = root / document_relative
    document.chmod(0o600)
    payload = bytearray(document.read_bytes())
    payload[-2] = ord("f") if payload[-2] != ord("f") else ord("e")
    document.write_bytes(payload)
    document.chmod(0o400)

    with pytest.raises(AuthorityRegistryCustodyError, match="identity changed"):
        registry.resolve_execution_cost_quote(
            cost_quote_sha256=quote.quote_sha256,
            observed_at=NOW,
        )


def test_registry_rejects_wrong_key_and_tampered_detached_signature(tmp_path: Path) -> None:
    signing_key = _private_key(1)
    pin = _pricing_pin(signing_key)
    card = _rate_card(pin)
    quote = _quote(card)
    installed = _install_pricing(
        tmp_path / "wrong-key",
        pin=pin,
        private_key=signing_key,
        card=card,
        quotes=(quote,),
    )
    filesystem_pin = _freeze(tmp_path / "wrong-key")
    with pytest.raises(AuthorityRegistrySignatureError, match="signature is invalid"):
        ExactExecutionCostQuoteRegistry(
            tmp_path / "wrong-key",
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=_pricing_pin(_private_key(3)),
        )

    signature = installed[quote.quote_sha256][1]
    signature.chmod(0o600)
    signature.write_bytes(b"\x00" * 64)
    signature.chmod(0o400)
    filesystem_pin = _freeze(tmp_path / "wrong-key")
    with pytest.raises(AuthorityRegistrySignatureError, match="signature is invalid"):
        ExactExecutionCostQuoteRegistry(
            tmp_path / "wrong-key",
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )


def test_registry_rejects_noncanonical_json_and_missing_signature(tmp_path: Path) -> None:
    key = _private_key(1)
    pin = _pricing_pin(key)
    card = _rate_card(pin)
    quote = _quote(card)
    root = tmp_path / "noncanonical"
    _write_document(
        root,
        namespace=RATE_CARD_NAMESPACE,
        model=card,
        signature_domain=pin.rate_card_signature_domain,
        private_key=key,
    )
    payload = json.dumps(quote.model_dump(mode="json"), sort_keys=True, indent=2).encode()
    digest = hashlib.sha256(payload).hexdigest()
    document_relative, signature_relative = authority_document_paths(
        namespace=EXECUTION_COST_QUOTE_NAMESPACE,
        digest=digest,
    )
    document = root / document_relative
    signature = root / signature_relative
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_bytes(payload)
    signature.write_bytes(
        key.sign(
            detached_signature_message(
                signature_domain=pin.quote_signature_domain,
                canonical_payload=payload,
            )
        )
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryError, match="canonical JSON"):
        ExactExecutionCostQuoteRegistry(
            root,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )

    missing = tmp_path / "missing-signature"
    installed = _install_pricing(
        missing,
        pin=pin,
        private_key=key,
        card=card,
        quotes=(quote,),
    )
    installed[quote.quote_sha256][1].unlink()
    filesystem_pin = _freeze(missing)
    with pytest.raises(AuthorityRegistryCustodyError, match="detached signature"):
        ExactExecutionCostQuoteRegistry(
            missing,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )


def test_exact_quote_registry_rejects_duplicate_attempt_and_rate_conflict(tmp_path: Path) -> None:
    key = _private_key(1)
    pin = _pricing_pin(key)
    card = _rate_card(pin)
    first = _quote(card)
    second = _quote(
        card,
        execution_id="exe_" + "e" * 32,
        selected_resource_ids=("cpu.socket-1",),
    )
    root = tmp_path / "duplicate"
    _install_pricing(
        root,
        pin=pin,
        private_key=key,
        card=card,
        quotes=(first, second),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryConflictError, match="conflicting quotes"):
        ExactExecutionCostQuoteRegistry(
            root,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )

    divergent = _quote(
        card,
        fixed_charge_microunits=101,
        maximum_charge_microunits=401,
    )
    root = tmp_path / "rate-conflict"
    _install_pricing(
        root,
        pin=pin,
        private_key=key,
        card=card,
        quotes=(divergent,),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryError, match="rate-card authority"):
        ExactExecutionCostQuoteRegistry(
            root,
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )


@pytest.mark.parametrize(
    "observed_at",
    [NOW - timedelta(minutes=6), NOW + timedelta(minutes=21)],
)
def test_exact_quote_registry_rejects_inactive_time(
    tmp_path: Path,
    observed_at: datetime,
) -> None:
    registry, _, _, quote = _pricing_registry(tmp_path / "registry")
    with pytest.raises(AuthorityRegistryError, match="inactive"):
        registry.resolve_execution_cost_quote(
            cost_quote_sha256=quote.quote_sha256,
            observed_at=observed_at,
        )


def test_revoked_pricing_pin_fails_closed(tmp_path: Path) -> None:
    key = _private_key(1)
    pin = _pricing_pin(key, revoked_at=NOW)
    card = _rate_card(pin)
    quote = _quote(card)
    _install_pricing(
        tmp_path / "registry",
        pin=pin,
        private_key=key,
        card=card,
        quotes=(quote,),
    )
    filesystem_pin = _freeze(tmp_path / "registry")
    with pytest.raises(AuthorityRegistryError, match="pricing authority"):
        ExactExecutionCostQuoteRegistry(
            tmp_path / "registry",
            filesystem_pin=filesystem_pin,
            pricing_authority_pin=pin,
        )


def test_budget_projection_rejects_source_divergence_and_double_projection(
    tmp_path: Path,
) -> None:
    key = _private_key(2)
    pin = _budget_pin(key)
    case = _budget_case(pin)
    divergent_authorization = case.authorization.model_copy(
        update={"maximum_cost_microunits": case.authorization.maximum_cost_microunits - 1}
    )
    divergent_projection = case.projection.model_copy(
        update={
            "budget_authorization": divergent_authorization,
            "budget_authorization_sha256": divergent_authorization.authorization_sha256,
        }
    )
    root = tmp_path / "divergent"
    _install_budget(
        root,
        pin=pin,
        private_key=key,
        sources=(case.source,),
        projections=(divergent_projection,),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryError, match="exact signed source"):
        SourceBudgetProjectionRegistry(
            root,
            filesystem_pin=filesystem_pin,
            source_budget_authority_pin=pin,
        )

    second_authorization = case.authorization.model_copy(
        update={"work_order_sha256": H4, "resource_budget_sha256": H5}
    )
    second_projection = case.projection.model_copy(
        update={
            "budget_authorization": second_authorization,
            "budget_authorization_sha256": second_authorization.authorization_sha256,
            "projected_at": NOW - timedelta(minutes=10),
        }
    )
    root = tmp_path / "double"
    _install_budget(
        root,
        pin=pin,
        private_key=key,
        sources=(case.source,),
        projections=(case.projection, second_projection),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryConflictError, match="duplicate or conflicting"):
        SourceBudgetProjectionRegistry(
            root,
            filesystem_pin=filesystem_pin,
            source_budget_authority_pin=pin,
        )


def test_budget_registry_rejects_resource_budget_double_projection(tmp_path: Path) -> None:
    key = _private_key(2)
    pin = _budget_pin(key)
    first = _budget_case(pin)
    second_source = first.source.model_copy(update={"source_budget_id": "budget:second"})
    second_authorization = first.authorization.model_copy(
        update={
            "source_budget_authorization_sha256": (
                second_source.source_budget_authorization_sha256
            ),
        }
    )
    second_projection = first.projection.model_copy(
        update={
            "source_budget_authorization_sha256": (
                second_source.source_budget_authorization_sha256
            ),
            "budget_authorization": second_authorization,
            "budget_authorization_sha256": second_authorization.authorization_sha256,
        }
    )
    root = tmp_path / "registry"
    _install_budget(
        root,
        pin=pin,
        private_key=key,
        sources=(first.source, second_source),
        projections=(first.projection, second_projection),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryConflictError, match="resource budget"):
        SourceBudgetProjectionRegistry(
            root,
            filesystem_pin=filesystem_pin,
            source_budget_authority_pin=pin,
        )


def test_budget_registry_rejects_inactive_and_revoked_authority(tmp_path: Path) -> None:
    registry, _, case = _budget_registry(tmp_path / "active")
    with pytest.raises(AuthorityRegistryError, match="inactive"):
        registry.resolve_budget_authorization(
            source_budget_authorization_sha256=(case.source.source_budget_authorization_sha256),
            observed_at=case.source.expires_at,
        )

    key = _private_key(2)
    revoked_pin = _budget_pin(key, revoked_at=NOW)
    revoked_case = _budget_case(revoked_pin)
    root = tmp_path / "revoked"
    _install_budget(
        root,
        pin=revoked_pin,
        private_key=key,
        sources=(revoked_case.source,),
        projections=(revoked_case.projection,),
    )
    filesystem_pin = _freeze(root)
    with pytest.raises(AuthorityRegistryError, match="source budget"):
        SourceBudgetProjectionRegistry(
            root,
            filesystem_pin=filesystem_pin,
            source_budget_authority_pin=revoked_pin,
        )


def test_registry_rejects_naive_observation_time(tmp_path: Path) -> None:
    pricing, _, _, quote = _pricing_registry(tmp_path / "pricing")
    with pytest.raises(AuthorityRegistryError, match="timezone-aware UTC"):
        pricing.resolve_execution_cost_quote(
            cost_quote_sha256=quote.quote_sha256,
            observed_at=NOW.replace(tzinfo=None),
        )
