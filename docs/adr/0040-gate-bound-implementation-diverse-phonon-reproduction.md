# ADR 0040: Gate-bound implementation-diverse phonon reproduction

- Status: Accepted for execution; production protocol/result pending
- Date: 2026-08-18

## Context

The retained F10 Matbench-phonons result reports a large aligned-structure advantage over a
capacity-matched within-role permutation. Re-running its verifier establishes deterministic
replay, not implementation independence. The production F11 Quest needs a distinct Campaign
reproduction receipt, while the available dataset remains the same public computed corpus.

There are two opposite risks:

1. changing data, split, features, estimator, and threshold together would make disagreement
   uninterpretable; and
2. calling the same repository function twice would turn code replay into a false reproduction.

The run also must not inspect a new production outcome before the real endurance window or relabel
same-source evidence as independent external validation.

## Decision

### Freeze invariants and vary implementation plus estimator

The reproduction binds the exact dataset bytes, target vector, chemical-system split, composition
matrix, species-blind geometry matrix, and acceptance threshold from the pre-fit F10 plan. A new
module independently reconstructs both matrices from public Matminer/Pymatgen APIs and requires
exact hash parity.

The estimator family changes from RandomForest to ExtraTrees. Its tree count, depth, leaf floor,
feature fraction, random seed, single-thread setting, permutation seed, cluster-bootstrap seed, and
confidence/relative-improvement floors are frozen before fitting. Aligned and permuted arms have
identical feature dimension and estimator budgets; composition-only is retained as the third arm.

### Bind execution to the real gate

Production preparation requires tracked, clean components and proves that the frozen Git commit
contains their exact bytes. The protocol binds the commissioned Quest, real gate/controller, and
distinct original/reproduction Campaign IDs. Static preflight requires the original Campaign to be
active, the reproduction Campaign to remain planned, no gate start, exact code/source files, and
zero model fits.

Model execution fails unless the bound gate is running and both Campaigns are active. The result's
completion timestamp comes from PostgreSQL after computation. A second graph/gate check closes the
race with Campaign stop or gate finalization.

### Retain all outcomes without automatic narrative repair

The frozen two-role confidence/relative-improvement policy mechanically yields confirmed,
contradicted, or inconclusive. Before evidence submission, the complete computation is physically
replayed. The outcome is registered respectively as result, non-droppable negative result, or
non-droppable limitation memory, then a typed reproduction receipt enters the restart-safe
controller spool.

A contradiction does not automatically claim a structural pivot. A later pivot must name the
negative fact, actual source/successor transitions, and an independently assessed change to at
least two strategy dimensions including predictions or discriminated pairs.

### Preserve the claim ceiling

This is same-source implementation-diverse reproduction only. Even a confirmation cannot establish
independent external replication, generalization to another calculation workflow, mechanism, or
causality. Those require the separately qualified external-corpus Campaign and F12.

## Consequences

- Matrix parity isolates implementation/estimator sensitivity better than an unconstrained rerun.
- Distinct code and estimator avoid calling deterministic self-replay an independent result.
- The production outcome remains unknown until after explicit gate start.
- Contradiction and inconclusiveness remain first-class evidence rather than retraining triggers.
- Same-source evidence can satisfy the bounded F11 reproduction receipt but cannot close F12.

## Rejected alternatives

### Use the F10 physical verifier as the reproduction

Rejected because it calls the same implementation and is evidence of reproducibility, not an
implementation-diverse scientific check.

### Download an external target before freezing the protocol

Rejected because lineage, target extraction, overlap, and target-blind material matching are not
yet qualified. Early inspection would contaminate the external Campaign.

### Fit the production ExtraTrees model during preparation

Rejected because the production result must be genuinely in-window and unknown at zero-fit
preflight. Synthetic tests exercise the contracts without opening the real outcome.

### Automatically pivot on any non-confirmation

Rejected because a structural pivot is a causal scientific event, not a status label. It requires
an exact negative result, changed discriminating strategy, and real graph transitions.
