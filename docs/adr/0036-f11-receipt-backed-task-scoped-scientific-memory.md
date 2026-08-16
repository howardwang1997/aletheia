# ADR 0036: Receipt-backed, task-scoped scientific memory

Date: 2026-08-17

## Status

Accepted for the F11-S4 engineering boundary. Autonomous portfolio choice, broad fault injection,
the 72-hour endurance gate, and scientific exit remain open.

## Context

The pre-F11 memory path stores embedded `MemoryChunk` rows for semantic recall. Both indexing and
retrieval deliberately swallow failures because recall is an optimization. That is appropriate for
finding potentially relevant prior work, but it cannot prove that a restarted scientist recovered
all contradictions, limitations, or failed hypotheses. A model-written conversation summary is
also not an authoritative scientific state: it can omit an inconvenient result, change across
providers, or become unavailable with the provider session that produced it.

Related primary work supports hierarchical memory, reflection, and selective context, but does not
by itself provide Aletheia's required scientific custody boundary:

- [MemGPT](https://arxiv.org/abs/2310.08560) treats context management as movement between memory
  tiers, motivating a durable tier outside the model context;
- [Generative Agents](https://arxiv.org/abs/2304.03442) retains an experience stream, synthesizes
  higher-level reflections, and retrieves memories for planning;
- [Reflexion](https://arxiv.org/abs/2303.11366) shows the value of retaining verbal feedback from
  failed trials in episodic memory; and
- [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) demonstrates that merely providing
  a long context does not mean a model robustly uses all relevant information, supporting explicit
  task selection and short exact sections.

The implementation also uses PostgreSQL row and deferred constraint triggers according to the
[PostgreSQL trigger contract](https://www.postgresql.org/docs/current/sql-createtrigger.html). These
references inform the design; Aletheia does not claim to reproduce their evaluations or to make a
model's narrative summary semantically correct by construction.

## Decision

### 1. Vector recall is not authoritative memory

`memory_chunks` remains a best-effort semantic index. F11-S4 adds a separate append-only ledger:

- `research_memory_facts` stores one frozen scientific fact and its content identity;
- `research_memory_task_bindings` states exactly which task may receive it and whether it must be
  rendered verbatim;
- `research_memory_compactions` binds one complete task/scope source set to a content-addressed
  artifact;
- `research_memory_compaction_members` is the per-fact coverage receipt; and
- `research_memory_context_receipts` records the exact provider-neutral prompt context delivered
  to a consumer.

A vector similarity result cannot insert a fact into an authoritative prompt context. Relevance is
an explicit ledger binding, not an embedding threshold.

### 2. Facts are immutable, scoped, provenance-carrying objects

A fact belongs to a Quest, Program, or Campaign and has at least one task binding. Campaign context
may inherit eligible facts from its Program and Quest; it cannot read a sibling Campaign. The
special task key `*` means explicitly global within the ancestry, not globally searchable.

Every fact records one or more source references with a source kind, stable ID, SHA-256, and
optional URI. Its deterministic `mem_<hash>` identity covers kind, statement, detail, task
bindings, and source references. Registration, rows, bindings, result receipt, and keyed event are
one `research_memory.mutation` transaction.

This schema freezes the claimed provenance reference; it does not imply that every external URI is
still reachable. Downstream scientific evidence systems remain responsible for their own physical
artifact verification.

### 3. Coverage is complete and negative scientific state is exact

A producer submits a narrative plus the exact fact IDs it claims to cover. The harness accepts the
draft only when that set equals every currently eligible fact under the target scope/task.

Coverage disposition is mechanical:

| Fact/binding | Artifact and prompt treatment |
| --- | --- |
| `negative_result` | exact, non-droppable |
| `contradiction` | exact, non-droppable |
| `limitation` | exact, non-droppable |
| `failed_hypothesis` | exact, non-droppable |
| `safety_boundary` | exact, non-droppable |
| any `required` task binding | exact |
| other eligible facts | covered by narrative, retained in the source ledger |

The producer never supplies the exact section. The harness copies it from verified fact rows into
the artifact. If the prompt character budget cannot hold the narrative plus all exact facts, the
context build fails; it never truncates a protected fact.

The coverage receipt proves set membership and exact preservation. It cannot prove that a free-form
narrative faithfully expresses every supporting fact. Exact facts and the original ledger remain
authoritative when narrative and structured state disagree.

### 4. A compaction is a write-once artifact, not destructive garbage collection

Compaction writes canonical JSON to the audited content-addressed archive before committing its
database reference. The artifact binds scope, task, parent, complete source manifest, per-fact
dispositions, producer identity, narrative, and mechanically copied exact facts. Reads re-open the
file, check byte count and SHA-256, parse the frozen schema, and compare it with the database rows.

Original facts are never deleted. A new fact makes the prior compaction stale. Recompaction must
cover the complete new set and extend the previous receipt chain. Partial unique indexes enforce
one root and one child per parent; the Quest lock prevents concurrent writers from creating
sibling successors. An archive written before a losing transaction may remain as an unreferenced
content-addressed object, but no ledger row or event points to it.

### 5. Reconstruction is deterministic and fails closed

`rebuild_memory(scope, task)` independently validates:

1. graph ancestry and Quest identity;
2. deterministic fact IDs/hashes and task bindings;
3. scientific-command result and keyed-event receipts;
4. complete compaction members, dispositions, counts, source manifest, and no-forgetting parent
   relation;
5. a single linear compaction chain; and
6. artifact bytes, schema, object identity, exact facts, and producer metadata.

Input rows are canonicalized by identity, so randomized input order reconstructs the same
`memory_sha256`. Operational rebuild time is excluded.

### 6. Prompt context is an explicit task projection

Context assembly reads only facts bound to the requested task or `*` within the requested node's
ancestry. It requires a fresh compaction whose members exactly equal that current set. The output
contains the verified narrative and exact required/non-droppable facts, then stores the exact
rendered text and a provider-neutral `context_sha256`.

Consumer provider/model appear in the delivery receipt, not in `context_sha256`. Rebuilding the
same scientific state for a different provider therefore produces identical scientific context
even though it creates a distinct delivery receipt. The worker accepts a context receipt ID,
rehydrates it from database and artifact, verifies that it targets the active provider/model, and
prepends only that text to the current task.

No ambient conversation, vector neighbor, sibling task, or old provider session is added by this
path.

### 7. PostgreSQL blocks mutation and incomplete receipts

Database triggers reject update/delete on all five memory tables. Insert guards require child rows
to be written while their exact scientific command is in `applying` state. They independently
check scope/Quest identity, task bindings, protected dispositions, artifact/context rebinding, and
prompt character counts. A deferred constraint trigger refuses a compaction whose member or exact
counts are incomplete at transaction commit.

Application rebuild remains the stronger end-to-end check because PostgreSQL does not reproduce
canonical Python JSON hashing inside constraints.

## Consequences

- Process and model-session loss no longer destroys the scientific memory needed for a task.
- Changing provider/model does not require replaying the old provider or trusting its private
  session history.
- Negative results increase exact context size. This is intentional; when they exceed policy, the
  system must redesign the task/context boundary rather than erase evidence.
- Each fact must be explicitly typed and task-bound. Incorrect upstream classification remains a
  risk and should be tested where legacy/domain ledgers emit facts.
- Supporting-fact narrative quality still needs independent evaluation; this slice proves custody
  and coverage, not semantic summarization accuracy.
- Context policy currently budgets Unicode characters, not tokenizer-specific tokens. Provider
  adapters may impose a stricter token gate later without weakening exact preservation.
- The worker boundary is wired, but individual scheduler stages must supply a fresh receipt ID;
  absence of a receipt does not silently invoke authoritative memory.

## Rejected alternatives

### Treat the vector store as the source of truth

Rejected because indexing/retrieval is intentionally best-effort, approximate, and provider/model
dependent. Missing a neighbor cannot be distinguished from “no relevant negative result exists.”

### Let the summarizer choose which failures matter

Rejected because this delegates the anti-confirmation-bias boundary to the same model producing a
persuasive narrative. Protected kinds and required bindings are selected mechanically.

### Delete facts after compaction

Rejected because an unreviewed summary would become irreversible scientific data loss. Compaction
is a derived projection, never garbage collection.

### Include the entire Quest history in every prompt

Rejected because unrelated context wastes budget and long-context position can impair use of
relevant information. Task bindings and graph ancestry define the maximum authorized projection.

### Store only a provider transcript/cache

Rejected because provider sessions, models, and cache keys change. Scientific state must survive a
transport switch and be independently reconstructible.
