# ADR 0027: Typed observation and negative-result boundary

Date: 2026-08-15
Status: Accepted

## Context

Capability executors can return successful measurements, scientific negative results, partial or
failed runs, malformed raw output, or values with incompatible units and conditions. Treating all
finite parsed numbers as observations would turn non-convergence into a physical negative, allow a
parser to discard failures, and let a reexecution be counted again as new evidence.

## Decision

1. Preserve three immutable layers: raw run/artifacts, parser candidate, and validator result. Raw
   artifacts use content-addressed, write-once storage and are physically reloaded before parsing
   and validation.
2. Parser output is untrusted. The harness adds raw lineage and parser identity itself; an exception
   becomes a retained parser-failure terminal state.
3. Every successful candidate requires a measured quantity, machine unit code, uncertainty object,
   measurement method, and at least one typed condition. Missing uncertainty is represented
   explicitly as `not_quantified`, never by omission.
4. Unit comparison initially uses UCUM literal limited conformance. Allowed codes are frozen per
   quantity kind; no implicit conversion occurs without a content-hashed conversion policy.
5. Generic harness checks cover registered quantity kind, unit, quantified uncertainty, interval
   consistency, sample count, required conditions, and raw-artifact lineage. A separately bound
   domain validator adds protocol, identity, controls, and scientific-validity checks.
6. Scientific outcome and validity are orthogonal. A valid negative is
   `validated_negative`; unit/protocol/control failures are `rejected_invalid`; executor, parser,
   and validator failures remain distinct blocked states.
7. Only a validated measurement-purpose run may cross the F9 exploratory admission gate. A
   registered capability with sufficient evidence level is additionally required for confirmatory
   admission. Exact reexecutions and parser fixtures are never admitted as new evidence.
8. The typed materials parser projects the frozen unseen-minus-control compression delta, bootstrap
   interval, dataset/model/partition conditions, and original outcome. Its validator reparses the
   complete raw result independently and compares the projection to the preregistered protocol.

## Standards basis

- [UCUM](https://ucum.org/ucum) defines machine-readable unit expressions and explicitly permits
  literal comparison as limited conformance.
- [QUDT](https://www.qudt.org/doc/2022/08/DOC_SCHEMA-QUDT-v2.1.html) separates quantity kind,
  numeric value, and unit semantics.
- [NIST measurement uncertainty guidance](https://physics.nist.gov/cuu/Uncertainty/) motivates
  retaining uncertainty kind, coverage, bounds/factor, and method.
- [W3C PROV-O](https://www.w3.org/TR/prov-o/) motivates immutable entity/activity/agent lineage for
  raw, parse, and validation layers.

## Consequences

- Failed or non-converged execution cannot masquerade as a negative scientific result.
- A parser cannot silently omit a failed run or invent an artifact outside the raw run.
- A domain validator can reject a syntactically valid but scientifically altered projection.
- The real slot-03 reexecution is a valid negative and reproducible validation artifact, but both
  F9 evidence-admission flags are false because it is an exact reexecution of known public data.
- Semantic unit conversion, physical measurement conditions, and the direct generic-to-F9-S6
  adapter remain later work.
