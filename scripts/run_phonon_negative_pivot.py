#!/usr/bin/env python3
"""Prepare and execute the conditional negative-result pivot for the phonon endurance Quest."""

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
from aletheia.domains.materials.phonon_negative_pivot import (
    PhononNegativePivotConflict,
    PhononNegativePivotWorkOrder,
    execute_phonon_negative_result_pivot,
    preflight_phonon_negative_pivot_start,
    prepare_phonon_negative_pivot_work_order,
    verify_phonon_negative_pivot_work_order,
)
from aletheia.domains.materials.phonon_reproduction import (
    PhononIndependentReplayProtocol,
    PhononReplayCommitReceipt,
)
from aletheia.programs.endurance_controller import EnduranceControllerManifest


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
        raise PhononNegativePivotConflict(f"refusing to replace pivot artifact: {destination}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("pivot artifact write made no progress")
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


def _work_order(path: Path) -> PhononNegativePivotWorkOrder:
    return PhononNegativePivotWorkOrder.model_validate(_read(path))


def _prepare(args: argparse.Namespace) -> int:
    output = Path(args.output)
    existing = _work_order(output) if output.exists() else None
    controller_path = Path(args.controller)
    protocol_path = Path(args.protocol)
    commissioning_path = Path(args.commissioning)
    work_order = prepare_phonon_negative_pivot_work_order(
        controller=EnduranceControllerManifest.model_validate(_read(controller_path)),
        controller_path=controller_path,
        protocol=PhononIndependentReplayProtocol.model_validate(_read(protocol_path)),
        protocol_path=protocol_path,
        commissioning=PhononQuestCommissioningManifest.model_validate(_read(commissioning_path)),
        commissioning_path=commissioning_path,
        prepared_at=existing.prepared_at if existing is not None else datetime.now(timezone.utc),
        transition_principal=args.transition_principal,
        assessed_by=args.assessed_by,
        producer=args.producer,
    )
    if existing is not None and existing != work_order:
        raise PhononNegativePivotConflict(
            "existing pivot work order differs from requested preparation"
        )
    created = _write_new(output, work_order)
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "gate_id": work_order.gate_id,
            "source_campaign_id": work_order.source_campaign_id,
            "successor_campaign_id": work_order.successor_campaign_id,
            "required_replay_conclusion": work_order.required_replay_conclusion,
            "automatic_pivot": False,
            "data_allocation_allowed": False,
            "outward_actions_allowed": False,
            "output": str(output.resolve()),
            "created": created,
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    work_order = _work_order(Path(args.work_order))
    verify_phonon_negative_pivot_work_order(
        work_order,
        require_initial_graph=args.require_initial_graph,
    )
    _print(
        {
            "work_order_id": work_order.work_order_id,
            "verified": True,
            "initial_graph_required": args.require_initial_graph,
            "automatic_pivot": False,
        }
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    report = preflight_phonon_negative_pivot_start(_work_order(Path(args.work_order)))
    _print(report)
    return 0 if report.ready_for_explicit_gate_start else 2


def _execute(args: argparse.Namespace) -> int:
    receipt = execute_phonon_negative_result_pivot(
        _work_order(Path(args.work_order)),
        PhononReplayCommitReceipt.model_validate(_read(Path(args.replay_commit))),
    )
    created = _write_new(Path(args.output), receipt)
    _print({"receipt": receipt.model_dump(mode="json"), "output_created": created})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze the contradiction-only pivot work order")
    prepare.add_argument("--controller", required=True)
    prepare.add_argument("--protocol", required=True)
    prepare.add_argument("--commissioning", required=True)
    prepare.add_argument("--transition-principal", default="controller:phonon-science")
    prepare.add_argument("--assessed-by", default="harness:phonon-negative-pivot")
    prepare.add_argument("--producer", default="harness:phonon-negative-pivot")
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=_prepare)
    verify = commands.add_parser("verify", help="verify committed code and bound source files")
    verify.add_argument("work_order")
    verify.add_argument("--require-initial-graph", action="store_true")
    verify.set_defaults(handler=_verify)
    preflight = commands.add_parser(
        "preflight-start",
        help="verify the frozen pre-start graph and absence of a live gate",
    )
    preflight.add_argument("work_order")
    preflight.set_defaults(handler=_preflight)
    execute = commands.add_parser(
        "execute",
        help="pivot only from an exact committed contradicted replay",
    )
    execute.add_argument("work_order")
    execute.add_argument("replay_commit")
    execute.add_argument("--output", required=True)
    execute.set_defaults(handler=_execute)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
