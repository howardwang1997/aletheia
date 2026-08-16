# F9-S9 K3 hidden-world scientific-exit harness implementation report

Date: 2026-08-15
Status: Engineering slice complete; live F9 scientific exit remains blocked

## Outcome

Aletheia now has an executable, preregistered K3-versus-K2 hidden-world comparison rather than a
prose-only exit criterion. It can run matched headline/K2/K3 treatments through the independent F7
evaluation plane, retain every attempt and authorized retry, verify signed truth-relative scorer
receipts, compute paired repeated-task statistics, qualify held-out thresholds using validation
evidence, and issue PASS/FAIL/BLOCKED decisions.

The checked-in protocol correctly reports BLOCKED. No live model matrix or private prospective
hidden-law suite has been supplied, so this implementation does not claim K3 superiority or F9
scientific completion.

## Delivered

### Evaluator-owned hidden endpoints

`DiscoveryWorldScientificExitMetrics` and
`derive_discoveryworld_scientific_exit_metrics` add:

- terminal normalized multiclass Brier score;
- top-label confidence/correctness inputs for aggregate ECE;
- issued and false mechanism indicators;
- evaluator-derived genuine discriminating trials and rate;
- wrong-explanation elimination speed over the finite law space;
- trials to exact identification; and
- truth-preserving substantive hypothesis-space contraction.

`DiscoveryWorldScorer` hashes the metrics object into the signed score evidence and copies its exact
values to objective scores. The K3 aggregator requires both copies to agree, binds the object to the
first of the exact reproduced action traces, and rejects valid-looking receipts from an older
scorer that lacks the new endpoints.

### Separate three-arm preregistration

`aletheia/evals/k3_hidden_world.py` defines exact semantic treatments for:

1. headline-metric optimization without explicit epistemic state;
2. historical K2 single-proposition Beta belief/EIG; and
3. F9 K3 versioned competing hypotheses with per-hypothesis prediction, multi-hypothesis
   discrimination, and alternative-exclusion gating.

All arms must share base model, public task prompt, tools, budget, wall time, sampling policy,
tasks, repeats, and seeds. Validation requires at least four tasks × three repeats; test requires at
least four tasks × five repeats and a validation-parent identity. Deterministic blocked
randomization prevents arm-order confounding.

### Resumable execution and raw-evidence audit

`K3HiddenWorldMatrixRunner` delegates every cell to a formal `IndependentEvaluationRunner`, supports
physical process-restart recovery, and permits retries only after retained infrastructure failures.
Aggregation reopens the ledger and verifies:

- run-plan registration, schedule, task/repeat/seed/system identities;
- attempt manifests, terminal states, execution receipts, submissions, and retry authorization;
- evaluator/scorer identity and HMAC signature;
- exact ledger/result equality with no omitted or undeclared attempt;
- the required number of reproducible hidden-world traces; and
- exact metric-object/objective-score consistency.

### Frozen statistics and decision

The report includes arm-level validity, task success, Brier distribution, ECE, false-mechanism and
claim-coverage rates, contraction, intervention/contamination counts, and three paired effects.
Task-cluster/repeat hierarchical bootstrap confidence intervals preserve pairing. Exact sign tests
for the two primary comparisons receive a Holm correction. All predeclared cells remain in the
denominator; there is no best-of-N selection.

`K3HiddenWorldThresholdPolicy` freezes F7-aligned calibration and false-discovery mappings plus
proper-score, coverage, contraction, paired-effect, non-inferiority, intervention, and contamination
limits before validation. `freeze_k3_hidden_world_acceptance` refuses post-validation policy freeze
or validation/test treatment drift. The final function reaggregates raw test evidence and requires
the acceptance config to predate test execution.

Public diagnostic evidence can pass every measured criterion but still returns BLOCKED when the
formal config requires a private prospective test. Passing F7 `PrivateCustodyEvidence` must bind the
exact private suite.

### Operator surface

- `scripts/real_k3_hidden_world_e2e.py` supports protocol inspection, materialization, live runner
  factories, signed aggregation, acceptance freeze, and final decision.
- `configs/evals/k3_hidden_world_v1.yaml` freezes the version-1 treatments, analysis, thresholds,
  and hash-checked manifest descriptors while listing unresolved live inputs.
- `docs/benchmarks/K3_HIDDEN_WORLD_SCIENTIFIC_EXIT.md` is the evaluator runbook.
- ADR 0024 records the trust, metric, comparison, and custody decisions.

The roadmap command now works and reports the current state without spending evaluation access:

```bash
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py \
  --suite configs/evals/k3_hidden_world_v1.yaml --repeats 5 --frozen
```

At implementation time it reports protocol hash
`5853e30dd37d1e0fc0b378938ebcbe389fe3f6eb7e6c99742a8ecc41b7861735` and
`scientific_exit_readiness: blocked`.

## Verification

- Focused K3 hidden-world tests: `6 passed`.
- Existing DiscoveryWorld scoring regression: `14 passed`.
- Broader evaluator, epistemics, non-Docker, and Docker regressions: pending final verification.
- Targeted Ruff, formatting, import, and compilation checks: pass at implementation checkpoint.

Focused coverage includes hidden-truth metric gold cases, false mechanism issuance, arm semantic and
comparability drift, minimum task/repeat power, deterministic blocked schedules, full signed
validation and test matrices, validation-qualified freeze, paired effects, calibration and
false-mechanism endpoints, public-suite BLOCKED semantics, diagnostic PASS, measured FAIL, omitted
attempt rejection, chronology failure, post-access threshold rejection, and the checked-in protocol
CLI.

## Explicit non-claims and remaining blockers

This slice proves the evaluation machinery, using synthetic signed-receipt fixtures. It does not
prove any scientific effect. The public four-instance validation bundle has been regenerated
against scorer `b84c5edc7418c5c26d9fbf7a207261ef057122a93d8f12a312756d4cff3eb852`.
Remaining blockers are material:

1. implement and independently review a same-provider/model three-arm runner factory with provider
   snapshot and usage receipts;
2. commission a contamination-reviewed prospective hidden-law/causal suite under F7 private
   custody;
3. fund and run validation, freeze acceptance, then consume one-time held-out test access;
4. independently review the resulting paired effect, calibration, and false-mechanism evidence.

F9-S10 subsequently completed the real-materials alternatives → discriminating experiment →
validated update limb with locally authenticated evidence. Its v2 result did not meet robust
hypothesis-space contraction and remains retrospective, so it does not remove items 1–4 or establish
the full F9 scientific exit.
