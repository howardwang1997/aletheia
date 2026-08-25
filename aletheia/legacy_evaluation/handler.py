#!/usr/local/bin/python3
"""Fixed-path handler for the PR-6 qualification-only OCI workload."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aletheia.domains.materials.matbench_task import MaterialsBandGapPlugin
from aletheia.legacy_evaluation.capability import (
    execute_qualified_legacy_evaluation_workload,
)
from aletheia.legacy_evaluation.contracts import (
    LegacyEvaluationHarnessManifest,
    LegacyEvaluationInvocation,
    LegacyEvaluationRawResult,
    canonical_json_bytes,
)

SOURCE_ROOT = Path("/opt/aletheia/src")
HARNESS_PATH = Path("/opt/aletheia/config/legacy-evaluation-materials-v1.json")
INVOCATION_PATH = Path("/opt/aletheia/input/legacy-evaluation-invocation.json")
TABLE_PATH = Path("/opt/aletheia/input/legacy-evaluation-table.csv")
OUTPUT_ROOT = Path("/opt/aletheia/output")
_MAX_JSON_BYTES = 2 * 1024 * 1024


class LegacyEvaluationHandlerError(RuntimeError):
    """The fixed image configuration or launch-gated input failed closed."""


def _read_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink():
        raise LegacyEvaluationHandlerError(f"{label} cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise LegacyEvaluationHandlerError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 2 or before.st_size > maximum_bytes:
            raise LegacyEvaluationHandlerError(f"{label} is not one bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise LegacyEvaluationHandlerError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LegacyEvaluationHandlerError(f"{label} grew while being read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise LegacyEvaluationHandlerError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _strict_json_bytes(path: Path, *, canonical: bool, label: str) -> bytes:
    payload = _read_regular_file(path, maximum_bytes=_MAX_JSON_BYTES, label=label)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LegacyEvaluationHandlerError(f"{label} repeats a JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise LegacyEvaluationHandlerError(f"{label} contains non-finite JSON {value}")

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyEvaluationHandlerError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise LegacyEvaluationHandlerError(f"{label} must contain one JSON object")
    if canonical and canonical_json_bytes(decoded) != payload:
        raise LegacyEvaluationHandlerError(f"{label} is not canonical JSON")
    return payload


def run_qualified_legacy_evaluation_handler(
    *,
    source_root: Path = SOURCE_ROOT,
    harness_path: Path = HARNESS_PATH,
    invocation_path: Path = INVOCATION_PATH,
    table_path: Path = TABLE_PATH,
    output_root: Path = OUTPUT_ROOT,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LegacyEvaluationRawResult:
    """Load only the fixed harness/invocation/table paths and execute the reviewed leaf."""

    try:
        harness = LegacyEvaluationHarnessManifest.model_validate_json(
            _strict_json_bytes(
                harness_path,
                canonical=False,
                label="legacy evaluation harness",
            )
        )
        invocation = LegacyEvaluationInvocation.model_validate_json(
            _strict_json_bytes(
                invocation_path,
                canonical=True,
                label="legacy evaluation invocation",
            )
        )
    except (LegacyEvaluationHandlerError, TypeError, ValueError) as exc:
        raise LegacyEvaluationHandlerError("legacy evaluation image inputs are invalid") from exc
    return execute_qualified_legacy_evaluation_workload(
        plugin=MaterialsBandGapPlugin(),
        harness=harness,
        source_root=source_root,
        invocation=invocation,
        input_table_path=table_path,
        output_root=output_root,
        clock=clock,
    )


def main() -> None:
    run_qualified_legacy_evaluation_handler()


if __name__ == "__main__":
    main()


__all__ = [
    "LegacyEvaluationHandlerError",
    "run_qualified_legacy_evaluation_handler",
]
