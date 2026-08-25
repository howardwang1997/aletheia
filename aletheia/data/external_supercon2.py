"""Pinned, deterministic preparation of the SuperCon2 external replication asset.

SuperCon2 was extracted automatically from superconductivity papers by a pipeline
independent of the hand-curated SuperCon/UCI training asset.  It is therefore a
useful *independent-extraction* replication set, but not a claim of wholly novel
materials: exact normalized-formula overlap with the primary asset is measured and
disclosed in the generated provenance rather than silently discarded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SUPERCON2_COMMIT = "c6dc5b231511d9617cf372fe553588d4773b24ea"
SUPERCON2_FILENAME = "supercon2_v22.12.03.csv"
SUPERCON2_URL = (
    "https://raw.githubusercontent.com/lfoppiano/supercon/"
    f"{SUPERCON2_COMMIT}/data/{SUPERCON2_FILENAME}"
)
SUPERCON2_RAW_SHA256 = "13a18935213c570bb06d1f7ba4aa209abe535b37eaae3c8dafa382b4bb8efc52"
SUPERCON2_DATASET_DOI = "10.48505/nims.3735"
SUPERCON2_METHOD_DOI = "10.1080/27660400.2022.2153633"
SUPERCON2_MDR_FILE_URL = (
    "https://mdr.nims.go.jp/filesets/b737b44a-b07a-4853-9378-8ba63f644e79?locale=en"
)
SUPERCON2_MDR_MD5 = "ed55ea8f43a0984d77b5c29576164e06"
PREPROCESSING_VERSION = "supercon2-external-v1"


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_supercon2_raw(path: Path | str) -> Path:
    """Materialize the immutable source file and verify its expected SHA-256."""
    import httpx

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        partial = path.with_suffix(path.suffix + ".part")
        with httpx.stream("GET", SUPERCON2_URL, follow_redirects=True, timeout=300.0) as response:
            response.raise_for_status()
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
        partial.replace(path)
    actual = file_sha256(path)
    if actual != SUPERCON2_RAW_SHA256:
        raise RuntimeError(
            f"SuperCon2 source checksum mismatch: expected {SUPERCON2_RAW_SHA256}, got {actual}"
        )
    return path


def _normalized_formula(value: Any) -> str | None:
    from pymatgen.core import Composition

    try:
        raw = str(value).strip()
        if not raw or raw.lower() in {"nan", "none", "null"}:
            return None
        formula = Composition(raw.replace(" ", "")).reduced_formula
        return formula or None
    except Exception:  # noqa: BLE001 - malformed extracted formula is a declared exclusion
        return None


def prepare_supercon2_external(
    raw_path: Path | str,
    output_path: Path | str,
    *,
    primary_path: Path | str | None = None,
) -> dict[str, Any]:
    """Apply a fixed, target-independent cleanup and write the external CSV + data card."""
    import numpy as np
    import pandas as pd

    raw_path = Path(raw_path)
    output_path = Path(output_path)
    raw_sha = file_sha256(raw_path)
    if raw_sha != SUPERCON2_RAW_SHA256:
        raise RuntimeError("refusing to prepare an unpinned SuperCon2 source")

    source = pd.read_csv(raw_path, low_memory=False)
    source_rows = int(len(source))
    source["_normalized_formula"] = source["formula"].map(_normalized_formula)
    # The official release stores Tc as text (for example ``2.8 K``), including ambiguous
    # ranges and multiple values.  Accept one scalar Kelvin value only; never choose a value from
    # a range after seeing whether it helps the hypothesis.
    scalar_kelvin = source["criticalTemperature"].astype(str).str.extract(
        r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*K\s*$",
        expand=False,
    )
    source["_tc"] = pd.to_numeric(scalar_kelvin, errors="coerce")
    source["_year"] = pd.to_numeric(source.get("year"), errors="coerce")

    # Fixed validity policy, declared before any replication outcome: Kelvin only,
    # parseable formula, finite physically plausible Tc, and non-malformed publication year.
    valid = (
        source["_normalized_formula"].notna()
        & np.isfinite(source["_tc"])
        & source["_tc"].between(0.0, 300.0)
        & (source["_year"].isna() | source["_year"].between(1900, 2022))
    )
    work = source.loc[valid].copy()
    work["_paper_key"] = (
        work["doi"].fillna("").astype(str).str.strip().str.lower()
    )
    fallback = work["hash"].fillna(work["id"]).astype(str)
    work.loc[work["_paper_key"].eq(""), "_paper_key"] = fallback
    before_dedup = int(len(work))
    work = work.drop_duplicates(["_normalized_formula", "_tc", "_paper_key"], keep="first")
    work = work.sort_values(
        ["_normalized_formula", "_tc", "_paper_key", "id"], kind="mergesort"
    ).reset_index(drop=True)

    result = pd.DataFrame({
        "material": work["_normalized_formula"].astype(str),
        "critical_temp": work["_tc"].astype(float),
        "source_id": work["id"].astype(str),
        "source_formula": work["formula"].astype(str),
        "source_doi": work["doi"].fillna("").astype(str),
        "source_year": work["_year"],
        "source_record_hash": work["hash"].fillna("").astype(str),
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, lineterminator="\n")
    processed_sha = file_sha256(output_path)

    overlap: dict[str, Any] = {"measured": False}
    if primary_path is not None:
        primary_path = Path(primary_path)
        primary = pd.read_csv(primary_path, usecols=["material"], low_memory=False)
        primary_formulas = {
            f for f in primary["material"].map(_normalized_formula).tolist() if f is not None
        }
        ext_formulas = set(result["material"])
        row_overlap = result["material"].isin(primary_formulas)
        overlap = {
            "measured": True,
            "primary_content_sha256": file_sha256(primary_path),
            "primary_unique_normalized_formulas": len(primary_formulas),
            "external_unique_normalized_formulas": len(ext_formulas),
            "overlap_unique_normalized_formulas": len(ext_formulas & primary_formulas),
            "nonoverlap_unique_normalized_formulas": len(ext_formulas - primary_formulas),
            "overlap_rows": int(row_overlap.sum()),
            "nonoverlap_rows": int((~row_overlap).sum()),
        }

    provenance: dict[str, Any] = {
        "data_role": "external_validation",
        "independence_class": "independent_literature_extraction_with_disclosed_formula_overlap",
        "source": {
            "name": "SuperCon2",
            "url": SUPERCON2_URL,
            "commit": SUPERCON2_COMMIT,
            "raw_filename": SUPERCON2_FILENAME,
            "raw_sha256": raw_sha,
            "mdr_file_url": SUPERCON2_MDR_FILE_URL,
            "mdr_md5": SUPERCON2_MDR_MD5,
            "dataset_doi": SUPERCON2_DATASET_DOI,
            "method_doi": SUPERCON2_METHOD_DOI,
            "license": "CC BY 4.0",
        },
        "preprocessing": {
            "version": PREPROCESSING_VERSION,
            "policy": [
                "criticalTemperature matches one scalar '<number> K' value (ranges/multiple values excluded)",
                "formula parses with pymatgen Composition and is reduced",
                "critical_temp is finite and in [0, 300] K",
                "publication year is missing or in [1900, 2022]",
                "deduplicate normalized_formula + critical_temp + DOI/source-record",
                "stable lexicographic row order",
            ],
            "source_rows": source_rows,
            "valid_rows_before_deduplication": before_dedup,
            "excluded_invalid_rows": source_rows - before_dedup,
            "deduplicated_rows": before_dedup - int(len(result)),
        },
        "processed": {
            "path": str(output_path),
            "sha256": processed_sha,
            "rows": int(len(result)),
            "unique_normalized_formulas": int(result["material"].nunique()),
            "unique_source_dois": int(result.loc[result["source_doi"].ne(""), "source_doi"].nunique()),
        },
        "primary_overlap": overlap,
        "limitations": [
            "This is an independent extraction pipeline, not an independent laboratory campaign.",
            "Some normalized formulas overlap the UCI/SuperCon-derived primary asset; overlap is disclosed.",
            "The locked demonstration is evaluated without outcome-dependent external preprocessing.",
        ],
    }
    card = output_path.with_suffix(output_path.suffix + ".provenance.json")
    card.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    return provenance
