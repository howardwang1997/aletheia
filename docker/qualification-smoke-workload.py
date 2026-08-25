#!/usr/local/bin/python3
"""Deterministic network-free workload used by the real OCI qualification campaign."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path


def _canonical_relative(value: str) -> str:
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in parts):
        raise argparse.ArgumentTypeError("artifact path must be canonical and relative")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--input", required=True, type=_canonical_relative)
    parser.add_argument("--output", required=True, type=_canonical_relative)
    arguments = parser.parse_args()
    source = arguments.input_root / arguments.input
    destination = arguments.output_root / arguments.output
    payload = source.read_bytes()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n"
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(result):
            written += os.write(descriptor, result[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
