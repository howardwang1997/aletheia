# F11-S4 Receipt-backed scientific memory implementation report

Date: 2026-08-17

## Outcome

F11-S4 is engineering-complete. Aletheia now has an authoritative, append-only scientific-memory
ledger that survives process, provider, model, and prompt-context loss without treating a vector
index or model-written summary as the source of truth. Immutable facts carry graph scope, typed
scientific meaning, source hashes, and explicit task bindings. Compaction creates a
content-addressed derived artifact with complete per-fact coverage receipts; it never deletes the
source ledger.

Negative results, contradictions, limitations, failed hypotheses, safety boundaries, and any fact
with a `required` task binding are selected mechanically and copied exactly into both the artifact
and task prompt. A summary producer cannot omit or downgrade them. Rebuild canonicalizes row order,
rehashes facts/commands/events/artifacts, validates one linear compaction chain, and refuses stale,
incomplete, corrupt, oversized, or consumer-mismatched delivery.

This closes F11-S4, not F11 or the autonomous-frontier-scientist goal. It proves memory custody,
coverage, exact preservation, deterministic recovery, and task-scoped delivery. It does not prove
that a narrative summary is semantically faithful, a source assertion is true, a scientific claim
is valid, or a portfolio action is valuable. F11-S5 through F11-S7 and F12 remain.

## Related-work basis

The design uses related work as motivation, not as borrowed evaluation evidence:

- [MemGPT](https://arxiv.org/abs/2310.08560) motivates moving durable state outside a model's
  bounded context;
- [Generative Agents](https://arxiv.org/abs/2304.03442) combines an experience stream with derived
  reflection and retrieval;
- [Reflexion](https://arxiv.org/abs/2303.11366) motivates retaining feedback from failed trials;
- [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) supports minimizing irrelevant
  long-context material rather than assuming that presence guarantees use; and
- [PostgreSQL CREATE TRIGGER](https://www.postgresql.org/docs/current/sql-createtrigger.html)
  defines the row/deferred constraint-trigger semantics used for database integrity guards.

Aletheia adds a narrower scientific contract: complete source membership, exact unfavorable-state
retention, graph/task authorization, provider-neutral delivery receipts, and deterministic replay.
The repository has not reproduced the evaluations in the cited papers.

## Authoritative data model

F11-S4 deliberately leaves `MemoryChunk` and vector recall as best-effort discovery aids. The new
source of truth consists of five PostgreSQL ledgers:

1. `research_memory_facts` stores a frozen typed fact, detail, source references, identity/hash,
   graph scope, creator, and exact scientific-command receipt;
2. `research_memory_task_bindings` explicitly allows `task_key` or `*` delivery and marks the fact
   `required` or `supporting`;
3. `research_memory_compactions` binds a scope/task source manifest to one content-addressed
   artifact and producer metadata;
4. `research_memory_compaction_members` records every fact hash, kind, and mechanical coverage
   disposition; and
5. `research_memory_context_receipts` freezes the exact prompt text/hash, source IDs, consumer
   provider/model, budget, command, and event delivered to a worker.

IDs are content-derived. Facts include source kind, stable source ID, SHA-256, and optional URI.
This proves what reference was frozen; it does not independently prove that an external URI remains
available or that its contents are scientifically correct.

## Task scope and anti-leakage boundary

Eligibility is an exact relational projection, not a similarity threshold. A Campaign task may
receive matching Campaign, parent Program, and Quest facts. It cannot receive facts from a sibling
Campaign or an unrelated task. `*` is explicit inheritance within that ancestry, not global search.

Task binding has two orthogonal effects:

- eligibility decides whether the fact may enter the task projection; and
- `required` decides whether an otherwise summarizable fact must also appear verbatim.

The five protected fact kinds are always `exact_non_droppable`; `required` facts are
`exact_required`; other eligible facts are `summary`. The artifact retains all members regardless
of disposition, and the original fact rows are never compacted away.

## Transactional compaction and receipts

`ResearchMemoryStore.register_fact`, `compact`, and `build_task_context` use the F11-S2 scientific
command/outbox transaction. Domain rows, canonical result receipt, and keyed event commit together.
Exact redelivery returns the first result; changed request content under a reused idempotency or
source-event identity fails closed.

For compaction, the producer submits narrative text and its claimed `covered_fact_ids`. The store:

1. resolves and locks the Quest scope;
2. reconstructs the current eligible fact set;
3. requires exact equality with the claimed coverage set;
4. computes all member dispositions itself;
5. copies exact facts from verified ledger rows;
6. writes canonical JSON to a content-addressed archive;
7. inserts the compaction and every member under one applying command; and
8. commits only if the current source set and expected parent are unchanged.

New facts make the preceding compaction stale. A successor must cover the complete current set and
retain the previous set. PostgreSQL partial unique indexes admit one root and one successor for each
parent; a Quest row lock serializes writers. Concurrent source registration versus compaction is
detected before commit. A losing transaction can leave an unreferenced immutable archive object,
but it cannot leave a ledger/event reference to incomplete state.

Even when the source set is unchanged, a newer reviewed compaction supersedes the previous leaf for
delivery. Old artifacts remain recoverable for audit, but explicit old-compaction context creation
and previously issued old-leaf context receipts become stale. This prevents a supporting narrative
from being rolled back while preserving the append-only history.

## Deterministic recovery

`rebuild_memory(scope_node_id, task_key)` does not trust stored projection hashes. It independently
checks:

- graph ancestry and Quest ownership;
- fact identity, source references, hashes, bindings, commands, results, and keyed events;
- complete member coverage, dispositions, source manifest, counts, and parent no-forgetting;
- one root-to-leaf compaction chain;
- archive byte count and SHA-256;
- parsed artifact schema, identity, members, narrative, exact facts, and producer metadata; and
- the deterministic final memory snapshot hash.

Rows and nested members are sorted by stable identity before hashing. The acceptance suite shuffles
input snapshots repeatedly and obtains one hash. Missing/corrupt archive bytes and direct database
mutation are rejected rather than silently falling back to a provider transcript.

## Provider-neutral task delivery

`build_task_context` requires a current complete compaction, renders its narrative and exact fact
section, and stores the entire prompt. Its `context_sha256` excludes consumer provider/model, so the
same scientific state produces the same context under an Anthropic/OpenAI switch. The switch still
creates a distinct immutable delivery receipt naming the new consumer.

`run_worker(..., memory_context_receipt_id=...)` reloads and revalidates the receipt, current
compaction, archive, source set, provider alias, and exact model before prepending the context to the
user prompt. It does not call the old provider or import ambient conversation. If the rendered
context exceeds the reviewed character budget, construction fails; exact evidence is never
truncated.

Scheduler/domain stages must explicitly register typed facts and request/pass a fresh receipt.
Legacy stages without a receipt continue to run without authoritative-memory injection; the worker
does not invent an implicit vector-recall fallback.

## Database enforcement

Three forward-only Alembic revisions extend the single migration head:

1. `20260817_0016` adds the five memory tables, foreign keys, unique constraints, checks, and base
   indexes;
2. `20260817_0017` adds append-only guards, applying-command insertion checks, protected
   disposition validation, context rebinding/budget checks, and a deferred completeness trigger;
3. `20260817_0018` adds partial unique indexes for a single root and a single child per compaction
   parent.

All five ledgers reject update/delete. Child inserts must belong to their exact applying scientific
command. The deferred trigger checks member and exact counts at transaction commit. Python recovery
remains the end-to-end canonical JSON/hash verifier rather than attempting to duplicate the full
application hashing algorithm in SQL.

## Operator surface

Authenticated `/research-graph/memory` endpoints support fact registration, compaction, rebuild,
artifact recovery, context receipt creation, and context receipt loading. API provenance is derived
from the authenticated principal; viewers are read-only.

`scripts/research_memory.py` provides `show`, `verify`, `artifact`, and `context` commands. Each
rehashes the same authoritative database/archive boundary consumed by the worker. Deployment,
provider switching, and fail-closed incident handling are documented in
`docs/programs/RECEIPT_BACKED_SCIENTIFIC_MEMORY.md`.

## Acceptance coverage

The F11-S4 suite covers:

1. migration/ORM equality and required PostgreSQL triggers;
2. exact preservation of negative result, contradiction, limitation, failed hypothesis, and
   required facts;
3. rejection of partial or extra summary coverage;
4. Quest/Program/Campaign ancestry, sibling-task exclusion, and `*` inheritance;
5. provider/model switching with an unchanged provider-neutral context hash;
6. stale context after a newly registered fact and recovery after complete recompaction;
7. old compaction/context receipt invalidation after a newer same-source leaf;
8. deterministic rebuild under repeated randomized input ordering;
9. missing/corrupt content-addressed artifacts;
10. refusal to truncate an over-budget exact context;
11. exact fact, compaction, and context-command replay;
12. direct fact mutation and late compaction-member insertion rejection;
13. concurrent fact registration versus in-flight compaction;
14. concurrent compactions producing one linear chain;
15. authenticated API end-to-end mutation/rebuild/recovery/context behavior; and
16. worker prompt injection plus consumer provider/model mismatch rejection.

## Validation

Closeout evidence recorded on 2026-08-17:

- final focused memory/program/API/worker/migration/outbox graph matrix: 52 passed in 5.16 s;
- full non-Docker regression: 1280 passed, 1 skipped, 29 deselected in 744.69 s;
- warnings: 2611 existing spglib deprecation warnings only;
- Alembic code head/current database: `20260817_0018` / `20260817_0018`;
- ORM/Alembic schema differences: 0;
- Ruff over all changed Python: passed;
- CLI parser/startup smoke: passed; and
- `git diff --check`: passed.

The frontend was not changed in this slice; the previous F11-S3 production build remains the
relevant UI evidence.

## Files

- `aletheia/programs/{memory_schemas,memory_archive,memory,persistence}.py`
- `aletheia/programs/__init__.py`
- `aletheia/api/programs.py`
- `aletheia/orchestrator/worker.py`
- `aletheia/jobs/outbox.py`
- `aletheia/{paths,schema_migrations}.py`
- `migrations/versions/20260817_0016_f11_receipt_backed_memory.py`
- `migrations/versions/20260817_0017_f11_memory_integrity_guards.py`
- `migrations/versions/20260817_0018_f11_memory_linear_chain.py`
- `scripts/research_memory.py`
- `tests/programs/{test_memory_compaction,test_api}.py`
- `tests/{test_worker,test_schema_migrations}.py`
- `docs/programs/RECEIPT_BACKED_SCIENTIFIC_MEMORY.md`
- `docs/adr/0036-f11-receipt-backed-task-scoped-scientific-memory.md`

## Honest boundary and next work

F11-S4 means a restart or provider switch no longer authorizes forgetting typed negative scientific
state, and a valid receipt proves exactly which source facts were compacted and delivered. It does
not make unstructured producer prose true, identify facts automatically from every legacy/domain
ledger, guarantee an external source remains retrievable, or prove that the model attends to every
delivered token.

F11-S5 is next: a portfolio planner with deterministic hard filters, harness-computed cost/EIG/
replication debt/diversity terms, explicit proposal receipts, and shadow-mode comparison against
human allocation before any autonomous budget authority. It must consume the graph and memory
ledgers without rewriting either.

Subsequent status (2026-08-18): F11-S5's shadow boundary is implemented in
`programs/SHADOW_RESEARCH_PORTFOLIO.md` and ADR 0037. It deliberately does not grant autonomous
budget authority; F11-S6 is next.
