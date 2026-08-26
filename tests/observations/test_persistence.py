from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.observations.persistence import (
    OBSERVATION_PERSISTENCE_TABLES,
    ResearchControllerRegistrationRecord,
    ResearchObservationIssuanceChallengeRecord,
)
from aletheia.observations.store import (
    ContinuationReceiptWrite,
    ControllerDeliveryAttemptWrite,
    ControllerDeliveryResolutionWrite,
    ControllerDeliveryWrite,
    ControllerRegistrationWrite,
    ObservationAdmissionWrite,
    ObservationIdentityConflict,
    ObservationIssuanceChallengeWrite,
    ObservationValidationReceiptWrite,
    ProtocolCompilationWrite,
    ScientificExecutionAuthorizationWrite,
    get_continuation_receipt_by_slot,
    get_controller_delivery_by_source,
    get_controller_registration_by_launch_request,
    get_observation_issuance_challenge_by_sha256,
    get_observation_admission_by_slot,
    get_observation_validation_receipt_by_slot,
    get_protocol_compilation_by_action,
    get_scientific_execution_authorization_by_slot,
    lock_scientific_execution_authorization_by_slot,
    list_controller_deliveries,
    record_continuation_receipt,
    record_controller_delivery,
    record_controller_delivery_attempt,
    record_controller_delivery_resolution,
    record_observation_admission,
    record_observation_issuance_challenge,
    record_observation_validation_receipt,
    register_controller,
    register_protocol_compilation,
    register_scientific_execution_authorization,
)
from aletheia.research_controller.contracts import (
    ControllerDeadLetterReason,
    ControllerDeliveryAttempt,
    ControllerDeliveryAttemptKind,
    ControllerDeliveryResolution,
    ControllerDeliveryResolutionDisposition,
)
from aletheia.research_kernel.schemas import canonical_sha256
from persistence_test_support import sqlite_observation_engine

UTC = timezone.utc
NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
QUEST = "qst_" + "1" * 32
SLOT = "sos_" + "2" * 32
ACTION = "a" * 64
AUTHORIZATION = "b" * 64
QUALIFICATION = "c" * 64
EXECUTION = "exe_" + "a" * 32
ATTEMPT = "iat_" + "b" * 32


def _hash_payload(schema_name: str, marker: str) -> tuple[str, dict[str, object]]:
    payload: dict[str, object] = {
        "schema_name": schema_name,
        "schema_version": 1,
        "marker": marker,
    }
    return canonical_sha256(payload), payload


def _registration(*, quest_id: str = QUEST, marker: str = "one") -> ControllerRegistrationWrite:
    registration_id = "rcr_" + canonical_sha256({"marker": marker})[:32]
    launch_hash = canonical_sha256({"launch": marker})
    payload = {
        "schema_name": "aletheia.research_controller_registration",
        "schema_version": 1,
        "registration_id": registration_id,
        "launch": marker,
    }
    return ControllerRegistrationWrite(
        registration_sha256=canonical_sha256(payload),
        registration_id=registration_id,
        quest_id=quest_id,
        controller_id="rctl_" + "3" * 32,
        controller_manifest_sha256="4" * 64,
        controller_principal_id="controller:worker",
        registered_by_principal_id="controller:launcher",
        launch_request_sha256=launch_hash,
        registration_json=payload,
        registered_at=NOW,
    )


def _seed_external_parents(session: Session) -> None:
    session.execute(text("INSERT INTO research_quest_streams VALUES (:quest)"), {"quest": QUEST})
    session.execute(
        text("INSERT INTO research_kernel_objects VALUES (:action)"), {"action": ACTION}
    )
    session.execute(
        text("INSERT INTO execution_qualification_admissions VALUES (:admission)"),
        {"admission": QUALIFICATION},
    )
    session.execute(
        text("INSERT INTO execution_attempts VALUES (:attempt, :execution)"),
        {"attempt": ATTEMPT, "execution": EXECUTION},
    )
    session.execute(
        text("INSERT INTO research_kernel_events VALUES (:quest, 1, :event, 'action_authorized')"),
        {"quest": QUEST, "event": "5" * 64},
    )


def _seed_controller_delivery_generation_zero(
    session: Session,
) -> tuple[ControllerDeliveryWrite, ControllerDeliveryAttempt]:
    registration = _registration()
    session.execute(text("INSERT INTO research_quest_streams VALUES (:quest)"), {"quest": QUEST})
    session.execute(text("INSERT INTO durable_tasks VALUES ('task-0')"))
    register_controller(session, registration)
    payload = {"schema_name": "test.controller_delivery", "marker": "generation-chain"}
    delivery = ControllerDeliveryWrite(
        delivery_sha256=canonical_sha256(payload),
        registration_sha256=registration.registration_sha256,
        registration_id=registration.registration_id,
        quest_id=QUEST,
        source_kind="launch",
        source_key=registration.registration_id,
        source_sha256=registration.launch_request_sha256,
        launch_request_sha256=registration.launch_request_sha256,
        task_id="task-0",
        delivery_json=payload,
        delivered_at=NOW,
    )
    record_controller_delivery(session, delivery)
    attempt = ControllerDeliveryAttempt(
        delivery_sha256=delivery.delivery_sha256,
        quest_id=QUEST,
        wakeup_sha256="5" * 64,
        controller_manifest_sha256=registration.controller_manifest_sha256,
        generation=0,
        kind=ControllerDeliveryAttemptKind.INITIAL,
        task_id="task-0",
        task_request_sha256="6" * 64,
        recorded_at=NOW,
    )
    record_controller_delivery_attempt(
        session,
        ControllerDeliveryAttemptWrite.from_contract(attempt),
    )
    return delivery, attempt


def _failure_redrive_attempt(
    delivery: ControllerDeliveryWrite,
    *,
    predecessor: ControllerDeliveryAttempt,
) -> ControllerDeliveryAttempt:
    return ControllerDeliveryAttempt(
        delivery_sha256=delivery.delivery_sha256,
        quest_id=QUEST,
        wakeup_sha256=predecessor.wakeup_sha256,
        controller_manifest_sha256=predecessor.controller_manifest_sha256,
        generation=predecessor.generation + 1,
        kind=ControllerDeliveryAttemptKind.FAILURE_REDRIVE,
        task_id=f"task-{predecessor.generation + 1}",
        task_request_sha256="7" * 64,
        supersedes_task_id=predecessor.task_id,
        predecessor_status="failed",
        predecessor_terminal_category="infrastructure_exhausted",
        predecessor_terminal_detail_sha256="8" * 64,
        recorded_at=NOW + timedelta(minutes=predecessor.generation + 1),
    )


def _failed_generation_resolution(
    delivery: ControllerDeliveryWrite,
    attempt: ControllerDeliveryAttempt,
) -> ControllerDeliveryResolution:
    return ControllerDeliveryResolution(
        delivery_sha256=delivery.delivery_sha256,
        quest_id=QUEST,
        latest_attempt_sha256=attempt.attempt_sha256,
        exhausted_generation=attempt.generation,
        max_delivery_generation=attempt.generation,
        terminal_task_id=attempt.task_id,
        terminal_task_status="failed",
        terminal_category="infrastructure_exhausted",
        terminal_detail_sha256="8" * 64,
        controller_manifest_sha256=attempt.controller_manifest_sha256,
        disposition=ControllerDeliveryResolutionDisposition.DEAD_LETTER,
        dead_letter_reason=ControllerDeadLetterReason.GENERATION_LIMIT_EXHAUSTED,
        resolved_at=NOW + timedelta(hours=1),
    )


def test_schema_inventory_and_deferred_incorporation_guards_are_explicit() -> None:
    assert tuple(table.name for table in OBSERVATION_PERSISTENCE_TABLES) == (
        "research_controller_registrations",
        "research_controller_deliveries",
        "research_controller_delivery_attempts",
        "research_controller_delivery_resolutions",
        "research_protocol_compilations",
        "research_scientific_execution_authorizations",
        "research_observation_issuance_challenges",
        "research_observation_validation_receipts",
        "research_observation_admissions",
        "research_continuation_receipts",
    )
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260828_0027_scientific_controller_persistence.py"
    ).read_text()
    protocol_ddl, sea_and_later_ddl = migration.split(
        "CREATE TABLE research_scientific_execution_authorizations", maxsplit=1
    )
    protocol_columns = protocol_ddl.rsplit(
        "CREATE TABLE research_protocol_compilations", maxsplit=1
    )[1]
    sea_columns = sea_and_later_ddl.split(
        "CREATE TABLE research_observation_issuance_challenges", maxsplit=1
    )[0]
    assert "execution_id varchar(36)" not in protocol_columns
    assert "attempt_id varchar(36)" not in protocol_columns
    assert "execution_id varchar(36) NOT NULL" in sea_columns
    assert "attempt_id varchar(36) NOT NULL" in sea_columns
    assert "fk_rsea_exact_attempt" not in sea_columns
    assert "uq_roic_row_scope" not in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration
    assert "trg_roa_incorporation_complete" in migration
    assert "trg_rke_observation_incorporation_complete" in migration
    assert "trg_rc_delivery_initial_attempt_complete" in migration
    assert "trg_rc_delivery_attempt_chain_complete" in migration
    assert "trg_rc_delivery_resolution_exact" in migration
    assert "predecessor_task_row.status IS DISTINCT FROM NEW.predecessor_status" in migration
    assert "task_row.result_sha256 IS DISTINCT FROM NEW.terminal_result_sha256" in migration
    assert migration.count("FOR UPDATE;") >= 2
    assert "resolved controller delivery cannot append another attempt" in migration
    assert "controller resolution does not target the latest delivery attempt" in migration
    assert "{step_receipt,disposition}" in migration
    assert "{step_receipt,signed_kernel_command_committed}" in migration
    assert "{step_receipt,independent_observation_admission_committed}" in migration
    assert "'research-controller-receipt:' || NEW.predecessor_tick_receipt_sha256" in migration
    assert "research_controller_delivery_resolutions" in migration
    assert "generation >= 0 AND generation <= 1024" in migration
    for field in (
        "scientific_slot_id",
        "committed_admission_sha256",
        "scientific_observation_sha256",
        "action_id",
        "branch_id",
        "outcome",
        "source_world_model_sha256",
    ):
        assert f"{{payload,{field}}}" in migration


def test_sea_preregistration_precedes_pr4_attempt_creation() -> None:
    engine = sqlite_observation_engine()
    authorization_sha256, authorization_json = _hash_payload(
        "aletheia.scientific_execution_authorization", "prelaunch"
    )
    write = ScientificExecutionAuthorizationWrite(
        authorization_sha256=authorization_sha256,
        quest_id=QUEST,
        scientific_slot_id=SLOT,
        action_sha256=ACTION,
        execution_id=EXECUTION,
        attempt_id=ATTEMPT,
        source_event_sequence=1,
        source_event_sha256="5" * 64,
        qualification_bundle_sha256="8" * 64,
        qualification_grant_sha256="9" * 64,
        authorization_json=authorization_json,
        authorized_at=NOW,
        registered_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(hours=1),
        observation_admission_deadline=NOW + timedelta(hours=2),
    )
    with Session(engine) as session, session.begin():
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"), {"quest": QUEST}
        )
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"), {"action": ACTION}
        )
        session.execute(
            text(
                "INSERT INTO research_kernel_events VALUES (:quest, 1, :event, 'action_authorized')"
            ),
            {"quest": QUEST, "event": "5" * 64},
        )
        assert register_scientific_execution_authorization(session, write).created
        assert session.execute(text("SELECT count(*) FROM execution_attempts")).scalar_one() == 0


def test_controller_registration_exact_retry_variant_and_caller_rollback() -> None:
    engine = sqlite_observation_engine()
    write = _registration()
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"), {"quest": QUEST}
        )
        first = register_controller(session, write)
        assert first.created
        assert not register_controller(session, write).created
        assert (
            get_controller_registration_by_launch_request(session, write.launch_request_sha256)
            == write
        )
        session.rollback()

    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchControllerRegistrationRecord))
            == 0
        )

    with Session(engine) as session:
        session.execute(
            text("INSERT OR IGNORE INTO research_quest_streams VALUES (:quest)"), {"quest": QUEST}
        )
        register_controller(session, write)
        with pytest.raises(ObservationIdentityConflict):
            register_controller(session, _registration(marker="variant"))


def test_three_controller_delivery_sources_are_exact_and_append_only() -> None:
    engine = sqlite_observation_engine()
    registration = _registration()
    with Session(engine) as session:
        _seed_external_parents(session)
        register_controller(session, registration)
        session.execute(
            text(
                "INSERT INTO research_kernel_events VALUES (:quest, 2, :event, 'continue_committed')"
            ),
            {"quest": QUEST, "event": "6" * 64},
        )
        session.execute(
            text("INSERT INTO research_kernel_outbox VALUES ('rko_test', :quest, 2, :event)"),
            {"quest": QUEST, "event": "6" * 64},
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_terminal_outbox "
                "VALUES ('qto_test', :execution, :attempt, :hash)"
            ),
            {"execution": EXECUTION, "attempt": ATTEMPT, "hash": "7" * 64},
        )

        cases = (
            (
                "launch",
                registration.registration_id,
                registration.launch_request_sha256,
                None,
                None,
                None,
            ),
            ("kernel_outbox", "rko_test", "6" * 64, 2, None, None),
            (
                "execution_terminal_outbox",
                "qto_test",
                "7" * 64,
                None,
                EXECUTION,
                ATTEMPT,
            ),
        )
        for index, (kind, key, source_hash, version, execution_id, attempt_id) in enumerate(cases):
            session.execute(
                text("INSERT INTO durable_tasks VALUES (:task)"), {"task": f"task-{index}"}
            )
            payload = {"schema_name": "test.delivery", "kind": kind, "index": index}
            write = ControllerDeliveryWrite(
                delivery_sha256=canonical_sha256(payload),
                registration_sha256=registration.registration_sha256,
                registration_id=registration.registration_id,
                quest_id=QUEST,
                source_kind=kind,
                source_key=key,
                source_sha256=source_hash,
                source_stream_version=version,
                launch_request_sha256=(
                    registration.launch_request_sha256 if kind == "launch" else None
                ),
                execution_id=execution_id,
                attempt_id=attempt_id,
                task_id=f"task-{index}",
                delivery_json=payload,
                delivered_at=NOW + timedelta(seconds=index),
            )
            assert record_controller_delivery(session, write).created
            assert (
                get_controller_delivery_by_source(session, source_kind=kind, source_key=key)
                == write
            )
        assert len(list_controller_deliveries(session)) == 3
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(text("DELETE FROM research_controller_deliveries"))


def test_portable_store_does_not_append_after_delivery_resolution() -> None:
    engine = sqlite_observation_engine()
    with Session(engine) as session:
        delivery, attempt = _seed_controller_delivery_generation_zero(session)
        attempt_write = ControllerDeliveryAttemptWrite.from_contract(attempt)
        resolution = _failed_generation_resolution(delivery, attempt)
        assert record_controller_delivery_resolution(
            session,
            ControllerDeliveryResolutionWrite.from_contract(resolution),
        ).created

        assert not record_controller_delivery_attempt(session, attempt_write).created
        session.execute(text("INSERT INTO durable_tasks VALUES ('task-1')"))
        successor = _failure_redrive_attempt(delivery, predecessor=attempt)
        with pytest.raises(
            ObservationIdentityConflict,
            match="resolved controller delivery cannot append",
        ):
            record_controller_delivery_attempt(
                session,
                ControllerDeliveryAttemptWrite.from_contract(successor),
            )


def test_portable_store_resolution_must_target_latest_attempt() -> None:
    engine = sqlite_observation_engine()
    with Session(engine) as session:
        delivery, initial = _seed_controller_delivery_generation_zero(session)
        session.execute(text("INSERT INTO durable_tasks VALUES ('task-1')"))
        successor = _failure_redrive_attempt(delivery, predecessor=initial)
        record_controller_delivery_attempt(
            session,
            ControllerDeliveryAttemptWrite.from_contract(successor),
        )

        stale_resolution = _failed_generation_resolution(delivery, initial)
        with pytest.raises(
            ObservationIdentityConflict,
            match="latest exact delivery attempt",
        ):
            record_controller_delivery_resolution(
                session,
                ControllerDeliveryResolutionWrite.from_contract(stale_resolution),
            )


def test_protocol_and_observation_chain_is_recoverable_by_action_and_slot() -> None:
    engine = sqlite_observation_engine()
    with Session(engine) as session:
        _seed_external_parents(session)

        protocol = {"protocol_id": "protocol.test", "version": 1, "revision_parent_sha256": None}
        request = {"protocol": protocol, "catalog": "frozen"}
        protocol_hash = canonical_sha256(protocol)
        receipt = {"protocol_sha256": protocol_hash, "accepted": True}
        result = {"receipt": receipt, "work_order": "exact"}
        request_hash = canonical_sha256(request)
        result_hash = canonical_sha256(result)
        receipt_hash = canonical_sha256(receipt)
        compilation_identity = canonical_sha256(
            {
                "schema_name": "aletheia.protocol_compilation_registration_identity",
                "schema_version": 1,
                "quest_id": QUEST,
                "action_sha256": ACTION,
                "request_sha256": request_hash,
                "result_sha256": result_hash,
                "receipt_sha256": receipt_hash,
            }
        )
        compilation = ProtocolCompilationWrite(
            compilation_sha256=compilation_identity,
            quest_id=QUEST,
            action_sha256=ACTION,
            protocol_id="protocol.test",
            protocol_version=1,
            protocol_sha256=protocol_hash,
            request_sha256=request_hash,
            result_sha256=result_hash,
            receipt_sha256=receipt_hash,
            request_json=request,
            result_json=result,
            registered_at=NOW,
        )
        register_protocol_compilation(session, compilation)

        auth_hash, auth_json = _hash_payload(
            "aletheia.scientific_execution_authorization", "authorization"
        )
        authorization = ScientificExecutionAuthorizationWrite(
            authorization_sha256=auth_hash,
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            action_sha256=ACTION,
            execution_id=EXECUTION,
            attempt_id=ATTEMPT,
            source_event_sequence=1,
            source_event_sha256="5" * 64,
            qualification_bundle_sha256="8" * 64,
            qualification_grant_sha256="9" * 64,
            authorization_json=auth_json,
            authorized_at=NOW,
            registered_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            observation_admission_deadline=NOW + timedelta(hours=2),
        )
        register_scientific_execution_authorization(session, authorization)

        raw_run = "d" * 64
        challenge_hash, challenge_json = _hash_payload("test.validation_challenge", "validation")
        validation_challenge = ObservationIssuanceChallengeWrite(
            challenge_sha256=challenge_hash,
            purpose="validation",
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=auth_hash,
            nonce_sha256="e" * 64,
            row_scope="validation:row",
            raw_run_sha256=raw_run,
            database_authority_policy_sha256="f" * 64,
            issued_by_principal_id="database:authority",
            issuance_key_id="0" * 64,
            challenge_json=challenge_json,
            issued_at=NOW + timedelta(minutes=1),
            recorded_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=5),
            observation_admission_deadline=NOW + timedelta(hours=2),
        )
        record_observation_issuance_challenge(session, validation_challenge)

        committed_validation, validation_json = _hash_payload(
            "aletheia.committed_observation_validation_receipt", "validation"
        )
        validation_receipt = "1" * 64
        observation = "2" * 64
        validation = ObservationValidationReceiptWrite(
            committed_receipt_sha256=committed_validation,
            validation_receipt_sha256=validation_receipt,
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=auth_hash,
            qualification_admission_sha256=QUALIFICATION,
            raw_run_sha256=raw_run,
            issuance_challenge_sha256=challenge_hash,
            validation_campaign_sha256="3" * 64,
            disposition="validated_confirmation",
            outcome="negative",
            scientific_observation_sha256=observation,
            committed_receipt_json=validation_json,
            validated_at=NOW + timedelta(minutes=1),
            registered_at=NOW + timedelta(minutes=2),
            committed_at=NOW + timedelta(minutes=2),
        )
        record_observation_validation_receipt(session, validation)

        admission_challenge_hash, admission_challenge_json = _hash_payload(
            "test.admission_challenge", "admission"
        )
        admission_challenge = ObservationIssuanceChallengeWrite(
            challenge_sha256=admission_challenge_hash,
            purpose="admission",
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=auth_hash,
            nonce_sha256="4" * 64,
            row_scope="admission:row",
            committed_validation_receipt_sha256=committed_validation,
            validation_receipt_sha256=validation_receipt,
            database_authority_policy_sha256="f" * 64,
            issued_by_principal_id="database:authority",
            issuance_key_id="0" * 64,
            challenge_json=admission_challenge_json,
            issued_at=NOW + timedelta(minutes=3),
            recorded_at=NOW + timedelta(minutes=3),
            expires_at=NOW + timedelta(minutes=6),
            observation_admission_deadline=NOW + timedelta(hours=2),
        )
        record_observation_issuance_challenge(session, admission_challenge)

        event_hash = "6" * 64
        session.execute(
            text(
                "INSERT INTO research_kernel_events "
                "VALUES (:quest, 2, :event, 'observation_incorporated')"
            ),
            {"quest": QUEST, "event": event_hash},
        )
        admission_hash, admission_json = _hash_payload(
            "aletheia.committed_observation_admission", "admission"
        )
        admission = ObservationAdmissionWrite(
            committed_admission_sha256=admission_hash,
            decision_sha256="7" * 64,
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=auth_hash,
            committed_validation_receipt_sha256=committed_validation,
            validation_receipt_sha256=validation_receipt,
            issuance_challenge_sha256=admission_challenge_hash,
            disposition="admitted",
            admitted_observation_sha256=observation,
            admission_json=admission_json,
            registered_at=NOW + timedelta(minutes=4),
            committed_at=NOW + timedelta(minutes=4),
            incorporated_event_sequence=2,
            incorporated_event_sha256=event_hash,
            incorporated_event_type="observation_incorporated",
        )
        record_observation_admission(session, admission)

        continuation_hash, continuation_json = _hash_payload(
            "aletheia.graph_scoped_continuation_receipt", "continuation"
        )
        continuation = ContinuationReceiptWrite(
            receipt_sha256=continuation_hash,
            quest_id=QUEST,
            action_sha256=ACTION,
            scientific_slot_id=SLOT,
            world_model_snapshot_sha256="8" * 64,
            observation_projection_sha256="9" * 64,
            scientific_observation_sha256=observation,
            committed_admission_sha256=admission_hash,
            disposition="hypothesis_set_fork_required",
            receipt_json=continuation_json,
            recorded_at=NOW + timedelta(minutes=5),
        )
        record_continuation_receipt(session, continuation)

        assert (
            get_protocol_compilation_by_action(session, quest_id=QUEST, action_sha256=ACTION)
            == compilation
        )
        assert (
            get_scientific_execution_authorization_by_slot(
                session, quest_id=QUEST, scientific_slot_id=SLOT
            )
            == authorization
        )
        assert (
            lock_scientific_execution_authorization_by_slot(
                session,
                quest_id=QUEST,
                scientific_slot_id=SLOT,
            )
            == authorization
        )
        assert (
            get_observation_issuance_challenge_by_sha256(
                session,
                challenge_sha256=challenge_hash,
            )
            == validation_challenge
        )
        assert (
            get_observation_validation_receipt_by_slot(
                session, quest_id=QUEST, scientific_slot_id=SLOT
            )
            == validation
        )
        assert (
            get_observation_admission_by_slot(session, quest_id=QUEST, scientific_slot_id=SLOT)
            == admission
        )
        assert (
            get_continuation_receipt_by_slot(session, quest_id=QUEST, scientific_slot_id=SLOT)
            == continuation
        )
        session.commit()


def test_expired_issuance_challenge_can_be_reissued_without_mutating_history() -> None:
    engine = sqlite_observation_engine()
    with Session(engine) as session:
        _seed_external_parents(session)
        authorization_hash, authorization_json = _hash_payload(
            "aletheia.scientific_execution_authorization", "challenge-reissue"
        )
        register_scientific_execution_authorization(
            session,
            ScientificExecutionAuthorizationWrite(
                authorization_sha256=authorization_hash,
                quest_id=QUEST,
                scientific_slot_id=SLOT,
                action_sha256=ACTION,
                execution_id=EXECUTION,
                attempt_id=ATTEMPT,
                source_event_sequence=1,
                source_event_sha256="5" * 64,
                qualification_bundle_sha256="8" * 64,
                qualification_grant_sha256="9" * 64,
                authorization_json=authorization_json,
                authorized_at=NOW,
                registered_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                observation_admission_deadline=NOW + timedelta(hours=2),
            ),
        )

        def challenge(marker: str, *, issued_at: datetime) -> ObservationIssuanceChallengeWrite:
            challenge_hash, challenge_json = _hash_payload("test.validation_challenge", marker)
            return ObservationIssuanceChallengeWrite(
                challenge_sha256=challenge_hash,
                purpose="validation",
                quest_id=QUEST,
                scientific_slot_id=SLOT,
                authorization_sha256=authorization_hash,
                nonce_sha256=canonical_sha256({"nonce": marker}),
                row_scope="validation:stable-row",
                raw_run_sha256="d" * 64,
                database_authority_policy_sha256="f" * 64,
                issued_by_principal_id="database:authority",
                issuance_key_id="0" * 64,
                challenge_json=challenge_json,
                issued_at=issued_at,
                recorded_at=issued_at,
                expires_at=issued_at + timedelta(minutes=1),
                observation_admission_deadline=NOW + timedelta(hours=2),
            )

        first = challenge("first", issued_at=NOW + timedelta(minutes=1))
        reissued = challenge("second", issued_at=first.expires_at)
        assert record_observation_issuance_challenge(session, first).created
        with pytest.raises(ObservationIdentityConflict, match="live variant"):
            record_observation_issuance_challenge(
                session,
                challenge("overlap", issued_at=NOW + timedelta(minutes=1, seconds=30)),
            )
        assert record_observation_issuance_challenge(session, reissued).created
        assert not record_observation_issuance_challenge(session, reissued).created

        rows = list(
            session.scalars(
                select(ResearchObservationIssuanceChallengeRecord).order_by(
                    ResearchObservationIssuanceChallengeRecord.issued_at
                )
            )
        )
        assert [(row.challenge_sha256, row.expires_at) for row in rows] == [
            (first.challenge_sha256, first.expires_at.replace(tzinfo=None)),
            (reissued.challenge_sha256, reissued.expires_at.replace(tzinfo=None)),
        ]


def test_contract_helpers_reject_variant_lineage_and_admission_without_event() -> None:
    request = {"protocol": {"protocol_id": "protocol.test", "version": 2}}
    result = {"receipt": {"protocol_sha256": "1" * 64}}
    with pytest.raises(ValidationError, match="immediately preceding"):
        ProtocolCompilationWrite(
            compilation_sha256="0" * 64,
            quest_id=QUEST,
            action_sha256=ACTION,
            protocol_id="protocol.test",
            protocol_version=2,
            revision_parent_version=2,
            revision_parent_sha256="2" * 64,
            protocol_sha256="1" * 64,
            request_sha256=canonical_sha256(request),
            result_sha256=canonical_sha256(result),
            receipt_sha256=canonical_sha256(result["receipt"]),
            request_json=request,
            result_json=result,
            registered_at=NOW,
        )

    admission_hash, admission_json = _hash_payload("test.admission", "missing-event")
    with pytest.raises(ValidationError, match="incorporation event"):
        ObservationAdmissionWrite(
            committed_admission_sha256=admission_hash,
            decision_sha256="1" * 64,
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=AUTHORIZATION,
            committed_validation_receipt_sha256="2" * 64,
            validation_receipt_sha256="3" * 64,
            issuance_challenge_sha256="4" * 64,
            disposition="admitted",
            admitted_observation_sha256="5" * 64,
            admission_json=admission_json,
            registered_at=NOW,
            committed_at=NOW,
        )

    with pytest.raises(ValidationError, match="Input should be 'admitted'"):
        ObservationAdmissionWrite(
            committed_admission_sha256=admission_hash,
            decision_sha256="1" * 64,
            quest_id=QUEST,
            scientific_slot_id=SLOT,
            authorization_sha256=AUTHORIZATION,
            committed_validation_receipt_sha256="2" * 64,
            validation_receipt_sha256="3" * 64,
            issuance_challenge_sha256="4" * 64,
            disposition="rejected",
            admitted_observation_sha256="5" * 64,
            admission_json=admission_json,
            registered_at=NOW,
            committed_at=NOW,
            incorporated_event_sequence=2,
            incorporated_event_sha256="6" * 64,
            incorporated_event_type="observation_incorporated",
        )
