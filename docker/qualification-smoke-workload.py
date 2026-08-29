#!/usr/local/bin/python3
"""Deterministic network-free workload used by the real OCI qualification campaign."""

from __future__ import annotations

import argparse
import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path


_MAXIMUM_MINIMUM_RUNTIME_SECONDS = 3_600


def _canonical_relative(value: str) -> str:
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in parts):
        raise argparse.ArgumentTypeError("artifact path must be canonical and relative")
    return value


def _bounded_runtime_seconds(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise argparse.ArgumentTypeError("minimum runtime seconds must be a canonical integer")
    seconds = int(value)
    if not 0 <= seconds <= _MAXIMUM_MINIMUM_RUNTIME_SECONDS:
        raise argparse.ArgumentTypeError(
            f"minimum runtime seconds must be between 0 and {_MAXIMUM_MINIMUM_RUNTIME_SECONDS}"
        )
    return seconds


def _wait_for_minimum_runtime(
    seconds: int,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = clock() + seconds
    while (remaining := deadline - clock()) > 0:
        sleeper(remaining)


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--input", required=True, type=_canonical_relative)
    parser.add_argument("--output", required=True, type=_canonical_relative)
    parser.add_argument(
        "--minimum-runtime-seconds",
        default=0,
        type=_bounded_runtime_seconds,
        help="hold the container alive before publishing output (0-3600 seconds)",
    )
    arguments = parser.parse_args()
    source = arguments.input_root / arguments.input
    destination = arguments.output_root / arguments.output
    payload = source.read_bytes()
    _wait_for_minimum_runtime(arguments.minimum_runtime_seconds)
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
