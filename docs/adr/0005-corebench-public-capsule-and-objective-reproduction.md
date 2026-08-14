# ADR 0005: Public-capsule isolation and objective CORE-Bench reproduction

- Status: Accepted
- Date: 2026-08-14
- Scope: implementation issue 7 / second F7-S3 public adapter

## Decision

Aletheia adapts AstaBench v0.3.1 `CORE-Bench-Hard` using only its public `validation` split,
which maps to the original CORE-Bench `train` split. Development tooling never downloads or
decrypts the benchmark `test` file. The default mini-suite is explicitly preselected as capsules
`6460826` and `0940461`: both contain MIT-licensed code and CC0-1.0 data, run on Python without a
GPU, and have reviewed offline package contracts. Adding a capsule is a source, license, hardware,
and environment review—not an unrestricted command-line option.

The independent runner supports evaluator-owned public task archives. A full task binds archive
reference, SHA-256, compressed/expanded byte counts, regular-file count, and non-overlapping mount
path. The research-facing view excludes the evaluator path. Before each attempt the runner reads
the exact archive from a disjoint evaluator root and safely expands only normalized directories
and regular files into a fresh research workspace. Absolute paths, traversal, duplicate entries,
links, devices, unexpected counts, and expansion beyond the frozen bound fail closed.

The preparation step validates each source capsule and removes `results/`, `REPRODUCING.md`, the
upstream environment directory, metadata debris, and any pre-existing `reproduction_artifacts/`.
It preserves the source code and data licenses, emits a deterministic public archive, and stores a
separate evaluator-only receipt containing the three official reference outcomes. Source capsule
archives are not copied into the bundle.

The submitted artifact is one UTF-8 reproduction program. The harness executes it twice from
fresh sanitized capsules in the same immutable no-network image. Each run must write
`capsule/report.json` and at least one regular file below `capsule/reproduction_artifacts/`.
Candidate and trusted scorer run in separate containers. The candidate never receives annotation
rows, gold outcomes, scorer code, another capsule, host credentials, or network access. The scorer
receives only the candidate report and exact hidden outcome list.

Objective answer comparison preserves the frozen MIT `inspect_evals` semantics: punctuation is
stripped from keys, percentage strings are converted numerically, strings compare case-insensitively
after trailing punctuation, lists compare exactly, and numerical answers use the 95% prediction
interval across the three reference runs. Scientific success additionally requires a non-empty
reproduction artifact tree. Both report and artifact-tree identities must agree across the two
runs; every run receipt is retained and no best run is selected.

## Consequences

- Correct report plus tangible deterministic reproduction output is scientific true.
- Runnable numerical error, missing report, missing reproduction output, and authored process
  failure are scientific false—not retryable infrastructure events.
- Declared/recognized answer overlap, missing submitted program, resource exhaustion, and
  non-reproducible report/artifacts are invalid, so no scientific inference is made.
- The frozen suite is a development/validation diagnostic. Its public answers and probable model
  training overlap prevent it from serving as the final Frontier Gate; private prospective tasks
  remain necessary.
- Scores must be reported as the Aletheia Asta CORE-Bench-Hard validation mini-suite, not an
  official AstaBench or CORE-Bench leaderboard submission, because artifact requirements,
  no-network execution, two-run determinism, and subset aggregation are deliberately stricter.
