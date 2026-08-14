from __future__ import annotations

import pytest

import aletheia.knowledge as k
from .f8s2_fixtures import (
    StepClock,
    build_adapters,
    build_citation_policy,
    build_search_plan,
    sha,
)


def _two_hop_graph(plan: k.SearchExecutionPlan):
    backward = sha("citation-backward-neighbor")
    forward = sha("citation-forward-neighbor")
    graph: dict[tuple[str, k.QueryFamily], tuple[str, ...]] = {}
    for seed in plan.protocol.seed_paper_snapshot_sha256s:
        graph[(seed, k.QueryFamily.CITATION_BACKWARD)] = (backward,)
        graph[(seed, k.QueryFamily.CITATION_FORWARD)] = (forward,)
    graph[(backward, k.QueryFamily.CITATION_BACKWARD)] = ()
    graph[(backward, k.QueryFamily.CITATION_FORWARD)] = ()
    graph[(forward, k.QueryFamily.CITATION_BACKWARD)] = ()
    graph[(forward, k.QueryFamily.CITATION_FORWARD)] = ()
    return graph, (backward, forward)


async def _initial_execution(tmp_path, policy: k.CitationTraversalPolicy):
    plan = build_search_plan(
        citation_traversal_policy_sha256=policy.policy_sha256
    )
    adapters = build_adapters(plan)
    graph, neighbors = _two_hop_graph(plan)
    for adapter in adapters.values():
        adapter.citation_graph = graph
    executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    )
    initial = await executor.execute(
        plan=plan, execution_id="f8s2-citation-initial"
    )
    return initial, executor, adapters, neighbors


@pytest.mark.asyncio
async def test_two_direction_traversal_derives_every_new_hit_and_saturates(tmp_path) -> None:
    policy = build_citation_policy(consecutive_saturated_rounds=1)
    initial, executor, _adapters, neighbors = await _initial_execution(tmp_path, policy)
    campaign = await k.run_citation_traversal(
        campaign_id="f8s2-citation-campaign",
        policy=policy,
        initial_execution=initial,
        executor=executor,
    )

    assert campaign.coverage_disposition == "eligible"
    assert campaign.stopping_reason is k.SearchStoppingReason.SATURATION
    assert not campaign.blockers
    assert len(campaign.rounds) == len(campaign.executions) == 2
    first, second = campaign.rounds
    assert set(first.new_paper_snapshot_sha256s) == set(neighbors)
    assert second.frontier_paper_snapshot_sha256s == tuple(sorted(neighbors))
    assert second.new_paper_snapshot_sha256s == ()
    assert second.saturated is True
    assert campaign.executions[1].plan.parent_execution_sha256 == initial.execution_sha256
    assert {
        query.seed_paper_snapshot_sha256
        for query in campaign.executions[1].plan.queries
    } == set(neighbors)
    assert all(
        audit.status is k.ReplayAuditStatus.COMPLETE
        for audit in campaign.replay_audits
    )
    assert all(
        k.load_search_execution(archive=executor.archive, ledger=ledger) == execution
        for execution, ledger in zip(
            campaign.executions, campaign.execution_ledgers, strict=True
        )
    )


@pytest.mark.asyncio
async def test_exhausted_frontier_is_complete_even_before_saturation_window(tmp_path) -> None:
    policy = build_citation_policy(consecutive_saturated_rounds=2)
    initial, executor, _adapters, _neighbors = await _initial_execution(tmp_path, policy)
    campaign = await k.run_citation_traversal(
        campaign_id="f8s2-source-exhaustion",
        policy=policy,
        initial_execution=initial,
        executor=executor,
    )
    assert campaign.stopping_reason is k.SearchStoppingReason.SOURCE_EXHAUSTED
    assert campaign.coverage_disposition == "eligible"
    assert campaign.rounds[-1].new_paper_snapshot_sha256s == ()


@pytest.mark.asyncio
async def test_derived_round_failure_is_recorded_replayed_and_blocks_coverage(tmp_path) -> None:
    policy = build_citation_policy()
    initial, executor, adapters, _neighbors = await _initial_execution(tmp_path, policy)
    for adapter in adapters.values():
        adapter.fail_citation_rounds.add(1)
    campaign = await k.run_citation_traversal(
        campaign_id="f8s2-derived-failure",
        policy=policy,
        initial_execution=initial,
        executor=executor,
    )

    assert campaign.stopping_reason is k.SearchStoppingReason.HARD_FAILURE
    assert campaign.coverage_disposition == "blocked"
    assert set(campaign.blockers) == {
        k.CitationCoverageBlocker.ROUND_EXECUTION_INCOMPLETE,
        k.CitationCoverageBlocker.REPLAY_INCOMPLETE,
    }
    assert campaign.rounds[1].failure_sha256s
    assert all(
        failure.kind is k.SearchFailureKind.CIRCUIT_OPEN
        for failure in campaign.executions[1].failures
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "maximum_requests, maximum_expanded, expected",
    [
        (1, 1_000, k.CitationCoverageBlocker.REQUEST_BUDGET_EXHAUSTED),
        (2_000, 1, k.CitationCoverageBlocker.FRONTIER_BUDGET_EXHAUSTED),
    ],
)
async def test_budget_boundaries_stop_before_unplanned_partial_expansion(
    tmp_path,
    maximum_requests: int,
    maximum_expanded: int,
    expected: k.CitationCoverageBlocker,
) -> None:
    policy = build_citation_policy(
        maximum_requests=maximum_requests,
        maximum_expanded_papers=maximum_expanded,
    )
    initial, executor, _adapters, _neighbors = await _initial_execution(tmp_path, policy)
    campaign = await k.run_citation_traversal(
        campaign_id=f"f8s2-budget-{expected.value}",
        policy=policy,
        initial_execution=initial,
        executor=executor,
    )
    assert campaign.stopping_reason is k.SearchStoppingReason.BUDGET_EXHAUSTED
    assert campaign.coverage_disposition == "blocked"
    assert expected in campaign.blockers
    assert len(campaign.executions) == 1


@pytest.mark.asyncio
async def test_maximum_rounds_without_saturation_is_not_coverage(tmp_path) -> None:
    policy = build_citation_policy(maximum_rounds=2)
    plan = build_search_plan(
        citation_traversal_policy_sha256=policy.policy_sha256
    )
    adapters = build_adapters(plan)
    first = sha("chain-first")
    second = sha("chain-second")
    graph: dict[tuple[str, k.QueryFamily], tuple[str, ...]] = {}
    for seed in plan.protocol.seed_paper_snapshot_sha256s:
        graph[(seed, k.QueryFamily.CITATION_BACKWARD)] = (first,)
        graph[(seed, k.QueryFamily.CITATION_FORWARD)] = ()
    graph[(first, k.QueryFamily.CITATION_BACKWARD)] = (second,)
    graph[(first, k.QueryFamily.CITATION_FORWARD)] = ()
    for adapter in adapters.values():
        adapter.citation_graph = graph
    executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    )
    initial = await executor.execute(plan=plan, execution_id="f8s2-chain-initial")
    campaign = await k.run_citation_traversal(
        campaign_id="f8s2-max-rounds",
        policy=policy,
        initial_execution=initial,
        executor=executor,
    )
    assert campaign.stopping_reason is k.SearchStoppingReason.BUDGET_EXHAUSTED
    assert campaign.blockers == (
        k.CitationCoverageBlocker.MAXIMUM_ROUNDS_WITHOUT_SATURATION,
    )
    assert second in campaign.reached_paper_snapshot_sha256s


@pytest.mark.asyncio
async def test_policy_must_be_frozen_into_initial_plan(tmp_path) -> None:
    frozen_policy = build_citation_policy()
    plan = build_search_plan(
        citation_traversal_policy_sha256=frozen_policy.policy_sha256
    )
    adapters = build_adapters(plan)
    executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    )
    initial = await executor.execute(plan=plan, execution_id="f8s2-policy-binding")
    different = k.CitationTraversalPolicy.model_validate(
        {
            **frozen_policy.model_dump(mode="python"),
            "maximum_requests": frozen_policy.maximum_requests - 1,
        }
    )
    with pytest.raises(ValueError, match="not frozen with this citation policy"):
        await k.run_citation_traversal(
            campaign_id="f8s2-policy-drift",
            policy=different,
            initial_execution=initial,
            executor=executor,
        )
