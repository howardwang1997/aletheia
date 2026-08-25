# ADR 0029: Precommitted matched structure discrimination

Date: 2026-08-15
Status: Accepted

## Context

A lower error from a structure-aware model does not by itself show that crystal structure carries
incremental task information. The result may instead come from a different split, larger model,
different hyperparameter search, unequal feature capacity, duplicated chemical families, or
selective reporting. Conversely, composition alone cannot distinguish polymorphs and should not be
treated as a structure representation.

The selected real task is the public `matbench_phonons` table: 1,265 structures with the frequency
of the last phonon density-of-states peak. Matminer identifies it as a Matbench filtering of the
Petretto et al. density-functional perturbation theory data. The
[Scientific Data descriptor](https://www.nature.com/articles/sdata201865) describes the upstream
DFPT calculations, while the
[parsed-data record](https://springernature.figshare.com/articles/dataset/Parsed_phonon_data/5649298)
declares CC0. This is computed retrospective data, not a new laboratory measurement.

## Decision

1. Bind the exact compressed distribution, licence-evidence record, parser environment, four source
   files that determine features/results, and all protocol parameters before model fitting.
2. Permit one target-blind preflight over structures only. It may select defensible compute/quality
   bounds and confirm that feature extraction is defined, but it may not inspect target performance.
3. Require every row to pass ordered-site, site-count, volume, minimum-distance, lattice-condition,
   and symmetry-standardization checks. Retain a content-addressed row ledger rather than silently
   deleting failures.
4. Use a fixed species-blind structure vector: primitive-standard-cell volume, lattice ratios and
   angles, space group, periodic-neighbour summaries, crystal-system indicators, and radial bins.
   Pair it with the existing 132-dimensional Magpie composition vector.
5. Assign whole chemical systems to 60% train, 20% internal validation, and 20% locked holdout using
   a target-blind seeded algorithm. No chemical system may cross roles.
6. Run exactly three preregistered random-forest arms with no tuning or best-of-N selection:
   composition only; composition plus aligned structure; and composition plus structure rows
   permuted independently within train, internal-validation, and holdout roles.
7. Give the aligned and permuted arms identical 159-dimensional inputs and exactly the same
   estimator/hyperparameter budget. The permutation changes alignment, not row counts or capacity.
8. Compare aligned and permuted absolute errors on the same rows. Bootstrap the paired difference
   by chemical system, not by treating correlated rows as independent.
9. Call the signal robust only if both internal and locked roles improve by at least 5% and both 95%
   chemical-system cluster-bootstrap intervals have a lower bound above zero. Preserve any negative
   or ambiguous result under the same schema.
10. Treat the locked holdout as an untouched role from the same public dataset. It is not an
    independent external replication, prospective blind test, causal intervention, or mechanism
    experiment.

## Rejected alternatives

- **Compare unrelated published model scores.** Different folds, budgets, preprocessing, and search
  procedures do not isolate representation alignment.
- **Use only composition versus structure models.** The structure arm has extra inputs; a matched
  misalignment control is needed to test whether correct row-level alignment matters.
- **Random-row split.** Closely related compositions and repeated chemical systems could cross the
  boundary and inflate apparent generalization.
- **Tune on the locked holdout or retain the best seed.** This converts the final role into model
  selection and hides partition sensitivity.
- **Bootstrap rows independently.** Multiple rows from one chemical system are not an honest set of
  independent scientific units.
- **Describe feature importance as a mechanism.** Coarse geometry descriptors and predictive error
  differences do not identify a physical causal pathway.

## Consequences

- A positive result supports the bounded claim that correctly aligned structure information has
  incremental predictive value for this frozen task, split, representation, and estimator.
- Exact raw bytes, plans, results, and physical recomputation remain auditable; an implementation or
  environment drift fails before execution.
- The design is deliberately stricter than a composition/structure leaderboard comparison but is
  still vulnerable to public-dataset familiarity, single-dataset effects, and representation/model
  specificity.
- F10-S5 must add a calibrated simulation boundary, and F10-S6 must seek an independent dataset or
  implementation plus a genuine intervention before any stronger scientific claim.
