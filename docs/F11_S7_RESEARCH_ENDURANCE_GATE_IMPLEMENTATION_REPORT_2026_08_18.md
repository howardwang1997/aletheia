# F11-S7 research endurance gate implementation report

Date: 2026-08-18

> Historical status note (2026-08-23): this report predates the first actual 72-hour run. That run
> subsequently completed with authoritative disposition `blocked`, not `passed`, because
> `structural_pivots` was `0/1`. The frozen v1 report in `tests/fixtures/legacy/v1/endurance/` is the
> terminal source of truth.

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

At this 2026-08-18 snapshot, no actual 72-hour run had completed. F11 scientific exit therefore
remained open; the 2026-08-23 status note above records the later blocked terminal result.

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

`scripts/run_endurance_controller.py` adds the production operational boundary:

- `prepare` freezes committed component hashes, the gate, principal, polling cadence, and spool;
- `preflight` revalidates code/source identity, competing gates, and empty spool state;
- explicit replay-safe `start` is separate from preparation and preflight;
- supervised run-once `tick` uses a PostgreSQL advisory lock, database clock, durable tail, and
  stable checkpoint command key;
- `submit` creates replay-safe content-addressed evidence envelopes; and
- `status` reports cadence/deadline state without mutating or finalizing the gate.

There is deliberately no controller `finalize` subcommand and no production clock parameter.

`aletheia/programs/endurance_supervisor.py` plus `scripts/run_endurance_supervisor.py` make the
external scheduler a frozen production component instead of an operator convention. The generated
launchd job binds Conda/Python executable hashes, repository/controller/plist/log paths, label,
domain, and cadence. Before explicit gate start it only returns `waiting_for_explicit_start`; after
start it delegates one locked controller tick. Preflight remains blocked until launchd reports the
exact job loaded. Neither the manifest nor CLI contains automatic start/finalization.

`scripts/submit_endurance_fault_evidence.py` closes the in-window fault-ingestion gap. It validates
the complete F11-S6 bundle, requires exact replay of its append-only committed report, derives the
API-process and provider receipts from the observed scenarios, rejects pre-window observations,
and submits one content-addressed controller envelope. The pre-start prerequisite report remains a
qualification only and cannot satisfy either in-window interruption count.

## Acceptance evidence

The focused suite currently passes:

~~~text
tests/programs/test_endurance_gate.py
6 passed in 2.48s

tests/programs/test_endurance_controller.py
4 passed in 2.31s

tests/programs/test_endurance_fault_evidence.py
2 passed in 1.47s

fault/endurance/controller/adapter/phonon-reproduction focused selection
24 passed in 16.34s

tests/programs/test_endurance_supervisor.py
3 passed in 1.87s

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

The controller supplement covers committed-code/spool preflight, explicit start replay,
advisory-lock exclusion, evidence submission replay, evidence-triggered and scheduled checkpoints,
and recovery after a database commit immediately precedes process death/local archival.
The fault adapter additionally covers pre-window and uncommitted-report rejection, deterministic
typed receipt selection, content-addressed retry, and durable checkpoint ingestion.
The supervisor supplement covers exact no-shell Conda arguments, loaded-state start gating,
pre-start no-op cycles, live run-once delegation, and plist drift rejection.

The first production scientific producer adds three synthetic acceptance tests for independently
reconstructed feature-matrix parity, RandomForest/ExtraTrees separation, deterministic replay,
target drift and disposition relabel rejection, Campaign/path integrity, and the committed-code
production boundary. It remains zero-fit on production data until the real gate starts.
The expanded F9/F10/F11 cross-component selection passes `61 passed in 21.45s`.

Changed-file Ruff, Python compilation, `git diff --check`, CLI help/list smoke, Alembic
`current/head = 20260818_0022`, and ORM schema diff `0` also pass. The repository's pre-existing
ESOL tests fetch a public remote CSV on each invocation; the final full run explicitly unset a
stale local `127.0.0.1:7890` proxy after targeted reproduction proved the earlier TLS failures were
transport-only.

## Changed implementation surface

- `aletheia/programs/{endurance,endurance_controller,endurance_schemas,persistence,__init__}.py`;
- `aletheia/jobs/outbox.py`;
- `aletheia/schema_migrations.py`;
- `migrations/versions/20260818_0022_f11_research_endurance_gate.py`;
- `scripts/run_endurance_gate.py`;
- `scripts/run_endurance_controller.py`;
- `aletheia/programs/endurance_fault_evidence.py` and
  `scripts/submit_endurance_fault_evidence.py`;
- `aletheia/programs/endurance_supervisor.py` and
  `scripts/run_endurance_supervisor.py`;
- `aletheia/domains/materials/phonon_reproduction.py` and
  `scripts/run_phonon_reproduction.py`;
- `tests/programs/{test_endurance_gate,test_endurance_controller,test_endurance_fault_evidence}.py`;
- `tests/programs/test_endurance_supervisor.py`;
- `tests/domains/materials/test_phonon_reproduction.py`;
- `docs/programs/RESEARCH_ENDURANCE_GATE.md`;
- `docs/programs/ENDURANCE_LAUNCHD_SUPERVISOR.md`;
- `docs/adr/0039-f11-durable-real-time-research-endurance-gate.md` and
  `docs/adr/0041-frozen-launchd-endurance-supervision.md`; and
- roadmap, README, and documentation-index status updates.

## Honest boundary and next work

Engineering completion means the real experiment can be commissioned safely; it is not evidence
that it has started or finished. The production gate is frozen, and code-bound controller and
same-source reproduction manifests can pass read-only preflight against it; identities must be
regenerated after any bound-component commit. Only after the scientific workers and external
supervisor are deployed should an operator explicitly start one frozen Quest and let it accumulate
at least 259,200 database-clock seconds with on-cadence checkpoints and real scientific/fault/
portfolio receipts. Only then can its audit become eligible for F11 review.

F12 still requires reality-linked execution and genuinely independent replication. Neither a real
F11 pass nor F12 automatically grants spending, task, holdout, laboratory, or external-action
authority; activation remains a separate signed and IAM-enforced decision.
