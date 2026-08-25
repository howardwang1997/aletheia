# ADR 0035: Reconstructible scientific program graph

Date: 2026-08-17

## Status

Accepted for the F11-S3 engineering boundary. Memory compaction, portfolio selection, broad fault
injection, endurance, and the final reality-linked scientific exit remain open.

## Context

The durable task DAG added in F11-S1 describes engineering delivery. It cannot answer the different
scientific questions “which campaign belongs to this long-term direction?”, “which prior campaign
must scientifically complete first?”, or “how many confirmatory attempts have been spent on this
hypothesis family?” Treating a task, run, campaign, and research program as synonyms would let
workflow retries silently change scientific multiplicity and strategic state.

The legacy `Run` is also too low-level to be the root. A Quest can exist before any run, can own
several research programs, and must carry the human value, safety, and resource boundary. A program
can own multiple questions and campaigns. A campaign can restart its execution container without
becoming a new statistical family.

Three primary specifications informed the narrow design:

- the [W3C PROV primer](https://www.w3.org/TR/prov-primer/) distinguishes entities, activities,
  agents, derivation, and collections, supporting explicit provenance links instead of embedding
  lineage only in prose;
- [PostgreSQL recursive-query and cycle-detection documentation](https://www.postgresql.org/docs/current/queries-with.html)
  documents recursive traversal and cycle semantics for hierarchical/graph data; and
- [Temporal child-workflow documentation](https://docs.temporal.io/child-workflows) treats workflow
  execution histories as independently scoped units, reinforcing the decision not to make a
  workflow tree the scientific ontology.

These are conceptual and implementation references. Aletheia does not claim full PROV conformance,
and it does not use Temporal in this slice.

## Decision

### 1. Scientific hierarchy and task delivery are different graphs

The scientific hierarchy is fixed as:

~~~text
Quest
  └── ResearchProgram
        └── Campaign ── scientific family (many campaigns may share one)
              ├── legacy Run binding
              └── Experiment binding

Durable Task DAG: separate execution/control graph
~~~

`research_graph_nodes` is the typed relational spine for Quest, Program, and Campaign. A Quest is
its own root. Program parent must be a Quest; Campaign parent must be a Program. PostgreSQL foreign
keys and an insert trigger enforce this shape and prevent cross-Quest parenting.

Experiments remain their existing atomic ledger objects. Typed binding tables connect F9
`ResearchQuestion`, legacy `Run`, and `Experiment` rows without copying their payloads or changing
their original identities.

### 2. Node identity and specification are immutable

Each node ID is deterministic from its type, stable caller identity, and parent scope. Its frozen
spec carries the substantive boundary:

- Quest: direction, value boundary, safety boundary, and resource boundary;
- Program: objective, problem domain, and knowledge boundary; and
- Campaign: objective, stopping boundary, and scientific-family reference.

The database rejects node deletion or changes to identity, parent, type, creator, content hash, or
spec JSON. Correcting a scientific boundary therefore requires a new stable object/version in a
future schema, not overwriting history.

### 3. Lifecycle is an append-only transition ledger plus a checked projection

Every node starts in its type-specific state (`draft`, `proposed`, or `planned`). Each state change
is one portfolio-scoped `research_graph.mutation` scientific command, one
`research_graph_transitions` row, one monotonic node projection update, and one keyed event in the
same PostgreSQL transaction.

Transition rows are append-only. A database trigger independently enforces allowed lifecycle edges
and monotonic versions. Reconstruction folds the complete transition sequence from version one,
validates every command/event receipt, and then checks that the materialized node state/version is
exactly the fold result. The projection is an index, not an alternative truth.

Activation requires an active parent and completed scientific prerequisites. Completing a node
requires scientifically completed children. A parent cannot pause or stop while a direct child is
active.

### 4. Scientific dependency edges are frozen before activation and must be acyclic

Dependencies connect Program→Program or Campaign→Campaign inside one Quest. They never point to a
durable task. The store serializes graph mutation on the Quest root and evaluates the prospective
graph before insert. A PostgreSQL trigger additionally takes a Quest-scoped advisory transaction
lock and uses recursive traversal to reject a cycle, including two concurrent opposite edges.

Dependencies cannot be added after the dependent node first becomes active. This prevents later
rewriting of the prerequisites under which a scientific campaign started.

### 5. Scientific family identity outlives Campaign and Run

`research_scientific_families` belongs to a Program and freezes a stable family key, scientific
scope, and multiplicity policy. Every Campaign has exactly one family binding, but several
Campaigns may bind the same family.

When a bound Run registers a `HypothesisAttempt`, the memory service derives the family from the
Campaign binding and persists `research_family_id`. A caller cannot supply a different family/key.
Family-level reads aggregate attempts across every Campaign and Run. Creating another Campaign
therefore cannot reset attempt counts or alpha-spending history.

### 6. Budget and data roles are explicit immutable allocations

Quest and Program scopes may own:

- same-kind integer-microunit budget caps, where Program allocations require a Quest parent and the
  sum of child caps cannot exceed it; and
- explicit dataset roles (`exploration`, `training`, `confirmation`, `external_validation`,
  `replication`, or `safety`).

An allocation references the existing `DataAsset`; it does not duplicate or relabel the asset.
Sealed external-validation assets cannot be allocated to adaptive roles. Exclusive data cannot be
allocated to another scope. Bound budget events verify the Run belongs to the allocation scope and
fail before insert when the cap would be exceeded.

Microunits avoid using floating-point values for allocation authority. Legacy `BudgetEvent.amount`
remains a float for backward compatibility; cap enforcement converts it at the allocation boundary.

### 7. Scientific commands may be portfolio scoped

F11-S2 required every scientific command to name a legacy Run. Quest creation precedes any Run, so
`scientific_commands.run_id` is now nullable. A null value is permitted only by contract to mean a
portfolio-scoped mutation; aggregate type/ID, command identity, content hashes, principal, result,
and keyed event remain mandatory. Existing Run-scoped commands retain their exact semantics.

### 8. API and UI are controllers/projections, never owners

FastAPI routes authenticate a user, replace caller-supplied provenance with the authenticated user
ID, invoke `ProgramGraphStore`, and return its receipt/snapshot. They hold no graph state. The
frontend “Scientific program ledger” panel fetches a fresh reconstructed snapshot; “Rebuild view”
only repeats that read. React state is a display cache and cannot advance a lifecycle or create an
allocation without a server command.

Viewers can read; only owner/operator roles can mutate through the existing method-aware access
policy.

## Reconstruction invariant

`ProgramGraphStore.get_quest` fails closed unless all of the following agree:

1. deterministic node IDs, parent shape, exact frozen specs, and content hashes;
2. contiguous transition versions and legal state edges;
3. committed scientific-command result and keyed-event receipts for every graph object;
4. the node projection and folded transition state;
5. dependency endpoint types, Quest scope, and acyclicity;
6. Campaign-to-family and family-to-Program identity;
7. external binding closure; and
8. allocation scope, policy hashes, parent budget kinds, and aggregate caps.

The returned `graph_sha256` excludes only operational rebuild time. Repeated rebuilds of unchanged
ledger state are byte-equivalent at the canonical JSON boundary.

## Consequences

- A process/context restart can reconstruct strategic program state without an LLM summary.
- Workflow retry and Campaign restart cannot silently reset scientific-family disclosure.
- Quest-scoped locking serializes graph/allocation mutation and may limit write throughput for one
  very large Quest; scientific graph mutation is expected to be low-volume relative to task events.
- The normalized schema has more joins than embedding the hierarchy in one JSON document, but gives
  real foreign keys and typed uniqueness at every existing ledger boundary.
- Current nodes have no general scientific-spec revision mechanism. A later design must preserve
  old nodes and introduce explicit lineage rather than relaxing immutability.
- This slice allocates budgets; it does not choose allocations autonomously. ADR 0037/F11-S5 now
  consumes these ledgers for shadow-only portfolio policy without granting activation authority.

## Rejected alternatives

### Reuse the durable task DAG as the scientific graph

Rejected because task retry/decomposition is an execution concern. Editing a workflow must not
change scientific family identity, campaign prerequisites, or attempt disclosure.

### Treat each new Campaign as a new family

Rejected because it creates a trivial multiplicity reset: restart the Campaign and recover a fresh
attempt count. Family identity is explicitly above Campaign.

### Store only the current node state

Rejected because a mutable status row cannot prove how it was reached and cannot detect projection
tampering after a restart.

### Put the whole graph in one JSON document

Rejected because Run, Experiment, Question, DataAsset, command, and budget identities would lose
database-enforced foreign-key closure and concurrent writers would contend on opaque document
replacement.

### Let the frontend own lifecycle state

Rejected because browser refresh, multiple controllers, or stale optimistic updates would create a
second source of strategic truth.
