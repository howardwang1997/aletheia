"""Scheduler-facing entry point for independent F9/K3 acceptance scoring.

The implementation remains in :mod:`aletheia.epistemics.acceptance` so the mathematical and
provenance checks stay independently testable without the scheduler driver.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from aletheia.epistemics.acceptance import (
    K3AcceptanceCampaign,
    K3AcceptancePolicy,
    K3AcceptanceRequest,
    K3AcceptanceScorerManifest,
    run_k3_acceptance,
)
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive


def score_k3(
    *,
    campaign_id: str,
    policy: K3AcceptancePolicy,
    scorer_manifest: K3AcceptanceScorerManifest,
    request: K3AcceptanceRequest,
    selection_archive: ContentAddressedResponseArchive,
    validation_archive: ContentAddressedResponseArchive,
    update_archive: ContentAddressedResponseArchive,
    evidence_archive: ContentAddressedResponseArchive,
    clock: Callable[[], datetime] | None = None,
) -> K3AcceptanceCampaign:
    """Score committed K3 evidence without giving the scheduler a second scoring path."""

    return run_k3_acceptance(
        campaign_id=campaign_id,
        policy=policy,
        scorer_manifest=scorer_manifest,
        request=request,
        selection_archive=selection_archive,
        validation_archive=validation_archive,
        update_archive=update_archive,
        evidence_archive=evidence_archive,
        clock=clock,
    )


__all__ = ["score_k3"]
