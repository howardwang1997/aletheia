# Typed capability observation pipeline

## State model

```text
executor bytes/status
  -> RawExperimentRun + write-once RawExperimentArtifact receipts
  -> ObservationParseResult
       -> CandidateCapabilityObservation
       -> ObservationParsingFailure
  -> CapabilityObservationPipelineResult
       -> validated_positive | validated_negative | validated_inconclusive
       -> rejected_invalid
       -> blocked_execution | blocked_parser | blocked_validator
```

Validity and sign are separate. `validated_negative` is admissible for an exploratory F9 update only
when the run purpose is `measurement`; `exact_reexecution` and `parser_fixture` are never new
evidence. Confirmatory admission also requires a registered capability whose maximum evidence level
is at least `confirmatory_internal`.

## Required typed content

Each successful measurement includes:

- quantity-kind identifier, finite value, and UCUM code;
- explicit uncertainty kind and parameters, or an explicit `not_quantified` reason;
- sample count and raw artifact IDs;
- measurement-method identity; and
- one or more typed quantitative/categorical conditions.

The frozen policy declares allowed UCUM literals per quantity kind, required condition IDs, minimum
sample count, and whether unquantified uncertainty is allowed. The first implementation performs
literal limited-conformance comparison and never converts units implicitly.

## Physical storage and replay

`CapabilityObservationArchive` writes raw artifacts content-addressed under `raw/aa/bb/<sha>.artifact`
with no-follow reads, regular-file/size checks, and SHA-256 revalidation. Parsed and validated
pipeline objects are committed through `ContentAddressedResponseArchive`. Loading a commitment
reloads both the ledger and every raw artifact.

The real materials demonstration is under
`workspaces/evaluator/materials-typed-observation-v1`:

```bash
conda run -n aletheia python scripts/typed_materials_observation_e2e.py verify \
  --plan workspaces/evaluator/materials-typed-observation-v1/plan.json \
  --committed workspaces/evaluator/materials-typed-observation-v1/committed.json \
  --workspace workspaces/evaluator/materials-typed-observation-v1 \
  --recompute
```

The source is the already frozen generic-shrinkage slot-03 result. The demo deliberately
reexecutes it to prove negative preservation; it is not an additional scientific replicate.

## Current boundary

This layer validates computational measurements and preserves their provenance. F10-S3 now provides
a separate material formula/structure/sample/batch identity and measurement-audit layer; see
`MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT.md`. The F10-S2 pipeline itself still does not bind those
objects into its generic candidate schema, perform general semantic UCUM algebra, prove an
experimental uncertainty/calibration model, or turn a provisional result into confirmatory evidence.
