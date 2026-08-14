"""Prepare the pinned SuperCon2 asset used by the real K2 external replication."""

from __future__ import annotations

import json
from pathlib import Path

from aletheia.data.external_supercon2 import (
    SUPERCON2_FILENAME,
    fetch_supercon2_raw,
    prepare_supercon2_external,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "artifacts" / "datasets" / "external"
    raw = fetch_supercon2_raw(data_dir / SUPERCON2_FILENAME)
    output = data_dir / "supercon2_external_v1.csv"
    primary = root / "artifacts" / "datasets" / "superconduct_unique_m.csv"
    if not primary.exists():
        raise SystemExit(f"missing primary asset: {primary}")
    provenance = prepare_supercon2_external(raw, output, primary_path=primary)
    print(json.dumps({
        "output": str(output),
        "sha256": provenance["processed"]["sha256"],
        "rows": provenance["processed"]["rows"],
        "formula_overlap": provenance["primary_overlap"],
    }, indent=2))


if __name__ == "__main__":
    main()
