"""Focused adversarial regressions for the PR-4b execution authority boundary."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import aletheia.execution.allocator as allocator_module
from aletheia.db import session_factory
from aletheia.execution.assignment_contracts import NodeAssignmentTransportPin

sys.path.insert(0, str(Path(__file__).resolve().parent))
from postgres_test_safety import require_isolated_pr4_postgres  # noqa: E402
from test_allocator import (  # noqa: E402
    _EXECUTION_TABLES,
    _prepared,
    _register_and_inventory,
)


@pytest.fixture(autouse=True)
def _clean_execution_tables() -> Iterator[None]:
    require_isolated_pr4_postgres()
    sessions = session_factory()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))
    yield
    require_isolated_pr4_postgres()
    with sessions() as session, session.begin():
        session.execute(text(f"TRUNCATE {', '.join(_EXECUTION_TABLES)} RESTART IDENTITY CASCADE"))


class _CorruptedPayloadEnvelope:
    """Keep valid relational columns while simulating hostile raw JSON persistence."""

    def __init__(self, envelope: object, updates: dict[str, object] | None) -> None:
        self._envelope = envelope
        self._updates = updates

    def __getattr__(self, name: str) -> object:
        return getattr(self._envelope, name)

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        if self._updates is None:
            return {}
        payload = self._envelope.model_dump(mode=mode)
        payload.update(self._updates)
        return payload


def test_deferred_envelope_trigger_rejects_missing_json_keys(monkeypatch) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    real_seal = allocator_module.seal_qualification_assignment

    def seal_with_missing_payload(**values: object) -> _CorruptedPayloadEnvelope:
        return _CorruptedPayloadEnvelope(real_seal(**values), None)

    monkeypatch.setattr(
        allocator_module,
        "seal_qualification_assignment",
        seal_with_missing_payload,
    )

    with pytest.raises(DBAPIError, match="sealed assignment envelope differs"):
        prepared.allocator.admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )


@pytest.mark.parametrize(
    "field",
    (
        "ephemeral_public_key_x25519_hex",
        "nonce_hex",
        "aad_sha256",
        "ciphertext_base64",
    ),
)
def test_deferred_envelope_trigger_rejects_null_crypto_fields(
    monkeypatch,
    field: str,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    real_seal = allocator_module.seal_qualification_assignment

    def seal_with_null_crypto_field(**values: object) -> _CorruptedPayloadEnvelope:
        return _CorruptedPayloadEnvelope(real_seal(**values), {field: None})

    monkeypatch.setattr(
        allocator_module,
        "seal_qualification_assignment",
        seal_with_null_crypto_field,
    )

    with pytest.raises(DBAPIError, match="sealed assignment envelope differs"):
        prepared.allocator.admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "1"),
        ("qualification_only", "true"),
        ("scientific_admission_allowed", "false"),
        ("aad_sha256", int("1" * 64)),
        ("ephemeral_public_key_x25519_hex", int("2" * 64)),
    ),
)
def test_deferred_envelope_trigger_rejects_noncanonical_scalar_types(
    monkeypatch,
    field: str,
    value: object,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    real_seal = allocator_module.seal_qualification_assignment

    def seal_with_wrong_scalar_type(**values: object) -> _CorruptedPayloadEnvelope:
        return _CorruptedPayloadEnvelope(real_seal(**values), {field: value})

    monkeypatch.setattr(
        allocator_module,
        "seal_qualification_assignment",
        seal_with_wrong_scalar_type,
    )

    with pytest.raises(DBAPIError, match="sealed assignment envelope differs"):
        prepared.allocator.admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("transport_policy_sha256", None),
        ("transport_principal_id", None),
        ("public_key_x25519_hex", None),
        ("valid_from", None),
        ("expires_at", None),
        ("revoked_at", "1999-01-01T00:00:00Z"),
        ("schema_version", "1"),
        ("qualification_only", "true"),
        ("scientific_admission_allowed", "false"),
        ("transport_policy_sha256", int("3" * 64)),
        ("public_key_x25519_hex", int("4" * 64)),
    ),
)
def test_deferred_envelope_trigger_rejects_invalid_transport_pin_fields(
    monkeypatch,
    field: str,
    value: object,
) -> None:
    prepared = _prepared(monkeypatch)
    _register_and_inventory(prepared)
    real_model_json = allocator_module._model_json

    def model_json_with_invalid_pin(model: object) -> dict[str, object]:
        payload = real_model_json(model)
        if isinstance(model, NodeAssignmentTransportPin):
            payload[field] = value
        return payload

    monkeypatch.setattr(allocator_module, "_model_json", model_json_with_invalid_pin)

    with pytest.raises(DBAPIError, match="sealed assignment envelope differs"):
        prepared.allocator.admit_and_reserve(
            bundle=prepared.bundle,
            grant=prepared.grant,
        )
