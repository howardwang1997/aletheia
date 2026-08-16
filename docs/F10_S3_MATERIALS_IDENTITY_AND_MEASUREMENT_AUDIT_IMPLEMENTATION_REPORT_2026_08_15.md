# F10-S3 materials identity and measurement audit implementation report

Date: 2026-08-15
Status: Core engineering, gold fixtures, and real Matbench capability audit complete

## Outcome

Aletheia now distinguishes normalized formula, normalized crystal structure, synthesis batch,
physical sample, and exact source-record identity. A split can require any combination of these
levels and fails closed on missing identity or cross-split overlap. Measurement audit now separates
failed execution, invalid metadata, exact duplicate provenance, same-sample repeats, incompatible
conditions, and unresolved conflicts before estimating variability.

This closes the F10-S3 engineering slice. It does not make the materials capability registered and
does not turn the existing Matbench table into physical-sample evidence.

## Implemented code boundary

### `aletheia/domains/materials/identity.py`

- content-hashed source bytes with source URI, explicit licence expression/URI, and licence-evidence
  hash;
- versioned pymatgen elemental-ratio formula normalization;
- representation-independent formula and chemical-system identities;
- exact CIF receipt plus versioned conventional-standard-cell structure identity;
- structure parser/quality warnings, order, space group, and volume-per-atom checks;
- explicit synthesis batch and physical sample identities with closed parent lineage;
- material records that declare missing structure/batch/sample levels exactly; and
- replayable multi-level split ledger with missing and overlap witnesses.

### `aletheia/domains/materials/measurements.py`

- property, unit conversion, method, condition, identity, and conflict policy frozen before audit;
- canonical measurement projections using only declared linear UCUM conversions;
- retention/exclusion of failed, partial, invalid, malformed, or metadata-incomplete records;
- exact-provenance duplicate groups and distinct same-sample repeat groups;
- pairwise incompatible-condition findings and conservative compatible strata;
- combined-uncertainty plus absolute same-sample conflict threshold;
- conflict exclusion before pooling; and
- explicit available/unavailable within-sample, between-batch, and between-source variability.

### Dataset capability audit

`aletheia/domains/materials/capabilities/measurement_audit.py` inventories exact tabular content and
identity/measurement field coverage without fuzzy column inference. It reports what the dataset can
support and what interpretations are forbidden. The companion CLI creates immutable evidence and
physically rehashes/recomputes both the base audit and normalized-composition collision report.

## Gold and adversarial evidence

The CC0 gold manifest contains formula-equivalence cases and two NaCl structures: rock-salt
(space group 225) and CsCl type (space group 221). They have the same normalized formula identity
and different normalized structure identities.

Fifteen new tests cover:

- equivalent and malformed formulas;
- same formula/different polymorph;
- source-byte tampering;
- explicit sample-to-batch closure;
- sample/batch cross-split leakage and missing sample identity;
- invalid units, missing conditions, and failed execution;
- duplicate provenance versus legitimate repeat;
- rejection when one provenance identity is projected into different scientific values;
- condition-stratified non-pooling;
- same-sample conflict exclusion;
- empirical three-level variability and explicit insufficient-data status; and
- forged split/audit summary rejection.

The combined materials and capability suite passes `43 passed`; targeted Ruff and formatter checks
pass. The only warnings are upstream spglib deprecation warnings during symmetry analysis.

The authoritative full non-Docker regression, rerun on the host so the suite could access its local
PostgreSQL service, passed `1180 passed, 1 skipped, 29 deselected` in 719.90 seconds. An initial
sandboxed attempt reached 968 passes but was not treated as an acceptance result because localhost
database and network fixtures were denied by the sandbox.

## Real Matbench audit

The audited installed `matbench_expt_gap` gzip has the expected Matminer SHA-256 and 4,604 rows. All
formula strings parse and all targets are finite. Physical replay rehashed the compressed bytes,
renormalized every formula, rebuilt the logical-row hash, and exactly reproduced both evidence
objects.

| Object or count | Value |
|---|---|
| compressed dataset SHA-256 | `783e7d1461eb83b00b2f2942da4b95fda5e58a0d1ae26b581c24cf8a82ca75b2` |
| compressed bytes | 37,200 |
| logical rows SHA-256 | `a2ffffb4c344b9949a65313d73fc92fbf183d8461812f3ada7d079675264fc10` |
| identity audit SHA-256 | `36c301590f5317305d7413a3470ad65bff13025dc34bac21f6057284b02ea6e9` |
| collision report SHA-256 | `7ae6b9d7cff2c54ba6d19f61a707f0df59ee0fd24fc9b473cfb106ac289bd334` |
| rows / normalized formula identities | 4,604 / 4,601 |
| chemical-system identities | 3,705 |
| normalized-composition collision groups | 3 groups / 6 rows |
| disposition | `composition_benchmark_only` |

The nine blockers are absent structure, sample, batch, uncertainty, measurement method, measurement
conditions, and row-source provenance; unit metadata that is not row-bound; and unresolved
dataset-specific licence. The Matminer metadata supplies citations, URL, and exact file hash but no
dataset-specific licence field. The evidence therefore uses `NOASSERTION`; it does not apply the
Matbench software repository's MIT licence to upstream experimental measurements by assumption.

### Unresolved normalized-composition collisions

| Row positions | Raw formula spellings | Values (eV) | Range (eV) |
|---|---|---:|---:|
| 1086, 1120 | `Cu1.8S1`, `Cu9S5` | 2.30, 0.00 | 2.30 |
| 1784, 1828 | `In1.5Cu0.5Se2.5`, `In3CuSe5` | 1.24, 0.60 | 0.64 |
| 3855, 3880 | `Te0.5Se0.5`, `TeSe` | 1.00, 1.58 | 0.58 |

These pairs share elemental ratios after exact reduction. The audit explicitly forbids concluding
that they are ingestion duplicates, polymorphs, repeated measurements, or experimental conflicts:
the source table lacks the identities required to decide among those possibilities. It also does
not change the already frozen F9/F10 evidence or retroactively rescore it.

## Standards and related-work effect

- The IUCr CIF specification motivated retaining exact CIF bytes and a separate parsed identity.
- IGSN practice motivated persistent physical-sample identity distinct from material composition;
  this implementation remains local and does not mint IGSNs.
- NIST traceability guidance motivated the explicit non-claim: source provenance and uncertainty
  are necessary but do not establish an unbroken calibration chain or fitness for purpose.
- UCUM remains the unit syntax; this slice adds only frozen linear conversions, not implicit unit
  inference.

See ADR 0028 for links and the detailed design decision.

## Implementation identities

These hashes identify the implementation and fixtures exercised by this report:

| Artifact | SHA-256 |
|---|---|
| `identity.py` | `7e2d6281f6420d3538460787e7bb80987c3105aa21a053e940dc8a6020c592b7` |
| `measurements.py` | `23c1a8d72ab268d18771ecbe122465afe9471504703a31f5237f4eebe6c8c15b` |
| dataset audit adapter | `f4981185d644798023bc00282e54f34fef0e81b335821d138884fa598949ee0b` |
| audit/replay CLI | `5ad0f8eaae9e6a12927b430da6da2d9d71daaaec98e6b5d612b1a9c61203f084` |
| gold manifest | `fbc0f6589e6e070ca415c4e4d58b7708ceb3f95cd363919322c899b2bd9c694f` |
| rock-salt CIF | `ae82935d63f5df5bde51ab3d0672e0af6b3414759e9f8258c1b4d53a97b4f31d` |
| CsCl-type CIF | `08f9de528da51635a6c4a7860077d6d3899289594e5ae487b785e129f750f162` |

Runtime versions were pymatgen 2026.5.4, spglib 2.7.0, Pydantic 2.13.4, and Matminer 0.10.1.

## Remaining work

- issue/resolve globally persistent sample identifiers and authenticate sample/batch custody;
- calibrate normalized-structure equivalence on a larger external corpus;
- bind instrument/reference/calibration chains and method-specific uncertainty models;
- ingest a structure/sample/batch-rich experimental dataset under resolved rights;
- connect identity-clean measurements to the generic F10-S2 → F9 confirmation adapter; and
- complete F10-S4's preregistered structure-aware experiment without reusing composition-only rows
  as structure evidence.
