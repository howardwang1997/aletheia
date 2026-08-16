# ADR 0028: Material identity and conservative measurement audit

Date: 2026-08-15
Status: Accepted

## Context

A composition string identifies an elemental ratio, not one crystal phase, synthesis batch, or
physical specimen. Experimental property tables often omit those higher-level identities and the
conditions, uncertainty, method, and source needed to distinguish duplicate ingestion from a real
repeat or conflict. Pooling such rows can leak related specimens across evaluation splits and can
turn polymorphism, batch variation, or condition differences into model error.

Relevant standards reinforce these distinctions:

- The [IUCr CIF specification](https://www.iucr.org/resources/cif/spec) defines formal syntax and
  dictionary semantics for interoperable crystallographic data; a CIF therefore remains exact
  source evidence as well as parsed structure content.
- [IGSN guidance](https://ev.igsn.org/about-igsns) describes globally unique persistent identifiers
  for physical samples and links between a sample and derived data. Aletheia follows the identity
  separation but does not claim to issue or validate an IGSN.
- [NIST metrological-traceability guidance](https://www.nist.gov/metrology/metrological-traceability)
  makes traceability a property of a measurement result supported by a documented calibration chain
  in which each link contributes uncertainty. A source citation or calibrated instrument name alone
  is therefore not enough.
- [UCUM](https://ucum.org/ucum) provides the machine-unit notation already adopted in F10-S2.

## Decision

1. Represent chemical system, formula, normalized structure, synthesis batch, sample, and source
   record as separate content-addressed levels.
2. Normalize formulas to a reduced integer elemental ratio under an exact parser-version policy;
   retain the original string but exclude it from representation-independent formula identity.
3. Bind structure identity to exact licensed CIF bytes and a versioned pymatgen conventional-cell
   projection. Retain quality warnings and do not claim global crystallographic canonicality.
4. Require explicit issuer batch/sample IDs and source records. Never derive either from formula or
   structure. A sample must bind one exact batch.
5. Require every material record to declare absent structure/batch/sample levels exactly.
6. Let each split policy select required separation levels, while always requiring record-level
   separation. Missing identity and cross-split overlap fail closed and remain inspectable.
7. Freeze property units/conversions, methods, required conditions, identity levels, and conflict
   thresholds before a measurement audit. Only declared linear conversions are executed.
8. Distinguish failed runs, exact provenance duplicates, same-sample repeats, incompatible
   conditions, and same-sample conflicts. None is silently pooled.
9. Partition eligible measurements into deterministic conservative condition cliques. Exclude both
   sides of an unresolved same-sample conflict from variability calculations.
10. Emit within-sample, between-batch, and between-source estimates only when replicated groups make
    them identifiable; otherwise emit `unavailable` with a reason.
11. Treat normalized-composition collisions in an identity-poor dataset as unresolved. Do not label
    them duplicates or experimental conflicts without structure/sample/batch/source evidence.
12. Bind every source to exact bytes and explicit licence evidence. `NOASSERTION` is a blocker; a
    software repository licence is not silently applied to upstream measurement data.

## Rejected alternatives

- **Formula as material/sample identity.** This collapses polymorphs, batches, and specimens.
- **Raw CIF hash as the only structure identity.** Equivalent serializations would look different,
  while a normalized digest without raw bytes would lose exact provenance.
- **Delete every repeated normalized formula.** A collision may be a polymorph, repeat, or ingestion
  duplicate; the available table may not identify which.
- **Pool then add condition covariates.** It still permits undeclared condition incompatibility and
  changes the estimand after seeing data.
- **Estimate a noise floor from singleton rows.** This manufactures repeatability evidence.
- **Assume repository licence covers source data.** Code and upstream experimental content can have
  different rights.

## Consequences

- The same formula can safely identify different polymorphs, and the same sample cannot cross a
  split that requires sample isolation.
- Failed, invalid, duplicate, conflicting, and condition-incompatible measurements remain visible
  without contaminating pooled estimates.
- Sparse datasets honestly yield unavailable variance components.
- The real Matbench table remains usable for composition prediction but is blocked from
  sample-level measurement, noise, structure-specific, and metrological claims.
- Production IGSN integration, large structure-normalization calibration, laboratory custody,
  calibration traceability, and capability registration remain future work.
