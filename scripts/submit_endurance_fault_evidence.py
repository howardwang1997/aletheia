#!/usr/bin/env python3
"""Submit process/provider receipts from a committed in-window fault bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aletheia.db import REPO_ROOT
from aletheia.jobs import FaultHarnessEvidenceBundle, validate_fault_harness_bundle
from aletheia.programs.endurance_controller import EnduranceControllerManifest
from aletheia.programs.endurance_fault_evidence import submit_endurance_fault_evidence


def _read(path: Path) -> Any:
    return json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("controller", type=Path)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--producer", required=True)
    parser.add_argument("--artifact-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args()
    controller = EnduranceControllerManifest.model_validate(_read(args.controller))
    report = validate_fault_harness_bundle(
        FaultHarnessEvidenceBundle.model_validate(_read(args.bundle))
    ).report
    receipt = submit_endurance_fault_evidence(
        controller,
        report,
        producer=args.producer,
        artifact_root=args.artifact_root,
    )
    print(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
