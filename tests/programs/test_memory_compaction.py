from __future__ import annotations

import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import DBAPIError

from aletheia.db import create_all, engine, session_scope
from aletheia.programs import (
    CampaignSpec,
    GraphCommandContext,
    MemoryContextRole,
    MemoryFactKind,
    MemorySourceKind,
    MemorySourceRef,
    MemorySummaryDraft,
    MemoryTaskBindingSpec,
    ProgramGraphStore,
    QuestSpec,
    ResearchMemoryConflict,
    ResearchMemoryContextOverflow,
    ResearchMemoryFactSpec,
    ResearchMemoryInvariantError,
    ResearchMemoryStale,
    ResearchMemoryStore,
    ResearchProgramSpec,
    ScientificFamilySpec,
    TaskContextRequest,
    build_research_memory_snapshot,
)
from aletheia.programs.persistence import (
    ResearchMemoryCompactionMemberRecord,
    ResearchMemoryFactRecord,
)
from aletheia.reproducibility.manifest import content_sha256
from aletheia.schema_migrations import schema_diffs


@pytest.fixture(autouse=True)
def _schema() -> None:
    create_all()


def _ctx(seed: str, label: str) -> GraphCommandContext:
    return GraphCommandContext(
        idempotency_key=f"f11s4:{seed[:20]}:{label}",
        principal="pytest:f11s4",
    )


def _seed_hierarchy(seed: str):
    graph = ProgramGraphStore()
    quest = QuestSpec(
        identity_key=f"memory-quest-{seed}",
        title="Receipt-backed memory quest",
        direction="Test a mechanism without forgetting negative evidence.",
        value_boundary="Truthful recovery outranks a persuasive summary.",
        safety_boundary=("No unreviewed external action",),
    )
    program = ResearchProgramSpec(
        quest_id=quest.node_id,
        identity_key=f"memory-program-{seed}",
        title="Memory program",
        objective="Discriminate two explanations.",
        problem_domain="synthetic",
    )
    family = ScientificFamilySpec(
        program_id=program.node_id,
        family_key=f"memory-family-{seed[:20]}",
        title="Memory family",
        scientific_scope="The same mechanism across resumptions.",
    )
    campaign = CampaignSpec(
        program_id=program.node_id,
        family_id=family.family_id,
        identity_key=f"memory-campaign-{seed}",
        title="Memory campaign",
        objective="Perform one falsifiable task.",
    )
    graph.create_quest(quest, _ctx(seed, "quest"))
    graph.create_program(program, _ctx(seed, "program"))
    graph.create_scientific_family(family, _ctx(seed, "family"))
    graph.create_campaign(campaign, _ctx(seed, "campaign"))
    return quest, program, campaign


def _source(label: str) -> MemorySourceRef:
    return MemorySourceRef(
        kind=MemorySourceKind.ARTIFACT,
        source_id=label,
        sha256=content_sha256({"fixture": label}),
        uri=f"fixture://{label}",
    )


def _fact(
    scope_node_id: str,
    *,
    label: str,
    kind: MemoryFactKind,
    task_key: str,
    role: MemoryContextRole = MemoryContextRole.SUPPORTING,
    statement: str | None = None,
) -> ResearchMemoryFactSpec:
    return ResearchMemoryFactSpec(
        scope_node_id=scope_node_id,
        kind=kind,
        statement=statement or f"Scientific fact {label}",
        detail={"fixture": label, "ordinal": len(label)},
        task_bindings=(MemoryTaskBindingSpec(task_key=task_key, context_role=role),),
        sources=(_source(label),),
    )


def _draft(facts, *, provider: str = "anthropic", model: str = "claude-test"):
    return MemorySummaryDraft(
        producer_provider=provider,
        producer_model=model,
        prompt_sha256=content_sha256({"prompt": "summarize scientific state v1"}),
        summary_text="The task remains open; the proposed method and its negative evidence coexist.",
        covered_fact_ids=tuple(sorted(fact.fact_id for fact in facts)),
    )


def test_migration_matches_orm_and_memory_guards_exist() -> None:
    expected = {
        "research_memory_facts",
        "research_memory_task_bindings",
        "research_memory_compactions",
        "research_memory_compaction_members",
        "research_memory_context_receipts",
    }
    with engine().connect() as connection:
        assert expected.issubset(inspect(connection).get_table_names())
        assert schema_diffs(connection) == []
        triggers = set(
            connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'trg_research_memory_%'"
                )
            ).scalars()
        )
    assert "trg_research_memory_fact_guard" in triggers
    assert "trg_research_memory_compaction_complete" in triggers
    assert "trg_research_memory_context_guard" in triggers
    assert "trg_research_memory_facts_append_only" in triggers


def test_compaction_mechanically_preserves_every_negative_fact(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    facts = (
        _fact(campaign.node_id, label="method", kind=MemoryFactKind.METHOD, task_key="analyze"),
        _fact(
            campaign.node_id,
            label="required-hypothesis",
            kind=MemoryFactKind.HYPOTHESIS,
            task_key="analyze",
            role=MemoryContextRole.REQUIRED,
        ),
        _fact(
            campaign.node_id,
            label="negative",
            kind=MemoryFactKind.NEGATIVE_RESULT,
            task_key="analyze",
        ),
        _fact(
            campaign.node_id,
            label="contradiction",
            kind=MemoryFactKind.CONTRADICTION,
            task_key="analyze",
        ),
        _fact(
            campaign.node_id,
            label="limitation",
            kind=MemoryFactKind.LIMITATION,
            task_key="analyze",
        ),
        _fact(
            campaign.node_id,
            label="failed",
            kind=MemoryFactKind.FAILED_HYPOTHESIS,
            task_key="analyze",
        ),
    )
    for index, fact in enumerate(facts):
        store.register_fact(fact, _ctx(seed, f"fact-{index}"))

    receipt = store.compact(
        scope_node_id=campaign.node_id,
        task_key="analyze",
        draft=_draft(facts),
        context=_ctx(seed, "compact"),
    )
    artifact = store.recover_compaction(receipt.object_id)
    exact_by_kind = {fact.kind for fact in artifact.exact_facts}
    assert MemoryFactKind.HYPOTHESIS in exact_by_kind
    assert MemoryFactKind.NEGATIVE_RESULT in exact_by_kind
    assert MemoryFactKind.CONTRADICTION in exact_by_kind
    assert MemoryFactKind.LIMITATION in exact_by_kind
    assert MemoryFactKind.FAILED_HYPOTHESIS in exact_by_kind
    assert MemoryFactKind.METHOD not in exact_by_kind
    assert len(artifact.members) == len(facts)


def test_summary_coverage_must_equal_the_full_eligible_set(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    facts = (
        _fact(campaign.node_id, label="one", kind=MemoryFactKind.RESULT, task_key="write"),
        _fact(
            campaign.node_id,
            label="two",
            kind=MemoryFactKind.LIMITATION,
            task_key="write",
        ),
    )
    for index, fact in enumerate(facts):
        store.register_fact(fact, _ctx(seed, f"fact-{index}"))
    incomplete = MemorySummaryDraft(
        producer_provider="provider-a",
        producer_model="model-a",
        prompt_sha256="a" * 64,
        summary_text="An incomplete summary.",
        covered_fact_ids=(facts[0].fact_id,),
    )
    with pytest.raises(ResearchMemoryConflict, match="exactly match"):
        store.compact(
            scope_node_id=campaign.node_id,
            task_key="write",
            draft=incomplete,
            context=_ctx(seed, "incomplete"),
        )


def test_task_context_excludes_other_tasks_and_is_provider_neutral(tmp_path) -> None:
    seed = uuid.uuid4().hex
    quest, program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    relevant = _fact(
        campaign.node_id,
        label="relevant",
        kind=MemoryFactKind.METHOD,
        task_key="design",
    )
    required = _fact(
        program.node_id,
        label="required",
        kind=MemoryFactKind.ASSUMPTION,
        task_key="design",
        role=MemoryContextRole.REQUIRED,
    )
    global_limitation = _fact(
        quest.node_id,
        label="global-limit",
        kind=MemoryFactKind.LIMITATION,
        task_key="*",
    )
    decoy = _fact(
        campaign.node_id,
        label="decoy-negative",
        kind=MemoryFactKind.NEGATIVE_RESULT,
        task_key="unrelated-task",
    )
    for index, fact in enumerate((relevant, required, global_limitation, decoy)):
        store.register_fact(fact, _ctx(seed, f"fact-{index}"))
    eligible = (relevant, required, global_limitation)
    compaction = store.compact(
        scope_node_id=campaign.node_id,
        task_key="design",
        draft=_draft(eligible),
        context=_ctx(seed, "compact"),
    )
    first = store.build_task_context(
        TaskContextRequest(
            scope_node_id=campaign.node_id,
            task_key="design",
            compaction_id=compaction.object_id,
            max_chars=12_000,
            consumer_provider="anthropic",
            consumer_model="claude-new",
        ),
        _ctx(seed, "context-a"),
    )
    second = store.build_task_context(
        TaskContextRequest(
            scope_node_id=campaign.node_id,
            task_key="design",
            max_chars=12_000,
            consumer_provider="openai",
            consumer_model="gpt-new",
        ),
        _ctx(seed, "context-b"),
    )
    assert first.context.context_sha256 == second.context.context_sha256
    assert first.context.prompt_text == second.context.prompt_text
    assert decoy.fact_id not in first.context.source_fact_ids
    assert "decoy-negative" not in first.context.prompt_text
    assert set(first.context.source_fact_ids) == {fact.fact_id for fact in eligible}
    assert {fact.fact_id for fact in first.context.exact_facts} == {
        required.fact_id,
        global_limitation.fact_id,
    }
    assert first.consumer_provider != second.consumer_provider


def test_new_fact_makes_context_stale_until_recompacted(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    first = _fact(campaign.node_id, label="first", kind=MemoryFactKind.RESULT, task_key="next")
    store.register_fact(first, _ctx(seed, "fact-first"))
    initial = store.compact(
        scope_node_id=campaign.node_id,
        task_key="next",
        draft=_draft((first,)),
        context=_ctx(seed, "compact-first"),
    )
    second = _fact(
        campaign.node_id,
        label="new-contradiction",
        kind=MemoryFactKind.CONTRADICTION,
        task_key="next",
    )
    store.register_fact(second, _ctx(seed, "fact-second"))
    with pytest.raises(ResearchMemoryStale, match="stale"):
        store.build_task_context(
            TaskContextRequest(
                scope_node_id=campaign.node_id,
                task_key="next",
                compaction_id=initial.object_id,
                consumer_provider="provider",
                consumer_model="model",
            ),
            _ctx(seed, "stale-context"),
        )
    refreshed = store.compact(
        scope_node_id=campaign.node_id,
        task_key="next",
        draft=_draft((first, second), provider="openai", model="replacement-model"),
        context=_ctx(seed, "compact-second"),
    )
    payload = store.build_task_context(
        TaskContextRequest(
            scope_node_id=campaign.node_id,
            task_key="next",
            compaction_id=refreshed.object_id,
            consumer_provider="provider-b",
            consumer_model="model-b",
        ),
        _ctx(seed, "fresh-context"),
    ).context
    assert second.fact_id in payload.source_fact_ids
    assert second.statement in payload.prompt_text


def test_newer_compaction_supersedes_old_context_with_the_same_fact_set(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    fact = _fact(
        campaign.node_id,
        label="stable-source",
        kind=MemoryFactKind.RESULT,
        task_key="interpret",
    )
    store.register_fact(fact, _ctx(seed, "fact"))
    first = store.compact(
        scope_node_id=campaign.node_id,
        task_key="interpret",
        draft=_draft((fact,), provider="provider-a", model="model-a"),
        context=_ctx(seed, "compact-first"),
    )
    old_context = store.build_task_context(
        TaskContextRequest(
            scope_node_id=campaign.node_id,
            task_key="interpret",
            compaction_id=first.object_id,
            consumer_provider="provider-a",
            consumer_model="model-a",
        ),
        _ctx(seed, "context-first"),
    )
    revised_draft = MemorySummaryDraft(
        producer_provider="provider-b",
        producer_model="model-b",
        prompt_sha256=content_sha256({"prompt": "revised scientific memory"}),
        summary_text="The same frozen result now has a reviewed, corrected interpretation.",
        covered_fact_ids=(fact.fact_id,),
    )
    second = store.compact(
        scope_node_id=campaign.node_id,
        task_key="interpret",
        draft=revised_draft,
        context=_ctx(seed, "compact-second"),
    )

    with pytest.raises(ResearchMemoryStale, match="superseded"):
        store.build_task_context(
            TaskContextRequest(
                scope_node_id=campaign.node_id,
                task_key="interpret",
                compaction_id=first.object_id,
                consumer_provider="provider-a",
                consumer_model="model-a",
            ),
            _ctx(seed, "context-old-explicit"),
        )
    with pytest.raises(ResearchMemoryStale, match="superseded"):
        store.load_task_context(old_context.context_receipt_id)

    current = store.build_task_context(
        TaskContextRequest(
            scope_node_id=campaign.node_id,
            task_key="interpret",
            consumer_provider="provider-b",
            consumer_model="model-b",
        ),
        _ctx(seed, "context-second"),
    )
    assert current.context.compaction_id == second.object_id
    assert revised_draft.summary_text in current.context.prompt_text


def test_randomized_rebuild_is_order_independent(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    facts = tuple(
        _fact(
            campaign.node_id,
            label=f"fact-{index}",
            kind=(MemoryFactKind.LIMITATION if index == 2 else MemoryFactKind.RESULT),
            task_key="rebuild",
        )
        for index in range(5)
    )
    for index, fact in enumerate(facts):
        store.register_fact(fact, _ctx(seed, f"fact-{index}"))
    store.compact(
        scope_node_id=campaign.node_id,
        task_key="rebuild",
        draft=_draft(facts),
        context=_ctx(seed, "compact"),
    )
    canonical = store.rebuild_memory(campaign.node_id, "rebuild")
    for shuffle_seed in range(20):
        shuffled_facts = list(canonical.facts)
        shuffled_compactions = list(canonical.compactions)
        random.Random(shuffle_seed).shuffle(shuffled_facts)
        random.Random(shuffle_seed + 100).shuffle(shuffled_compactions)
        rebuilt = build_research_memory_snapshot(
            quest_id=canonical.quest_id,
            scope_node_id=canonical.scope_node_id,
            task_key=canonical.task_key,
            facts=shuffled_facts,
            compactions=shuffled_compactions,
        )
        assert rebuilt == canonical
        assert rebuilt.memory_sha256 == canonical.memory_sha256


def test_missing_or_corrupt_artifact_blocks_recovery(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    root = tmp_path / "archive"
    store = ResearchMemoryStore(archive_root=root)
    fact = _fact(campaign.node_id, label="artifact", kind=MemoryFactKind.RESULT, task_key="read")
    store.register_fact(fact, _ctx(seed, "fact"))
    receipt = store.compact(
        scope_node_id=campaign.node_id,
        task_key="read",
        draft=_draft((fact,)),
        context=_ctx(seed, "compact"),
    )
    artifact = store.recover_compaction(receipt.object_id)
    snapshot = store.rebuild_memory(campaign.node_id, "read")
    path = root / snapshot.compactions[0].artifact.relative_path
    assert artifact.compaction_id == receipt.object_id
    path.chmod(0o600)
    path.write_text("{}")
    with pytest.raises(ResearchMemoryInvariantError, match="missing or corrupt"):
        store.recover_compaction(receipt.object_id)
    with pytest.raises(ResearchMemoryInvariantError, match="missing or corrupt"):
        store.rebuild_memory(campaign.node_id, "read")


def test_context_budget_never_causes_protected_fact_elision(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    fact = _fact(
        campaign.node_id,
        label="large-negative",
        kind=MemoryFactKind.NEGATIVE_RESULT,
        task_key="budget",
        statement="negative evidence " + ("x" * 2_000),
    )
    store.register_fact(fact, _ctx(seed, "fact"))
    store.compact(
        scope_node_id=campaign.node_id,
        task_key="budget",
        draft=_draft((fact,)),
        context=_ctx(seed, "compact"),
    )
    with pytest.raises(ResearchMemoryContextOverflow, match="exceeds"):
        store.build_task_context(
            TaskContextRequest(
                scope_node_id=campaign.node_id,
                task_key="budget",
                max_chars=512,
                consumer_provider="provider",
                consumer_model="model",
            ),
            _ctx(seed, "context"),
        )


def test_exact_replay_is_idempotent_and_changed_replay_fails(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    fact = _fact(campaign.node_id, label="replay", kind=MemoryFactKind.RESULT, task_key="task")
    context = _ctx(seed, "fact")
    first = store.register_fact(fact, context)
    second = store.register_fact(fact, context)
    assert first.object_id == second.object_id
    assert first.created is True
    assert second.created is False
    compact_context = _ctx(seed, "compact")
    draft = _draft((fact,))
    first_compaction = store.compact(
        scope_node_id=campaign.node_id,
        task_key="task",
        draft=draft,
        context=compact_context,
    )
    second_compaction = store.compact(
        scope_node_id=campaign.node_id,
        task_key="task",
        draft=draft,
        context=compact_context,
    )
    assert first_compaction.object_id == second_compaction.object_id
    assert second_compaction.created is False
    request = TaskContextRequest(
        scope_node_id=campaign.node_id,
        task_key="task",
        consumer_provider="openai",
        consumer_model="model",
    )
    context_command = _ctx(seed, "context")
    first_context = store.build_task_context(request, context_command)
    second_context = store.build_task_context(request, context_command)
    assert first_context.context_receipt_id == second_context.context_receipt_id
    assert second_context.command.created is False
    assert store.load_task_context(first_context.context_receipt_id) == second_context
    changed = _fact(
        campaign.node_id,
        label="changed",
        kind=MemoryFactKind.RESULT,
        task_key="task",
    )
    with pytest.raises(Exception, match="different content"):
        store.register_fact(changed, context)


def test_database_rejects_mutation_and_late_compaction_members(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    fact = _fact(campaign.node_id, label="immutable", kind=MemoryFactKind.RESULT, task_key="task")
    store.register_fact(fact, _ctx(seed, "fact"))
    compaction = store.compact(
        scope_node_id=campaign.node_id,
        task_key="task",
        draft=_draft((fact,)),
        context=_ctx(seed, "compact"),
    )
    with pytest.raises(DBAPIError, match="append-only"):
        with engine().begin() as connection:
            connection.execute(
                update(ResearchMemoryFactRecord)
                .where(ResearchMemoryFactRecord.fact_id == fact.fact_id)
                .values(statement="tampered")
            )

    late = _fact(campaign.node_id, label="late", kind=MemoryFactKind.RESULT, task_key="task")
    store.register_fact(late, _ctx(seed, "late-fact"))
    with pytest.raises(DBAPIError, match="after compaction commit"):
        with session_scope() as session:
            session.add(
                ResearchMemoryCompactionMemberRecord(
                    compaction_id=compaction.object_id,
                    fact_id=late.fact_id,
                    fact_sha256=late.fact_sha256,
                    fact_kind=late.kind.value,
                    disposition="summary",
                )
            )


def test_concurrent_fact_commit_makes_inflight_compaction_fail_closed(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    first = _fact(campaign.node_id, label="first-race", kind=MemoryFactKind.RESULT, task_key="race")
    second = _fact(
        campaign.node_id,
        label="second-race",
        kind=MemoryFactKind.CONTRADICTION,
        task_key="race",
    )
    store.register_fact(first, _ctx(seed, "first"))
    underlying = store._archive
    archive_started = Event()
    allow_archive = Event()

    class PausingArchive:
        def store(self, artifact, *, archived_at):
            archive_started.set()
            assert allow_archive.wait(timeout=5)
            return underlying.store(artifact, archived_at=archived_at)

        def read(self, receipt):
            return underlying.read(receipt)

    store._archive = PausingArchive()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            store.compact,
            scope_node_id=campaign.node_id,
            task_key="race",
            draft=_draft((first,)),
            context=_ctx(seed, "compact-race"),
        )
        assert archive_started.wait(timeout=5)
        store.register_fact(second, _ctx(seed, "second"))
        allow_archive.set()
        with pytest.raises(ResearchMemoryStale, match="changed"):
            future.result(timeout=10)
    snapshot = store.rebuild_memory(campaign.node_id, "race")
    assert {fact.fact_id for fact in snapshot.facts} == {first.fact_id, second.fact_id}
    assert snapshot.compactions == ()


def test_concurrent_compactions_form_one_linear_receipt_chain(tmp_path) -> None:
    seed = uuid.uuid4().hex
    _quest, _program, campaign = _seed_hierarchy(seed)
    store = ResearchMemoryStore(archive_root=tmp_path / "archive")
    fact = _fact(campaign.node_id, label="linear", kind=MemoryFactKind.RESULT, task_key="linear")
    store.register_fact(fact, _ctx(seed, "fact"))
    underlying = store._archive
    both_prepared = Barrier(2)

    class BarrierArchive:
        def store(self, artifact, *, archived_at):
            both_prepared.wait(timeout=5)
            return underlying.store(artifact, archived_at=archived_at)

        def read(self, receipt):
            return underlying.read(receipt)

    store._archive = BarrierArchive()

    def compact(label: str):
        draft = MemorySummaryDraft(
            producer_provider=f"provider-{label}",
            producer_model=f"model-{label}",
            prompt_sha256=content_sha256({"prompt": label}),
            summary_text=f"Concurrent summary {label}.",
            covered_fact_ids=(fact.fact_id,),
        )
        return store.compact(
            scope_node_id=campaign.node_id,
            task_key="linear",
            draft=draft,
            context=_ctx(seed, f"compact-{label}"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compact, label) for label in ("a", "b")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except ResearchMemoryStale as exc:
                outcomes.append(exc)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ResearchMemoryStale) for item in outcomes) == 1
    snapshot = store.rebuild_memory(campaign.node_id, "linear")
    assert len(snapshot.compactions) == 1
    assert snapshot.latest_compaction_id == snapshot.compactions[0].compaction_id
