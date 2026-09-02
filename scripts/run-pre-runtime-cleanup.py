#!/usr/bin/env python3
"""Run one frozen, attempt-scoped pre-runtime cleanup command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aletheia.execution.node_agent import NodeRunOutcome
from aletheia.execution.qualification_node_service import (
    compose_attempt_scoped_pre_runtime_cleanup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--attempt-id", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    worker = compose_attempt_scoped_pre_runtime_cleanup(
        configuration_bytes=args.config.read_bytes(),
        expected_configuration_sha256=args.config_sha256,
        attempt_id=args.attempt_id,
    )
    result = worker.recover(attempt_id=args.attempt_id)
    print(
        json.dumps(
            {
                "attempt_id": result.attempt_id,
                "outcome": result.outcome.value,
                "qualification_only": True,
                "scientific_admission_allowed": False,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if result.outcome is NodeRunOutcome.PRE_RUNTIME_RELEASED else 75


if __name__ == "__main__":
    raise SystemExit(main())
