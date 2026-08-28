#!/usr/bin/env python3
"""Prepare, issue, or freshly verify one deployment-pinned ARL-1 qualification."""

from __future__ import annotations

import argparse
import sys

from aletheia.arl1_qualification_runtime import (
    ARL1QualificationIssuanceDeploymentV1,
    ARL1QualificationVerificationDeploymentV1,
    ARL1SourceVerificationDeploymentV1,
    issue_arl1_qualification_deployment,
    load_arl1_qualification_runtime_deployment,
    prepare_arl1_evidence_bundle_deployment,
    verify_arl1_qualification_deployment,
)
from aletheia.research_kernel.schemas import canonical_json_bytes

_OPERATIONS = {
    "prepare": (
        ARL1SourceVerificationDeploymentV1,
        "PREPARE_ARL1_EVIDENCE_BUNDLE",
        prepare_arl1_evidence_bundle_deployment,
    ),
    "issue": (
        ARL1QualificationIssuanceDeploymentV1,
        "ISSUE_ARL1_QUALIFICATION",
        issue_arl1_qualification_deployment,
    ),
    "verify": (
        ARL1QualificationVerificationDeploymentV1,
        "VERIFY_ARL1_QUALIFICATION",
        verify_arl1_qualification_deployment,
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=tuple(_OPERATIONS))
    parser.add_argument("--deployment-manifest", required=True)
    parser.add_argument("--deployment-manifest-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledge", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    model_type, acknowledgement, operation = _OPERATIONS[args.operation]
    if not args.apply or args.acknowledge != acknowledgement:
        raise SystemExit(
            f"refusing ARL-1 {args.operation} without --apply and exact "
            f"--acknowledge {acknowledgement}"
        )
    deployment = load_arl1_qualification_runtime_deployment(
        args.deployment_manifest,
        expected_file_sha256=args.deployment_manifest_sha256,
        model_type=model_type,
    )
    result = operation(deployment)
    sys.stdout.buffer.write(canonical_json_bytes(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
