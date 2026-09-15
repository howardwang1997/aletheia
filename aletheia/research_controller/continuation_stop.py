"""Hash-frozen stop policy for graph-scoped F9-v2 continuation (ARL-2 roadmap step 5).

The three scientific dispositions in ``continuation.py`` derive from fit assessments
alone, and none of them can end a quest.  Ending one is a policy act, so it rides a
separate preregistered contract: a frozen :class:`StopPolicyPin` whose triggers are
enumerated, whose mapped stop reasons are pinned per trigger, and whose evaluation is
a pure function of hash-closed inputs (observed rounds, current hypothesis beliefs,
best available next-experiment information gain).  A pin takes effect only when its
hash is listed in the deployment's assessment policy pin
(``ContinuationAssessmentPolicyPin.allowed_stop_policy_sha256s``); an unauthorized
stop policy raises instead of deriving.

Operator-class reasons (EMERGENCY_STOP, RISK_BOUNDARY, ETHICS_BOUNDARY,
LICENSE_BOUNDARY, HUMAN_VALUE_JUDGMENT_REQUIRED, CAPABILITY_UNAVAILABLE) are
unreachable from this module by construction: every trigger's mapped reason is pinned
to exactly one ordinary-path value.  Live ops wall-clock budgets and
ResourceBudgetContract preemption stay out of the derivation entirely.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.protocols.world_models import HypothesisBeliefV2
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
    StopReason,
    canonical_sha256,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StopTriggerCode(str, Enum):
    """Preregistered, derivable stop triggers; precedence follows declaration order."""

    ROUND_BUDGET_EXHAUSTED = "round_budget_exhausted"
    BELIEF_DECISION_THRESHOLD = "belief_decision_threshold"
    BELIEF_INDISTINGUISHABLE = "belief_indistinguishable"
    MARGINAL_INFORMATION_FLOOR = "marginal_information_floor"


# The preregistered narrow form (design record 2026-09-15, question 2): each trigger
# maps to exactly one ordinary-path kernel reason and nothing else.
_PINNED_STOP_REASONS = {
    StopTriggerCode.ROUND_BUDGET_EXHAUSTED: StopReason.BUDGET_EXHAUSTED,
    StopTriggerCode.BELIEF_DECISION_THRESHOLD: StopReason.EVIDENCE_THRESHOLD_REACHED,
    StopTriggerCode.BELIEF_INDISTINGUISHABLE: StopReason.CURRENTLY_INDISTINGUISHABLE,
    StopTriggerCode.MARGINAL_INFORMATION_FLOOR: StopReason.LOW_MARGINAL_INFORMATION_VALUE,
}

_BELIEF_TRIGGERS = frozenset(
    {
        StopTriggerCode.BELIEF_DECISION_THRESHOLD,
        StopTriggerCode.BELIEF_INDISTINGUISHABLE,
    }
)


class StopReasonMappingV1(ControllerModel):
    """One trigger's pinned stop reason and its preregistered reopen conditions."""

    schema_name: Literal["aletheia.continuation_stop_reason_mapping"] = (
        "aletheia.continuation_stop_reason_mapping"
    )
    schema_version: Literal[1] = 1
    trigger: StopTriggerCode
    stop_reason: StopReason
    reopen_conditions: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _mapping_is_pinned(self) -> "StopReasonMappingV1":
        if self.stop_reason is not _PINNED_STOP_REASONS[self.trigger]:
            raise ValueError(
                f"stop trigger {self.trigger.value} maps to exactly "
                f"{_PINNED_STOP_REASONS[self.trigger].value}"
            )
        if self.reopen_conditions != tuple(sorted(set(self.reopen_conditions))):
            raise ValueError("stop reopen conditions must be unique and canonical")
        if any(not condition.strip() for condition in self.reopen_conditions):
            raise ValueError("stop reopen conditions cannot be blank")
        return self

    @property
    def mapping_sha256(self) -> str:
        return canonical_sha256(self)


class StopPolicyPin(ControllerModel):
    """Deployment-frozen stop thresholds; a change is a new commissioning window."""

    schema_name: Literal["aletheia.continuation_stop_policy_pin"] = (
        "aletheia.continuation_stop_policy_pin"
    )
    schema_version: Literal[1] = 1
    max_rounds: int = Field(ge=1)
    eig_floor: float = Field(gt=0.0)
    belief_decision_threshold_lower: float = Field(gt=0.0, lt=1.0)
    belief_decision_threshold_upper: float = Field(gt=0.0, lt=1.0)
    stop_reason_mappings: tuple[StopReasonMappingV1, ...]

    @model_validator(mode="after")
    def _policy_is_canonical(self) -> "StopPolicyPin":
        if not math.isfinite(self.eig_floor):
            raise ValueError("stop eig floor must be finite")
        if self.belief_decision_threshold_upper <= self.belief_decision_threshold_lower:
            raise ValueError("stop belief decision thresholds must bracket a decision band")
        triggers = tuple(item.trigger for item in self.stop_reason_mappings)
        if triggers != tuple(sorted(set(triggers))):
            raise ValueError("stop reason mappings must be unique and canonically ordered")
        if set(triggers) != set(StopTriggerCode):
            raise ValueError("stop policy must map every preregistered trigger")
        return self

    def mapping_for(self, trigger: StopTriggerCode) -> StopReasonMappingV1:
        return next(item for item in self.stop_reason_mappings if item.trigger is trigger)

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class StopGrounds(ControllerModel):
    """Why one derivation stopped: trigger, pinned reason, inputs, reopen text."""

    schema_name: Literal["aletheia.continuation_stop_grounds"] = (
        "aletheia.continuation_stop_grounds"
    )
    schema_version: Literal[1] = 1
    stop_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    trigger: StopTriggerCode
    stop_reason: StopReason
    rounds_observed: int = Field(ge=1)
    max_hypothesis_belief: float | None = Field(default=None, ge=0.0, le=1.0)
    max_available_eig: float | None = Field(default=None, gt=0.0)
    reopen_conditions: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _grounds_are_pinned(self) -> "StopGrounds":
        if self.stop_reason is not _PINNED_STOP_REASONS[self.trigger]:
            raise ValueError(
                f"stop grounds trigger {self.trigger.value} must carry "
                f"{_PINNED_STOP_REASONS[self.trigger].value}"
            )
        if self.max_hypothesis_belief is not None and not math.isfinite(
            self.max_hypothesis_belief
        ):
            raise ValueError("stop grounds belief must be finite")
        if self.max_available_eig is not None and not math.isfinite(self.max_available_eig):
            raise ValueError("stop grounds eig must be finite")
        if (self.trigger in _BELIEF_TRIGGERS) != (self.max_hypothesis_belief is not None):
            raise ValueError("belief triggers record the max hypothesis belief, and only they")
        if (self.trigger is StopTriggerCode.MARGINAL_INFORMATION_FLOOR) != (
            self.max_available_eig is not None
        ):
            raise ValueError("the information-floor trigger records eig, and only it")
        if self.reopen_conditions != tuple(sorted(set(self.reopen_conditions))):
            raise ValueError("stop grounds reopen conditions must be canonical")
        return self

    @property
    def grounds_sha256(self) -> str:
        return canonical_sha256(self)


def evaluate_stop_policy(
    *,
    stop_policy: StopPolicyPin,
    rounds_observed: int,
    hypothesis_beliefs: tuple[HypothesisBeliefV2, ...] | None,
    max_available_eig: float | None,
) -> StopGrounds | None:
    """Apply the frozen policy; returns the fired trigger's grounds or ``None``.

    Fail-closed on absent inputs: a preregistered trigger that cannot be evaluated
    raises rather than silently disarming.  Precedence is the preregistered order —
    round budget, then decisive belief, then indistinguishable belief, then the
    information floor.
    """

    if rounds_observed < 0:
        raise ValueError("rounds observed cannot be negative")

    def grounds(trigger: StopTriggerCode, **readings: float) -> StopGrounds:
        mapping = stop_policy.mapping_for(trigger)
        return StopGrounds(
            stop_policy_sha256=stop_policy.policy_sha256,
            trigger=trigger,
            stop_reason=mapping.stop_reason,
            rounds_observed=rounds_observed,
            reopen_conditions=mapping.reopen_conditions,
            **readings,
        )

    if rounds_observed >= stop_policy.max_rounds:
        return grounds(StopTriggerCode.ROUND_BUDGET_EXHAUSTED)

    if hypothesis_beliefs is None:
        raise ValueError("stop policy requires the world model's hypothesis beliefs")
    max_belief = max(item.probability for item in hypothesis_beliefs)
    if max_belief >= stop_policy.belief_decision_threshold_upper:
        return grounds(
            StopTriggerCode.BELIEF_DECISION_THRESHOLD, max_hypothesis_belief=max_belief
        )
    if max_belief <= stop_policy.belief_decision_threshold_lower:
        return grounds(
            StopTriggerCode.BELIEF_INDISTINGUISHABLE, max_hypothesis_belief=max_belief
        )

    if max_available_eig is None:
        raise ValueError("stop policy requires the best available next-experiment eig")
    if max_available_eig < stop_policy.eig_floor:
        return grounds(StopTriggerCode.MARGINAL_INFORMATION_FLOOR, max_available_eig=max_available_eig)
    return None


def count_rounds_observed(
    events: tuple[ResearchEvent, ...], *, branch_id: str
) -> int:
    """Count OBSERVATION_INCORPORATED events on one branch from the closed event stream."""

    rounds = 0
    for event in events:
        if event.event_type is not EventType.OBSERVATION_INCORPORATED:
            continue
        payload = event.payload
        if isinstance(payload, ObservationIncorporatedPayload) and (
            payload.branch_id == branch_id
        ):
            rounds += 1
    return rounds


__all__ = [
    "StopGrounds",
    "StopPolicyPin",
    "StopReasonMappingV1",
    "StopTriggerCode",
    "count_rounds_observed",
    "evaluate_stop_policy",
]
