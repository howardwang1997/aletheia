"""Profile a dataset so Aletheia plans against the *real* columns/target.

Used in the "push" scenario (the human provides data up front): the profile is
written to ``DataAsset.profile_json`` and seeded into the scoping conversation so
the agent designs against actual columns rather than guessing. ``pandas`` is a
Phase-1 dependency and is imported lazily so this module stays import-safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Column names that commonly denote a regression target in materials/ML datasets.
_TARGET_HINTS = (
    "band_gap",
    "bandgap",
    "gap",
    "target",
    "label",
    "e_form",
    "formation_energy",
    "energy",
    "property",
    "value",
    "y",
)
# Column names that commonly hold a chemical composition / formula.
_COMPOSITION_HINTS = ("composition", "formula", "comp", "material")


def profile_dataframe(
    df: Any, target_hint: str | None = None, max_cols: int = 200
) -> dict[str, Any]:
    """Return a compact, JSON-serializable profile of a DataFrame."""
    import pandas as pd

    n_rows, n_cols = int(df.shape[0]), int(df.shape[1])
    cols = [str(c) for c in list(df.columns)[:max_cols]]
    dtypes = {c: str(df[c].dtype) for c in cols}
    numeric_cols = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]

    # target candidates: explicit hint first, then name-matched, then any numeric col
    candidates: list[str] = []
    if target_hint and target_hint in dtypes:
        candidates.append(target_hint)
    for c in cols:
        if any(h in c.lower() for h in _TARGET_HINTS) and c not in candidates:
            candidates.append(c)
    for c in numeric_cols:
        if c not in candidates:
            candidates.append(c)

    composition_candidates = [c for c in cols if any(h in c.lower() for h in _COMPOSITION_HINTS)]

    stats: dict[str, dict[str, float]] = {}
    for c in numeric_cols[:50]:
        s = df[c].dropna()
        if len(s):
            stats[c] = {
                "min": float(s.min()),
                "max": float(s.max()),
                "mean": float(s.mean()),
                "std": float(s.std()) if len(s) > 1 else 0.0,
                "n_missing": int(df[c].isna().sum()),
            }

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "columns": cols,
        "dtypes": dtypes,
        "numeric_columns": numeric_cols,
        "composition_candidates": composition_candidates,
        "target_candidates": candidates[:10],
        "numeric_stats": stats,
    }


def profile_path(
    path: str, target_hint: str | None = None, sample_rows: int = 200_000
) -> dict[str, Any]:
    """Profile a file OR a directory of files, sampling for very large data.

    Reads at most ``sample_rows`` rows (across files for a directory) so profiling
    stays cheap on huge / sharded datasets, and records how many files were found,
    whether the profile is a sample, and the cheap total row count when available.
    """
    from aletheia.data.loaders import count_rows, list_tabular_files, read_tabular

    p = Path(path)
    files = list_tabular_files(p)
    df = read_tabular(p, nrows=sample_rows)
    profile = profile_dataframe(df, target_hint=target_hint)
    profile["path"] = str(p)
    profile["is_directory"] = p.is_dir()
    profile["n_files"] = len(files)
    profile["files"] = [str(f) for f in files[:20]]
    profile["profiled_rows"] = int(len(df))

    total = count_rows(p)
    if total is not None:
        profile["total_rows"] = int(total)
        profile["sampled"] = total > len(df)
    else:
        # couldn't count cheaply; we sampled if we hit the row cap
        profile["sampled"] = len(df) >= sample_rows
    return profile


# Back-compat alias (single-file callers): now also handles directories.
def profile_file(path: str, target_hint: str | None = None) -> dict[str, Any]:
    return profile_path(path, target_hint=target_hint)
