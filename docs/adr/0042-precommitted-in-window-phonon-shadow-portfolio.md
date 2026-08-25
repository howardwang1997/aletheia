# ADR 0042: Precommit the in-window phonon shadow portfolio

Date: 2026-08-18

Status: accepted

## Context

The real F11-S7 gate requires a portfolio epoch, but creating that epoch before the clock would not
be in-window evidence, and creating the candidate slate after seeing reproduction results would
permit hindsight leakage. A valid human comparison also requires the human baseline to exist before
the deterministic planner result. The generic F11-S5 store already enforces a shadow-only epoch;
the production Quest still needs a content-bound work order that fixes its candidates, evidence,
source manifests, and operation order.

The production source is exploration-only Matbench data. Candidate external corpora have not passed
lineage and target audits, so a portfolio adapter must not turn their mere existence into data
readiness or allocation authority.

## Decision

Add a content-addressed `PhononEndurancePortfolioWorkOrder` with four precommitted alternatives:
same-source implementation replication, mechanism ablation, activation of the commissioned
mechanism Campaign, and qualification of an independent corpus. Freeze deterministic assessment
inputs and derive a common discrete information model from the union of every hypothesis prediction
outcome. The external-corpus alternative requires `external_validation`, which is intentionally
absent and therefore produces a durable hard-filter blocker.

Use four ordered phases:

1. after the final code commit, prepare the work order without database or model output;
2. before gate start, stage exactly one `portfolio-plan` memory fact/context and one shadow slate;
3. require a `human:*` principal to commit an observation-blind baseline while no epoch exists; and
4. after explicit gate start but before any graph transition, materialize exactly one PostgreSQL-
   timed shadow epoch.

The production wrapper re-verifies committed code, controller, replay protocol, commissioning
manifest, repository-contained paths, and initial graph at every phase. Receipts omit first-call
flags so exact retries are byte-stable. Evaluation carries no action-enqueue or graph-transition
path, and the gate start remains outside this module.

## Consequences

- Candidate and assessment construction cannot adapt to the later reproduction result.
- A human baseline cannot be fabricated by a planner/controller principal.
- The planner epoch is causally inside the endurance window while the baseline remains causally
  before it.
- Missing external-validation custody is visible as scientific infeasibility, not silently treated
  as available data.
- Staging is a production one-shot boundary: interim work orders must not pollute the Quest's exact
  `portfolio-plan` memory context.
- The epoch supports audit and calibration only; it grants no autonomous allocation, outward
  action, Campaign transition, or scientific claim.

## Rejected alternatives

### Evaluate before gate start

Rejected because the epoch would not be real endurance evidence.

### Construct candidates after observing reproduction

Rejected because the portfolio could encode hindsight and make a negative-result pivot appear
preplanned.

### Let the system choose the human baseline

Rejected because that would erase the observation-blind human/planner comparison.

### Mark candidate corpora ready from URLs and metadata

Rejected because source discovery is not an independent lineage, target, license, leakage, and
measurement audit.

### Automatically execute the selected shadow batch

Rejected because the F11-S5 contract intentionally has no production activation authority.
