# F11-S5 shadow research portfolio implementation report

Date: 2026-08-18
Status: engineering-complete shadow boundary; production autonomous allocation remains disabled

## Outcome

Aletheia can now freeze a multi-action research slate against one exact Quest graph, current Program
budget availability, and a receipt-backed Quest-level scientific-memory context. A proposal names
typed actions and rationales only. An independent assessment supplies evidence-bound inputs; a
deterministic harness derives hard blockers, EIG, utility, replication quota, diversity, and a
constrained batch. A human commits a plan before any planner result is materialized, after which an
append-only shadow epoch records the comparison.

This implementation does not enqueue an action, reserve/charge budget, transition the scientific
graph, or enable autonomous allocation. The readiness audit can only make a slate history eligible
for human activation review and always returns `autonomous_allocation_enabled=false`.

## Research and design basis

The implementation review examined current primary work on long-horizon scientific agents and
portfolio/observation selection:

- [AI co-scientist](https://arxiv.org/abs/2502.18864) for asynchronous generation/debate/evolution
  and resource allocation across candidate hypotheses;
- [The AI Scientist-v2](https://arxiv.org/abs/2504.08066) for tree-search-based end-to-end research;
- [Kosmos](https://arxiv.org/abs/2511.02824) for long-running agents plus a structured world model;
- an [independent Kosmos evaluation](https://arxiv.org/abs/2511.13825) that includes false-hypothesis
  and null settings; and
- [Robust Submodular Observation Selection](https://jmlr.csail.mit.edu/papers/v9/krause08b.html)
  for diminishing-return/diversity structure under uncertainty.

The local F9-S5 constrained experiment selector supplied the nearest reusable trust pattern:
observation-blind inputs, harness-recomputed EIG, measurement/capability/fresh-data hard gates,
replication debt, and deterministic tie breaks. F11-S5 lifts that pattern from choosing one
experiment to choosing a cross-Campaign/Program batch, while keeping the graph, budget, and memory
ledgers authoritative.

The cited systems demonstrate valuable search/scaling methods. They do not make automated scoring
independent truth, guarantee likelihood calibration, or grant resource authority. This is why the
new boundary is explicitly shadow-only.

## Implemented contracts

### Proposal has no score channel

`PortfolioActionSpec` supports nine typed actions. `PortfolioProposal` contains action/rationale,
exact graph hash, exact memory receipt, proposer principal/provider/model identity, prompt hash, and
generation time. Pydantic forbids extra fields, and a regression test proves that `total_score`
injection fails validation.

Candidate identity excludes free-form rationale but includes substantive action routing; the full
action hash includes rationale. This permits exact action comparison while still detecting changed
explanations.

### Assessment is role-separated and evidence-bound

`PortfolioAssessmentManifest` freezes assessor kind/code/schema and optional model transport. The
assessor principal differs from the proposer; an independent model cannot reuse the proposer model
identity. Candidate assessments bind exact action hashes and provide:

- typed microunit costs and duration;
- risk and optional approval evidence;
- measurement status/evidence;
- required/available capability hashes;
- required data roles and readiness evidence;
- integer-ppm priors/likelihoods;
- independently assessed value terms and evidence;
- replication debt/protocol; and
- correlation/diversity tags.

The assessment output schema hash is frozen into the manifest. Every candidate is covered exactly
once and cannot predate proposal generation.

### Decimal/integer harness owns calculation

`portfolio_harness.py` calculates discrete expected information gain with Decimal precision 60 and
half-even integer micronats. It stores an exact entropy identity:

~~~text
expected posterior entropy + expected information gain = prior entropy
~~~

Randomized tests generate 100 probability models and verify deterministic replay, bounds, and exact
reconciliation; equal likelihoods produce zero EIG.

All utility inputs and weights are integer ppm. The harness, not the proposal, derives cost burden,
duration burden, risk burden, replication-debt reduction ratio, base microscore, marginal diversity
bonus, and stable tie breaks.

### Hard filters precede utility

The harness blocks incompatible lifecycle/dependency state, absent Program budget, cumulative
overspend, absent data roles, missing capabilities, unvalidated experiment measurement, excessive
duration/risk, missing or expired approval, sub-floor EIG, and invalid replication debt/protocol.

Infeasible candidates remain in the epoch with canonical blocker codes and cannot be selected.
Human hard-filter violations are reported rather than silently removed from the human baseline.

### Constrained batch and replication quota

Deterministic greedy selection enforces total, Program, family, target, correlation, and cumulative
budget constraints. Shared allocation tests show two individually affordable 0.6-unit actions
against a 1.0-unit budget produce one selection and an exact 0.6/0.4 projection, with no reservation
write. A required replication is selected first when feasible; an unsatisfiable quota returns
`policy_blocked` and an empty batch.

### Human plan precedes planner materialization

Each slate has at most one `HumanPortfolioPlanSpec`; `planner_output_access` is literally `none`.
The principal cannot be proposer or assessor. PostgreSQL rejects candidates from another slate and
the one-shot race. A concurrent two-plan test commits exactly one plan.

The comparison stores exact/set match, Jaccard ppm, human hard/batch violations, and feasible utility
sums. This provides a controlled software workflow, not proof against out-of-band human anchoring.

## Persistence and transaction boundary

Alembic `20260818_0019` creates:

1. `research_portfolio_slates`;
2. `research_portfolio_candidates`;
3. `research_portfolio_human_plans`;
4. `research_portfolio_epochs`; and
5. `research_portfolio_scores`.

Alembic `20260818_0020` adds append-only triggers, applying-command insert guards, graph/family and
memory bindings, exact command/plan bindings, blinded-plan checks, shadow-only epoch checks, and
deferred candidate/score completeness. Upgrade from `0018`, downgrade back to `0018`, and
re-upgrade to `0020` all complete transactionally; ORM schema diff is zero.

Every workflow write uses `research_portfolio.mutation`. The state rows, immutable command result,
and keyed event commit together. Exact command replay returns the original receipt. A registration
callback explicitly flushes its parent slate before candidate guards resolve the applying command.

Registration locks the Quest and Program budget allocations, then verifies exact current graph,
budget spend, and latest memory context. Evaluation repeats those checks, recomputes the harness
inside the transaction, and writes an epoch whose database and Pydantic contracts both require:

~~~text
shadow_only = true
actions_enqueued = false
~~~

Portfolio command timestamps use PostgreSQL `clock_timestamp()`, matching the scientific command
ledger and graph projection clock. This avoids treating host/database clock skew as a scientific
ordering failure; callers can anchor a human plan to the preceding command receipt's
`committed_at`.

The whole-repository pass also exposed a pre-existing edge case when the database clock is
corrected backwards between graph transactions. Graph transitions now set the projection timestamp
to `greatest(previous updated_at, now())`; the database trigger continues to reject any genuinely
non-monotonic state/version update.

## Reconstruction and staleness

`get_slate` validates spec/graph/budget hashes, every candidate row, scientific command/event
receipts, and the historical memory-context receipt. `get_epoch` additionally validates every score
and rank, re-runs the harness from frozen inputs, and checks the epoch hash.

New graph state, budget spend, or scientific memory prevents a new epoch. Tests cover all three.
After an epoch commits, later graph change does not prevent historical replay because the epoch uses
its frozen graph and budget. Old memory receipts remain audit-readable but are not accepted as fresh
planning context.

## Control surfaces

FastAPI adds authenticated endpoints to register/list/get slates, commit the human plan, evaluate a
shadow epoch, get/replay an epoch, and aggregate shadow readiness. The end-to-end API test confirms
the returned decision is shadow-only and the audit cannot enable autonomous allocation.

`scripts/research_portfolio.py` is a read-only CLI for slate/epoch reconstruction, Quest listing,
and readiness audit.

## Main files

- `aletheia/programs/portfolio_schemas.py`
- `aletheia/programs/portfolio_harness.py`
- `aletheia/programs/portfolio.py`
- `aletheia/programs/persistence.py`
- `aletheia/programs/__init__.py`
- `aletheia/api/programs.py`
- `aletheia/jobs/outbox.py`
- `aletheia/schema_migrations.py`
- `migrations/versions/20260818_0019_f11_shadow_research_portfolio.py`
- `migrations/versions/20260818_0020_f11_portfolio_integrity_guards.py`
- `scripts/research_portfolio.py`
- `tests/programs/test_portfolio.py`
- `tests/test_schema_migrations.py`
- `docs/programs/SHADOW_RESEARCH_PORTFOLIO.md`
- `docs/adr/0037-f11-observation-blind-shadow-research-portfolio.md`

## Validation evidence

At implementation-report freeze:

- Alembic upgrade/downgrade/re-upgrade: passed, current/head `20260818_0020`;
- ORM schema diff: `0`;
- F11-S5 focused suite: `13 passed`;
- Programs/outbox/schema related regression: `55 passed`;
- 100 randomized probability models plus input-order replay: passed;
- Ruff and Python compilation for the new implementation surface: passed; and
- complete non-Docker repository regression: `1293 passed, 2 skipped, 29 deselected` in
  `766.87s`.

The first full pass surfaced the database-clock rollback edge case described above and otherwise
passed 1292 tests. After the monotonic graph projection fix, the complete second pass was clean.

## Honest boundary

F11-S5 proves a strong audit and shadow-decision boundary. It does not prove:

- that proposal actions are novel, important, or exhaustive;
- that priors, likelihoods, value assessments, costs, or durations are calibrated;
- that evidence hashes resolve to independently valid scientific evidence;
- that the human plan is free of out-of-band knowledge;
- that the greedy batch is globally optimal;
- that an approval evidence hash is a signature or IAM authorization;
- that agreement with a human implies scientific superiority; or
- that autonomous budget/task/action authority is safe.

No activation implementation exists. Production allocation remains a future, separately reviewed
decision.

## Next work: F11-S6

F11-S6 should inject failures at every portfolio-relevant transaction boundary:

1. API/worker process kill before and after state/event commits;
2. database reconnect and serialization retry;
3. duplicate messages and stale leases;
4. evaluator timeout and provider unavailability;
5. disk quota/archive failure;
6. model/selector mismatch on resume; and
7. interruption around one-time outward action reconciliation.

Acceptance must demonstrate no lost scientific state, no duplicate budget/action mutation, exact
replay, and explicit blocked/reconciliation states. F11-S7 then runs the frozen 72-hour endurance
gate. Neither step should silently promote the F11-S5 shadow audit into action authority.
