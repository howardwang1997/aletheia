# F10-S2 typed observation pipeline implementation report

Date: 2026-08-15
Status: Core engineering and real materials reexecution complete

## Outcome

Aletheia now retains raw executor output, typed parser projection, independent domain validation,
generic measurement checks, and final admission state as distinct content-addressed objects. A
valid negative result remains evidence; invalid measurement and execution/parser/validator failures
cannot be confused with it.

## Implemented boundary

- `aletheia/capabilities/observations.py`
  - explicit measurement, exact-reexecution, and parser-fixture purposes;
  - successful/failed/timeout/partial/cancelled run states;
  - write-once raw artifact archive with physical rehash;
  - typed quantity, UCUM unit, uncertainty, method, conditions, sample count, and raw lineage;
  - candidate observation and retained parser failure.
- `aletheia/capabilities/validators.py`
  - frozen per-quantity unit/condition/sample policy;
  - harness-derived generic checks;
  - separately bound domain-validator report;
  - validated positive/negative/inconclusive versus rejected/blocked states;
  - exploratory/confirmatory F9 admission flags with anti-double-counting; and
  - content-addressed commit and physical replay.
- Materials adapters independently parse and reparse the complete K3 result, bind the exact
  preregistration, check the frozen outcome rule and minimum systems, and compare the typed delta,
  bootstrap interval, method, dataset/model/partition conditions, and raw lineage.

## Append-only capability update

Provisional manifest v2.1.0 keeps the v2 schemas but replaces the old raw-result parser/validator
bindings with the typed pipeline adapters. It exactly supersedes v2.0.0 and is frozen in the
three-version registry snapshot.

| Object | SHA-256 |
|---|---|
| manifest v2.1.0 | `33af6218c330569ca8800093d2bb8bae3db2b77d1407e94a2b6beaa7bba93d4b` |
| registry v3 | `9d685a16cee9ea5a56abc0d7343f40e8a4a5307e6baf1a644cd06c2e9a004ae5` |
| typed demo plan | `bdadc78c4bede3aaa6a52948878884ca6c9da4096c092a91999e0eee9c1f1f8c` |
| physical result | `7bd2ecc9d4e3bd6ddb9845b1927726246e28080a63d4a95b45d23ad4686c5851` |
| raw artifact | `73d3fe219546fd1ffe15309c5d2d39be59c59b07a39a3b73c01a523c39ed9606` |
| pipeline | `8eb67d1b3719220b185afb4c59f2926c53065f539c18e514a5fb2c9a47fbe368` |
| committed pipeline | `83d3ebb67e3ce36efc6e9725241d0171ece77816d10ad46b002048f635df9cda` |

## Real typed reexecution

Before reexecution, the plan bound the known slot-03 generic-shrinkage result and explicitly set
purpose `negative_result_preservation`, run purpose `exact_reexecution`, and
`new_scientific_evidence_admission_forbidden=true`. The executor then rebuilt the real Matbench
result exactly.

The parser emitted delta 0.0351793 (unit `1`) with interval [-0.0333846, 0.1104403], sample count
from the smaller chemical-system arm, and dataset/model/partition conditions. The independent
validator reparsed the raw result and passed schema, protocol, outcome, sample, and candidate
projection checks. Final state was `validated_negative`.

Both F9 admission flags are false. This proves a valid negative survives the pipeline while the
same known run cannot be double-counted. A second `verify --recompute` pass reloaded raw and ledger
objects and rebuilt the model result exactly.

## Verification

- General typed observation and materials adapter tests: `12 passed`.
- Capability registry, observation, and materials focused suite: `23 passed` before the real demo.
- Adversarial cases cover bad unit, missing quantified uncertainty, missing condition, failed
  control, failed execution, failure-dropping parser, parser/validator exceptions, raw corruption,
  derived-result tampering, malicious metric projection, provisional confirmatory blocking, and
  reexecution double-counting.
- Targeted Ruff and formatting checks: pass.

## Remaining work

F10-S2 does not make the materials capability registered. The generic admission object is not yet a
direct adapter to the existing F9-S6 confirmation campaign, semantic unit conversion is not
implemented, and real material formula/structure/sample/batch identity is still missing. The next
engineering slice is F10-S3 materials identity and measurement audit.
