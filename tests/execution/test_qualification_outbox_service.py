from __future__ import annotations

import hashlib
import os
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import aletheia.qualification_service_runtime as service_runtime
import aletheia.execution.qualification_outbox_service as outbox_service
from aletheia.execution.qualification_outbox_composition import build_outbox_service
from aletheia.execution.qualification_outbox_service import (
    QualificationOutboxSpoolRootPinV1,
    QualificationTerminalOutboxError,
    QualificationTerminalOutboxService,
    QualificationTerminalOutboxServiceConfigV1,
    QualificationTerminalOutboxSpool,
    QualificationTerminalSpoolEnvelopeV1,
    _legacy_envelope,
    _qualification_envelope,
)
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    QualificationTerminalDeadlineExpiration,
)
from aletheia.execution.schemas import (
    ExecutionFailure,
    ExecutionFailureCategory,
    ExecutionReceipt,
    ExecutionTerminalState,
    canonical_json_bytes,
    canonical_sha256,
)

_EXECUTION_TESTS = Path(__file__).resolve().parent
if str(_EXECUTION_TESTS) not in sys.path:
    sys.path.insert(0, str(_EXECUTION_TESTS))

from test_runtime_contracts import _qualification_case  # noqa: E402

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _legacy_row(*, status: str = "pending") -> dict[str, object]:
    intent = _qualification_case().bundle.intent
    started_at = NOW
    ended_at = NOW + timedelta(seconds=1)
    verified_at = NOW + timedelta(seconds=2)
    receipt = ExecutionReceipt(
        intent=intent,
        worker_node_manifest_sha256=_digest("legacy:node"),
        node_inventory_sha256=_digest("legacy:inventory"),
        resource_lease_sha256=_digest("legacy:lease"),
        node_execution_receipt_sha256=_digest("legacy:node-receipt"),
        started_at=started_at,
        observed_at=verified_at,
        ended_at=ended_at,
        terminal_state=ExecutionTerminalState.EXECUTION_FAILED,
        failure=ExecutionFailure(
            category=ExecutionFailureCategory.INFRASTRUCTURE,
            detail_sha256=_digest("legacy:failure"),
        ),
        verified_by_principal_id="principal:execution-verifier",
        verified_at=verified_at,
    )
    receipt_sha256 = receipt.execution_receipt_sha256
    execution_id = intent.execution_id
    attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
    return {
        "outbox_id": f"xob_{canonical_sha256({'receipt_sha256': receipt_sha256})}",
        "receipt_sha256": receipt_sha256,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "topic": "execution.terminal.v1",
        "delivery_key": f"execution:{execution_id}:{attempt_id}",
        "payload_sha256": receipt_sha256,
        "payload_json": receipt.model_dump(mode="json"),
        "status": status,
        "publish_attempts": 1 if status == "published" else 0,
        "created_at": NOW + timedelta(seconds=3),
        "published_at": NOW + timedelta(seconds=4) if status == "published" else None,
    }


def _accepted(attempt_id: str) -> AcceptedQualificationTerminalSubmission:
    return AcceptedQualificationTerminalSubmission(
        attempt_id=attempt_id,
        node_manifest_sha256=_digest("v2:node"),
        terminal_submission_sha256=_digest("v2:submission"),
        accepted_runtime_termination_sha256=_digest("v2:termination"),
        artifact_manifest_sha256=_digest("v2:manifest"),
        output_tree_sha256=_digest("v2:tree"),
        artifact_verified_receipt_sha256s=(_digest("v2:artifact"),),
        disposition="process_succeeded",
        node_submitted_at=NOW,
        artifact_submission_deadline=NOW + timedelta(minutes=5),
        accepted_at=NOW + timedelta(seconds=1),
        runtime_control_policy_sha256=_digest("v2:policy"),
        accepted_by_principal_id="principal:runtime-control",
        acceptance_key_id=_digest("v2:key"),
        signature_ed25519_hex="a" * 128,
    )


def _qualification_row() -> dict[str, object]:
    execution_id = "exe_" + "1" * 32
    attempt_id = "iat_" + "2" * 32
    payload = _accepted(attempt_id)
    authority_sha256 = payload.accepted_terminal_submission_sha256
    return {
        "outbox_id": f"qto_{authority_sha256}",
        "terminal_authority_kind": "accepted_terminal_submission",
        "terminal_authority_sha256": authority_sha256,
        "accepted_terminal_submission_sha256": authority_sha256,
        "terminal_deadline_expiration_sha256": None,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "topic": "execution.qualification_terminal.v2",
        "delivery_key": f"execution-v2:{execution_id}:{attempt_id}",
        "payload_sha256": authority_sha256,
        "payload_json": payload.model_dump(mode="json"),
        "created_at": NOW + timedelta(seconds=2),
    }


def _qualification_expiration_row() -> dict[str, object]:
    execution_id = "exe_" + "3" * 32
    attempt_id = "iat_" + "4" * 32
    deadline = NOW + timedelta(minutes=5)
    payload = QualificationTerminalDeadlineExpiration(
        attempt_id=attempt_id,
        execution_id=execution_id,
        intent_sha256=_digest("expiration:intent"),
        node_id="node:qualification",
        node_manifest_sha256=_digest("expiration:node"),
        node_inventory_sha256=_digest("expiration:inventory"),
        resource_lease_sha256=_digest("expiration:lease"),
        runtime_preparation_sha256=_digest("expiration:preparation"),
        runtime_launch_authorization_request_sha256=_digest("expiration:request"),
        runtime_launch_authorization_sha256=_digest("expiration:authorization"),
        node_runtime_launch_receipt_sha256=_digest("expiration:launch"),
        runtime_termination_challenge_sha256=_digest("expiration:challenge"),
        node_runtime_termination_receipt_sha256=_digest("expiration:termination-receipt"),
        accepted_runtime_termination_sha256=_digest("expiration:termination"),
        runtime_identity_sha256=_digest("expiration:runtime"),
        runtime_inspection_evidence_sha256=_digest("expiration:inspection"),
        engine_terminal_journal_sha256=_digest("expiration:journal"),
        inspection_sequence=1,
        fencing_epoch=1,
        lease_token_sha256=_digest("expiration:token"),
        runtime_ended_at=NOW,
        exit_code=0,
        hard_deadline=NOW + timedelta(minutes=1),
        artifact_submission_deadline=deadline,
        accepted_runtime_termination_at=NOW + timedelta(seconds=1),
        authorized_at=NOW + timedelta(seconds=1),
        expired_at=deadline,
        runtime_control_policy_sha256=_digest("expiration:policy"),
        adjudicated_by_principal_id="principal:runtime-control",
        adjudication_key_id=_digest("expiration:key"),
        signature_ed25519_hex="b" * 128,
    )
    authority_sha256 = payload.terminal_deadline_expiration_sha256
    return {
        "outbox_id": f"qto_{authority_sha256}",
        "terminal_authority_kind": "terminal_deadline_expiration",
        "terminal_authority_sha256": authority_sha256,
        "accepted_terminal_submission_sha256": None,
        "terminal_deadline_expiration_sha256": authority_sha256,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "topic": "execution.qualification_terminal.v2",
        "delivery_key": f"execution-v2:{execution_id}:{attempt_id}",
        "payload_sha256": authority_sha256,
        "payload_json": payload.model_dump(mode="json"),
        "created_at": deadline + timedelta(seconds=1),
    }


def _pin(monkeypatch: pytest.MonkeyPatch, root: Path) -> QualificationOutboxSpoolRootPinV1:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    metadata = root.stat()
    monkeypatch.setattr(outbox_service, "host_parent_chain_sha256", lambda _path: "e" * 64)
    return QualificationOutboxSpoolRootPinV1(
        path=str(root),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        parent_chain_sha256="e" * 64,
    )


def _process_and_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[QualificationServiceProcessDeploymentV1, QualificationTerminalOutboxServiceConfigV1]:
    pin = _pin(monkeypatch, (tmp_path / "outbox-spool").resolve())
    process = QualificationServiceProcessDeploymentV1(
        deployment_id="qualification:test",
        role=QualificationServiceRole.OUTBOX,
        operation="run",
        process_uid=pin.owner_uid,
        process_gid=pin.owner_gid,
        reviewed_code_root="/opt/aletheia/release",
        composition_factory_module="aletheia.execution.qualification_outbox_composition",
        composition_factory_attribute="build_outbox_service",
        composition_factory_source_path=(
            "/opt/aletheia/release/aletheia/execution/qualification_outbox_composition.py"
        ),
        composition_factory_source_sha256="a" * 64,
        composition_factory_owner_uid=0,
        composition_factory_owner_gid=0,
        composition_factory_mode=0o444,
        composition_config_path="/etc/aletheia/services/outbox.json",
        composition_config_file_sha256="0" * 64,
        composition_config_owner_uid=0,
        composition_config_owner_gid=0,
        composition_config_mode=0o440,
    )
    config = QualificationTerminalOutboxServiceConfigV1(
        deployment_id=process.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(process),
        database_url_sha256=hashlib.sha256(b"postgresql://outbox").hexdigest(),
        schema_revision="20260829_0028",
        postgresql_role="aletheia_exec_outbox",
        spool_root=pin,
        prepared_at=NOW,
    )
    payload = canonical_json_bytes(config)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        process.model_copy(
            update={
                "process_id": None,
                "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ).model_dump(mode="python")
    )
    return process, config


def test_terminal_spool_envelopes_are_typed_deterministic_and_closed() -> None:
    legacy, status, attempts = _legacy_envelope(_legacy_row())
    qualification = _qualification_envelope(_qualification_row())

    assert status == "pending" and attempts == 0
    assert legacy.source_kind == "execution_terminal_v1"
    assert qualification.source_kind == "qualification_terminal_v2"
    assert legacy.filename == f"{legacy.outbox_id}.json"
    assert len(legacy.envelope_sha256) == 64
    assert canonical_json_bytes(legacy) == canonical_json_bytes(
        QualificationTerminalSpoolEnvelopeV1.model_validate_json(canonical_json_bytes(legacy))
    )

    row = _qualification_row()
    row["delivery_key"] = "execution-v2:rebound"
    with pytest.raises(QualificationTerminalOutboxError, match="invalid"):
        _qualification_envelope(row)

    payload = legacy.model_dump(mode="python")
    payload["payload_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="payload identity"):
        QualificationTerminalSpoolEnvelopeV1.model_validate(payload)


def test_terminal_spool_accepts_the_exact_deadline_expiration_variant() -> None:
    envelope = _qualification_envelope(_qualification_expiration_row())

    assert envelope.terminal_authority_kind == "terminal_deadline_expiration"
    assert isinstance(envelope.payload, QualificationTerminalDeadlineExpiration)
    assert envelope.execution_id == envelope.payload.execution_id
    assert envelope.attempt_id == envelope.payload.attempt_id

    rebound = _qualification_expiration_row()
    rebound["accepted_terminal_submission_sha256"] = rebound["terminal_authority_sha256"]
    with pytest.raises(QualificationTerminalOutboxError, match="variant is rebound"):
        _qualification_envelope(rebound)


@pytest.mark.parametrize(
    "phase",
    ("pending-created", "pending-written", "pending-sealed", "final-linked"),
)
def test_spool_crash_residues_recover_the_same_bytes(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = _pin(monkeypatch, (tmp_path / "spool").resolve())
    spool = QualificationTerminalOutboxSpool(pin)
    envelope = _qualification_envelope(_qualification_row())
    lock = spool.acquire_service_lock()
    raised = False

    def checkpoint(observed: str, _path: Path) -> None:
        nonlocal raised
        if observed == phase and not raised:
            raised = True
            raise RuntimeError("injected crash")

    monkeypatch.setattr(outbox_service, "_spool_checkpoint", checkpoint)
    with pytest.raises(RuntimeError, match="injected"):
        spool.retain(envelope)
    monkeypatch.setattr(outbox_service, "_spool_checkpoint", lambda _phase, _path: None)

    spool.retain(envelope)
    final = pin.path + "/" + envelope.filename
    assert Path(final).read_bytes() == canonical_json_bytes(envelope)
    assert stat_mode(Path(final)) == 0o400
    assert not (Path(pin.path) / f".{envelope.outbox_id}.pending").exists()
    spool.verify_inventory((envelope,))
    os.close(lock)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_published_legacy_source_requires_retained_exact_spool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = _pin(monkeypatch, (tmp_path / "spool").resolve())
    spool = QualificationTerminalOutboxSpool(pin)
    envelope, _status, _attempts = _legacy_envelope(_legacy_row())
    lock = spool.acquire_service_lock()
    assert spool.retain(envelope) is True
    spool.require_published(envelope)
    (Path(pin.path) / envelope.filename).unlink()

    with pytest.raises(QualificationTerminalOutboxError, match="cannot be opened"):
        spool.require_published(envelope)
    os.close(lock)


def test_published_spool_rejects_same_size_byte_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = _pin(monkeypatch, (tmp_path / "spool").resolve())
    spool = QualificationTerminalOutboxSpool(pin)
    envelope, _status, _attempts = _legacy_envelope(_legacy_row())
    lock = spool.acquire_service_lock()
    assert spool.retain(envelope) is True
    target = spool.root / envelope.filename
    payload = target.read_bytes()
    target.chmod(0o600)
    target.write_bytes(payload[:-1] + (b"0" if payload[-1:] != b"0" else b"1"))
    target.chmod(0o400)

    with pytest.raises(QualificationTerminalOutboxError, match="lost its exact"):
        spool.require_published(envelope)
    os.close(lock)


def test_spool_allows_only_one_service_lock_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = _pin(monkeypatch, (tmp_path / "spool").resolve())
    spool = QualificationTerminalOutboxSpool(pin)
    lock = spool.acquire_service_lock()
    try:
        with pytest.raises(QualificationTerminalOutboxError, match="another"):
            spool.acquire_service_lock()
    finally:
        os.close(lock)


class _MappingResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _UpdateResult:
    def __init__(self, row: tuple[int, datetime] | None) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Session:
    def __init__(self, legacy: dict[str, object], qualification: dict[str, object]) -> None:
        self.legacy = legacy
        self.qualification = qualification

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def begin(self):
        return nullcontext()

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if "FROM execution_qualification_terminal_outbox" in sql:
            return _MappingResult([self.qualification])
        if "FROM execution_outbox" in sql:
            return _MappingResult([self.legacy])
        if "UPDATE execution_outbox" in sql:
            assert parameters == {
                "outbox_id": self.legacy["outbox_id"],
                "publish_attempts": self.legacy["publish_attempts"],
            }
            self.legacy["status"] = "published"
            self.legacy["publish_attempts"] = int(self.legacy["publish_attempts"]) + 1
            self.legacy["published_at"] = NOW + timedelta(seconds=10)
            return _UpdateResult((self.legacy["publish_attempts"], self.legacy["published_at"]))
        raise AssertionError(sql)


class _FailingUpdateSession(_Session):
    def execute(self, statement, parameters=None):
        if "UPDATE execution_outbox" in str(statement):
            return _UpdateResult(None)
        return super().execute(statement, parameters)


def test_service_tick_spools_both_generations_then_replays_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _process_and_config(monkeypatch, tmp_path)
    spool = QualificationTerminalOutboxSpool(config.spool_root)
    session = _Session(_legacy_row(), _qualification_row())
    service = QualificationTerminalOutboxService(
        config=config,
        spool=spool,
        sessions=lambda: session,
    )
    lock = spool.acquire_service_lock()

    first = service.tick()
    second = service.tick()

    assert len(first.source_outbox_ids) == 2
    assert first.newly_spooled_outbox_ids == first.source_outbox_ids
    assert first.legacy_published_outbox_ids == (session.legacy["outbox_id"],)
    assert first.work_performed is True
    assert second.newly_spooled_outbox_ids == ()
    assert second.replayed_spool_outbox_ids == second.source_outbox_ids
    assert second.legacy_published_outbox_ids == ()
    assert second.work_performed is False
    os.close(lock)


def test_service_replays_fsynced_file_after_database_cas_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _process_and_config(monkeypatch, tmp_path)
    spool = QualificationTerminalOutboxSpool(config.spool_root)
    legacy = _legacy_row()
    qualification = _qualification_row()
    failing = _FailingUpdateSession(legacy, qualification)
    succeeding = _Session(legacy, qualification)
    sessions = iter((failing, succeeding))
    service = QualificationTerminalOutboxService(
        config=config,
        spool=spool,
        sessions=lambda: next(sessions),
    )
    lock = spool.acquire_service_lock()

    with pytest.raises(QualificationTerminalOutboxError, match="CAS failed"):
        service.tick()
    envelope, _status, _attempts = _legacy_envelope(legacy)
    assert (spool.root / envelope.filename).read_bytes() == canonical_json_bytes(envelope)

    replay = service.tick()
    assert replay.newly_spooled_outbox_ids == (_qualification_envelope(qualification).outbox_id,)
    assert replay.replayed_spool_outbox_ids == (envelope.outbox_id,)
    assert replay.legacy_published_outbox_ids == (envelope.outbox_id,)
    os.close(lock)


def test_service_rejects_a_source_set_above_the_deployment_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _process_and_config(monkeypatch, tmp_path)
    config = QualificationTerminalOutboxServiceConfigV1.model_validate(
        config.model_copy(update={"maximum_source_rows_per_kind": 1}).model_dump(mode="python")
    )
    spool = QualificationTerminalOutboxSpool(config.spool_root)
    session = _Session(_legacy_row(), _qualification_row())
    original_execute = session.execute

    def execute(statement, parameters=None):
        if "FROM execution_outbox" in str(statement):
            row = _legacy_row()
            second = dict(row)
            second["outbox_id"] = "xob_" + "f" * 64
            return _MappingResult([row, second])
        return original_execute(statement, parameters)

    monkeypatch.setattr(session, "execute", execute)
    service = QualificationTerminalOutboxService(
        config=config,
        spool=spool,
        sessions=lambda: session,
    )

    with pytest.raises(QualificationTerminalOutboxError, match="bounded deployment scan"):
        service.tick()


def test_service_rejects_foreign_spool_inventory_and_rebound_published_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _process_and_config(monkeypatch, tmp_path)
    spool = QualificationTerminalOutboxSpool(config.spool_root)
    session = _Session(_legacy_row(), _qualification_row())
    service = QualificationTerminalOutboxService(
        config=config,
        spool=spool,
        sessions=lambda: session,
    )
    lock = spool.acquire_service_lock()
    (spool.root / "foreign.json").write_text("{}", encoding="utf-8")

    with pytest.raises(QualificationTerminalOutboxError, match="foreign"):
        service.tick()

    (spool.root / "foreign.json").unlink()
    session.legacy.update({"status": "published", "publish_attempts": 0, "published_at": NOW})
    with pytest.raises(QualificationTerminalOutboxError, match="state is rebound"):
        service.tick()
    os.close(lock)


def test_outbox_factory_binds_process_database_and_canonical_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, config = _process_and_config(monkeypatch, tmp_path)
    calls: list[str] = []

    class FakeSpool:
        def __init__(self, pin) -> None:
            assert pin == config.spool_root

    class FakeService:
        def __init__(self, *, config: object, spool: object, sessions: object) -> None:
            assert config == config_value
            assert isinstance(spool, FakeSpool)
            assert callable(sessions)

        def run_forever(self) -> None:
            calls.append("run")

    config_value = config
    monkeypatch.setattr(
        outbox_service, "get_settings", lambda: SimpleNamespace(database_url="postgresql://outbox")
    )
    monkeypatch.setattr(outbox_service, "expected_schema_revision", lambda: config.schema_revision)
    monkeypatch.setattr(outbox_service, "_verify_live_database_binding", lambda _config: None)
    monkeypatch.setattr(outbox_service, "QualificationTerminalOutboxSpool", FakeSpool)
    monkeypatch.setattr(outbox_service, "QualificationTerminalOutboxService", FakeService)
    payload = canonical_json_bytes(config)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        process.model_copy(
            update={
                "process_id": None,
                "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ).model_dump(mode="python")
    )

    handlers = build_outbox_service(deployment=process, configuration_bytes=payload)
    handlers.handler(poll_milliseconds=None)

    assert handlers.role is QualificationServiceRole.OUTBOX
    assert calls == ["run"]
    with pytest.raises(QualificationTerminalOutboxError, match="node-only"):
        handlers.handler(poll_milliseconds=250)
    with pytest.raises(QualificationTerminalOutboxError, match="not canonical"):
        build_outbox_service(deployment=process, configuration_bytes=payload + b"\n")


def test_guarded_loader_accepts_the_final_outbox_config_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pins = tmp_path / "pins"
    pins.mkdir()
    process, config = _process_and_config(monkeypatch, pins)
    release = (tmp_path / "release").resolve()
    source = release / "aletheia/execution/qualification_outbox_composition.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        Path("aletheia/execution/qualification_outbox_composition.py").resolve().read_bytes()
    )
    source.chmod(0o444)
    config_path = (tmp_path / "config/outbox.json").resolve()
    config_path.parent.mkdir(parents=True)
    source_metadata = source.stat()
    prototype = QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            "reviewed_code_root": str(release),
            "composition_factory_source_path": str(source),
            "composition_factory_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "composition_factory_owner_uid": source_metadata.st_uid,
            "composition_factory_owner_gid": source_metadata.st_gid,
            "composition_factory_mode": 0o444,
            "composition_config_path": str(config_path),
            "composition_config_file_sha256": "0" * 64,
            "composition_config_owner_uid": os.geteuid(),
            "composition_config_owner_gid": os.getegid(),
            "composition_config_mode": 0o440,
        }
    )
    config = QualificationTerminalOutboxServiceConfigV1.model_validate(
        config.model_copy(
            update={
                "process_config_binding_sha256": (
                    qualification_service_process_config_binding_sha256(prototype)
                )
            }
        ).model_dump(mode="python")
    )
    payload = canonical_json_bytes(config)
    config_path.write_bytes(payload)
    config_path.chmod(0o440)
    process = QualificationServiceProcessDeploymentV1.model_validate(
        {
            **prototype.model_dump(mode="python", exclude={"process_id"}),
            "composition_config_file_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )

    class FakeSpool:
        def __init__(self, pin) -> None:
            assert pin == config.spool_root

    class FakeService:
        def __init__(self, *, config: object, spool: object, sessions: object) -> None:
            assert config == config_value
            assert isinstance(spool, FakeSpool)
            assert callable(sessions)

        def run_forever(self) -> None:
            return None

    config_value = config
    monkeypatch.setattr(
        outbox_service, "get_settings", lambda: SimpleNamespace(database_url="postgresql://outbox")
    )
    monkeypatch.setattr(outbox_service, "expected_schema_revision", lambda: config.schema_revision)
    monkeypatch.setattr(outbox_service, "_verify_live_database_binding", lambda _config: None)
    monkeypatch.setattr(outbox_service, "QualificationTerminalOutboxSpool", FakeSpool)
    monkeypatch.setattr(outbox_service, "QualificationTerminalOutboxService", FakeService)

    handlers = service_runtime._load_handler_set(process)  # noqa: SLF001
    assert handlers.role is QualificationServiceRole.OUTBOX


def test_live_database_binding_requires_exact_role_and_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _process, config = _process_and_config(monkeypatch, tmp_path)

    class Result:
        def __init__(self, row) -> None:
            self._row = row

        def one(self):
            return self._row

    class Session:
        def __init__(self, row) -> None:
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return Result(self._row)

    monkeypatch.setattr(
        outbox_service,
        "session_factory",
        lambda: lambda: Session((config.postgresql_role, 1, config.schema_revision)),
    )
    outbox_service._verify_live_database_binding(config)

    monkeypatch.setattr(
        outbox_service,
        "session_factory",
        lambda: lambda: Session(("wrong_role", 1, config.schema_revision)),
    )
    with pytest.raises(QualificationTerminalOutboxError, match="role or live schema"):
        outbox_service._verify_live_database_binding(config)


def test_spool_root_rejects_live_mode_or_inode_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pin = _pin(monkeypatch, (tmp_path / "spool").resolve())
    QualificationTerminalOutboxSpool(pin)
    Path(pin.path).chmod(0o750)

    with pytest.raises(QualificationTerminalOutboxError, match="custody differs"):
        QualificationTerminalOutboxSpool(pin)
