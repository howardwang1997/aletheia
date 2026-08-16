"""F10-S2 raw, parser, validator, unit, uncertainty, and admission tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import aletheia.capabilities as c
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = (
    ROOT / "configs/capabilities/materials_band_gap_range_compression_provisional_v2.yaml"
)
BASE = datetime(2026, 8, 15, 7, tzinfo=timezone.utc)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _manifest(*, registered: bool = False) -> c.ExperimentCapabilityManifest:
    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    roles = raw["roles"]
    role_values = {
        "executor": ("tests.capabilities.fake:execute", sha("executor-code"), sha("executor")),
        "observation_parser": (
            "tests.capabilities.fake:parse",
            sha("parser-code"),
            sha("parser"),
        ),
        "validator": (
            "tests.capabilities.fake:validate",
            sha("validator-code"),
            sha("validator"),
        ),
    }
    for role in roles:
        values = role_values.get(role["role"])
        if values is not None:
            role["adapter_ref"], role["implementation_sha256"], role["principal_sha256"] = values
    raw["supersedes_manifest_sha256"] = None
    raw["frozen_at"] = BASE - timedelta(minutes=10)
    if registered:
        raw["lifecycle"] = "registered"
        raw["maximum_evidence_level"] = "confirmatory_internal"
        roles[-1]["agent_authored"] = False
        raw["registration_evidence"] = {
            "reference_fixtures_sha256": sha("reference"),
            "adversarial_fixtures_sha256": sha("adversarial"),
            "positive_control_receipt_sha256": sha("positive"),
            "negative_control_receipt_sha256": sha("negative"),
            "independent_recomputation_receipt_sha256": sha("recompute"),
            "reproduction_policy_evidence_sha256": sha("reproduction"),
            "safety_review_sha256": sha("safety"),
            "domain_review_receipt_sha256": sha("domain-review"),
            "domain_reviewer_principal_sha256": sha("domain-reviewer"),
            "promotion_auditor_principal_sha256": sha("promotion-auditor"),
            "reviewed_at": BASE - timedelta(minutes=20),
        }
    return c.ExperimentCapabilityManifest.model_validate(raw)


def _role(manifest, role):
    return next(item for item in manifest.roles if item.role is role)


def _policy(manifest) -> c.CapabilityObservationValidationPolicy:
    return c.CapabilityObservationValidationPolicy(
        policy_id="materials-observation-policy-v1",
        capability_manifest_sha256=manifest.manifest_sha256,
        unit_contracts=(
            c.QuantityUnitContract(
                quantity_kind_id="band_gap",
                canonical_ucum_code="eV",
                allowed_ucum_codes=("eV",),
                conversion_policy_sha256=sha("band-gap-unit-policy"),
            ),
            c.QuantityUnitContract(
                quantity_kind_id="thermodynamic_temperature",
                canonical_ucum_code="K",
                allowed_ucum_codes=("K",),
                conversion_policy_sha256=sha("temperature-unit-policy"),
            ),
        ),
        required_condition_ids=("temperature",),
        minimum_sample_count=30,
        frozen_at=BASE,
    )


def _payload(
    *,
    outcome: c.ScientificOutcomeClass = c.ScientificOutcomeClass.NEGATIVE,
    unit: str = "eV",
    uncertainty: c.MeasurementUncertainty | None = None,
    condition_id: str = "temperature",
):
    return c.ParsedObservationPayload(
        scientific_outcome=outcome,
        measurements=(
            c.MeasuredQuantity(
                measurement_id="band-gap-primary",
                quantity_kind_id="band_gap",
                value=0.5,
                unit_ucum=unit,
                uncertainty=uncertainty
                or c.MeasurementUncertainty(
                    kind=c.UncertaintyKind.CONFIDENCE_INTERVAL,
                    lower=0.4,
                    upper=0.6,
                    coverage_probability=0.95,
                    method_sha256=sha("bootstrap"),
                ),
                sample_count=50,
                raw_artifact_ids=("result",),
            ),
        ),
        context=c.ObservationContext(
            measurement_method_id="frozen-band-gap-protocol-v1",
            conditions=(
                c.ObservationCondition(
                    condition_id=condition_id,
                    quantity_kind_id=(
                        "thermodynamic_temperature" if condition_id == "temperature" else None
                    ),
                    numeric_value=300.0 if condition_id == "temperature" else None,
                    unit_ucum="K" if condition_id == "temperature" else None,
                    categorical_value=("argon" if condition_id != "temperature" else None),
                ),
            ),
            sample_id="sample-001",
            batch_id="batch-001",
        ),
    )


class FakeParser:
    def __init__(self, manifest, output=None, error: Exception | None = None):
        binding = _role(manifest, c.CapabilityRole.OBSERVATION_PARSER)
        self.adapter_ref = binding.adapter_ref
        self.implementation_sha256 = binding.implementation_sha256
        self.principal_sha256 = binding.principal_sha256
        self.output = output or _payload()
        self.error = error

    def parse(self, *, raw_run, artifacts):
        assert tuple(sorted(artifacts)) == tuple(
            sorted(item.artifact_id for item in raw_run.artifacts)
        )
        if self.error is not None:
            raise self.error
        return self.output


class FakeValidator:
    def __init__(
        self,
        manifest,
        *,
        failed_check: bool = False,
        error: Exception | None = None,
    ):
        binding = _role(manifest, c.CapabilityRole.VALIDATOR)
        self.adapter_ref = binding.adapter_ref
        self.implementation_sha256 = binding.implementation_sha256
        self.principal_sha256 = binding.principal_sha256
        self.failed_check = failed_check
        self.error = error
        self.calls = 0

    def validate(self, *, candidate, raw_run, artifacts):
        self.calls += 1
        assert candidate.raw_run_sha256 == raw_run.run_sha256
        assert set(artifacts) == {item.artifact_id for item in raw_run.artifacts}
        if self.error is not None:
            raise self.error
        return c.DomainValidationPayload(
            checks=(
                c.DomainValidationCheck(
                    check_id="control.negative",
                    passed=not self.failed_check,
                    failure_code=("negative_control_failed" if self.failed_check else None),
                    evidence_sha256s=(sha("negative-control"),),
                ),
                c.DomainValidationCheck(
                    check_id="schema.exact",
                    passed=True,
                    evidence_sha256s=(sha("schema"),),
                ),
            ),
            protocol_adherence_verified=True,
            measurement_identity_verified=True,
        )


def _successful_raw(tmp_path, manifest):
    archive = c.CapabilityObservationArchive(tmp_path / "raw")
    payload = json.dumps({"band_gap_ev": 0.5}, sort_keys=True).encode()
    artifact = archive.store(
        artifact_id="result",
        payload=payload,
        media_type="application/json",
        captured_at=BASE + timedelta(minutes=2),
    )
    run = c.build_raw_experiment_run(
        run_id="materials-observation-run-001",
        manifest=manifest,
        preregistration_sha256=sha("preregistration"),
        input_sha256=sha("input"),
        status=c.ExperimentRunStatus.SUCCEEDED,
        artifacts=(artifact,),
        started_at=BASE + timedelta(minutes=1),
        ended_at=BASE + timedelta(minutes=2),
        exit_code=0,
    )
    return archive, run


def _execute_pipeline(tmp_path, *, manifest=None, parser_output=None, validator=None):
    manifest = manifest or _manifest()
    archive, run = _successful_raw(tmp_path, manifest)
    parse_result = c.parse_capability_observation(
        manifest=manifest,
        raw_run=run,
        archive=archive,
        adapter=FakeParser(manifest, output=parser_output),
        parsed_at=BASE + timedelta(minutes=3),
    )
    validator = validator or FakeValidator(manifest)
    result = c.validate_capability_observation(
        manifest=manifest,
        policy=_policy(manifest),
        parse_result=parse_result,
        archive=archive,
        adapter=validator,
        validated_at=BASE + timedelta(minutes=4),
    )
    return archive, run, parse_result, validator, result


def test_valid_negative_is_preserved_and_only_exploratory_for_provisional_capability(
    tmp_path,
):
    raw_archive, _run, _parsed, validator, result = _execute_pipeline(tmp_path)
    assert validator.calls == 1
    assert result.disposition is c.CapabilityObservationDisposition.VALIDATED_NEGATIVE
    validation = result.validation
    assert validation is not None
    assert validation.scientific_negative_preserved is True
    assert validation.admissible_for_f9_exploratory_update is True
    assert validation.admissible_for_f9_confirmatory_update is False
    assert validation.evidence_level is c.CapabilityEvidenceLevel.EXPLORATORY
    assert not validation.blockers

    ledger_archive = ContentAddressedResponseArchive(tmp_path / "ledger")
    committed = c.commit_capability_observation_pipeline(
        archive=ledger_archive,
        result=result,
        committed_at=BASE + timedelta(minutes=5),
    )
    assert (
        c.load_committed_capability_observation_pipeline(
            ledger_archive=ledger_archive,
            raw_archive=raw_archive,
            committed=committed,
        )
        == result
    )


@pytest.mark.parametrize(
    ("payload", "blocker"),
    [
        (_payload(unit="meV"), "unit_not_allowed:band_gap:meV"),
        (
            _payload(
                uncertainty=c.MeasurementUncertainty(
                    kind=c.UncertaintyKind.NOT_QUANTIFIED,
                    not_quantified_reason="source omitted uncertainty",
                )
            ),
            "uncertainty_not_quantified:band-gap-primary",
        ),
        (_payload(condition_id="atmosphere"), "condition_missing:temperature"),
    ],
)
def test_unit_uncertainty_and_required_conditions_reject_invalid_measurement(
    tmp_path, payload, blocker
):
    _archive, _run, _parsed, _validator, result = _execute_pipeline(tmp_path, parser_output=payload)
    assert result.disposition is c.CapabilityObservationDisposition.REJECTED_INVALID
    assert result.validation is not None
    assert blocker in result.validation.blockers
    assert result.validation.admissible_for_f9_exploratory_update is False
    assert result.validation.scientific_negative_preserved is False


def test_domain_validator_failure_is_invalid_not_a_scientific_negative(tmp_path):
    manifest = _manifest()
    validator = FakeValidator(manifest, failed_check=True)
    _archive, _run, _parsed, _validator, result = _execute_pipeline(
        tmp_path, manifest=manifest, validator=validator
    )
    assert result.disposition is c.CapabilityObservationDisposition.REJECTED_INVALID
    assert result.validation is not None
    assert "negative_control_failed" in result.validation.blockers
    assert result.validation.scientific_negative_preserved is False


def test_failed_execution_must_be_acknowledged_and_never_calls_domain_validator(tmp_path):
    manifest = _manifest()
    archive = c.CapabilityObservationArchive(tmp_path / "raw")
    artifact = archive.store(
        artifact_id="stderr",
        payload=b"solver did not converge",
        media_type="text/plain",
        captured_at=BASE + timedelta(minutes=2),
    )
    run = c.build_raw_experiment_run(
        run_id="materials-failed-run-001",
        manifest=manifest,
        preregistration_sha256=sha("preregistration"),
        input_sha256=sha("input"),
        status=c.ExperimentRunStatus.FAILED,
        artifacts=(artifact,),
        started_at=BASE + timedelta(minutes=1),
        ended_at=BASE + timedelta(minutes=2),
        exit_code=17,
        failure=c.ExperimentExecutionFailure(
            failure_kind="non_convergence",
            detail_sha256=sha("solver did not converge"),
            raw_failure_artifact_ids=("stderr",),
        ),
    )
    acknowledged = c.ParsedObservationPayload(
        scientific_outcome=c.ScientificOutcomeClass.NOT_EVALUABLE,
        execution_failure_acknowledged=True,
    )
    parsed = c.parse_capability_observation(
        manifest=manifest,
        raw_run=run,
        archive=archive,
        adapter=FakeParser(manifest, output=acknowledged),
        parsed_at=BASE + timedelta(minutes=3),
    )
    validator = FakeValidator(manifest, error=AssertionError("must not run"))
    result = c.validate_capability_observation(
        manifest=manifest,
        policy=_policy(manifest),
        parse_result=parsed,
        archive=archive,
        adapter=validator,
        validated_at=BASE + timedelta(minutes=4),
    )
    assert validator.calls == 0
    assert result.disposition is c.CapabilityObservationDisposition.BLOCKED_EXECUTION
    assert result.validation is not None
    assert result.validation.blockers == ("execution_status:failed",)

    dropped = c.parse_capability_observation(
        manifest=manifest,
        raw_run=run,
        archive=archive,
        adapter=FakeParser(
            manifest,
            output=c.ParsedObservationPayload(
                scientific_outcome=c.ScientificOutcomeClass.NOT_EVALUABLE,
                execution_failure_acknowledged=False,
            ),
        ),
        parsed_at=BASE + timedelta(minutes=3),
    )
    assert dropped.failure is not None
    assert dropped.failure.raw_artifacts_retained is True


def test_parser_and_validator_exceptions_are_distinct_retained_terminal_states(tmp_path):
    manifest = _manifest()
    archive, run = _successful_raw(tmp_path, manifest)
    parsed = c.parse_capability_observation(
        manifest=manifest,
        raw_run=run,
        archive=archive,
        adapter=FakeParser(manifest, error=ValueError("malformed payload")),
        parsed_at=BASE + timedelta(minutes=3),
    )
    assert parsed.failure is not None
    blocked_parser = c.validate_capability_observation(
        manifest=manifest,
        policy=_policy(manifest),
        parse_result=parsed,
        archive=archive,
        adapter=FakeValidator(manifest),
        validated_at=BASE + timedelta(minutes=4),
    )
    assert blocked_parser.disposition is c.CapabilityObservationDisposition.BLOCKED_PARSER

    parsed_ok = c.parse_capability_observation(
        manifest=manifest,
        raw_run=run,
        archive=archive,
        adapter=FakeParser(manifest),
        parsed_at=BASE + timedelta(minutes=3),
    )
    blocked_validator = c.validate_capability_observation(
        manifest=manifest,
        policy=_policy(manifest),
        parse_result=parsed_ok,
        archive=archive,
        adapter=FakeValidator(manifest, error=RuntimeError("validator unavailable")),
        validated_at=BASE + timedelta(minutes=4),
    )
    assert blocked_validator.disposition is c.CapabilityObservationDisposition.BLOCKED_VALIDATOR
    assert blocked_validator.validator_failure is not None
    assert blocked_validator.validator_failure.candidate_and_raw_retained is True


def test_registered_valid_observation_can_cross_confirmatory_admission_gate(tmp_path):
    manifest = _manifest(registered=True)
    _archive, _run, _parsed, _validator, result = _execute_pipeline(tmp_path, manifest=manifest)
    assert result.validation is not None
    assert result.validation.admissible_for_f9_confirmatory_update is True
    assert result.validation.evidence_level is c.CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL


def test_raw_corruption_and_derived_validation_tampering_fail_closed(tmp_path):
    archive, run, _parsed, _validator, result = _execute_pipeline(tmp_path)
    receipt = run.artifacts[0]
    target = archive.root / receipt.relative_path
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    with pytest.raises(c.CapabilityObservationArchiveError, match="metadata changed"):
        archive.read(receipt)

    raw = result.model_dump(mode="python")
    raw["validation"]["disposition"] = "validated_positive"
    raw["disposition"] = "validated_positive"
    raw["validation"]["scientific_negative_preserved"] = False
    with pytest.raises(ValidationError, match="not derived"):
        c.CapabilityObservationPipelineResult.model_validate(raw)
