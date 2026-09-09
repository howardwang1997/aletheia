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
10. Label public analysis as a retrospective development diagnostic; distinct local keys authenticate
    artifacts and process separation but do not establish external custody.

## Rejected alternatives

### Treat the exploratory aggregate as confirmation

An outcome used to select thresholds cannot independently confirm those thresholds.

### Call distinct local HMAC keys independent replication

Rejected. They authenticate exact bytes and role separation, but one operator controls both keys and
the public dataset.
