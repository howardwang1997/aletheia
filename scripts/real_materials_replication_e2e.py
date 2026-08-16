"""Freeze, execute, aggregate, and audit the F10 materials replication matrix."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from aletheia.capabilities import CapabilityRegistrySnapshot, ExperimentCapabilityManifest
from aletheia.domains.materials.capabilities.replication import (
    MaterialsReplicationAggregation,
    MaterialsReplicationBundle,
    MaterialsReplicationPlan,
    MaterialsReplicationSlotEvidence,
    assemble_materials_replication_bundle,
    assemble_materials_replication_slot_evidence,
    build_materials_replication_plan,
    derive_materials_replication_aggregation,
    materials_replication_implementation_sha256,
    verify_materials_replication_bundle,
)
from aletheia.domains.materials.k3_evidence import (
    MaterialsBeliefUpdate,
    MaterialsK3Protocol,
    MaterialsPreregistration,
    SignedMaterialsObservation,
    SignedMaterialsValidation,
    derive_materials_belief_update,
    measure_materials_experiment,
    validate_materials_observation,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate(_read(path))


def _key(path: Path) -> bytes:
    resolved = path.expanduser().resolve(strict=True)
    value = resolved.read_bytes()
    if len(value) < 32:
        raise ValueError(f"signing key {resolved} must contain at least 32 bytes")
    return value


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace immutable evidence: {destination}")
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


def _atomic_new_key(path: Path) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace signing key: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(48))
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


def _ensure_exact(path: Path, expected: ModelT, model_type: type[ModelT]) -> ModelT:
    if path.exists():
        actual = _model(path, model_type)
        if actual != expected:
            raise ValueError(f"immutable artifact differs from frozen value: {path}")
        return actual
    _atomic_new_json(path, expected)
    return expected


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _preregister(args: argparse.Namespace) -> None:
    manifest = _model(args.manifest, ExperimentCapabilityManifest)
    registry = _model(args.registry, CapabilityRegistrySnapshot)
    base_protocol = _model(args.base_protocol, MaterialsK3Protocol)
    protocol_frozen_at = datetime.now(timezone.utc)
    preregistered_at = protocol_frozen_at + timedelta(microseconds=1)
    plan = build_materials_replication_plan(
        plan_id=args.plan_id,
        manifest=manifest,
        registry=registry,
        base_protocol=base_protocol,
        protocol_frozen_at=protocol_frozen_at,
        preregistered_at=preregistered_at,
        frozen_at=preregistered_at + timedelta(microseconds=1),
    )
    destination = _atomic_new_json(args.output, plan)
    _print(
        {
            "output": str(destination),
            "plan_sha256": plan.plan_sha256,
            "capability_manifest_sha256": plan.capability_manifest_sha256,
            "registry_snapshot_sha256": plan.registry_snapshot_sha256,
            "replication_implementation_sha256": (plan.replication_implementation_sha256),
            "seeds": [item.seed for item in plan.slots],
            "all_slots_frozen_before_measurement": True,
            "evidence_level": plan.evidence_level,
        }
    )


def _init_keys(args: argparse.Namespace) -> None:
    measurement = _atomic_new_key(args.measurement_key)
    validation = _atomic_new_key(args.validation_key)
    _print(
        {
            "measurement_key": str(measurement),
            "validation_key": str(validation),
            "distinct_keys": _key(measurement) != _key(validation),
            "permissions": "0600",
        }
    )


def _load_or_measure(
    *,
    path: Path,
    commitment,
    measurement_key: bytes,
) -> SignedMaterialsObservation:
    policy = commitment.preregistration.protocol.evidence_policy
    if path.exists():
        observation = _model(path, SignedMaterialsObservation)
        observation.verify(key=measurement_key, expected_key_id=policy.measurement_key_id)
        return observation
    observation = measure_materials_experiment(
        preregistration=commitment.preregistration,
        signing_key=measurement_key,
    )
    _atomic_new_json(path, observation)
    return observation


def _load_or_validate(
    *,
    path: Path,
    commitment,
    observation: SignedMaterialsObservation,
    measurement_key: bytes,
    validation_key: bytes,
) -> SignedMaterialsValidation:
    policy = commitment.preregistration.protocol.evidence_policy
    if path.exists():
        validation = _model(path, SignedMaterialsValidation)
        validation.verify(key=validation_key, expected_key_id=policy.validation_key_id)
        return validation
    validation = validate_materials_observation(
        preregistration=commitment.preregistration,
        signed_observation=observation,
        observation_key=measurement_key,
        validation_key=validation_key,
    )
    _atomic_new_json(path, validation)
    return validation


def _load_or_update(
    *,
    path: Path,
    commitment,
    observation: SignedMaterialsObservation,
    validation: SignedMaterialsValidation,
    measurement_key: bytes,
    validation_key: bytes,
) -> MaterialsBeliefUpdate:
    if path.exists():
        update = _model(path, MaterialsBeliefUpdate)
        expected = derive_materials_belief_update(
            preregistration=commitment.preregistration,
            signed_observation=observation,
            signed_validation=validation,
            observation_key=measurement_key,
            validation_key=validation_key,
            updated_at=update.updated_at,
        )
        if expected != update:
            raise ValueError(f"stored update is not mechanically derived: {path}")
        return update
    update = derive_materials_belief_update(
        preregistration=commitment.preregistration,
        signed_observation=observation,
        signed_validation=validation,
        observation_key=measurement_key,
        validation_key=validation_key,
    )
    _atomic_new_json(path, update)
    return update


def _load_or_slot_evidence(
    *,
    path: Path,
    plan: MaterialsReplicationPlan,
    commitment,
    observation: SignedMaterialsObservation,
    validations: tuple[SignedMaterialsValidation, ...],
    update: MaterialsBeliefUpdate,
) -> MaterialsReplicationSlotEvidence:
    if path.exists():
        evidence = _model(path, MaterialsReplicationSlotEvidence)
        if (
            evidence.slot_id != commitment.slot_id
            or evidence.slot_commitment_sha256 != commitment.commitment_sha256
            or evidence.signed_observation != observation
            or tuple(item.signed_validation for item in evidence.exact_reexecutions) != validations
            or evidence.update != update
        ):
            raise ValueError(f"stored slot evidence does not match checkpoints: {path}")
        return evidence
    evidence = assemble_materials_replication_slot_evidence(
        commitment=commitment,
        signed_observation=observation,
        exact_reexecutions=validations,
        update=update,
        required_exact_reexecutions=(plan.aggregation_rule.required_exact_reexecutions_per_slot),
    )
    _atomic_new_json(path, evidence)
    return evidence


def _record_failure(path: Path, *, slot_id: str, phase: str, error: Exception) -> None:
    if path.exists():
        return
    _atomic_new_json(
        path,
        {
            "schema_name": "aletheia.materials_replication_failure",
            "schema_version": 1,
            "slot_id": slot_id,
            "phase": phase,
            "error_type": type(error).__name__,
            "error": str(error),
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retry_forbidden_without_new_preregistration": True,
        },
    )


def _run_all(args: argparse.Namespace) -> None:
    plan = _model(args.plan, MaterialsReplicationPlan)
    if plan.replication_implementation_sha256 != materials_replication_implementation_sha256():
        raise ValueError("current replication implementation differs from frozen plan")
    measurement_key = _key(args.measurement_key)
    validation_key = _key(args.validation_key)
    if measurement_key == validation_key:
        raise ValueError("measurement and validation keys must be physically distinct")
    root = args.output_dir.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Materialize every preregistration before any dataset-bearing execution starts.
    for commitment in plan.slots:
        slot_root = root / commitment.slot_id
        _ensure_exact(
            slot_root / "preregistration.json",
            commitment.preregistration,
            MaterialsPreregistration,
        )

    evidence_rows: list[MaterialsReplicationSlotEvidence] = []
    for commitment in plan.slots:
        slot_root = root / commitment.slot_id
        failure_path = slot_root / "failure.json"
        if failure_path.exists():
            raise RuntimeError(
                f"{commitment.slot_id} has a retained failure; a new preregistration is required"
            )
        phase = "measurement"
        try:
            observation = _load_or_measure(
                path=slot_root / "observation.json",
                commitment=commitment,
                measurement_key=measurement_key,
            )
            validations: list[SignedMaterialsValidation] = []
            for index in range(
                1,
                plan.aggregation_rule.required_exact_reexecutions_per_slot + 1,
            ):
                phase = f"exact_reexecution_{index}"
                validations.append(
                    _load_or_validate(
                        path=slot_root / f"validation-{index}.json",
                        commitment=commitment,
                        observation=observation,
                        measurement_key=measurement_key,
                        validation_key=validation_key,
                    )
                )
            phase = "belief_update"
            update = _load_or_update(
                path=slot_root / "update.json",
                commitment=commitment,
                observation=observation,
                validation=validations[-1],
                measurement_key=measurement_key,
                validation_key=validation_key,
            )
            phase = "slot_assembly"
            evidence_rows.append(
                _load_or_slot_evidence(
                    path=slot_root / "evidence.json",
                    plan=plan,
                    commitment=commitment,
                    observation=observation,
                    validations=tuple(validations),
                    update=update,
                )
            )
        except Exception as error:
            _record_failure(
                failure_path,
                slot_id=commitment.slot_id,
                phase=phase,
                error=error,
            )
            raise

    evidence = tuple(evidence_rows)
    aggregation_path = root / "aggregation.json"
    if aggregation_path.exists():
        aggregation = _model(aggregation_path, MaterialsReplicationAggregation)
        expected = derive_materials_replication_aggregation(
            plan=plan,
            evidence=evidence,
            aggregated_at=aggregation.aggregated_at,
        )
        if aggregation != expected:
            raise ValueError("stored replication aggregation is not mechanically derived")
    else:
        aggregation = derive_materials_replication_aggregation(plan=plan, evidence=evidence)
        _atomic_new_json(aggregation_path, aggregation)

    bundle_path = root / "bundle.json"
    if bundle_path.exists():
        bundle = _model(bundle_path, MaterialsReplicationBundle)
        if (
            bundle.plan != plan
            or bundle.slot_evidence != evidence
            or bundle.aggregation != aggregation
        ):
            raise ValueError("stored replication bundle differs from its immutable inputs")
    else:
        bundle = assemble_materials_replication_bundle(
            plan=plan,
            evidence=evidence,
            aggregation=aggregation,
        )
        _atomic_new_json(bundle_path, bundle)
    verify_materials_replication_bundle(
        bundle=bundle,
        observation_key=measurement_key,
        validation_key=validation_key,
    )
    _print(
        {
            "bundle": str(bundle_path),
            "bundle_sha256": bundle.bundle_sha256,
            "aggregation_sha256": aggregation.aggregation_sha256,
            "pattern": aggregation.pattern.value,
            "consensus_outcome_id": (
                aggregation.consensus_outcome_id.value
                if aggregation.consensus_outcome_id is not None
                else None
            ),
            "outcome_counts": {
                key.value: value for key, value in aggregation.outcome_counts.items()
            },
            "delta_mean": aggregation.delta_mean,
            "delta_median": aggregation.delta_median,
            "delta_sample_sd": aggregation.delta_sample_sd,
            "all_slots_included": aggregation.all_slots_included,
            "exact_reexecutions_per_slot": (
                plan.aggregation_rule.required_exact_reexecutions_per_slot
            ),
            "supports_capability_promotion": aggregation.supports_capability_promotion,
        }
    )


def _inspect(args: argparse.Namespace) -> None:
    plan = _model(args.plan, MaterialsReplicationPlan)
    _print(
        {
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "capability_manifest_sha256": plan.capability_manifest_sha256,
            "registry_snapshot_sha256": plan.registry_snapshot_sha256,
            "replication_implementation_sha256": (plan.replication_implementation_sha256),
            "slots": [
                {
                    "slot_id": item.slot_id,
                    "seed": item.seed,
                    "protocol_sha256": item.preregistration.protocol_sha256,
                    "preregistration_sha256": item.preregistration.preregistration_sha256,
                }
                for item in plan.slots
            ],
            "aggregation_rule": plan.aggregation_rule.model_dump(mode="json"),
            "state": plan.state,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    bundle = _model(args.bundle, MaterialsReplicationBundle)
    verify_materials_replication_bundle(
        bundle=bundle,
        observation_key=_key(args.measurement_key),
        validation_key=_key(args.validation_key),
        physically_recompute=args.recompute,
    )
    _print(
        {
            "bundle_sha256": bundle.bundle_sha256,
            "signatures_valid": True,
            "all_slot_updates_derived": True,
            "aggregation_derived": True,
            "physical_recomputation_performed": args.recompute,
            "physical_recomputation_slots": (len(bundle.plan.slots) if args.recompute else 0),
            "pattern": bundle.aggregation.pattern.value,
        }
    )


def _add_keys(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--measurement-key", type=Path, required=True)
    parser.add_argument("--validation-key", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="F10 materials replication matrix")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("--plan-id", required=True)
    preregister.add_argument("--manifest", type=Path, required=True)
    preregister.add_argument("--registry", type=Path, required=True)
    preregister.add_argument("--base-protocol", type=Path, required=True)
    preregister.add_argument("--output", type=Path, required=True)
    preregister.set_defaults(handler=_preregister)

    init_keys = subparsers.add_parser("init-keys")
    _add_keys(init_keys)
    init_keys.set_defaults(handler=_init_keys)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--plan", type=Path, required=True)
    inspect.set_defaults(handler=_inspect)

    run_all = subparsers.add_parser("run-all")
    run_all.add_argument("--plan", type=Path, required=True)
    _add_keys(run_all)
    run_all.add_argument("--output-dir", type=Path, required=True)
    run_all.set_defaults(handler=_run_all)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    _add_keys(verify)
    verify.add_argument("--recompute", action="store_true")
    verify.set_defaults(handler=_verify)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
