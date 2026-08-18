# F11 production phonon Quest commissioning report

Date: 2026-08-18

## Outcome

The first production-shaped scientific Quest has been commissioned from real retained F10 evidence.
This closes the gap where all eligible F11-S7 prerequisites could be assembled only through test
fixtures. It does not close the real 72-hour gate or F12.

The applied instance is:

~~~text
commissioning  pcm_2bd8b42a47aab1afadb8781b0eec170d
Quest          qst_cd143727c9e8c48fcff45ab6087db3d2
Program        prg_85ceadbe36c9135b9e44af05799a7e41
active branch  cmp_a269864873184e324da89875e1a52c9e
graph SHA-256  41a47946b28c9685468b5946e6b782c7f9979a8c2e9fada6d201a4b2c34286b8
~~~

First application created 31 durable objects. Immediate replay created zero and returned the 31
existing receipts. A separate read-only commissioning audit and the general research-graph CLI
independently reconstructed the same graph hash.

## Real evidence boundary

Preparation loaded the actual ignored workspace files through the frozen F10 schemas and rehashed
their bytes:

| Artifact | Byte SHA-256 |
|---|---|
| `matbench_phonons.json.gz` | `4db551f21ec5f577e6202725f10e34dfc509aa7df3a6bdaac497da7f6dbbb9b3` |
| pre-fit `plan.json` | `ee228bdd8b6830ec4bbe52f1d85a160eef902e191890f4e5ffdc36a029e830f3` |
| retained `result.json` | `cdfdc4a35ca32cd304b9e8f9fbe87eccdb7583dab9a162f69839e3a8731b7675` |

The internal result identity is
`f1384600dfbc8289e6643aae13e6dbb16b0b429c89e7325bd83c88cd8522bb29`.
The evidence supports aligned structural predictive information on one public computed corpus. The
commissioning preserves the source flags that forbid independent-external-replication and
causal/mechanism claims.

## Scientific design

`aletheia/domains/materials/phonon_commissioning.py` freezes three Campaigns under one Program and
scientific family:

1. independent implementation replay;
2. local-packing versus global-lattice/symmetry ablation; and
3. independent calculation-corpus validation.

Only the replay Campaign is active. The other two remain planned so that a negative result can
cause a real strategy transition rather than a wording-only pivot.

Two F9 snapshots pose:

- which of local packing, global lattice/symmetry, or a null artifact explanation accounts for the
  source gain; and
- whether the gain survives same-data independent implementation and a separately calculated
  compatible corpus, versus total failure or source-only reproduction under domain shift.

Each question has exactly one null, one primary, and one alternative hypothesis; every hypothesis
has an assumption, violation consequence, discriminating prediction, protocol hash, and prior.

## Replay-safe persistence

Preparation is database-free and write-once at the CLI boundary. The manifest binds:

- exact evidence files and F10 content identities;
- code hashes for the commissioning, graph, epistemic, and persistence surfaces;
- graph specifications and initial lifecycle;
- deterministic Run and DataAsset identities;
- world-model snapshots;
- data-role policy and external-candidate non-allocation; and
- five kinds of Quest/Program budget caps.

Application checks all bytes and live code before mutation. Runs and DataAssets use PostgreSQL
`INSERT ... ON CONFLICT DO NOTHING` followed by exact content verification. World models use their
existing immutable content-addressed store. Graph mutations use commissioning-derived scientific
command keys. Stable identity with changed content fails; exact crash replay returns existing
receipts.

The audit rejects hidden nodes, dependencies, families, bindings, allocations, budget changes,
state-version round trips, source changes, and world-model changes. It is an exact initial-state
audit, not the evolving research ledger after Campaign work begins.

## Data, budget, and authority

The F10 source is the only allocated asset and has role `exploration`. Its frozen policy allows
physical replay, independent implementation, and mechanism-ablation design, while forbidding:

- independent external replication;
- causal mechanism; and
- experimental validation.

Quest/Program caps cover USD, GPU hours, model tokens, wall-clock hours, and experiment count. The
three Run USD/GPU caps fit within the Program caps. Outward action and autonomous allocation remain
disabled.

## External-source research

Primary official sources were used to distinguish candidates from eligible validation data:

- [Phonondb migration index](https://github.com/atztogo/phonondb): candidate PBEsol/finite-
  displacement calculations, still requiring exact lineage, overlap, and target extraction audit;
- [Alexandria datasets](https://alexandria.icams.rub.de/datasets.html): candidate PBE phonon
  recalculation of MDR materials, still requiring workflow and target harmonisation audit;
- [Materials Project phonon methodology](https://docs.materialsproject.org/methodology/materials-methodology/phonon-dispersion):
  excluded as an independent source because the current Matbench target traces to that legacy
  Materials Project/Petretto family; and
- [Phonix](https://phonix-db.org/): candidate for an anharmonic/thermal-conductivity question, not a
  direct last-harmonic-DOS-peak replication.

No candidate was downloaded or allocated by commissioning.

## Evidence and verification

Local write-once operational evidence is stored under the gitignored
`artifacts/phonon-quest/` directory:

| File | SHA-256 |
|---|---|
| manifest | `7dd478efc9375d32bf97dc12b466ccd118aaf87c974c2a048a7704f157f3fbc5` |
| replay receipt | `1f86faa0ec6d60e0e34e5f1a3d3e8dc2d765208eb1e1071907d34caf84ae933d` |
| initial audit | `2bc419ba9996a8b291a0fdabc33bba53dce003bd409902245c13e9eefc97850d` |

The new focused suite passes:

~~~text
tests/domains/materials/test_phonon_commissioning.py
3 passed in 1.78s

F9/F10/F11 world-model/structure/graph/endurance/data cross-component selection
50 passed in 9.83s
~~~

It covers the closed scientific blueprint, honest external-source/data role boundary, artifact
drift before mutation, initial apply, exact replay, exact graph reconstruction, and read-only audit.
Ruff and Python compilation pass for the changed code and CLI.

## Remaining blockers

The durable apply/audit receipts explicitly retain:

1. `independent_external_dataset_not_yet_qualified_or_registered`;
2. `independent_reproduction_result_not_yet_committed`;
3. `quest_scoped_fault_prerequisite_not_yet_run`; and
4. `restart_safe_real_time_endurance_controller_not_yet_commissioned`.

The next safe step is to commit this code, run the production ten-boundary fault prerequisite for
the commissioned Quest, and implement the restart-safe hourly controller before starting the real
database-clock window. The active scientific branch must also produce an honest reproduction
outcome; it cannot be fabricated from the existing same-code replay.
