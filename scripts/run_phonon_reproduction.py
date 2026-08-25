#!/usr/bin/env python3
"""Prepare and execute the production phonon implementation-diverse reproduction."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from aletheia.db import REPO_ROOT
from aletheia.domains.materials.capabilities.structure_discrimination import (
    StructureAwareExperimentPlan,
    StructureAwareExperimentResult,
)
from aletheia.domains.materials.phonon_commissioning import (
    PhononQuestCommissioningManifest,
)
from aletheia.domains.materials.phonon_reproduction import (
    PhononIndependentReplayProtocol,
    PhononIndependentReplayResult,
    activate_phonon_reproduction_campaign,
    commit_phonon_reproduction_outcome,
    execute_phonon_independent_replay,
    preflight_phonon_independent_replay,
    prepare_phonon_independent_replay_protocol,
    verify_phonon_independent_replay,
    verify_phonon_replay_artifact_files,
)
from aletheia.programs.endurance_controller import EnduranceControllerManifest

ModelT = TypeVar("ModelT", bound=BaseModel)


def _read(path: Path) -> Any:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def _model(path: Path, model: type[ModelT]) -> ModelT:
    return model.model_validate(_read(path))


def _dataset(path: Path, plan: StructureAwareExperimentPlan) -> tuple[bytes, Any]:
    from matminer.utils.io import load_dataframe_from_json

    resolved = path.resolve(strict=True)
    payload = resolved.read_bytes()
    frame = load_dataframe_from_json(str(resolved), pbar=False)
    if len(frame) != plan.dataset_receipt.row_count:
        raise ValueError("phonon reproduction dataset row count differs from source plan")
    return payload, frame


def _sources(protocol: PhononIndependentReplayProtocol):
    paths = verify_phonon_replay_artifact_files(protocol)
    plan = _model(paths["source_plan"], StructureAwareExperimentPlan)
    source_result = _model(paths["source_result"], StructureAwareExperimentResult)
    payload, frame = _dataset(paths["dataset"], plan)
    return plan, source_result, payload, frame


def _render(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _print(value: Any) -> None:
    print(_render(value).decode("utf-8"), end="")


def _write_new(path: Path, payload: bytes) -> None:
    destination = path.resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        view = memoryview(payload)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - OS writes progress or raises
                raise OSError("phonon reproduction evidence write made no progress")
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
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _prepare(args: argparse.Namespace) -> int:
    controller = _model(args.controller, EnduranceControllerManifest)
    commissioning = _model(args.commissioning, PhononQuestCommissioningManifest)
    plan = _model(args.source_plan, StructureAwareExperimentPlan)
    result = _model(args.source_result, StructureAwareExperimentResult)
    protocol = prepare_phonon_independent_replay_protocol(
        controller=controller,
        commissioning=commissioning,
        dataset_path=args.dataset,
        source_plan_path=args.source_plan,
        source_result_path=args.source_result,
        source_plan=plan,
        source_result=result,
        reproduction_campaign_id=args.reproduction_campaign_id,
        prepared_at=datetime.now(timezone.utc),
    )
    _write_new(args.output, _render(protocol))
    _print(
        {
            "protocol_id": protocol.protocol_id,
            "protocol_sha256": protocol.protocol_sha256,
            "gate_id": protocol.gate_id,
            "original_campaign_id": protocol.original_campaign_id,
            "reproduction_campaign_id": protocol.reproduction_campaign_id,
            "code_sha256": protocol.code_identity.aggregate_sha256,
            "output": str(args.output.resolve()),
            "same_source_only": True,
            "model_fit_count": 0,
        }
    )
    return 0


def _activate(args: argparse.Namespace) -> int:
    _print(
        activate_phonon_reproduction_campaign(
            _model(args.protocol, PhononIndependentReplayProtocol),
            principal=args.principal,
        )
    )
    return 0


def _preflight(args: argparse.Namespace) -> int:
    report = preflight_phonon_independent_replay(
        _model(args.protocol, PhononIndependentReplayProtocol),
        _model(args.controller, EnduranceControllerManifest),
    )
    _print(report)
    return 0 if report.ready_for_gate_start else 2


def _run(args: argparse.Namespace) -> int:
    protocol = _model(args.protocol, PhononIndependentReplayProtocol)
    plan, source_result, payload, frame = _sources(protocol)
    result = execute_phonon_independent_replay(
        protocol=protocol,
        plan=plan,
        source_result=source_result,
        dataframe=frame,
        dataset_file_bytes=payload,
    )
    _write_new(args.output, _render(result))
    _print(
        {
            "result_id": result.result_id,
            "result_sha256": result.result_sha256,
            "disposition": result.disposition.value,
            "completed_at": result.completed_at.isoformat(),
            "output": str(args.output.resolve()),
            "same_source_only": True,
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    protocol = _model(args.protocol, PhononIndependentReplayProtocol)
    result = _model(args.result, PhononIndependentReplayResult)
    plan, source_result, payload, frame = _sources(protocol)
    verify_phonon_independent_replay(
        protocol=protocol,
        result=result,
        plan=plan,
        source_result=source_result,
        dataframe=frame,
        dataset_file_bytes=payload,
    )
    _print(
        {
            "protocol_id": protocol.protocol_id,
            "result_id": result.result_id,
            "result_sha256": result.result_sha256,
            "exact_replay": True,
            "source_bytes_rehashed": True,
            "feature_matrices_independently_reconstructed": True,
            "same_source_only": True,
        }
    )
    return 0


def _commit(args: argparse.Namespace) -> int:
    protocol = _model(args.protocol, PhononIndependentReplayProtocol)
    result = _model(args.result, PhononIndependentReplayResult)
    controller = _model(args.controller, EnduranceControllerManifest)
    plan, source_result, payload, frame = _sources(protocol)
    _print(
        commit_phonon_reproduction_outcome(
            protocol=protocol,
            result=result,
            controller=controller,
            plan=plan,
            source_result=source_result,
            dataframe=frame,
            dataset_file_bytes=payload,
            result_uri=args.result_uri,
            principal=args.principal,
            producer=args.producer,
            artifact_root=args.artifact_root,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="freeze the zero-fit reproduction protocol")
    prepare.add_argument("--controller", type=Path, required=True)
    prepare.add_argument("--commissioning", type=Path, required=True)
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--source-plan", type=Path, required=True)
    prepare.add_argument("--source-result", type=Path, required=True)
    prepare.add_argument("--reproduction-campaign-id", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.set_defaults(handler=_prepare)

    preflight = commands.add_parser(
        "preflight",
        help="verify zero-fit code/source/Campaign readiness without starting the gate",
    )
    preflight.add_argument("protocol", type=Path)
    preflight.add_argument("--controller", type=Path, required=True)
    preflight.set_defaults(handler=_preflight)

    activate = commands.add_parser(
        "activate",
        help="activate the reproduction Campaign after the endurance gate starts",
    )
    activate.add_argument("protocol", type=Path)
    activate.add_argument("--principal", required=True)
    activate.set_defaults(handler=_activate)

    run = commands.add_parser(
        "run",
        help="fit the frozen independent estimator only inside the live gate",
    )
    run.add_argument("protocol", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=_run)

    verify = commands.add_parser("verify", help="physically replay the retained result")
    verify.add_argument("protocol", type=Path)
    verify.add_argument("result", type=Path)
    verify.set_defaults(handler=_verify)

    commit = commands.add_parser(
        "commit",
        help="record the outcome fact and spool its typed endurance reproduction receipt",
    )
    commit.add_argument("protocol", type=Path)
    commit.add_argument("result", type=Path)
    commit.add_argument("--controller", type=Path, required=True)
    commit.add_argument("--result-uri", required=True)
    commit.add_argument("--principal", required=True)
    commit.add_argument("--producer", required=True)
    commit.add_argument("--artifact-root", type=Path, default=REPO_ROOT)
    commit.set_defaults(handler=_commit)
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
