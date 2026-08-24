# Research program graph

This guide describes the durable Quest, ResearchProgram, ScientificFamily, Campaign, Experiment,
budget, and data-role boundary established in F11-S3 and consumed by F11-S4 scientific memory and
F11-S5 shadow portfolio planning.

## What this adds

The program graph can now represent and reconstruct:

~~~text
Quest (human direction/value/safety/resources)
  └── Program (problem + knowledge boundary + questions)
        ├── ScientificFamily (multiplicity identity)
        └── Campaign (adaptive sequence)
              ├── Run
              └── Experiment(s)
~~~

Scientific `depends_on` edges form a separate DAG among Programs or among Campaigns. Durable task
dependencies remain in `durable_task_dependencies`; do not translate one graph into the other.

## Deploy

Upgrade before starting API or workers:

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

Expected repository head: `20260825_0024` (`0013` creates the graph, `0014` freezes allocated DataAsset scope,
`0015` guards legacy family/budget writes, `0016`–`0018` add receipt-backed memory, and
`0019`–`0020` add the shadow portfolio ledger and integrity guards). Revisions `0021`–`0022`
add fault/endurance evidence; `0023` adds the separate research-kernel authority store and the
shared Quest identity guard without making this legacy graph authoritative for new Quests. `0024`
adds the separate qualification-only local-execution store and does not change this legacy scope.

Runtime startup continues to fail closed unless the database is exactly at that head. The migration
adds ten graph/allocation tables, two nullable legacy bindings, portfolio-scoped scientific
commands, typed hierarchy/lifecycle triggers, cycle detection, and append-only triggers.

Downgrade is intentionally refused by the existing immutable scientific-command contract if the
database already contains portfolio graph commands. Preserve/restore the ledger with matching code
instead of deleting scientific history merely to fit an older binary.

## Python controller

All mutations need frozen content plus an idempotency context:

~~~python
from aletheia.programs import GraphCommandContext, ProgramGraphStore, QuestSpec

store = ProgramGraphStore()
quest = QuestSpec(
    identity_key="materials-mechanisms-2026",
    title="Mechanistic materials discovery",
    direction="Discover a reproducible mechanism, not only a benchmark delta.",
    value_boundary="Retain unfavorable and null results.",
    safety_boundary=("No unreviewed external action",),
    resource_boundary={"currency": "USD"},
)
receipt = store.create_quest(
    quest,
    GraphCommandContext(
        idempotency_key="quest:materials-mechanisms-2026",
        principal="operator:alice",
    ),
)
snapshot = store.get_quest(receipt.object_id)
~~~

Exact replay returns `command.created == false` and the original receipt. Reusing the same command
or source-event identity with changed content fails. Reusing the stable node identity under a new
command with changed content also fails.

Creation order is:

1. Quest;
2. Program;
3. ScientificFamily;
4. Campaign bound to that family;
5. legacy Run binding;
6. Experiment/ResearchQuestion bindings; and
7. budget/data allocations.

The order is deliberate: each child is accepted only after its authority scope exists.

## Lifecycle

| Node | Initial | Normal active path | Terminal/archive path |
| --- | --- | --- | --- |
| Quest | `draft` | `draft → active ↔ paused` | `active/paused → completed → archived` |
| Program | `proposed` | `proposed → active ↔ paused` | `active/paused → stopped` or `active → completed`, then `archived` |
| Campaign | `planned` | `planned → active ↔ paused` | `active/paused → stopped/failed` or `active → completed`, then `archived` |

The caller supplies `expected_version`. Stale writes fail rather than overwriting a concurrent
transition. Activation also requires:

- active parent, unless the node is the Quest root; and
- every scientific dependency to be completed (or archived from completed).

Completing a node requires every direct child to have scientifically completed. Pause/stop direct
children before pausing/stopping their parent.

## Dependency graph

Only same-type, same-Quest edges are accepted:

- Program may depend on Program;
- Campaign may depend on Campaign; and
- Quest and cross-level edges are rejected.

Freeze dependencies before the dependent node becomes active. The service performs a prospective
DAG check under a Quest row lock. PostgreSQL repeats cycle protection under a Quest-scoped advisory
transaction lock, so concurrent `A → B` and `B → A` requests commit at most one edge.

## Cross-Campaign scientific family

A family freezes `family_key`, scientific scope, and multiplicity policy once per Program. Campaign
specs reference its `family_id`. A Run binding lets `register_hypothesis_attempt` derive the family;
the caller cannot rebind it:

~~~python
from aletheia.memory.service import (
    list_scientific_family_attempts,
    register_hypothesis_attempt,
)

register_hypothesis_attempt(
    run_id,
    experiment_id=experiment_id,
    family_key="mechanism-a",
    hypothesis_text="...",
    round_index=1,
    phase="confirmation",
    split_hash="...",
    alpha_allocated=0.01,
)
attempts = list_scientific_family_attempts(family_id)
~~~

`attempts` covers every Campaign/Run bound to that family. A Campaign restart is therefore not a
fresh statistical family.

## Budget allocations

Allocation caps use integer microunits:

- `1_000_000` USD microunits = 1 USD;
- other continuous kinds use the same millionth-unit convention; and
- `experiment_count` uses microunits for one consistent authority field, so one experiment is
  represented as `1_000_000`.

A Quest allocation has no parent. A Program allocation must name the same-kind Quest allocation.
The sum of Program child caps cannot exceed the Quest cap.

To bind existing spend events:

~~~python
record_budget_event(
    run_id,
    "usd",
    0.25,
    research_budget_allocation_id=program_allocation_id,
)
~~~

The event is rejected if the kind differs, the Run is outside the allocation scope, the charge is
negative, or cumulative allocation spend exceeds the cap. Legacy unallocated events remain
readable and keep their former behavior.

## Data-role allocations

Allocation roles are `exploration`, `training`, `confirmation`, `external_validation`,
`replication`, and `safety`. The referenced `DataAsset` Run must already belong to a descendant
Campaign.

An asset registered as `external_validation` can only be allocated to external validation or
replication. A primary/adaptive asset cannot be relabeled as external validation. Exclusive assets
cannot be shared into another scope.

The allocation records policy and policy SHA-256; it does not open, copy, or read the dataset.
Existing one-time external-validation receipts still control information release.

## API and dashboard

Read endpoints:

- `GET /legacy/research-graph/quests`
- `GET /legacy/research-graph/quests/{quest_id}`

Mutation endpoints cover Quest, Program, Family, Campaign, transition, dependency, external
binding, and allocation creation under the deprecated `/legacy/research-graph` compatibility
surface. Requests carry idempotency/source-event
metadata, but the server derives `principal` from the authenticated identity. Viewers receive 403
for mutations.

The dashboard panel is deliberately a read projection. Its “Rebuild view” button calls the list
endpoint again. It neither folds events in the browser nor stores lifecycle authority in local
state.

Operators can run the same fail-closed read path without the API:

~~~bash
conda run -n aletheia python scripts/research_graph.py list
conda run -n aletheia python scripts/research_graph.py show qst_<32-hex>
conda run -n aletheia python scripts/research_graph.py verify qst_<32-hex>
~~~

## Reconstruction and incident handling

`get_quest` revalidates frozen specs, hashes, hierarchy, transition chains, command/event receipts,
dependencies, family closure, bindings, and allocations. It then hashes the canonical graph.

If rebuild fails:

1. stop automated mutation for the affected Quest;
2. retain database and keyed-event evidence unchanged;
3. inspect the named node/command/allocation from the exception;
4. compare deployed code to Alembic head `20260825_0024`; and
5. restore a verified backup or deploy matching code—do not patch an append-only row in place.

A missing UI card is not evidence that the Quest disappeared. The PostgreSQL ledger and successful
rebuild are authoritative.

## Current boundary

This graph slice provides durable strategic identity and allocations. F11-S4 adds a separate
receipt-backed memory ledger and uses graph ancestry for task-scoped context; F11-S5 consumes both
to generate a constrained shadow portfolio without rewriting either. See
`RECEIPT_BACKED_SCIENTIFIC_MEMORY.md` and `SHADOW_RESEARCH_PORTFOLIO.md`. None of these layers
automatically grants budget/action authority, proves scientific quality, or completes the
frontier-scientist exit.
