"""Dataset-level identity coverage and unresolved collision tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from aletheia.domains.materials.capabilities.measurement_audit import (
    DatasetCoverageStatus,
    DatasetIdentityAuditDisposition,
    MaterialsIdentityColumnMap,
    audit_materials_dataframe_identity,
    report_composition_collisions,
)
from aletheia.domains.materials.identity import LicensedSourceArtifact


BASE = datetime(2026, 8, 15, 8, tzinfo=timezone.utc)


def sha(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(payload).hexdigest()


def dataset_source() -> LicensedSourceArtifact:
    payload = b"synthetic composition dataset"
    return LicensedSourceArtifact(
        artifact_id="synthetic-dataset",
        sha256=sha(payload),
        bytes=len(payload),
        media_type="application/json",
        source_uri="https://example.test/synthetic-dataset",
        license_expression="CC0-1.0",
        license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
        license_evidence_sha256=sha("CC0 licence fixture"),
        retrieved_at=BASE,
    )


def test_composition_only_dataset_is_bounded_and_collisions_remain_unresolved():
    dataframe = pd.DataFrame(
        {
            "formula": ["Fe2O3", "O3Fe2", "NaCl"],
            "gap": [1.0, 2.0, 3.0],
        }
    )
    column_map = MaterialsIdentityColumnMap(
        formula_column="formula",
        property_value_column="gap",
        dataset_level_property_unit_ucum="eV",
    )
    audit = audit_materials_dataframe_identity(
        audit_id="synthetic-identity-audit",
        dataset_ref="fixture://composition-only",
        dataset_source=dataset_source(),
        dataframe=dataframe,
        column_map=column_map,
        audited_at=BASE,
    )
    assert audit.disposition is DatasetIdentityAuditDisposition.COMPOSITION_BENCHMARK_ONLY
    assert audit.unique_formula_identities == 2
    assert audit.composition_collision_groups == 1
    assert audit.measurement_audit_eligible is False
    assert "sample_identity_absent" in audit.blockers
    unit = next(item for item in audit.field_coverage if item.field_id == "property_unit")
    assert unit.status is DatasetCoverageStatus.DATASET_METADATA_ONLY

    report = report_composition_collisions(
        report_id="synthetic-collision-report",
        source_audit=audit,
        dataframe=dataframe,
        reported_at=BASE,
    )
    assert report.collision_group_count == 1
    assert report.affected_row_count == 2
    assert report.maximum_property_range == 1.0
    assert report.collisions[0].duplicate_or_conflict_determination_forbidden is True


def test_unparseable_formula_or_nonfinite_target_rejects_even_composition_benchmark():
    dataframe = pd.DataFrame(
        {
            "formula": ["not a formula", "NaCl"],
            "gap": [1.0, float("nan")],
        }
    )
    audit = audit_materials_dataframe_identity(
        audit_id="invalid-identity-audit",
        dataset_ref="fixture://invalid",
        dataset_source=dataset_source(),
        dataframe=dataframe,
        column_map=MaterialsIdentityColumnMap(
            formula_column="formula",
            property_value_column="gap",
            dataset_level_property_unit_ucum="eV",
        ),
        audited_at=BASE,
    )
    assert audit.disposition is DatasetIdentityAuditDisposition.REJECTED_FORMULA_OR_TARGET_INVALID
    assert "formula_parse_failure" in audit.blockers
    assert "property_value_invalid" in audit.blockers
