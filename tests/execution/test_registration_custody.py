from __future__ import annotations

from datetime import timedelta
import hashlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
from aletheia.execution.registration_custody import (
    QualificationExecutionRegistrationConfig,
    compose_qualification_execution_registration,
)
from aletheia.execution.runtime_contracts import (
    ExternalBridgeAuthority,
    QualificationAuthorityPin,
    QualificationVerificationError,
    qualification_key_id,
)

_CONTROLLER_TESTS = Path(__file__).resolve().parents[1] / "research_controller"
if str(_CONTROLLER_TESTS) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_TESTS))

from test_terminal_runtime import (  # noqa: E402
    _config as _terminal_config,
    _public_key,
)


def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    terminal, _controller_manifest = _terminal_config(monkeypatch, tmp_path)
    custody = QualificationPreAdmissionCustodyConfig(
        artifact_store_root=terminal.artifact_store_root,
        artifact_verifier_principal_id=terminal.artifact_verifier_principal_id,
        artifact_object_store_id=terminal.artifact_object_store_id,
        artifact_max_object_bytes=terminal.artifact_max_object_bytes,
        authority_registry_root=terminal.authority_registry_root,
        authority_registry_filesystem_pin=terminal.authority_registry_filesystem_pin,
        pricing_authority_pin=terminal.pricing_authority_pin,
        source_budget_authority_pin=terminal.source_budget_authority_pin,
        qualification_authority_pin=terminal.qualification_authority_pin,
        terminal_verification_authority_pin=terminal.terminal_verification_authority_pin,
        input_resolver_principal_id=terminal.input_resolver_principal_id,
        prepared_at=terminal.prepared_at,
    )
    return QualificationExecutionRegistrationConfig(
        qualification_custody=custody,
        runtime_control_authority_pin=terminal.runtime_control_authority_pin,
        node_authorities=terminal.node_authorities,
        allowed_rate_card_sha256s=terminal.allowed_rate_card_sha256s,
        allowed_currency_codes=terminal.allowed_currency_codes,
        allocator_principal_id=terminal.allocator_principal_id,
        prepared_at=terminal.prepared_at,
    )


def test_registration_custody_composes_public_verifiers_and_unsigned_allocator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    config = config.model_copy(update={"initial_assignment_lease_seconds": 2400})

    composition = compose_qualification_execution_registration(config)

    assert composition.allocator.runtime_control_issuance_enabled is False
    assert composition.allocator.runtime_control_verification_enabled is True
    assert composition.allocator._initial_assignment_lease == timedelta(seconds=2400)
    assert composition.allocator._artifact_resolver._artifact_store.read_only is True
    assert composition.qualification_authority.pin == (
        config.qualification_custody.qualification_authority_pin
    )
    with pytest.raises(QualificationVerificationError, match="cannot assert"):
        composition.qualification_custody.verify_qualification_admission(
            qualification_admission_sha256="a" * 64,
            bundle=object(),  # type: ignore[arg-type]
            grant=object(),  # type: ignore[arg-type]
            observed_at=config.prepared_at,
        )


def test_registration_custody_rejects_role_overlap_or_mutation_expansion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    payload = config.model_dump(mode="python")
    payload["allocator_principal_id"] = (
        config.qualification_custody.qualification_authority_pin.principal_id
    )
    with pytest.raises(ValidationError, match="distinct principals"):
        QualificationExecutionRegistrationConfig.model_validate(payload)

    payload = config.model_dump(mode="python")
    payload["execution_launch_allowed"] = True
    with pytest.raises(ValidationError):
        QualificationExecutionRegistrationConfig.model_validate(payload)

    payload = config.model_dump(mode="python")
    payload["initial_assignment_lease_seconds"] = 7201
    with pytest.raises(ValidationError):
        QualificationExecutionRegistrationConfig.model_validate(payload)


def _bridge_authority(terminal, *, label: str, class_ids: tuple[str, ...]):
    manifest = terminal.node_authorities[0].manifest.model_copy(
        update={"resource_class_ids": class_ids}
    )
    public_key = _public_key(f"registration-external-bridge-{label}")
    pin = QualificationAuthorityPin(
        policy_sha256=hashlib.sha256(
            f"registration-external-bridge-{label}:policy".encode()
        ).hexdigest(),
        principal_id=f"principal:registration-external-bridge-{label}",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=terminal.prepared_at - timedelta(days=1),
        expires_at=terminal.prepared_at + timedelta(days=1),
    )
    return ExternalBridgeAuthority(manifest=manifest, bridge_authority_pin=pin)


def test_registration_accepts_unsorted_unique_bridge_classes_but_no_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(monkeypatch, tmp_path)
    payload = config.model_dump(mode="python")
    terminal_root = tmp_path / "bridge-terminal"
    terminal_root.mkdir()
    terminal, _controller_manifest = _terminal_config(monkeypatch, terminal_root)

    # two bridge hosts whose served-class concatenation is not hash-sorted:
    # unique classes, so the closure must accept it (contradiction #15 review
    # finding: the old ordering check rejected this shape)
    payload["external_bridge_authorities"] = (
        _bridge_authority(terminal, label="one", class_ids=("rsc_mmmm",)),
        _bridge_authority(terminal, label="two", class_ids=("rsc_bbbb",)),
    )
    accepted = QualificationExecutionRegistrationConfig.model_validate(payload)
    assert accepted.external_bridge_authorities[0].served_resource_class_ids == (
        "rsc_mmmm",
    )

    # two distinct bridges both serving rsc_dup: duplicate service must fail
    # on uniqueness alone, whatever the ordering
    payload["external_bridge_authorities"] = (
        _bridge_authority(terminal, label="one", class_ids=("rsc_aaaa", "rsc_dup")),
        _bridge_authority(terminal, label="two", class_ids=("rsc_bbbb", "rsc_dup")),
    )
    with pytest.raises(ValidationError, match="must serve unique classes"):
        QualificationExecutionRegistrationConfig.model_validate(payload)
