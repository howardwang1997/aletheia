#!/usr/bin/env python3
"""Operate the precommitted shadow-portfolio work order for the phonon endurance Quest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aletheia.domains.materials.phonon_commissioning import (
    PhononQuestCommissioningManifest,
)
from aletheia.domains.materials.phonon_endurance_portfolio import (
    PhononBlindPortfolioSelection,
    PhononEndurancePortfolioConflict,
    PhononEndurancePortfolioWorkOrder,
    PhononPortfolioStageReceipt,
    commit_phonon_blind_portfolio_plan,
    evaluate_phonon_endurance_portfolio,
    preflight_phonon_portfolio_start,
    prepare_phonon_endurance_portfolio_work_order,
    stage_phonon_endurance_portfolio,
    verify_phonon_endurance_portfolio_work_order,
)
from aletheia.domains.materials.phonon_reproduction import (
    PhononIndependentReplayProtocol,
)
from aletheia.programs.endurance_controller import EnduranceControllerManifest


def _read(path: Path) -> Any:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def _render(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _print(value: Any) -> None:
    print(_render(value).decode("utf-8"), end="")


def _write_new(path: Path, value: Any) -> bool:
    payload = _render(value)
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == payload:
            return False
        raise PhononEndurancePortfolioConflict(
            f"refusing to replace portfolio artifact: {destination}"
        )
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("portfolio artifact write made no progress")
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


def _work_order(path: Path) -> PhononEndurancePortfolioWorkOrder:
    return PhononEndurancePortfolioWorkOrder.model_validate(_read(path))


def _stage(path: Path) -> PhononPortfolioStageReceipt:
    return PhononPortfolioStageReceipt.model_validate(_read(path))


def _prepare(args: argparse.Namespace) -> int:
    output = Path(args.output)
    existing = _work_order(output) if output.exists() else None
    controller_path = Path(args.controller)
    protocol_path = Path(args.protocol)
    commissioning_path = Path(args.commissioning)
    work_order = prepare_phonon_endurance_portfolio_work_order(
        controller=EnduranceControllerManifest.model_validate(_read(controller_path)),
        controller_path=controller_path,
        protocol=PhononIndependentReplayProtocol.model_validate(_read(protocol_path)),
        protocol_path=protocol_path,
        commissioning=PhononQuestCommissioningManifest.model_validate(_read(commissioning_path)),
        commissioning_path=commissioning_path,
        prepared_at=existing.prepared_at if existing is not None else datetime.now(timezone.utc),
    )
    if existing is not None and existing != work_order:
        raise PhononEndurancePortfolioConflict(
            "existing portfolio work order differs from requested preparation"
        )
    created = _write_new(output, work_order)
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "gate_id": work_order.gate_id,
            "candidate_count": len(work_order.actions),
            "candidate_ids": [item.candidate_id for item in work_order.actions],
            "human_plan_required": True,
            "planner_output_materialized": False,
            "actions_enqueued": False,
            "output": str(output.resolve()),
            "created": created,
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    work_order = _work_order(Path(args.work_order))
    verify_phonon_endurance_portfolio_work_order(work_order)
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "gate_id": work_order.gate_id,
            "verified": True,
            "human_plan_required": True,
            "actions_enqueued": False,
        }
    )
    return 0


def _stage_command(args: argparse.Namespace) -> int:
    output = Path(args.output)
    receipt = stage_phonon_endurance_portfolio(_work_order(Path(args.work_order)))
    _write_new(output, receipt)
    _print(receipt)
    return 0


def _commit_plan(args: argparse.Namespace) -> int:
    receipt = commit_phonon_blind_portfolio_plan(
        _work_order(Path(args.work_order)),
        _stage(Path(args.stage)),
        PhononBlindPortfolioSelection.model_validate(_read(Path(args.selection))),
        human_principal=args.human_principal,
    )
    created = _write_new(Path(args.output), receipt)
    _print({"receipt": receipt.model_dump(mode="json"), "output_created": created})
    return 0


def _preflight_start(args: argparse.Namespace) -> int:
    report = preflight_phonon_portfolio_start(
        _work_order(Path(args.work_order)),
        _stage(Path(args.stage)),
    )
    _print(report)
    return 0 if report.ready_for_explicit_gate_start else 2


def _evaluate(args: argparse.Namespace) -> int:
    receipt = evaluate_phonon_endurance_portfolio(
        _work_order(Path(args.work_order)),
        _stage(Path(args.stage)),
    )
    created = _write_new(Path(args.output), receipt)
    _print({"receipt": receipt.model_dump(mode="json"), "output_created": created})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze the zero-output portfolio work order")
    prepare.add_argument("--controller", required=True)
    prepare.add_argument("--protocol", required=True)
    prepare.add_argument("--commissioning", required=True)
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=_prepare)
    verify = commands.add_parser("verify", help="verify committed code, files, and initial graph")
    verify.add_argument("work_order")
    verify.set_defaults(handler=_verify)
    stage = commands.add_parser(
        "stage",
        help="register memory and slate before gate start without materializing planner output",
    )
    stage.add_argument("work_order")
    stage.add_argument("--output", required=True)
    stage.set_defaults(handler=_stage_command)
    plan = commands.add_parser(
        "commit-plan",
        help="commit an explicit human-blind baseline before planner evaluation",
    )
    plan.add_argument("work_order")
    plan.add_argument("stage")
    plan.add_argument("selection")
    plan.add_argument("--human-principal", required=True)
    plan.add_argument("--output", required=True)
    plan.set_defaults(handler=_commit_plan)
    preflight = commands.add_parser(
        "preflight-start",
        help="require a human plan and no pre-start planner output",
    )
    preflight.add_argument("work_order")
    preflight.add_argument("stage")
    preflight.set_defaults(handler=_preflight_start)
    evaluate = commands.add_parser(
        "evaluate",
        help="materialize one shadow epoch after gate start and before graph transitions",
    )
    evaluate.add_argument("work_order")
    evaluate.add_argument("stage")
    evaluate.add_argument("--output", required=True)
    evaluate.set_defaults(handler=_evaluate)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
