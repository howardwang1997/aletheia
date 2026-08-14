# F7 issue 9 baseline matrix implementation report

Date: 2026-08-14

## Outcome

F7 issue 9 is engineering-complete. Aletheia can now preregister, execute, reconcile, and aggregate
the fixed direct-model, generic-agent, Aletheia-no-K2, and Aletheia-full-K2 matrix on the independent
evaluator plane.

This is not a scientific pass. The acceptance suite has not been unlocked, real four-arm model
calls have not been spent, and the synthetic contract tests are not evidence that K2 improves
science. Actual system/model identities, operator runtimes, and budgets must be frozen before any
public diagnostic or private prospective run.

## Delivered

### Typed preregistration

`aletheia/evals/baselines.py` adds:

- four canonical `BaselineArmId` values and semantic treatment validation;
- content-addressed arm identities for system, base model, prompt, tool, budget, and wall-time
  policy;
- mandatory same-base-model identity and distinct system manifests;
- exact disclosures for any tool, budget, or wall-time mismatch, with conditional-only
  interpretation;
- validation/test phase separation, a frozen validation parent for test, at least three validation
  repeats and five test repeats per task;
- one paired task/repeat/seed block shared by all arms;
- preregistered endpoint, secondary metrics, bootstrap seed/method, missing/invalid handling,
  no-best-of-N selection policy, and Holm correction;
- deterministic within-block arm order derived from frozen identities and a randomization seed;
- four deterministic `EvaluationRunPlan` objects bound to the matrix.

Labels are not decorative. For example, `aletheia_full_k2` is rejected unless the scaffold is
Aletheia and both campaign learning and K2 are enabled. A base-model mismatch is rejected rather
than disclosed because it would destroy the intended system-level attribution.

### Paired execution

`BaselineMatrixRunner` requires one formal `IndependentEvaluationRunner` per arm. All arms must
share the same evaluator manifest, evaluator root, and hash-chained ledger. It executes every
preregistered cell in deterministic blocked order.

Only infrastructure failures trigger an automatic retry, up to the frozen allowance. Every failed
attempt remains in the result and ledger, the retry preserves task/repeat/seed, and an evaluator
`retry_authorized` event is required. Scientific negatives, invalids, timeouts, process errors, and
generic scorer failures cannot buy another sample.

If the evaluator process stops between cells, a new matrix runner reconstructs the completed
schedule prefix from terminal ledger states and evaluator-owned manifests/submissions/receipts, then
continues at the first unstarted cell. It never reruns a terminal cell. A residual nonterminal
attempt fails closed for evaluator adjudication instead of being silently retried.

The operator CLI supports:

- `materialize`: validate and emit the four plans plus exact schedule;
- `run`: obtain evaluator-owned runners from an explicit `MODULE:CALLABLE` factory and execute all
  cells;
- `aggregate`: verify ledger and signed receipts and write the statistical artifact.

Frozen outputs refuse overwrite. Scorer keys are passed by base64 environment-variable reference,
not serialized into manifests or exposed in command arguments.

### No-best-of-N and receipt audit

`aletheia/evals/statistics.py` fails closed unless:

- matrix, suite, derived plans, and schedule hashes agree;
- the current ledger is byte/head identical to the ledger sealed in the result;
- every ledger-created attempt appears exactly once in the result and no undeclared attempt is
  present;
- all arm/task/repeat cells are complete;
- retry lineage is contiguous, infrastructure-only, authorized, and within allowance;
- terminal attempt states and execution/scorer receipt hashes agree with ledger events;
- every scorer HMAC verifies with an evaluator-owned trusted key;
- system, suite, task, repeat, seed, submission, execution, scorer, and evaluator identities agree.

This closes the result-side omission attack: serializing only a preferred attempt does not produce
a report even if the retained receipt itself is valid.

### Statistics and failure decomposition

Every arm reports:

- operational pass@1 over all planned cells;
- scientific success rate over valid scientific verdicts only;
- final-cell and all-attempt status counts;
- execution exit reasons, invalid reasons, unscored invalids, and scored-but-nonfinal adjudications;
- every objective metric with observed/missing count, total, mean, median, quartiles, and range;
- observed USD, input/output tokens, wall time, intervention count, and contamination declarations;
- infrastructure retry count and all retry cost observations.

Each full-K2 comparison reports scientific and operational paired risk differences, win/loss/tie
counts, a deterministic hierarchical task/repeat bootstrap interval, an exact paired sign-test
p-value, Holm-adjusted p-value across the three primary comparisons, paired secondary-objective
effects, and paired cost difference. Invalid pairs are excluded from the scientific estimand and
reported, while counting as non-passes in the separate operational estimand.

Any preregistered budget/tool/wall-time mismatch survives into the comparison and forces
`unconditional_claim_allowed=false`.

## Adversarial verification

The 13 new focused tests cover:

- wrong arm semantics, model drift, noncanonical or missing arms;
- three/five-repeat phase requirements and test-to-validation binding;
- undeclared and correctly disclosed comparability mismatch;
- deterministic paired schedules and identical run-plan slots;
- complete four-arm execution and paired aggregation;
- crash-safe reconstruction of a completed schedule prefix without rerunning it, and fail-closed
  handling of a residual nonterminal attempt;
- retained automatic infrastructure retry;
- invalid-versus-scientific-failure separation;
- mismatch propagation into claim scope;
- omitted ledger attempt and forged HMAC rejection;
- ledger mutation after result sealing;
- CLI materialization and signed aggregate serialization round trip.

Final verification after formatting:

- focused baseline matrix: **13 passed**;
- all non-Docker evaluator tests: **137 passed, 22 deselected**;
- complete non-Docker project: **635 passed, 1 skipped, 29 deselected**;
- complete real Docker group: **29 passed**. An additional final repetition encountered a Colima
  stale-state container that had already printed `ALETHEIA_JOB_OK` and had no process; stopping that
  one temporary container let the suite return 29/29, and the exact affected authored-training test
  then passed cleanly in isolation (**1 passed**);
- Ruff check, Ruff format check, Python compilation, and `git diff --check`: passed.

## Interpretation and limitations

The result models make a trustworthy comparison possible; they do not create the comparison data.
Formal live agents still require evaluator-owned, metered provider executors or immutable candidate
runtimes that implement each frozen treatment. Opening arbitrary network access inside the research
container is not an acceptable substitute.

Public adapter matrices remain diagnostic because model-weight contamination cannot be excluded.
Issue 10 has since established the private prospective custody, access, cleanup, retirement, and
contamination contract; see
[`F7_ISSUE_10_PRIVATE_SUITE_IMPLEMENTATION_REPORT_2026_08_14.md`](F7_ISSUE_10_PRIVATE_SUITE_IMPLEMENTATION_REPORT_2026_08_14.md).
Issue 11 must calibrate numerical thresholds on validation, freeze them before private-test access,
and issue the final receipt-linked Frontier Gate report.

The next implementation issue is now **F7 issue 11: report and acceptance configuration**.
