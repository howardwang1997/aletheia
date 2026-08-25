#!/usr/bin/env python3
"""Copy the two reviewed PR-0 legacy projections into a release source directory."""

from __future__ import annotations

import argparse
from pathlib import Path


REVIEWED_PATHS = (
    "endurance/report.json",
    "run_projections.v1.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    for relative_path in REVIEWED_PATHS:
        source = args.source_root / relative_path
        destination = args.output_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
