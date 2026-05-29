"""Composition featurization for the materials domain.

Magpie (matminer ``ElementProperty``) turns a chemical formula into ~132 numeric
descriptors from elemental properties — composition-only, no crystal structure, so
it's CPU-cheap and works fully offline (matminer bundles the element data).
"""

from __future__ import annotations

from typing import Any


def _to_composition(value: Any):
    from pymatgen.core import Composition

    return value if isinstance(value, Composition) else Composition(value)


def magpie_features(df: Any, composition_col: str = "composition") -> tuple[Any, list[str], Any]:
    """Featurize a DataFrame's composition column with Magpie.

    Returns ``(X, feature_names, work_df)`` where ``X`` is the feature DataFrame
    (rows with un-featurizable compositions dropped) and ``work_df`` is the aligned
    frame (so the caller can pull the target column for the same rows).
    """
    from matminer.featurizers.composition import ElementProperty

    work = df.copy()
    work["_comp_obj"] = work[composition_col].map(_to_composition)

    ep = ElementProperty.from_preset("magpie")
    # single-process: robust inside subprocesses / test runners
    try:
        ep.set_n_jobs(1)
    except Exception:  # pragma: no cover - matminer version differences
        ep.n_jobs = 1

    work = ep.featurize_dataframe(work, "_comp_obj", ignore_errors=True, pbar=False)
    feature_names = ep.feature_labels()

    # drop rows Magpie couldn't featurize (NaN across all feature cols)
    work = work.dropna(subset=feature_names, how="any")
    X = work[feature_names]
    return X, feature_names, work
