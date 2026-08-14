# F7 four-arm baseline and ablation matrix

## What this adds

F7 issue 9 provides one evaluator-owned path for comparing the same frozen base model in four
system treatments:

| Arm ID | Scaffold | Campaign learning | K2 |
|---|---|---:|---:|
| `direct_model` | direct answer or direct code | off | off |
| `generic_agent` | generic coding/research agent | off | off |
| `aletheia_no_k2` | Aletheia | off | off |
| `aletheia_full_k2` | Aletheia | on | on |

The implementation is in [`aletheia/evals/baselines.py`](../../aletheia/evals/baselines.py) and
[`aletheia/evals/statistics.py`](../../aletheia/evals/statistics.py). It reuses the independent F7
runner; it does not give any arm access to hidden assets, evaluator code, or score internals.

This is an evaluation capability, not a scientific result. A real comparison exists only after an
evaluator owner freezes actual system/model/prompt/tool/budget manifests and spends the declared
validation or private-test access.

## Preregistration contract

`BaselineMatrixPlan` is immutable and content addressed. Validation fails unless all of the
following are true:

- all four arm IDs occur once in canonical order and bind distinct system manifests;
- each label agrees with its scaffold, campaign-learning, and K2 switches;
- all arms bind the same base-model manifest—model upgrades cannot masquerade as system gains;
- every arm receives the same task/repeat/seed slots;
- validation has at least three repeats per task and test has at least five;
- a test matrix references the frozen validation matrix from which it was selected;
- the primary endpoint, three full-K2 comparisons, bootstrap method/seed, missing-run policy,
  multiplicity correction, and secondary metrics are frozen before execution;
- any budget, tool-policy, or wall-time mismatch relative to full K2 has an exact disclosure.

A disclosed mismatch is retained in the comparison output and forces
`unconditional_claim_allowed=false`. A base-model mismatch is never accepted because it defeats
the causal purpose of this ablation.

## Execution and audit flow

```text
BaselineMatrixPlan
  -> four content-addressed EvaluationRunPlan objects
  -> deterministic task/repeat blocks, arm order hashed from the frozen randomization seed
  -> IndependentEvaluationRunner for every cell
  -> append-only ledger + execution receipt + signed scorer receipt
  -> ledger/receipt reconciliation
  -> paired aggregate report
```

Infrastructure failures may consume only the preregistered retry allowance. The failed attempt is
retained, the retry keeps the same task/repeat/seed, and the ledger records an explicit
`retry_authorized` event. Scientific failure, timeout, invalid output, or a scorer implementation
bug cannot request another sample.

`BaselineMatrixRunner` requires all four runners to use the same formal evaluator root, evaluator
manifest, and hash-chained ledger. It executes every cell once in the frozen block order and
automatically consumes only authorized infrastructure retries. After an evaluator-process restart,
it reconstructs a terminal schedule prefix from the ledger, attempt manifests, sealed submissions,
and receipts, then continues at the first unstarted cell. A nonterminal residual attempt is not
guessed or rerun; it requires evaluator adjudication and fails closed.

## No-best-of-N audit

Aggregation is fail closed. `aggregate_baseline_matrix` checks:

1. matrix, suite, schedule, and all four derived run plans match their frozen hashes;
2. the ledger has not changed since the result bundle was sealed;
3. every ledger-created attempt for these run plans occurs in the result, and no extra attempt
   occurs there;
4. every cell has one initial attempt and only contiguous, authorized infrastructure retries;
5. every terminal attempt, execution receipt, and signed score agrees with the ledger;
6. every scorer HMAC verifies with an evaluator-owned trusted key;
7. all four arms cover every preregistered task/repeat/seed cell.

The scientific observation for a cell is the last attempt only when every predecessor was an
infrastructure failure. All predecessors remain in failure and cost accounting. There is no API
for selecting the best seed, choosing among scientific outcomes, or silently dropping an invalid
cell.

## Reported statistics

The aggregate JSON keeps scientific validity separate from operational reliability:

- operational `pass_at_1`: scientific successes divided by all preregistered cells;
- scientific success rate: successes divided only by valid scientific verdicts;
- complete final-status and all-attempt status counts;
- invalid reasons, unscored invalids, scored-but-nonfinal adjudications, execution exit reasons,
  timeouts, and exhausted infrastructure failures;
- every objective metric's count, missingness, total, mean, median, quartiles, and range;
- observed USD, input/output tokens, wall time, interventions, and contamination declarations;
- paired full-K2 risk differences against direct, generic, and no-K2 arms;
- deterministic hierarchical task/repeat bootstrap confidence intervals;
- exact paired sign-test p-values with Holm correction across the three primary comparisons;
- paired secondary-objective effects and paired cost differences;
- an explicit no-best-of-N completeness receipt.

Invalid cells are excluded from the *scientific* effect and reported separately. They count as
non-passes in the operational view. This prevents both common distortions: calling a protocol
failure a scientific negative, or hiding system unreliability by conditioning only on valid runs.

## Operator commands

The command-line entry point has three stages:

```bash
# Validate hashes and materialize the exact four run plans and blocked schedule.
conda run -n aletheia python scripts/run_baseline_matrix.py materialize \
  --matrix /evaluator/preregistration/baseline_matrix.v1.json \
  --suite-bundle /evaluator/suites/frontier_suite.v1.json \
  --output /evaluator/runs/matrix_materialization.v1.json

# Execute with evaluator-owned runner construction. The callable receives
# matrix=..., suite=..., tasks=..., config=... and returns one runner per BaselineArmId.
conda run -n aletheia python scripts/run_baseline_matrix.py run \
  --matrix /evaluator/preregistration/baseline_matrix.v1.json \
  --suite-bundle /evaluator/suites/frontier_suite.v1.json \
  --runner-factory evaluator_runtime.baselines:build_runners \
  --factory-config /evaluator/config/runner_factory.json \
  --output /evaluator/runs/baseline_result.v1.json

# Keep verification keys out of argv and JSON. The environment value is base64-encoded key bytes.
export FRONTIER_SCORER_KEY_B64='...'
conda run -n aletheia python scripts/run_baseline_matrix.py aggregate \
  --matrix /evaluator/preregistration/baseline_matrix.v1.json \
  --suite-bundle /evaluator/suites/frontier_suite.v1.json \
  --result /evaluator/runs/baseline_result.v1.json \
  --ledger /evaluator/ledger/events.jsonl \
  --receipt-key-env evaluator-key-v1=FRONTIER_SCORER_KEY_B64 \
  --output /evaluator/runs/baseline_aggregate.v1.json
```

Every output command refuses to replace an existing frozen file. The factory is evaluator-side
operator code: it owns credentials, model clients, executors, scorers, and hidden storage. Those
objects must not be serialized into the matrix or mounted into a research workspace.

## Remaining F7 boundary

Issue 9 completes the engineering path for preregistered baseline execution and analysis. Issue 10
supplies the private-suite custody, access, cleanup, retirement, and contamination contract in
[`PRIVATE_SUITE_CUSTODY.md`](PRIVATE_SUITE_CUSTODY.md). Issue 11 now consumes both through the
validation-calibrated, receipt-linked decision in
[`FRONTIER_GATE_REPORT.md`](FRONTIER_GATE_REPORT.md).

The remaining boundary is operational and scientific: actual public diagnostic and private
prospective matrices still require operator-frozen model/system manifests, independent reference
and approval evidence, commissioned private tasks, and real budget expenditure. The repository's
synthetic contract matrices are not a Frontier Gate result.

No claim that full K2 is superior should be made from the synthetic contract tests or from a
matrix with a disclosed comparability mismatch.
