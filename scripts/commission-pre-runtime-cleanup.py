#!/usr/bin/env python3
"""Commission one target-local attempt-scoped cleanup key and config."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from aletheia.execution.schemas import canonical_json_bytes
from aletheia.pre_runtime_cleanup_commissioning import (
    PreRuntimeCleanupCommissioningRequestV1,
    commission_pre_runtime_cleanup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-node-config", required=True, type=Path)
    parser.add_argument("--source-node-config-sha256", required=True)
    parser.add_argument("--cleanup-key", required=True, type=Path)
    parser.add_argument("--cleanup-config", required=True, type=Path)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--runtime-preparation-sha256", required=True)
    parser.add_argument("--runtime-launch-authorization-sha256", required=True)
    parser.add_argument("--cleanup-absence-epoch", required=True, type=int)
    parser.add_argument("--valid-from", required=True, type=datetime.fromisoformat)
    parser.add_argument("--expires-at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--configured-at", required=True, type=datetime.fromisoformat)
    return parser


def main() -> int:
    args = _parser().parse_args()
    request = PreRuntimeCleanupCommissioningRequestV1(
        source_node_config_path=str(args.source_node_config),
        source_node_config_sha256=args.source_node_config_sha256,
        target_cleanup_key_path=str(args.cleanup_key),
        target_cleanup_config_path=str(args.cleanup_config),
        principal_id=args.principal_id,
        policy_sha256=args.policy_sha256,
        infrastructure_attempt_id=args.attempt_id,
        runtime_preparation_sha256=args.runtime_preparation_sha256,
        runtime_launch_authorization_sha256=(args.runtime_launch_authorization_sha256),
        cleanup_absence_epoch=args.cleanup_absence_epoch,
        valid_from=args.valid_from,
        expires_at=args.expires_at,
        configured_at=args.configured_at,
    )
    receipt = commission_pre_runtime_cleanup(request)
    print(canonical_json_bytes(receipt).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
