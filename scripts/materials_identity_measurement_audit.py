"""Run and physically replay the F10-S3 Matbench identity/measurement audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aletheia.domains.materials.capabilities.measurement_audit import (
    MaterialsCompositionCollisionReport,
    MaterialsDatasetIdentityAudit,
    MaterialsIdentityColumnMap,
    audit_materials_dataframe_identity,
    report_composition_collisions,
)
from aletheia.domains.materials.datasets import load_benchmark
from aletheia.domains.materials.identity import LicensedSourceArtifact


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace immutable identity audit: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_audit(path: Path) -> MaterialsDatasetIdentityAudit:
    return MaterialsDatasetIdentityAudit.model_validate_json(
        path.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )


def _load_collision_report(path: Path) -> MaterialsCompositionCollisionReport:
    return MaterialsCompositionCollisionReport.model_validate_json(
        path.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )


def _column_map() -> MaterialsIdentityColumnMap:
    return MaterialsIdentityColumnMap(
        formula_column="composition",
        property_value_column="gap expt",
        dataset_level_property_unit_ucum="eV",
    )


def _run(args: argparse.Namespace) -> None:
    dataset_path = args.dataset_file.expanduser().resolve(strict=True)
    payload = dataset_path.read_bytes()
    audited_at = datetime.now(timezone.utc)
    source = LicensedSourceArtifact(
        artifact_id="matbench-expt-gap-json-gz",
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        media_type="application/gzip",
        source_uri="https://ml.materialsproject.org/projects/matbench_expt_gap.json.gz",
        license_expression="NOASSERTION",
        license_uri="https://github.com/materialsproject/matbench",
        license_evidence_sha256=hashlib.sha256(
            (
                "matminer dataset metadata inspected 2026-08-15: citations and source URL "
                "are present, but no dataset-specific licence field is declared; the Matbench "
                "code repository licence is not assumed to license upstream measurements"
            ).encode()
        ).hexdigest(),
        retrieved_at=audited_at,
    )
    source.verify_bytes(payload)
    dataframe = load_benchmark(args.dataset_ref)
    audit = audit_materials_dataframe_identity(
        audit_id=args.audit_id,
        dataset_ref=args.dataset_ref,
        dataset_source=source,
        dataframe=dataframe,
        column_map=_column_map(),
        audited_at=audited_at,
    )
    destination = _atomic_new_json(args.output, audit)
    _print(
        {
            "audit": str(destination),
            "audit_sha256": audit.audit_sha256,
            "dataset_file_sha256": source.sha256,
            "logical_rows_sha256": audit.logical_rows_sha256,
            "row_count": audit.row_count,
            "unique_formula_identities": audit.unique_formula_identities,
            "unique_chemical_system_identities": audit.unique_chemical_system_identities,
            "disposition": audit.disposition.value,
            "measurement_audit_eligible": audit.measurement_audit_eligible,
            "structure_experiment_eligible": audit.structure_experiment_eligible,
            "blockers": list(audit.blockers),
        }
    )


def _verify(args: argparse.Namespace) -> None:
    audit = _load_audit(args.audit)
    dataset_path = args.dataset_file.expanduser().resolve(strict=True)
    audit.dataset_source.verify_bytes(dataset_path.read_bytes())
    dataframe = load_benchmark(audit.dataset_ref)
    replay = audit_materials_dataframe_identity(
        audit_id=audit.audit_id,
        dataset_ref=audit.dataset_ref,
        dataset_source=audit.dataset_source,
        dataframe=dataframe,
        column_map=audit.column_map,
        audited_at=audit.audited_at,
    )
    if replay != audit:
        raise ValueError("physical dataset replay differs from committed identity audit")
    _print(
        {
            "audit_sha256": audit.audit_sha256,
            "dataset_bytes_rehashed": True,
            "logical_rows_recomputed": True,
            "formula_identities_recomputed": True,
            "audit_exactly_replayed": True,
            "disposition": audit.disposition.value,
            "blockers": list(audit.blockers),
        }
    )


def _report_collisions(args: argparse.Namespace) -> None:
    audit = _load_audit(args.audit)
    dataset_path = args.dataset_file.expanduser().resolve(strict=True)
    audit.dataset_source.verify_bytes(dataset_path.read_bytes())
    report = report_composition_collisions(
        report_id=args.report_id,
        source_audit=audit,
        dataframe=load_benchmark(audit.dataset_ref),
        reported_at=datetime.now(timezone.utc),
    )
    destination = _atomic_new_json(args.output, report)
    _print(
        {
            "report": str(destination),
            "report_sha256": report.report_sha256,
            "source_audit_sha256": report.source_audit_sha256,
            "collision_group_count": report.collision_group_count,
            "affected_row_count": report.affected_row_count,
            "maximum_property_range_eV": report.maximum_property_range,
            "collisions": [
                {
                    "canonical_formula": item.canonical_formula,
                    "raw_formulas": list(item.raw_formulas),
                    "property_values_eV": list(item.property_values),
                    "property_range_eV": item.property_range,
                }
                for item in report.collisions
            ],
            "disposition": report.disposition,
        }
    )


def _verify_collisions(args: argparse.Namespace) -> None:
    audit = _load_audit(args.audit)
    report = _load_collision_report(args.report)
    dataset_path = args.dataset_file.expanduser().resolve(strict=True)
    audit.dataset_source.verify_bytes(dataset_path.read_bytes())
    replay = report_composition_collisions(
        report_id=report.report_id,
        source_audit=audit,
        dataframe=load_benchmark(audit.dataset_ref),
        reported_at=report.reported_at,
    )
    if replay != report:
        raise ValueError("physical collision replay differs from committed report")
    _print(
        {
            "report_sha256": report.report_sha256,
            "source_audit_sha256": audit.audit_sha256,
            "dataset_bytes_rehashed": True,
            "collision_identities_recomputed": True,
            "report_exactly_replayed": True,
            "disposition": report.disposition,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="create one immutable real-dataset audit")
    run.add_argument("--dataset-ref", default="matbench_expt_gap")
    run.add_argument("--dataset-file", type=Path, required=True)
    run.add_argument("--audit-id", default="matbench-expt-gap-identity-audit-v1")
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)
    verify = subparsers.add_parser("verify", help="rehash and fully replay an existing audit")
    verify.add_argument("--audit", type=Path, required=True)
    verify.add_argument("--dataset-file", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    collisions = subparsers.add_parser(
        "report-collisions", help="retain unresolved normalized-composition collisions"
    )
    collisions.add_argument("--audit", type=Path, required=True)
    collisions.add_argument("--dataset-file", type=Path, required=True)
    collisions.add_argument("--report-id", default="matbench-expt-gap-collisions-v1")
    collisions.add_argument("--output", type=Path, required=True)
    collisions.set_defaults(handler=_report_collisions)
    verify_collisions = subparsers.add_parser(
        "verify-collisions", help="rehash and fully replay a collision report"
    )
    verify_collisions.add_argument("--audit", type=Path, required=True)
    verify_collisions.add_argument("--report", type=Path, required=True)
    verify_collisions.add_argument("--dataset-file", type=Path, required=True)
    verify_collisions.set_defaults(handler=_verify_collisions)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
