"""Prepare, run, and physically replay the frozen F10-S4 structure experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from aletheia.domains.materials.capabilities.structure_discrimination import (
    StructureAwareExperimentPlan,
    StructureAwareExperimentProtocol,
    StructureAwareExperimentResult,
    build_structure_aware_experiment_plan,
    run_structure_aware_experiment,
    verify_structure_aware_experiment,
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


def _load_exact_dataset(
    path: Path, protocol: StructureAwareExperimentProtocol
) -> tuple[bytes, Any]:
    from matminer.utils.io import load_dataframe_from_json

    resolved = path.expanduser().resolve(strict=True)
    payload = resolved.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != protocol.dataset.expected_file_sha256:
        raise ValueError(
            "dataset bytes differ from frozen protocol: "
            f"expected {protocol.dataset.expected_file_sha256}, got {actual}"
        )
    dataframe = load_dataframe_from_json(str(resolved), pbar=False)
    return payload, dataframe


def _prepare(args: argparse.Namespace) -> None:
    protocol = _model(args.protocol, StructureAwareExperimentProtocol)
    payload, dataframe = _load_exact_dataset(args.dataset_file, protocol)
    plan = build_structure_aware_experiment_plan(
        plan_id=args.plan_id,
        protocol=protocol,
        dataframe=dataframe,
        dataset_file_bytes=payload,
        prepared_at=datetime.now(timezone.utc),
    )
    destination = _atomic_new_json(args.output, plan)
    _print(
        {
            "plan": str(destination),
            "plan_sha256": plan.plan_sha256,
            "protocol_sha256": plan.protocol.protocol_sha256,
            "dataset_receipt_sha256": plan.dataset_receipt.receipt_sha256,
            "quality_ledger_sha256": plan.quality_ledger.ledger_sha256,
            "split_receipt_sha256": plan.split_receipt.receipt_sha256,
            "composition_feature_receipt_sha256": plan.composition_features.receipt_sha256,
            "structure_feature_receipt_sha256": plan.structure_features.receipt_sha256,
            "rows": plan.dataset_receipt.row_count,
            "train_rows": plan.split_receipt.train_rows,
            "internal_validation_rows": plan.split_receipt.internal_validation_rows,
            "locked_holdout_rows": plan.split_receipt.locked_holdout_rows,
            "model_fit_count_at_freeze": plan.model_fit_count_at_freeze,
            "state": plan.state,
        }
    )


def _run(args: argparse.Namespace) -> None:
    plan = _model(args.plan, StructureAwareExperimentPlan)
    payload, dataframe = _load_exact_dataset(args.dataset_file, plan.protocol)
    result = run_structure_aware_experiment(
        plan=plan,
        dataframe=dataframe,
        dataset_file_bytes=payload,
        completed_at=datetime.now(timezone.utc),
    )
    destination = _atomic_new_json(args.output, result)
    _print(
        {
            "result": str(destination),
            "result_sha256": result.result_sha256,
            "plan_sha256": result.plan_sha256,
            "disposition": result.disposition.value,
            "matched_capacity": result.matched_capacity.model_dump(mode="json"),
            "arm_evaluations": [item.model_dump(mode="json") for item in result.arm_evaluations],
            "signal_evaluations": [
                item.model_dump(mode="json") for item in result.signal_evaluations
            ],
            "all_preregistered_arms_retained": result.all_preregistered_arms_retained,
            "holdout_is_same_dataset_not_external_replication": (
                result.holdout_is_same_dataset_not_external_replication
            ),
            "causal_or_mechanism_claim_forbidden": result.causal_or_mechanism_claim_forbidden,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    plan = _model(args.plan, StructureAwareExperimentPlan)
    result = _model(args.result, StructureAwareExperimentResult)
    payload, dataframe = _load_exact_dataset(args.dataset_file, plan.protocol)
    verify_structure_aware_experiment(
        plan=plan,
        result=result,
        dataframe=dataframe,
        dataset_file_bytes=payload,
    )
    _print(
        {
            "plan_sha256": plan.plan_sha256,
            "result_sha256": result.result_sha256,
            "dataset_bytes_rehashed": True,
            "structures_and_features_recomputed": True,
            "split_recomputed": True,
            "all_models_deterministically_replayed": True,
            "bootstrap_recomputed": True,
            "result_exactly_replayed": True,
            "disposition": result.disposition.value,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="build an immutable feature/split plan before any model fit"
    )
    prepare.add_argument("--protocol", type=Path, required=True)
    prepare.add_argument("--dataset-file", type=Path, required=True)
    prepare.add_argument("--plan-id", default="matbench-phonons-structure-aware-plan-v1")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    run = subparsers.add_parser("run", help="fit every frozen arm once and retain every outcome")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--dataset-file", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)

    verify = subparsers.add_parser(
        "verify", help="rehash the source and physically replay an immutable result"
    )
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--result", type=Path, required=True)
    verify.add_argument("--dataset-file", type=Path, required=True)
    verify.set_defaults(handler=_verify)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
