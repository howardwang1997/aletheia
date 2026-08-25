"""Calibrate, freeze, and report the receipt-linked Frontier Gate acceptance decision.

Raw receipt keys are read only from base64-encoded environment variables.  The evidence-index
JSON stores environment-variable names and filesystem locations, never key bytes or private
plaintext.  Missing configured tracks produce a BLOCKED report; malformed or forged supplied
evidence is rejected instead of being converted into an operator-selected aggregate.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from aletheia.evals.baselines import BaselineMatrixPlan, BaselineMatrixResult
from aletheia.evals.frontier_gate import (
    FrontierGateAcceptanceConfig,
    FrontierGateTier,
    FrontierGateTrack,
    GateEvaluationInput,
    PrivateGateInput,
    SuiteAcceptanceConfig,
    SuiteCalibrationPlan,
    calibrate_suite_acceptance,
    freeze_frontier_gate_acceptance,
    generate_frontier_gate_report,
    render_frontier_gate_markdown,
    render_frontier_gate_svg,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.private_suite import PrivateCustodyLedger, PrivateSuiteManifest
from aletheia.evals.schemas import EvaluationSuite


def _read_json(path: Path) -> Any:
    return json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))


def _load_model(path: Path, model: Any, *, wrappers: Sequence[str] = ()) -> Any:
    raw = _read_json(path)
    if isinstance(raw, dict):
        for wrapper in wrappers:
            if wrapper in raw:
                raw = raw[wrapper]
                break
    return model.model_validate(raw)


def _load_suite(path: Path) -> EvaluationSuite:
    return _load_model(path, EvaluationSuite, wrappers=("suite",))


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return parsed


def _receipt_keys(specifications: Sequence[str]) -> dict[str, bytes]:
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


def _json_text(payload: object) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _new_destination(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.exists() or expanded.is_symlink():
        raise FileExistsError(f"refusing to replace frozen output: {expanded}")
    destination = expanded.resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to replace frozen output: {destination}")
    return destination


def _write_new_artifacts(artifacts: Mapping[Path, str]) -> None:
    resolved: dict[Path, str] = {}
    for path, text in artifacts.items():
        destination = _new_destination(path)
        if destination in resolved:
            raise ValueError(f"duplicate output path: {destination}")
        resolved[destination] = text
    temporary_paths: dict[Path, Path] = {}
    try:
        for destination, text in resolved.items():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            temporary_path = Path(temporary)
            temporary_paths[destination] = temporary_path
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(0o600)
        for destination, temporary in temporary_paths.items():
            os.replace(temporary, destination)
    finally:
        for temporary in temporary_paths.values():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _resolve_index_path(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"evidence index {label} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=True)


def _calibrate_suite(args: argparse.Namespace) -> None:
    output = _new_destination(args.output)
    config = calibrate_suite_acceptance(
        plan=_load_model(args.plan, SuiteCalibrationPlan, wrappers=("plan",)),
        validation_matrix=_load_model(
            args.validation_matrix, BaselineMatrixPlan, wrappers=("matrix",)
        ),
        validation_suite=_load_suite(args.validation_suite_bundle),
        validation_result=_load_model(
            args.validation_result, BaselineMatrixResult, wrappers=("result",)
        ),
        validation_ledger=EvaluationLedger(args.validation_ledger),
        receipt_keys=_receipt_keys(args.receipt_key_env),
        test_matrix=_load_model(args.test_matrix, BaselineMatrixPlan, wrappers=("matrix",)),
        test_suite=_load_suite(args.test_suite_bundle),
        suite_config_id=args.suite_config_id,
        calibrated_at=_datetime(args.calibrated_at),
    )
    _write_new_artifacts({output: _json_text(config)})
    print(output.resolve(strict=True))


def _freeze_config(args: argparse.Namespace) -> None:
    output = _new_destination(args.output)
    request = _read_json(args.freeze_request)
    if not isinstance(request, dict):
        raise ValueError("freeze request must be a JSON object")
    suites = tuple(
        _load_model(path, SuiteAcceptanceConfig, wrappers=("suite_config", "config"))
        for path in args.suite_config
    )
    config = freeze_frontier_gate_acceptance(
        program_config_id=str(request["program_config_id"]),
        version=str(request["version"]),
        tier=FrontierGateTier(request["tier"]),
        suites=suites,
        acceptance_owner_principal_sha256=str(request["acceptance_owner_principal_sha256"]),
        independent_auditor_principal_sha256=str(request["independent_auditor_principal_sha256"]),
        owner_approval_evidence_sha256=str(request["owner_approval_evidence_sha256"]),
        auditor_approval_evidence_sha256=str(request["auditor_approval_evidence_sha256"]),
        scientific_claim=str(request["scientific_claim"]),
        frozen_at=_datetime(request.get("frozen_at")),
    )
    _write_new_artifacts({output: _json_text(config)})
    print(output.resolve(strict=True))


def _evaluation_input(
    *,
    track: FrontierGateTrack,
    row: object,
    base: Path,
) -> GateEvaluationInput:
    if not isinstance(row, dict):
        raise ValueError(f"evidence index track {track.value!r} must be an object")
    receipt_specs = row.get("receipt_key_env", [])
    if not isinstance(receipt_specs, list) or any(
        not isinstance(item, str) for item in receipt_specs
    ):
        raise ValueError("track receipt_key_env must be a list of KEY_ID=ENV strings")
    return GateEvaluationInput(
        track=track,
        matrix=_load_model(
            _resolve_index_path(base, row.get("matrix"), f"{track.value}.matrix"),
            BaselineMatrixPlan,
            wrappers=("matrix",),
        ),
        suite=_load_suite(
            _resolve_index_path(base, row.get("suite_bundle"), f"{track.value}.suite_bundle")
        ),
        result=_load_model(
            _resolve_index_path(base, row.get("result"), f"{track.value}.result"),
            BaselineMatrixResult,
            wrappers=("result",),
        ),
        ledger=EvaluationLedger(
            _resolve_index_path(base, row.get("ledger"), f"{track.value}.ledger")
        ),
        receipt_keys=_receipt_keys(receipt_specs),
    )


def _report(args: argparse.Namespace) -> None:
    outputs = (args.output_json, args.output_markdown, args.output_svg)
    resolved_outputs = tuple(_new_destination(path) for path in outputs)
    if len(set(resolved_outputs)) != 3:
        raise ValueError("JSON, Markdown, and SVG outputs must use distinct paths")

    config = _load_model(
        args.config,
        FrontierGateAcceptanceConfig,
        wrappers=("acceptance_config", "config"),
    )
    index_path = args.evidence_index.expanduser().resolve(strict=True)
    raw = _read_json(index_path)
    if not isinstance(raw, dict):
        raise ValueError("evidence index must be a JSON object")
    tracks = raw.get("tracks", {})
    if not isinstance(tracks, dict):
        raise ValueError("evidence index tracks must be an object")
    evaluations: dict[FrontierGateTrack, GateEvaluationInput] = {}
    for name, row in tracks.items():
        track = FrontierGateTrack(name)
        evaluations[track] = _evaluation_input(
            track=track,
            row=row,
            base=index_path.parent,
        )

    private_input: PrivateGateInput | None = None
    private_row = raw.get("private")
    if private_row is not None:
        if not isinstance(private_row, dict):
            raise ValueError("evidence index private entry must be an object")
        private_input = PrivateGateInput(
            manifest=_load_model(
                _resolve_index_path(
                    index_path.parent,
                    private_row.get("manifest"),
                    "private.manifest",
                ),
                PrivateSuiteManifest,
                wrappers=("manifest",),
            ),
            ledger=PrivateCustodyLedger(
                _resolve_index_path(
                    index_path.parent,
                    private_row.get("ledger"),
                    "private.ledger",
                )
            ),
        )
    report = generate_frontier_gate_report(
        config=config,
        evaluations=evaluations,
        private_input=private_input,
        generated_at=_datetime(args.generated_at),
    )
    markdown = render_frontier_gate_markdown(
        report,
        plot_filename=args.output_svg.name,
    )
    svg = render_frontier_gate_svg(report)
    _write_new_artifacts(
        {
            args.output_json: _json_text(report),
            args.output_markdown: markdown,
            args.output_svg: svg,
        }
    )
    print(args.output_json.expanduser().resolve(strict=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate and issue fail-closed, receipt-linked Frontier Gate reports."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    calibrate = subparsers.add_parser(
        "calibrate-suite",
        help="derive one immutable test threshold set from audited validation evidence",
    )
    calibrate.add_argument("--plan", type=Path, required=True)
    calibrate.add_argument("--validation-matrix", type=Path, required=True)
    calibrate.add_argument("--validation-suite-bundle", type=Path, required=True)
    calibrate.add_argument("--validation-result", type=Path, required=True)
    calibrate.add_argument("--validation-ledger", type=Path, required=True)
    calibrate.add_argument("--test-matrix", type=Path, required=True)
    calibrate.add_argument("--test-suite-bundle", type=Path, required=True)
    calibrate.add_argument("--suite-config-id", required=True)
    calibrate.add_argument("--calibrated-at")
    calibrate.add_argument(
        "--receipt-key-env",
        action="append",
        default=[],
        metavar="KEY_ID=BASE64_ENV",
    )
    calibrate.add_argument("--output", type=Path, required=True)
    calibrate.set_defaults(handler=_calibrate_suite)

    freeze = subparsers.add_parser(
        "freeze-config",
        help="combine reviewed suite thresholds into the final pre-test program contract",
    )
    freeze.add_argument("--freeze-request", type=Path, required=True)
    freeze.add_argument("--suite-config", type=Path, action="append", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.set_defaults(handler=_freeze_config)

    report = subparsers.add_parser(
        "report",
        help="re-audit raw test evidence and emit immutable JSON, Markdown, and SVG artifacts",
    )
    report.add_argument("--config", type=Path, required=True)
    report.add_argument("--evidence-index", type=Path, required=True)
    report.add_argument("--generated-at")
    report.add_argument("--output-json", type=Path, required=True)
    report.add_argument("--output-markdown", type=Path, required=True)
    report.add_argument("--output-svg", type=Path, required=True)
    report.set_defaults(handler=_report)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
