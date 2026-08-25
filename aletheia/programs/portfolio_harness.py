"""Deterministic hard filters and shadow-only portfolio batch selection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from aletheia.programs.portfolio_schemas import (
    INFORMATION_ACTIONS,
    HumanPortfolioPlanSpec,
    PortfolioActionSpec,
    PortfolioActionType,
    PortfolioBudgetAvailability,
    PortfolioBudgetProjection,
    PortfolioCandidateAssessment,
    PortfolioCandidateScore,
    PortfolioEpochDisposition,
    PortfolioInformationAudit,
    PortfolioInformationModel,
    PortfolioRiskLevel,
    PortfolioSelectionDecision,
    PortfolioSelectionEntry,
    PortfolioSelectionPolicy,
    PortfolioShadowComparison,
    PortfolioSlateSpec,
)
from aletheia.programs.schemas import (
    BudgetKind,
    GraphNodeState,
    GraphNodeType,
    QuestGraphSnapshot,
)
from aletheia.reproducibility.manifest import content_sha256

_PPM = 1_000_000
PORTFOLIO_SELECTOR_CODE_SHA256 = content_sha256(
    {
        "schema": "aletheia.shadow_portfolio_selector.v1",
        "probability_arithmetic": "decimal_ppm_entropy_round_half_even",
        "hard_filter_contract": 1,
        "greedy_batch_contract": 1,
        "tie_break_contract": 1,
    }
)
_EXPERIMENT_ACTIONS = frozenset(
    {
        PortfolioActionType.ADVANCE_CAMPAIGN,
        PortfolioActionType.DISCRIMINATING_EXPERIMENT,
        PortfolioActionType.REPLICATION,
        PortfolioActionType.MECHANISM_TEST,
    }
)
_RISK_ORDER = {
    PortfolioRiskLevel.NEGLIGIBLE: 0,
    PortfolioRiskLevel.LOW: 1,
    PortfolioRiskLevel.MODERATE: 2,
    PortfolioRiskLevel.HIGH: 3,
    PortfolioRiskLevel.PROHIBITED: 4,
}
_RISK_BURDEN_PPM = {
    PortfolioRiskLevel.NEGLIGIBLE: 0,
    PortfolioRiskLevel.LOW: 250_000,
    PortfolioRiskLevel.MODERATE: 500_000,
    PortfolioRiskLevel.HIGH: 750_000,
    PortfolioRiskLevel.PROHIBITED: _PPM,
}


@dataclass(frozen=True)
class DerivedPortfolioEpoch:
    scores: tuple[PortfolioCandidateScore, ...]
    decision: PortfolioSelectionDecision
    comparison: PortfolioShadowComparison


def _ratio_ppm(numerator: int, denominator: int) -> int:
    if numerator <= 0 or denominator <= 0:
        return 0
    with localcontext() as context:
        context.prec = 60
        value = (Decimal(numerator) * Decimal(_PPM) / Decimal(denominator)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
    return min(_PPM, max(0, int(value)))


def _weighted(weight_ppm: int, value_ppm: int) -> int:
    with localcontext() as context:
        context.prec = 60
        value = (Decimal(weight_ppm) * Decimal(value_ppm) / Decimal(_PPM)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
    return int(value)


def _entropy(probabilities: tuple[Decimal, ...]) -> Decimal:
    return -sum((value * value.ln() for value in probabilities if value > 0), Decimal(0))


def derive_information_audit(model: PortfolioInformationModel) -> PortfolioInformationAudit:
    """Recompute discrete expected information gain without floating-point authority."""

    with localcontext() as context:
        context.prec = 60
        priors = {
            item.hypothesis_id: Decimal(item.probability_ppm) / Decimal(_PPM)
            for item in model.priors
        }
        likelihoods = {
            item.hypothesis_id: {
                outcome.outcome_id: Decimal(outcome.probability_ppm) / Decimal(_PPM)
                for outcome in item.outcomes
            }
            for item in model.likelihoods
        }
        hypothesis_ids = tuple(sorted(priors))
        outcome_ids = tuple(sorted(next(iter(likelihoods.values()))))
        prior_entropy = _entropy(tuple(priors[item] for item in hypothesis_ids))
        expected_posterior = Decimal(0)
        for outcome_id in outcome_ids:
            marginal = sum(
                (
                    priors[hypothesis_id] * likelihoods[hypothesis_id][outcome_id]
                    for hypothesis_id in hypothesis_ids
                ),
                Decimal(0),
            )
            if marginal <= 0:
                continue
            posterior = tuple(
                priors[hypothesis_id] * likelihoods[hypothesis_id][outcome_id] / marginal
                for hypothesis_id in hypothesis_ids
            )
            expected_posterior += marginal * _entropy(posterior)
        information_gain = max(Decimal(0), prior_entropy - expected_posterior)
        prior_micros = int(
            (prior_entropy * Decimal(_PPM)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        )
        information_micros = min(
            prior_micros,
            int((information_gain * Decimal(_PPM)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)),
        )
    return PortfolioInformationAudit(
        information_model_sha256=model.information_model_sha256,
        prior_entropy_micronats=prior_micros,
        expected_posterior_entropy_micronats=prior_micros - information_micros,
        expected_information_gain_micronats=information_micros,
        expected_information_gain_ratio_ppm=_ratio_ppm(information_micros, prior_micros),
    )


def _graph_maps(graph: QuestGraphSnapshot):
    nodes = {item.node_id: item for item in graph.nodes}
    campaign_families = {item.campaign_id: item.family_id for item in graph.campaign_families}
    families = {item.family_id: item for item in graph.scientific_families}
    dependencies: dict[str, set[str]] = defaultdict(set)
    for item in graph.dependencies:
        dependencies[item.node_id].add(item.dependency_node_id)
    return nodes, campaign_families, families, dependencies


def _scientifically_completed(graph: QuestGraphSnapshot, node_id: str) -> bool:
    node = next((item for item in graph.nodes if item.node_id == node_id), None)
    if node is None:
        return False
    if node.state is GraphNodeState.COMPLETED:
        return True
    if node.state is not GraphNodeState.ARCHIVED:
        return False
    last = max(
        (item for item in graph.transitions if item.node_id == node_id),
        key=lambda item: item.to_version,
        default=None,
    )
    return last is not None and last.from_state is GraphNodeState.COMPLETED


def candidate_program_and_family(
    graph: QuestGraphSnapshot,
    action: PortfolioActionSpec,
) -> tuple[str, str | None]:
    """Resolve the Program and scientific family from a frozen graph snapshot."""

    nodes, campaign_families, families, _ = _graph_maps(graph)
    target = nodes.get(action.target_node_id)
    if target is None:
        raise ValueError(f"portfolio target is absent from graph: {action.target_node_id}")
    if target.node_type is GraphNodeType.PROGRAM:
        program_id = target.node_id
        derived_family = action.family_id
    elif target.node_type is GraphNodeType.CAMPAIGN:
        if target.parent_node_id is None:
            raise ValueError(f"portfolio Campaign has no Program: {target.node_id}")
        program_id = target.parent_node_id
        derived_family = campaign_families.get(target.node_id)
        if derived_family is None:
            raise ValueError(f"portfolio Campaign has no scientific family: {target.node_id}")
        if action.family_id is not None and action.family_id != derived_family:
            raise ValueError("portfolio action changed its Campaign scientific family")
    else:
        raise ValueError("portfolio actions cannot target the Quest directly")
    if derived_family is not None:
        family = families.get(derived_family)
        if family is None or family.program_id != program_id:
            raise ValueError("portfolio action family belongs to another Program")
    return program_id, derived_family


def _candidate_score(
    *,
    policy: PortfolioSelectionPolicy,
    graph: QuestGraphSnapshot,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    action: PortfolioActionSpec,
    assessment: PortfolioCandidateAssessment,
    evaluated_at: datetime,
) -> PortfolioCandidateScore:
    nodes, _, _, dependencies = _graph_maps(graph)
    target = nodes.get(action.target_node_id)
    quest = nodes.get(graph.quest_id)
    program_id, family_id = candidate_program_and_family(graph, action)
    program = nodes[program_id]
    blockers: list[str] = []

    if evaluated_at < assessment.completed_at:
        blockers.append("assessment:completed_in_future")
    if quest is None or quest.state is not GraphNodeState.ACTIVE:
        blockers.append("graph:quest_not_active")
    if program.state is not GraphNodeState.ACTIVE and action.action_type not in {
        PortfolioActionType.PAUSE_PROGRAM,
        PortfolioActionType.STOP_PROGRAM,
    }:
        blockers.append(f"graph:program_state:{program.state.value}")
    if action.action_type in _EXPERIMENT_ACTIONS:
        assert target is not None
        if target.state is not GraphNodeState.ACTIVE:
            blockers.append(f"graph:campaign_state:{target.state.value}")
    elif action.action_type is PortfolioActionType.START_CAMPAIGN:
        if program.state is not GraphNodeState.ACTIVE:
            blockers.append(f"graph:start_campaign_from:{program.state.value}")
    elif action.action_type is PortfolioActionType.PAUSE_PROGRAM:
        if program.state is not GraphNodeState.ACTIVE:
            blockers.append(f"graph:pause_program_from:{program.state.value}")
    elif action.action_type is PortfolioActionType.STOP_PROGRAM:
        if program.state not in {
            GraphNodeState.PROPOSED,
            GraphNodeState.ACTIVE,
            GraphNodeState.PAUSED,
        }:
            blockers.append(f"graph:stop_program_from:{program.state.value}")

    dependency_nodes = set(dependencies.get(program_id, set()))
    dependency_nodes.update(dependencies.get(action.target_node_id, set()))
    blockers.extend(
        f"dependency:not_completed:{item}"
        for item in sorted(dependency_nodes)
        if not _scientifically_completed(graph, item)
    )

    available_roles = {
        item.role
        for item in graph.data_allocations
        if item.scope_node_id in {graph.quest_id, program_id}
    }
    blockers.extend(
        f"data:missing_role:{item.value}"
        for item in assessment.required_data_roles
        if item not in available_roles
    )
    missing_capabilities = sorted(
        set(assessment.required_capability_sha256s) - set(assessment.available_capability_sha256s)
    )
    blockers.extend(f"capability:missing:{item}" for item in missing_capabilities)
    if (
        policy.require_validated_measurement
        and action.action_type in _EXPERIMENT_ACTIONS
        and assessment.measurement_status.value != "validated"
    ):
        blockers.append(f"measurement:not_validated:{assessment.measurement_status.value}")
    if assessment.estimated_duration_seconds > policy.maximum_duration_seconds:
        blockers.append("duration:limit_exceeded")
    if assessment.risk_level is PortfolioRiskLevel.PROHIBITED:
        blockers.append("risk:prohibited")
    elif _RISK_ORDER[assessment.risk_level] > _RISK_ORDER[policy.maximum_risk_level]:
        blockers.append("risk:level_exceeded")
    if assessment.risk_level in policy.required_approval_risks:
        approval = assessment.approval
        if approval is None:
            blockers.append("approval:missing")
        elif approval.issued_at > evaluated_at:
            blockers.append("approval:not_yet_issued")
        elif approval.expires_at <= evaluated_at:
            blockers.append("approval:expired")

    budgets = {(item.program_id, item.kind): item for item in budget_state}
    cost_ratios: list[int] = []
    for cost in assessment.estimated_costs:
        available = budgets.get((program_id, cost.kind))
        if available is None:
            blockers.append(f"budget:program_allocation_missing:{cost.kind.value}")
            cost_ratios.append(_PPM)
        elif cost.amount_microunits > available.available_microunits:
            blockers.append(f"budget:available_exceeded:{cost.kind.value}")
            cost_ratios.append(_PPM)
        else:
            cost_ratios.append(_ratio_ppm(cost.amount_microunits, available.available_microunits))

    information_audit = (
        derive_information_audit(assessment.information_model)
        if assessment.information_model is not None
        else None
    )
    if action.action_type in INFORMATION_ACTIONS:
        if information_audit is None:
            blockers.append("information:model_missing")
        elif (
            information_audit.expected_information_gain_ratio_ppm
            < policy.minimum_expected_information_gain_ratio_ppm
        ):
            blockers.append("information:eig_below_floor")

    replication_ppm = _ratio_ppm(
        assessment.expected_replication_debt_reduction,
        assessment.replication_debt_before,
    )
    cost_ppm = max(cost_ratios, default=0)
    duration_ppm = _ratio_ppm(
        assessment.estimated_duration_seconds,
        policy.maximum_duration_seconds,
    )
    risk_ppm = _RISK_BURDEN_PPM[assessment.risk_level]
    information_ppm = (
        information_audit.expected_information_gain_ratio_ppm
        if information_audit is not None
        else 0
    )
    weights = policy.weights
    base_utility = sum(
        (
            _weighted(weights.expected_information_gain, information_ppm),
            _weighted(weights.importance, assessment.importance_ppm),
            _weighted(weights.novelty, assessment.novelty_ppm),
            _weighted(weights.success_probability, assessment.success_probability_ppm),
            _weighted(weights.replication_debt_reduction, replication_ppm),
        )
    ) - sum(
        (
            _weighted(weights.cost_penalty, cost_ppm),
            _weighted(weights.duration_penalty, duration_ppm),
            _weighted(weights.risk_penalty, risk_ppm),
        )
    )
    canonical_blockers = tuple(sorted(set(blockers)))
    return PortfolioCandidateScore(
        candidate_id=action.candidate_id,
        action_sha256=action.action_sha256,
        assessment_sha256=assessment.assessment_sha256,
        program_id=program_id,
        family_id=family_id,
        information_audit=information_audit,
        importance_ppm=assessment.importance_ppm,
        novelty_ppm=assessment.novelty_ppm,
        success_probability_ppm=assessment.success_probability_ppm,
        replication_debt_reduction_ppm=replication_ppm,
        cost_burden_ppm=cost_ppm,
        duration_burden_ppm=duration_ppm,
        risk_burden_ppm=risk_ppm,
        base_utility_microscore=base_utility,
        feasible=not canonical_blockers,
        blockers=canonical_blockers,
    )


def derive_candidate_scores(
    *,
    spec: PortfolioSlateSpec,
    graph: QuestGraphSnapshot,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    evaluated_at: datetime,
) -> tuple[PortfolioCandidateScore, ...]:
    assessments = {item.candidate_id: item for item in spec.assessment_batch.assessments}
    return tuple(
        _candidate_score(
            policy=spec.policy,
            graph=graph,
            budget_state=budget_state,
            action=action,
            assessment=assessments[action.candidate_id],
            evaluated_at=evaluated_at,
        )
        for action in spec.proposal.candidates
    )


@dataclass
class _BatchState:
    selected: list[str]
    targets: set[str]
    program_counts: Counter[str]
    family_counts: Counter[str]
    correlation_counts: Counter[str]
    diversity_tags: set[str]
    remaining_budget: dict[tuple[str, BudgetKind], int]


def _new_batch_state(
    budget_state: tuple[PortfolioBudgetAvailability, ...],
) -> _BatchState:
    return _BatchState(
        selected=[],
        targets=set(),
        program_counts=Counter(),
        family_counts=Counter(),
        correlation_counts=Counter(),
        diversity_tags=set(),
        remaining_budget={
            (item.program_id, item.kind): item.available_microunits for item in budget_state
        },
    )


def _batch_blockers(
    *,
    state: _BatchState,
    policy: PortfolioSelectionPolicy,
    action: PortfolioActionSpec,
    assessment: PortfolioCandidateAssessment,
    score: PortfolioCandidateScore,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if len(state.selected) >= policy.maximum_selected_actions:
        blockers.append("batch:action_cap_reached")
    if action.target_node_id in state.targets:
        blockers.append("batch:target_conflict")
    if state.program_counts[score.program_id] >= policy.maximum_actions_per_program:
        blockers.append("batch:program_cap_reached")
    if score.family_id is not None and (
        state.family_counts[score.family_id] >= policy.maximum_actions_per_family
    ):
        blockers.append("batch:family_cap_reached")
    blockers.extend(
        f"batch:correlation_cap:{tag}"
        for tag in assessment.correlation_tags
        if state.correlation_counts[tag] >= policy.maximum_actions_per_correlation_tag
    )
    for cost in assessment.estimated_costs:
        remaining = state.remaining_budget.get((score.program_id, cost.kind), 0)
        if cost.amount_microunits > remaining:
            blockers.append(f"batch:budget_exceeded:{cost.kind.value}")
    return tuple(sorted(set(blockers)))


def _diversity_bonus(
    *,
    policy: PortfolioSelectionPolicy,
    assessment: PortfolioCandidateAssessment,
    state: _BatchState,
) -> int:
    tags = set(assessment.diversity_tags)
    if not tags:
        return 0
    novelty_ppm = len(tags - state.diversity_tags) * _PPM // len(tags)
    return _weighted(policy.weights.diversity, novelty_ppm)


def _add_to_batch(
    *,
    state: _BatchState,
    action: PortfolioActionSpec,
    assessment: PortfolioCandidateAssessment,
    score: PortfolioCandidateScore,
) -> None:
    state.selected.append(action.candidate_id)
    state.targets.add(action.target_node_id)
    state.program_counts[score.program_id] += 1
    if score.family_id is not None:
        state.family_counts[score.family_id] += 1
    state.correlation_counts.update(assessment.correlation_tags)
    state.diversity_tags.update(assessment.diversity_tags)
    for cost in assessment.estimated_costs:
        key = (score.program_id, cost.kind)
        state.remaining_budget[key] -= cost.amount_microunits


def _choose_next(
    *,
    candidates: set[str],
    policy: PortfolioSelectionPolicy,
    actions: dict[str, PortfolioActionSpec],
    assessments: dict[str, PortfolioCandidateAssessment],
    scores: dict[str, PortfolioCandidateScore],
    state: _BatchState,
) -> tuple[str, int] | None:
    choices: list[tuple[tuple[object, ...], str, int]] = []
    for candidate_id in candidates:
        action = actions[candidate_id]
        assessment = assessments[candidate_id]
        score = scores[candidate_id]
        if not score.feasible or _batch_blockers(
            state=state,
            policy=policy,
            action=action,
            assessment=assessment,
            score=score,
        ):
            continue
        marginal = score.base_utility_microscore + _diversity_bonus(
            policy=policy,
            assessment=assessment,
            state=state,
        )
        information = (
            score.information_audit.expected_information_gain_micronats
            if score.information_audit is not None
            else 0
        )
        key: tuple[object, ...] = (
            -marginal,
            -score.base_utility_microscore,
            -information,
            score.cost_burden_ppm,
            candidate_id,
        )
        choices.append((key, candidate_id, marginal))
    if not choices:
        return None
    _, candidate_id, marginal = min(choices)
    if marginal <= 0:
        return None
    return candidate_id, marginal


def _budget_projection(
    *,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    state: _BatchState,
) -> tuple[PortfolioBudgetProjection, ...]:
    return tuple(
        PortfolioBudgetProjection(
            allocation_id=item.allocation_id,
            kind=item.kind,
            before_microunits=item.available_microunits,
            selected_microunits=item.available_microunits
            - state.remaining_budget[(item.program_id, item.kind)],
            after_microunits=state.remaining_budget[(item.program_id, item.kind)],
        )
        for item in sorted(budget_state, key=lambda value: value.allocation_id)
    )


def derive_selection_decision(
    *,
    spec: PortfolioSlateSpec,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    scores: tuple[PortfolioCandidateScore, ...],
) -> PortfolioSelectionDecision:
    policy = spec.policy
    actions = {item.candidate_id: item for item in spec.proposal.candidates}
    assessments = {item.candidate_id: item for item in spec.assessment_batch.assessments}
    score_by_id = {item.candidate_id: item for item in scores}
    feasible = {item.candidate_id for item in scores if item.feasible}
    state = _new_batch_state(budget_state)
    marginal_by_id: dict[str, int] = {}
    selected_reason: dict[str, str] = {}

    replication_pool = {
        candidate_id
        for candidate_id in feasible
        if actions[candidate_id].action_type is PortfolioActionType.REPLICATION
    }
    while (
        len(
            [
                candidate_id
                for candidate_id in state.selected
                if actions[candidate_id].action_type is PortfolioActionType.REPLICATION
            ]
        )
        < policy.minimum_replication_actions
    ):
        choice = _choose_next(
            candidates=replication_pool - set(state.selected),
            policy=policy,
            actions=actions,
            assessments=assessments,
            scores=score_by_id,
            state=state,
        )
        if choice is None:
            rankings = tuple(
                PortfolioSelectionEntry(
                    rank=index,
                    candidate_id=score.candidate_id,
                    score_sha256=score.score_sha256,
                    selected=False,
                    reasons=(
                        score.blockers
                        if not score.feasible
                        else ("policy:replication_quota_unmet",)
                    ),
                )
                for index, score in enumerate(
                    sorted(
                        scores,
                        key=lambda item: (
                            not item.feasible,
                            -item.base_utility_microscore,
                            item.candidate_id,
                        ),
                    ),
                    start=1,
                )
            )
            return PortfolioSelectionDecision(
                selected_candidate_ids=(),
                rankings=rankings,
                budget_projection=_budget_projection(
                    budget_state=budget_state,
                    state=_new_batch_state(budget_state),
                ),
                disposition=PortfolioEpochDisposition.POLICY_BLOCKED,
            )
        candidate_id, marginal = choice
        _add_to_batch(
            state=state,
            action=actions[candidate_id],
            assessment=assessments[candidate_id],
            score=score_by_id[candidate_id],
        )
        marginal_by_id[candidate_id] = marginal
        selected_reason[candidate_id] = "selected:replication_quota"

    while len(state.selected) < policy.maximum_selected_actions:
        choice = _choose_next(
            candidates=feasible - set(state.selected),
            policy=policy,
            actions=actions,
            assessments=assessments,
            scores=score_by_id,
            state=state,
        )
        if choice is None:
            break
        candidate_id, marginal = choice
        _add_to_batch(
            state=state,
            action=actions[candidate_id],
            assessment=assessments[candidate_id],
            score=score_by_id[candidate_id],
        )
        marginal_by_id[candidate_id] = marginal
        selected_reason[candidate_id] = "selected:greedy_marginal_utility"

    nonselected = sorted(
        (item for item in scores if item.candidate_id not in state.selected),
        key=lambda item: (
            not item.feasible,
            -item.base_utility_microscore,
            item.candidate_id,
        ),
    )
    ordered_ids = [*state.selected, *(item.candidate_id for item in nonselected)]
    rankings: list[PortfolioSelectionEntry] = []
    for rank, candidate_id in enumerate(ordered_ids, start=1):
        score = score_by_id[candidate_id]
        if candidate_id in marginal_by_id:
            reasons = (selected_reason[candidate_id],)
            marginal = marginal_by_id[candidate_id]
        elif not score.feasible:
            reasons = score.blockers
            marginal = None
        else:
            batch_blockers = _batch_blockers(
                state=state,
                policy=policy,
                action=actions[candidate_id],
                assessment=assessments[candidate_id],
                score=score,
            )
            reasons = batch_blockers or ("utility:not_selected",)
            marginal = None
        rankings.append(
            PortfolioSelectionEntry(
                rank=rank,
                candidate_id=candidate_id,
                score_sha256=score.score_sha256,
                selected=candidate_id in marginal_by_id,
                marginal_utility_microscore=marginal,
                reasons=tuple(sorted(reasons)),
            )
        )
    disposition = (
        PortfolioEpochDisposition.SHADOW_READY
        if state.selected
        else PortfolioEpochDisposition.NO_FEASIBLE_ACTION
    )
    return PortfolioSelectionDecision(
        selected_candidate_ids=tuple(state.selected),
        rankings=tuple(rankings),
        budget_projection=_budget_projection(budget_state=budget_state, state=state),
        disposition=disposition,
    )


def derive_shadow_comparison(
    *,
    spec: PortfolioSlateSpec,
    human_plan: HumanPortfolioPlanSpec,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    scores: tuple[PortfolioCandidateScore, ...],
    decision: PortfolioSelectionDecision,
) -> PortfolioShadowComparison:
    policy = spec.policy
    actions = {item.candidate_id: item for item in spec.proposal.candidates}
    assessments = {item.candidate_id: item for item in spec.assessment_batch.assessments}
    score_by_id = {item.candidate_id: item for item in scores}
    hard_violations: list[str] = []
    batch_violations: list[str] = []
    state = _new_batch_state(budget_state)
    for candidate_id in human_plan.selected_candidate_ids:
        score = score_by_id[candidate_id]
        hard_violations.extend(f"{candidate_id}:{item}" for item in score.blockers)
        blockers = _batch_blockers(
            state=state,
            policy=policy,
            action=actions[candidate_id],
            assessment=assessments[candidate_id],
            score=score,
        )
        batch_violations.extend(f"{candidate_id}:{item}" for item in blockers)
        if not blockers:
            _add_to_batch(
                state=state,
                action=actions[candidate_id],
                assessment=assessments[candidate_id],
                score=score,
            )
    human = set(human_plan.selected_candidate_ids)
    planner = set(decision.selected_candidate_ids)
    union = human | planner
    return PortfolioShadowComparison(
        human_selected_candidate_ids=human_plan.selected_candidate_ids,
        planner_selected_candidate_ids=decision.selected_candidate_ids,
        overlap_count=len(human & planner),
        union_count=len(union),
        jaccard_ppm=_PPM if not union else len(human & planner) * _PPM // len(union),
        exact_set_match=human == planner,
        human_hard_filter_violations=tuple(sorted(set(hard_violations))),
        human_batch_constraint_violations=tuple(sorted(set(batch_violations))),
        planner_base_utility_sum=sum(
            score_by_id[item].base_utility_microscore for item in decision.selected_candidate_ids
        ),
        human_feasible_base_utility_sum=sum(
            score_by_id[item].base_utility_microscore
            for item in human_plan.selected_candidate_ids
            if score_by_id[item].feasible
        ),
    )


def derive_shadow_epoch(
    *,
    spec: PortfolioSlateSpec,
    graph: QuestGraphSnapshot,
    budget_state: tuple[PortfolioBudgetAvailability, ...],
    human_plan: HumanPortfolioPlanSpec,
    evaluated_at: datetime,
) -> DerivedPortfolioEpoch:
    scores = derive_candidate_scores(
        spec=spec,
        graph=graph,
        budget_state=budget_state,
        evaluated_at=evaluated_at,
    )
    decision = derive_selection_decision(
        spec=spec,
        budget_state=budget_state,
        scores=scores,
    )
    comparison = derive_shadow_comparison(
        spec=spec,
        human_plan=human_plan,
        budget_state=budget_state,
        scores=scores,
        decision=decision,
    )
    return DerivedPortfolioEpoch(scores=scores, decision=decision, comparison=comparison)


__all__ = [
    "DerivedPortfolioEpoch",
    "PORTFOLIO_SELECTOR_CODE_SHA256",
    "candidate_program_and_family",
    "derive_candidate_scores",
    "derive_information_audit",
    "derive_selection_decision",
    "derive_shadow_comparison",
    "derive_shadow_epoch",
]
