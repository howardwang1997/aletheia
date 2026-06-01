"""Dataset resolution for the molecules domain.

The canonical benchmark is **MoleculeNet ESOL** (Delaney aqueous solubility), the
field's standard small regression benchmark with a published scaffold-split SOTA. We
load the public CSV by URL (cached) — the unit test never downloads (it featurizes a
tiny in-memory SMILES frame).
"""

from __future__ import annotations

from typing import Any

ESOL_URL = "https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/delaney-processed.csv"

# Target column shipped by each known benchmark (SMILES is the feature).
KNOWN_TARGETS = {
    "esol": "measured log solubility in mols per litre",
    "delaney": "measured log solubility in mols per litre",
}
KNOWN_SMILES = {"esol": "smiles", "delaney": "smiles"}


def load_benchmark(ref: str) -> Any:
    """Load a known molecular benchmark by name (downloads + caches on first use)."""
    import pandas as pd

    key = (ref or "esol").strip().lower()
    if key in ("esol", "delaney"):
        return pd.read_csv(ESOL_URL)
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
