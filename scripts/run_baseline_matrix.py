"""Materialize, execute, or aggregate a frozen four-arm Frontier Gate baseline matrix.

Execution dependencies stay evaluator-owned.  ``run`` loads an explicit operator factory with the
signature ``factory(*, matrix, suite, tasks, config)``; it must return exactly one formal
``IndependentEvaluationRunner`` for each ``BaselineArmId``.  Secrets and hidden assets therefore
never enter the preregistration JSON or the research process.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aletheia.evals.baselines import (
    BaselineArmId,
    BaselineMatrixPlan,
    BaselineMatrixResult,
    BaselineMatrixRunner,
    baseline_execution_schedule,
    baseline_schedule_sha256,
    build_baseline_run_plans,
    validate_matrix_suite,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.schemas import EvaluationSuite, EvaluationTask
from aletheia.evals.statistics import aggregate_baseline_matrix


def _read_json(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    return json.loads(resolved.read_text(encoding="utf-8"))


def _load_matrix(path: Path) -> BaselineMatrixPlan:
    raw = _read_json(path)
    if isinstance(raw, dict) and "matrix" in raw:
        raw = raw["matrix"]
    return BaselineMatrixPlan.model_validate(raw)


def _load_suite_bundle(path: Path) -> tuple[EvaluationSuite, dict[str, EvaluationTask]]:
    raw = _read_json(path)
    suite_raw = raw.get("suite", raw) if isinstance(raw, dict) else raw
    suite = EvaluationSuite.model_validate(suite_raw)
    task_rows = raw.get("tasks", []) if isinstance(raw, dict) else []
    tasks = [EvaluationTask.model_validate(row) for row in task_rows]
    task_map = {task.manifest_sha256: task for task in tasks}
    if tasks and len(task_map) != len(tasks):
        raise ValueError("suite bundle contains duplicate task manifests")
    return suite, task_map


def _atomic_new_json(path: Path, payload: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace frozen output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
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


def _factory(reference: str) -> Callable[..., Mapping[Any, IndependentEvaluationRunner]]:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("runner factories must use MODULE:CALLABLE")
    value = getattr(importlib.import_module(module_name), attribute_name)
    if not callable(value):
        raise TypeError(f"runner factory {reference!r} is not callable")
    return value


def _runner_map(raw: Mapping[Any, Any]) -> dict[BaselineArmId, IndependentEvaluationRunner]:
    runners: dict[BaselineArmId, IndependentEvaluationRunner] = {}
    for key, value in raw.items():
        arm_id = key if isinstance(key, BaselineArmId) else BaselineArmId(str(key))
        if not isinstance(value, IndependentEvaluationRunner):
            raise TypeError(
                f"factory value for {arm_id.value} is not an IndependentEvaluationRunner"
            )
        runners[arm_id] = value
    return runners


def _receipt_keys(specifications: list[str]) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for specification in specifications:
        key_id, separator, environment_name = specification.partition("=")
        if not separator or not key_id or not environment_name:
            raise ValueError("receipt key specs must use KEY_ID=BASE64_ENVIRONMENT_VARIABLE")
        if key_id in keys:
            raise ValueError(f"duplicate receipt key id {key_id!r}")
        encoded = os.environ.get(environment_name)
        if encoded is None:
            raise ValueError(f"receipt key environment variable {environment_name!r} is unset")
        try:
            key = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError(
                f"receipt key environment variable {environment_name!r} is not base64"
            ) from exc
        if len(key) < 32:
            raise ValueError("receipt verification keys must contain at least 32 bytes")
        keys[key_id] = key
    return keys


def _materialize(args: argparse.Namespace) -> None:
    matrix = _load_matrix(args.matrix)
    suite, _tasks = _load_suite_bundle(args.suite_bundle)
    validate_matrix_suite(matrix, suite)
    schedule = baseline_execution_schedule(matrix)
    payload = {
        "schema_name": "aletheia.baseline_matrix_materialization",
        "schema_version": 1,
        "matrix_manifest_sha256": matrix.manifest_sha256,
        "suite_manifest_sha256": suite.manifest_sha256,
        "schedule_sha256": baseline_schedule_sha256(matrix),
        "run_plans": [
            item.model_dump(mode="json") for item in build_baseline_run_plans(matrix, suite)
        ],
        "schedule": [cell.model_dump(mode="json") for cell in schedule],
    }
    _atomic_new_json(args.output, payload)
    print(args.output.expanduser().resolve(strict=True))


def _run(args: argparse.Namespace) -> None:
    output = args.output.expanduser().resolve(strict=False)
    if output.exists():
        raise FileExistsError(f"refusing to spend evaluation access for existing output: {output}")
    matrix = _load_matrix(args.matrix)
    suite, tasks = _load_suite_bundle(args.suite_bundle)
    if not tasks:
        raise ValueError("run requires a suite bundle containing evaluator-owned task manifests")
    config = _read_json(args.factory_config) if args.factory_config else {}
    factory = _factory(args.runner_factory)
    runners = _runner_map(factory(matrix=matrix, suite=suite, tasks=tasks, config=config))
    result = BaselineMatrixRunner(
        matrix=matrix,
        suite=suite,
        tasks=tasks,
        runners=runners,
    ).run()
    _atomic_new_json(output, result)
    print(output.resolve(strict=True))


def _aggregate(args: argparse.Namespace) -> None:
    matrix = _load_matrix(args.matrix)
    suite, _tasks = _load_suite_bundle(args.suite_bundle)
    result = BaselineMatrixResult.model_validate(_read_json(args.result))
    ledger = EvaluationLedger(args.ledger)
    report = aggregate_baseline_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=ledger,
        receipt_keys=_receipt_keys(args.receipt_key_env),
    )
    _atomic_new_json(args.output, report)
    print(args.output.expanduser().resolve(strict=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an evaluator-owned, preregistered direct/generic/no-K2/full-K2 matrix."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    materialize = subparsers.add_parser(
        "materialize", help="validate a preregistration and emit its four run plans and schedule"
    )
    materialize.add_argument("--matrix", type=Path, required=True)
    materialize.add_argument("--suite-bundle", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    materialize.set_defaults(handler=_materialize)

    run = subparsers.add_parser("run", help="execute every paired cell through an operator factory")
    run.add_argument("--matrix", type=Path, required=True)
    run.add_argument("--suite-bundle", type=Path, required=True)
    run.add_argument("--runner-factory", required=True, metavar="MODULE:CALLABLE")
    run.add_argument("--factory-config", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)

    aggregate = subparsers.add_parser(
        "aggregate", help="reconcile ledger/receipts and emit paired statistics"
    )
    aggregate.add_argument("--matrix", type=Path, required=True)
    aggregate.add_argument("--suite-bundle", type=Path, required=True)
    aggregate.add_argument("--result", type=Path, required=True)
    aggregate.add_argument("--ledger", type=Path, required=True)
    aggregate.add_argument(
        "--receipt-key-env",
        action="append",
        default=[],
        metavar="KEY_ID=BASE64_ENV",
        help="trusted scorer key ID and environment variable containing its base64 bytes",
    )
    aggregate.add_argument("--output", type=Path, required=True)
    aggregate.set_defaults(handler=_aggregate)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
