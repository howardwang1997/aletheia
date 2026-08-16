"""Pure lifecycle contract for Quest, ResearchProgram, and Campaign nodes."""

from __future__ import annotations

from aletheia.programs.schemas import GraphNodeState, GraphNodeType

INITIAL_STATE = {
    GraphNodeType.QUEST: GraphNodeState.DRAFT,
    GraphNodeType.PROGRAM: GraphNodeState.PROPOSED,
    GraphNodeType.CAMPAIGN: GraphNodeState.PLANNED,
}

ALLOWED_STATES = {
    GraphNodeType.QUEST: {
        GraphNodeState.DRAFT,
        GraphNodeState.ACTIVE,
        GraphNodeState.PAUSED,
        GraphNodeState.COMPLETED,
        GraphNodeState.ARCHIVED,
    },
    GraphNodeType.PROGRAM: {
        GraphNodeState.PROPOSED,
        GraphNodeState.ACTIVE,
        GraphNodeState.PAUSED,
        GraphNodeState.STOPPED,
        GraphNodeState.COMPLETED,
        GraphNodeState.ARCHIVED,
    },
    GraphNodeType.CAMPAIGN: {
        GraphNodeState.PLANNED,
        GraphNodeState.ACTIVE,
        GraphNodeState.PAUSED,
        GraphNodeState.STOPPED,
        GraphNodeState.COMPLETED,
        GraphNodeState.FAILED,
        GraphNodeState.ARCHIVED,
    },
}

ALLOWED_TRANSITIONS = {
    GraphNodeType.QUEST: {
        GraphNodeState.DRAFT: {GraphNodeState.ACTIVE},
        GraphNodeState.ACTIVE: {GraphNodeState.PAUSED, GraphNodeState.COMPLETED},
        GraphNodeState.PAUSED: {GraphNodeState.ACTIVE, GraphNodeState.COMPLETED},
        GraphNodeState.COMPLETED: {GraphNodeState.ARCHIVED},
    },
    GraphNodeType.PROGRAM: {
        GraphNodeState.PROPOSED: {GraphNodeState.ACTIVE, GraphNodeState.STOPPED},
        GraphNodeState.ACTIVE: {
            GraphNodeState.PAUSED,
            GraphNodeState.STOPPED,
            GraphNodeState.COMPLETED,
        },
        GraphNodeState.PAUSED: {GraphNodeState.ACTIVE, GraphNodeState.STOPPED},
        GraphNodeState.STOPPED: {GraphNodeState.ARCHIVED},
        GraphNodeState.COMPLETED: {GraphNodeState.ARCHIVED},
    },
    GraphNodeType.CAMPAIGN: {
        GraphNodeState.PLANNED: {GraphNodeState.ACTIVE, GraphNodeState.STOPPED},
        GraphNodeState.ACTIVE: {
            GraphNodeState.PAUSED,
            GraphNodeState.STOPPED,
            GraphNodeState.COMPLETED,
            GraphNodeState.FAILED,
        },
        GraphNodeState.PAUSED: {
            GraphNodeState.ACTIVE,
            GraphNodeState.STOPPED,
            GraphNodeState.FAILED,
        },
        GraphNodeState.STOPPED: {GraphNodeState.ARCHIVED},
        GraphNodeState.COMPLETED: {GraphNodeState.ARCHIVED},
        GraphNodeState.FAILED: {GraphNodeState.ARCHIVED},
    },
}


def transition_allowed(
    node_type: GraphNodeType,
    source: GraphNodeState,
    target: GraphNodeState,
) -> bool:
    return target in ALLOWED_TRANSITIONS.get(node_type, {}).get(source, set())


__all__ = ["ALLOWED_STATES", "ALLOWED_TRANSITIONS", "INITIAL_STATE", "transition_allowed"]
