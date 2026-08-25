#!/usr/bin/env python3
"""Run the PR-0 migration checks and every frozen legacy golden test node."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_CONTRACT = REPOSITORY_ROOT / "tests/fixtures/legacy/v1/golden_contract.v1.json"


def golden_test_nodeids() -> tuple[str, ...]:
    payload = json.loads(GOLDEN_CONTRACT.read_text(encoding="utf-8"))
    nodeids: list[str] = []
    for source in payload["source_identities"]:
        if source["kind"] != "offline_test":
            continue
        relative = Path(source["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe golden test path: {source['path']!r}")
        for node in source["test_nodes"]:
            nodeids.append(f"{relative.as_posix()}::{node}")
    if not nodeids or len(nodeids) != len(set(nodeids)):
        raise ValueError("golden test node IDs must be non-empty and unique")
    return tuple(nodeids)


def gate_pytest_args() -> tuple[str, ...]:
    return ("-q", "tests/migration", *golden_test_nodeids())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the exact pytest arguments without executing the gate",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="exercise the real subprocess/collection path without running tests",
    )
    args = parser.parse_args()
    pytest_args = gate_pytest_args()
    if args.list:
        print("\n".join(pytest_args))
        return 0
    if args.collect_only:
        pytest_args = ("--collect-only", *pytest_args)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args],
        cwd=REPOSITORY_ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
