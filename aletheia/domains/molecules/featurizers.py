"""Molecular featurization for the molecules domain.

Morgan / ECFP fingerprints (RDKit) turn a SMILES string into a fixed-length binary
descriptor — the field's standard cheap baseline featurization, CPU-only and offline
(RDKit bundles everything). The leakage-aware grouping is the **Bemis–Murcko scaffold**
(the molecular core): molecules sharing a scaffold are kept together across CV folds,
the standard "scaffold split" that measures generalization to novel chemotypes rather
than memorizing close analogues. RDKit is imported lazily so this module loads (and the
plugin's ``profile()``) without RDKit present — only ``featurize`` needs it.
"""

from __future__ import annotations

from typing import Any


def morgan_features(
    df: Any, smiles_col: str = "smiles", *, n_bits: int = 1024, radius: int = 2
) -> tuple[Any, list[str], Any]:
    """Featurize a DataFrame's SMILES column with Morgan/ECFP fingerprints.

    Returns ``(X, feature_names, work_df)`` where rows with un-parseable SMILES are
    dropped and ``work_df`` is the aligned frame (so the caller can pull the target +
    compute scaffolds for the same rows). A ``_mol`` column holds the RDKit mol.
    """
    import numpy as np
    import pandas as pd
    from rdkit import Chem, DataStructs
    from rdkit.Chem import AllChem

    work = df.copy()
    work["_mol"] = work[smiles_col].map(lambda s: Chem.MolFromSmiles(str(s)) if s is not None else None)
    work = work[work["_mol"].notna()].reset_index(drop=True)

    rows = []
    for mol in work["_mol"]:
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, radius, nBits=n_bits)
        arr = np.zeros((n_bits,), dtype=np.int8)
        DataStructs.ConvertToNumpyArray(fp, arr)
        rows.append(arr)
    feature_names = [f"ecfp_{i}" for i in range(n_bits)]
    X = pd.DataFrame(np.vstack(rows), columns=feature_names, index=work.index)
    return X, feature_names, work


def scaffold_groups(work: Any) -> Any:
    """Per-row Bemis–Murcko scaffold key (the molecular core SMILES), aligned with the
    rows ``morgan_features`` kept. Acyclic molecules get an empty scaffold (grouped
    together). The shared protocol falls back to KFold if too few distinct scaffolds."""
    from rdkit.Chem.Scaffolds import MurckoScaffold

    def _scaffold(mol: Any) -> str:
        try:
            return MurckoScaffold.MurckoScaffoldSmiles(mol=mol) or "_acyclic"
        except Exception:  # pragma: no cover - defensive on odd molecules
            return "_acyclic"

    return work["_mol"].map(_scaffold).to_numpy()
