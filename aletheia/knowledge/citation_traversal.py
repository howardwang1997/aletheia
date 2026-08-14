"""Deterministic forward/backward citation traversal with saturation evidence."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.schemas import (
    KnowledgeModel,
    QueryFamily,
    QueryOutcome,
    SaturationRule,
    SearchStoppingReason,
)
from aletheia.knowledge.response_archive import ArchivedSearchLedger
from aletheia.knowledge.search import build_citation_round_execution_plan
from aletheia.knowledge.search_execution import (
    ReplayAuditStatus,
    SearchExecutionBundle,
    SearchExecutor,
    SearchReplayAudit,
    replay_search_execution,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


_CITATION_FAMILIES = {QueryFamily.CITATION_BACKWARD, QueryFamily.CITATION_FORWARD}


class CitationCoverageBlocker(str, Enum):
    INITIAL_EXECUTION_INCOMPLETE = "initial_execution_incomplete"
    ROUND_EXECUTION_INCOMPLETE = "round_execution_incomplete"
    REPLAY_INCOMPLETE = "replay_incomplete"
    REQUEST_BUDGET_EXHAUSTED = "request_budget_exhausted"
    FRONTIER_BUDGET_EXHAUSTED = "frontier_budget_exhausted"
    MAXIMUM_ROUNDS_WITHOUT_SATURATION = "maximum_rounds_without_saturation"


_BLOCKER_ORDER = tuple(CitationCoverageBlocker)


class CitationTraversalPolicy(KnowledgeModel):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    saturation_rule: SaturationRule
    maximum_requests: int = Field(ge=1, le=100_000)
    maximum_expanded_papers: int = Field(ge=1, le=100_000)
    require_backward_citations: Literal[True] = True
    require_forward_citations: Literal[True] = True
    frontier_order: Literal["paper_sha256_ascending"] = "paper_sha256_ascending"
    marginal_fraction_denominator: Literal["unique_discovered_this_round"] = (
        "unique_discovered_this_round"
    )
    derivation: Literal["all_new_hits_no_model_selection"] = (
        "all_new_hits_no_model_selection"
    )
    failure_policy: Literal["record_and_fail_hard_coverage"] = (
        "record_and_fail_hard_coverage"
    )
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @property
    def policy_sha256(self) -> str:
        return content_sha256(self)


class CitationTraversalRound(KnowledgeModel):
    schema_version: Literal[1] = 1
    round_index: int = Field(ge=0, le=100)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_paper_snapshot_sha256s: tuple[str, ...] = Field(min_length=1)
    backward_discovered_sha256s: tuple[str, ...]
    forward_discovered_sha256s: tuple[str, ...]
    new_paper_snapshot_sha256s: tuple[str, ...]
    cumulative_paper_snapshot_sha256s: tuple[str, ...] = Field(min_length=1)
    response_sha256s: tuple[str, ...]
    marginal_new_fraction: float = Field(ge=0.0, le=1.0)
    saturated: bool
    request_count: int = Field(ge=1)
    failure_sha256s: tuple[str, ...]

    @model_validator(mode="after")
    def _round_is_canonical(self) -> "CitationTraversalRound":
        fields = (
            self.frontier_paper_snapshot_sha256s,
            self.backward_discovered_sha256s,
            self.forward_discovered_sha256s,
            self.new_paper_snapshot_sha256s,
            self.cumulative_paper_snapshot_sha256s,
            self.response_sha256s,
            self.failure_sha256s,
        )
        if any(values != tuple(sorted(set(values))) for values in fields):
            raise ValueError("citation round identity sets must be unique and sorted")
        discovered = set(self.backward_discovered_sha256s) | set(
            self.forward_discovered_sha256s
        )
        if not set(self.new_paper_snapshot_sha256s) <= discovered:
            raise ValueError("new citation papers must occur in this round's discoveries")
        if not set(self.frontier_paper_snapshot_sha256s) <= set(
            self.cumulative_paper_snapshot_sha256s
        ):
            raise ValueError("citation frontier must remain in cumulative closure")
        expected_fraction = len(self.new_paper_snapshot_sha256s) / max(1, len(discovered))
        if abs(self.marginal_new_fraction - expected_fraction) > 1e-12:
            raise ValueError("citation marginal-new fraction is not derived from discoveries")
        return self

    @property
    def round_sha256(self) -> str:
        return content_sha256(self)


class CitationTraversalCampaign(KnowledgeModel):
    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    policy: CitationTraversalPolicy
    initial_execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executions: tuple[SearchExecutionBundle, ...] = Field(min_length=1)
    execution_ledgers: tuple[ArchivedSearchLedger, ...] = Field(min_length=1)
    replay_audits: tuple[SearchReplayAudit, ...] = Field(min_length=1)
    rounds: tuple[CitationTraversalRound, ...] = Field(min_length=1)
    reached_paper_snapshot_sha256s: tuple[str, ...] = Field(min_length=1)
    stopping_reason: SearchStoppingReason
    blockers: tuple[CitationCoverageBlocker, ...]
    coverage_disposition: Literal["eligible", "blocked"]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _campaign_is_a_closed_derivation_chain(self) -> "CitationTraversalCampaign":
        if not (
            len(self.executions)
            == len(self.execution_ledgers)
            == len(self.replay_audits)
            == len(self.rounds)
        ):
            raise ValueError(
                "citation executions, ledgers, replays, and rounds must be one-to-one"
            )
        if self.ended_at < self.started_at:
            raise ValueError("citation traversal ended before it started")
        if self.executions[0].execution_sha256 != self.initial_execution_sha256:
            raise ValueError("citation campaign starts from a different initial execution")
        if (
            self.executions[0].plan.citation_traversal_policy_sha256
            != self.policy.policy_sha256
        ):
            raise ValueError("initial search was not frozen with this citation policy")
        for index, (execution, ledger, audit, round_result) in enumerate(
            zip(
                self.executions,
                self.execution_ledgers,
                self.replay_audits,
                self.rounds,
                strict=True,
            )
        ):
            if round_result.round_index != index:
                raise ValueError("citation rounds must be contiguous from zero")
            if (
                execution.execution_sha256 != round_result.execution_sha256
                or execution.plan.plan_sha256 != round_result.plan_sha256
                or audit.execution_sha256 != execution.execution_sha256
                or audit.audit_sha256 != round_result.replay_audit_sha256
            ):
                raise ValueError("citation round does not bind its plan, execution, and replay")
            execution_payload = canonical_json_bytes(execution)
            if (
                ledger.object_sha256 != execution.execution_sha256
                or ledger.ledger_sha256
                != hashlib.sha256(execution_payload).hexdigest()
                or ledger.ledger_bytes != len(execution_payload)
            ):
                raise ValueError("citation execution ledger does not commit its exact execution")
            if index == 0:
                if execution.plan.plan_kind != "initial_search":
                    raise ValueError("citation campaign round zero must be the initial search")
            else:
                if (
                    execution.plan.plan_kind != "citation_round"
                    or execution.plan.parent_execution_sha256
                    != self.executions[index - 1].execution_sha256
                    or execution.plan.derivation_policy_sha256
                    != self.policy.policy_sha256
                    or execution.plan.citation_frontier_sha256s
                    != round_result.frontier_paper_snapshot_sha256s
                ):
                    raise ValueError("derived citation round breaks the mechanical parent chain")
        if self.reached_paper_snapshot_sha256s != tuple(
            sorted(set(self.reached_paper_snapshot_sha256s))
        ):
            raise ValueError("reached citation papers must be unique and sorted")
        if self.reached_paper_snapshot_sha256s != self.rounds[-1].cumulative_paper_snapshot_sha256s:
            raise ValueError("campaign reach must equal the final round closure")
        expected_blockers = tuple(
            sorted(set(self.blockers), key=_BLOCKER_ORDER.index)
        )
        if self.blockers != expected_blockers:
            raise ValueError("citation blockers must be unique and canonically ordered")
        evidence_complete = (
            not self.blockers
            and all(
                execution.coverage_disposition == "eligible"
                for execution in self.executions
            )
            and all(audit.status is ReplayAuditStatus.COMPLETE for audit in self.replay_audits)
            and self.stopping_reason
            in {SearchStoppingReason.SATURATION, SearchStoppingReason.SOURCE_EXHAUSTED}
        )
        expected_disposition = "eligible" if evidence_complete else "blocked"
        if self.coverage_disposition != expected_disposition:
            raise ValueError("citation coverage disposition does not match campaign evidence")
        if (self.stopping_reason is SearchStoppingReason.HARD_FAILURE) != bool(
            set(self.blockers)
            & {
                CitationCoverageBlocker.INITIAL_EXECUTION_INCOMPLETE,
                CitationCoverageBlocker.ROUND_EXECUTION_INCOMPLETE,
                CitationCoverageBlocker.REPLAY_INCOMPLETE,
            }
        ):
            raise ValueError("citation hard-failure stopping reason does not match blockers")
        return self

    @property
    def campaign_sha256(self) -> str:
        return content_sha256(self)


def _citation_query_map(execution: SearchExecutionBundle):
    return {
        query.logical_query_sha256: query
        for query in execution.plan.queries
        if query.family in _CITATION_FAMILIES
    }


def _citation_request_count(execution: SearchExecutionBundle) -> int:
    query_map = _citation_query_map(execution)
    return sum(
        receipt.logical_query_sha256 in query_map
        for receipt in execution.page_receipts
    )


def _round_evidence(
    *,
    round_index: int,
    execution: SearchExecutionBundle,
    replay_audit: SearchReplayAudit,
    frontier: tuple[str, ...],
    reached_before: set[str],
    saturation_rule: SaturationRule,
) -> CitationTraversalRound:
    query_map = _citation_query_map(execution)
    record_by_id = {record.query_id: record for record in execution.session.queries}
    backward: set[str] = set()
    forward: set[str] = set()
    responses: set[str] = set()
    failure_hashes: set[str] = set()
    for receipt in execution.page_receipts:
        query = query_map.get(receipt.logical_query_sha256)
        if query is None:
            continue
        if receipt.failure_sha256 is not None:
            failure_hashes.add(receipt.failure_sha256)
        if receipt.outcome is not QueryOutcome.SUCCESS:
            continue
        record = record_by_id[receipt.request_id]
        destination = (
            backward
            if query.family is QueryFamily.CITATION_BACKWARD
            else forward
        )
        destination.update(hit.paper_snapshot_sha256 for hit in record.hits)
        if receipt.response is not None:
            responses.add(receipt.response.response_sha256)
    discovered = backward | forward
    new_papers = discovered - reached_before
    cumulative = reached_before | discovered
    marginal = len(new_papers) / max(1, len(discovered))
    saturated = (
        round_index + 1 >= saturation_rule.minimum_rounds
        and marginal <= saturation_rule.marginal_new_relevant_fraction
    )
    return CitationTraversalRound(
        round_index=round_index,
        plan_sha256=execution.plan.plan_sha256,
        execution_sha256=execution.execution_sha256,
        replay_audit_sha256=replay_audit.audit_sha256,
        frontier_paper_snapshot_sha256s=frontier,
        backward_discovered_sha256s=tuple(sorted(backward)),
        forward_discovered_sha256s=tuple(sorted(forward)),
        new_paper_snapshot_sha256s=tuple(sorted(new_papers)),
        cumulative_paper_snapshot_sha256s=tuple(sorted(cumulative)),
        response_sha256s=tuple(sorted(responses)),
        marginal_new_fraction=marginal,
        saturated=saturated,
        request_count=_citation_request_count(execution),
        failure_sha256s=tuple(sorted(failure_hashes)),
    )


def _estimated_round_requests(execution: SearchExecutionBundle, frontier_size: int) -> int:
    total = 0
    for manifest in execution.plan.adapters:
        directions = sum(
            family in manifest.supports_query_families for family in _CITATION_FAMILIES
        )
        total += directions * frontier_size * manifest.max_pages
    return total


async def run_citation_traversal(
    *,
    campaign_id: str,
    policy: CitationTraversalPolicy,
    initial_execution: SearchExecutionBundle,
    executor: SearchExecutor,
) -> CitationTraversalCampaign:
    """Expand every new citation hit in hash order until saturation or a hard boundary."""

    if initial_execution.plan.plan_kind != "initial_search":
        raise ValueError("citation traversal requires an initial-search execution")
    if (
        initial_execution.plan.citation_traversal_policy_sha256
        != policy.policy_sha256
    ):
        raise ValueError("initial search plan was not frozen with this citation policy")
    started_at = initial_execution.session.started_at
    executions = [initial_execution]
    execution_ledgers = [
        executor.archive.store_ledger(
            value=initial_execution,
            object_sha256=initial_execution.execution_sha256,
            archived_at=executor._now(),
        )
    ]
    initial_audit = replay_search_execution(
        execution=initial_execution,
        archive=executor.archive,
        adapters=executor.adapters,
        audited_at=executor._now(),
    )
    audits = [initial_audit]
    initial_frontier = tuple(
        sorted(initial_execution.plan.protocol.seed_paper_snapshot_sha256s)
    )
    reached_before = set(initial_frontier)
    first_round = _round_evidence(
        round_index=0,
        execution=initial_execution,
        replay_audit=initial_audit,
        frontier=initial_frontier,
        reached_before=reached_before,
        saturation_rule=policy.saturation_rule,
    )
    rounds = [first_round]
    reached = set(first_round.cumulative_paper_snapshot_sha256s)
    total_requests = first_round.request_count
    expanded_papers = len(initial_frontier)
    consecutive_saturated = 1 if first_round.saturated else 0
    blockers: set[CitationCoverageBlocker] = set()
    stopping_reason: SearchStoppingReason | None = None

    if total_requests > policy.maximum_requests:
        blockers.add(CitationCoverageBlocker.REQUEST_BUDGET_EXHAUSTED)
        stopping_reason = SearchStoppingReason.BUDGET_EXHAUSTED
    if expanded_papers > policy.maximum_expanded_papers:
        blockers.add(CitationCoverageBlocker.FRONTIER_BUDGET_EXHAUSTED)
        stopping_reason = SearchStoppingReason.BUDGET_EXHAUSTED
    if initial_execution.coverage_disposition != "eligible":
        blockers.add(CitationCoverageBlocker.INITIAL_EXECUTION_INCOMPLETE)
    if initial_audit.status is not ReplayAuditStatus.COMPLETE:
        blockers.add(CitationCoverageBlocker.REPLAY_INCOMPLETE)
    if blockers & {
        CitationCoverageBlocker.INITIAL_EXECUTION_INCOMPLETE,
        CitationCoverageBlocker.REPLAY_INCOMPLETE,
    }:
        stopping_reason = SearchStoppingReason.HARD_FAILURE
    frontier = first_round.new_paper_snapshot_sha256s
    if stopping_reason is None and not frontier:
        stopping_reason = SearchStoppingReason.SOURCE_EXHAUSTED

    while stopping_reason is None:
        round_index = len(rounds)
        if round_index >= policy.saturation_rule.maximum_rounds:
            blockers.add(CitationCoverageBlocker.MAXIMUM_ROUNDS_WITHOUT_SATURATION)
            stopping_reason = SearchStoppingReason.BUDGET_EXHAUSTED
            break
        estimated_requests = _estimated_round_requests(initial_execution, len(frontier))
        if total_requests + estimated_requests > policy.maximum_requests:
            blockers.add(CitationCoverageBlocker.REQUEST_BUDGET_EXHAUSTED)
            stopping_reason = SearchStoppingReason.BUDGET_EXHAUSTED
            break
        if expanded_papers + len(frontier) > policy.maximum_expanded_papers:
            blockers.add(CitationCoverageBlocker.FRONTIER_BUDGET_EXHAUSTED)
            stopping_reason = SearchStoppingReason.BUDGET_EXHAUSTED
            break

        parent = executions[-1]
        round_plan = build_citation_round_execution_plan(
            plan_id=f"{campaign_id}.citation.{round_index}",
            protocol=initial_execution.plan.protocol,
            term_set=initial_execution.plan.term_set,
            adapters=initial_execution.plan.adapters,
            frontier_paper_snapshot_sha256s=frontier,
            parent_execution_sha256=parent.execution_sha256,
            derivation_policy_sha256=policy.policy_sha256,
            round_index=round_index,
            frozen_at=executor._now(),
        )
        round_commit = await executor.execute_and_commit(
            plan=round_plan,
            execution_id=f"{campaign_id}.citation.{round_index}",
        )
        round_execution = round_commit.execution
        round_audit = replay_search_execution(
            execution=round_execution,
            archive=executor.archive,
            adapters=executor.adapters,
            audited_at=executor._now(),
        )
        round_result = _round_evidence(
            round_index=round_index,
            execution=round_execution,
            replay_audit=round_audit,
            frontier=frontier,
            reached_before=reached,
            saturation_rule=policy.saturation_rule,
        )
        executions.append(round_execution)
        execution_ledgers.append(round_commit.ledger)
        audits.append(round_audit)
        rounds.append(round_result)
        reached = set(round_result.cumulative_paper_snapshot_sha256s)
        total_requests += round_result.request_count
        expanded_papers += len(frontier)

        if round_execution.coverage_disposition != "eligible":
            blockers.add(CitationCoverageBlocker.ROUND_EXECUTION_INCOMPLETE)
        if round_audit.status is not ReplayAuditStatus.COMPLETE:
            blockers.add(CitationCoverageBlocker.REPLAY_INCOMPLETE)
        if blockers:
            stopping_reason = SearchStoppingReason.HARD_FAILURE
            break
        consecutive_saturated = (
            consecutive_saturated + 1 if round_result.saturated else 0
        )
        if (
            consecutive_saturated
            >= policy.saturation_rule.consecutive_saturated_rounds
        ):
            stopping_reason = SearchStoppingReason.SATURATION
            break
        frontier = round_result.new_paper_snapshot_sha256s
        if not frontier:
            stopping_reason = SearchStoppingReason.SOURCE_EXHAUSTED
            break

    assert stopping_reason is not None
    ordered_blockers = tuple(sorted(blockers, key=_BLOCKER_ORDER.index))
    eligible = (
        not ordered_blockers
        and stopping_reason
        in {SearchStoppingReason.SATURATION, SearchStoppingReason.SOURCE_EXHAUSTED}
    )
    return CitationTraversalCampaign(
        campaign_id=campaign_id,
        policy=policy,
        initial_execution_sha256=initial_execution.execution_sha256,
        executions=tuple(executions),
        execution_ledgers=tuple(execution_ledgers),
        replay_audits=tuple(audits),
        rounds=tuple(rounds),
        reached_paper_snapshot_sha256s=tuple(sorted(reached)),
        stopping_reason=stopping_reason,
        blockers=ordered_blockers,
        coverage_disposition="eligible" if eligible else "blocked",
        started_at=started_at,
        ended_at=executor._now(),
    )


__all__ = [
    "CitationCoverageBlocker",
    "CitationTraversalCampaign",
    "CitationTraversalPolicy",
    "CitationTraversalRound",
    "run_citation_traversal",
]
