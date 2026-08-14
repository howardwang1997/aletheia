"""Deterministic preparation of the pinned external replication asset."""

from __future__ import annotations

import pandas as pd

from aletheia.data import external_supercon2 as supercon2


def test_preparation_is_target_independent_deduplicated_and_discloses_overlap(
    tmp_path, monkeypatch
):
    raw = tmp_path / "raw.csv"
    primary = tmp_path / "primary.csv"
    output = tmp_path / "external.csv"
    pd.DataFrame([
        {
            "id": "1", "formula": "Ba2 Ca Cu2 O6", "criticalTemperature": "80 K",
            "year": 2020, "doi": "10.test/a", "hash": "h1",
        },
        # Same material/Tc/paper mention: deterministic pseudo-replicate removal.
        {
            "id": "2", "formula": "Ba2CaCu2O6", "criticalTemperature": "80 K",
            "year": 2020, "doi": "10.test/a", "hash": "h2",
        },
        {
            "id": "3", "formula": "FeSe", "criticalTemperature": "8 K",
            "year": 2021, "doi": "10.test/b", "hash": "h3",
        },
        {
            "id": "4", "formula": "FeSe", "criticalTemperature": "8-10 K",
            "year": 2021, "doi": "10.test/c", "hash": "h4",
        },
    ]).to_csv(raw, index=False)
    pd.DataFrame({"material": ["Ba2CaCu2O6", "MgB2"]}).to_csv(primary, index=False)
    raw_sha = supercon2.file_sha256(raw)
    monkeypatch.setattr(supercon2, "SUPERCON2_RAW_SHA256", raw_sha)

    first = supercon2.prepare_supercon2_external(raw, output, primary_path=primary)
    first_bytes = output.read_bytes()
    second = supercon2.prepare_supercon2_external(raw, output, primary_path=primary)

    assert output.read_bytes() == first_bytes
    assert first == second
    assert first["processed"]["rows"] == 2
    assert first["preprocessing"]["deduplicated_rows"] == 1
    assert first["preprocessing"]["excluded_invalid_rows"] == 1
    assert first["primary_overlap"]["overlap_rows"] == 1
    assert first["primary_overlap"]["nonoverlap_rows"] == 1
    assert output.with_suffix(".csv.provenance.json").exists()
