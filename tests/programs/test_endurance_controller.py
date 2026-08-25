from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

import aletheia.programs.endurance_controller as controller_module
from aletheia.db import REPO_ROOT, create_all, engine
from aletheia.programs import (
    EnduranceCheckpointEvidence,
    EnduranceCommandContext,
    EnduranceControllerAction,
    EnduranceControllerConflict,
    EnduranceControllerPreflightError,
    EnduranceEvidenceClass,
    EnduranceReproductionConclusion,
    EnduranceReproductionReceipt,
    ResearchEnduranceStore,
    controller_advisory_key,
    controller_status,
    preflight_endurance_controller,
    prepare_endurance_controller_manifest,
    prepare_endurance_gate_manifest,
    run_controller_tick,
    start_endurance_controller_gate,
    submit_controller_evidence,
    verify_endurance_controller_code_identity,
)
from .test_endurance_gate import _seed_store_prerequisites, _sha


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _fixture(tmp_path: Path, *, real_time: bool = False):
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    _, quest, _, campaigns, _, fault = _seed_store_prerequisites(seed, base)
    gate = prepare_endurance_gate_manifest(
        gate_key=f"controller-{seed}",
        quest_id=quest.node_id,
        evidence_class=(
            EnduranceEvidenceClass.REAL_TIME_72H
            if real_time
            else EnduranceEvidenceClass.ACCELERATED_ENGINEERING
        ),
        required_duration_seconds=72 * 60 * 60 if real_time else 120,
        checkpoint_interval_seconds=60 * 60 if real_time else 30,
        maximum_checkpoint_gap_seconds=2 * 60 * 60 if real_time else 60,
        prerequisite_fault_campaign_id=fault.campaign_id,
        harness_code_sha256=_sha(f"{seed}:controller-harness"),
        environment_manifest_sha256=_sha(f"{seed}:controller-environment"),
    )
    manifest = prepare_endurance_controller_manifest(
        gate,
        controller_key=f"run-once-{seed}",
        principal="controller:endurance-test",
        spool_root="spool",
        supervisor_poll_seconds=300 if real_time else 5,
        prepared_at=base,
        require_committed=False,
    )
    return seed, base, campaigns, gate, manifest


def _reproduction(seed: str, base: datetime, campaigns) -> EnduranceCheckpointEvidence:
    return EnduranceCheckpointEvidence(
        reproductions=(
            EnduranceReproductionReceipt(
                original_campaign_id=campaigns[0].node_id,
                reproduction_campaign_id=campaigns[2].node_id,
                protocol_sha256=_sha(f"{seed}:controller-protocol"),
                original_result_sha256=_sha(f"{seed}:controller-original"),
                reproduction_result_sha256=_sha(f"{seed}:controller-reproduction"),
                conclusion=EnduranceReproductionConclusion.CONFIRMED,
                evidence_sha256s=(_sha(f"{seed}:controller-reproduction-evidence"),),
                validated_by="harness:endurance-controller",
                completed_at=base + timedelta(seconds=11),
            ),
        )
    )


def test_run_once_controller_is_idempotent_locked_and_restart_safe(tmp_path: Path) -> None:
    seed, base, campaigns, gate, manifest = _fixture(tmp_path)
    preflight = preflight_endurance_controller(
        manifest,
        artifact_root=tmp_path,
        now=base - timedelta(seconds=1),
    )
    assert preflight.eligible_to_start is True
    assert preflight.blockers == ()

    started = start_endurance_controller_gate(manifest, artifact_root=tmp_path, now=base)
    assert started.action is EnduranceControllerAction.STARTED
    replayed_start = start_endurance_controller_gate(
        manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=1),
    )
    assert replayed_start.action is EnduranceControllerAction.STARTED
    assert "replayed" in replayed_start.message

    early = run_controller_tick(
        manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=10),
    )
    assert early.action is EnduranceControllerAction.NOT_DUE
    assert early.resulting_checkpoint_count == 0

    evidence = _reproduction(seed, base, campaigns)
    envelope, created = submit_controller_evidence(
        manifest,
        evidence,
        producer="worker:first",
        submitted_at=base + timedelta(seconds=11),
        artifact_root=tmp_path,
    )
    retried, recreated = submit_controller_evidence(
        manifest,
        evidence,
        producer="worker:restarted",
        submitted_at=base + timedelta(seconds=12),
        artifact_root=tmp_path,
    )
    assert created is True
    assert recreated is False
    assert retried == envelope

    checkpointed = run_controller_tick(
        manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=12),
    )
    assert checkpointed.action is EnduranceControllerAction.CHECKPOINTED
    assert checkpointed.checkpoint_envelope_ids == (envelope.envelope_id,)
    assert checkpointed.resulting_checkpoint_count == 1
    assert checkpointed.checkpoint_due_at_before_action == base + timedelta(seconds=30)
    assert checkpointed.next_checkpoint_due_at == base + timedelta(seconds=42)
    assert checkpointed.overdue_before_action is False
    assert not tuple((tmp_path / "spool" / "pending").glob("*.json"))
    assert (tmp_path / "spool" / "committed" / f"{envelope.envelope_id}.json").is_file()

    resumed_manifest = manifest.model_validate(manifest.model_dump(mode="json"))
    scheduled = run_controller_tick(
        resumed_manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=42),
    )
    assert scheduled.action is EnduranceControllerAction.CHECKPOINTED
    assert scheduled.checkpoint_envelope_ids == ()
    assert scheduled.resulting_checkpoint_count == 2

    assert gate.gate_id is not None
    key = controller_advisory_key(gate.gate_id)
    with engine().connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            busy = run_controller_tick(
                manifest,
                artifact_root=tmp_path,
                now=base + timedelta(seconds=43),
            )
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    assert busy.action is EnduranceControllerAction.LOCK_BUSY

    status = controller_status(
        manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=43),
    )
    assert status["state"] == "running"
    assert status["checkpoint_count"] == 2
    assert len(tuple((tmp_path / "spool" / "receipts").glob("*.json"))) >= 6

    snapshot = ResearchEnduranceStore().get(str(gate.gate_id))
    assert len(snapshot.checkpoints) == 2
    assert snapshot.report is None


def test_controller_recovers_commit_before_local_archive(tmp_path: Path) -> None:
    seed, base, campaigns, gate, manifest = _fixture(tmp_path)
    start_endurance_controller_gate(manifest, artifact_root=tmp_path, now=base)
    evidence = _reproduction(seed, base, campaigns)
    envelope, _ = submit_controller_evidence(
        manifest,
        evidence,
        producer="worker:crash-window",
        submitted_at=base + timedelta(seconds=11),
        artifact_root=tmp_path,
    )

    assert manifest.controller_id is not None
    ResearchEnduranceStore().append_checkpoint(
        str(gate.gate_id),
        evidence,
        EnduranceCommandContext(
            idempotency_key=(f"{manifest.controller_id}:checkpoint:{gate.manifest_sha256[:32]}"),
            principal=manifest.principal,
        ),
        now=base + timedelta(seconds=12),
    )

    recovered = run_controller_tick(
        manifest,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=13),
    )
    assert recovered.action is EnduranceControllerAction.RECOVERED_SPOOL
    assert recovered.recovered_envelope_ids == (envelope.envelope_id,)
    assert recovered.checkpoint_envelope_ids == ()
    assert recovered.resulting_checkpoint_count == 1
    assert not tuple((tmp_path / "spool" / "pending").glob("*.json"))
    assert (tmp_path / "spool" / "committed" / f"{envelope.envelope_id}.json").is_file()


def test_preflight_blocks_dirty_spool_and_code_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, base, campaigns, _, manifest = _fixture(tmp_path)
    submit_controller_evidence(
        manifest,
        _reproduction(seed, base, campaigns),
        producer="worker:premature",
        submitted_at=base,
        artifact_root=tmp_path,
    )
    preflight = preflight_endurance_controller(
        manifest,
        artifact_root=tmp_path,
        now=base,
    )
    assert preflight.eligible_to_start is False
    assert "spool:evidence_submitted_before_start" in preflight.blockers

    monkeypatch.setattr(controller_module, "_sha256_file", lambda _path: "0" * 64)
    with pytest.raises(EnduranceControllerConflict, match="differs from frozen identity"):
        verify_endurance_controller_code_identity(
            manifest.code_identity,
            repository_root=REPO_ROOT,
        )


def test_real_time_controller_has_no_injected_clock_path(tmp_path: Path) -> None:
    _, base, _, _, manifest = _fixture(tmp_path, real_time=True)
    with pytest.raises(EnduranceControllerPreflightError, match="rejects an injected clock"):
        preflight_endurance_controller(
            manifest,
            artifact_root=tmp_path,
            now=base,
        )
    with pytest.raises(EnduranceControllerPreflightError, match="rejects an injected clock"):
        start_endurance_controller_gate(
            manifest,
            artifact_root=tmp_path,
            now=base,
        )
    with pytest.raises(EnduranceControllerConflict, match="rejects an injected clock"):
        run_controller_tick(
            manifest,
            artifact_root=tmp_path,
            now=base,
        )
