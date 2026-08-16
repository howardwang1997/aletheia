"""Build and audit a real, signed F9 materials evidence chain.

The commands are intentionally split so preregistration is physically written before the benchmark
is loaded.  Measurement and validation use separate keys; validation reruns the complete frozen
experiment before issuing its receipt.

Example::

    conda run -n aletheia python scripts/real_k3_materials_e2e.py inspect \
      --protocol configs/materials/k3_band_gap_range_compression_v1.yaml
    conda run -n aletheia python scripts/real_k3_materials_e2e.py preregister ...
    conda run -n aletheia python scripts/real_k3_materials_e2e.py measure ...
    conda run -n aletheia python scripts/real_k3_materials_e2e.py validate ...
    conda run -n aletheia python scripts/real_k3_materials_e2e.py update ...
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from aletheia.domains.materials.k3_evidence import (
    MaterialsK3EvidenceBundle,
    MaterialsK3Protocol,
    MaterialsPreregistration,
    SignedMaterialsObservation,
    SignedMaterialsValidation,
    assemble_materials_evidence_bundle,
    build_materials_preregistration,
    derive_materials_belief_update,
    derive_materials_candidate_audits,
    derive_materials_scientific_decision,
    measure_materials_experiment,
    run_materials_experiment,
    validate_materials_observation,
    verify_materials_evidence_bundle,
)


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _model(path: Path, model_type):
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


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _inspect(args: argparse.Namespace) -> None:
    protocol = _model(args.protocol, MaterialsK3Protocol)
    audits = derive_materials_candidate_audits(protocol)
    _print(
        {
            "protocol_id": protocol.protocol_id,
            "protocol_sha256": protocol.protocol_sha256,
            "state": protocol.state,
            "dataset_ref": protocol.dataset_ref,
            "observation_access": "none",
            "candidate_audits": [item.model_dump(mode="json") for item in audits],
            "evidence_scope": protocol.evidence_policy.evidence_scope,
            "formal_prospective_evidence": False,
            "formal_external_replication": False,
        }
    )


def _preregister(args: argparse.Namespace) -> None:
    protocol = _model(args.protocol, MaterialsK3Protocol)
    preregistration = build_materials_preregistration(
        preregistration_id=args.preregistration_id,
        protocol=protocol,
    )
    destination = _atomic_new_json(args.output, preregistration)
    _print(
        {
            "output": str(destination),
            "preregistration_sha256": preregistration.preregistration_sha256,
            "protocol_sha256": preregistration.protocol_sha256,
            "selected_candidate_id": preregistration.selected_candidate_id,
            "preregistered_at": preregistration.preregistered_at.isoformat(),
        }
    )


def _measure(args: argparse.Namespace) -> None:
    preregistration = _model(args.preregistration, MaterialsPreregistration)
    observation = measure_materials_experiment(
        preregistration=preregistration,
        signing_key=_key(args.measurement_key),
    )
    destination = _atomic_new_json(args.output, observation)
    result = observation.observation.result
    _print(
        {
            "output": str(destination),
            "observation_envelope_sha256": observation.envelope_sha256,
            "result_sha256": result.result_sha256,
            "outcome_id": result.outcome_id.value,
            "metrics": result.metrics.model_dump(mode="json"),
            "dataset_sha256": result.dataset.logical_rows_sha256,
            "split": result.split.model_dump(mode="json"),
        }
    )


def _validate(args: argparse.Namespace) -> None:
    preregistration = _model(args.preregistration, MaterialsPreregistration)
    observation = _model(args.observation, SignedMaterialsObservation)
    validation = validate_materials_observation(
        preregistration=preregistration,
        signed_observation=observation,
        observation_key=_key(args.measurement_key),
        validation_key=_key(args.validation_key),
    )
    destination = _atomic_new_json(args.output, validation)
    _print(
        {
            "output": str(destination),
            "validation_envelope_sha256": validation.envelope_sha256,
            "recomputed_result_sha256": validation.receipt.recomputed_result_sha256,
            "physical_recomputation_performed": True,
            "exact_result_match": True,
        }
    )


def _update(args: argparse.Namespace) -> None:
    preregistration = _model(args.preregistration, MaterialsPreregistration)
    observation = _model(args.observation, SignedMaterialsObservation)
    validation = _model(args.validation, SignedMaterialsValidation)
    observation_key = _key(args.measurement_key)
    validation_key = _key(args.validation_key)
    update = derive_materials_belief_update(
        preregistration=preregistration,
        signed_observation=observation,
        signed_validation=validation,
        observation_key=observation_key,
        validation_key=validation_key,
    )
    decision = derive_materials_scientific_decision(update=update)
    bundle = assemble_materials_evidence_bundle(
        preregistration=preregistration,
        signed_observation=observation,
        signed_validation=validation,
        update=update,
        decision=decision,
    )
    verify_materials_evidence_bundle(
        bundle=bundle,
        observation_key=observation_key,
        validation_key=validation_key,
    )
    destination = _atomic_new_json(args.output, bundle)
    nominal = next(item for item in update.scenario_posteriors if item.scenario_id == "nominal")
    _print(
        {
            "output": str(destination),
            "bundle_sha256": bundle.bundle_sha256,
            "disposition": decision.disposition.value,
            "observed_outcome_id": update.observed_outcome_id.value,
            "nominal_posterior": {
                item.hypothesis_id: item.posterior_probability for item in nominal.probabilities
            },
            "winner_stable_across_sensitivity": update.winner_stable_across_sensitivity,
            "minimum_effective_count_contraction": (update.minimum_effective_count_contraction),
            "hypothesis_space_contracted": update.hypothesis_space_contracted,
            "formal_prospective_evidence": decision.formal_prospective_evidence,
            "formal_external_replication": decision.formal_external_replication,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    bundle = _model(args.bundle, MaterialsK3EvidenceBundle)
    observation_key = _key(args.measurement_key)
    validation_key = _key(args.validation_key)
    verify_materials_evidence_bundle(
        bundle=bundle,
        observation_key=observation_key,
        validation_key=validation_key,
    )
    physically_recomputed = False
    if args.recompute:
        result = run_materials_experiment(bundle.preregistration)
        if result != bundle.signed_observation.observation.result:
            raise ValueError("audit recomputation differs from signed observation")
        physically_recomputed = True
    _print(
        {
            "bundle_sha256": bundle.bundle_sha256,
            "signatures_valid": True,
            "derived_update_and_decision_valid": True,
            "physical_recomputation_performed": physically_recomputed,
            "disposition": bundle.decision.disposition.value,
        }
    )


def _add_preregistration(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preregistration", type=Path, required=True)


def _add_keys(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--measurement-key", type=Path, required=True)
    parser.add_argument("--validation-key", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real signed K3 materials evidence chain")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--protocol", type=Path, required=True)
    inspect.set_defaults(handler=_inspect)

    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("--protocol", type=Path, required=True)
    preregister.add_argument("--preregistration-id", required=True)
    preregister.add_argument("--output", type=Path, required=True)
    preregister.set_defaults(handler=_preregister)

    measure = subparsers.add_parser("measure")
    _add_preregistration(measure)
    measure.add_argument("--measurement-key", type=Path, required=True)
    measure.add_argument("--output", type=Path, required=True)
    measure.set_defaults(handler=_measure)

    validate = subparsers.add_parser("validate")
    _add_preregistration(validate)
    validate.add_argument("--observation", type=Path, required=True)
    _add_keys(validate)
    validate.add_argument("--output", type=Path, required=True)
    validate.set_defaults(handler=_validate)

    update = subparsers.add_parser("update")
    _add_preregistration(update)
    update.add_argument("--observation", type=Path, required=True)
    update.add_argument("--validation", type=Path, required=True)
    _add_keys(update)
    update.add_argument("--output", type=Path, required=True)
    update.set_defaults(handler=_update)

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
