"""Prepare, execute, and audit a typed F10-S2 materials observation reexecution."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from aletheia.capabilities import (
    CapabilityObservationArchive,
    CapabilityObservationValidationPolicy,
    CapabilityRegistry,
    CapabilityRegistrySnapshot,
    CommittedCapabilityObservationPipeline,
    ExperimentCapabilityManifest,
    ExperimentRunPurpose,
    ExperimentRunStatus,
    build_raw_experiment_run,
    commit_capability_observation_pipeline,
    load_committed_capability_observation_pipeline,
    parse_capability_observation,
    validate_capability_observation,
)
from aletheia.capabilities.schemas import CapabilityRole
from aletheia.domains.materials.capabilities.range_compression_validator import (
    TypedRangeCompressionValidator,
    build_range_compression_validation_policy,
    typed_range_compression_validator_implementation_sha256,
)
from aletheia.domains.materials.capabilities.typed_range_compression import (
    TypedRangeCompressionParser,
    typed_range_compression_parser_implementation_sha256,
)
from aletheia.domains.materials.capabilities.replication import MaterialsReplicationBundle
from aletheia.domains.materials.k3_evidence import (
    MaterialsPreregistration,
    run_materials_experiment,
)
from aletheia.evals.schemas import FrozenModel
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from aletheia.reproducibility.manifest import content_sha256


ModelT = TypeVar("ModelT", bound=BaseModel)


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate(_read(path))


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace immutable typed evidence: {destination}")
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


class TypedObservationDemoPurpose(str, Enum):
    NEGATIVE_RESULT_PRESERVATION = "negative_result_preservation"


class TypedMaterialsObservationPlan(FrozenModel):
    schema_name: Literal["aletheia.typed_materials_observation_plan"] = (
        "aletheia.typed_materials_observation_plan"
    )
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    manifest: ExperimentCapabilityManifest
    registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_replication_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_replication_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_slot_id: str
    source_slot_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_outcome_id: str
    preregistration: MaterialsPreregistration
    validation_policy: CapabilityObservationValidationPolicy
    run_purpose: Literal["exact_reexecution"] = "exact_reexecution"
    demonstration_purpose: TypedObservationDemoPurpose
    new_scientific_evidence_admission_forbidden: Literal[True] = True
    prepared_at: AwareDatetime
    state: Literal["frozen_before_reexecution"] = "frozen_before_reexecution"

    @model_validator(mode="after")
    def _plan_binds_current_typed_roles(self) -> "TypedMaterialsObservationPlan":
        policy = self.validation_policy
        if policy.capability_manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("typed observation policy is bound to another manifest")
        if policy.frozen_at > self.prepared_at:
            raise ValueError("typed observation plan predates its validation policy")
        if self.preregistration.preregistered_at >= self.prepared_at:
            raise ValueError("typed observation plan predates its source preregistration")
        parser = next(
            item for item in self.manifest.roles if item.role is CapabilityRole.OBSERVATION_PARSER
        )
        validator = next(
            item for item in self.manifest.roles if item.role is CapabilityRole.VALIDATOR
        )
        if (
            parser.adapter_ref != TypedRangeCompressionParser.adapter_ref
            or parser.implementation_sha256
            != typed_range_compression_parser_implementation_sha256()
            or validator.adapter_ref != TypedRangeCompressionValidator.adapter_ref
            or validator.implementation_sha256
            != typed_range_compression_validator_implementation_sha256()
        ):
            raise ValueError("typed observation plan role implementation differs from manifest")
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


def _prepare(args: argparse.Namespace) -> None:
    manifest = _model(args.manifest, ExperimentCapabilityManifest)
    registry = _model(args.registry, CapabilityRegistrySnapshot)
    selected = CapabilityRegistry(registry).get(
        manifest.capability_id,
        version=manifest.version,
        allow_provisional=True,
    )
    if selected != manifest:
        raise ValueError("registry capability differs from supplied typed manifest")
    source = _model(args.source_bundle, MaterialsReplicationBundle)
    rows = [item for item in source.slot_evidence if item.slot_id == args.slot_id]
    commitments = [item for item in source.plan.slots if item.slot_id == args.slot_id]
    if len(rows) != 1 or len(commitments) != 1:
        raise ValueError("source replication bundle does not contain the requested exact slot")
    evidence = rows[0]
    commitment = commitments[0]
    outcome = evidence.signed_observation.observation.result.outcome_id
    if outcome.value != "generic_model_shrinkage":
        raise ValueError("negative-preservation demo requires a frozen generic-shrinkage slot")
    policy_frozen_at = datetime.now(timezone.utc)
    policy = build_range_compression_validation_policy(
        manifest=manifest,
        frozen_at=policy_frozen_at,
    )
    plan = TypedMaterialsObservationPlan(
        plan_id=args.plan_id,
        manifest=manifest,
        registry_snapshot_sha256=registry.snapshot_sha256,
        source_replication_bundle_sha256=source.bundle_sha256,
        source_replication_plan_sha256=source.plan.plan_sha256,
        source_slot_id=args.slot_id,
        source_slot_evidence_sha256=evidence.evidence_sha256,
        source_result_sha256=evidence.signed_observation.observation.result.result_sha256,
        source_outcome_id=outcome.value,
        preregistration=commitment.preregistration,
        validation_policy=policy,
        demonstration_purpose=(TypedObservationDemoPurpose.NEGATIVE_RESULT_PRESERVATION),
        prepared_at=policy_frozen_at + timedelta(microseconds=1),
    )
    destination = _atomic_new_json(args.output, plan)
    _print(
        {
            "output": str(destination),
            "plan_sha256": plan.plan_sha256,
            "manifest_sha256": manifest.manifest_sha256,
            "registry_snapshot_sha256": registry.snapshot_sha256,
            "source_slot_id": plan.source_slot_id,
            "source_outcome_id": plan.source_outcome_id,
            "run_purpose": plan.run_purpose,
            "new_scientific_evidence_admission_forbidden": True,
        }
    )


def _run(args: argparse.Namespace) -> None:
    plan = _model(args.plan, TypedMaterialsObservationPlan)
    policy = plan.validation_policy
    workspace = args.workspace.expanduser().resolve(strict=False)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_archive = CapabilityObservationArchive(workspace / "raw-archive")
    ledger_archive = ContentAddressedResponseArchive(workspace / "ledger-archive")
    started_at = datetime.now(timezone.utc)
    if started_at <= plan.prepared_at:
        started_at = plan.prepared_at + timedelta(microseconds=1)
    result = run_materials_experiment(plan.preregistration)
    ended_at = datetime.now(timezone.utc)
    if result.result_sha256 != plan.source_result_sha256:
        raise ValueError("typed reexecution differs from its frozen source result")
    receipt = raw_archive.store(
        artifact_id="result",
        payload=result.model_dump_json().encode(),
        media_type="application/json",
        captured_at=ended_at,
    )
    raw_run = build_raw_experiment_run(
        run_id=f"{plan.plan_id}.run",
        manifest=plan.manifest,
        preregistration_sha256=plan.preregistration.preregistration_sha256,
        input_sha256=content_sha256(
            {
                "preregistration_sha256": (plan.preregistration.preregistration_sha256),
                "source_result_sha256": plan.source_result_sha256,
            }
        ),
        status=ExperimentRunStatus.SUCCEEDED,
        artifacts=(receipt,),
        started_at=started_at,
        ended_at=ended_at,
        run_purpose=ExperimentRunPurpose.EXACT_REEXECUTION,
    )
    parsed_at = datetime.now(timezone.utc)
    parsed = parse_capability_observation(
        manifest=plan.manifest,
        raw_run=raw_run,
        archive=raw_archive,
        adapter=TypedRangeCompressionParser(),
        parsed_at=parsed_at,
    )
    validated_at = datetime.now(timezone.utc)
    pipeline = validate_capability_observation(
        manifest=plan.manifest,
        policy=policy,
        parse_result=parsed,
        archive=raw_archive,
        adapter=TypedRangeCompressionValidator(preregistration=plan.preregistration),
        validated_at=validated_at,
    )
    committed_at = datetime.now(timezone.utc)
    committed = commit_capability_observation_pipeline(
        archive=ledger_archive,
        result=pipeline,
        committed_at=committed_at,
    )
    _atomic_new_json(workspace / "raw-run.json", raw_run)
    _atomic_new_json(workspace / "parse-result.json", parsed)
    _atomic_new_json(workspace / "pipeline.json", pipeline)
    _atomic_new_json(workspace / "committed.json", committed)
    validation = pipeline.validation
    _print(
        {
            "pipeline_sha256": pipeline.pipeline_sha256,
            "commitment_sha256": committed.receipt_sha256,
            "result_sha256": result.result_sha256,
            "disposition": pipeline.disposition.value,
            "scientific_negative_preserved": (
                validation.scientific_negative_preserved if validation else False
            ),
            "admissible_for_f9_exploratory_update": (
                validation.admissible_for_f9_exploratory_update if validation else False
            ),
            "admissible_for_f9_confirmatory_update": (
                validation.admissible_for_f9_confirmatory_update if validation else False
            ),
            "raw_artifact_sha256": receipt.sha256,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    plan = _model(args.plan, TypedMaterialsObservationPlan)
    committed = _model(args.committed, CommittedCapabilityObservationPipeline)
    workspace = args.workspace.expanduser().resolve(strict=True)
    loaded = load_committed_capability_observation_pipeline(
        ledger_archive=ContentAddressedResponseArchive(workspace / "ledger-archive"),
        raw_archive=CapabilityObservationArchive(workspace / "raw-archive"),
        committed=committed,
    )
    physically_recomputed = False
    if args.recompute:
        result = run_materials_experiment(plan.preregistration)
        if result.result_sha256 != plan.source_result_sha256:
            raise ValueError("physical typed-observation audit differs from frozen source")
        physically_recomputed = True
    validation = loaded.validation
    _print(
        {
            "pipeline_sha256": loaded.pipeline_sha256,
            "physical_raw_and_ledger_reload": True,
            "physical_model_recomputation": physically_recomputed,
            "disposition": loaded.disposition.value,
            "scientific_negative_preserved": (
                validation.scientific_negative_preserved if validation else False
            ),
            "new_scientific_evidence_admission_forbidden": (
                not validation.admissible_for_f9_exploratory_update if validation else True
            ),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Typed materials observation pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--plan-id", required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--registry", type=Path, required=True)
    prepare.add_argument("--source-bundle", type=Path, required=True)
    prepare.add_argument("--slot-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    run = subparsers.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.set_defaults(handler=_run)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--committed", type=Path, required=True)
    verify.add_argument("--workspace", type=Path, required=True)
    verify.add_argument("--recompute", action="store_true")
    verify.set_defaults(handler=_verify)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
