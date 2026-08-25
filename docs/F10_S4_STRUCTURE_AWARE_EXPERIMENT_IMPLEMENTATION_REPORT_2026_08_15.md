# F10-S4 structure-aware experiment implementation report

Date: 2026-08-15
Status: Core engineering and real same-dataset structure-discrimination evidence complete

## Outcome

Aletheia can now quality-gate crystal structures, create content-addressed species-blind geometry
features, freeze a chemical-system-disjoint experiment before any model fit, run composition-only,
aligned-structure, and matched permuted-structure arms, and derive a paired cluster-bootstrap
verdict without selecting a favorable seed or dropping an arm.

The real frozen `matbench_phonons` run found a robust aligned-structure signal in both evaluation
roles and was exactly physically replayed. This closes F10-S4's engineering slice. It does not close
the project's scientific-exit goal: the holdout is from the same public retrospective computed
dataset, and the result is neither an external replication nor causal/mechanistic evidence.

## Implemented code boundary

### Structure quality and features

`aletheia/domains/materials/structures.py` adds:

- ordered-occupancy, site-count, volume, overlap, lattice-condition, and symmetry gates;
- per-row raw parsed-structure, formula, chemical-system, primitive-standard projection, and quality
  receipts;
- a complete all-row ledger that rejects rather than silently filters bad structures;
- fixed species-blind lattice, symmetry, neighbour-distance, crystal-system, and radial features;
  and
- exact feature-name, matrix, policy, and source-ledger hashes.

### Matched experiment

`aletheia/domains/materials/capabilities/structure_discrimination.py` adds:

- exact dataset/licence/environment/source-code contracts;
- target-blind, row-balanced chemical-system assignment with group-disjoint validation;
- 132-dimensional Magpie composition and 27-dimensional structure matrices;
- independent within-role structure permutation for train, internal validation, and locked holdout;
- equal 159-dimensional capacity and estimator budgets for aligned/control arms;
- one fixed random-forest fit per arm during result generation;
- complete six-cell arm/role metrics and prediction/error hashes;
- paired chemical-system cluster bootstrap and mechanically derived disposition; and
- exact physical replay that fails on data, feature, split, environment, model, metric, bootstrap,
  or result drift.

The immutable CLI in `scripts/structure_aware_materials_e2e.py` separates zero-fit plan creation,
execution, and replay and refuses to overwrite evidence.

## Gold and adversarial evidence

Nine focused tests cover:

- same formula with different geometry and structure hashes;
- disordered and overlapping-site rejection;
- target-blind split invariance under a shifted target vector;
- complete chemical-system group isolation;
- all three arms and both evaluation roles;
- exact feature-capacity matching and within-role permutation;
- a synthetic aligned signal stronger than its matched control;
- result/disposition/arm-metric tampering;
- changed implementation rejection; and
- inclusion of all four feature/result source files in the implementation commitment.

The combined F10-S3/S4 focused selection passes `23 passed`; the broader materials/capabilities
selection passes `52 passed`. Ruff and formatting checks pass. The upstream spglib calls emit
deprecation warnings but no quality or numerical failures.

The authoritative host non-Docker regression passes `1189 passed, 1 skipped, 29 deselected` in
721.83 seconds. The host run was required for the suite's local PostgreSQL integration tests; Docker
tests remain deliberately deselected from this acceptance run.

## Frozen real protocol and pre-fit plan

A structure-only preflight was completed before the protocol freeze. It did not inspect target
performance. All 1,265 rows passed the proposed quality gate; the data contained 1,265 unique
structure projections, 1,171 formula identities, and 1,082 chemical systems. The preflight feature
matrix was defined for every row.

The final protocol then froze the exact gzip, licence evidence, code bundle, package versions,
quality/feature policies, split/model/permutation/bootstrap seeds, estimator budget, and acceptance
rule. Only after that freeze did plan construction hash the target vector. The plan records zero
model fits.

| Frozen object | Value |
|---|---|
| dataset bytes / SHA-256 | 459,672 / `4db551f21ec5f577e6202725f10e34dfc509aa7df3a6bdaac497da7f6dbbb9b3` |
| protocol SHA-256 | `f0062d6a611901cefb43832d77f9486ae334adad9fdc4400a19d43eb5cc9f253` |
| implementation bundle SHA-256 | `308ab13e681800b52b6fc91a42b01a3cb664ea1b66df8b536b38e5f10597cb1d` |
| plan SHA-256 | `3e1bca95857b735e01b88f1b07a410e92b68bd675075efbd7b779731e43449ee` |
| quality ledger SHA-256 | `b280b1477392f2dd95c213c5a9baefec7bdb6d70e77cc0273dad75e72546dbbd` |
| split receipt SHA-256 | `49a0eabc675bc2b5839c58d365b01f31ec74901f28b96fecd486ffa0fbdea57a` |
| composition feature receipt SHA-256 | `c6659d934d89a3200b90d11be239ac75ae92a157ce711cee2c24f20a4a0aeb77` |
| structure feature receipt SHA-256 | `f21b78585fcaa2549141dd397b62206e79271edbee1f13cfaabcc223bfa256fd` |
| rows / chemical-system roles | 759 train / 253 internal / 253 locked |
| groups by role | 650 train / 216 internal / 216 locked |
| model fits at freeze | 0 |

## Real result

Every preregistered arm and role was retained:

| Role | Arm | Features | MAE (`cm-1`) | RMSE (`cm-1`) |
|---|---|---:|---:|---:|
| internal validation | composition only | 132 | 93.025 | 208.853 |
| internal validation | permuted structure control | 159 | 98.750 | 218.313 |
| internal validation | aligned structure | 159 | 47.207 | 80.461 |
| locked holdout | composition only | 132 | 82.887 | 148.960 |
| locked holdout | permuted structure control | 159 | 87.530 | 155.783 |
| locked holdout | aligned structure | 159 | 45.666 | 75.060 |

The primary paired comparisons were:

| Role | Control minus aligned MAE | Relative improvement | 95% chemical-system cluster CI | Bootstrap P(gain > 0) |
|---|---:|---:|---:|---:|
| internal validation | 51.543 | 52.20% | [33.393, 73.451] | 1.000 |
| locked holdout | 41.863 | 47.83% | [22.414, 68.321] | 1.000 |

Both roles exceeded the frozen 5% floor and both interval lower bounds were positive, so the exact
mechanical disposition is `robust_aligned_structure_signal`. Physical replay rehashed/reparsed the
source, rebuilt every identity/feature/split, refit all deterministic arms, and reproduced result
SHA-256 `f1384600dfbc8289e6643aae13e6dbb16b0b429c89e7325bd83c88cd8522bb29`.

## Implementation identities

| Artifact | SHA-256 |
|---|---|
| `structures.py` | `1848019da924b5516426283ad6415900bb021bf70758f554d01f2d1db2a5bf9b` |
| `structure_discrimination.py` | `c9fc4122d6517095dbdbc502b6406471090d54efdc4fbba08ca393751d7d6319` |
| exact-run CLI | `75024c13c6af86a3af61c5bcb0829171689733d3d8b6a1a005463e05cf50b4c2` |
| frozen protocol YAML | `2487055028ded3ac648e1895d627495ba57844038843eb50a784beb0f07d68b6` |
| licence evidence | `59e349b7bf7f51a76fcac0295fe39b0d5fe122806fcef9def97309c30533fb0f` |
| gold/adversarial test | `c787d16450529f0417182dcefa3f6692e52aee507e42b695d26cf3a66186f525` |

Runtime versions are Matminer 0.10.1, NumPy 2.4.6, pandas 2.3.3, pymatgen 2026.5.4,
scikit-learn 1.8.0, and spglib 2.7.0. These versions are frozen in the protocol rather than merely
reported after execution.

## Scientific interpretation and limits

The result supports one bounded statement: for the frozen chemical-system-disjoint Matbench phonon
task, correctly aligned structure descriptors add predictive information beyond both composition
alone and an equal-capacity misaligned structure control.

It does not show that the descriptors encode a unique physical mechanism, that an intervention on
one feature would change a phonon spectrum, that performance transfers to experimental samples, or
that the result independently replicates Petretto et al. The public target was not under external
custody, and the two evaluation roles come from one processed dataset. The CC0 lineage record is
source-based rather than independent legal review.

## Remaining work

- build F10-S5's digest-pinned simulation adapter with convergence and failure validation;
- calibrate on reference systems before treating simulated outcomes as observations;
- design a structure intervention that distinguishes explicit F9 hypotheses;
- reproduce the signal on an independently sourced dataset or independent implementation;
- add uncertainty/calibration strata and structural-distribution diagnostics; and
- promote only after independent validator/reviewer and capability-registry evidence requirements
  are met.
