# ADR 0026: Versioned capability contracts and full-matrix replication

Date: 2026-08-15
Status: Accepted

## Context

F9 produced one authenticated real-materials result, but opening additional seeds one at a time
would permit optional stopping and result selection. F10 also needs an explicit contract separating
planner, executor, observation parser, and validator, with provisional capabilities unable to emit
confirmatory or mechanism evidence.

The first frozen capability manifest exposed a contract defect: its output schema described a
summary containing `result_sha256`, while the bound executor actually returned a complete
`MaterialsExperimentResult`. Replacing that manifest in place would destroy the audit trail.

## Decision

1. Capability manifests are immutable, semantic-versioned objects in append-only registry
   snapshots. Discovery is exact; unknown or ineligible capabilities return `unsupported`.
2. Any input, output, or preregistration schema-content change is breaking and requires a major
   version increment. Adding newly required metadata is also breaking.
3. The defective provisional v1 manifest remains frozen. Provisional v2.0.0 explicitly supersedes
   it and its schemas validate the actual preregistration, complete executor result, and replication
   plan objects.
4. Provisional capabilities are limited to exploratory evidence and cannot support mechanism or
   experimental-causal claims. Registered promotion requires independent validation and review
   evidence that this local run does not possess.
5. All five partition/model/bootstrap seeds are frozen together before measurement. Every slot is
   retained, has at most one measurement attempt, and is physically recomputed twice.
6. A pattern requires at least four matching preregistered outcome labels. There is no early stop,
   best-of-N selection, or efficacy/futility rule.
7. Partitions of the same public dataset are not treated as independent scientific replications.
   The aggregate reports outcome counts and heterogeneity; it deliberately does not multiply the
   five Bayesian updates into a joint posterior.

## Consequences

- The registry can retain and explain contract mistakes without rewriting history.
- A capability query cannot silently cross evidence, metadata, safety, or approval boundaries.
- The completed materials matrix is an auditable capability demonstration, not a registered
  capability or external replication.
- The observed 2 unseen-specific / 2 generic / 1 ambiguous split is classified
  `partition_sensitive`. All five deltas are positive, but only two confidence intervals are
  strictly above zero; direction consistency is not upgraded into outcome stability.
- Promotion remains blocked by an agent-authored validator, missing independent domain review,
  public retrospective data, local single-operator key custody, and absent external replication.

## Rejected alternatives

- Editing the frozen v1 manifest or registry snapshot in place.
- Treating a JSON-schema change under the same schema ID as patch-compatible.
- Continuing to try seeds until four favorable outcomes appear.
- Pooling same-dataset partitions as independent likelihood factors.
- Calling two locally keyed roles external independence.
