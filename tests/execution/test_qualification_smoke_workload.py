from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKLOAD_PATH = REPOSITORY_ROOT / "docker" / "qualification-smoke-workload.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qualification_smoke_workload", WORKLOAD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", ("-1", "+1", "01", "601", "1.0", "one"))
def test_minimum_runtime_rejects_noncanonical_or_unbounded_values(value: str) -> None:
    workload = _module()

    with pytest.raises(argparse.ArgumentTypeError, match="minimum runtime seconds"):
        workload._bounded_runtime_seconds(value)  # noqa: SLF001


def test_wait_rechecks_the_clock_after_an_early_wakeup() -> None:
    workload = _module()
    samples = iter((10.0, 10.5, 11.75, 12.0))
    sleeps: list[float] = []

    workload._wait_for_minimum_runtime(  # noqa: SLF001
        2,
        clock=lambda: next(samples),
        sleeper=sleeps.append,
    )

    assert sleeps == [1.5, 0.25]


def test_main_holds_before_atomically_publishing_the_same_deterministic_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workload = _module()
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    input_root.joinpath("payload.bin").write_bytes(b"qualification payload")
    waits: list[int] = []
    monkeypatch.setattr(workload, "_wait_for_minimum_runtime", waits.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(WORKLOAD_PATH),
            "--input-root",
            str(input_root),
            "--output-root",
            str(output_root),
            "--input",
            "payload.bin",
            "--output",
            "result.sha256",
            "--minimum-runtime-seconds",
            "90",
        ],
    )

    workload.main()

    assert waits == [90]
    assert output_root.joinpath("result.sha256").read_bytes() == (
        hashlib.sha256(b"qualification payload").hexdigest().encode() + b"\n"
    )
    assert output_root.joinpath("result.sha256").stat().st_mode & 0o777 == 0o600
