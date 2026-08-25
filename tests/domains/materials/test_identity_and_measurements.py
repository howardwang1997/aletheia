"""Gold and adversarial fixtures for F10-S3 material identity and measurement audit."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.capabilities.observations import (
    MeasuredQuantity,
    MeasurementUncertainty,
    ObservationCondition,
    ObservationContext,
    UncertaintyKind,
)
from aletheia.domains.materials.identity import (
    LicensedSourceArtifact,
    MaterialIdentityLevel,
    MaterialRecordIdentity,
    MaterialSplitLedger,
    MaterialSplitPolicy,
    SampleIdentity,
    SplitAuditDisposition,
    SynthesisBatchIdentity,
    build_material_split_ledger,
    build_structure_identity_from_cif,
    normalize_formula,
)
from aletheia.domains.materials.measurements import (
    ConditionValueKind,
    EstimateAvailability,
    LinearUnitConversion,
    MaterialMeasurementAudit,
    MaterialMeasurementAuditPolicy,
    MaterialMeasurementRecord,
    MaterialMeasurementStatus,
    MeasurementAuditDisposition,
    MeasurementConditionContract,
    VariabilityLevel,
    audit_material_measurements,
)


BASE = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/materials_identity"


def sha(label: str | bytes) -> str:
    payload = label if isinstance(label, bytes) else label.encode()
    return hashlib.sha256(payload).hexdigest()


def source(
    artifact_id: str,
    payload: bytes | None = None,
    *,
    source_group: str | None = None,
) -> LicensedSourceArtifact:
    payload = payload or f"artifact:{artifact_id}".encode()
    return LicensedSourceArtifact(
        artifact_id=artifact_id,
        sha256=sha(payload),
        bytes=len(payload),
        media_type="application/octet-stream",
        source_uri=f"https://example.test/{source_group or artifact_id}",
        license_expression="CC0-1.0",
        license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
        license_evidence_sha256=sha("CC0-1.0 licence evidence"),
        retrieved_at=BASE,
    )


def material(
    record_id: str,
    *,
    batch_id: str = "batch-001",
    sample_id: str = "sample-001",
) -> MaterialRecordIdentity:
    formula = normalize_formula("NaCl")
    batch = SynthesisBatchIdentity(
        batch_id=batch_id,
        issuer="gold-lab",
        formula_identity_sha256=formula.formula_identity_sha256,
        synthesis_record=source(f"{batch_id}-record"),
        produced_at=BASE,
    )
    sample = SampleIdentity(
        sample_id=sample_id,
        issuer="gold-lab",
        batch_identity_sha256=batch.batch_identity_sha256,
        sample_record=source(f"{sample_id}-record"),
        prepared_at=BASE + timedelta(minutes=1),
    )
    return MaterialRecordIdentity(
        record_id=record_id,
        source=source(f"{record_id}-material"),
        formula=formula,
        batch=batch,
        sample=sample,
        declared_missing_levels=(MaterialIdentityLevel.STRUCTURE,),
    )


def anonymous_material(record_id: str) -> MaterialRecordIdentity:
    return MaterialRecordIdentity(
        record_id=record_id,
        source=source(f"{record_id}-material"),
        formula=normalize_formula("NaCl"),
        declared_missing_levels=(
            MaterialIdentityLevel.BATCH,
            MaterialIdentityLevel.SAMPLE,
            MaterialIdentityLevel.STRUCTURE,
        ),
    )


def audit_policy() -> MaterialMeasurementAuditPolicy:
    return MaterialMeasurementAuditPolicy(
        policy_id="band-gap-measurement-gold-v1",
        property_id="materials.band_gap",
        quantity_kind_id="materials.band_gap.energy",
        canonical_ucum_code="eV",
        quantity_unit_conversions=(
            LinearUnitConversion(
                source_ucum_code="eV",
                canonical_ucum_code="eV",
                scale=1,
            ),
            LinearUnitConversion(
                source_ucum_code="meV",
                canonical_ucum_code="eV",
                scale=0.001,
            ),
        ),
        required_conditions=(
            MeasurementConditionContract(
                condition_id="atmosphere",
                value_kind=ConditionValueKind.CATEGORICAL,
                allowed_categories=("air", "vacuum"),
            ),
            MeasurementConditionContract(
                condition_id="temperature",
                value_kind=ConditionValueKind.QUANTITATIVE,
                quantity_kind_id="thermodynamic_temperature",
                canonical_ucum_code="K",
                unit_conversions=(
                    LinearUnitConversion(
                        source_ucum_code="Cel",
                        canonical_ucum_code="K",
                        scale=1,
                        offset=273.15,
                    ),
                    LinearUnitConversion(
                        source_ucum_code="K",
                        canonical_ucum_code="K",
                        scale=1,
                    ),
                ),
                compatibility_tolerance=2,
            ),
        ),
        allowed_measurement_method_ids=("uv-vis-tauc-v1",),
        required_identity_levels=(
            MaterialIdentityLevel.BATCH,
            MaterialIdentityLevel.FORMULA,
            MaterialIdentityLevel.SAMPLE,
        ),
        pooling_identity_level=MaterialIdentityLevel.FORMULA,
        conflict_absolute_tolerance=0.2,
        conflict_combined_standard_uncertainty_multiplier=3,
        frozen_at=BASE - timedelta(minutes=1),
    )


def measurement(
    record_id: str,
    material_record: MaterialRecordIdentity,
    *,
    value: float = 1.0,
    unit: str = "eV",
    temperature: float = 300,
    temperature_unit: str = "K",
    atmosphere: str = "air",
    source_group: str = "source-A",
    artifact_key: str | None = None,
    measurement_id: str | None = None,
    status: MaterialMeasurementStatus = MaterialMeasurementStatus.SUCCEEDED,
    omit_temperature: bool = False,
    uncertainty: float = 0.02,
) -> MaterialMeasurementRecord:
    artifact_key = artifact_key or record_id
    artifact = source(f"raw-{artifact_key}", source_group=source_group)
    if status is not MaterialMeasurementStatus.SUCCEEDED:
        return MaterialMeasurementRecord(
            measurement_record_id=record_id,
            material=material_record,
            property_id="materials.band_gap",
            source_group_id=source_group,
            measurement_source=artifact,
            status=status,
            failure_code=f"fixture_{status.value}",
            measured_at=BASE + timedelta(minutes=2),
        )
    conditions = [ObservationCondition(condition_id="atmosphere", categorical_value=atmosphere)]
    if not omit_temperature:
        conditions.append(
            ObservationCondition(
                condition_id="temperature",
                quantity_kind_id="thermodynamic_temperature",
                numeric_value=temperature,
                unit_ucum=temperature_unit,
            )
        )
    return MaterialMeasurementRecord(
        measurement_record_id=record_id,
        material=material_record,
        property_id="materials.band_gap",
        source_group_id=source_group,
        measurement_source=artifact,
        status=status,
        quantity=MeasuredQuantity(
            measurement_id=measurement_id or f"quantity-{record_id}",
            quantity_kind_id="materials.band_gap.energy",
            value=value,
            unit_ucum=unit,
            uncertainty=MeasurementUncertainty(
                kind=UncertaintyKind.STANDARD,
                value=uncertainty,
            ),
            sample_count=1,
            raw_artifact_ids=(artifact.artifact_id,),
        ),
        context=ObservationContext(
            measurement_method_id="uv-vis-tauc-v1",
            conditions=tuple(sorted(conditions, key=lambda item: item.condition_id)),
            sample_id=material_record.sample.sample_id if material_record.sample else None,
            batch_id=material_record.batch.batch_id if material_record.batch else None,
        ),
        measured_at=BASE + timedelta(minutes=2),
    )


def run_audit(*records: MaterialMeasurementRecord) -> MaterialMeasurementAudit:
    return audit_material_measurements(
        audit_id="materials-measurement-gold-audit",
        policy=audit_policy(),
        records=tuple(records),
        audited_at=BASE + timedelta(minutes=10),
    )


def test_formula_gold_equivalence_and_source_byte_verification():
    gold = json.loads((FIXTURES / "gold_cases.json").read_text(encoding="utf-8"))
    for case in gold["formula_equivalence"]:
        identities = [normalize_formula(item) for item in case["inputs"]]
        assert {item.canonical_formula for item in identities} == {case["canonical_formula"]}
        assert {item.chemical_system for item in identities} == {case["chemical_system"]}
        assert len({item.formula_identity_sha256 for item in identities}) == 1

    payload = b"licensed source bytes"
    receipt = source("licensed-source", payload)
    receipt.verify_bytes(payload)
    with pytest.raises(ValueError, match="do not match"):
        receipt.verify_bytes(payload + b"tampered")


def test_same_formula_different_polymorphs_have_distinct_structure_identity():
    gold = json.loads((FIXTURES / "gold_cases.json").read_text(encoding="utf-8"))
    case = gold["polymorph_pair"]
    formula = normalize_formula(case["formula"])
    identities = []
    for side in ("left", "right"):
        payload = (FIXTURES / case[f"{side}_cif"]).read_bytes()
        identities.append(
            build_structure_identity_from_cif(
                cif_bytes=payload,
                source=source(f"{side}-cif", payload),
                expected_formula=formula,
            )
        )
    left, right = identities
    assert left.formula.formula_identity_sha256 == right.formula.formula_identity_sha256
    assert left.structure_identity_sha256 != right.structure_identity_sha256
    assert left.space_group_number == case["left_space_group_number"]
    assert right.space_group_number == case["right_space_group_number"]


def test_sample_and_batch_cross_split_leakage_is_rejected_and_cannot_be_forged():
    shared = material("record-train")
    second_record_same_sample = shared.model_copy(
        update={
            "record_id": "record-test",
            "source": source("record-test-material"),
        }
    )
    policy = MaterialSplitPolicy(
        policy_id="sample-isolated-gold-v1",
        required_identity_levels=(
            MaterialIdentityLevel.BATCH,
            MaterialIdentityLevel.RECORD,
            MaterialIdentityLevel.SAMPLE,
        ),
        allowed_splits=("test", "train"),
        frozen_at=BASE,
    )
    ledger = build_material_split_ledger(
        ledger_id="sample-leakage-gold-ledger",
        dataset_source=source("split-dataset"),
        policy=policy,
        records=(("train", shared), ("test", second_record_same_sample)),
        created_at=BASE + timedelta(minutes=1),
    )
    assert ledger.disposition is SplitAuditDisposition.REJECTED_IDENTITY_LEAKAGE
    assert {item.identity_level for item in ledger.cross_split_overlaps} == {
        MaterialIdentityLevel.BATCH,
        MaterialIdentityLevel.SAMPLE,
    }
    tampered = ledger.model_dump(mode="json")
    tampered["disposition"] = SplitAuditDisposition.CLEAN.value
    with pytest.raises(ValidationError, match="split disposition"):
        MaterialSplitLedger.model_validate(tampered)


def test_required_sample_identity_missing_from_split_fails_closed():
    policy = MaterialSplitPolicy(
        policy_id="sample-required-gold-v1",
        required_identity_levels=(
            MaterialIdentityLevel.RECORD,
            MaterialIdentityLevel.SAMPLE,
        ),
        allowed_splits=("test", "train"),
        frozen_at=BASE,
    )
    ledger = build_material_split_ledger(
        ledger_id="sample-missing-gold-ledger",
        dataset_source=source("anonymous-dataset"),
        policy=policy,
        records=(
            ("train", anonymous_material("anonymous-train")),
            ("test", anonymous_material("anonymous-test")),
        ),
        created_at=BASE + timedelta(minutes=1),
    )
    assert ledger.disposition is SplitAuditDisposition.REJECTED_IDENTITY_LEAKAGE
    assert {item.identity_level for item in ledger.missing_identities} == {
        MaterialIdentityLevel.SAMPLE
    }


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (lambda item: measurement("bad-unit", item, unit="J"), "quantity_unit_unsupported"),
        (
            lambda item: measurement("missing-condition", item, omit_temperature=True),
            "condition_missing:temperature",
        ),
        (
            lambda item: measurement("failed-run", item, status=MaterialMeasurementStatus.FAILED),
            "measurement_status:failed",
        ),
    ],
)
def test_invalid_unit_condition_and_failed_execution_are_retained_but_never_pooled(record, reason):
    audit = run_audit(record(material("material-invalid")))
    assert audit.disposition is MeasurementAuditDisposition.REJECTED_NO_ELIGIBLE_MEASUREMENTS
    assert audit.eligible_record_sha256s == ()
    assert reason in audit.exclusions[0].reason_codes
    assert all(item.availability is EstimateAvailability.UNAVAILABLE for item in audit.variability)
    assert all(item.unavailable_reason == "no_eligible_measurements" for item in audit.variability)


def test_incompatible_conditions_are_split_into_separate_nonpooled_strata():
    low = measurement(
        "temperature-low",
        material("material-low", batch_id="batch-low", sample_id="sample-low"),
        temperature=300,
    )
    high = measurement(
        "temperature-high",
        material("material-high", batch_id="batch-high", sample_id="sample-high"),
        temperature=350,
    )
    audit = run_audit(low, high)
    assert audit.disposition is MeasurementAuditDisposition.VALID_CONDITION_STRATIFIED
    assert len(audit.incompatible_conditions) == 1
    assert audit.incompatible_conditions[0].incompatible_condition_ids == ("temperature",)
    assert len({item.condition_stratum_sha256 for item in audit.variability}) == 2
    assert all(item.measurement_count == 1 for item in audit.variability)


def test_exact_duplicate_is_retained_once_and_excluded_once():
    item = material("duplicate-material")
    first = measurement(
        "duplicate-record-a",
        item,
        artifact_key="shared-raw",
        measurement_id="shared-measurement",
    )
    second = measurement(
        "duplicate-record-b",
        item,
        artifact_key="shared-raw",
        measurement_id="shared-measurement",
    )
    audit = run_audit(first, second)
    assert audit.disposition is MeasurementAuditDisposition.VALID_WITH_EXCLUSIONS
    assert len(audit.exact_duplicates) == 1
    assert len(audit.eligible_record_sha256s) == 1
    assert "exact_duplicate" in audit.exclusions[0].reason_codes


def test_same_provenance_with_different_projection_is_a_conflict_not_a_duplicate():
    item = material("projection-conflict-material")
    first = measurement(
        "projection-conflict-a",
        item,
        value=1.0,
        artifact_key="projection-shared-raw",
        measurement_id="projection-shared-measurement",
    )
    second = measurement(
        "projection-conflict-b",
        item,
        value=1.1,
        artifact_key="projection-shared-raw",
        measurement_id="projection-shared-measurement",
    )
    independent = measurement(
        "projection-independent",
        material(
            "projection-independent-material",
            batch_id="projection-batch-2",
            sample_id="projection-sample-2",
        ),
        value=1.05,
    )
    audit = run_audit(first, second, independent)
    assert audit.disposition is MeasurementAuditDisposition.REQUIRES_REVIEW_CONFLICT
    assert audit.exact_duplicates == ()
    assert len(audit.provenance_conflicts) == 1
    assert len(audit.eligible_record_sha256s) == 1
    assert all(
        "conflicting_projection_for_same_provenance" in item.reason_codes
        for item in audit.exclusions
    )


def test_conflicting_same_sample_repeat_is_flagged_and_excluded_from_variability():
    shared = material("conflict-material")
    low = measurement("conflict-low", shared, value=1.0, source_group="source-A")
    high = measurement("conflict-high", shared, value=2.0, source_group="source-B")
    independent = measurement(
        "independent-ok",
        material("independent-material", batch_id="batch-002", sample_id="sample-002"),
        value=1.1,
        source_group="source-C",
    )
    audit = run_audit(low, high, independent)
    assert audit.disposition is MeasurementAuditDisposition.REQUIRES_REVIEW_CONFLICT
    assert len(audit.conflicts) == 1
    assert len(audit.eligible_record_sha256s) == 1
    assert all(item.measurement_count == 1 for item in audit.variability)
    assert all("conflicting_same_sample_repeat" in item.reason_codes for item in audit.exclusions)


def test_compatible_repeats_estimate_noise_and_three_variability_levels():
    sample_a = material("material-a", batch_id="batch-A", sample_id="sample-A")
    sample_b = material("material-b", batch_id="batch-B", sample_id="sample-B")
    records = (
        measurement("a-source-1", sample_a, value=1.00, source_group="source-1"),
        measurement("a-source-2", sample_a, value=1.04, source_group="source-2"),
        measurement("b-source-1", sample_b, value=1.10, source_group="source-1"),
        measurement("b-source-2", sample_b, value=1.14, source_group="source-2"),
    )
    audit = run_audit(*records)
    assert audit.disposition is MeasurementAuditDisposition.CLEAN
    assert len(audit.same_sample_repeats) == 2
    estimates = {item.level: item for item in audit.variability}
    assert set(estimates) == set(VariabilityLevel)
    assert all(item.availability is EstimateAvailability.AVAILABLE for item in estimates.values())
    assert estimates[VariabilityLevel.WITHIN_SAMPLE].group_count == 2
    assert estimates[VariabilityLevel.BETWEEN_BATCH].group_count == 2
    assert estimates[VariabilityLevel.BETWEEN_SOURCE].group_count == 2
    assert estimates[VariabilityLevel.WITHIN_SAMPLE].median_standard_uncertainty == 0.02


def test_measurement_audit_derived_fields_cannot_be_relabelled():
    audit = run_audit(measurement("one-valid", material("one-valid-material")))
    payload = audit.model_dump(mode="json")
    payload["eligible_record_sha256s"] = []
    with pytest.raises(ValidationError, match="eligible_record_sha256s"):
        MaterialMeasurementAudit.model_validate(payload)
