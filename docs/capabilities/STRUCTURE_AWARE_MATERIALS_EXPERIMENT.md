# Structure-aware materials experiment

## What this capability does

F10-S4 tests whether correctly aligned crystal-structure information adds predictive value beyond
composition on one frozen materials task. It runs three arms on identical row roles:

```text
132 Magpie composition features
  ├── composition only
  ├── + 27 aligned species-blind geometry features
  └── + 27 within-role-permuted geometry features (matched control)
```

The aligned and permuted arms have the same 159 feature columns, random-forest settings, training
rows, evaluation rows, and fit budget. The primary estimand is paired
`permuted MAE - aligned MAE`; composition-only error is a descriptive reference.

## Quality and identity boundary

Every structure is projected to a primitive standard cell under frozen pymatgen/spglib versions.
The all-row gate checks ordered occupancy, 1–64 sites, volume per atom, distinct-site distance,
lattice conditioning, and symmetry standardization. It retains exact source-row, normalized formula,
chemical-system, structure-projection, and quality hashes.

The projection is a versioned local comparison contract, not a universal crystallographic
canonical form. Chemical systems—not individual rows—are assigned to train, internal validation,
or locked holdout. Missing, rejected, or cross-role identities fail closed.

## Frozen real protocol

The protocol is
`configs/materials/f10_matbench_phonons_structure_aware_v1.yaml`. It binds:

- the 459,672-byte gzip with SHA-256
  `4db551f21ec5f577e6202725f10e34dfc509aa7df3a6bdaac497da7f6dbbb9b3`;
- 1,265 rows and exact `structure` / `last phdos peak` columns;
- the CC0 provenance record and exact licence-evidence bytes;
- structure quality and 27-dimensional geometry policies;
- 60/20/20 target-blind chemical-system roles;
- a 512-tree fixed random forest for every arm, without tuning;
- one within-role permutation seed; and
- 5,000 chemical-system cluster-bootstrap resamples and a frozen 5% acceptance floor.

The implementation commitment is a combined hash over the experiment, structure, formula-identity,
and Magpie source files. The protocol also requires exact Matminer, NumPy, pandas, pymatgen,
scikit-learn, and spglib versions.

## Acquire and run

Raw data and generated evidence live under ignored `workspaces/`; they are not committed to source
control. Download and verify the exact public distribution:

```bash
mkdir -p workspaces/evaluator/materials-structure-phonons-v1/source
curl -L --fail --retry 3 \
  --output workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz \
  https://ml.materialsproject.org/projects/matbench_phonons.json.gz
shasum -a 256 \
  workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz
```

Create the immutable zero-fit plan, then run every frozen arm once:

```bash
conda run -n aletheia python scripts/structure_aware_materials_e2e.py prepare \
  --protocol configs/materials/f10_matbench_phonons_structure_aware_v1.yaml \
  --dataset-file workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz \
  --output workspaces/evaluator/materials-structure-phonons-v1/plan.json

conda run -n aletheia python scripts/structure_aware_materials_e2e.py run \
  --plan workspaces/evaluator/materials-structure-phonons-v1/plan.json \
  --dataset-file workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz \
  --output workspaces/evaluator/materials-structure-phonons-v1/result.json
```

The CLI refuses to overwrite either evidence object. Physical replay rehashes the dataset, rebuilds
all identities/features/splits, refits all deterministic arms, and recomputes the bootstrap:

```bash
conda run -n aletheia python scripts/structure_aware_materials_e2e.py verify \
  --plan workspaces/evaluator/materials-structure-phonons-v1/plan.json \
  --result workspaces/evaluator/materials-structure-phonons-v1/result.json \
  --dataset-file workspaces/evaluator/materials-structure-phonons-v1/source/matbench_phonons.json.gz
```

## Current real result

All 1,265 rows passed the gate. The split contains 759/253/253 rows and 650/216/216 disjoint chemical
systems. The frozen run produced:

| Role | Composition MAE | Permuted-control MAE | Aligned MAE | Relative improvement | 95% cluster CI for MAE gain |
|---|---:|---:|---:|---:|---:|
| internal validation | 93.025 | 98.750 | 47.207 | 52.20% | [33.393, 73.451] |
| locked holdout | 82.887 | 87.530 | 45.666 | 47.83% | [22.414, 68.321] |

Units are reciprocal centimetres (`cm-1`). Both roles passed the preregistered rule, producing
`robust_aligned_structure_signal`. Exact replay reproduced result hash
`f1384600dfbc8289e6643aae13e6dbb16b0b429c89e7325bd83c88cd8522bb29`.

## Claim boundary

This supports incremental predictive value of correctly aligned structure for this one frozen
Matbench phonon task. It does not establish:

- a causal or physical mechanism;
- performance on a fresh, external, experimental, or laboratory dataset;
- global superiority of structure-aware models;
- uncertainty calibration or measurement traceability;
- that the coarse feature vector is a complete structure representation; or
- registered confirmatory capability status.

The task is public, retrospective, and computed with DFPT. Stronger claims require F10-S5/S6
simulation calibration, an intervention, and independently sourced or implemented confirmation.
