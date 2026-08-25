# ADR 0025: Authenticated real-materials K3 evidence chain

- Status: Accepted
- Date: 2026-08-15

## Context

F9-S1 through S9 supplied a complete synthetic competing-hypothesis chain and a frozen hidden-world
ablation, but the scientific exit also requires one real materials problem to traverse alternatives,
discriminating experiment selection, validated observation, and belief update. Existing materials
runs used real data, but did not bind three alternative explanations to pre-observation likelihoods
and separately signed physical validation.

The available `matbench_expt_gap` benchmark is real measured band-gap data, but it is public and has
already been used during exploration. It can support a transparent retrospective model diagnostic;
it cannot be relabeled as a prospective laboratory result or external replication.

## Decision

1. Freeze three explanations: no material compression, unseen-system-specific extrapolation
   compression, and generic random-forest shrinkage.
2. Compare an unseen-system/represented-system controlled experiment with a cheaper random-holdout-
   only candidate. Select mechanically by expected information gain before loading data.
3. Partition by hashed chemical-system identity. Whole unseen systems form one test; held-out rows
   from systems retained in training form the negative control. No target value enters partitioning.
4. Freeze model, split seed, bootstrap seed, outcome thresholds, priors, nominal likelihoods, and two
   sensitivity likelihood families before measurement.
5. Sign the result with a measurement key. A distinct validation key may sign only after rerunning
   featurization, partitioning, fitting, prediction, cluster bootstrap, and exact-result comparison.
6. Permit Bayesian update only from the signed validation. The update never consumes mutable console
   output or unsigned metrics.
7. Define robust contraction as a stable winner and at least 10% effective-hypothesis-count
   contraction in every frozen likelihood scenario.
8. Permit retirement only when a nonwinner is below the retirement floor in every sensitivity
   scenario. Otherwise narrow it. Nominal evidence alone cannot retire an explanation.
9. Withhold mechanism claims: this experiment diagnoses model generalization, not a physical band-gap
   mechanism.
10. Label the evidence `retrospective_internal_confirmation`; distinct local keys authenticate
    artifacts and process separation but do not establish external custody.

## Consequences

- The selected controlled experiment had 0.3804 nats expected information gain, versus 0.00315 for
  the random-holdout-only candidate.
- The first 20260816 run observed an unseen-specific pattern, but its original implementation used
  nominal-only retirement. The immutable artifacts and exact source were retained; the terminal
  revision was superseded rather than overwritten.
- A new implementation and new 20260817 partition were frozen after that audit. It reproduced the
  existence of compression but classified it as generic shrinkage, with only 1.34% worst-case
  effective-count contraction. The real alternatives → experiment → validated update chain is
  complete, but the substantial-contraction scientific gate is not.
- Cross-partition instability becomes a registered-replication target for F10 rather than being
  hidden or resolved by choosing the favorable seed.

## Rejected alternatives

### Treat the exploratory aggregate as confirmation

Rejected because thresholds were chosen after inspecting that result.

### Keep the favorable v1 result and ignore v2

Rejected as best-of-N selection. Both attempts and their opposite classifications are material
scientific evidence.

### Retire H0 from the nominal posterior alone

Rejected after audit. Under the frozen skeptical likelihood, v1 left H0 above the retirement floor.

### Call distinct local HMAC keys independent replication

Rejected. They authenticate exact bytes and role separation, but one operator controls both keys and
the public dataset.
