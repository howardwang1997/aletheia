"""Run and audit the frozen F9 K3-versus-K2 hidden-world ablation.

The command never invents a model runner or receipt key.  Execution loads an evaluator-owned
factory, while aggregation and the final decision re-open the append-only ledger and verify every
signed scorer receipt.  A convenience protocol check supports the roadmap command shape::

    python scripts/real_k3_hidden_world_e2e.py \
      --suite configs/evals/k3_hidden_world_v1.yaml --repeats 5 --frozen
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from aletheia.evals.frontier_gate import PrivateCustodyEvidence
from aletheia.evals.k3_hidden_world import (
    K3HiddenWorldAcceptanceConfig,
    K3HiddenWorldAnalysisPolicy,
    K3HiddenWorldArm,
    K3HiddenWorldArmId,
    K3HiddenWorldAggregateReport,
    K3HiddenWorldMatrixPlan,
    K3HiddenWorldMatrixResult,
    K3HiddenWorldMatrixRunner,
    K3HiddenWorldThresholdPolicy,
    aggregate_k3_hidden_world_matrix,
    build_k3_hidden_world_run_plans,
    freeze_k3_hidden_world_acceptance,
    generate_k3_hidden_world_scientific_exit_decision,
    k3_hidden_world_execution_schedule,
    k3_hidden_world_schedule_sha256,
    validate_k3_hidden_world_matrix_suite,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.runner import IndependentEvaluationRunner
from aletheia.evals.schemas import EvaluationSuite, EvaluationTask, FrozenModel, content_sha256
from aletheia.migration.dynamic_loader import resolve_guarded_dynamic_attribute


class FrozenK3ProtocolFile(FrozenModel):
    """Human-maintained protocol freeze; live evidence has separate content-addressed models."""

    schema_name: Literal["aletheia.k3_hidden_world_protocol"] = "aletheia.k3_hidden_world_protocol"
    schema_version: Literal[1] = 1
    protocol_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
    state: Literal["protocol_frozen"] = "protocol_frozen"
    validation_repeats: Literal[3] = 3
    test_repeats: Literal[5] = 5
    minimum_hidden_law_tasks: int = Field(default=4, ge=4)
    required_reproduction_runs: int = Field(default=2, ge=2, le=5)
    validation_suite_bundle: str
    test_suite_bundle: str | None = None
    arms: tuple[K3HiddenWorldArm, ...] = Field(min_length=3, max_length=3)
    analysis: K3HiddenWorldAnalysisPolicy
    threshold_policy: K3HiddenWorldThresholdPolicy
    manifest_descriptors: dict[str, dict[str, Any]] = Field(min_length=1)
    unresolved_scientific_exit_inputs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _manifest_hashes_have_embedded_descriptors(self) -> "FrozenK3ProtocolFile":
        identities = {
            digest
            for arm in self.arms
            for digest in (
                arm.system_manifest_sha256,
                arm.base_model_manifest_sha256,
                arm.base_task_prompt_sha256,
                arm.treatment_prompt_sha256,
                arm.tool_policy_sha256,
                arm.budget_policy_sha256,
                arm.wall_time_policy_sha256,
                arm.sampling_policy_sha256,
            )
        }
        if set(self.manifest_descriptors) != identities:
            raise ValueError("protocol descriptors must exactly cover every arm manifest identity")
        mismatches = [
            digest
            for digest, descriptor in self.manifest_descriptors.items()
            if content_sha256(descriptor) != digest
        ]
        if mismatches:
            raise ValueError(f"protocol manifest descriptor hashes are invalid: {mismatches}")
        return self

    @property
    def protocol_sha256(self) -> str:
        return content_sha256(self)


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


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


def _model(path: Path, model_type, *, key: str | None = None):
    raw = _read(path)
    if key is not None and isinstance(raw, dict) and key in raw:
        raw = raw[key]
    return model_type.model_validate(raw)


def _suite_bundle(path: Path) -> tuple[EvaluationSuite, dict[str, EvaluationTask]]:
    raw = _read(path)
    suite_raw = raw.get("suite", raw) if isinstance(raw, dict) else raw
    suite = EvaluationSuite.model_validate(suite_raw)
    task_rows = raw.get("tasks", []) if isinstance(raw, dict) else []
    tasks = [EvaluationTask.model_validate(row) for row in task_rows]
    task_map = {task.manifest_sha256: task for task in tasks}
    if tasks and len(task_map) != len(tasks):
        raise ValueError("K3 suite bundle contains duplicate task manifests")
    return suite, task_map


def _factory(reference: str) -> Callable[..., Mapping[Any, IndependentEvaluationRunner]]:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("K3 runner factories must use MODULE:CALLABLE")
    value = resolve_guarded_dynamic_attribute(module_name, attribute_name)
    if not callable(value):
        raise TypeError(f"K3 runner factory {reference!r} is not callable")
    return value


def _runner_map(raw: Mapping[Any, Any]) -> dict[K3HiddenWorldArmId, IndependentEvaluationRunner]:
    runners: dict[K3HiddenWorldArmId, IndependentEvaluationRunner] = {}
    for key, value in raw.items():
        arm_id = key if isinstance(key, K3HiddenWorldArmId) else K3HiddenWorldArmId(str(key))
        if not isinstance(value, IndependentEvaluationRunner):
            raise TypeError(f"runner for {arm_id.value} is not an IndependentEvaluationRunner")
        runners[arm_id] = value
    return runners


def _receipt_keys(specifications: list[str]) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for specification in specifications:
        key_id, separator, environment_name = specification.partition("=")
        if not separator or not key_id or not environment_name:
            raise ValueError("receipt keys must use KEY_ID=BASE64_ENVIRONMENT_VARIABLE")
        if key_id in keys:
            raise ValueError(f"duplicate scorer key ID {key_id!r}")
        encoded = os.environ.get(environment_name)
        if encoded is None:
            raise ValueError(f"receipt key environment variable {environment_name!r} is unset")
        key = base64.b64decode(encoded, validate=True)
        if len(key) < 32:
            raise ValueError("K3 scorer verification keys must contain at least 32 bytes")
        keys[key_id] = key
    return keys


def _check_protocol(args: argparse.Namespace) -> None:
    protocol_path = args.suite.expanduser().resolve(strict=True)
    protocol = FrozenK3ProtocolFile.model_validate(_read(protocol_path))
    if args.repeats != protocol.test_repeats:
        raise ValueError(
            f"requested repeats={args.repeats} differs from frozen repeats={protocol.test_repeats}"
        )
    if not args.frozen:
        raise ValueError("protocol inspection requires --frozen; mutable evaluation is forbidden")
    base = protocol_path.parent
    validation_path = (base / protocol.validation_suite_bundle).resolve(strict=False)
    test_path = (
        (base / protocol.test_suite_bundle).resolve(strict=False)
        if protocol.test_suite_bundle is not None
        else None
    )
    unresolved = list(protocol.unresolved_scientific_exit_inputs)
    validation_requirement = "operator_materialized_scorer_bound_validation_suite"
    if validation_path.is_file() and validation_requirement in unresolved:
        unresolved.remove(validation_requirement)
    readiness = "ready" if not unresolved else "blocked"
    print(
        json.dumps(
            {
                "protocol_id": protocol.protocol_id,
                "protocol_sha256": protocol.protocol_sha256,
                "state": protocol.state,
                "scientific_exit_readiness": readiness,
                "validation_suite_exists": validation_path.is_file(),
                "test_suite_exists": bool(test_path and test_path.is_file()),
                "unresolved_scientific_exit_inputs": unresolved,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _materialize(args: argparse.Namespace) -> None:
    matrix = _model(args.matrix, K3HiddenWorldMatrixPlan, key="matrix")
    suite, _tasks = _suite_bundle(args.suite_bundle)
    validate_k3_hidden_world_matrix_suite(matrix, suite)
    payload = {
        "schema_name": "aletheia.k3_hidden_world_materialization",
        "schema_version": 1,
        "matrix_manifest_sha256": matrix.manifest_sha256,
        "suite_manifest_sha256": suite.manifest_sha256,
        "schedule_sha256": k3_hidden_world_schedule_sha256(matrix),
        "run_plans": [
            item.model_dump(mode="json") for item in build_k3_hidden_world_run_plans(matrix, suite)
        ],
        "schedule": [
            cell.model_dump(mode="json") for cell in k3_hidden_world_execution_schedule(matrix)
        ],
    }
    _atomic_new_json(args.output, payload)
    print(args.output.expanduser().resolve(strict=True))


def _run(args: argparse.Namespace) -> None:
    matrix = _model(args.matrix, K3HiddenWorldMatrixPlan, key="matrix")
    suite, tasks = _suite_bundle(args.suite_bundle)
    if not tasks:
        raise ValueError("K3 execution requires evaluator-owned task manifests")
    config = _read(args.factory_config) if args.factory_config else {}
    factory = _factory(args.runner_factory)
    runners = _runner_map(factory(matrix=matrix, suite=suite, tasks=tasks, config=config))
    result = K3HiddenWorldMatrixRunner(
        matrix=matrix, suite=suite, tasks=tasks, runners=runners
    ).run()
    _atomic_new_json(args.output, result)
    print(args.output.expanduser().resolve(strict=True))


def _aggregate(args: argparse.Namespace) -> None:
    matrix = _model(args.matrix, K3HiddenWorldMatrixPlan, key="matrix")
    suite, _tasks = _suite_bundle(args.suite_bundle)
    result = _model(args.result, K3HiddenWorldMatrixResult, key="result")
    report = aggregate_k3_hidden_world_matrix(
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=EvaluationLedger(args.ledger),
        receipt_keys=_receipt_keys(args.receipt_key_env),
    )
    _atomic_new_json(args.output, report)
    print(args.output.expanduser().resolve(strict=True))


def _freeze_acceptance(args: argparse.Namespace) -> None:
    config = freeze_k3_hidden_world_acceptance(
        config_id=args.config_id,
        threshold_policy=_model(args.threshold_policy, K3HiddenWorldThresholdPolicy),
        validation_matrix=_model(args.validation_matrix, K3HiddenWorldMatrixPlan, key="matrix"),
        validation_result=_model(args.validation_result, K3HiddenWorldMatrixResult, key="result"),
        validation_report=_model(
            args.validation_report, K3HiddenWorldAggregateReport, key="report"
        ),
        test_matrix=_model(args.test_matrix, K3HiddenWorldMatrixPlan, key="matrix"),
        require_private_prospective_test=not args.allow_public_diagnostic,
    )
    _atomic_new_json(args.output, config)
    print(args.output.expanduser().resolve(strict=True))


def _decide(args: argparse.Namespace) -> None:
    matrix = _model(args.matrix, K3HiddenWorldMatrixPlan, key="matrix")
    suite, _tasks = _suite_bundle(args.suite_bundle)
    result = _model(args.result, K3HiddenWorldMatrixResult, key="result")
    custody = _model(args.private_custody, PrivateCustodyEvidence) if args.private_custody else None
    report, decision = generate_k3_hidden_world_scientific_exit_decision(
        config=_model(args.acceptance_config, K3HiddenWorldAcceptanceConfig),
        threshold_policy=_model(args.threshold_policy, K3HiddenWorldThresholdPolicy),
        matrix=matrix,
        suite=suite,
        result=result,
        ledger=EvaluationLedger(args.ledger),
        receipt_keys=_receipt_keys(args.receipt_key_env),
        private_custody=custody,
    )
    _atomic_new_json(
        args.output,
        {
            "schema_name": "aletheia.k3_hidden_world_scientific_exit_bundle",
            "schema_version": 1,
            "report": report.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        },
    )
    print(args.output.expanduser().resolve(strict=True))


def _common_matrix(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--suite-bundle", type=Path, required=True)


def _common_receipts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--receipt-key-env",
        action="append",
        default=[],
        metavar="KEY_ID=BASE64_ENV",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Frozen K3-versus-K2 hidden-world evaluation")
    parser.add_argument("--suite", type=Path, help="frozen protocol YAML (roadmap command mode)")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--frozen", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    materialize = subparsers.add_parser("materialize")
    _common_matrix(materialize)
    materialize.add_argument("--output", type=Path, required=True)
    materialize.set_defaults(handler=_materialize)

    run = subparsers.add_parser("run")
    _common_matrix(run)
    run.add_argument("--runner-factory", required=True, metavar="MODULE:CALLABLE")
    run.add_argument("--factory-config", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)

    aggregate = subparsers.add_parser("aggregate")
    _common_matrix(aggregate)
    _common_receipts(aggregate)
    aggregate.add_argument("--output", type=Path, required=True)
    aggregate.set_defaults(handler=_aggregate)

    freeze = subparsers.add_parser("freeze-acceptance")
    freeze.add_argument("--config-id", required=True)
    freeze.add_argument("--threshold-policy", type=Path, required=True)
    freeze.add_argument("--validation-matrix", type=Path, required=True)
    freeze.add_argument("--validation-result", type=Path, required=True)
    freeze.add_argument("--validation-report", type=Path, required=True)
    freeze.add_argument("--test-matrix", type=Path, required=True)
    freeze.add_argument("--allow-public-diagnostic", action="store_true")
    freeze.add_argument("--output", type=Path, required=True)
    freeze.set_defaults(handler=_freeze_acceptance)

    decide = subparsers.add_parser("decide")
    _common_matrix(decide)
    _common_receipts(decide)
    decide.add_argument("--acceptance-config", type=Path, required=True)
    decide.add_argument("--threshold-policy", type=Path, required=True)
    decide.add_argument("--private-custody", type=Path)
    decide.add_argument("--output", type=Path, required=True)
    decide.set_defaults(handler=_decide)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command is None:
        if args.suite is None:
            raise ValueError("supply a subcommand or --suite PROTOCOL.yaml")
        _check_protocol(args)
        return
    args.handler(args)


if __name__ == "__main__":
    main()
