"""Gold and adversarial tests for the F10-S5 ASE/EMT simulation boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aletheia.capabilities import (
    CapabilityRegistry,
    CapabilityRegistrySnapshot,
    ExperimentCapabilityManifest,
    UnsupportedCapability,
)
from aletheia.capabilities.observations import CapabilityObservationArchive
from aletheia.domains.materials.simulation import (
    AseEmtEosJob,
    AseEmtSimulationBundle,
    AseEmtSimulationProtocol,
    EmtCalculatorPolicy,
    EosScanPolicy,
    SimulationContainerPolicy,
    SimulationExecutionStatus,
    SimulationGoldReference,
    SimulationQualityPolicy,
    SimulationRawRun,
    SimulationSecurityReceipt,
    SimulationValidation,
    SimulationValidationDisposition,
    ase_emt_worker_sha256,
    assemble_ase_emt_simulation_bundle,
    bind_ase_emt_job,
    compare_ase_emt_simulation_reproduction,
    parse_ase_emt_simulation,
    simulation_executor_implementation_sha256,
    simulation_parser_implementation_sha256,
    validate_ase_emt_simulation,
)
from aletheia.reproducibility.manifest import content_sha256


BASE = datetime(2026, 8, 15, 9, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/materials_simulation"
JOB_BYTES = (FIXTURES / "cu_fcc_eos_job.json").read_bytes()
RESULT_BYTES = (FIXTURES / "cu_fcc_eos_result.json").read_bytes()
CHECKPOINT_BYTES = (FIXTURES / "cu_fcc_eos_checkpoint.json").read_bytes()


def job() -> AseEmtEosJob:
    return AseEmtEosJob.model_validate_json(JOB_BYTES)


def protocol(*, lattice_constant: float = 3.589825) -> AseEmtSimulationProtocol:
    current_job = job()
    implementation = simulation_parser_implementation_sha256()
    return AseEmtSimulationProtocol(
        protocol_id="ase-emt-cu-fcc-eos-reference-v1",
        container=SimulationContainerPolicy(
            image_id=("sha256:54190c4fdf338fa4cf342f11f573593d47a623fabfe9c34f0828b8cac29b4b24"),
            image_tag="aletheia-ase-emt:3.29.0",
            worker_sha256=ase_emt_worker_sha256(),
            pids_limit=32,
            memory_mebibytes=256,
            cpus=1,
            timeout_seconds=10,
        ),
        calculator=EmtCalculatorPolicy(),
        scan=EosScanPolicy(points=5, volume_strain_fraction=0.04),
        runtime_versions={"ase": "3.29.0", "numpy": "2.4.6", "scipy": "1.17.1"},
        host_executor_implementation_sha256=simulation_executor_implementation_sha256(),
        parser_implementation_sha256=implementation,
        validator_implementation_sha256=implementation,
        quality=SimulationQualityPolicy(
            minimum_bulk_modulus_eV_per_angstrom3=0.1,
            maximum_bulk_modulus_eV_per_angstrom3=2.0,
            maximum_fit_rmse_eV=1e-5,
            maximum_fit_absolute_residual_eV=2e-5,
        ),
        gold_reference=SimulationGoldReference(
            reference_id="ase-official-cu-fcc-eos-doctest-v1",
            job_sha256=current_job.job_sha256,
            exact_job_bytes_sha256=hashlib.sha256(JOB_BYTES).hexdigest(),
            expected_result_payload_sha256=(
                "f8d94b2850f51ec72037521fb87d72546e966968fc2de9845c8dfe2c6c7057f7"
            ),
            expected_lattice_constant_angstrom=lattice_constant,
            lattice_absolute_tolerance_angstrom=1e-6,
            official_reference_uri="https://docs.ase-lib.org/ase/eos.html",
        ),
        frozen_at=BASE,
    )


def raw_run(
    tmp_path: Path,
    *,
    result_bytes: bytes = RESULT_BYTES,
    status: SimulationExecutionStatus = SimulationExecutionStatus.SUCCEEDED,
    protocol_value: AseEmtSimulationProtocol | None = None,
) -> tuple[SimulationRawRun, CapabilityObservationArchive, dict[str, bytes]]:
    archive = CapabilityObservationArchive(tmp_path / "archive")
    payloads = (
        {
            "checkpoint-json": CHECKPOINT_BYTES,
            "input-job-json": JOB_BYTES,
            "result-json": result_bytes,
            "stdout": b'{"status":"completed"}\n',
        }
        if status is SimulationExecutionStatus.SUCCEEDED
        else {
            "input-job-json": JOB_BYTES,
            "stderr": b'{"error_type":"NotImplementedError"}\n',
        }
    )
    artifacts = tuple(
        sorted(
            (
                archive.store(
                    artifact_id=artifact_id,
                    payload=payload,
                    media_type=(
                        "application/json" if artifact_id.endswith("json") else "text/plain"
                    ),
                    captured_at=BASE + timedelta(seconds=1),
                )
                for artifact_id, payload in payloads.items()
            ),
            key=lambda item: item.artifact_id,
        )
    )
    current_protocol = protocol_value or protocol()
    run = SimulationRawRun(
        run_id=f"gold-run-{status.value}",
        protocol_sha256=current_protocol.protocol_sha256,
        job_sha256=job().job_sha256,
        exact_job_bytes_sha256=hashlib.sha256(JOB_BYTES).hexdigest(),
        image_id=current_protocol.container.image_id,
        worker_sha256=current_protocol.container.worker_sha256,
        executor_implementation_sha256=simulation_executor_implementation_sha256(),
        command_sha256=content_sha256(["docker", "run", status.value]),
        security=SimulationSecurityReceipt(
            pids_limit=32,
            memory_mebibytes=256,
            cpus=1,
            timeout_seconds=10,
        ),
        status=status,
        exit_code=0 if status is SimulationExecutionStatus.SUCCEEDED else 1,
        failure_kind=None
        if status is SimulationExecutionStatus.SUCCEEDED
        else "worker_nonzero_exit",
        artifacts=artifacts,
        started_at=BASE + timedelta(seconds=1),
        ended_at=BASE + timedelta(seconds=2),
        checkpoint_retained=status is SimulationExecutionStatus.SUCCEEDED,
    )
    return run, archive, payloads


def parse_and_validate(tmp_path: Path):
    current_protocol = protocol()
    current_job = job()
    run, archive, _ = raw_run(tmp_path)
    artifacts = {item.artifact_id: archive.read(item) for item in run.artifacts}
    parsed = parse_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        exact_job_bytes=JOB_BYTES,
        raw_run=run,
        artifacts=artifacts,
        parsed_at=BASE + timedelta(seconds=3),
    )
    validation = validate_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        raw_run=run,
        parse_result=parsed,
        validated_at=BASE + timedelta(seconds=4),
    )
    return current_protocol, current_job, run, parsed, validation


def test_official_cu_gold_parses_validates_and_closes_bundle(tmp_path: Path):
    current_protocol, current_job, run, parsed, validation = parse_and_validate(tmp_path)
    assert parsed.candidate is not None
    result = parsed.candidate.result
    lattice = (4 * result.equilibrium_volume_per_atom_angstrom3) ** (1 / 3)
    assert lattice == pytest.approx(3.589824595554312, abs=1e-12)
    assert validation.disposition is SimulationValidationDisposition.VALIDATED_CLASSICAL_REFERENCE
    assert validation.all_required_checks_passed is True
    bundle = assemble_ase_emt_simulation_bundle(
        protocol=current_protocol,
        job=current_job,
        raw_run=run,
        parse_result=parsed,
        validation=validation,
    )
    AseEmtSimulationBundle.model_validate(bundle.model_dump(mode="json"))


def test_changed_worker_result_payload_fails_parse_closed(tmp_path: Path):
    payload = json.loads(RESULT_BYTES)
    payload["bulk_modulus_eV_per_angstrom3"] += 0.5
    changed = json.dumps(payload, sort_keys=True).encode()
    run, archive, _ = raw_run(tmp_path, result_bytes=changed)
    artifacts = {item.artifact_id: archive.read(item) for item in run.artifacts}
    parsed = parse_ase_emt_simulation(
        protocol=protocol(),
        job=job(),
        exact_job_bytes=JOB_BYTES,
        raw_run=run,
        artifacts=artifacts,
        parsed_at=BASE + timedelta(seconds=3),
    )
    assert parsed.candidate is None
    assert parsed.failure.failure_kind == "invalid_simulation_output"  # type: ignore[union-attr]


def test_checkpoint_tampering_fails_parse_closed(tmp_path: Path):
    run, archive, payloads = raw_run(tmp_path)
    checkpoint = json.loads(payloads["checkpoint-json"])
    checkpoint["completed_evaluations"] = 4
    replacement = archive.store(
        artifact_id="checkpoint-json",
        payload=json.dumps(checkpoint).encode(),
        media_type="application/json",
        captured_at=BASE + timedelta(seconds=1),
    )
    changed_artifacts = tuple(
        replacement if item.artifact_id == "checkpoint-json" else item for item in run.artifacts
    )
    changed_run = SimulationRawRun.model_validate(
        {**run.model_dump(mode="json"), "artifacts": changed_artifacts}
    )
    artifacts = {item.artifact_id: archive.read(item) for item in changed_run.artifacts}
    parsed = parse_ase_emt_simulation(
        protocol=protocol(),
        job=job(),
        exact_job_bytes=JOB_BYTES,
        raw_run=changed_run,
        artifacts=artifacts,
        parsed_at=BASE + timedelta(seconds=3),
    )
    assert parsed.candidate is None


def test_execution_failure_is_not_relabelled_as_physical_result(tmp_path: Path):
    current_protocol = protocol()
    current_job = job()
    run, archive, _ = raw_run(tmp_path, status=SimulationExecutionStatus.FAILED)
    parsed = parse_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        exact_job_bytes=JOB_BYTES,
        raw_run=run,
        artifacts={item.artifact_id: archive.read(item) for item in run.artifacts},
        parsed_at=BASE + timedelta(seconds=3),
    )
    validation = validate_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        raw_run=run,
        parse_result=parsed,
        validated_at=BASE + timedelta(seconds=4),
    )
    assert parsed.candidate is None
    assert validation.disposition is SimulationValidationDisposition.REJECTED_EXECUTION


def test_gold_mismatch_is_distinct_from_numerical_quality_failure(tmp_path: Path):
    mismatched = protocol(lattice_constant=4.0)
    current_job = job()
    run, archive, _ = raw_run(tmp_path, protocol_value=mismatched)
    parsed = parse_ase_emt_simulation(
        protocol=mismatched,
        job=current_job,
        exact_job_bytes=JOB_BYTES,
        raw_run=run,
        artifacts={item.artifact_id: archive.read(item) for item in run.artifacts},
        parsed_at=BASE + timedelta(seconds=3),
    )
    validation = validate_ase_emt_simulation(
        protocol=mismatched,
        job=current_job,
        raw_run=run,
        parse_result=parsed,
        validated_at=BASE + timedelta(seconds=4),
    )
    assert validation.disposition is SimulationValidationDisposition.REJECTED_GOLD_MISMATCH


def test_changed_job_or_worker_is_rejected_before_execution():
    current_protocol = protocol()
    payload = job().model_dump(mode="json")
    payload["scan"]["volume_strain_fraction"] = 0.05
    with pytest.raises(ValueError, match="calculator or scan"):
        bind_ase_emt_job(
            protocol=current_protocol,
            job=AseEmtEosJob.model_validate(payload),
            exact_job_bytes=JOB_BYTES,
        )
    protocol_payload = current_protocol.model_dump(mode="json")
    protocol_payload["container"]["worker_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="worker implementation"):
        bind_ase_emt_job(
            protocol=AseEmtSimulationProtocol.model_validate(protocol_payload),
            job=job(),
            exact_job_bytes=JOB_BYTES,
        )


def test_validation_disposition_cannot_be_forged(tmp_path: Path):
    *_, validation = parse_and_validate(tmp_path)
    payload = validation.model_dump(mode="json")
    payload["disposition"] = "rejected_quality"
    with pytest.raises(ValidationError, match="disposition"):
        SimulationValidation.model_validate(payload)


def test_container_contract_is_digest_pinned_and_worker_source_bound():
    current_protocol = protocol()
    assert current_protocol.container.image_id.startswith("sha256:")
    assert (
        current_protocol.container.worker_sha256
        == hashlib.sha256((ROOT / "docker/simulation/emt_worker.py").read_bytes()).hexdigest()
    )


def test_frozen_v2_protocol_binds_v1_failure_lineage_and_current_sources():
    v1 = AseEmtSimulationProtocol.model_validate(
        yaml.safe_load(
            (ROOT / "configs/materials/f10_ase_emt_cu_fcc_eos_reference_v1.yaml").read_text()
        )
    )
    v2 = AseEmtSimulationProtocol.model_validate(
        yaml.safe_load(
            (ROOT / "configs/materials/f10_ase_emt_cu_fcc_eos_reference_v2.yaml").read_text()
        )
    )
    assert v1.protocol_sha256 == "e6245206b59dbfd64b8d5f203311f02d780f2d844e3e39d2cbb8e62122ee14f5"
    assert v2.protocol_sha256 == "d4e224336fd3a062839eb8ceaba01309aa3b5285a550237a5f3f0721158b22d5"
    assert v2.supersedes_protocol_sha256 == v1.protocol_sha256
    assert v2.correction_reason is not None
    assert v2.host_executor_implementation_sha256 == simulation_executor_implementation_sha256()
    assert v2.parser_implementation_sha256 == simulation_parser_implementation_sha256()
    assert v2.validator_implementation_sha256 == simulation_parser_implementation_sha256()
    assert v2.container.worker_sha256 == ase_emt_worker_sha256()
    assert v2.gold_reference.exact_job_bytes_sha256 == hashlib.sha256(JOB_BYTES).hexdigest()
    assert (
        v2.gold_reference.expected_result_payload_sha256
        == json.loads(RESULT_BYTES)["payload_sha256"]
    )


def test_worker_retains_unsupported_element_as_execution_failure(tmp_path: Path):
    payload = json.loads(JOB_BYTES)
    payload["structure"]["symbols"] = ["Xe"]
    input_path = tmp_path / "unsupported-element.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    output_path = tmp_path / "output"
    output_path.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "docker/simulation/emt_worker.py"),
            str(input_path),
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 1
    assert not (output_path / "result.json").exists()
    assert not (output_path / "checkpoint.json").exists()
    failure = json.loads((output_path / "failure.json").read_text())
    assert failure["error_type"] == "NotImplementedError"
    assert "Xe" in failure["message"]


def test_simulation_capability_is_discoverable_but_provisional_only(
    materials_registry_v4: CapabilityRegistrySnapshot,
):
    manifest = ExperimentCapabilityManifest.model_validate(
        yaml.safe_load(
            (
                ROOT / "configs/capabilities/materials_ase_emt_eos_reference_provisional_v1.yaml"
            ).read_text()
        )
    )
    assert manifest.manifest_sha256 == (
        "ff5507f8fd891b6fea3354a2e963da74f3c5bd0c8af1d49ac1340eea97931546"
    )
    assert manifest.lifecycle.value == "provisional"
    assert manifest.maximum_evidence_level.value == "exploratory"
    assert manifest.registration_evidence is None
    assert all(role.agent_authored for role in manifest.roles)
    assert manifest.roles[1].implementation_sha256 == simulation_executor_implementation_sha256()
    assert manifest.roles[2].implementation_sha256 == simulation_parser_implementation_sha256()
    assert manifest.roles[3].implementation_sha256 == simulation_parser_implementation_sha256()

    software_evidence = json.loads(
        (ROOT / "configs/capabilities/evidence/ase_emt_reference_software_v1.json").read_text()
    )
    minimum_evidence = json.loads(
        (
            ROOT / "configs/capabilities/evidence/ase_emt_reference_minimum_sample_v1.json"
        ).read_text()
    )
    assert manifest.license_egress_policy.license_evidence_sha256 == content_sha256(
        software_evidence
    )
    assert manifest.minimum_sample_rule.rule_sha256 == content_sha256(minimum_evidence)

    assert materials_registry_v4.snapshot_sha256 == (
        "80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77"
    )
    registry = CapabilityRegistry(materials_registry_v4)
    with pytest.raises(UnsupportedCapability, match="provisional"):
        registry.get(manifest.capability_id)
    assert registry.get(manifest.capability_id, allow_provisional=True) == manifest


def test_two_distinct_validated_runs_produce_an_honestly_bounded_reproduction_receipt(
    tmp_path: Path,
):
    current_protocol, current_job, source_run, source_parse, source_validation = parse_and_validate(
        tmp_path
    )
    source = assemble_ase_emt_simulation_bundle(
        protocol=current_protocol,
        job=current_job,
        raw_run=source_run,
        parse_result=source_parse,
        validation=source_validation,
    )
    replay_run = SimulationRawRun.model_validate(
        {
            **source_run.model_dump(mode="json"),
            "run_id": "gold-run-physical-replay",
            "started_at": BASE + timedelta(seconds=5),
            "ended_at": BASE + timedelta(seconds=6),
        }
    )
    archive = CapabilityObservationArchive(tmp_path / "archive")
    replay_parse = parse_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        exact_job_bytes=JOB_BYTES,
        raw_run=replay_run,
        artifacts={item.artifact_id: archive.read(item) for item in replay_run.artifacts},
        parsed_at=BASE + timedelta(seconds=7),
    )
    replay_validation = validate_ase_emt_simulation(
        protocol=current_protocol,
        job=current_job,
        raw_run=replay_run,
        parse_result=replay_parse,
        validated_at=BASE + timedelta(seconds=8),
    )
    replay = assemble_ase_emt_simulation_bundle(
        protocol=current_protocol,
        job=current_job,
        raw_run=replay_run,
        parse_result=replay_parse,
        validation=replay_validation,
    )
    receipt = compare_ase_emt_simulation_reproduction(
        source=source,
        replay=replay,
        compared_at=BASE + timedelta(seconds=9),
    )
    assert receipt.exact_result_payload_match is True
    assert receipt.source_bundle_sha256 != receipt.replay_bundle_sha256
    assert receipt.same_image_and_implementation_not_independent_replication is True
