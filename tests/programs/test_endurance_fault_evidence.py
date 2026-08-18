from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.db import create_all
from aletheia.jobs import FaultCampaignCommitContext, FaultCampaignStore
from aletheia.programs import (
    EnduranceControllerAction,
    EnduranceEvidenceClass,
    EnduranceFaultEvidenceError,
    EnduranceInterruptionKind,
    ResearchEnduranceStore,
    build_endurance_interruption_evidence,
    prepare_endurance_controller_manifest,
    prepare_endurance_gate_manifest,
    run_controller_tick,
    start_endurance_controller_gate,
    submit_endurance_fault_evidence,
)

from .test_endurance_gate import _passing_fault_report, _seed_store_prerequisites, _sha


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def test_fault_report_maps_exact_process_and_provider_scenarios() -> None:
    base = datetime.now(timezone.utc) - timedelta(minutes=1)
    report = _passing_fault_report(uuid.uuid4().hex, "qst_" + "1" * 32, base)
    evidence = build_endurance_interruption_evidence(report)
    assert {item.kind for item in evidence.interruptions} == {
        EnduranceInterruptionKind.PROCESS_KILL,
        EnduranceInterruptionKind.PROVIDER_TRANSPORT,
    }
    by_kind = {item.kind: item for item in evidence.interruptions}
    assert by_kind[EnduranceInterruptionKind.PROCESS_KILL].scenario_id.startswith("api_process")
    assert by_kind[EnduranceInterruptionKind.PROVIDER_TRANSPORT].scenario_id.startswith("provider")
    assert all(item.fault_report_sha256 == report.report_sha256 for item in evidence.interruptions)


def test_committed_in_window_fault_evidence_submits_once_and_checkpoints(tmp_path: Path) -> None:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    _, quest, _, _, prerequisite_report, prerequisite = _seed_store_prerequisites(seed, base)
    gate = prepare_endurance_gate_manifest(
        gate_key=f"fault-evidence-{seed}",
        quest_id=quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=120,
        checkpoint_interval_seconds=30,
        maximum_checkpoint_gap_seconds=60,
        prerequisite_fault_campaign_id=prerequisite.campaign_id,
        harness_code_sha256=_sha(f"{seed}:endurance-harness"),
        environment_manifest_sha256=_sha(f"{seed}:endurance-environment"),
    )
    controller = prepare_endurance_controller_manifest(
        gate,
        controller_key=f"fault-evidence-controller-{seed}",
        principal="controller:fault-evidence-test",
        spool_root="spool",
        supervisor_poll_seconds=5,
        prepared_at=base,
        require_committed=False,
    )
    start_endurance_controller_gate(controller, artifact_root=tmp_path, now=base)
    with pytest.raises(EnduranceFaultEvidenceError, match="before the endurance window"):
        submit_endurance_fault_evidence(
            controller,
            prerequisite_report,
            producer="worker:fault-evidence-pre-window",
            artifact_root=tmp_path,
        )

    live_report = _passing_fault_report(f"{seed}-live", quest.node_id, base + timedelta(seconds=1))
    with pytest.raises(EnduranceFaultEvidenceError, match="not committed"):
        submit_endurance_fault_evidence(
            controller,
            live_report,
            producer="worker:fault-evidence-uncommitted",
            artifact_root=tmp_path,
        )
    FaultCampaignStore().commit(
        live_report,
        FaultCampaignCommitContext(
            idempotency_key=f"fault-evidence:{seed}:commit",
            principal="harness:fault-evidence-test",
        ),
        now=base + timedelta(seconds=5),
    )
    first = submit_endurance_fault_evidence(
        controller,
        live_report,
        producer="worker:fault-evidence-first",
        artifact_root=tmp_path,
    )
    replay = submit_endurance_fault_evidence(
        controller,
        live_report,
        producer="worker:fault-evidence-retry",
        artifact_root=tmp_path,
    )
    assert first.envelope_created is True
    assert replay.envelope_created is False
    assert replay.envelope == first.envelope

    tick = run_controller_tick(
        controller,
        artifact_root=tmp_path,
        now=base + timedelta(seconds=6),
    )
    assert tick.action is EnduranceControllerAction.CHECKPOINTED
    snapshot = ResearchEnduranceStore().get(str(gate.gate_id))
    interruptions = snapshot.checkpoints[-1].checkpoint.evidence.interruptions
    assert {item.receipt_id for item in interruptions} == {
        first.process_receipt_id,
        first.provider_receipt_id,
    }
