"""Database-independent tests for the public qualification-terminal outbox projection."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from aletheia.execution.allocator import (
    AdmissionConflict,
    PostgreSQLExecutionAllocator,
    QualificationTerminalOutboxItem,
    _qualification_terminal_outbox_item,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    QualificationTerminalDeadlineExpiration,
)

_NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _accepted(attempt_id: str, label: str) -> AcceptedQualificationTerminalSubmission:
    return AcceptedQualificationTerminalSubmission(
        attempt_id=attempt_id,
        node_manifest_sha256=_digest(f"{label}:node"),
        terminal_submission_sha256=_digest(f"{label}:submission"),
        accepted_runtime_termination_sha256=_digest(f"{label}:termination"),
        artifact_manifest_sha256=_digest(f"{label}:manifest"),
        output_tree_sha256=_digest(f"{label}:tree"),
        artifact_verified_receipt_sha256s=(_digest(f"{label}:artifact"),),
        disposition="process_succeeded",
        node_submitted_at=_NOW,
        artifact_submission_deadline=_NOW + timedelta(minutes=5),
        accepted_at=_NOW + timedelta(seconds=1),
        runtime_control_policy_sha256=_digest(f"{label}:policy"),
        accepted_by_principal_id="principal:runtime-control",
        acceptance_key_id=_digest(f"{label}:key"),
        signature_ed25519_hex="a" * 128,
    )


def _row_and_attempt(*, execution_id: str, attempt_id: str, label: str):
    payload = _accepted(attempt_id, label)
    authority_sha256 = payload.accepted_terminal_submission_sha256
    row = SimpleNamespace(
        outbox_id=f"qto_{authority_sha256}",
        terminal_authority_kind="accepted_terminal_submission",
        terminal_authority_sha256=authority_sha256,
        accepted_terminal_submission_sha256=authority_sha256,
        terminal_deadline_expiration_sha256=None,
        execution_id=execution_id,
        attempt_id=attempt_id,
        topic="execution.qualification_terminal.v2",
        delivery_key=f"execution-v2:{execution_id}:{attempt_id}",
        payload_sha256=authority_sha256,
        payload_json=payload.model_dump(mode="json"),
        created_at=_NOW + timedelta(seconds=2),
    )
    return row, SimpleNamespace(execution_id=execution_id, attempt_id=attempt_id), payload


def _expiration_row_and_attempt(*, execution_id: str, attempt_id: str, label: str):
    deadline = _NOW + timedelta(minutes=5)
    payload = QualificationTerminalDeadlineExpiration(
        attempt_id=attempt_id,
        execution_id=execution_id,
        intent_sha256=_digest(f"{label}:intent"),
        node_id="node:qualification",
        node_manifest_sha256=_digest(f"{label}:node"),
        node_inventory_sha256=_digest(f"{label}:inventory"),
        resource_lease_sha256=_digest(f"{label}:lease"),
        runtime_preparation_sha256=_digest(f"{label}:preparation"),
        runtime_launch_authorization_request_sha256=_digest(f"{label}:request"),
        runtime_launch_authorization_sha256=_digest(f"{label}:authorization"),
        node_runtime_launch_receipt_sha256=_digest(f"{label}:launch"),
        runtime_termination_challenge_sha256=_digest(f"{label}:challenge"),
        node_runtime_termination_receipt_sha256=_digest(f"{label}:termination-receipt"),
        accepted_runtime_termination_sha256=_digest(f"{label}:termination"),
        runtime_identity_sha256=_digest(f"{label}:runtime"),
        runtime_inspection_evidence_sha256=_digest(f"{label}:inspection"),
        engine_terminal_journal_sha256=_digest(f"{label}:journal"),
        inspection_sequence=1,
        fencing_epoch=1,
        lease_token_sha256=_digest(f"{label}:token"),
        runtime_ended_at=_NOW,
        exit_code=0,
        hard_deadline=_NOW + timedelta(minutes=1),
        artifact_submission_deadline=deadline,
        accepted_runtime_termination_at=_NOW + timedelta(seconds=1),
        authorized_at=_NOW + timedelta(seconds=1),
        expired_at=deadline,
        runtime_control_policy_sha256=_digest(f"{label}:policy"),
        adjudicated_by_principal_id="principal:runtime-control",
        adjudication_key_id=_digest(f"{label}:key"),
        signature_ed25519_hex="b" * 128,
    )
    authority_sha256 = payload.terminal_deadline_expiration_sha256
    row = SimpleNamespace(
        outbox_id=f"qto_{authority_sha256}",
        terminal_authority_kind="terminal_deadline_expiration",
        terminal_authority_sha256=authority_sha256,
        accepted_terminal_submission_sha256=None,
        terminal_deadline_expiration_sha256=authority_sha256,
        execution_id=execution_id,
        attempt_id=attempt_id,
        topic="execution.qualification_terminal.v2",
        delivery_key=f"execution-v2:{execution_id}:{attempt_id}",
        payload_sha256=authority_sha256,
        payload_json=payload.model_dump(mode="json"),
        created_at=deadline + timedelta(seconds=1),
    )
    return row, SimpleNamespace(execution_id=execution_id, attempt_id=attempt_id), payload


class _Result:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, object]]:
        return self._rows

    def one_or_none(self) -> tuple[object, object] | None:
        if len(self._rows) > 1:
            raise AssertionError("fake query unexpectedly returned multiple rows")
        return self._rows[0] if self._rows else None


class _RowsSession(Session):
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self.rows = rows

    def execute(self, _statement: object) -> _Result:
        return _Result(self.rows)


def _allocator() -> PostgreSQLExecutionAllocator:
    return object.__new__(PostgreSQLExecutionAllocator)


def test_public_terminal_outbox_projection_is_frozen_and_exact() -> None:
    execution_id = "exe_" + "1" * 32
    attempt_id = "iat_" + "2" * 32
    row, attempt, payload = _row_and_attempt(
        execution_id=execution_id,
        attempt_id=attempt_id,
        label="exact",
    )

    item = _qualification_terminal_outbox_item(row, attempt)

    assert isinstance(item, QualificationTerminalOutboxItem)
    assert item.outbox_id == row.outbox_id
    assert item.terminal_authority_sha256 == payload.accepted_terminal_submission_sha256
    assert item.execution_id == execution_id
    assert item.attempt_id == attempt_id
    assert item.payload == payload
    assert item.payload_sha256 == row.payload_sha256
    assert item.created_at == row.created_at
    with pytest.raises(ValidationError, match="frozen"):
        item.execution_id = "exe_" + "3" * 32


def test_deadline_expiration_outbox_projection_preserves_typed_authority() -> None:
    row, attempt, payload = _expiration_row_and_attempt(
        execution_id="exe_" + "9" * 32,
        attempt_id="iat_" + "a" * 32,
        label="expiration",
    )

    item = _qualification_terminal_outbox_item(row, attempt)

    assert item.terminal_authority_kind == "terminal_deadline_expiration"
    assert item.payload == payload
    assert item.payload.execution_id == item.execution_id
    assert item.payload_sha256 == payload.terminal_deadline_expiration_sha256


@pytest.mark.parametrize(
    "tamper",
    ("payload", "variant", "execution", "routing"),
)
def test_terminal_outbox_projection_rejects_rebound_rows(tamper: str) -> None:
    row, attempt, _payload = _row_and_attempt(
        execution_id="exe_" + "3" * 32,
        attempt_id="iat_" + "4" * 32,
        label=f"tamper:{tamper}",
    )
    if tamper == "payload":
        row.payload_json = {
            **row.payload_json,
            "accepted_by_principal_id": "principal:rebound",
        }
    elif tamper == "variant":
        row.accepted_terminal_submission_sha256 = None
    elif tamper == "execution":
        attempt.execution_id = "exe_" + "5" * 32
    else:
        row.delivery_key = "execution-v2:rebound"

    with pytest.raises(AdmissionConflict, match="outbox"):
        _qualification_terminal_outbox_item(row, attempt)


def test_caller_owned_allowlist_read_is_canonically_ordered() -> None:
    first = _row_and_attempt(
        execution_id="exe_" + "1" * 32,
        attempt_id="iat_" + "2" * 32,
        label="first",
    )
    second = _row_and_attempt(
        execution_id="exe_" + "2" * 32,
        attempt_id="iat_" + "1" * 32,
        label="second",
    )
    session = _RowsSession([(second[0], second[1]), (first[0], first[1])])

    items = _allocator().list_qualification_terminal_outbox_in_session(
        session,
        execution_id_allowlist=(first[0].execution_id, second[0].execution_id),
    )

    assert tuple((item.execution_id, item.attempt_id) for item in items) == (
        (first[0].execution_id, first[0].attempt_id),
        (second[0].execution_id, second[0].attempt_id),
    )


def test_allowlist_read_rejects_broad_or_noncanonical_scope() -> None:
    allocator = _allocator()
    session = _RowsSession([])

    with pytest.raises(ValueError, match="require an execution or attempt allowlist"):
        allocator.list_qualification_terminal_outbox_in_session(session)
    with pytest.raises(ValueError, match="canonically ordered"):
        allocator.list_qualification_terminal_outbox_in_session(
            session,
            execution_id_allowlist=("exe_" + "2" * 32, "exe_" + "1" * 32),
        )
    with pytest.raises(TypeError, match="must be a tuple"):
        allocator.list_qualification_terminal_outbox_in_session(
            session,
            attempt_id_allowlist=["iat_" + "1" * 32],  # type: ignore[arg-type]
        )


def test_exact_execution_attempt_read_fails_closed_on_cross_execution_binding() -> None:
    row, attempt, _payload = _row_and_attempt(
        execution_id="exe_" + "6" * 32,
        attempt_id="iat_" + "7" * 32,
        label="exact-read",
    )
    allocator = _allocator()

    item = allocator.load_qualification_terminal_outbox_in_session(
        _RowsSession([(row, attempt)]),
        execution_id=row.execution_id,
        attempt_id=row.attempt_id,
    )
    assert item is not None and item.outbox_id == row.outbox_id

    with pytest.raises(AdmissionConflict, match="execution-attempt pair"):
        allocator.load_qualification_terminal_outbox_in_session(
            _RowsSession([(row, attempt)]),
            execution_id="exe_" + "8" * 32,
            attempt_id=row.attempt_id,
        )
    assert (
        allocator.load_qualification_terminal_outbox_in_session(
            _RowsSession([]),
            execution_id=row.execution_id,
            attempt_id=row.attempt_id,
        )
        is None
    )
