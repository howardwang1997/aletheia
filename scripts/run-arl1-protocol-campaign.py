#!/usr/bin/env python3
"""Run one explicitly approved, byte-pinned ARL-1 given-protocol campaign."""

from __future__ import annotations

import argparse
import sys

from aletheia.arl1_runtime import (
    execute_arl1_campaign_deployment,
    load_arl1_campaign_runtime_deployment,
)
from aletheia.research_kernel.schemas import canonical_json_bytes
from aletheia.schema_migrations import require_schema_exact

_ACKNOWLEDGEMENT = "RUN_ARL1_PROTOCOL_CAMPAIGN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-manifest",
        required=True,
        help="absolute path to the canonical one-shot deployment manifest",
    )
    parser.add_argument(
        "--deployment-manifest-sha256",
        required=True,
        help="out-of-band SHA-256 pin for the deployment-manifest bytes",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="allow the already-authorized campaign to reserve and execute work",
    )
    parser.add_argument(
        "--acknowledge",
        help=f"must equal {_ACKNOWLEDGEMENT!r}",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.apply or args.acknowledge != _ACKNOWLEDGEMENT:
        raise SystemExit(
            f"refusing ARL-1 campaign without --apply and exact --acknowledge {_ACKNOWLEDGEMENT}"
        )
    require_schema_exact()
    deployment = load_arl1_campaign_runtime_deployment(
        args.deployment_manifest,
        expected_file_sha256=args.deployment_manifest_sha256,
    )
    receipt = execute_arl1_campaign_deployment(deployment)
    sys.stdout.buffer.write(canonical_json_bytes(receipt))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
