"""Independent validation and F9 admission boundary for typed capability observations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.capabilities.observations import (
    CandidateCapabilityObservation,
    CapabilityObservationArchive,
    ExperimentRunPurpose,
    ExperimentRunStatus,
    ObservationParseResult,
    RawExperimentRun,
    ScientificOutcomeClass,
    UncertaintyKind,
)
from aletheia.capabilities.schemas import (
    CapabilityEvidenceLevel,
    CapabilityLifecycle,
    CapabilityRole,
    ExperimentCapabilityManifest,
    evidence_level_rank,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.knowledge.response_archive import (
    ArchivedKnowledgeLedger,
    ContentAddressedResponseArchive,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


class CapabilityObservationDisposition(str, Enum):
    VALIDATED_POSITIVE = "validated_positive"
    VALIDATED_NEGATIVE = "validated_negative"
    VALIDATED_INCONCLUSIVE = "validated_inconclusive"
    REJECTED_INVALID = "rejected_invalid"
    BLOCKED_EXECUTION = "blocked_execution"
    BLOCKED_PARSER = "blocked_parser"
    BLOCKED_VALIDATOR = "blocked_validator"


_VALIDATED_DISPOSITIONS = {
    CapabilityObservationDisposition.VALIDATED_POSITIVE,
    CapabilityObservationDisposition.VALIDATED_NEGATIVE,
    CapabilityObservationDisposition.VALIDATED_INCONCLUSIVE,
}


class QuantityUnitContract(FrozenModel):
    schema_version: Literal[1] = 1
    quantity_kind_id: str = Field(min_length=1, max_length=256)
    canonical_ucum_code: str = Field(pattern=r"^[!-~]{1,64}$")
    allowed_ucum_codes: tuple[str, ...] = Field(min_length=1)
    conversion_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _unit_codes_are_canonical(self) -> "QuantityUnitContract":
        if self.allowed_ucum_codes != tuple(sorted(set(self.allowed_ucum_codes))):
            raise ValueError("allowed UCUM codes must be unique and sorted")
        if self.canonical_ucum_code not in self.allowed_ucum_codes:
            raise ValueError("canonical UCUM code must be allowed")
        return self


class CapabilityObservationValidationPolicy(FrozenModel):
    schema_name: Literal["aletheia.capability_observation_validation_policy"] = (
        "aletheia.capability_observation_validation_policy"
    )
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    capability_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit_contracts: tuple[QuantityUnitContract, ...] = Field(min_length=1)
    required_condition_ids: tuple[str, ...] = Field(min_length=1)
    minimum_sample_count: int = Field(default=1, ge=1, le=1_000_000_000)
    allow_not_quantified_uncertainty: bool = False
    unit_comparison: Literal["ucum_literal_limited_conformance"] = (
        "ucum_literal_limited_conformance"
    )
    raw_artifact_recheck_required: Literal[True] = True
    domain_validation_required: Literal[True] = True
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _policy_sets_are_canonical(self) -> "CapabilityObservationValidationPolicy":
        kinds = tuple(item.quantity_kind_id for item in self.unit_contracts)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("unit contracts must use unique sorted quantity kinds")
        if self.required_condition_ids != tuple(sorted(set(self.required_condition_ids))):
            raise ValueError("required conditions must be unique and sorted")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class DomainValidationCheck(FrozenModel):
    schema_version: Literal[1] = 1
    check_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    passed: bool
    failure_code: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_sha256s: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_is_complete(self) -> "DomainValidationCheck":
        if self.passed == (self.failure_code is not None):
            raise ValueError("domain check failure code must exist exactly when failed")
        if self.evidence_sha256s != tuple(sorted(set(self.evidence_sha256s))):
            raise ValueError("domain check evidence must be unique and sorted")
        return self


class DomainValidationPayload(FrozenModel):
    """Untrusted domain-validator output; the harness adds identity and lineage."""

    schema_version: Literal[1] = 1
    checks: tuple[DomainValidationCheck, ...] = Field(min_length=1)
    protocol_adherence_verified: bool
    measurement_identity_verified: bool

    @model_validator(mode="after")
    def _checks_are_canonical(self) -> "DomainValidationPayload":
        check_ids = tuple(item.check_id for item in self.checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("domain validation checks must be unique and sorted")
        return self


class DomainValidationReport(FrozenModel):
    schema_name: Literal["aletheia.domain_validation_report"] = "aletheia.domain_validation_report"
    schema_version: Literal[1] = 1
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_adapter_ref: str
    validator_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[DomainValidationCheck, ...] = Field(min_length=1)
    protocol_adherence_verified: bool
    measurement_identity_verified: bool
    raw_artifacts_physically_reloaded: Literal[True] = True
    validated_at: AwareDatetime

    @property
    def report_sha256(self) -> str:
        return content_sha256(self)


class CapabilityDomainValidatorAdapter(Protocol):
    adapter_ref: str
    implementation_sha256: str
    principal_sha256: str

    def validate(
        self,
        *,
        candidate: CandidateCapabilityObservation,
        raw_run: RawExperimentRun,
        artifacts: Mapping[str, bytes],
    ) -> DomainValidationPayload | Mapping[str, object]: ...


class HarnessValidationCheck(FrozenModel):
    schema_version: Literal[1] = 1
    check_id: str
    passed: bool
    failure_code: str | None = None
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _failure_code_matches_result(self) -> "HarnessValidationCheck":
        if self.passed == (self.failure_code is not None):
            raise ValueError("harness check failure code must exist exactly when failed")
        return self


class ValidatedCapabilityObservation(FrozenModel):
    schema_name: Literal["aletheia.validated_capability_observation"] = (
        "aletheia.validated_capability_observation"
    )
    schema_version: Literal[1] = 1
    validation_id: str
    candidate: CandidateCapabilityObservation
    raw_run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_report: DomainValidationReport | None = None
    harness_checks: tuple[HarnessValidationCheck, ...] = Field(min_length=1)
    blockers: tuple[str, ...]
    disposition: CapabilityObservationDisposition
    evidence_level: CapabilityEvidenceLevel
    admissible_for_f9_exploratory_update: bool
    admissible_for_f9_confirmatory_update: bool
    scientific_negative_preserved: bool
    validated_at: AwareDatetime
    state: Literal["validation_complete"] = "validation_complete"

    @model_validator(mode="after")
    def _admission_and_negative_semantics_are_derived(
        self,
    ) -> "ValidatedCapabilityObservation":
        if self.blockers != tuple(sorted(set(self.blockers))):
            raise ValueError("validated observation blockers must be unique and sorted")
        check_ids = tuple(item.check_id for item in self.harness_checks)
        if check_ids != tuple(sorted(set(check_ids))):
            raise ValueError("harness validation checks must be unique and sorted")
        valid = self.disposition in _VALIDATED_DISPOSITIONS
        evidence_admissible = (
            valid and self.candidate.run_purpose is ExperimentRunPurpose.MEASUREMENT
        )
        if self.admissible_for_f9_exploratory_update != evidence_admissible:
            raise ValueError("F9 exploratory admission must exclude non-measurement runs")
        if self.admissible_for_f9_confirmatory_update and not valid:
            raise ValueError("invalid observation cannot enter confirmatory F9 update")
        expected_negative = (
            self.disposition is CapabilityObservationDisposition.VALIDATED_NEGATIVE
            and self.candidate.scientific_outcome is ScientificOutcomeClass.NEGATIVE
        )
        if self.scientific_negative_preserved != expected_negative:
            raise ValueError("scientific negative preservation flag is not derived")
        if valid != (not self.blockers):
            raise ValueError("validated disposition must be derived from empty blockers")
        if self.domain_report is None and (
            self.disposition is not CapabilityObservationDisposition.BLOCKED_EXECUTION
        ):
            raise ValueError("non-execution validation requires a domain report")
        if self.domain_report is not None and (
            self.domain_report.candidate_sha256 != self.candidate.candidate_sha256
            or self.domain_report.raw_run_sha256 != self.raw_run_sha256
            or self.domain_report.validated_at != self.validated_at
        ):
            raise ValueError("domain report changed candidate, raw-run, or time binding")
        return self

    @property
    def validation_sha256(self) -> str:
        return content_sha256(self)


class ObservationValidatorFailure(FrozenModel):
    schema_name: Literal["aletheia.observation_validator_failure"] = (
        "aletheia.observation_validator_failure"
    )
    schema_version: Literal[1] = 1
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_run_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_adapter_ref: str
    validator_implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_principal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_at: AwareDatetime
    candidate_and_raw_retained: Literal[True] = True

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class CapabilityObservationPipelineResult(FrozenModel):
    schema_name: Literal["aletheia.capability_observation_pipeline_result"] = (
        "aletheia.capability_observation_pipeline_result"
    )
    schema_version: Literal[1] = 1
    manifest: ExperimentCapabilityManifest
    policy: CapabilityObservationValidationPolicy
    parse_result: ObservationParseResult
    validation: ValidatedCapabilityObservation | None = None
    validator_failure: ObservationValidatorFailure | None = None
    disposition: CapabilityObservationDisposition
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _terminal_state_is_closed(self) -> "CapabilityObservationPipelineResult":
        raw_run = self.parse_result.raw_run
        if self.policy.capability_manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("observation policy is bound to another capability manifest")
        if raw_run.capability_manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("raw run is bound to another capability manifest")
        executor = next(
            item for item in self.manifest.roles if item.role is CapabilityRole.EXECUTOR
        )
        if (
            raw_run.capability_id != self.manifest.capability_id
            or raw_run.capability_version != self.manifest.version
            or raw_run.executor_adapter_ref != executor.adapter_ref
            or raw_run.executor_implementation_sha256 != executor.implementation_sha256
            or raw_run.executor_principal_sha256 != executor.principal_sha256
        ):
            raise ValueError("raw run changed frozen executor identity")
        candidate = self.parse_result.candidate
        if candidate is not None:
            parser = next(
                item
                for item in self.manifest.roles
                if item.role is CapabilityRole.OBSERVATION_PARSER
            )
            if (
                candidate.parser_adapter_ref != parser.adapter_ref
                or candidate.parser_implementation_sha256 != parser.implementation_sha256
                or candidate.parser_principal_sha256 != parser.principal_sha256
            ):
                raise ValueError("candidate changed frozen parser identity")
        if self.parse_result.failure is not None:
            parser = next(
                item
                for item in self.manifest.roles
                if item.role is CapabilityRole.OBSERVATION_PARSER
            )
            failure = self.parse_result.failure
            if (
                self.validation is not None
                or self.validator_failure is not None
                or self.disposition is not CapabilityObservationDisposition.BLOCKED_PARSER
                or failure.parser_adapter_ref != parser.adapter_ref
                or failure.parser_implementation_sha256 != parser.implementation_sha256
                or failure.parser_principal_sha256 != parser.principal_sha256
            ):
                raise ValueError("parser failure terminal state is invalid")
            terminal_at = self.parse_result.failure.failed_at
        elif self.validator_failure is not None:
            if (
                self.validation is not None
                or self.disposition is not CapabilityObservationDisposition.BLOCKED_VALIDATOR
            ):
                raise ValueError("validator failure terminal state is invalid")
            candidate = self.parse_result.candidate
            validator = next(
                item for item in self.manifest.roles if item.role is CapabilityRole.VALIDATOR
            )
            if (
                candidate is None
                or self.validator_failure.candidate_sha256 != candidate.candidate_sha256
                or self.validator_failure.raw_run_sha256 != raw_run.run_sha256
                or self.validator_failure.validator_adapter_ref != validator.adapter_ref
                or self.validator_failure.validator_implementation_sha256
                != validator.implementation_sha256
                or self.validator_failure.validator_principal_sha256 != validator.principal_sha256
            ):
                raise ValueError("validator failure changed frozen lineage")
            terminal_at = self.validator_failure.failed_at
        else:
            if self.validation is None or self.disposition is not self.validation.disposition:
                raise ValueError("validated terminal state is invalid")
            candidate = self.parse_result.candidate
            if candidate is None:
                raise ValueError("validated pipeline lacks a candidate")
            expected_validation = _derive_validated_observation(
                manifest=self.manifest,
                policy=self.policy,
                raw_run=raw_run,
                candidate=candidate,
                domain_report=self.validation.domain_report,
                validated_at=self.validation.validated_at,
            )
            if self.validation != expected_validation:
                raise ValueError("capability observation validation is not derived")
            if self.validation.domain_report is not None:
                validator = next(
                    item for item in self.manifest.roles if item.role is CapabilityRole.VALIDATOR
                )
                report = self.validation.domain_report
                if (
                    report.validator_adapter_ref != validator.adapter_ref
                    or report.validator_implementation_sha256 != validator.implementation_sha256
                    or report.validator_principal_sha256 != validator.principal_sha256
                ):
                    raise ValueError("domain report changed frozen validator identity")
            terminal_at = self.validation.validated_at
        if terminal_at < self.parse_result.completed_at:
            raise ValueError("observation validation predates parsing")
        if self.completed_at < terminal_at:
            raise ValueError("observation pipeline predates its terminal evidence")
        return self

    @property
    def pipeline_sha256(self) -> str:
        return content_sha256(self)


class CommittedCapabilityObservationPipeline(FrozenModel):
    schema_version: Literal[1] = 1
    result: CapabilityObservationPipelineResult
    ledger: ArchivedKnowledgeLedger
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def _ledger_commits_pipeline(self) -> "CommittedCapabilityObservationPipeline":
        payload = canonical_json_bytes(self.result)
        if (
            self.ledger.object_sha256 != self.result.pipeline_sha256
            or self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest()
            or self.ledger.ledger_bytes != len(payload)
            or self.ledger.archived_at != self.committed_at
        ):
            raise ValueError("observation pipeline ledger does not commit its result")
        if self.committed_at < self.result.completed_at:
            raise ValueError("observation pipeline commitment predates completion")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


def _check(
    *, check_id: str, passed: bool, failure_code: str, evidence: object
) -> HarnessValidationCheck:
    return HarnessValidationCheck(
        check_id=check_id,
        passed=passed,
        failure_code=None if passed else failure_code,
        evidence_sha256=content_sha256({"check_id": check_id, "evidence": evidence}),
    )


def _derive_harness_checks(
    *,
    policy: CapabilityObservationValidationPolicy,
    raw_run: RawExperimentRun,
    candidate: CandidateCapabilityObservation,
    domain_report: DomainValidationReport | None,
) -> tuple[HarnessValidationCheck, ...]:
    checks: list[HarnessValidationCheck] = []
    checks.append(
        _check(
            check_id="execution.succeeded",
            passed=raw_run.status is ExperimentRunStatus.SUCCEEDED,
            failure_code=f"execution_status:{raw_run.status.value}",
            evidence=raw_run.run_sha256,
        )
    )
    if raw_run.status is not ExperimentRunStatus.SUCCEEDED:
        return tuple(sorted(checks, key=lambda item: item.check_id))
    contracts = {item.quantity_kind_id: item for item in policy.unit_contracts}
    raw_ids = {item.artifact_id for item in raw_run.artifacts}
    for measurement in candidate.measurements:
        contract = contracts.get(measurement.quantity_kind_id)
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.quantity_registered",
                passed=contract is not None,
                failure_code=f"quantity_unregistered:{measurement.quantity_kind_id}",
                evidence=measurement.model_dump(mode="json"),
            )
        )
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.unit_allowed",
                passed=(
                    contract is not None and measurement.unit_ucum in contract.allowed_ucum_codes
                ),
                failure_code=(
                    f"unit_not_allowed:{measurement.quantity_kind_id}:{measurement.unit_ucum}"
                ),
                evidence={
                    "unit": measurement.unit_ucum,
                    "contract": contract.model_dump(mode="json") if contract else None,
                },
            )
        )
        uncertainty_valid = (
            policy.allow_not_quantified_uncertainty
            or measurement.uncertainty.kind is not UncertaintyKind.NOT_QUANTIFIED
        )
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.uncertainty_quantified",
                passed=uncertainty_valid,
                failure_code=f"uncertainty_not_quantified:{measurement.measurement_id}",
                evidence=measurement.uncertainty.model_dump(mode="json"),
            )
        )
        interval_contains_value = measurement.uncertainty.kind not in {
            UncertaintyKind.CONFIDENCE_INTERVAL,
            UncertaintyKind.CREDIBLE_INTERVAL,
        } or (
            measurement.uncertainty.lower
            <= measurement.value  # type: ignore[operator]
            <= measurement.uncertainty.upper  # type: ignore[operator]
        )
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.interval_contains_value",
                passed=interval_contains_value,
                failure_code=f"uncertainty_interval_excludes_value:{measurement.measurement_id}",
                evidence={
                    "value": measurement.value,
                    "uncertainty": measurement.uncertainty.model_dump(mode="json"),
                },
            )
        )
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.sample_count",
                passed=measurement.sample_count >= policy.minimum_sample_count,
                failure_code=f"sample_below_minimum:{measurement.measurement_id}",
                evidence={
                    "observed": measurement.sample_count,
                    "minimum": policy.minimum_sample_count,
                },
            )
        )
        checks.append(
            _check(
                check_id=f"measurement.{measurement.measurement_id}.raw_lineage",
                passed=set(measurement.raw_artifact_ids).issubset(raw_ids),
                failure_code=f"raw_lineage_invalid:{measurement.measurement_id}",
                evidence={
                    "measurement_artifacts": measurement.raw_artifact_ids,
                    "run_artifacts": tuple(sorted(raw_ids)),
                },
            )
        )
    condition_ids = (
        {item.condition_id for item in candidate.context.conditions}
        if candidate.context is not None
        else set()
    )
    for condition_id in policy.required_condition_ids:
        checks.append(
            _check(
                check_id=f"condition.{condition_id}.present",
                passed=condition_id in condition_ids,
                failure_code=f"condition_missing:{condition_id}",
                evidence=tuple(sorted(condition_ids)),
            )
        )
    if candidate.context is not None:
        for condition in candidate.context.conditions:
            if condition.numeric_value is None:
                continue
            contract = contracts.get(condition.quantity_kind_id)  # type: ignore[arg-type]
            checks.append(
                _check(
                    check_id=f"condition.{condition.condition_id}.unit_allowed",
                    passed=(
                        contract is not None and condition.unit_ucum in contract.allowed_ucum_codes
                    ),
                    failure_code=f"condition_unit_not_allowed:{condition.condition_id}",
                    evidence=condition.model_dump(mode="json"),
                )
            )
    if domain_report is not None:
        checks.extend(
            (
                _check(
                    check_id="domain.measurement_identity",
                    passed=domain_report.measurement_identity_verified,
                    failure_code="domain_measurement_identity_unverified",
                    evidence=domain_report.report_sha256,
                ),
                _check(
                    check_id="domain.protocol_adherence",
                    passed=domain_report.protocol_adherence_verified,
                    failure_code="domain_protocol_adherence_unverified",
                    evidence=domain_report.report_sha256,
                ),
            )
        )
        for item in domain_report.checks:
            checks.append(
                _check(
                    check_id=f"domain.{item.check_id}",
                    passed=item.passed,
                    failure_code=item.failure_code or "domain_check_failed",
                    evidence=item.model_dump(mode="json"),
                )
            )
    return tuple(sorted(checks, key=lambda item: item.check_id))


def _derive_validated_observation(
    *,
    manifest: ExperimentCapabilityManifest,
    policy: CapabilityObservationValidationPolicy,
    raw_run: RawExperimentRun,
    candidate: CandidateCapabilityObservation,
    domain_report: DomainValidationReport | None,
    validated_at: datetime,
) -> ValidatedCapabilityObservation:
    checks = _derive_harness_checks(
        policy=policy,
        raw_run=raw_run,
        candidate=candidate,
        domain_report=domain_report,
    )
    blockers = tuple(sorted(item.failure_code for item in checks if not item.passed))
    if raw_run.status is not ExperimentRunStatus.SUCCEEDED:
        disposition = CapabilityObservationDisposition.BLOCKED_EXECUTION
    elif blockers:
        disposition = CapabilityObservationDisposition.REJECTED_INVALID
    else:
        disposition = {
            ScientificOutcomeClass.POSITIVE: (CapabilityObservationDisposition.VALIDATED_POSITIVE),
            ScientificOutcomeClass.NEGATIVE: (CapabilityObservationDisposition.VALIDATED_NEGATIVE),
            ScientificOutcomeClass.INCONCLUSIVE: (
                CapabilityObservationDisposition.VALIDATED_INCONCLUSIVE
            ),
        }[candidate.scientific_outcome]
    valid = disposition in _VALIDATED_DISPOSITIONS
    evidence_admissible = valid and candidate.run_purpose is ExperimentRunPurpose.MEASUREMENT
    confirmatory = (
        evidence_admissible
        and manifest.lifecycle is CapabilityLifecycle.REGISTERED
        and evidence_level_rank(manifest.maximum_evidence_level)
        >= evidence_level_rank(CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL)
    )
    return ValidatedCapabilityObservation(
        validation_id=f"{candidate.candidate_id}.validation",
        candidate=candidate,
        raw_run_sha256=raw_run.run_sha256,
        policy_sha256=policy.policy_sha256,
        domain_report=domain_report,
        harness_checks=checks,
        blockers=blockers,
        disposition=disposition,
        evidence_level=manifest.maximum_evidence_level,
        admissible_for_f9_exploratory_update=evidence_admissible,
        admissible_for_f9_confirmatory_update=confirmatory,
        scientific_negative_preserved=(
            disposition is CapabilityObservationDisposition.VALIDATED_NEGATIVE
        ),
        validated_at=validated_at,
    )


def validate_capability_observation(
    *,
    manifest: ExperimentCapabilityManifest,
    policy: CapabilityObservationValidationPolicy,
    parse_result: ObservationParseResult,
    archive: CapabilityObservationArchive,
    adapter: CapabilityDomainValidatorAdapter,
    validated_at: datetime,
) -> CapabilityObservationPipelineResult:
    """Validate a parsed observation while retaining invalid, negative, and failed states."""

    raw_run = parse_result.raw_run
    if policy.capability_manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("observation validation policy changed capability manifest")
    if policy.frozen_at > raw_run.started_at:
        raise ValueError("observation validation policy was not frozen before execution")
    if parse_result.failure is not None:
        return CapabilityObservationPipelineResult(
            manifest=manifest,
            policy=policy,
            parse_result=parse_result,
            disposition=CapabilityObservationDisposition.BLOCKED_PARSER,
            completed_at=validated_at,
        )
    candidate = parse_result.candidate
    if candidate is None:
        raise ValueError("successful parse result lacks a candidate")
    if candidate.raw_run_sha256 != raw_run.run_sha256:
        raise ValueError("candidate is bound to another raw run")
    receipt_hashes = tuple(sorted(item.receipt_sha256 for item in raw_run.artifacts))
    if candidate.raw_artifact_receipt_sha256s != receipt_hashes:
        raise ValueError("candidate does not retain every raw artifact receipt")
    artifacts = {item.artifact_id: archive.read(item) for item in raw_run.artifacts}
    if raw_run.status is not ExperimentRunStatus.SUCCEEDED:
        validation = _derive_validated_observation(
            manifest=manifest,
            policy=policy,
            raw_run=raw_run,
            candidate=candidate,
            domain_report=None,
            validated_at=validated_at,
        )
        return CapabilityObservationPipelineResult(
            manifest=manifest,
            policy=policy,
            parse_result=parse_result,
            validation=validation,
            disposition=validation.disposition,
            completed_at=validated_at,
        )
    validator = next(item for item in manifest.roles if item.role is CapabilityRole.VALIDATOR)
    identity = {
        "adapter_ref": validator.adapter_ref,
        "implementation_sha256": validator.implementation_sha256,
        "principal_sha256": validator.principal_sha256,
    }
    for field_name, expected in identity.items():
        if getattr(adapter, field_name) != expected:
            raise ValueError(f"observation validator changed {field_name}")
    try:
        output = DomainValidationPayload.model_validate(
            adapter.validate(candidate=candidate, raw_run=raw_run, artifacts=artifacts)
        )
        execution_sha256 = content_sha256(
            {
                "candidate_sha256": candidate.candidate_sha256,
                "raw_run_sha256": raw_run.run_sha256,
                "validator_implementation_sha256": validator.implementation_sha256,
                "output": output.model_dump(mode="json"),
            }
        )
        report = DomainValidationReport(
            candidate_sha256=candidate.candidate_sha256,
            raw_run_sha256=raw_run.run_sha256,
            validator_adapter_ref=validator.adapter_ref,
            validator_implementation_sha256=validator.implementation_sha256,
            validator_principal_sha256=validator.principal_sha256,
            validator_execution_sha256=execution_sha256,
            checks=output.checks,
            protocol_adherence_verified=output.protocol_adherence_verified,
            measurement_identity_verified=output.measurement_identity_verified,
            validated_at=validated_at,
        )
        validation = _derive_validated_observation(
            manifest=manifest,
            policy=policy,
            raw_run=raw_run,
            candidate=candidate,
            domain_report=report,
            validated_at=validated_at,
        )
        return CapabilityObservationPipelineResult(
            manifest=manifest,
            policy=policy,
            parse_result=parse_result,
            validation=validation,
            disposition=validation.disposition,
            completed_at=validated_at,
        )
    except Exception as error:
        failure = ObservationValidatorFailure(
            candidate_sha256=candidate.candidate_sha256,
            raw_run_sha256=raw_run.run_sha256,
            validator_adapter_ref=validator.adapter_ref,
            validator_implementation_sha256=validator.implementation_sha256,
            validator_principal_sha256=validator.principal_sha256,
            error_class=type(error).__name__,
            error_detail_sha256=hashlib.sha256(str(error).encode()).hexdigest(),
            failed_at=validated_at,
        )
        return CapabilityObservationPipelineResult(
            manifest=manifest,
            policy=policy,
            parse_result=parse_result,
            validator_failure=failure,
            disposition=CapabilityObservationDisposition.BLOCKED_VALIDATOR,
            completed_at=validated_at,
        )


def commit_capability_observation_pipeline(
    *,
    archive: ContentAddressedResponseArchive,
    result: CapabilityObservationPipelineResult,
    committed_at: datetime,
) -> CommittedCapabilityObservationPipeline:
    ledger = archive.store_ledger(
        value=result,
        object_sha256=result.pipeline_sha256,
        archived_at=committed_at,
    )
    return CommittedCapabilityObservationPipeline(
        result=result,
        ledger=ledger,
        committed_at=committed_at,
    )


def load_committed_capability_observation_pipeline(
    *,
    ledger_archive: ContentAddressedResponseArchive,
    raw_archive: CapabilityObservationArchive,
    committed: CommittedCapabilityObservationPipeline,
) -> CapabilityObservationPipelineResult:
    payload = ledger_archive.read_ledger(committed.ledger)
    result = CapabilityObservationPipelineResult.model_validate_json(payload)
    if result != committed.result:
        raise ValueError("physical observation pipeline differs from committed result")
    for receipt in result.parse_result.raw_run.artifacts:
        raw_archive.read(receipt)
    return result


__all__ = [
    "CapabilityDomainValidatorAdapter",
    "CapabilityObservationDisposition",
    "CapabilityObservationPipelineResult",
    "CapabilityObservationValidationPolicy",
    "CommittedCapabilityObservationPipeline",
    "DomainValidationCheck",
    "DomainValidationPayload",
    "DomainValidationReport",
    "HarnessValidationCheck",
    "ObservationValidatorFailure",
    "QuantityUnitContract",
    "ValidatedCapabilityObservation",
    "commit_capability_observation_pipeline",
    "load_committed_capability_observation_pipeline",
    "validate_capability_observation",
]
