# F7-S2 Independent Runner Implementation Report

Date: 2026-08-13

## Outcome

F7-S2 is engineering-complete: Aletheia now has an evaluator-owned runner that consumes a frozen
attempt plan, executes one isolated research attempt, validates its submission, runs a trusted
scorer over hidden data, issues a signed receipt, and records the full lineage in an append-only
hash-chained ledger.

This is an engineering result, not a Frontier Gate scientific pass. Public benchmark adapters,
private tasks, repeated-run statistics, baselines, and the release report are later F7 stages.

## Delivered

### Frozen plans and manifests

- `EvaluationRunPlan` pre-registers suite/system/evaluator identity, all task repeats and seeds, and
  the maximum infrastructure retries.
- Repeat indices are contiguous from zero; per-task seeds are unique; plans obey hidden-test access
  limits and cannot be replaced for the same suite/system/evaluator identity.
- Each attempt freezes a public-only request and content-addressed attempt/executor manifest.

### Independent hard runner

- `DockerEvaluationExecutor` applies exact wall-clock, CPU, memory, tool, GPU, token, and USD
  contracts on the hardened no-network container boundary.
- Formal mode rejects the development host-process executor; unmetered token/USD budgets fail
  before execution.
- Research sees only a read-only public request, writable workspace, and submission inbox. The
  evaluator workspace and hidden root are disjoint and unmounted.
- `docker/evaluator-agent.Dockerfile` deliberately does not copy repository/evaluator code.

### Append-only audit and retry semantics

- Evaluator JSONL events carry sequence, previous hash, content hash, timestamp, file lock, flush,
  and `fsync` durability. Slot consumption is atomic across concurrent processes.
- All attempt states, failures, submissions, receipts, and retries remain visible.
- Only evaluator-classified executor infrastructure failure or explicit scorer-infrastructure
  failure is retryable; retry preserves the exact repeat and seed.
- Scientific negatives, timeout, resource exhaustion, authored process failure, protocol failure,
  and generic scorer bugs are terminal.

### Submission and receipt integrity

- Requirements enforce kind, media type, byte cap, declared size, and SHA-256.
- `inbox://` paths reject traversal, symlinks, aliases, missing, and undeclared artifacts.
- Research and inbox directories are sealed before the hidden scorer runs.
- Execution receipts bind attempt/executor/image/budget, timing, exit reason, bounded output, and
  trusted token/cost observations.
- Signed scorer receipts bind plan, attempt manifest, task, system, submission, execution receipt,
  scorer, and evaluator identities under an evaluator-only HMAC key.

## Verification

Focused F7 tests cover success and scientific negatives, limits, plan/access policy, path traversal,
symlink and forged-hash attacks, score forgery, receipt replay/tampering, atomic concurrent slot
claims, hash-chain corruption, and infra-only retry. Exact final test counts are recorded in the
closing validation output for this implementation turn.

Final verification:

- F7-S2 focused non-Docker suite: 41 passed, 2 Docker tests deselected.
- Real Docker isolation suite: 2 passed, including hidden/evaluator/host-secret/network denial and
  exact timeout-container cleanup.
- Full project non-Docker regression: 538 passed, 1 skipped, 8 Docker tests deselected.
- Ruff and `git diff --check`: passed.
- Dedicated research-plane image built locally as `aletheia-evaluator-agent:latest`, resolved at
  runtime to immutable image ID
  `sha256:0859dd009c92f9bffa0c5dc1ee04ea54b6418c6919d1d302a7553b34e22e9af1`.

## Known next work

- F7-S3: ScienceAgentBench (issue 6) is complete; CORE/Asta reproduction and DiscoveryWorld remain.
- F7-S4: encrypted private task custody and retirement workflow.
- F7-S5/S6: frozen baselines, repeated statistics, contamination reporting, and Frontier Gate report.
- Operations: external secret manager/key rotation, evaluator service account, WORM retention, and
  dedicated metered agent/GPU executors.
