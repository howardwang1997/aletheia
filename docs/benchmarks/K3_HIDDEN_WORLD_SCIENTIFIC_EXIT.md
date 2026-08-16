# K3 hidden-world scientific-exit protocol

This is the evaluator runbook for the F9 K3-versus-K2 ablation. It is an executable protocol, not
evidence that K3 already outperforms K2. The checked-in public DiscoveryWorld suite can validate the
machinery; a formal scientific exit remains blocked until a prospective private suite, independent
custody, a provider-bound same-model runner, and real signed executions exist.

## Why this is a separate matrix

The F7 four-arm matrix calls the historical campaign-learning treatment `full_k2`. F9's K3 denotes
a newer epistemic world model with versioned competing hypotheses. Relabelling K3 as F7 full-K2
would destroy the ablation. `aletheia.evals.k3_hidden_world` therefore freezes three exact arms:

| arm | explicit state | prediction | selection | alternative-exclusion gate |
|---|---|---|---|---|
| `headline_metric` | none | none | public task progress | no |
| `k2_single_hypothesis` | one Beta proposition | one proposition | single-proposition EIG | no |
| `k3_competing_hypotheses` | versioned rival set | per-hypothesis, pre-observation | multi-hypothesis EIG/discrimination | yes |

The matrix validator requires the same base-model manifest, public task prompt, tools, budget,
wall-time policy, sampling policy, tasks, repeats, and paired seeds. Only the declared treatment
prompt and system manifest may differ. A deterministic hash order randomizes the three arms inside
each task/repeat block.

## Evaluator-owned endpoints

`DiscoveryWorldScorer` now issues a content-addressed `scientific_exit_metrics` object after both
trusted runs agree. It derives the object from the hidden governing rule and authoritative action
trace; candidate-authored claims cannot populate these fields.

- `wrong_explanation_elimination_score`: area under the fraction of the three wrong rules excluded
  over four possible pure-substance trials, padding unused opportunities with the last objective
  state. Earlier valid exclusion is better.
- `discriminating_trial_rate`: non-redundant pure trials that strictly shrink the evaluator-owned
  viable set divided by all informative trials; zero experiments score zero.
- `posterior_brier_score`: one-half the four-class quadratic score at the terminal belief state;
  zero is perfect and one is worst.
- `top_label_ece`: fixed ten-bin aggregate gap between reported top confidence and hidden-rule
  correctness.
- `false_mechanism_rate`: wrong issued terminal rules divided by all valid cells.
- `mechanism_claim_coverage`: issued terminal rules divided by valid cells, preventing universal
  abstention from gaming the false-mechanism endpoint.
- `hypothesis_contraction_rate`: fraction of valid cells whose objective viable set shrinks while
  retaining the true rule.

The Brier endpoint follows the original probability-forecast verification score. ECE is retained as
a separately named finite-bin diagnostic, not treated as a sufficient proof of calibration. The
DiscoveryWorld paper motivates complete hypothesis–experiment–conclusion cycles, while the protocol
adds endpoints that distinguish epistemic progress from task completion.

Primary paired comparisons are fixed before access:

1. K3 minus K2 wrong-explanation elimination;
2. K3 minus headline-agent discriminating-trial rate; and
3. K3 minus K2 scientific success as a non-inferiority guard.

Task-cluster/repeat hierarchical bootstrap intervals retain the pairing. The two superiority tests
use exact sign tests and Holm correction. Every preregistered cell remains in the ledger; only
authorized infrastructure failures may produce a retained retry, and there is no best-of-N path.

## Threshold freeze

`K3HiddenWorldThresholdPolicy` must predate the first validation attempt. Version 1 maps:

- F7 `calibration_error` → `top_label_ece`, maximum 0.10; and
- F7 `false_discovery_rate` → `false_mechanism_rate`, maximum 0.05.

It additionally freezes Brier ≤ 0.10, claim coverage and contraction ≥ 0.80, valid-cell fraction ≥
0.80, paired practical effects ≥ 0.05, positive hierarchical confidence bounds, Holm-adjusted
`p ≤ 0.05`, K3-vs-K2 success non-inferiority within 0.05, zero human interventions, and zero
contamination declarations. These are preregistered F9-v1 absolute limits; they are not represented
as empirically calibrated domain-universal constants.

Validation must pass the policy before `freeze_k3_hidden_world_acceptance` can bind the separate
test matrix. Treatment, scorer, evaluator, analysis, and reproduction identities cannot drift.
The acceptance config must predate test execution.

## Commands

Inspect the checked-in protocol and its unresolved live inputs:

```bash
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py \
  --suite configs/evals/k3_hidden_world_v1.yaml \
  --repeats 5 \
  --frozen
```

The current response is deliberately `scientific_exit_readiness: blocked`. It identifies the
missing private prospective suite, passing custody evidence, provider model snapshot receipt,
three-arm runner factory, and funded executions. The public validation bundle has already been
regenerated against the new truth-relative scorer hash.

For an evaluator-owned live deployment, use the explicit subcommands:

```bash
# Validate a matrix and materialize its exact run plans and schedule.
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py materialize \
  --matrix validation.matrix.json \
  --suite-bundle validation.suite.json \
  --output validation.materialization.json

# Execute through a reviewed factory returning exactly three formal runners.
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py run \
  --matrix validation.matrix.json \
  --suite-bundle validation.suite.json \
  --runner-factory evaluator_factory:build_k3_runners \
  --factory-config evaluator.private.json \
  --output validation.result.json

# Re-open the append-only ledger and verify signed receipts before aggregation.
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py aggregate \
  --matrix validation.matrix.json \
  --suite-bundle validation.suite.json \
  --result validation.result.json \
  --ledger evaluator/events.jsonl \
  --receipt-key-env evaluator-key=K3_EVALUATOR_KEY_B64 \
  --output validation.report.json
```

Then use `freeze-acceptance` with the pre-validation threshold policy and the separate test matrix.
After private execution, `decide` reaggregates raw test evidence and optionally consumes F7
`PrivateCustodyEvidence`; it emits one report/decision bundle without trusting an operator-supplied
aggregate.

## PASS, FAIL, and BLOCKED

- `PASS`: every integrity, custody, paired-effect, calibration, false-mechanism, coverage,
  contraction, contamination, and chronology criterion passes.
- `FAIL`: complete valid measurements miss a frozen scientific threshold.
- `BLOCKED`: evidence needed to interpret the claim is absent—for example a public diagnostic suite
  in place of a private prospective suite, missing custody, or insufficient valid endpoint cells.

Malformed plans, changed ledgers, forged signatures, omitted attempts, scorer drift, missing new
metric objects, inconsistent objective copies, and non-reproduced traces raise an integrity error;
they do not become negative scientific observations.

## Current non-claims

The passing test fixture uses synthetic signed receipts to exercise the protocol. The official
DiscoveryWorld source and answers are public and only four deterministic easy instances are
currently prepared. Consequently:

- no live K3, K2, or headline model comparison has run;
- no private contamination-resistant hidden-law result exists;
- no empirical superiority, calibration, or false-mechanism claim is made; and
- the separate F9 requirement for one real materials alternatives → experiment → update chain is
  still open.

## Research basis

- [DiscoveryWorld paper (NeurIPS 2024)](https://papers.nips.cc/paper_files/paper/2024/hash/13836f251823945316ae067350a5c366-Abstract-Datasets_and_Benchmarks_Track.html)
- [Brier, probability-forecast verification (1950)](https://journals.ametsoc.org/doi/10.1175/1520-0493%281950%29078%3C0001%3AVOFEIT%3E2.0.CO%3B2)
- [Guo et al., confidence calibration and ECE (ICML 2017)](https://proceedings.mlr.press/v70/guo17a.html)
- [Center for Open Science TOP Guidelines](https://www.cos.io/initiatives/top-guidelines)
