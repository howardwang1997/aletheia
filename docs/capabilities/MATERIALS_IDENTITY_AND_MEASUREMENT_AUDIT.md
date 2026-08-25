# Materials identity and measurement audit

## Identity hierarchy

F10-S3 uses six separate identities. None is silently substituted for another.

```text
chemical system
  -> normalized formula
       -> normalized crystal structure (optional)
            -> explicit synthesis batch (optional)
                 -> explicit physical sample (optional)
                      -> exact source record
```

- A formula is normalized to a reduced integer elemental ratio under a versioned pymatgen policy.
  The raw formula remains in the receipt, but equivalent spellings share a formula identity.
- A structure binds exact licensed CIF bytes and a versioned conventional-cell projection. Its
  normalized identity is separate from both the source-byte receipt and formula identity.
- A synthesis batch has an issuer-provided ID and exact synthesis-record source. A physical sample
  has its own issuer-provided ID and must bind one exact batch.
- Missing structure, batch, and sample levels are declared explicitly. A formula can never generate
  a sample or batch ID.

The structure normalizer implements a local, versioned comparison contract. It does not claim that
all crystallographic representations have one universal canonical byte sequence. The CIF parser,
pymatgen version, symmetry tolerances, cell convention, coordinate precision, source hash, warnings,
space group, ordering state, and volume-per-atom check are retained.

## Split ledger

`MaterialSplitPolicy.required_identity_levels` states which levels must be disjoint. Record identity
is always required. For every required level, `build_material_split_ledger` derives:

- missing identities for each row/split;
- every identity occurring in more than one split;
- all record identities associated with each overlap; and
- an exact policy-and-membership SHA-256.

Missing required identity and cross-split overlap both produce
`rejected_identity_leakage`. For example, a formula-level generalization split may intentionally
separate formulas, while a prospective measurement split can require batch and sample separation.
The ledger never treats formula separation as proof of sample separation.

## Measurement audit

`audit_material_measurements` consumes frozen material records and a property policy. The policy
declares the quantity kind, canonical UCUM code, exact linear conversions, required conditions and
compatibility tolerances, allowed methods, required identity levels, pooling level, and conflict
threshold.

The replayable audit performs these steps:

1. retain failed/partial/invalidated runs but exclude them from values;
2. reject unsupported property, quantity, unit, method, uncertainty, condition, or identity;
3. convert only units explicitly listed in the frozen policy;
4. identify exact provenance duplicates and keep one canonical copy;
5. record legitimate same-sample repeats separately from exact duplicates;
6. split incompatible conditions into conservative pairwise-compatible strata;
7. flag incompatible same-sample values beyond the larger of the absolute and combined-uncertainty
   conflict limits, then exclude both values from variability pooling; and
8. estimate within-sample, between-batch, and between-source standard deviation only when the
   corresponding replication exists.

An insufficient estimate is an explicit `unavailable` object with a reason. The median standard
uncertainty is retained as a reported measurement-noise indicator when available; it is not relabelled
as empirical repeatability.

## Gold fixtures

The checked-in CC0 fixtures under `tests/fixtures/materials_identity` include equivalent formula
spellings and two NaCl polymorphs (rock-salt and CsCl type). The tests cover:

- equal formula but unequal structure identity;
- exact source-byte tampering;
- same sample/batch across train and test;
- missing sample identity;
- bad units and missing conditions;
- failed execution;
- exact duplicates and conflicting repeats;
- inconsistent projections claiming the same raw provenance;
- condition-stratified non-pooling;
- available and unavailable three-level variability; and
- attempts to relabel mechanically derived split/audit outcomes.

Run the focused checks with:

```bash
conda run -n aletheia pytest -q tests/domains/materials tests/capabilities
```

## Real Matbench audit

The immutable evidence is under
`workspaces/evaluator/materials-identity-measurement-audit-v1`. To physically rehash the installed
Matminer asset and replay all 4,604 formula/target rows:

```bash
conda run -n aletheia python scripts/materials_identity_measurement_audit.py verify \
  --audit workspaces/evaluator/materials-identity-measurement-audit-v1/audit.json \
  --dataset-file <conda-env>/lib/python3.11/site-packages/matminer/datasets/matbench_expt_gap.json.gz

conda run -n aletheia python scripts/materials_identity_measurement_audit.py verify-collisions \
  --audit workspaces/evaluator/materials-identity-measurement-audit-v1/audit.json \
  --report workspaces/evaluator/materials-identity-measurement-audit-v1/composition-collisions.json \
  --dataset-file <conda-env>/lib/python3.11/site-packages/matminer/datasets/matbench_expt_gap.json.gz
```

The dataset is sufficient for a composition-level predictive benchmark only. It lacks structure,
sample, batch, uncertainty, method, conditions, and row-source provenance. The unit is dataset-level
metadata rather than a row-bound measurement field. Its Matminer metadata gives citations, source
URL, and expected file hash but no dataset-specific licence field; the audit therefore records
`NOASSERTION` instead of assuming that the Matbench code repository's MIT licence covers upstream
experimental measurements.

Formula normalization exposes three unresolved same-composition collisions. Their band-gap ranges
are 2.30 eV (`Cu1.8S1`/`Cu9S5`), 0.64 eV
(`In1.5Cu0.5Se2.5`/`In3CuSe5`), and 0.58 eV (`Te0.5Se0.5`/`TeSe`). Without
structure/sample/batch/source identity, these cannot honestly be classified as duplicate rows,
polymorphs, repeated measurements, or conflicts.

## Current boundary

- No IGSN is minted or resolved; issuer-provided sample IDs are only locally bound.
- No laboratory chain of custody, instrument calibration chain, reference material, operator
  authentication, or metrological-traceability claim is implemented.
- Structure normalization has gold fixtures but no large external equivalence calibration.
- Unit conversion is frozen linear conversion, not a general UCUM algebra engine.
- The Matbench audit is real data-quality evidence, not a new band-gap measurement or scientific
  replication.
- The F10 materials capability remains provisional and cannot create confirmatory F9 evidence.
