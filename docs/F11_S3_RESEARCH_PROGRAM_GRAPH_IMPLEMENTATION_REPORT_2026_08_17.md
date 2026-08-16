# F11-S3 Research program graph implementation report

Date: 2026-08-17

## Outcome

F11-S3 is engineering-complete: Aletheia now has a transactionally committed,
database-constrained, reconstructible Quest → ResearchProgram → Campaign → Experiment hierarchy.
Scientific dependency edges are distinct from the durable task DAG; scientific-family identity
survives Campaign/Run restarts; budget and data-role allocations are Quest/Program scoped; and the
API/dashboard are controllers and read projections over the ledger.

This does not make Aletheia an autonomous frontier scientist. F11-S4 through F11-S7 and F12 remain.

## Implemented contracts

### Hierarchy and immutable identity

`aletheia.programs.schemas` adds frozen Quest, Program, ScientificFamily, Campaign, transition,
dependency, binding, allocation, receipt, and snapshot models. IDs are deterministic from stable
scope identities. Node specs include the human/knowledge/stopping boundaries needed to interpret
the object after model-context loss.

`research_graph_nodes` provides typed foreign-key hierarchy. Insert triggers require Quest roots,
Program→Quest, Campaign→Program, and same-Quest ancestry. Updates/deletes cannot change identity or
spec.

### Transactional lifecycle and reconstruction

Graph creation/mutation extends the F11-S2 command/outbox boundary with
`research_graph.mutation`. Because a Quest exists before a Run, commands can be portfolio scoped
with `run_id = NULL`; all other command, hash, aggregate, result, principal, event, and receipt
requirements remain.

Every node creation and transition writes its domain rows and keyed event in one transaction.
Transition rows are append-only, versions are contiguous, and lifecycle edges are enforced in both
Python and PostgreSQL. `ProgramGraphStore.get_quest` reconstructs from the transition ledger,
revalidates every command/event receipt, compares the current projection, validates the entire
graph, and returns a deterministic `graph_sha256`.

### Scientific dependencies

Dependencies are same-Quest/same-type Program or Campaign edges, frozen before first activation.
The store locks the Quest and checks the prospective DAG. The database trigger takes a Quest-scoped
advisory transaction lock and recursively rejects cycles. Concurrent opposite-edge acceptance has
an explicit test.

### Cross-Campaign family disclosure

Families are Program-owned immutable identities with scientific scope and multiplicity policy.
Campaigns bind exactly one. `HypothesisAttempt.research_family_id` is automatically derived from a
bound Run, and changed family/key input fails. `list_scientific_family_attempts` aggregates across
Campaign/Run boundaries, closing the “new Campaign resets attempts” loophole.

### Budget and data roles

Budget allocations use integer microunit authority. Program caps require a same-kind Quest parent;
sibling Program caps cannot exceed it. `BudgetEvent` can bind an allocation, validates its Run
scope/kind, and rejects cap overflow before insert.

Data-role allocations reference existing DataAsset identities and require the asset Run to be in a
descendant Campaign. External-validation/adaptive roles cannot be relabeled across the evidence
boundary; exclusive allocations cannot be shared.

### Existing scientific objects

Typed tables bind:

- Program ↔ immutable F9 ResearchQuestion version;
- Campaign ↔ legacy Run; and
- Campaign ↔ Experiment, only after the Experiment's Run is bound to that Campaign.

Reconstruction verifies this closure rather than copying payloads into the graph.

### API and UI boundary

Authenticated `/research-graph` endpoints expose reads and all accepted mutation types. The API
replaces caller provenance with `api:{authenticated_user_id}`. Existing method-aware access permits
viewer reads and blocks viewer mutation.

The Next.js “Scientific program ledger” panel fetches reconstructed snapshots and displays counts,
lineage/state, and graph hash. Its refresh control only re-fetches the server projection. Next.js
production compilation/type checking passes.

## Schema

Alembic `20260817_0013` adds:

- `research_graph_nodes`
- `research_graph_transitions`
- `research_scientific_families`
- `research_campaign_families`
- `research_graph_dependencies`
- `research_program_questions`
- `research_campaign_runs`
- `research_campaign_experiments`
- `research_data_role_allocations`
- `research_budget_allocations`

It also adds nullable bindings from `hypothesis_attempts` to scientific family and from
`budget_events` to budget allocation, and permits portfolio-scoped `scientific_commands.run_id`.
Alembic `20260817_0014` freezes each allocated legacy DataAsset's Run, source role, source/ref,
optional content hash, canonical projection, and projection SHA-256 so later registry mutation is
detected during graph rebuild.
Alembic `20260817_0015` makes attempt family identity immutable/inherited at the database boundary
and makes BudgetEvent rows append-only while independently enforcing allocation scope/kind/cap.

Append-only triggers protect transition, family, dependency, binding, and allocation rows. Node
triggers protect shape, immutable spec, monotonic state version, and allowed lifecycle edges.

## Acceptance coverage

The focused F11-S3 tests cover:

1. Alembic/ORM equality and required database triggers;
2. exact hierarchy creation and deterministic repeated rebuild;
3. full Quest/Program/Campaign lifecycle and parent/child readiness;
4. dependency readiness;
5. concurrent opposite edges committing at most one edge;
6. exact command replay and changed-content identity rejection;
7. database rejection of node/transition mutation;
8. one family across two Campaigns/Runs/Experiments;
9. family-key reset rejection;
10. F9 ResearchQuestion, Run, and Experiment graph binding;
11. data-role isolation;
12. Quest→Program budget allocation and spend cap;
13. API mutation/replay/rebuild behavior; and
14. viewer read/controller permission separation.

## Validation

Closeout values are recorded after the final regression run:

- expanded program + queue/outbox/migration/budget/F9 integration matrix: 80 passed in 122.88 s;
- frontend `npm run build`: passed;
- Alembic head/current: `20260817_0015`;
- ORM/Alembic schema differences: 0;
- Ruff on changed Python: passed;
- full non-Docker regression: 1266 passed, 1 skipped, 29 deselected in 765.25 s;
- warnings: 2611 existing spglib deprecation warnings only; and
- `git diff --check`: passed.

## Files

- `aletheia/programs/{schemas,state,persistence,graph}.py`
- `aletheia/api/programs.py`
- `aletheia/jobs/{outbox,persistence}.py`
- `aletheia/memory/{ledger,service}.py`
- `aletheia/schema_migrations.py`
- `migrations/versions/20260817_0013_f11_research_program_graph.py`
- `migrations/versions/20260817_0014_f11_data_allocation_identity.py`
- `migrations/versions/20260817_0015_f11_family_budget_binding_guards.py`
- `scripts/research_graph.py`
- `frontend/components/ProgramGraph.tsx`
- `frontend/{app/page.tsx,app/globals.css,lib/api.ts}`
- `tests/programs/{test_graph,test_api}.py`
- `docs/programs/RESEARCH_PROGRAM_GRAPH.md`
- `docs/adr/0035-f11-reconstructible-scientific-program-graph.md`

## Honest boundary and next work

F11-S3 means strategic hierarchy, family disclosure, allocations, and current state no longer
depend on one model context or browser session. It does not yet compact memory with omission-proof
receipts, choose research portfolios, survive the full random fault matrix, or demonstrate a 72-hour
unattended run.

The next slice is F11-S4: artifact-backed memory compaction whose receipts enumerate covered source
artifacts and cannot omit contradictions, limitations, blockers, or failed hypotheses. Rebuild must
remain possible from authoritative ledgers when any summary/provider/model changes.
