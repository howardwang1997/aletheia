"""Typed raw-to-candidate observation boundary for F10 experiment capabilities."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.schemas import CapabilityRole, ExperimentCapabilityManifest
from aletheia.evals.schemas import FrozenModel
from aletheia.reproducibility.manifest import content_sha256


class CapabilityObservationArchiveError(RuntimeError):
    """Raw capability evidence is missing, corrupt, oversized, or unsafe."""


class ExperimentRunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class ExperimentRunPurpose(str, Enum):
    MEASUREMENT = "measurement"
    EXACT_REEXECUTION = "exact_reexecution"
    PARSER_FIXTURE = "parser_fixture"


class ScientificOutcomeClass(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    INCONCLUSIVE = "inconclusive"
    NOT_EVALUABLE = "not_evaluable"


class UncertaintyKind(str, Enum):
    STANDARD = "standard_uncertainty"
    EXPANDED = "expanded_uncertainty"
    CONFIDENCE_INTERVAL = "confidence_interval"
    CREDIBLE_INTERVAL = "credible_interval"
    NOT_QUANTIFIED = "not_quantified"


class RawExperimentArtifact(FrozenModel):
    schema_name: Literal["aletheia.raw_experiment_artifact"] = "aletheia.raw_experiment_artifact"
    schema_version: Literal[1] = 1
    artifact_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=1, le=256 * 1024 * 1024)
    media_type: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(pattern=r"^raw/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.artifact$")
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def _path_matches_content(self) -> "RawExperimentArtifact":
        expected = f"raw/{self.sha256[:2]}/{self.sha256[2:4]}/{self.sha256}.artifact"
        if self.relative_path != expected:
            raise ValueError("raw artifact path does not match its content hash")
        if self.media_type != self.media_type.strip() or any(
            character in self.media_type for character in "\r\n"
        ):
            raise ValueError("raw artifact media type is not canonical")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class ExperimentExecutionFailure(FrozenModel):
    schema_version: Literal[1] = 1
    failure_kind: str = Field(min_length=1, max_length=128)
    detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_failure_artifact_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _failure_artifacts_are_canonical(self) -> "ExperimentExecutionFailure":
        if self.raw_failure_artifact_ids != tuple(sorted(set(self.raw_failure_artifact_ids))):
            raise ValueError("execution failure artifact IDs must be unique and sorted")
        return self


class RawExperimentRun(FrozenModel):
    schema_name: Literal["aletheia.raw_experiment_run"] = "aletheia.raw_experiment_run"
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    capability_id: str
    capability_version: str
    capability_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preregistration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_adapter_ref: str
    executor_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executor_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_number: Literal[1] = 1
    run_purpose: ExperimentRunPurpose = ExperimentRunPurpose.MEASUREMENT
    status: ExperimentRunStatus
    exit_code: int | None = None
    artifacts: tuple[RawExperimentArtifact, ...] = Field(min_length=1)
    failure: ExperimentExecutionFailure | None = None
    started_at: AwareDatetime
    ended_at: AwareDatetime
    state: Literal["raw_complete"] = "raw_complete"

    @model_validator(mode="after")
    def _run_is_complete_and_failure_preserving(self) -> "RawExperimentRun":
        if self.ended_at < self.started_at:
            raise ValueError("raw experiment run ended before it started")
        artifact_ids = tuple(item.artifact_id for item in self.artifacts)
        if artifact_ids != tuple(sorted(set(artifact_ids))):
            raise ValueError("raw experiment artifacts must use canonical unique IDs")
        if self.status is ExperimentRunStatus.SUCCEEDED:
            if self.failure is not None:
                raise ValueError("successful raw run cannot carry an execution failure")
            if self.exit_code not in {None, 0}:
                raise ValueError("successful raw run cannot carry a nonzero exit code")
        else:
            if self.failure is None:
                raise ValueError("non-success raw run must retain an execution failure")
            missing = set(self.failure.raw_failure_artifact_ids) - set(artifact_ids)
            if missing:
                raise ValueError("execution failure references missing raw artifacts")
        return self

    @property
    def run_sha256(self) -> str:
        return content_sha256(self)


class MeasurementUncertainty(FrozenModel):
    schema_version: Literal[1] = 1
    kind: UncertaintyKind
    value: float | None = None
    lower: float | None = None
    upper: float | None = None
    coverage_probability: float | None = Field(default=None, gt=0, lt=1)
    coverage_factor: float | None = Field(default=None, gt=0)
    method_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    not_quantified_reason: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _uncertainty_shape_matches_kind(self) -> "MeasurementUncertainty":
        numbers = (self.value, self.lower, self.upper, self.coverage_factor)
        if any(value is not None and not math.isfinite(value) for value in numbers):
            raise ValueError("measurement uncertainty values must be finite")
        interval = self.kind in {
            UncertaintyKind.CONFIDENCE_INTERVAL,
            UncertaintyKind.CREDIBLE_INTERVAL,
        }
        if interval:
            if (
                self.lower is None
                or self.upper is None
                or self.lower > self.upper
                or self.coverage_probability is None
                or self.value is not None
                or self.coverage_factor is not None
                or self.not_quantified_reason is not None
            ):
                raise ValueError("interval uncertainty requires bounds and coverage probability")
        elif self.kind is UncertaintyKind.STANDARD:
            if (
                self.value is None
                or self.value < 0
                or self.lower is not None
                or self.upper is not None
                or self.coverage_probability is not None
                or self.coverage_factor is not None
                or self.not_quantified_reason is not None
            ):
                raise ValueError("standard uncertainty requires one nonnegative value")
        elif self.kind is UncertaintyKind.EXPANDED:
            if (
                self.value is None
                or self.value < 0
                or self.coverage_factor is None
                or self.lower is not None
                or self.upper is not None
                or self.not_quantified_reason is not None
            ):
                raise ValueError("expanded uncertainty requires a value and coverage factor")
        elif (
            any(value is not None for value in numbers[:3])
            or any(
                value is not None
                for value in (self.coverage_probability, self.coverage_factor, self.method_sha256)
            )
            or self.not_quantified_reason is None
        ):
            raise ValueError("unquantified uncertainty requires only an explicit reason")
        return self


class MeasuredQuantity(FrozenModel):
    schema_version: Literal[1] = 1
    measurement_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    quantity_kind_id: str = Field(min_length=1, max_length=256)
    value: float
    unit_ucum: str = Field(pattern=r"^[!-~]{1,64}$")
    uncertainty: MeasurementUncertainty
    sample_count: int = Field(ge=1, le=1_000_000_000)
    raw_artifact_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _quantity_is_finite_and_canonical(self) -> "MeasuredQuantity":
        if not math.isfinite(self.value):
            raise ValueError("measured quantity value must be finite")
        if self.raw_artifact_ids != tuple(sorted(set(self.raw_artifact_ids))):
            raise ValueError("measured quantity raw artifact IDs must be unique and sorted")
        return self


class ObservationCondition(FrozenModel):
    schema_version: Literal[1] = 1
    condition_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    quantity_kind_id: str | None = Field(default=None, min_length=1, max_length=256)
    numeric_value: float | None = None
    unit_ucum: str | None = Field(default=None, pattern=r"^[!-~]{1,64}$")
    categorical_value: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _condition_has_one_typed_value(self) -> "ObservationCondition":
        quantitative = self.numeric_value is not None
        categorical = self.categorical_value is not None
        if quantitative == categorical:
            raise ValueError("condition requires exactly one quantitative or categorical value")
        if quantitative:
            if (
                not math.isfinite(self.numeric_value)  # type: ignore[arg-type]
                or self.quantity_kind_id is None
                or self.unit_ucum is None
            ):
                raise ValueError("quantitative condition requires finite value, kind, and unit")
        elif self.quantity_kind_id is not None or self.unit_ucum is not None:
            raise ValueError("categorical condition cannot carry quantity/unit fields")
        return self


class ObservationContext(FrozenModel):
    schema_version: Literal[1] = 1
    measurement_method_id: str = Field(min_length=1, max_length=256)
    conditions: tuple[ObservationCondition, ...] = Field(min_length=1)
    sample_id: str | None = Field(default=None, min_length=1, max_length=256)
    batch_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _conditions_are_canonical(self) -> "ObservationContext":
        condition_ids = tuple(item.condition_id for item in self.conditions)
        if condition_ids != tuple(sorted(set(condition_ids))):
            raise ValueError("observation conditions must be unique and sorted")
        return self


class ParsedObservationPayload(FrozenModel):
    """Untrusted typed parser output; the harness adds immutable lineage fields."""

    schema_version: Literal[1] = 1
    scientific_outcome: ScientificOutcomeClass
    measurements: tuple[MeasuredQuantity, ...] = ()
    context: ObservationContext | None = None
    execution_failure_acknowledged: bool = False
    parser_warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _payload_is_canonical(self) -> "ParsedObservationPayload":
        measurement_ids = tuple(item.measurement_id for item in self.measurements)
        if measurement_ids != tuple(sorted(set(measurement_ids))):
            raise ValueError("parsed measurement IDs must be unique and sorted")
        if self.parser_warnings != tuple(sorted(set(self.parser_warnings))):
            raise ValueError("parser warnings must be unique and sorted")
        return self


class CandidateCapabilityObservation(FrozenModel):
    schema_name: Literal["aletheia.candidate_capability_observation"] = (
        "aletheia.candidate_capability_observation"
    )
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    capability_id: str
    capability_version: str
    capability_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_artifact_receipt_sha256s: tuple[str, ...] = Field(min_length=1)
    parser_adapter_ref: str
    parser_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_purpose: ExperimentRunPurpose
    run_status: ExperimentRunStatus
    scientific_outcome: ScientificOutcomeClass
    measurements: tuple[MeasuredQuantity, ...] = ()
    context: ObservationContext | None = None
    execution_failure_acknowledged: bool = False
    parser_warnings: tuple[str, ...] = ()
    parsed_at: AwareDatetime
    state: Literal["candidate_unvalidated"] = "candidate_unvalidated"

    @model_validator(mode="after")
    def _candidate_preserves_failure_and_measurement_semantics(
        self,
    ) -> "CandidateCapabilityObservation":
        if self.raw_artifact_receipt_sha256s != tuple(
            sorted(set(self.raw_artifact_receipt_sha256s))
        ):
            raise ValueError("candidate raw artifact receipts must be unique and sorted")
        if self.parser_warnings != tuple(sorted(set(self.parser_warnings))):
            raise ValueError("candidate parser warnings must be unique and sorted")
        if self.run_status is ExperimentRunStatus.SUCCEEDED:
            if self.execution_failure_acknowledged:
                raise ValueError("successful candidate cannot acknowledge execution failure")
            if self.scientific_outcome is ScientificOutcomeClass.NOT_EVALUABLE:
                raise ValueError("successful run must classify its scientific outcome")
            if not self.measurements or self.context is None:
                raise ValueError("successful candidate requires measurements and conditions")
        elif (
            not self.execution_failure_acknowledged
            or self.scientific_outcome is not ScientificOutcomeClass.NOT_EVALUABLE
            or self.measurements
            or self.context is not None
        ):
            raise ValueError("failed execution must be retained as a non-evaluable candidate")
        artifact_ids = {
            artifact_id
            for measurement in self.measurements
            for artifact_id in measurement.raw_artifact_ids
        }
        if self.run_status is ExperimentRunStatus.SUCCEEDED and not artifact_ids:
            raise ValueError("candidate measurements must retain raw artifact lineage")
        return self

    @property
    def candidate_sha256(self) -> str:
        return content_sha256(self)


class ObservationParsingFailure(FrozenModel):
    schema_name: Literal["aletheia.observation_parsing_failure"] = (
        "aletheia.observation_parsing_failure"
    )
    schema_version: Literal[1] = 1
    raw_run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_artifact_receipt_sha256s: tuple[str, ...] = Field(min_length=1)
    parser_adapter_ref: str
    parser_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_at: AwareDatetime
    raw_artifacts_retained: Literal[True] = True

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class ObservationParseResult(FrozenModel):
    schema_name: Literal["aletheia.observation_parse_result"] = "aletheia.observation_parse_result"
    schema_version: Literal[1] = 1
    raw_run: RawExperimentRun
    candidate: CandidateCapabilityObservation | None = None
    failure: ObservationParsingFailure | None = None
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _result_has_exactly_one_terminal_output(self) -> "ObservationParseResult":
        if (self.candidate is None) == (self.failure is None):
            raise ValueError("parse result requires exactly one candidate or failure")
        receipt_hashes = tuple(sorted(item.receipt_sha256 for item in self.raw_run.artifacts))
        raw_artifact_ids = {item.artifact_id for item in self.raw_run.artifacts}
        if self.candidate is not None:
            candidate = self.candidate
            if (
                candidate.raw_run_sha256 != self.raw_run.run_sha256
                or candidate.raw_artifact_receipt_sha256s != receipt_hashes
                or candidate.run_status is not self.raw_run.status
                or candidate.run_purpose is not self.raw_run.run_purpose
                or candidate.capability_id != self.raw_run.capability_id
                or candidate.capability_version != self.raw_run.capability_version
                or candidate.capability_manifest_sha256 != self.raw_run.capability_manifest_sha256
            ):
                raise ValueError("candidate observation changed raw-run lineage")
            referenced = {
                artifact_id
                for measurement in candidate.measurements
                for artifact_id in measurement.raw_artifact_ids
            }
            if not referenced.issubset(raw_artifact_ids):
                raise ValueError("candidate observation references unknown raw artifacts")
            output = ParsedObservationPayload(
                scientific_outcome=candidate.scientific_outcome,
                measurements=candidate.measurements,
                context=candidate.context,
                execution_failure_acknowledged=(candidate.execution_failure_acknowledged),
                parser_warnings=candidate.parser_warnings,
            )
            expected_execution = content_sha256(
                {
                    "raw_run_sha256": self.raw_run.run_sha256,
                    "parser_implementation_sha256": candidate.parser_implementation_sha256,
                    "output": output.model_dump(mode="json"),
                }
            )
            if candidate.parser_execution_sha256 != expected_execution:
                raise ValueError("candidate parser execution hash is invalid")
        else:
            failure = self.failure
            if (
                failure.raw_run_sha256 != self.raw_run.run_sha256
                or failure.raw_artifact_receipt_sha256s != receipt_hashes
            ):
                raise ValueError("parser failure changed raw-run lineage")
        terminal_at = (
            self.candidate.parsed_at if self.candidate is not None else self.failure.failed_at
        )
        if terminal_at < self.raw_run.ended_at or self.completed_at < terminal_at:
            raise ValueError("observation parse chronology is invalid")
        return self

    @property
    def result_sha256(self) -> str:
        return content_sha256(self)


class ObservationParserAdapter(Protocol):
    adapter_ref: str
    implementation_sha256: str
    principal_sha256: str

    def parse(
        self, *, raw_run: RawExperimentRun, artifacts: Mapping[str, bytes]
    ) -> ParsedObservationPayload | Mapping[str, object]: ...


class CapabilityObservationArchive:
    """Write-once arbitrary raw artifact storage with rehashed, no-symlink reads."""

    def __init__(self, root: Path, *, max_artifact_bytes: int = 256 * 1024 * 1024) -> None:
        if max_artifact_bytes < 1 or max_artifact_bytes > 256 * 1024 * 1024:
            raise ValueError("raw artifact limit must be between 1 byte and 256 MiB")
        candidate = Path(root)
        if candidate.is_symlink():
            raise CapabilityObservationArchiveError("observation archive root is a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise CapabilityObservationArchiveError("observation archive root is unsafe")
        self.root = candidate.resolve(strict=True)
        self.max_artifact_bytes = max_artifact_bytes

    def _path(self, digest: str) -> tuple[str, Path]:
        relative = f"raw/{digest[:2]}/{digest[2:4]}/{digest}.artifact"
        target = self.root.joinpath(*Path(relative).parts)
        if self.root not in target.parents:
            raise CapabilityObservationArchiveError("raw artifact path escapes archive")
        return relative, target

    def _read_exact(self, receipt: RawExperimentArtifact) -> bytes:
        if receipt.bytes > self.max_artifact_bytes:
            raise CapabilityObservationArchiveError("raw artifact exceeds archive policy")
        _, target = self._path(receipt.sha256)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise CapabilityObservationArchiveError("raw artifact is missing or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != receipt.bytes:
                raise CapabilityObservationArchiveError("raw artifact metadata changed")
            chunks: list[bytes] = []
            remaining = receipt.bytes
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise CapabilityObservationArchiveError("raw artifact ended unexpectedly")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise CapabilityObservationArchiveError("raw artifact exceeds receipt size")
        finally:
            os.close(descriptor)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != receipt.sha256:
            raise CapabilityObservationArchiveError("raw artifact content hash changed")
        return payload

    def store(
        self,
        *,
        artifact_id: str,
        payload: bytes,
        media_type: str,
        captured_at: datetime,
    ) -> RawExperimentArtifact:
        if not payload or len(payload) > self.max_artifact_bytes:
            raise CapabilityObservationArchiveError("raw artifact is empty or oversized")
        if (
            not media_type
            or media_type != media_type.strip()
            or len(media_type) > 256
            or any(character in media_type for character in "\r\n")
        ):
            raise CapabilityObservationArchiveError("raw artifact media type is invalid")
        digest = hashlib.sha256(payload).hexdigest()
        relative, target = self._path(digest)
        current = self.root
        for part in target.parent.relative_to(self.root).parts:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if current.is_symlink() or not current.is_dir():
                raise CapabilityObservationArchiveError("raw archive contains unsafe directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o400)
        except FileExistsError:
            receipt = RawExperimentArtifact(
                artifact_id=artifact_id,
                sha256=digest,
                bytes=len(payload),
                media_type=media_type,
                relative_path=relative,
                captured_at=captured_at,
            )
            if self._read_exact(receipt) != payload:
                raise CapabilityObservationArchiveError("existing raw artifact differs")
            return receipt
        except OSError as exc:
            raise CapabilityObservationArchiveError("raw archive refused artifact") from exc
        committed = False
        try:
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise CapabilityObservationArchiveError("raw artifact write stalled")
                written += count
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            committed = True
        finally:
            os.close(descriptor)
            if not committed:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        receipt = RawExperimentArtifact(
            artifact_id=artifact_id,
            sha256=digest,
            bytes=len(payload),
            media_type=media_type,
            relative_path=relative,
            captured_at=captured_at,
        )
        self._read_exact(receipt)
        return receipt

    def read(self, receipt: RawExperimentArtifact) -> bytes:
        return self._read_exact(receipt)


def build_raw_experiment_run(
    *,
    run_id: str,
    manifest: ExperimentCapabilityManifest,
    preregistration_sha256: str,
    input_sha256: str,
    status: ExperimentRunStatus,
    artifacts: tuple[RawExperimentArtifact, ...],
    started_at: datetime,
    ended_at: datetime,
    exit_code: int | None = None,
    failure: ExperimentExecutionFailure | None = None,
    run_purpose: ExperimentRunPurpose = ExperimentRunPurpose.MEASUREMENT,
) -> RawExperimentRun:
    executor = next(item for item in manifest.roles if item.role is CapabilityRole.EXECUTOR)
    return RawExperimentRun(
        run_id=run_id,
        capability_id=manifest.capability_id,
        capability_version=manifest.version,
        capability_manifest_sha256=manifest.manifest_sha256,
        preregistration_sha256=preregistration_sha256,
        input_sha256=input_sha256,
        executor_adapter_ref=executor.adapter_ref,
        executor_implementation_sha256=executor.implementation_sha256,
        executor_principal_sha256=executor.principal_sha256,
        run_purpose=run_purpose,
        status=status,
        exit_code=exit_code,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        failure=failure,
        started_at=started_at,
        ended_at=ended_at,
    )


def _validate_raw_binding(
    *, manifest: ExperimentCapabilityManifest, raw_run: RawExperimentRun
) -> None:
    executor = next(item for item in manifest.roles if item.role is CapabilityRole.EXECUTOR)
    expected = {
        "capability_id": manifest.capability_id,
        "capability_version": manifest.version,
        "capability_manifest_sha256": manifest.manifest_sha256,
        "executor_adapter_ref": executor.adapter_ref,
        "executor_implementation_sha256": executor.implementation_sha256,
        "executor_principal_sha256": executor.principal_sha256,
    }
    for field_name, expected_value in expected.items():
        if getattr(raw_run, field_name) != expected_value:
            raise ValueError(f"raw experiment run changed {field_name}")


def parse_capability_observation(
    *,
    manifest: ExperimentCapabilityManifest,
    raw_run: RawExperimentRun,
    archive: CapabilityObservationArchive,
    adapter: ObservationParserAdapter,
    parsed_at: datetime,
) -> ObservationParseResult:
    """Physically reload every raw artifact and retain either a candidate or parse failure."""

    _validate_raw_binding(manifest=manifest, raw_run=raw_run)
    parser = next(item for item in manifest.roles if item.role is CapabilityRole.OBSERVATION_PARSER)
    adapter_identity = {
        "adapter_ref": parser.adapter_ref,
        "implementation_sha256": parser.implementation_sha256,
        "principal_sha256": parser.principal_sha256,
    }
    for field_name, expected in adapter_identity.items():
        if getattr(adapter, field_name) != expected:
            raise ValueError(f"observation parser changed {field_name}")
    artifacts = {item.artifact_id: archive.read(item) for item in raw_run.artifacts}
    receipt_hashes = tuple(sorted(item.receipt_sha256 for item in raw_run.artifacts))
    try:
        output = ParsedObservationPayload.model_validate(
            adapter.parse(raw_run=raw_run, artifacts=artifacts)
        )
        if raw_run.status is ExperimentRunStatus.SUCCEEDED:
            if output.execution_failure_acknowledged:
                raise ValueError("parser falsely acknowledged failure for successful execution")
        elif not output.execution_failure_acknowledged:
            raise ValueError("parser silently dropped a failed execution")
        artifact_ids = {item.artifact_id for item in raw_run.artifacts}
        referenced = {
            artifact_id
            for measurement in output.measurements
            for artifact_id in measurement.raw_artifact_ids
        }
        if not referenced.issubset(artifact_ids):
            raise ValueError("parser referenced a raw artifact outside the run")
        execution_sha256 = content_sha256(
            {
                "raw_run_sha256": raw_run.run_sha256,
                "parser_implementation_sha256": parser.implementation_sha256,
                "output": output.model_dump(mode="json"),
            }
        )
        candidate = CandidateCapabilityObservation(
            candidate_id=f"{raw_run.run_id}.candidate",
            capability_id=manifest.capability_id,
            capability_version=manifest.version,
            capability_manifest_sha256=manifest.manifest_sha256,
            raw_run_sha256=raw_run.run_sha256,
            raw_artifact_receipt_sha256s=receipt_hashes,
            parser_adapter_ref=parser.adapter_ref,
            parser_implementation_sha256=parser.implementation_sha256,
            parser_principal_sha256=parser.principal_sha256,
            parser_execution_sha256=execution_sha256,
            run_purpose=raw_run.run_purpose,
            run_status=raw_run.status,
            scientific_outcome=output.scientific_outcome,
            measurements=output.measurements,
            context=output.context,
            execution_failure_acknowledged=output.execution_failure_acknowledged,
            parser_warnings=output.parser_warnings,
            parsed_at=parsed_at,
        )
        return ObservationParseResult(
            raw_run=raw_run,
            candidate=candidate,
            completed_at=parsed_at,
        )
    except Exception as error:
        failure = ObservationParsingFailure(
            raw_run_sha256=raw_run.run_sha256,
            raw_artifact_receipt_sha256s=receipt_hashes,
            parser_adapter_ref=parser.adapter_ref,
            parser_implementation_sha256=parser.implementation_sha256,
            parser_principal_sha256=parser.principal_sha256,
            error_class=type(error).__name__,
            error_detail_sha256=hashlib.sha256(str(error).encode()).hexdigest(),
            failed_at=parsed_at,
        )
        return ObservationParseResult(
            raw_run=raw_run,
            failure=failure,
            completed_at=parsed_at,
        )


__all__ = [
    "CandidateCapabilityObservation",
    "CapabilityObservationArchive",
    "CapabilityObservationArchiveError",
    "ExperimentExecutionFailure",
    "ExperimentRunPurpose",
    "ExperimentRunStatus",
    "MeasuredQuantity",
    "MeasurementUncertainty",
    "ObservationCondition",
    "ObservationContext",
    "ObservationParseResult",
    "ObservationParserAdapter",
    "ObservationParsingFailure",
    "ParsedObservationPayload",
    "RawExperimentArtifact",
    "RawExperimentRun",
    "ScientificOutcomeClass",
    "UncertaintyKind",
    "build_raw_experiment_run",
    "parse_capability_observation",
]
