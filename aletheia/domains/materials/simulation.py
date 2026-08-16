"""Typed, replayable ASE/EMT simulation evidence for F10-S5."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.observations import RawExperimentArtifact
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IMAGE_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _worker_payload_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(dict(value))).hexdigest()


class EmtCalculatorPolicy(FrozenModel):
    name: Literal["ase.emt"] = "ase.emt"
    asap_cutoff: Literal[False] = False


class EosScanPolicy(FrozenModel):
    eos_model: Literal["sj"] = "sj"
    points: int = Field(ge=5, le=31)
    volume_strain_fraction: float = Field(ge=0.005, le=0.20)

    @model_validator(mode="after")
    def _points_are_odd(self) -> "EosScanPolicy":
        if self.points % 2 != 1:
            raise ValueError("EOS point count must be odd")
        return self


class PeriodicSimulationStructure(FrozenModel):
    symbols: tuple[str, ...] = Field(min_length=1, max_length=256)
    positions_angstrom: tuple[tuple[float, float, float], ...] = Field(min_length=1)
    cell_angstrom: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    pbc: tuple[Literal[True], Literal[True], Literal[True]] = (True, True, True)

    @model_validator(mode="after")
    def _structure_is_finite_and_nondegenerate(self) -> "PeriodicSimulationStructure":
        import numpy as np
        from ase.data import atomic_numbers

        if len(self.symbols) != len(self.positions_angstrom):
            raise ValueError("simulation symbols and positions differ in length")
        if any(symbol not in atomic_numbers for symbol in self.symbols):
            raise ValueError("simulation contains an unknown element symbol")
        values = (
            *(value for row in self.positions_angstrom for value in row),
            *(value for row in self.cell_angstrom for value in row),
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("simulation structure contains a nonfinite value")
        if abs(float(np.linalg.det(np.asarray(self.cell_angstrom, dtype=float)))) <= 1e-6:
            raise ValueError("simulation cell must have nonzero volume")
        return self

    @property
    def structure_sha256(self) -> str:
        return content_sha256(self)


class AseEmtEosJob(FrozenModel):
    schema_name: Literal["aletheia.ase_emt_eos_job"] = "aletheia.ase_emt_eos_job"
    schema_version: Literal[1] = 1
    job_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    calculator: EmtCalculatorPolicy
    structure: PeriodicSimulationStructure
    scan: EosScanPolicy

    @property
    def job_sha256(self) -> str:
        return content_sha256(self)


class SimulationContainerPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    image_id: str = Field(pattern=_IMAGE_ID_PATTERN)
    image_tag: str = Field(min_length=1, max_length=256)
    platform: Literal["linux/arm64"] = "linux/arm64"
    worker_sha256: str = Field(pattern=_SHA256_PATTERN)
    init: Literal[True] = True
    network: Literal["none"] = "none"
    read_only_root: Literal[True] = True
    cap_drop: Literal["ALL"] = "ALL"
    no_new_privileges: Literal[True] = True
    user: Literal["65532:65532"] = "65532:65532"
    pids_limit: int = Field(ge=8, le=1024)
    memory_mebibytes: int = Field(ge=64, le=65_536)
    cpus: float = Field(gt=0, le=64)
    timeout_seconds: float = Field(gt=0, le=86_400)
    output_file_limit: int = Field(default=4, ge=1, le=64)
    output_bytes_limit: int = Field(default=8 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)


class SimulationQualityPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    require_fit_inside_scan: Literal[True] = True
    require_sample_minimum_interior: Literal[True] = True
    minimum_bulk_modulus_eV_per_angstrom3: float = Field(gt=0)
    maximum_bulk_modulus_eV_per_angstrom3: float = Field(gt=0)
    maximum_fit_rmse_eV: float = Field(gt=0)
    maximum_fit_absolute_residual_eV: float = Field(gt=0)

    @model_validator(mode="after")
    def _bulk_bounds_are_ordered(self) -> "SimulationQualityPolicy":
        if self.maximum_bulk_modulus_eV_per_angstrom3 <= self.minimum_bulk_modulus_eV_per_angstrom3:
            raise ValueError("simulation bulk-modulus bounds are not ordered")
        return self


class SimulationGoldReference(FrozenModel):
    schema_version: Literal[1] = 1
    reference_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    job_sha256: str = Field(pattern=_SHA256_PATTERN)
    exact_job_bytes_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_result_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    lattice_relation: Literal["fcc_conventional_a_from_primitive_volume"] = (
        "fcc_conventional_a_from_primitive_volume"
    )
    expected_lattice_constant_angstrom: float = Field(gt=0)
    lattice_absolute_tolerance_angstrom: float = Field(gt=0)
    official_reference_uri: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _reference_uri_is_absolute(self) -> "SimulationGoldReference":
        if ":" not in self.official_reference_uri:
            raise ValueError("simulation gold reference URI must be absolute")
        return self


class AseEmtSimulationProtocol(FrozenModel):
    schema_name: Literal["aletheia.ase_emt_simulation_protocol"] = (
        "aletheia.ase_emt_simulation_protocol"
    )
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    container: SimulationContainerPolicy
    calculator: EmtCalculatorPolicy
    scan: EosScanPolicy
    runtime_versions: dict[Literal["ase", "numpy", "scipy"], str] = Field(
        min_length=3, max_length=3
    )
    host_executor_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    quality: SimulationQualityPolicy
    gold_reference: SimulationGoldReference
    evidence_scope: Literal["classical_potential_reference_calibration"] = (
        "classical_potential_reference_calibration"
    )
    dft_claim_forbidden: Literal[True] = True
    experimental_claim_forbidden: Literal[True] = True
    mechanism_claim_forbidden: Literal[True] = True
    supersedes_protocol_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    correction_reason: str | None = Field(default=None, min_length=1, max_length=1024)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _runtime_contract_is_closed(self) -> "AseEmtSimulationProtocol":
        if set(self.runtime_versions) != {"ase", "numpy", "scipy"} or any(
            not value.strip() for value in self.runtime_versions.values()
        ):
            raise ValueError("simulation runtime-version contract is incomplete")
        if (self.supersedes_protocol_sha256 is None) != (self.correction_reason is None):
            raise ValueError("simulation protocol supersession requires hash and reason together")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


class SimulationExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OUTPUT_QUOTA_EXCEEDED = "output_quota_exceeded"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


class SimulationSecurityReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    init: Literal[True] = True
    network_none: Literal[True] = True
    read_only_root: Literal[True] = True
    all_capabilities_dropped: Literal[True] = True
    no_new_privileges: Literal[True] = True
    nonroot_user: Literal[True] = True
    pids_limit: int = Field(ge=8)
    memory_mebibytes: int = Field(ge=64)
    cpus: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


class SimulationRawRun(FrozenModel):
    schema_name: Literal["aletheia.simulation_raw_run"] = "aletheia.simulation_raw_run"
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    job_sha256: str = Field(pattern=_SHA256_PATTERN)
    exact_job_bytes_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_id: str = Field(pattern=_IMAGE_ID_PATTERN)
    worker_sha256: str = Field(pattern=_SHA256_PATTERN)
    executor_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    command_sha256: str = Field(pattern=_SHA256_PATTERN)
    security: SimulationSecurityReceipt
    status: SimulationExecutionStatus
    exit_code: int | None = None
    failure_kind: str | None = Field(default=None, min_length=1, max_length=128)
    artifacts: tuple[RawExperimentArtifact, ...] = Field(min_length=1)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    checkpoint_retained: bool
    state: Literal["raw_complete"] = "raw_complete"

    @model_validator(mode="after")
    def _run_is_closed(self) -> "SimulationRawRun":
        if self.ended_at < self.started_at:
            raise ValueError("simulation run ended before it started")
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        if artifact_ids != tuple(sorted(set(artifact_ids))):
            raise ValueError("simulation artifacts must have sorted unique IDs")
        artifact_set = set(artifact_ids)
        if self.status is SimulationExecutionStatus.SUCCEEDED:
            if self.exit_code != 0 or self.failure_kind is not None:
                raise ValueError("successful simulation has inconsistent execution status")
            if not {"checkpoint-json", "result-json", "stdout"}.issubset(artifact_set):
                raise ValueError("successful simulation omitted required raw artifacts")
            if not self.checkpoint_retained:
                raise ValueError("successful simulation must retain its checkpoint")
        elif self.failure_kind is None:
            raise ValueError("failed simulation must retain a failure kind")
        return self

    @property
    def run_sha256(self) -> str:
        return content_sha256(self)


class EosPointObservation(FrozenModel):
    index: int = Field(ge=0)
    volume_angstrom3: float = Field(gt=0)
    energy_eV: float
    maximum_force_eV_per_angstrom: float = Field(ge=0)
    maximum_absolute_stress_eV_per_angstrom3: float = Field(ge=0)

    @model_validator(mode="after")
    def _values_are_finite(self) -> "EosPointObservation":
        if any(
            not math.isfinite(value)
            for value in (
                self.volume_angstrom3,
                self.energy_eV,
                self.maximum_force_eV_per_angstrom,
                self.maximum_absolute_stress_eV_per_angstrom3,
            )
        ):
            raise ValueError("EOS observation contains a nonfinite value")
        return self


class AseEmtWorkerResult(FrozenModel):
    schema_name: Literal["aletheia.ase_emt_eos_result"] = "aletheia.ase_emt_eos_result"
    schema_version: Literal[1] = 1
    job_id: str
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_versions: dict[Literal["ase", "numpy", "scipy"], str]
    calculator: EmtCalculatorPolicy
    scan: EosScanPolicy
    site_count: int = Field(ge=1, le=256)
    evaluations: int = Field(ge=1, le=31)
    observations: tuple[EosPointObservation, ...] = Field(min_length=1, max_length=31)
    equilibrium_volume_angstrom3: float = Field(gt=0)
    equilibrium_volume_per_atom_angstrom3: float = Field(gt=0)
    minimum_energy_eV: float
    bulk_modulus_eV_per_angstrom3: float
    fit_rmse_eV: float = Field(ge=0)
    fit_maximum_absolute_residual_eV: float = Field(ge=0)
    fit_inside_scanned_volume_range: bool
    sample_minimum_is_interior: bool
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _result_is_self_authenticating(self) -> "AseEmtWorkerResult":
        numeric = (
            self.equilibrium_volume_angstrom3,
            self.equilibrium_volume_per_atom_angstrom3,
            self.minimum_energy_eV,
            self.bulk_modulus_eV_per_angstrom3,
            self.fit_rmse_eV,
            self.fit_maximum_absolute_residual_eV,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("EOS result contains a nonfinite value")
        if self.evaluations != len(self.observations):
            raise ValueError("EOS result evaluation count differs from observations")
        if tuple(item.index for item in self.observations) != tuple(range(self.evaluations)):
            raise ValueError("EOS observations must have complete ordered indices")
        payload = self.model_dump(mode="json", exclude={"payload_sha256"})
        if self.payload_sha256 != _worker_payload_sha256(payload):
            raise ValueError("EOS result payload hash is invalid")
        return self


class SimulationCandidateResult(FrozenModel):
    schema_name: Literal["aletheia.simulation_candidate_result"] = (
        "aletheia.simulation_candidate_result"
    )
    schema_version: Literal[1] = 1
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_artifact_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    checkpoint_artifact_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    result: AseEmtWorkerResult
    parsed_at: AwareDatetime

    @property
    def candidate_sha256(self) -> str:
        return content_sha256(self)


class SimulationParseFailure(FrozenModel):
    schema_version: Literal[1] = 1
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    failure_kind: str = Field(min_length=1, max_length=128)
    detail_sha256: str = Field(pattern=_SHA256_PATTERN)
    failed_at: AwareDatetime


class SimulationParseResult(FrozenModel):
    schema_name: Literal["aletheia.simulation_parse_result"] = "aletheia.simulation_parse_result"
    schema_version: Literal[1] = 1
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate: SimulationCandidateResult | None = None
    failure: SimulationParseFailure | None = None
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _parse_has_one_terminal_state(self) -> "SimulationParseResult":
        if (self.candidate is None) == (self.failure is None):
            raise ValueError("simulation parse requires exactly one terminal state")
        terminal = self.candidate.parsed_at if self.candidate else self.failure.failed_at  # type: ignore[union-attr]
        if self.completed_at < terminal:
            raise ValueError("simulation parse chronology is invalid")
        lineage = (
            self.candidate.raw_run_sha256
            if self.candidate is not None
            else self.failure.raw_run_sha256  # type: ignore[union-attr]
        )
        if lineage != self.raw_run_sha256:
            raise ValueError("simulation parse changed raw-run lineage")
        return self

    @property
    def parse_sha256(self) -> str:
        return content_sha256(self)


class SimulationValidationDisposition(str, Enum):
    VALIDATED_CLASSICAL_REFERENCE = "validated_classical_reference"
    REJECTED_EXECUTION = "rejected_execution"
    REJECTED_PARSE = "rejected_parse"
    REJECTED_QUALITY = "rejected_quality"
    REJECTED_GOLD_MISMATCH = "rejected_gold_mismatch"


class SimulationValidationCheck(FrozenModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    passed: bool
    detail_sha256: str = Field(pattern=_SHA256_PATTERN)


class SimulationValidation(FrozenModel):
    schema_name: Literal["aletheia.simulation_validation"] = "aletheia.simulation_validation"
    schema_version: Literal[1] = 1
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    job_sha256: str = Field(pattern=_SHA256_PATTERN)
    raw_run_sha256: str = Field(pattern=_SHA256_PATTERN)
    parse_sha256: str = Field(pattern=_SHA256_PATTERN)
    validator_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    checks: tuple[SimulationValidationCheck, ...] = Field(min_length=2)
    all_required_checks_passed: bool
    disposition: SimulationValidationDisposition
    claim_ceiling: Literal["classical_potential_reference_calibration"] = (
        "classical_potential_reference_calibration"
    )
    validated_at: AwareDatetime

    @model_validator(mode="after")
    def _validation_is_derived(self) -> "SimulationValidation":
        ids = tuple(item.check_id for item in self.checks)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("simulation validation checks must be unique and sorted")
        passed = all(item.passed for item in self.checks)
        if self.all_required_checks_passed != passed:
            raise ValueError("simulation validation aggregate is not derived")
        if passed != (
            self.disposition is SimulationValidationDisposition.VALIDATED_CLASSICAL_REFERENCE
        ):
            raise ValueError("simulation validation disposition is inconsistent with checks")
        return self

    @property
    def validation_sha256(self) -> str:
        return content_sha256(self)


class AseEmtSimulationBundle(FrozenModel):
    schema_name: Literal["aletheia.ase_emt_simulation_bundle"] = (
        "aletheia.ase_emt_simulation_bundle"
    )
    schema_version: Literal[1] = 1
    protocol: AseEmtSimulationProtocol
    job: AseEmtEosJob
    raw_run: SimulationRawRun
    parse_result: SimulationParseResult
    validation: SimulationValidation

    @model_validator(mode="after")
    def _bundle_lineage_is_exact(self) -> "AseEmtSimulationBundle":
        if (
            self.raw_run.protocol_sha256 != self.protocol.protocol_sha256
            or self.raw_run.job_sha256 != self.job.job_sha256
            or self.parse_result.raw_run_sha256 != self.raw_run.run_sha256
            or self.validation.protocol_sha256 != self.protocol.protocol_sha256
            or self.validation.job_sha256 != self.job.job_sha256
            or self.validation.raw_run_sha256 != self.raw_run.run_sha256
            or self.validation.parse_sha256 != self.parse_result.parse_sha256
        ):
            raise ValueError("simulation bundle changed evidence lineage")
        return self

    @property
    def bundle_sha256(self) -> str:
        return content_sha256(self)


class SimulationReproductionReceipt(FrozenModel):
    schema_name: Literal["aletheia.simulation_reproduction_receipt"] = (
        "aletheia.simulation_reproduction_receipt"
    )
    schema_version: Literal[1] = 1
    source_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    replay_bundle_sha256: str = Field(pattern=_SHA256_PATTERN)
    protocol_sha256: str = Field(pattern=_SHA256_PATTERN)
    job_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_id: str = Field(pattern=_IMAGE_ID_PATTERN)
    source_result_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    replay_result_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    exact_result_payload_match: Literal[True] = True
    both_runs_independently_validated: Literal[True] = True
    same_image_and_implementation_not_independent_replication: Literal[True] = True
    compared_at: AwareDatetime

    @model_validator(mode="after")
    def _reproduction_is_nonvacuous(self) -> "SimulationReproductionReceipt":
        if self.source_bundle_sha256 == self.replay_bundle_sha256:
            raise ValueError("simulation reproduction must compare two distinct run bundles")
        if self.source_result_payload_sha256 != self.replay_result_payload_sha256:
            raise ValueError("simulation reproduction result payloads differ")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def ase_emt_worker_sha256() -> str:
    path = Path(__file__).resolve().parents[3] / "docker/simulation/emt_worker.py"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def simulation_parser_implementation_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def simulation_executor_implementation_sha256() -> str:
    path = Path(__file__).resolve().parents[3] / "scripts/ase_emt_simulation_e2e.py"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_ase_emt_job(
    *,
    protocol: AseEmtSimulationProtocol,
    job: AseEmtEosJob,
    exact_job_bytes: bytes,
) -> None:
    if protocol.container.worker_sha256 != ase_emt_worker_sha256():
        raise ValueError("simulation protocol froze another worker implementation")
    if (
        protocol.host_executor_implementation_sha256 != simulation_executor_implementation_sha256()
        or protocol.parser_implementation_sha256 != simulation_parser_implementation_sha256()
        or protocol.validator_implementation_sha256 != simulation_parser_implementation_sha256()
    ):
        raise ValueError("simulation protocol froze another host implementation")
    if job.calculator != protocol.calculator or job.scan != protocol.scan:
        raise ValueError("simulation job changed the frozen calculator or scan")
    exact_hash = hashlib.sha256(exact_job_bytes).hexdigest()
    if (
        job.job_sha256 != protocol.gold_reference.job_sha256
        or exact_hash != protocol.gold_reference.exact_job_bytes_sha256
    ):
        raise ValueError("simulation job differs from the frozen gold input")


def _artifact_map(raw_run: SimulationRawRun) -> dict[str, RawExperimentArtifact]:
    return {item.artifact_id: item for item in raw_run.artifacts}


def _checked_artifact(*, receipt: RawExperimentArtifact, payload: bytes) -> bytes:
    if len(payload) != receipt.bytes or hashlib.sha256(payload).hexdigest() != receipt.sha256:
        raise ValueError("simulation artifact bytes differ from raw receipt")
    return payload


def parse_ase_emt_simulation(
    *,
    protocol: AseEmtSimulationProtocol,
    job: AseEmtEosJob,
    exact_job_bytes: bytes,
    raw_run: SimulationRawRun,
    artifacts: Mapping[str, bytes],
    parsed_at: datetime,
) -> SimulationParseResult:
    """Parse only retained bytes; execution failures remain explicit parse failures."""

    bind_ase_emt_job(protocol=protocol, job=job, exact_job_bytes=exact_job_bytes)
    expected_security = SimulationSecurityReceipt(
        pids_limit=protocol.container.pids_limit,
        memory_mebibytes=protocol.container.memory_mebibytes,
        cpus=protocol.container.cpus,
        timeout_seconds=protocol.container.timeout_seconds,
    )
    if (
        raw_run.protocol_sha256 != protocol.protocol_sha256
        or raw_run.job_sha256 != job.job_sha256
        or raw_run.exact_job_bytes_sha256 != hashlib.sha256(exact_job_bytes).hexdigest()
        or raw_run.image_id != protocol.container.image_id
        or raw_run.worker_sha256 != protocol.container.worker_sha256
        or raw_run.executor_implementation_sha256 != protocol.host_executor_implementation_sha256
        or raw_run.security != expected_security
        or raw_run.started_at <= protocol.frozen_at
    ):
        raise ValueError("simulation raw run differs from frozen protocol/job")
    receipts = _artifact_map(raw_run)
    try:
        if raw_run.status is not SimulationExecutionStatus.SUCCEEDED:
            raise RuntimeError(f"execution_{raw_run.status.value}")
        result_receipt = receipts["result-json"]
        checkpoint_receipt = receipts["checkpoint-json"]
        result_bytes = _checked_artifact(receipt=result_receipt, payload=artifacts["result-json"])
        checkpoint_bytes = _checked_artifact(
            receipt=checkpoint_receipt, payload=artifacts["checkpoint-json"]
        )
        result = AseEmtWorkerResult.model_validate_json(result_bytes)
        checkpoint = json.loads(checkpoint_bytes)
        if (
            result.job_id != job.job_id
            or result.input_sha256 != hashlib.sha256(exact_job_bytes).hexdigest()
            or checkpoint.get("job_id") != job.job_id
            or checkpoint.get("input_sha256") != result.input_sha256
            or checkpoint.get("completed_evaluations") != result.evaluations
            or checkpoint.get("observations")
            != [item.model_dump(mode="json") for item in result.observations]
        ):
            raise ValueError("simulation result/checkpoint lineage differs from exact job")
        candidate = SimulationCandidateResult(
            raw_run_sha256=raw_run.run_sha256,
            result_artifact_receipt_sha256=result_receipt.receipt_sha256,
            checkpoint_artifact_receipt_sha256=checkpoint_receipt.receipt_sha256,
            parser_implementation_sha256=simulation_parser_implementation_sha256(),
            result=result,
            parsed_at=parsed_at,
        )
        return SimulationParseResult(
            raw_run_sha256=raw_run.run_sha256,
            candidate=candidate,
            completed_at=parsed_at,
        )
    except Exception as error:
        failure = SimulationParseFailure(
            raw_run_sha256=raw_run.run_sha256,
            failure_kind=(
                "execution_not_succeeded"
                if raw_run.status is not SimulationExecutionStatus.SUCCEEDED
                else "invalid_simulation_output"
            ),
            detail_sha256=hashlib.sha256(
                f"{type(error).__name__}:{str(error)[:1000]}".encode()
            ).hexdigest(),
            failed_at=parsed_at,
        )
        return SimulationParseResult(
            raw_run_sha256=raw_run.run_sha256,
            failure=failure,
            completed_at=parsed_at,
        )


def _check(check_id: str, passed: bool, detail: object) -> SimulationValidationCheck:
    return SimulationValidationCheck(
        check_id=check_id,
        passed=passed,
        detail_sha256=content_sha256(detail),
    )


def validate_ase_emt_simulation(
    *,
    protocol: AseEmtSimulationProtocol,
    job: AseEmtEosJob,
    raw_run: SimulationRawRun,
    parse_result: SimulationParseResult,
    validated_at: datetime,
) -> SimulationValidation:
    """Independently derive convergence, gold agreement, and the bounded claim ceiling."""

    candidate = parse_result.candidate
    if (
        raw_run.protocol_sha256 != protocol.protocol_sha256
        or raw_run.job_sha256 != job.job_sha256
        or parse_result.raw_run_sha256 != raw_run.run_sha256
        or (
            candidate is not None
            and candidate.parser_implementation_sha256 != protocol.parser_implementation_sha256
        )
        or protocol.validator_implementation_sha256 != simulation_parser_implementation_sha256()
    ):
        raise ValueError("simulation validator received mismatched evidence lineage")
    execution_ok = raw_run.status is SimulationExecutionStatus.SUCCEEDED
    parse_ok = candidate is not None
    checks = [
        _check("execution_succeeded", execution_ok, raw_run.status.value),
        _check("parser_succeeded", parse_ok, parse_result.parse_sha256),
    ]
    gold_ok = False
    if candidate is not None:
        result = candidate.result
        volumes = tuple(item.volume_angstrom3 for item in result.observations)
        runtime_ok = result.runtime_versions == protocol.runtime_versions
        calculator_ok = result.calculator == protocol.calculator and result.scan == protocol.scan
        complete = result.evaluations == protocol.scan.points
        monotonic = all(left < right for left, right in zip(volumes, volumes[1:]))
        bulk_ok = (
            protocol.quality.minimum_bulk_modulus_eV_per_angstrom3
            <= result.bulk_modulus_eV_per_angstrom3
            <= protocol.quality.maximum_bulk_modulus_eV_per_angstrom3
        )
        residual_ok = (
            result.fit_rmse_eV <= protocol.quality.maximum_fit_rmse_eV
            and result.fit_maximum_absolute_residual_eV
            <= protocol.quality.maximum_fit_absolute_residual_eV
        )
        lattice = (4.0 * result.equilibrium_volume_per_atom_angstrom3) ** (1.0 / 3.0)
        gold_ok = (
            abs(lattice - protocol.gold_reference.expected_lattice_constant_angstrom)
            <= protocol.gold_reference.lattice_absolute_tolerance_angstrom
            and result.payload_sha256 == protocol.gold_reference.expected_result_payload_sha256
        )
        checks.extend(
            (
                _check("bulk_modulus_in_policy", bulk_ok, result.bulk_modulus_eV_per_angstrom3),
                _check("calculator_and_scan_exact", calculator_ok, result.calculator),
                _check("evaluation_count_complete", complete, result.evaluations),
                _check("fit_inside_scan", result.fit_inside_scanned_volume_range, volumes),
                _check(
                    "fit_residual_in_policy",
                    residual_ok,
                    {
                        "rmse": result.fit_rmse_eV,
                        "maximum": result.fit_maximum_absolute_residual_eV,
                    },
                ),
                _check(
                    "gold_reference_exact",
                    gold_ok,
                    {"lattice": lattice, "payload_sha256": result.payload_sha256},
                ),
                _check("runtime_versions_exact", runtime_ok, result.runtime_versions),
                _check(
                    "sample_minimum_interior",
                    result.sample_minimum_is_interior,
                    result.sample_minimum_is_interior,
                ),
                _check("volumes_strictly_increasing", monotonic, volumes),
            )
        )
    checks_tuple = tuple(sorted(checks, key=lambda item: item.check_id))
    all_passed = all(item.passed for item in checks_tuple)
    if all_passed:
        disposition = SimulationValidationDisposition.VALIDATED_CLASSICAL_REFERENCE
    elif not execution_ok:
        disposition = SimulationValidationDisposition.REJECTED_EXECUTION
    elif not parse_ok:
        disposition = SimulationValidationDisposition.REJECTED_PARSE
    elif not gold_ok:
        disposition = SimulationValidationDisposition.REJECTED_GOLD_MISMATCH
    else:
        disposition = SimulationValidationDisposition.REJECTED_QUALITY
    return SimulationValidation(
        protocol_sha256=protocol.protocol_sha256,
        job_sha256=job.job_sha256,
        raw_run_sha256=raw_run.run_sha256,
        parse_sha256=parse_result.parse_sha256,
        validator_implementation_sha256=simulation_parser_implementation_sha256(),
        checks=checks_tuple,
        all_required_checks_passed=all_passed,
        disposition=disposition,
        validated_at=validated_at,
    )


def assemble_ase_emt_simulation_bundle(
    *,
    protocol: AseEmtSimulationProtocol,
    job: AseEmtEosJob,
    raw_run: SimulationRawRun,
    parse_result: SimulationParseResult,
    validation: SimulationValidation,
) -> AseEmtSimulationBundle:
    return AseEmtSimulationBundle(
        protocol=protocol,
        job=job,
        raw_run=raw_run,
        parse_result=parse_result,
        validation=validation,
    )


def compare_ase_emt_simulation_reproduction(
    *,
    source: AseEmtSimulationBundle,
    replay: AseEmtSimulationBundle,
    compared_at: datetime,
) -> SimulationReproductionReceipt:
    """Compare two distinct physical runs without calling them independent implementations."""

    if (
        source.protocol != replay.protocol
        or source.job != replay.job
        or source.raw_run.image_id != replay.raw_run.image_id
        or source.validation.disposition
        is not SimulationValidationDisposition.VALIDATED_CLASSICAL_REFERENCE
        or replay.validation.disposition
        is not SimulationValidationDisposition.VALIDATED_CLASSICAL_REFERENCE
        or source.parse_result.candidate is None
        or replay.parse_result.candidate is None
    ):
        raise ValueError("simulation reproduction inputs are not comparable validated runs")
    source_payload = source.parse_result.candidate.result.payload_sha256
    replay_payload = replay.parse_result.candidate.result.payload_sha256
    if source_payload != replay_payload:
        raise ValueError("simulation physical replay changed the result payload")
    return SimulationReproductionReceipt(
        source_bundle_sha256=source.bundle_sha256,
        replay_bundle_sha256=replay.bundle_sha256,
        protocol_sha256=source.protocol.protocol_sha256,
        job_sha256=source.job.job_sha256,
        image_id=source.raw_run.image_id,
        source_result_payload_sha256=source_payload,
        replay_result_payload_sha256=replay_payload,
        compared_at=compared_at,
    )


__all__ = [
    "AseEmtEosJob",
    "AseEmtSimulationBundle",
    "AseEmtSimulationProtocol",
    "AseEmtWorkerResult",
    "EmtCalculatorPolicy",
    "EosPointObservation",
    "EosScanPolicy",
    "PeriodicSimulationStructure",
    "SimulationCandidateResult",
    "SimulationContainerPolicy",
    "SimulationExecutionStatus",
    "SimulationGoldReference",
    "SimulationParseFailure",
    "SimulationParseResult",
    "SimulationQualityPolicy",
    "SimulationRawRun",
    "SimulationReproductionReceipt",
    "SimulationSecurityReceipt",
    "SimulationValidation",
    "SimulationValidationCheck",
    "SimulationValidationDisposition",
    "ase_emt_worker_sha256",
    "assemble_ase_emt_simulation_bundle",
    "bind_ase_emt_job",
    "compare_ase_emt_simulation_reproduction",
    "parse_ase_emt_simulation",
    "simulation_parser_implementation_sha256",
    "simulation_executor_implementation_sha256",
    "validate_ase_emt_simulation",
]
