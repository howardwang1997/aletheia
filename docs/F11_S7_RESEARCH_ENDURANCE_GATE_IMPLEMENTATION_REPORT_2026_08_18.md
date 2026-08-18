# F11-S7 research endurance gate implementation report

Date: 2026-08-18

## Outcome

The F11-S7 engineering capability is complete. Aletheia can now freeze one eligible Quest, start a
database-clock endurance window, append a parent-hashed sequence of reconstructed scientific-state
observations, resume after controller replacement, validate typed milestone evidence against the
authoritative graph/memory/fault/portfolio ledgers, and retain a terminal complete portfolio report.

The accelerated acceptance run passes all scientific-shape requirements while correctly reporting:

~~~text
evidence_class = accelerated_engineering
disposition = passed
real_72h_passed = false
eligible_for_f11_scientific_exit_review = false
autonomous_allocation_enabled = false
~~~

No actual 72-hour run has completed. F11 scientific exit therefore remains open.

## Related-work decisions

The design uses primary operational guidance rather than treating a long `sleep` as evidence:

- [Principles of Chaos Engineering](https://principlesofchaos.org/) motivates a measurable steady
  state, real failure events, attempts to disprove continuity, automation, and bounded blast radius.
- [Google SRE canarying releases](https://sre.google/workbook/canarying-releases/) motivates a
  duration representative of long work units and warns that synthetic load covers code better than
  accumulated state.
- [Reliable Data Processing with Minimal Toil](https://sre.google/static/pdf/reliable_data_processing_with_minimal_toil.pdf)
  motivates soak windows that cover worst-case job time, validation, and peak conditions.
- [PostgreSQL date/time functions](https://www.postgresql.org/docs/current/functions-datetime.html)
  distinguishes changing `clock_timestamp()` from transaction-start `now()`.
- [Python clock documentation](https://docs.python.org/3/library/time.html) supports monotonic
  in-process timeout measurement but not a durable cross-restart epoch.

These sources informed the database-owned clock, cadence/gap evidence, and explicit split between
accelerated engineering and real-time scientific passage.

## Frozen contracts

`aletheia/programs/endurance_schemas.py` adds content-addressed contracts for:

- real-time versus accelerated manifests and hard 259,200-second floor;
- strategy fingerprints, reproduction/interruption/pivot/efficiency receipts;
- balanced budget state and independently hashed ledger observations;
- parent-hashed checkpoints and complete Campaign-status portfolio reports;
- terminal reports with derived duration, gaps, counts, disposition, real-time eligibility, and
  self-hash; and
- Quest audits that can never enable autonomous allocation.

A production manifest rejects caller time. A report cannot set `real_72h_passed` unless its class is
real-time, its disposition passes, and timestamp-derived elapsed time is at least 72 hours. Database
constraints repeat this rule.

## Causal pivot boundary

The endurance store does not accept a model-authored `pivoted=true`. A structural-pivot receipt must
resolve:

1. one exact in-window `negative_result` memory fact;
2. a later source-Campaign transition to paused, stopped, or failed;
3. a later distinct successor-Campaign transition to active;
4. an assessor independent from both transition principals; and
5. before/after fingerprints changing at least two dimensions, including the prediction pattern or
   discriminated hypothesis pairs.

The contract test rejects a wording-only/analysis-label change.

## Durable clock and recovery

Alembic revision `20260818_0022` adds:

- `research_endurance_gates`;
- `research_endurance_checkpoints`; and
- `research_endurance_reports`.

All three are append-only and insert-guarded. Their start/checkpoint/finalize mutations commit
through `research_endurance.mutation` with one keyed event. Quest/gate row locks serialize concurrent
starts and tails. Exact idempotent replay returns the existing object; a changed receipt conflicts.

For real-time evidence, start, checkpoint, and final timestamps come from PostgreSQL
`clock_timestamp()`. Both the application and triggers reject an injected production clock. For
accelerated tests, explicit aware timestamps exercise the same chain while the evidence class
prevents escalation of the claim.

## Reconstructed observation and final report

Each checkpoint independently rebuilds and binds:

- Quest spec, graph hash, exact frozen questions, and all Campaign IDs;
- exact in-window negative-result facts and replay-verified portfolio epochs;
- frozen budget/data authority and current balanced spending;
- one-time outward intents/receipts/reconciliation state; and
- six loss, duplicate, ambiguity, and state/event mismatch counters.

Finalization recomputes the current observation, full start/checkpoint/final gap distribution,
milestone counts, efficiency floor, Campaign state/reason table, evidence receipt IDs, budget state,
and complete final portfolio. Missing evidence creates `blocked`; frozen-boundary or zero-invariant
violations create `failed`. Both remain queryable.

## Operator interface

`scripts/run_endurance_gate.py` provides:

- `prepare` for a real or accelerated manifest;
- `start` for the immutable start receipt;
- `checkpoint` for new evidence and the next chain link;
- `finalize` for the terminal report;
- `show` and `list` for full reconstruction; and
- `audit` for latest real-time F11-exit eligibility.

The CLI names the test clock `--accelerated-now`; passing it to a real manifest fails closed.

## Acceptance evidence

The focused suite currently passes:

~~~text
tests/programs/test_endurance_gate.py
6 passed in 2.48s

F11 durable-queue/outbox/graph/memory/portfolio/fault/endurance cross-component suite
65 passed in 10.63s

full non-Docker repository suite
1306 passed, 2 skipped, 29 deselected in 925.41s
~~~

It covers:

- rejection of a sub-72-hour real manifest and forged real-time report;
- rejection of a cosmetic pivot;
- append-only migration/ORM parity, trigger presence, and final trigger-function body bindings;
- checkpoint replay plus resume from a new store/process identity;
- retention of a deliberately incomplete blocked report;
- rejection of production clock injection; and
- a complete accelerated acceptance run with two questions, three Campaign branches, one exact
  negative result, one reproduction, process and provider interruption receipts, one causally
  ordered structural pivot, one replay-verified portfolio epoch, and material efficiency gain.

Changed-file Ruff, Python compilation, `git diff --check`, CLI help/list smoke, Alembic
`current/head = 20260818_0022`, and ORM schema diff `0` also pass. The repository's pre-existing
ESOL tests fetch a public remote CSV on each invocation; the final full run explicitly unset a
stale local `127.0.0.1:7890` proxy after targeted reproduction proved the earlier TLS failures were
transport-only.

## Changed implementation surface

- `aletheia/programs/{endurance,endurance_schemas,persistence,__init__}.py`;
- `aletheia/jobs/outbox.py`;
- `aletheia/schema_migrations.py`;
- `migrations/versions/20260818_0022_f11_research_endurance_gate.py`;
- `scripts/run_endurance_gate.py`;
- `tests/programs/test_endurance_gate.py`;
- `docs/programs/RESEARCH_ENDURANCE_GATE.md`;
- `docs/adr/0039-f11-durable-real-time-research-endurance-gate.md`; and
- roadmap, README, and documentation-index status updates.

## Honest boundary and next work

Engineering completion means the real experiment can start safely; it is not evidence that it has
finished. The remaining F11 scientific exit is to prepare a production manifest and let one frozen
Quest accumulate at least 259,200 database-clock seconds with on-cadence checkpoints and real
scientific/fault/portfolio receipts. Only then can its audit become eligible for F11 review.

F12 still requires reality-linked execution and genuinely independent replication. Neither a real
F11 pass nor F12 automatically grants spending, task, holdout, laboratory, or external-action
authority; activation remains a separate signed and IAM-enforced decision.
