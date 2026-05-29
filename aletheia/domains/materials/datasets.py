"""Dataset resolution for the materials domain.

The canonical Phase-1 dataset is ``matbench_expt_gap`` (experimental band gaps,
composition-only). We load it via matminer's dataset loader rather than the
``matbench`` PyPI package (whose pins don't build on py3.11); the data are the same.
"""

from __future__ import annotations

from typing import Any

# Target column shipped by each known benchmark (composition is the feature).
KNOWN_TARGETS = {
    "matbench_expt_gap": "gap expt",
    "expt_gap": "gap",
    "expt_gap_kingsbury": "expt_gap",
    "matbench_mp_gap": "gap pbe",
}
# Likely composition column names per benchmark (default "composition").
KNOWN_COMPOSITION = {}


def load_benchmark(ref: str) -> Any:
    """Load a matminer-hosted benchmark by name (downloads + caches on first use)."""
    from matminer.datasets import load_dataset

    return load_dataset(ref)


def resolve_columns(
    df: Any, data_spec: dict[str, Any]
) -> tuple[str, str]:
    """Decide which columns are composition (feature) and target.

    Order of preference: explicit spec -> known benchmark mapping -> heuristic
    (a string/object column for composition; the first numeric column for target).
    """
    import pandas as pd

    ref = (data_spec.get("ref") or "").strip()
    cols = [str(c) for c in df.columns]

    # --- composition column ---
    comp_col = data_spec.get("composition_column") or KNOWN_COMPOSITION.get(ref)
    if comp_col not in cols:
        comp_col = next(
            (c for c in cols if c.lower() in ("composition", "formula", "comp")),
            None,
        )
    if comp_col is None:
        # first non-numeric column
        comp_col = next((c for c in cols if not pd.api.types.is_numeric_dtype(df[c])), cols[0])

    # --- target column ---
    target = data_spec.get("target_column") or KNOWN_TARGETS.get(ref)
    if target not in cols:
        target = next(
            (c for c in cols if c != comp_col and pd.api.types.is_numeric_dtype(df[c])),
            None,
        )
    if target is None:
        raise ValueError(f"No numeric target column found in {cols!r}")
    return comp_col, target
