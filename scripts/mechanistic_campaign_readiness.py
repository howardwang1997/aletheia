#!/usr/bin/env python3
"""Build a fail-closed F10-S6 readiness audit from a capability registry."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from aletheia.capabilities import CapabilityRegistrySnapshot
from aletheia.domains.materials import (
    ConfirmationIndependenceKind,
    MechanisticCapabilityQualification,
    build_mechanistic_campaign_readiness_audit,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset or Z")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--audited-at", type=_aware_datetime, required=True)
    parser.add_argument(
        "--family-qualification",
        action="append",
        default=[],
        type=Path,
        help="repeatable JSON qualification for assigning one latest manifest to C1-C4",
    )
    parser.add_argument("--production-direction-gate-sha256")
    parser.add_argument("--ready-hypothesis-campaign-sha256")
    parser.add_argument("--ready-causal-campaign-sha256")
    parser.add_argument("--fresh-confirmation-reservation-sha256")
    parser.add_argument(
        "--independent-confirmation-kind",
        choices=tuple(item.value for item in ConfirmationIndependenceKind),
    )
    parser.add_argument("--require-execution-ready", action="store_true")
    parser.add_argument("--require-scientific-release-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    registry = CapabilityRegistrySnapshot.model_validate_json(
        args.registry.read_text(encoding="utf-8")
    )
    qualifications = tuple(
        MechanisticCapabilityQualification.model_validate_json(path.read_text(encoding="utf-8"))
        for path in args.family_qualification
    )
    audit = build_mechanistic_campaign_readiness_audit(
        audit_id=args.audit_id,
        registry=registry,
        family_qualifications=qualifications,
        production_direction_gate_sha256=args.production_direction_gate_sha256,
        ready_hypothesis_campaign_sha256=args.ready_hypothesis_campaign_sha256,
        ready_causal_campaign_sha256=args.ready_causal_campaign_sha256,
        fresh_confirmation_reservation_sha256=(args.fresh_confirmation_reservation_sha256),
        independent_confirmation_kind=(
            ConfirmationIndependenceKind(args.independent_confirmation_kind)
            if args.independent_confirmation_kind is not None
            else None
        ),
        audited_at=args.audited_at,
    )
    print(json.dumps(audit.model_dump(mode="json"), indent=2, sort_keys=True))
    if args.require_scientific_release_ready and not audit.scientific_release_ready:
        return 3
    if args.require_execution_ready and not audit.execution_ready:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
