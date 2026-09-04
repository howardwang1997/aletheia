"""Integrity and offline-loading checks for packaged molecular benchmarks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aletheia.domains.molecules import datasets


def test_esol_benchmark_is_packaged_and_digest_pinned() -> None:
    payload = datasets.ESOL_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == datasets.ESOL_SHA256

    frame = datasets.load_benchmark("esol")
    assert len(frame) == datasets.ESOL_RECORD_COUNT == 1128
    assert frame.iloc[0]["Compound ID"] == "Amigdalin"
    assert frame.iloc[-1]["Compound ID"] == "Stirofos"


def test_esol_benchmark_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    corrupt_path = tmp_path / "delaney-processed.csv"
    corrupt_path.write_bytes(b"changed upstream bytes")
    monkeypatch.setattr(datasets, "ESOL_PATH", corrupt_path)

    with pytest.raises(
        datasets.MolecularBenchmarkIntegrityError,
        match="differs from its digest",
    ):
        datasets.load_benchmark("esol")
