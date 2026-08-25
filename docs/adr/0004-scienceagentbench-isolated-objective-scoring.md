# ADR 0004: Isolated objective scoring for ScienceAgentBench

- Status: Accepted
- Date: 2026-08-14
- Scope: implementation issue 6 / first F7-S3 public adapter

## Decision

Aletheia freezes the verified ScienceAgentBench annotation, a predeclared licensed task subset,
per-task data/evaluator receipts, reviewed package contracts, and immutable candidate/scorer image
IDs before running an attempt. The default subset is CC-BY-4.0 IDs `16`, `21`, `29`, and `40`; task
IDs retaining original upstream licenses require explicit opt-in and attribution.

Candidate code and official objective evaluation run in different hardened, no-network containers.
The candidate receives only the dataset roots declared by its task. It never receives evaluator
scripts, gold programs, another task's data, evaluator storage, score receipts, or host secrets. The
trusted scorer receives only the exact hashed evaluator script, exact hashed task data, and the
candidate output mounted read-only. Unzipped upstream assets remain in evaluator-owned storage and
are never copied into distributable suite bundles.

Gold programs are never mounted. The upstream CodeBERT code-similarity value is not used as a
scientific verdict: similarity to a reference implementation neither establishes numerical
correctness nor justifies widening the hidden-answer surface. The official per-task objective
evaluator remains authoritative for output correctness.

Every submitted program runs twice under the same frozen seed and environment. Both output identity
and objective score must agree. Aletheia retains every result and never selects a best run.
Scientific success requires a valid program output and objective success rate 1 in both runs.
Numerically wrong or missing output is scientific false; contamination, missing submission,
non-reproducibility, and resource exhaustion are invalid. Only evaluator-owned asset/runtime/scorer
failure is infrastructure failure and retryable under the frozen plan.

Docker exit code 125 is not sufficient evidence of infrastructure failure because a candidate can
return it deliberately. The hard runner uses a host-only Docker CID file to prove whether the
container launched; a launched candidate returning 125 is an authored process error. At a host
deadline the runner also inspects the exact random container name: an actually running container is
a resource timeout, while a stopped container whose Docker client failed to return is evaluator
infrastructure failure.

## Consequences

The adapter has a small, auditable scientific-coding benchmark with explicit licensing, objective
numeric scoring, deterministic reproduction, exact evidence receipts, and a tested answer-secrecy
boundary. It intentionally diverges from the upstream best-of-three/CodeBERT presentation, so its
scores must be reported as the Aletheia frozen mini-suite rather than as official leaderboard
scores.

The official archive is not redistributed or bundled. Operators must independently acquire the
verified archive under upstream terms. Adding another task requires license review, a package
contract, specialized-image support, and adapter tests; it is not a runtime flag-only change.
