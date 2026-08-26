from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
from aletheia.execution.registration_custody import (
    QualificationExecutionRegistrationConfig,
    compose_qualification_execution_registration,
)
from aletheia.execution.runtime_contracts import QualificationVerificationError

_CONTROLLER_TESTS = Path(__file__).resolve().parents[1] / "research_controller"
if str(_CONTROLLER_TESTS) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_TESTS))

from test_terminal_runtime import _config as _terminal_config  # noqa: E402


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

    composition = compose_qualification_execution_registration(config)

    assert composition.allocator.runtime_control_issuance_enabled is False
    assert composition.allocator.runtime_control_verification_enabled is True
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
