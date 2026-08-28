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
    AtomicScientificExecutionCampaignRegistrationReceipt,
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
from test_scientific_bridge import _bridge_case, _replicate_bridge_cases  # noqa: E402


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
        self.snapshot = None

    @property
    def runtime_control_issuance_enabled(self) -> bool:
        return self.issuance_enabled

    @property
    def runtime_control_verification_enabled(self) -> bool:
        return self.verification_enabled

    def load_exact_qualification_reservation_in_session(self, session, *, bundle, grant):
        assert session.in_transaction()
        if self.snapshot is not None:
            assert self.snapshot.execution_id == bundle.intent.execution_id
            assert self.snapshot.grant_sha256 == grant.grant_sha256
        return self.snapshot

    def admit_and_reserve_in_session(self, session, *, bundle, grant):
        assert session.in_transaction()
        self.calls.append((bundle, grant, session))
        if self.fail:
            raise RuntimeError("injected allocator failure")
        intent = bundle.intent
        created = self.snapshot is None
        if created:
            self.snapshot = SimpleNamespace(
                execution_id=intent.execution_id,
                attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
                intent_sha256=intent.intent_sha256,
                admission_sha256="a" * 64,
                resource_lease_sha256="b" * 64,
                bundle_sha256=bundle.bundle_sha256,
                grant_sha256=grant.grant_sha256,
                status="reserved",
                reserved_at=self.reserved_at,
            )
        return SimpleNamespace(
            created=created,
            lease_token="never-exposed" if created else None,
            snapshot=self.snapshot,
        )


class _CampaignAllocator(PostgreSQLExecutionAllocator):
    def __init__(self, *, first_reserved_at, fail_on_call: int | None = None) -> None:
        self.first_reserved_at = first_reserved_at
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[object, object, Session]] = []
        self.snapshots: dict[str, object] = {}

    @property
    def runtime_control_issuance_enabled(self) -> bool:
        return False

    @property
    def runtime_control_verification_enabled(self) -> bool:
        return True

    def load_exact_qualification_reservation_in_session(self, session, *, bundle, grant):
        assert session.in_transaction()
        attempt_id = bundle.intent.infrastructure_attempt.infrastructure_attempt_id
        snapshot = self.snapshots.get(attempt_id)
        if snapshot is not None:
            assert snapshot.execution_id == bundle.intent.execution_id
            assert snapshot.grant_sha256 == grant.grant_sha256
        return snapshot

    def admit_and_reserve_in_session(self, session, *, bundle, grant):
        assert session.in_transaction()
        self.calls.append((bundle, grant, session))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected campaign allocator failure")
        intent = bundle.intent
        attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
        snapshot = self.snapshots.get(attempt_id)
        created = snapshot is None
        if created:
            snapshot = SimpleNamespace(
                execution_id=intent.execution_id,
                attempt_id=attempt_id,
                intent_sha256=intent.intent_sha256,
                admission_sha256=(f"{len(self.calls):064x}"),
                resource_lease_sha256=(f"{len(self.calls) + 100:064x}"),
                bundle_sha256=bundle.bundle_sha256,
                grant_sha256=grant.grant_sha256,
                status="reserved",
                reserved_at=self.first_reserved_at + timedelta(seconds=len(self.snapshots)),
            )
            self.snapshots[attempt_id] = snapshot
        return SimpleNamespace(
            created=created,
            lease_token="never-exposed" if created else None,
            snapshot=snapshot,
        )


class _CurrentActionAuthority:
    def __init__(self, *, result_sha256: str | None = None) -> None:
        self.result_sha256 = result_sha256
        self.calls: list[tuple[Session, object, object]] = []

    def verify_current_action_protocol_binding_in_session(
        self,
        session,
        *,
        binding,
        observed_at,
    ):
        assert session.in_transaction()
        self.calls.append((session, binding, observed_at))
        return self.result_sha256 or binding.binding_sha256


class _Clock:
    def __init__(self, *values) -> None:
        self.values = list(values)

    def __call__(self, _session):
        if not self.values:
            raise AssertionError("execution-registration test clock was exhausted")
        return self.values.pop(0)


def _verification(
    case,
    *,
    current_action_authority: _CurrentActionAuthority | None = None,
) -> ScientificExecutionRegistrationVerificationContext:
    return ScientificExecutionRegistrationVerificationContext(
        qualification_authority=case.qualification_authority,
        current_action_authority=current_action_authority or _CurrentActionAuthority(),
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
    current_action_authority = _CurrentActionAuthority()
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=_Clock(registered_at, allocator.reserved_at),
    )
    prior_external_action_audits = len(case.action_authority.calls)

    receipt = registrar.register_and_reserve(case.authorization)

    assert receipt.authorization_sha256 == case.authorization.authorization_sha256
    assert receipt.registered_at < receipt.reserved_at
    assert receipt.qualification_only is True
    assert receipt.scientific_admission_allowed is False
    assert receipt.authorization_registration_committed is True
    assert receipt.qualification_reservation_committed is True
    assert receipt.exact_retry_stable is True
    assert "never-exposed" not in receipt.model_dump_json()
    assert len(allocator.calls) == 1
    assert len(current_action_authority.calls) == 1
    assert current_action_authority.calls[0][0] is allocator.calls[0][2]
    assert len(case.action_authority.calls) == prior_external_action_audits
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ResearchScientificExecutionAuthorizationRecord)
            )
            == 1
        )


def test_atomic_registrar_exact_retry_is_byte_stable_and_skips_current_head_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    current_action_authority = _CurrentActionAuthority()
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=_Clock(
            registered_at,
            allocator.reserved_at,
            allocator.reserved_at + timedelta(seconds=1),
        ),
    )

    first = registrar.register_and_reserve(case.authorization)
    allocator.snapshot.status = "running"
    retried = registrar.register_and_reserve(case.authorization)

    assert retried == first
    assert retried.receipt_sha256 == first.receipt_sha256
    assert len(current_action_authority.calls) == 1
    assert len(allocator.calls) == 2


def test_replicate_campaign_preregisters_every_slot_before_any_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _replicate_bridge_cases()
    authorizations = tuple(item.authorization for item in cases)
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, cases[0])
    registered_at = authorizations[0].message.authorized_at + timedelta(seconds=1)
    first_reserved_at = registered_at + timedelta(seconds=1)
    final_observed_at = first_reserved_at + timedelta(seconds=2)
    allocator = _CampaignAllocator(first_reserved_at=first_reserved_at)
    current_action_authority = _CurrentActionAuthority()
    locked_slots: list[str] = []
    monkeypatch.setattr(
        registration_module,
        "_lock_scientific_slot",
        lambda _session, slot_id: locked_slots.append(slot_id),
    )
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            cases[0],
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=_Clock(registered_at, final_observed_at),
    )

    receipt = registrar.register_and_reserve_campaign(authorizations)

    assert isinstance(receipt, AtomicScientificExecutionCampaignRegistrationReceipt)
    assert receipt.authorizations == authorizations
    assert tuple(item.scientific_slot_id for item in receipt.registration_receipts) == tuple(
        item.message.scientific_slot_id for item in authorizations
    )
    assert tuple(
        item.message.action_protocol_binding.replicate_slot.slot_index
        for item in receipt.authorizations
    ) == (1, 2)
    assert locked_slots == sorted(locked_slots)
    assert len(current_action_authority.calls) == 2
    assert len(allocator.calls) == 2
    assert max(item.registered_at for item in receipt.registration_receipts) < min(
        item.reserved_at for item in receipt.registration_receipts
    )
    assert "never-exposed" not in receipt.model_dump_json()
    with Session(engine) as session:
        rows = session.scalars(
            select(ResearchScientificExecutionAuthorizationRecord).order_by(
                ResearchScientificExecutionAuthorizationRecord.scientific_slot_id
            )
        ).all()
        assert len(rows) == 2
        assert len({item.source_event_sha256 for item in rows}) == 1


def test_replicate_campaign_exact_retry_is_stable_and_rejects_partial_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _replicate_bridge_cases()
    authorizations = tuple(item.authorization for item in cases)
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, cases[0])
    registered_at = authorizations[0].message.authorized_at + timedelta(seconds=1)
    first_reserved_at = registered_at + timedelta(seconds=1)
    allocator = _CampaignAllocator(first_reserved_at=first_reserved_at)
    current_action_authority = _CurrentActionAuthority()
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            cases[0],
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=_Clock(
            registered_at,
            first_reserved_at + timedelta(seconds=2),
            first_reserved_at + timedelta(seconds=3),
        ),
    )

    first = registrar.register_and_reserve_campaign(authorizations)
    retried = registrar.register_and_reserve_campaign(authorizations)

    assert retried == first
    assert retried.campaign_registration_sha256 == first.campaign_registration_sha256
    assert len(current_action_authority.calls) == 2
    assert len(allocator.calls) == 4

    partial_engine = sqlite_observation_engine()
    with Session(partial_engine) as session, session.begin():
        _seed_authorization_parents(session, cases[0])
    partial_allocator = _CampaignAllocator(first_reserved_at=first_reserved_at)
    partial_registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(cases[0]),
        allocator=partial_allocator,
        session_scope_factory=_scope(partial_engine),
        database_clock=_Clock(
            registered_at,
            first_reserved_at + timedelta(seconds=1),
            first_reserved_at + timedelta(seconds=2),
        ),
    )
    partial_registrar.register_and_reserve(authorizations[0])
    with pytest.raises(ScientificExecutionRegistrationError, match="partially registered"):
        partial_registrar.register_and_reserve_campaign(authorizations)


def test_replicate_campaign_rejects_noncanonical_or_partial_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _replicate_bridge_cases()
    authorizations = tuple(item.authorization for item in cases)
    registered_at = authorizations[0].message.authorized_at + timedelta(seconds=1)
    first_reserved_at = registered_at + timedelta(seconds=1)
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, cases[0])
    allocator = _CampaignAllocator(
        first_reserved_at=first_reserved_at,
        fail_on_call=2,
    )
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(cases[0]),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="failed atomic"):
        registrar.register_and_reserve_campaign(authorizations)
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ResearchScientificExecutionAuthorizationRecord)
            )
            == 0
        )

    with pytest.raises(ScientificExecutionRegistrationError, match="campaign failed atomic"):
        registrar.register_and_reserve_campaign(tuple(reversed(authorizations)))


def test_registrar_rejects_a_one_sided_historical_sea_before_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
        registration_module.register_scientific_execution_authorization(
            session,
            registration_module.ScientificExecutionAuthorizationWrite.from_contract(
                case.authorization,
                registered_at=registered_at,
            ),
        )
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    current_action_authority = _CurrentActionAuthority()
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="one-sided"):
        registrar.register_and_reserve(case.authorization)

    assert allocator.calls == []
    assert current_action_authority.calls == []


def test_registrar_rejects_a_one_sided_historical_reservation_before_sea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    with Session(engine) as session, session.begin():
        allocator.admit_and_reserve_in_session(
            session,
            bundle=case.authorization.message.qualification_bundle,
            grant=case.authorization.message.qualification_grant,
        )
    current_action_authority = _CurrentActionAuthority()
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="one-sided"):
        registrar.register_and_reserve(case.authorization)

    assert len(allocator.calls) == 1
    assert current_action_authority.calls == []


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


def test_new_registration_rejects_rebound_current_kernel_head_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    current_action_authority = _CurrentActionAuthority(result_sha256="f" * 64)
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: registered_at,
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="rebound"):
        registrar.register_and_reserve(case.authorization)

    assert allocator.calls == []
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ResearchScientificExecutionAuthorizationRecord)
            )
            == 0
        )


def test_new_registration_rechecks_sea_liveness_after_allocator_linearization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _bridge_case()
    engine = sqlite_observation_engine()
    with Session(engine) as session, session.begin():
        _seed_authorization_parents(session, case)
    registered_at = case.authorization.message.authorized_at + timedelta(seconds=1)
    allocator = _Allocator(reserved_at=registered_at + timedelta(seconds=1))
    current_action_authority = _CurrentActionAuthority()
    clock_values = iter((registered_at, case.authorization.message.expires_at))
    monkeypatch.setattr(registration_module, "_lock_scientific_slot", lambda *_args: None)
    registrar = PostgreSQLAtomicScientificExecutionRegistrar(
        verification=_verification(
            case,
            current_action_authority=current_action_authority,
        ),
        allocator=allocator,
        session_scope_factory=_scope(engine),
        database_clock=lambda _session: next(clock_values),
    )

    with pytest.raises(ScientificExecutionRegistrationError, match="failed atomic"):
        registrar.register_and_reserve(case.authorization)

    assert len(allocator.calls) == 1
    assert len(current_action_authority.calls) == 1
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
