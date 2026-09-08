"""Opt-in PostgreSQL regression test for the admission incorporation trigger.

The 0027 incorporation-complete trigger compared the Kernel event payload's
``source_world_model_sha256`` against an ``admission_json`` navigation ending
at ``protocol.world_model.world_model_sha256``.  That key is a computed
``canonical_sha256`` property on ``WorldModelSnapshotV2``, not a serialized
field, so pydantic never emits it: the JSON side of the comparison was always
NULL while the payload side is a required digest, and every admission commit
raised ``observation_incorporated event lacks its exact admission row`` at
COMMIT.  The first live ARL-1 admission (generation 20260908o, release
52095d4, 2026-09-08T13:49:44Z) failed closed on exactly this; SQLite fixtures
never saw it because the portable harness has no triggers at all.

Migration 0033 promotes the digest to a real ``source_world_model_sha256``
column and rewrites both trigger branches to compare that column.  This test
reproduces the failure under the captured 0027 trigger function, proves the
absent key was the only defect (hand-injecting the never-serialized key makes
the original trigger pass), and verifies the captured 0033 function commits
the faithful shape while still rejecting a tampered world-model digest.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Register the real referenced tables in Base.metadata so SQLAlchemy can resolve
# every FK while emitting only the isolated observation tables.
import aletheia.execution.persistence  # noqa: F401
import aletheia.jobs.persistence  # noqa: F401
import aletheia.research_store.persistence  # noqa: F401
from aletheia.observations.persistence import OBSERVATION_PERSISTENCE_TABLES
from aletheia.observations.store import (
    ObservationAdmissionWrite,
    ObservationIssuanceChallengeWrite,
    ObservationValidationReceiptWrite,
    ScientificExecutionAuthorizationWrite,
    get_observation_admission_by_slot,
    record_observation_admission,
    record_observation_issuance_challenge,
    record_observation_validation_receipt,
    register_scientific_execution_authorization,
)
from aletheia.research_kernel.schemas import canonical_sha256
from persistence_test_support import _PARENT_STUBS

UTC = timezone.utc
_NOW = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", None})
_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
_INCORPORATION_FUNCTION = "aletheia_observation_incorporation_complete"

# The shared parent stub keeps research_kernel_events at four columns; the
# incorporation trigger reads NEW.event_json, so this harness widens only that
# stub with the payload column the production table carries.
_EVENTS_STUB_WITH_JSON = """
    CREATE TABLE research_kernel_events (
      quest_id varchar(36) NOT NULL,
      sequence bigint NOT NULL,
      event_sha256 varchar(64) PRIMARY KEY,
      event_type varchar(64) NOT NULL,
      event_json jsonb NOT NULL,
      UNIQUE (quest_id, sequence, event_sha256, event_type)
    )
"""


class _RecordingOp:
    """Capture a migration's op.execute SQL without touching any database."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, *args: object, **kwargs: object) -> None:
        self.statements.append(sql)

    def __getattr__(self, name: str):  # every other op.* call is DDL we skip
        return lambda *args, **kwargs: None


def _captured_upgrade_statements(filename: str, needle: str) -> list[str]:
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"_aletheia_captured_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()
    return [statement for statement in recorder.statements if needle in statement]


def _fresh_suffix() -> str:
    return f"{int(datetime.now(UTC).strftime('%H%M%S%f')):032d}"[-32:]


def _sqlstate(integrity_error: IntegrityError) -> str | None:
    # psycopg3 surfaces the SQLSTATE either as pgcode on the error or on its
    # diag, depending on how the driver handed the commit-time failure up.
    original = integrity_error.orig
    return getattr(original, "pgcode", None) or getattr(
        getattr(original, "diag", None), "sqlstate", None
    )


@pytest.fixture(scope="module")
def engine():
    raw_url = os.environ.get("ALETHEIA_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip(
            "incorporation-trigger PostgreSQL regression requires "
            "explicit ALETHEIA_TEST_POSTGRES_URL"
        )
    url = make_url(raw_url)
    if (
        not url.drivername.startswith("postgresql")
        or url.host not in _LOOPBACK_HOSTS
        or url.database is None
        or not url.database.startswith("aletheia_test")
    ):
        pytest.skip(
            "incorporation-trigger PostgreSQL regression requires "
            "loopback aletheia_test* database"
        )
    postgres = create_engine(raw_url, future=True)
    with postgres.begin() as connection:
        for statement in _PARENT_STUBS:
            if "research_kernel_events" in statement:
                continue
            connection.exec_driver_sql(
                statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            )
        connection.exec_driver_sql(
            _EVENTS_STUB_WITH_JSON.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        )
        OBSERVATION_PERSISTENCE_TABLES[0].metadata.create_all(
            bind=connection, tables=OBSERVATION_PERSISTENCE_TABLES, checkfirst=True
        )
        original = _captured_upgrade_statements(
            "20260828_0027_scientific_controller_persistence.py",
            _INCORPORATION_FUNCTION,
        )
        assert len(original) == 1, "expected one captured function-and-triggers statement"
        # The 0027 statement uses plain CREATE FUNCTION / CREATE TRIGGER, so a
        # throwaway database reused across pytest runs needs the prior install
        # dropped first; text() carries the multi-statement DDL without psycopg3
        # client-side %-interpolation (exec_driver_sql chokes on "%ROWTYPE").
        connection.execute(
            text(
                "DROP TRIGGER IF EXISTS trg_roa_incorporation_complete "
                "ON research_observation_admissions"
            )
        )
        connection.execute(
            text(
                "DROP TRIGGER IF EXISTS trg_rke_observation_incorporation_complete "
                "ON research_kernel_events"
            )
        )
        connection.execute(
            text(f"DROP FUNCTION IF EXISTS {_INCORPORATION_FUNCTION}()")
        )
        connection.execute(text(original[0]))
    yield postgres


def _seed_and_commit(
    postgres,
    suffix: str,
    *,
    embed_world_model_path: bool,
    column_world_model: str | None = None,
) -> ObservationAdmissionWrite:
    """Seed the full parent chain and commit one admission, or raise at COMMIT."""

    def sha(seed: str) -> str:
        return canonical_sha256({"scenario": suffix, "seed": seed})

    quest = f"qst_{suffix}"
    slot = f"sos_{suffix}"
    execution = f"exe_{suffix}"
    attempt = f"iat_{suffix}"
    action_object = sha("kernel-object-action")
    qualification = sha("qualification-admission")
    raw_run = sha("raw-run")
    world_model = sha("world-model")
    decision = sha("admission-decision")
    observation = sha("scientific-observation")
    validation_receipt = sha("validation-receipt")
    action_id = f"action:incorporation-pg-{suffix}"
    branch_id = f"rbr_{suffix}"
    outcome = "negative"
    authorized_event = sha("action-authorized-event")
    incorporated_event = sha("observation-incorporated-event")

    authorization_json = {
        "schema_name": "aletheia.scientific_execution_authorization",
        "schema_version": 1,
        "message": {
            "scientific_slot_id": slot,
            "action_protocol_binding": {"action": {"quest_id": quest}},
            "qualification_bundle": {
                "intent": {
                    "execution_id": execution,
                    "infrastructure_attempt": {"infrastructure_attempt_id": attempt},
                }
            },
        },
    }
    authorization = canonical_sha256(authorization_json)

    validation_challenge_json = {
        "schema_name": "aletheia.observation_issuance_challenge",
        "schema_version": 1,
        "message": {
            "scientific_slot_id": slot,
            "nonce_sha256": sha("validation-nonce"),
            "row_scope": f"validation:{slot}",
        },
    }
    validation_challenge = canonical_sha256(validation_challenge_json)

    validation_json = {
        "schema_name": "aletheia.committed_observation_validation_receipt",
        "schema_version": 1,
        "message": {
            "scientific_slot_id": slot,
            "validation_receipt_sha256": validation_receipt,
            "issuance_challenge_sha256": validation_challenge,
            "raw_run_sha256": raw_run,
        },
    }
    committed_validation = canonical_sha256(validation_json)

    admission_challenge_json = {
        "schema_name": "aletheia.observation_issuance_challenge",
        "schema_version": 1,
        "message": {
            "scientific_slot_id": slot,
            "nonce_sha256": sha("admission-nonce"),
            "row_scope": f"admission:{slot}",
            "committed_validation_receipt_sha256": committed_validation,
            "validation_receipt_sha256": validation_receipt,
        },
    }
    admission_challenge = canonical_sha256(admission_challenge_json)

    # The faithful production shape: the world-model snapshot object is present
    # in the serialized authorization, but its sha256 is a computed property and
    # therefore never a JSON key.  The control path hand-injects that key.
    world_model_block: dict[str, object] = {"hypotheses": []}
    if embed_world_model_path:
        world_model_block["world_model_sha256"] = world_model
    admission_json = {
        "schema_name": "aletheia.committed_observation_admission",
        "schema_version": 1,
        "message": {
            "scientific_slot_id": slot,
            "decision_sha256": decision,
            "committed_validation_receipt_sha256": committed_validation,
            "exact_registered_validation_receipt_sha256": validation_receipt,
            "issuance_challenge_sha256": admission_challenge,
            "decision": {
                "message": {
                    "committed_validation_receipt": {
                        "message": {
                            "receipt": {
                                "message": {
                                    "raw_run": {
                                        "scientific_authorization": {
                                            "message": {
                                                "action_protocol_binding": {
                                                    "action": {"action_id": action_id},
                                                    "compilation_request": {
                                                        "protocol": {
                                                            "graph_scope": {
                                                                "branch_id": branch_id
                                                            },
                                                            "world_model": world_model_block,
                                                        }
                                                    },
                                                }
                                            }
                                        }
                                    },
                                    "outcome": outcome,
                                }
                            }
                        }
                    }
                }
            },
        },
    }
    committed_admission = canonical_sha256(admission_json)

    payload = {
        "scientific_slot_id": slot,
        "committed_admission_sha256": committed_admission,
        "scientific_observation_sha256": observation,
        "source_world_model_sha256": world_model,
        "action_id": action_id,
        "branch_id": branch_id,
        "outcome": outcome,
    }

    with Session(postgres) as session:
        session.execute(
            text("INSERT INTO research_quest_streams (quest_id) VALUES (:quest)"),
            {"quest": quest},
        )
        session.execute(
            text("INSERT INTO research_kernel_objects (object_sha256) VALUES (:action)"),
            {"action": action_object},
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_admissions (admission_sha256) "
                "VALUES (:qualification)"
            ),
            {"qualification": qualification},
        )
        session.execute(
            text(
                "INSERT INTO research_kernel_events "
                "(quest_id, sequence, event_sha256, event_type, event_json) "
                "VALUES (:quest, 1, :event, 'action_authorized', CAST(:json AS jsonb))"
            ),
            {"quest": quest, "event": authorized_event, "json": json.dumps({})},
        )
        session.execute(
            text(
                "INSERT INTO research_kernel_events "
                "(quest_id, sequence, event_sha256, event_type, event_json) "
                "VALUES (:quest, 2, :event, 'observation_incorporated', "
                "CAST(:json AS jsonb))"
            ),
            {"quest": quest, "event": incorporated_event, "json": json.dumps({"payload": payload})},
        )
        register_scientific_execution_authorization(
            session,
            ScientificExecutionAuthorizationWrite(
                authorization_sha256=authorization,
                quest_id=quest,
                scientific_slot_id=slot,
                action_sha256=action_object,
                execution_id=execution,
                attempt_id=attempt,
                source_event_sequence=1,
                source_event_sha256=authorized_event,
                qualification_bundle_sha256=sha("qualification-bundle"),
                qualification_grant_sha256=sha("qualification-grant"),
                authorization_json=authorization_json,
                authorized_at=_NOW,
                registered_at=_NOW,
                expires_at=_NOW + timedelta(hours=1),
                observation_admission_deadline=_NOW + timedelta(hours=2),
            ),
        )
        record_observation_issuance_challenge(
            session,
            ObservationIssuanceChallengeWrite(
                challenge_sha256=validation_challenge,
                purpose="validation",
                quest_id=quest,
                scientific_slot_id=slot,
                authorization_sha256=authorization,
                nonce_sha256=sha("validation-nonce"),
                row_scope=f"validation:{slot}",
                raw_run_sha256=raw_run,
                database_authority_policy_sha256=sha("authority-policy"),
                issued_by_principal_id="database:authority",
                issuance_key_id=sha("issuance-key"),
                challenge_json=validation_challenge_json,
                issued_at=_NOW + timedelta(minutes=1),
                recorded_at=_NOW + timedelta(minutes=1),
                expires_at=_NOW + timedelta(minutes=5),
                observation_admission_deadline=_NOW + timedelta(hours=2),
            ),
        )
        record_observation_validation_receipt(
            session,
            ObservationValidationReceiptWrite(
                committed_receipt_sha256=committed_validation,
                validation_receipt_sha256=validation_receipt,
                quest_id=quest,
                scientific_slot_id=slot,
                authorization_sha256=authorization,
                qualification_admission_sha256=qualification,
                raw_run_sha256=raw_run,
                issuance_challenge_sha256=validation_challenge,
                disposition="validated_confirmation",
                outcome=outcome,
                scientific_observation_sha256=observation,
                committed_receipt_json=validation_json,
                validated_at=_NOW + timedelta(minutes=2),
                registered_at=_NOW + timedelta(minutes=2),
                committed_at=_NOW + timedelta(minutes=2),
            ),
        )
        record_observation_issuance_challenge(
            session,
            ObservationIssuanceChallengeWrite(
                challenge_sha256=admission_challenge,
                purpose="admission",
                quest_id=quest,
                scientific_slot_id=slot,
                authorization_sha256=authorization,
                nonce_sha256=sha("admission-nonce"),
                row_scope=f"admission:{slot}",
                committed_validation_receipt_sha256=committed_validation,
                validation_receipt_sha256=validation_receipt,
                database_authority_policy_sha256=sha("authority-policy"),
                issued_by_principal_id="database:authority",
                issuance_key_id=sha("issuance-key"),
                challenge_json=admission_challenge_json,
                issued_at=_NOW + timedelta(minutes=3),
                recorded_at=_NOW + timedelta(minutes=3),
                expires_at=_NOW + timedelta(minutes=6),
                observation_admission_deadline=_NOW + timedelta(hours=2),
            ),
        )
        admission = ObservationAdmissionWrite(
            committed_admission_sha256=committed_admission,
            decision_sha256=decision,
            quest_id=quest,
            scientific_slot_id=slot,
            authorization_sha256=authorization,
            committed_validation_receipt_sha256=committed_validation,
            validation_receipt_sha256=validation_receipt,
            issuance_challenge_sha256=admission_challenge,
            disposition="admitted",
            admitted_observation_sha256=observation,
            source_world_model_sha256=(column_world_model or world_model),
            admission_json=admission_json,
            registered_at=_NOW + timedelta(minutes=4),
            committed_at=_NOW + timedelta(minutes=4),
            incorporated_event_sequence=2,
            incorporated_event_sha256=incorporated_event,
            incorporated_event_type="observation_incorporated",
        )
        record_observation_admission(session, admission)
        session.commit()
    return admission


def test_faithful_admission_rolls_back_under_original_trigger(engine) -> None:
    suffix = _fresh_suffix()

    with pytest.raises(IntegrityError, match="lacks its exact") as failure:
        _seed_and_commit(engine, suffix, embed_world_model_path=False)
    assert _sqlstate(failure.value) == "23514"

    with Session(engine) as session:
        assert (
            session.execute(
                text(
                    "SELECT count(*) FROM research_observation_admissions "
                    "WHERE quest_id = :quest"
                ),
                {"quest": f"qst_{suffix}"},
            ).scalar_one()
            == 0
        ), "failed admission commit must roll back the whole transaction"


def test_injected_world_model_path_satisfies_original_trigger(engine) -> None:
    # Control pinning the root cause: hand-injecting the one key pydantic never
    # serializes is the only change needed for the 0027 trigger to accept the
    # otherwise-identical pair.
    admission = _seed_and_commit(engine, _fresh_suffix(), embed_world_model_path=True)

    with Session(engine) as session:
        assert (
            get_observation_admission_by_slot(
                session,
                quest_id=admission.quest_id,
                scientific_slot_id=admission.scientific_slot_id,
            )
            == admission
        )


def test_fixed_trigger_commits_faithful_admission_and_rejects_tampering(engine) -> None:
    fixed = _captured_upgrade_statements(
        "20260909_0033_observation_admission_world_model_sha.py",
        f"CREATE OR REPLACE FUNCTION {_INCORPORATION_FUNCTION}",
    )
    assert len(fixed) == 1, "expected one captured replacement function statement"
    with engine.begin() as connection:
        connection.execute(text(fixed[0]))

    admitted = _seed_and_commit(engine, _fresh_suffix(), embed_world_model_path=False)
    with Session(engine) as session:
        assert (
            get_observation_admission_by_slot(
                session,
                quest_id=admitted.quest_id,
                scientific_slot_id=admitted.scientific_slot_id,
            )
            == admitted
        )

    suffix = _fresh_suffix()
    with pytest.raises(IntegrityError, match="lacks its exact") as failure:
        _seed_and_commit(
            engine,
            suffix,
            embed_world_model_path=False,
            column_world_model=canonical_sha256(
                {"scenario": suffix, "seed": "tampered-world-model"}
            ),
        )
    assert _sqlstate(failure.value) == "23514"
