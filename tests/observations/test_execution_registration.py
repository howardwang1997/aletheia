from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aletheia.execution.allocator import PostgreSQLExecutionAllocator
from aletheia.observations import execution_registration as registration_module
from aletheia.observations.execution_registration import (
    PostgreSQLAtomicScientificExecutionRegistrar,
    ScientificExecutionRegistrationError,
    ScientificExecutionRegistrationVerificationContext,
)
from aletheia.observations.persistence import (
    ResearchScientificExecutionAuthorizationRecord,
)
from persistence_test_support import sqlite_observation_engine

_OBSERVATION_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_OBSERVATION_TESTS))
from test_scientific_bridge import _bridge_case  # noqa: E402


class _Allocator(PostgreSQLExecutionAllocator):
    def __init__(
        self,
        *,
        reserved_at,
        fail: bool = False,
        issuance_enabled: bool = False,
        verification_enabled: bool = True,
    ) -> None:
        self.reserved_at = reserved_at
        self.fail = fail
        self.issuance_enabled = issuance_enabled
        self.verification_enabled = verification_enabled
        self.calls: list[tuple[object, object, Session]] = []

    @property
    def runtime_control_issuance_enabled(self) -> bool:
        return self.issuance_enabled

    @property
    def runtime_control_verification_enabled(self) -> bool:
        return self.verification_enabled

    def admit_and_reserve_in_session(self, session, *, bundle, grant):
        assert session.in_transaction()
        self.calls.append((bundle, grant, session))
        if self.fail:
            raise RuntimeError("injected allocator failure")
        intent = bundle.intent
        return SimpleNamespace(
            created=True,
            lease_token="never-exposed",
            snapshot=SimpleNamespace(
                execution_id=intent.execution_id,
                attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
                intent_sha256=intent.intent_sha256,
                admission_sha256="a" * 64,
                resource_lease_sha256="b" * 64,
                bundle_sha256=bundle.bundle_sha256,
                grant_sha256=grant.grant_sha256,
                status="reserved",
                reserved_at=self.reserved_at,
            ),
        )


def _verification(case) -> ScientificExecutionRegistrationVerificationContext:
    return ScientificExecutionRegistrationVerificationContext(
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
    )


def _seed_authorization_parents(session: Session, case) -> None:
    message = case.authorization.message
    binding = message.action_protocol_binding
    event = binding.action_authorized_event
    session.execute(
        text("INSERT INTO research_quest_streams VALUES (:quest)"),
        {"quest": binding.action.quest_id},
    )
    session.execute(
        text("INSERT INTO research_kernel_objects VALUES (:action)"),
        {"action": binding.action.object_sha256},
    )
    session.execute(
        text(
            "INSERT INTO research_kernel_events "
            "VALUES (:quest, :sequence, :event, 'action_authorized')"
        ),
        {
            "quest": binding.action.quest_id,
            "sequence": event.sequence,
            "event": event.event_sha256,
        },
    )


def _scope(engine):
    @contextmanager
    def scope():
        with Session(engine) as session, session.begin():
            yield session

    return scope


def test_atomic_registrar_commits_sea_and_exact_nonsecret_reservation_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(case),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    receipt = registrar.register_and_reserve(case.authorization)

    assert receipt.authorization_sha256 == case.authorization.authorization_sha256
    assert receipt.registered_at < receipt.reserved_at
    assert receipt.qualification_only is True
    assert receipt.scientific_admission_allowed is False
    assert "never-exposed" not in receipt.model_dump_json()
    assert len(allocator.calls) == 1
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ResearchScientificExecutionAuthorizationRecord)
            )
            == 1
        )


def test_allocator_failure_rolls_back_the_sea_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(
        reserved_at=registered_at + timedelta(seconds=1),
        fail=True,
    )
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(case),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="failed atomic"):
        registrar.register_and_reserve(case.authorization)

    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ResearchScientificExecutionAuthorizationRecord)
            )
            == 0
        )


def test_registrar_rejects_runtime_signing_or_missing_public_verification() -> None:
    case = _bridge_case()
    verification = _verification(case)
    signing = _Allocator(
        reserved_at=case.authorization.message.authorized_at,
        issuance_enabled=True,
    )
    with pytest.raises(ValueError, match="cannot load a runtime-control signer"):
        PostgreSQLAtomicScientificExecutionRegistrar(
            verification=verification,
            allocator=signing,
        )
    unverified = _Allocator(
        reserved_at=case.authorization.message.authorized_at,
        verification_enabled=False,
    )
    with pytest.raises(ValueError, match="requires public-key runtime verification"):
        PostgreSQLAtomicScientificExecutionRegistrar(
            verification=verification,
            allocator=unverified,
        )
