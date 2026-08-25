"""The Phase-1 research lifecycle FSM.

Scoping already produced a human-confirmed plan (an Experiment at stage
``experiment_design``), so the launched loop starts at EXPERIMENT_DESIGN and walks
to ARCHIVE. Hard gates (critique_design, review_results) can send it back to design,
bounded by a per-stage loop guard so it can't oscillate forever.
"""

from __future__ import annotations

from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.memory.ledger import Decision
from aletheia.reproducibility.manifest import content_sha256

# Ordered stages the driver walks (the two gates are folded into the driver logic).
PHASE1_STAGES = (
    "survey",
    "ideate",
    "experiment_design",
    "execution",
    "analysis",
    "optimize",
    "write_up",
    "archive",
)


class LoopGuard:
    """Bounds re-entries into a stage (e.g. design revisions after a reject) so the
    autonomous loop escalates instead of oscillating forever."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._counts: dict[str, int] = {}

    def bump(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def exceeded(self, key: str) -> bool:
        return self._counts.get(key, 0) >= self.limit


async def record_transition(
    run_id: str,
    experiment_id: str | None,
    stage_from: str | None,
    stage_to: str,
    rationale: str,
    actor: str = "orchestrator",
    critique_panel_id: str | None = None,
    idempotency_key: str | None = None,
    source_event_key: str | None = None,
) -> ScientificCommandReceipt:
    """Commit a Decision and its durable ``stage`` event under one exact command."""
    import asyncio

    command_input = {
        "experiment_id": experiment_id,
        "stage_from": stage_from,
        "stage_to": stage_to,
        "rationale": rationale,
        "actor": actor,
        "critique_panel_id": critique_panel_id,
    }
    key = idempotency_key or f"stage:{content_sha256({'run_id': run_id, **command_input})}"
    spec = ScientificCommandSpec(
        run_id=run_id,
        command_type=ScientificCommandType.STAGE_TRANSITION.value,
        aggregate_type="experiment_stage",
        aggregate_id=experiment_id or run_id,
        idempotency_key=key,
        source_event_key=source_event_key,
        input=command_input,
        principal=actor,
        event_type="stage",
    )

    def apply(session):
        decision = Decision(
            run_id=run_id,
            experiment_id=experiment_id,
            stage_from=stage_from,
            stage_to=stage_to,
            rationale=rationale,
            actor=actor,
            critique_panel_id=critique_panel_id,
            scientific_command_id=spec.command_id,
        )
        session.add(decision)
        session.flush()
        return ScientificMutation(
            result={"decision_id": int(decision.id)},
            event_projection={
                "decision_id": int(decision.id),
                "experiment_id": experiment_id,
                "stage": stage_to,
                "from": stage_from,
                "rationale": rationale,
                "critique_panel_id": critique_panel_id,
            },
        )

    return await asyncio.to_thread(
        ScientificTransitionStore().execute,
        spec,
        apply,
    )
