# ADR 0003: Evaluator-owned runner, append-only ledger, and infra-only retry

- Status: Accepted
- Date: 2026-08-13
- Scope: F7-S2 independent runner

## Decision

Formal Frontier Gate attempts run through an evaluator-owned process. The research system receives
one read-only public request, one writable ephemeral workspace, and one writable submission inbox.
It never receives a mount containing the evaluator runner, scorer implementation, hidden asset,
ledger, signing key, or score receipt. The dedicated research-plane image is built from
`docker/evaluator-agent.Dockerfile`; it intentionally does not copy the Aletheia package.

Before execution, the evaluator freezes a content-addressed run plan and attempt manifest. A run
plan declares every task, repeat index, seed, system manifest, evaluator manifest, and permitted
infrastructure retry count. Repeat indices are contiguous, seeds are unique per task, and the plan
cannot exceed the task's hidden-test access limit. Once a `(suite, system, evaluator)` identity has
registered a plan, it cannot register a second plan to expand best-of-N.

Every state transition is appended to an evaluator-private JSONL ledger. Records are sequenced,
hash chained, locked during append, flushed, and `fsync`ed. Slot consumption is one atomic locked
operation, so concurrent callers cannot launch the same planned slot twice. Existing states are
never updated or deleted by the runner API.

The hard executor must enforce immutable image identity; no network; no inherited host environment;
a read-only container root; only public request, research workspace, and inbox mounts; dropped
capabilities; no privilege escalation; PID/file limits; and the frozen resource/tool contract.
Token or USD caps fail closed unless a trusted executor/provider receipt can measure them. GPU
access also fails closed unless a dedicated GPU executor exists.

Submission artifacts use `inbox://` URIs. The evaluator rejects path traversal, symlinks, aliases,
missing or undeclared kinds, media-type mismatch, size mismatch, and content-hash mismatch. It
seals retained research and inbox trees before scoring.

Only trusted infrastructure classifications may authorize retry: executor setup/daemon failures
classified by the evaluator, or explicit `EvaluationScorerInfrastructureError` from evaluator
infrastructure. Authored process errors, timeouts, resource exhaustion, protocol errors, scientific
negatives, generic scorer bugs, and forged retry requests are terminal. A retry preserves the exact
task, repeat, seed, system manifest, and original failed attempt in the ledger.

The evaluator produces a scorer receipt binding plan, attempt manifest, task, system, submission,
execution receipt, scorer, and evaluator hashes. It wraps the receipt in an HMAC-SHA256 envelope
using an evaluator-only key. A research-authored `score.json` has no authority.

## Consequences

F7-S2 now supplies the executable trust boundary and audit trail required by later public/private
benchmark adapters. It does not yet provide benchmark adapters, statistical aggregation, external
key management, remote evaluator deployment, or a GPU sandbox. HMAC authenticity depends on
keeping the evaluator key outside the research plane; formal deployment should source it from a
secret manager and isolate evaluator storage under a separate OS/service account.
