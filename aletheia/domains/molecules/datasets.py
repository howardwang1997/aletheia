"""Dataset resolution for the molecules domain.

The canonical benchmark is **MoleculeNet ESOL** (Delaney aqueous solubility), the
field's standard small regression benchmark with a published scaffold-split SOTA. Its
exact public CSV bytes are packaged and hash-verified so scientific demonstrations do
not depend on network availability or silently follow upstream data drift.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

ESOL_URL = "https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/delaney-processed.csv"
ESOL_RESOURCE = "data/delaney-processed.csv"
ESOL_PATH = Path(__file__).resolve().parent / ESOL_RESOURCE
ESOL_SHA256 = "8c06a76f0c6487d29ab0f903e6a7a7139f189ab3c1178f159c8be8964602f189"
ESOL_RECORD_COUNT = 1128
# AqSolDB curated aqueous-solubility dataset (~10k compounds), Harvard Dataverse
# (doi:10.7910/DVN/OVHAW8, curated-solubility-dataset.csv). OPTIONAL — a larger second
# benchmark for replicating a finding across datasets; network-gated, so callers that need
# offline determinism (e.g. the milestone law re-run) default to ESOL and fail-closed here.
AQSOLDB_URL = "https://dataverse.harvard.edu/api/access/datafile/3407241"

# Target column shipped by each known benchmark (SMILES is the feature).
KNOWN_TARGETS = {
    "esol": "measured log solubility in mols per litre",
    "delaney": "measured log solubility in mols per litre",
    "aqsoldb": "Solubility",
}
KNOWN_SMILES = {"esol": "smiles", "delaney": "smiles", "aqsoldb": "SMILES"}


class MolecularBenchmarkIntegrityError(RuntimeError):
    """A packaged scientific benchmark is absent or differs from its reviewed bytes."""


def _load_frozen_esol() -> Any:
    import pandas as pd

    try:
        payload = ESOL_PATH.read_bytes()
    except OSError as exc:
        raise MolecularBenchmarkIntegrityError("frozen ESOL benchmark is unavailable") from exc
    if hashlib.sha256(payload).hexdigest() != ESOL_SHA256:
        raise MolecularBenchmarkIntegrityError("frozen ESOL benchmark differs from its digest")
    frame = pd.read_csv(BytesIO(payload))
    if len(frame) != ESOL_RECORD_COUNT:
        raise MolecularBenchmarkIntegrityError("frozen ESOL benchmark record count differs")
    return frame


def load_benchmark(ref: str) -> Any:
    """Load a known molecular benchmark from reviewed bytes or its explicit remote source."""
    import pandas as pd

    key = (ref or "esol").strip().lower()
    if key in ("esol", "delaney"):
        return _load_frozen_esol()
    if key == "aqsoldb":
        return pd.read_csv(AQSOLDB_URL)
    raise ValueError(f"unknown molecular benchmark: {ref!r}")


def resolve_columns(df: Any, data_spec: dict[str, Any]) -> tuple[str, str]:
    """Decide which columns are SMILES (feature) and target.

    Order of preference: explicit spec -> known benchmark mapping -> heuristic (a
    column named like 'smiles' for the feature; the first numeric column for target).
    """
    import pandas as pd

    ref = (data_spec.get("ref") or "").strip().lower()
    cols = [str(c) for c in df.columns]

    smiles_col = data_spec.get("smiles_column") or KNOWN_SMILES.get(ref)
    if smiles_col not in cols:
        smiles_col = next((c for c in cols if "smiles" in c.lower()), None)
    if smiles_col is None:
        smiles_col = next((c for c in cols if not pd.api.types.is_numeric_dtype(df[c])), cols[0])

    target = data_spec.get("target_column") or KNOWN_TARGETS.get(ref)
    if target not in cols:
        target = next(
            (c for c in cols if c != smiles_col and pd.api.types.is_numeric_dtype(df[c])),
            None,
        )
    if target is None:
        raise ValueError(f"No numeric target column found in {cols!r}")
    return smiles_col, target
