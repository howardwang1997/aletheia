"""Opt-in PostgreSQL regression test for the external placement authority guard.

Round 2 of PR #188 shipped a catalog-membership clause that referenced only
the intent JSON, and round 3 rewrote it to evaluate every envelope predicate
on the catalog row matched by ``class_key``.  Both defects were CI-blind:
the allocator suite that drives this trigger is gated on the loopback pr4
scratch databases and skips in CI, and the alembic steps apply the migration
without ever firing the rewritten trigger — exactly the failure class that
shipped the round-2 bug twice.  This test commits bypassed-writer-shaped
rows directly against a fully migrated schema so the database-level guard
is exercised in CI: the honest external placement commits, a catalog-absent
class key is rejected at COMMIT, a lease/attempt key mirror mismatch is
rejected, and the placement-mode CHECK arms reject half-external rows at
INSERT.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aletheia.db import expected_schema_revision

UTC = timezone.utc
_NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
_LEASE_SECONDS = 600
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", None})


@pytest.fixture(scope="module")
def migrated_engine():
    unavailable = (
        pytest.fail if os.environ.get("ALETHEIA_REQUIRE_POSTGRES_TESTS") == "1" else pytest.skip
    )
    raw_url = os.environ.get("ALETHEIA_TEST_MIGRATED_POSTGRES_URL")
    if not raw_url:
        unavailable(
            "placement-trigger regression requires explicit ALETHEIA_TEST_MIGRATED_POSTGRES_URL"
        )
    url = make_url(raw_url)
    if (
        not url.drivername.startswith("postgresql")
        or url.host not in _LOOPBACK_HOSTS
        or url.database is None
        or not url.database.startswith("aletheia_test")
    ):
        unavailable(
            "placement-trigger regression requires a loopback aletheia_test* migrated database"
        )
    engine = create_engine(raw_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == expected_schema_revision()
            )
        yield engine
    finally:
        engine.dispose()


def _suffix() -> str:
    return f"{int(datetime.now(UTC).strftime('%H%M%S%f')):032d}"[-32:]


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _seed_external_placement(
    engine,
    suffix: str,
    *,
    committed_class_key: str,
    catalog_class_key: str,
    lease_key_mirror: str | None = None,
    lease_json_key_mirror: str | None = None,
    quote_key_in_bundle: str | None = None,
    attempt_key: str | None = None,
) -> None:
    """Commit one bypassed-writer-shaped external placement, or raise at COMMIT.

    Every value is self-authored (no models, no allocator): this is the
    writer the frozen guards exist for.  The knobs vary exactly one mirror
    or one catalog binding per call so each guard clause fails in isolation.
    """

    def sha(seed: str) -> str:
        return _sha(f"{suffix}:{seed}")

    quest = f"qst_{suffix}"
    execution = f"exe_{suffix}"
    attempt_id = f"iat_{suffix}"
    admission = sha("admission")
    grant = sha("grant")
    bundle_sha = sha("bundle")
    quote_sha = sha("quote")
    intent_sha = sha("intent")
    authorization = sha("budget-authorization")
    reservation_id = f"rvn_{suffix}"
    lease_id = f"lse_{suffix}"
    class_id = f"rsc_{suffix[:32]}"
    action_kind = "run_cuprate_diagnostic"
    attempt_key = attempt_key if attempt_key is not None else committed_class_key
    lease_key = lease_key_mirror if lease_key_mirror is not None else attempt_key
    lease_json_key = lease_json_key_mirror if lease_json_key_mirror is not None else attempt_key
    quote_key = quote_key_in_bundle if quote_key_in_bundle is not None else attempt_key

    authorized_at = _NOW
    reserved_at = _NOW + timedelta(minutes=1)
    hard_deadline = reserved_at + timedelta(seconds=_LEASE_SECONDS)
    window_end = hard_deadline + timedelta(hours=1)

    bundle_json = {
        "intent": {
            "execution_id": execution,
            "external_action_kind": action_kind,
            "deadline": _iso(window_end),
            "resource_request": {
                "cpu_cores": 2,
                "memory_bytes": 8_000_000_000,
                "scratch_bytes": 4_000_000_000,
                "exclusive": False,
                "accelerator_count": 0,
                "required_features": ["feature.bridge"],
                "accepted_resource_class_ids": [class_id],
            },
        },
        "cost_quote": {
            "currency_code": "USD",
            "fixed_charge_microunits": 100,
            "charge_per_second_microunits": 5,
            "maximum_lease_seconds": _LEASE_SECONDS,
            "maximum_charge_microunits": 100 + 5 * _LEASE_SECONDS,
            "expires_at": _iso(window_end),
            "selected_external_resource_class_id": class_id,
            "selected_external_resource_class_key": quote_key,
            "selected_node_manifest_sha256": None,
            "selected_resource_ids": [],
        },
        "budget_authorization": {"expires_at": _iso(window_end)},
        "compilation_request": {
            "resource_catalog": {
                "resource_classes": [
                    {
                        # the serialized catalog shape: the derived
                        # resource_class_id never appears in JSON
                        "class_key": catalog_class_key,
                        "kind": "external",
                        "external_action_kinds": [action_kind],
                        "network_policies": ["none"],
                        "cpu_cores": 8,
                        "memory_bytes": 64_000_000_000,
                        "scratch_bytes": 32_000_000_000,
                        "features": ["feature.bridge"],
                    }
                ]
            }
        },
    }
    grant_json = {"message": {"expires_at": _iso(window_end)}}
    lease_json = {
        "execution_id": execution,
        "attempt_id": attempt_id,
        "intent_sha256": intent_sha,
        "external_resource_class_id": class_id,
        "external_resource_class_key": lease_json_key,
        "selected_resource_ids": [],
        "fencing_epoch_at_acquisition": 1,
        "acquired_at": _iso(reserved_at),
        "hard_deadline": _iso(hard_deadline),
    }
    event_payload = {
        "reservation_id": reservation_id,
        "authorization_sha256": authorization,
        "sequence": 1,
        "previous_event_sha256": None,
        "event_type": "reserved",
        "reserved_delta_microunits": 100 + 5 * _LEASE_SECONDS,
        "spent_delta_microunits": 0,
        "recorded_at": _iso(reserved_at),
    }

    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_budget_authorizations"
                " (authorization_sha256, quest_id, protocol_sha256, work_order_sha256,"
                "  resource_budget_sha256, source_budget_authorization_sha256,"
                "  currency_code, cap_microunits, authorized_at, expires_at,"
                "  authorized_by_principal_id, payload_sha256, payload_json, registered_at)"
                " VALUES (:a, :quest, :p, :w, :rb, :sb, 'USD', 1000000,"
                "         :authorized, :expires, 'principal:test', :ps,"
                "         CAST(:payload AS jsonb), :authorized)"
            ),
            {
                "a": authorization,
                "quest": quest,
                "p": sha("protocol"),
                "w": sha("work-order"),
                "rb": sha("resource-budget"),
                "sb": sha("source-budget"),
                "authorized": _iso(authorized_at),
                "expires": _iso(window_end),
                "ps": sha("authorization-payload"),
                "payload": json.dumps({"authorization_sha256": authorization}),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_budget_heads"
                " (authorization_sha256, currency_code, cap_microunits,"
                "  reserved_microunits, spent_microunits, state_version, updated_at)"
                " VALUES (:a, 'USD', 1000000, :held, 0, 1, :authorized)"
            ),
            {"a": authorization, "held": 100 + 5 * _LEASE_SECONDS, "authorized": _iso(reserved_at)},
        )
        session.execute(
            text(
                "INSERT INTO execution_heads"
                " (execution_id, quest_id, protocol_sha256, work_order_id, work_order_sha256,"
                "  replicate_slot_id, replicate_slot_sha256, last_attempt_number,"
                "  active_attempt_id, state_version, created_at, updated_at)"
                " VALUES (:e, :quest, :p, :wo, :ws, :slot, :slot_sha, 1, :attempt, 1,"
                "         :authorized, :authorized)"
            ),
            {
                "e": execution,
                "quest": quest,
                "p": sha("protocol"),
                "wo": f"word_{suffix[:27]}",
                "ws": sha("work-order"),
                "slot": f"sos_{suffix[:29]}",
                "slot_sha": sha("slot"),
                "attempt": attempt_id,
                "authorized": _iso(authorized_at),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_qualification_admissions"
                " (admission_sha256, grant_sha256, bundle_sha256, intent_sha256,"
                "  execution_id, infrastructure_attempt_id, budget_authorization_sha256,"
                "  cost_quote_sha256, authority_policy_sha256, authority_key_id,"
                "  bundle_json, grant_json, verified_receipt_json, verified_at, admitted_at)"
                " VALUES (:a, :grant, :bundle, :intent, :e, :attempt, :auth, :quote,"
                "         :policy, :authority_key, CAST(:bundle_json AS jsonb),"
                "         CAST(:grant_json AS jsonb), CAST('{}' AS jsonb),"
                "         :authorized, :authorized)"
            ),
            {
                "a": admission,
                "grant": grant,
                "bundle": bundle_sha,
                "intent": intent_sha,
                "e": execution,
                "attempt": attempt_id,
                "auth": authorization,
                "quote": quote_sha,
                "policy": sha("policy"),
                # the frozen CHECK pins authority_key_id to the 64-hex shape
                "authority_key": sha("authority-key"),
                "bundle_json": json.dumps(bundle_json),
                "grant_json": json.dumps(grant_json),
                "authorized": _iso(authorized_at),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_attempts"
                " (attempt_id, execution_id, attempt_number, intent_sha256, intent_json,"
                "  admission_sha256, grant_sha256, bundle_sha256, cost_quote_sha256,"
                "  node_id, node_inventory_sha256, external_resource_class_id,"
                "  external_resource_class_key, status, state_version, fencing_epoch,"
                "  lease_token_sha256, adoption_count, last_runtime_inspection_sequence,"
                "  authorized_at, reserved_at, heartbeat_at, lease_expires_at,"
                "  hard_deadline, runtime_launch_authorization_count,"
                "  pre_runtime_absence_count, runtime_termination_challenge_count,"
                "  updated_at)"
                " VALUES (:attempt, :e, 1, :intent, CAST('{}' AS jsonb), :a, :grant,"
                "         :bundle, :quote, NULL, NULL, :class_id, :class_key,"
                "         'reserved', 1, 1, :token, 0, 0, :authorized, :reserved,"
                "         :reserved, :hard, :hard, 0, 0, 0, :reserved)"
            ),
            {
                "attempt": attempt_id,
                "e": execution,
                "intent": intent_sha,
                "a": admission,
                "grant": grant,
                "bundle": bundle_sha,
                "quote": quote_sha,
                "class_id": class_id,
                "class_key": attempt_key,
                "token": sha("lease-token"),
                "authorized": _iso(authorized_at),
                "reserved": _iso(reserved_at),
                "hard": _iso(hard_deadline),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_resource_leases"
                " (lease_id, attempt_id, node_id, inventory_sha256,"
                "  external_resource_class_id, external_resource_class_key, lease_sha256,"
                "  lease_json, state, fencing_epoch, cpu_cores, memory_bytes,"
                "  scratch_bytes, exclusive, accelerator_count, acquired_at,"
                "  heartbeat_at, lease_expires_at)"
                " VALUES (:lease, :attempt, NULL, NULL, :class_id, :class_key, :lease_sha,"
                "         CAST(:lease_json AS jsonb), 'held', 1, 2, 8000000000,"
                "         4000000000, false, 0, :reserved, :reserved, :hard)"
            ),
            {
                "lease": lease_id,
                "attempt": attempt_id,
                "class_id": class_id,
                "class_key": lease_key,
                "lease_sha": sha("lease"),
                "lease_json": json.dumps(lease_json),
                "reserved": _iso(reserved_at),
                "hard": _iso(hard_deadline),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_budget_reservations"
                " (reservation_id, authorization_sha256, attempt_id, execution_id,"
                "  cost_quote_sha256, currency_code, fixed_charge_microunits,"
                "  charge_per_second_microunits, maximum_lease_seconds, held_microunits,"
                "  settled_microunits, state, reserved_at)"
                " VALUES (:reservation, :auth, :attempt, :e, :quote, 'USD', 100, 5,"
                "         :seconds, :held, 0, 'held', :reserved)"
            ),
            {
                "reservation": reservation_id,
                "auth": authorization,
                "attempt": attempt_id,
                "e": execution,
                "quote": quote_sha,
                "seconds": _LEASE_SECONDS,
                "held": 100 + 5 * _LEASE_SECONDS,
                "reserved": _iso(reserved_at),
            },
        )
        session.execute(
            text(
                "INSERT INTO execution_budget_events"
                " (event_id, event_sha256, reservation_id, authorization_sha256,"
                "  sequence, previous_event_sha256, event_type, reserved_delta_microunits,"
                "  spent_delta_microunits, payload_sha256, payload_json, recorded_at)"
                " VALUES (:event, :event_sha, :reservation, :auth, 1, NULL, 'reserved',"
                "         :held, 0, :payload_sha, CAST(:payload AS jsonb), :reserved)"
            ),
            {
                "event": f"bev_{suffix[:29]}",
                "event_sha": sha("budget-event"),
                "reservation": reservation_id,
                "auth": authorization,
                "held": 100 + 5 * _LEASE_SECONDS,
                "payload_sha": sha("budget-event-payload"),
                "payload": json.dumps(event_payload),
                "reserved": _iso(reserved_at),
            },
        )
        session.commit()


def test_honest_external_placement_commits_through_the_frozen_guard(migrated_engine) -> None:
    suffix = _suffix()
    _seed_external_placement(
        migrated_engine,
        suffix,
        committed_class_key="bridge.placement-trigger-honest",
        catalog_class_key="bridge.placement-trigger-honest",
    )
    with Session(migrated_engine) as session:
        state = session.execute(
            text(
                "SELECT a.status, l.state, r.state FROM execution_attempts a"
                "  JOIN execution_resource_leases l ON l.attempt_id = a.attempt_id"
                "  JOIN execution_budget_reservations r ON r.attempt_id = a.attempt_id"
                " WHERE a.attempt_id = :attempt"
            ),
            {"attempt": f"iat_{suffix}"},
        ).one()
        assert state == ("reserved", "held", "held")


def test_catalog_absent_class_key_is_rejected_at_commit(migrated_engine) -> None:
    # the round-2 regression shape: mirrors internally consistent, the intent
    # accepts the committed class id, but no catalog row carries the committed
    # class_key, so the envelope has nothing to evaluate against
    suffix = _suffix()
    with pytest.raises(IntegrityError, match="absent from the frozen catalog") as failure:
        _seed_external_placement(
            migrated_engine,
            suffix,
            committed_class_key="bridge.ghost-key",
            catalog_class_key="bridge.placement-trigger-honest",
        )
    assert _sqlstate(failure.value) == "23514"
    _assert_attempt_rolled_back(migrated_engine, suffix)


def test_lease_attempt_class_key_mirror_mismatch_is_rejected_at_commit(migrated_engine) -> None:
    suffix = _suffix()
    with pytest.raises(IntegrityError, match="differs from exact intent/quote payload") as failure:
        _seed_external_placement(
            migrated_engine,
            suffix,
            committed_class_key="bridge.placement-trigger-honest",
            catalog_class_key="bridge.placement-trigger-honest",
            lease_key_mirror="bridge.other-lease-key",
        )
    assert _sqlstate(failure.value) == "23514"
    _assert_attempt_rolled_back(migrated_engine, suffix)


def test_quote_bundle_class_key_disagreement_is_rejected_at_commit(migrated_engine) -> None:
    # the admission's serialized quote names a different key than the rows
    # committed under: E1's quote-agreement conjunct must fail closed
    suffix = _suffix()
    with pytest.raises(IntegrityError, match="differs from exact intent/quote payload") as failure:
        _seed_external_placement(
            migrated_engine,
            suffix,
            committed_class_key="bridge.placement-trigger-honest",
            catalog_class_key="bridge.placement-trigger-honest",
            quote_key_in_bundle="bridge.other-quote-key",
        )
    assert _sqlstate(failure.value) == "23514"
    _assert_attempt_rolled_back(migrated_engine, suffix)


def test_half_external_rows_violate_the_placement_mode_check(migrated_engine) -> None:
    # a nodeless attempt carrying the class id but not the key fails the
    # CHECK at INSERT, before any deferred guard runs
    suffix = _suffix()

    def sha(seed: str) -> str:
        return _sha(f"{suffix}:{seed}")

    with Session(migrated_engine) as session:
        session.execute(
            text(
                "INSERT INTO execution_qualification_admissions"
                " (admission_sha256, grant_sha256, bundle_sha256, intent_sha256,"
                "  execution_id, infrastructure_attempt_id, budget_authorization_sha256,"
                "  cost_quote_sha256, authority_policy_sha256, authority_key_id,"
                "  bundle_json, grant_json, verified_receipt_json, verified_at, admitted_at)"
                " VALUES (:a, :grant, :bundle, :intent, :e, :attempt, :auth, :quote,"
                "         :policy, :authority_key, CAST('{}' AS jsonb),"
                "         CAST('{}' AS jsonb), CAST('{}' AS jsonb), :now, :now)"
            ),
            {
                "a": sha("admission"),
                "grant": sha("grant"),
                "bundle": sha("bundle"),
                "intent": sha("intent"),
                "e": f"exe_{suffix}",
                "attempt": f"iat_{suffix}",
                "auth": sha("budget-authorization"),
                "quote": sha("quote"),
                "policy": sha("policy"),
                "authority_key": sha("authority-key"),
                "now": _iso(_NOW),
            },
        )
        with pytest.raises(IntegrityError, match="ck_execution_attempts_placement_mode"):
            session.execute(
                text(
                    "INSERT INTO execution_attempts"
                    " (attempt_id, execution_id, attempt_number, intent_sha256,"
                    "  intent_json, admission_sha256, grant_sha256, bundle_sha256,"
                    "  cost_quote_sha256, node_id, node_inventory_sha256,"
                    "  external_resource_class_id, external_resource_class_key,"
                    "  status, state_version, fencing_epoch, lease_token_sha256,"
                    "  adoption_count, last_runtime_inspection_sequence, authorized_at,"
                    "  reserved_at, heartbeat_at, lease_expires_at, hard_deadline,"
                    "  runtime_launch_authorization_count, pre_runtime_absence_count,"
                    "  runtime_termination_challenge_count, updated_at)"
                    " VALUES (:attempt, :e, 1, :intent, CAST('{}' AS jsonb), :a, :grant,"
                    "         :bundle, :quote, NULL, NULL, :class_id, NULL, 'reserved',"
                    "         1, 1, :token, 0, 0, :now, :now, :now, :now, :now,"
                    "         0, 0, 0, :now)"
                ),
                {
                    "attempt": f"iat_{suffix}",
                    "e": f"exe_{suffix}",
                    "intent": sha("intent"),
                    "a": sha("admission"),
                    "grant": sha("grant"),
                    "bundle": sha("bundle"),
                    "quote": sha("quote"),
                    "class_id": f"rsc_{suffix[:32]}",
                    "token": sha("lease-token"),
                    "now": _iso(_NOW),
                },
            )
        session.rollback()


def _sqlstate(integrity_error: IntegrityError) -> str | None:
    original = integrity_error.orig
    return getattr(original, "pgcode", None) or getattr(
        getattr(original, "diag", None), "sqlstate", None
    )


def _assert_attempt_rolled_back(engine, suffix: str) -> None:
    with Session(engine) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM execution_attempts WHERE attempt_id = :attempt"),
                {"attempt": f"iat_{suffix}"},
            ).scalar_one()
            == 0
        ), "failed placement commit must roll back the whole transaction"
