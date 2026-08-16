"""Scheduler-facing F9 world-model continuation entry points.

All derivation, persistence, and authorization invariants remain in
``aletheia.epistemics.continuation``.  The scheduler gets no alternate mutation or bypass path.
"""

from __future__ import annotations

from datetime import datetime

from aletheia.epistemics.acceptance import CommittedK3AcceptanceCampaign
from aletheia.epistemics.causal import CausalWorldModelSource
from aletheia.epistemics.continuation import (
    WorldModelTransition,
    WorldModelTransitionStoreReceipt,
    load_authorized_next_round_source,
    persist_world_model_transition,
)
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive


def persist_k3_round_transition(
    transition: WorldModelTransition,
) -> WorldModelTransitionStoreReceipt:
    """Commit a mechanically derived transition through the one atomic persistence path."""

    return persist_world_model_transition(transition)


def authorize_k3_next_round(
    *,
    transition_sha256: str,
    committed_acceptance: CommittedK3AcceptanceCampaign,
    acceptance_archive: ContentAddressedResponseArchive,
    authorized_at: datetime,
) -> CausalWorldModelSource:
    """Return the exact persisted next-round source only after independent K3 authorization."""

    return load_authorized_next_round_source(
        transition_sha256=transition_sha256,
        committed_acceptance=committed_acceptance,
        acceptance_archive=acceptance_archive,
        authorized_at=authorized_at,
    )


__all__ = ["authorize_k3_next_round", "persist_k3_round_transition"]
