# Receipt-backed scientific memory

This is the F11-S4 operator/developer guide for immutable scientific facts, non-destructive
compaction, task-minimal prompt context, artifact recovery, and provider/model switching.

## Deploy

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

Expected repository head: `20260825_0024`.

- `0016` adds facts, task bindings, compactions, members, and context receipts;
- `0017` adds append-only, command-binding, disposition, completeness, and context guards; and
- `0018` enforces a single root and single successor for each scope/task compaction chain;
- `0019`–`0020` add the F11-S5 shadow portfolio ledger and its integrity guards.
- `0021`–`0023` add later fault/endurance evidence and the separate research-kernel authority
  store; they do not change this legacy memory's authority scope.
- `0024` adds the separate qualification-only local-execution store and does not change this legacy
  memory authority.

API and worker startup fail closed at any other revision.

## Register authoritative facts

~~~python
from aletheia.programs import (
    GraphCommandContext,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemoryTaskBindingSpec,
    ResearchMemoryFactSpec,
    ResearchMemoryStore,
)

store = ResearchMemoryStore()
fact = ResearchMemoryFactSpec(
    scope_node_id="cmp_<32-hex>",
    kind=MemoryFactKind.CONTRADICTION,
    statement="The validation response contradicts the proposed mechanism.",
    detail={"comparison": "precommitted-control"},
    task_bindings=(
        MemoryTaskBindingSpec(
            task_key="revise-mechanism",
            context_role=MemoryContextRole.SUPPORTING,
        ),
    ),
    sources=(
        MemorySourceRef(
            kind=MemorySourceKind.ARTIFACT,
            source_id="validation-receipt-42",
            sha256="<64-hex>",
            uri="artifact://validation/42",
        ),
    ),
)
receipt = store.register_fact(
    fact,
    GraphCommandContext(
        idempotency_key="memory:validation-42",
        principal="worker:validator",
    ),
)
~~~

Use `*` only for facts deliberately required across every task in the fact's graph ancestry.
Prefer a narrow task key. A Campaign context inherits matching Program and Quest facts; it does not
inherit sibling Campaign facts.

`MemoryChunk`/`memory_recall` remains useful for discovering candidates. It is not accepted as a
replacement for `ResearchMemoryFactSpec`.

## Produce and commit a compaction

First obtain the exact eligible set:

~~~python
facts = store.eligible_facts("cmp_<32-hex>", "revise-mechanism")
~~~

Give the producer only this task set. Its draft must claim every ID:

~~~python
from aletheia.programs import MemorySummaryDraft
from aletheia.reproducibility.manifest import content_sha256

draft = MemorySummaryDraft(
    producer_provider="openai",
    producer_model="gpt-example",
    prompt_sha256=content_sha256({"template": "scientific-memory-summary-v1"}),
    summary_text="The original mechanism needs revision after the validation contradiction.",
    covered_fact_ids=tuple(fact.fact_id for fact in facts),
)
compaction = store.compact(
    scope_node_id="cmp_<32-hex>",
    task_key="revise-mechanism",
    draft=draft,
    context=GraphCommandContext(
        idempotency_key="memory-compact:revision-1",
        principal="worker:memory-compactor",
    ),
)
~~~

The harness—not the producer—copies negative results, contradictions, limitations, failed
hypotheses, safety boundaries, and `required` bindings into the exact section. Omission/extra IDs,
an empty set, changed facts during commit, or a stale parent rejects the transaction.

## Build and deliver minimal task context

~~~python
from aletheia.programs import TaskContextRequest

delivery = store.build_task_context(
    TaskContextRequest(
        scope_node_id="cmp_<32-hex>",
        task_key="revise-mechanism",
        compaction_id=compaction.object_id,
        max_chars=12_000,
        consumer_provider="openai",
        consumer_model="gpt-example",
    ),
    GraphCommandContext(
        idempotency_key="memory-context:revision-worker-1",
        principal="scheduler:portfolio",
    ),
)
~~~

Pass `delivery.context_receipt_id` to `run_worker(...,
memory_context_receipt_id=...)`. The worker reloads the database receipt and artifact, rejects stale
state or a provider/model mismatch, and prepends the verified context to the current task. Do not
copy arbitrary transcript text into the prompt alongside it.

If exact facts exceed `max_chars`, context construction raises
`ResearchMemoryContextOverflow`. Increase the reviewed budget or split the task. Do not catch the
error and fall back to a truncated summary.

## Provider/model switch

Build a new delivery receipt naming the replacement provider/model. The underlying compaction does
not need to be regenerated if its source set is still current. The new and old delivery receipts
have different consumer metadata but the same provider-neutral `context_sha256` and prompt text.

No call to the old provider is made during `load_task_context`.

## API

Deprecated compatibility endpoints under `/legacy/research-graph` (authenticated):

- `POST /memory/facts`;
- `POST /memory/compactions`;
- `GET /memory/{scope_node_id}?task_key=...`;
- `GET /memory/compactions/{compaction_id}/artifact`;
- `POST /memory/contexts`; and
- `GET /memory/contexts/{context_receipt_id}`.

The server derives mutation principal from the authenticated identity. Viewer roles can rebuild and
inspect but cannot register, compact, or create a delivery receipt.

## CLI verification

~~~bash
conda run -n aletheia python scripts/research_memory.py verify cmp_<32-hex> revise-mechanism
conda run -n aletheia python scripts/research_memory.py show cmp_<32-hex> revise-mechanism
conda run -n aletheia python scripts/research_memory.py artifact mcp_<32-hex>
conda run -n aletheia python scripts/research_memory.py context mctx_<32-hex> --prompt-only
~~~

Each command rehashes the same ledger/artifact boundary used by the worker.

## Incident handling

When rebuild, artifact recovery, or context loading fails:

1. stop delivery for the affected scope/task;
2. preserve PostgreSQL, keyed events, and archive bytes;
3. confirm deployed code and database are at `20260825_0024`;
4. inspect the named fact, command, compaction, member, or context receipt;
5. restore a verified database/archive pair or deploy matching code; and
6. never repair an append-only row in place.

A stale compaction is normal after a new fact: create a complete successor. A missing/corrupt
artifact is not normal and blocks recovery. A newer reviewed compaction also supersedes old context
receipts even when its fact set is unchanged; old artifacts remain audit-readable but cannot be
delivered to a worker.

## Current boundary

This layer proves custody, complete source membership, exact negative-state retention, deterministic
rebuild, and provider-neutral delivery. It does not prove narrative faithfulness, source truth,
scientific validity, or portfolio value. Scheduler stages must explicitly emit typed facts and
request fresh contexts. F11-S5 consumes those receipts for shadow planning but intentionally does
not own production autonomous allocation; see `SHADOW_RESEARCH_PORTFOLIO.md`.
