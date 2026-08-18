from __future__ import annotations

import json
import os
import plistlib
import shutil
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.db import REPO_ROOT, create_all
from aletheia.programs import (
    EnduranceControllerAction,
    EnduranceControllerManifest,
    EnduranceEvidenceClass,
    EnduranceSupervisorManifest,
    EnduranceSupervisorCycleAction,
    EnduranceSupervisorConflict,
    capture_supervisor_runtime,
    preflight_endurance_supervisor,
    prepare_controller_spool,
    prepare_endurance_controller_manifest,
    prepare_endurance_gate_manifest,
    prepare_endurance_supervisor_manifest,
    render_endurance_launchd_plist,
    run_endurance_supervisor_cycle,
    start_endurance_controller_gate,
)

from .test_endurance_gate import _seed_store_prerequisites, _sha


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _runtime():
    conda = shutil.which("conda")
    assert conda is not None
    return capture_supervisor_runtime(
        conda_executable=Path(conda),
        conda_environment="aletheia",
    )


def _json_bytes(value: object) -> bytes:
    assert hasattr(value, "model_dump")
    payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


@pytest.fixture
def supervisor_deployment() -> Iterator[
    tuple[
        EnduranceControllerManifest,
        EnduranceSupervisorManifest,
        Path,
        datetime,
    ]
]:
    seed = uuid.uuid4().hex
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    _, quest, _, _, _, prerequisite = _seed_store_prerequisites(seed, base)
    gate = prepare_endurance_gate_manifest(
        gate_key=f"supervisor-{seed}",
        quest_id=quest.node_id,
        evidence_class=EnduranceEvidenceClass.ACCELERATED_ENGINEERING,
        required_duration_seconds=120,
        checkpoint_interval_seconds=30,
        maximum_checkpoint_gap_seconds=60,
        prerequisite_fault_campaign_id=prerequisite.campaign_id,
        harness_code_sha256=_sha(f"{seed}:endurance-harness"),
        environment_manifest_sha256=_sha(f"{seed}:endurance-environment"),
    )
    relative_root = Path("artifacts") / f"supervisor-test-{seed}"
    deployment_root = REPO_ROOT / relative_root
    controller_path = deployment_root / "controller.json"
    manifest_path = deployment_root / "supervisor.json"
    plist_path = deployment_root / "supervisor.plist"
    controller = prepare_endurance_controller_manifest(
        gate,
        controller_key=f"supervisor-controller-{seed}",
        principal="controller:supervisor-test",
        spool_root=(relative_root / "spool").as_posix(),
        supervisor_poll_seconds=5,
        prepared_at=base,
        require_committed=False,
    )
    deployment_root.mkdir(parents=True, mode=0o700)
    controller_path.write_bytes(_json_bytes(controller))
    prepare_controller_spool(controller, artifact_root=REPO_ROOT)
    manifest = prepare_endurance_supervisor_manifest(
        controller,
        controller_manifest_path=controller_path,
        supervisor_manifest_path=manifest_path,
        launchd_plist_path=plist_path,
        stdout_log_path=deployment_root / "stdout.log",
        stderr_log_path=deployment_root / "stderr.log",
        supervisor_key=f"supervisor-{seed}",
        launchd_label=f"org.aletheia.endurance.test-{seed}",
        launchd_domain=f"gui/{os.getuid()}",
        runtime=_runtime(),
        repository_root=REPO_ROOT,
        prepared_at=base,
    )
    manifest_path.write_bytes(_json_bytes(manifest))
    plist_path.write_bytes(render_endurance_launchd_plist(manifest))
    try:
        yield controller, manifest, deployment_root, base
    finally:
        shutil.rmtree(deployment_root, ignore_errors=True)


def test_launchd_plist_is_exact_run_once_conda_invocation(
    supervisor_deployment: tuple[
        EnduranceControllerManifest,
        EnduranceSupervisorManifest,
        Path,
        datetime,
    ],
) -> None:
    controller, manifest, deploy, _ = supervisor_deployment
    payload = plistlib.loads(render_endurance_launchd_plist(manifest))
    assert payload["StartInterval"] == controller.supervisor_poll_seconds
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is False
    assert payload["ProgramArguments"][:6] == [
        manifest.runtime.conda_executable,
        "run",
        "--no-capture-output",
        "-n",
        "aletheia",
        "python",
    ]
    assert payload["ProgramArguments"][6].endswith("scripts/run_endurance_supervisor.py")
    assert payload["ProgramArguments"][7:] == [
        "cycle",
        str(deploy / "supervisor.json"),
    ]
    assert manifest.automatic_start is False
    assert manifest.automatic_finalization is False


def test_loaded_supervisor_waits_then_ticks_without_start_or_finalize(
    supervisor_deployment: tuple[
        EnduranceControllerManifest,
        EnduranceSupervisorManifest,
        Path,
        datetime,
    ],
) -> None:
    controller, manifest, _, base = supervisor_deployment
    unloaded = preflight_endurance_supervisor(manifest, loaded_probe=lambda _: False)
    assert unloaded.eligible_for_explicit_start is False
    assert unloaded.blockers == ("launchd:job_not_loaded",)
    loaded = preflight_endurance_supervisor(manifest, loaded_probe=lambda _: True)
    assert loaded.eligible_for_explicit_start is True
    waiting = run_endurance_supervisor_cycle(manifest, now=base)
    assert waiting.action is EnduranceSupervisorCycleAction.WAITING_FOR_EXPLICIT_START
    assert waiting.gate_state == "not_started"
    assert waiting.controller_tick is None

    started = start_endurance_controller_gate(
        controller,
        artifact_root=REPO_ROOT,
        now=base + timedelta(seconds=1),
    )
    assert started.action is EnduranceControllerAction.STARTED
    cycle = run_endurance_supervisor_cycle(
        manifest,
        now=base + timedelta(seconds=2),
    )
    assert cycle.action is EnduranceSupervisorCycleAction.CONTROLLER_TICK
    assert cycle.controller_tick is not None
    assert cycle.controller_tick.action is EnduranceControllerAction.NOT_DUE
    assert cycle.automatic_start is False
    assert cycle.automatic_finalization is False


def test_supervisor_cycle_rejects_plist_drift(
    supervisor_deployment: tuple[
        EnduranceControllerManifest,
        EnduranceSupervisorManifest,
        Path,
        datetime,
    ],
) -> None:
    _, manifest, deployment_root, base = supervisor_deployment
    (deployment_root / "supervisor.plist").write_bytes(b"changed")
    with pytest.raises(EnduranceSupervisorConflict, match="plist bytes differ"):
        run_endurance_supervisor_cycle(manifest, now=base)
