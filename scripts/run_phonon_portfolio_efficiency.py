#!/usr/bin/env python3
"""Freeze and derive the phonon shadow-portfolio efficiency receipt."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aletheia.domains.materials.phonon_endurance_portfolio import (
    PhononEndurancePortfolioWorkOrder,
    PhononPortfolioStageReceipt,
)
from aletheia.domains.materials.phonon_portfolio_efficiency import (
    PhononPortfolioEfficiencyAssessment,
    PhononPortfolioEfficiencyConflict,
    PhononPortfolioEfficiencyWorkOrder,
    assess_phonon_portfolio_efficiency,
    preflight_phonon_portfolio_efficiency_start,
    prepare_phonon_portfolio_efficiency_work_order,
    verify_phonon_portfolio_efficiency_assessment,
    verify_phonon_portfolio_efficiency_work_order,
)


def _read(path: Path) -> Any:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def _render(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _print(value: Any) -> None:
    print(_render(value).decode(), end="")


def _write_new(path: Path, value: Any) -> bool:
    payload = _render(value)
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == payload:
            return False
        raise PhononPortfolioEfficiencyConflict(
            f"refusing to replace efficiency artifact: {destination}"
        )
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("efficiency artifact write made no progress")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _work_order(path: Path) -> PhononPortfolioEfficiencyWorkOrder:
    return PhononPortfolioEfficiencyWorkOrder.model_validate(_read(path))


def _prepare(args: argparse.Namespace) -> int:
    output = Path(args.output)
    existing = _work_order(output) if output.exists() else None
    portfolio_path = Path(args.portfolio_work_order)
    stage_path = Path(args.stage)
    work_order = prepare_phonon_portfolio_efficiency_work_order(
        portfolio=PhononEndurancePortfolioWorkOrder.model_validate(_read(portfolio_path)),
        portfolio_path=portfolio_path,
        stage=PhononPortfolioStageReceipt.model_validate(_read(stage_path)),
        stage_path=stage_path,
        prepared_at=existing.prepared_at if existing is not None else datetime.now(timezone.utc),
        assessed_by=args.assessed_by,
    )
    if existing is not None and existing != work_order:
        raise PhononPortfolioEfficiencyConflict(
            "existing efficiency work order differs from requested preparation"
        )
    created = _write_new(output, work_order)
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "gate_id": work_order.gate_id,
            "human_plan_id": work_order.human_plan_id,
            "baseline_candidate_id": work_order.baseline_candidate_id,
            "baseline_value_units": work_order.baseline_value_units,
            "baseline_cost_microunits": work_order.baseline_cost_microunits,
            "minimum_improvement_ppm": work_order.minimum_improvement_ppm,
            "expected_not_realized_scientific_efficiency": True,
            "actions_enqueued": False,
            "output": str(output.resolve()),
            "created": created,
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    work_order = _work_order(Path(args.work_order))
    verify_phonon_portfolio_efficiency_work_order(
        work_order,
        require_no_epoch=args.require_no_epoch,
    )
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "verified": True,
            "no_epoch_required": args.require_no_epoch,
        }
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    report = preflight_phonon_portfolio_efficiency_start(
        _work_order(Path(args.work_order))
    )
    _print(report)
    return 0 if report.ready_for_explicit_gate_start else 2


def _assess(args: argparse.Namespace) -> int:
    assessment = assess_phonon_portfolio_efficiency(
        _work_order(Path(args.work_order))
    )
    assessment_created = _write_new(Path(args.assessment_output), assessment)
    receipt_created = _write_new(Path(args.receipt_output), assessment.receipt)
    _print(
        {
            "assessment": assessment.model_dump(mode="json"),
            "assessment_output_created": assessment_created,
            "receipt_output_created": receipt_created,
        }
    )
    return 0


def _verify_assessment(args: argparse.Namespace) -> int:
    work_order = _work_order(Path(args.work_order))
    assessment = PhononPortfolioEfficiencyAssessment.model_validate(
        _read(Path(args.assessment))
    )
    verify_phonon_portfolio_efficiency_assessment(work_order, assessment)
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "epoch_id": assessment.epoch_id,
            "efficiency_receipt_id": assessment.receipt.receipt_id,
            "improvement_ppm": assessment.receipt.improvement_ppm,
            "meets_gate_floor": assessment.meets_gate_floor,
            "verified": True,
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare",
        help="freeze one blind human baseline before planner output",
    )
    prepare.add_argument("--portfolio-work-order", required=True)
    prepare.add_argument("--stage", required=True)
    prepare.add_argument("--assessed-by", default="harness:phonon-portfolio-efficiency")
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=_prepare)
    verify = commands.add_parser("verify", help="verify the frozen efficiency work order")
    verify.add_argument("work_order")
    verify.add_argument("--require-no-epoch", action="store_true")
    verify.set_defaults(handler=_verify)
    preflight = commands.add_parser(
        "preflight-start",
        help="require the blind baseline and absence of planner output/live gate",
    )
    preflight.add_argument("work_order")
    preflight.set_defaults(handler=_preflight)
    assess = commands.add_parser(
        "assess",
        help="derive expected question-coverage efficiency from the in-window shadow epoch",
    )
    assess.add_argument("work_order")
    assess.add_argument("--assessment-output", required=True)
    assess.add_argument("--receipt-output", required=True)
    assess.set_defaults(handler=_assess)
    verify_assessment = commands.add_parser(
        "verify-assessment",
        help="replay an efficiency assessment from the durable epoch",
    )
    verify_assessment.add_argument("work_order")
    verify_assessment.add_argument("assessment")
    verify_assessment.set_defaults(handler=_verify_assessment)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
