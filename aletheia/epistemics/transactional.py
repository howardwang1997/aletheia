"""F11-S2 database/event boundaries for F9 scientific archive commitments.

Content-addressed archive writes happen first and are safe to orphan because they are immutable.
The authoritative commit then records the archive receipt, any world-model rows, and a keyed event
under one PostgreSQL transaction.  Prediction, observation validation, and belief update use
distinct command identities so one failed boundary cannot silently roll another backward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from aletheia.epistemics.belief_update import (
    CommittedObservationValidationCampaign,
    CommittedWorldBeliefUpdateCampaign,
    ObservationValidationCampaign,
    WorldBeliefUpdateCampaign,
    commit_observation_validation_campaign,
    commit_world_belief_update_campaign,
)
from aletheia.epistemics.persistence import _store_world_model_snapshot
from aletheia.epistemics.prediction import (
    CommittedPredictionCommitmentCampaign,
    PredictionCommitmentCampaign,
    commit_prediction_commitment_campaign,
)
from aletheia.jobs.outbox import (
    ScientificCommandReceipt,
    ScientificCommandSpec,
    ScientificCommandType,
    ScientificMutation,
    ScientificTransitionStore,
)
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive

CommittedT = TypeVar(
    "CommittedT",
    CommittedPredictionCommitmentCampaign,
    CommittedObservationValidationCampaign,
    CommittedWorldBeliefUpdateCampaign,
)


@dataclass(frozen=True)
class TransactionalScientificCommit(Generic[CommittedT]):
    committed: CommittedT
    transaction: ScientificCommandReceipt


def _prediction_run_id(campaign: PredictionCommitmentCampaign) -> str:
    return campaign.source_causal_campaign.world_model_snapshot.question.run_id


def _validation_prediction(
    campaign: ObservationValidationCampaign,
) -> PredictionCommitmentCampaign:
    request = campaign.request
    selection = request.committed_selection.campaign
    matches = [
        candidate
        for candidate in selection.request.candidates
        if candidate.candidate_id == request.selected_candidate_id
    ]
    if len(matches) != 1:
        raise ValueError("observation validation cannot resolve its selected prediction")
    return matches[0].committed_prediction.campaign


def _validation_run_id(campaign: ObservationValidationCampaign) -> str:
    return _prediction_run_id(_validation_prediction(campaign))


def _archive_result(*, committed, disposition: str) -> dict:
    return {
        "campaign_sha256": committed.campaign.campaign_sha256,
        "commitment_receipt_sha256": committed.receipt_sha256,
        "ledger": committed.ledger.model_dump(mode="json"),
        "ledger_receipt_sha256": committed.ledger.receipt_sha256,
        "disposition": disposition,
        "committed_at": committed.committed_at.isoformat(),
    }


def commit_prediction_transactionally(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: PredictionCommitmentCampaign,
    committed_at: datetime,
    principal: str,
    idempotency_key: str | None = None,
    source_event_key: str | None = None,
    store: ScientificTransitionStore | None = None,
) -> TransactionalScientificCommit[CommittedPredictionCommitmentCampaign]:
    """Commit one immutable pre-observation prediction under its own DB/event boundary."""

    committed = commit_prediction_commitment_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=committed_at,
    )
    result = _archive_result(
        committed=committed,
        disposition=campaign.disposition.value,
    )
    key = idempotency_key or f"prediction:{campaign.campaign_sha256}"
    spec = ScientificCommandSpec(
        run_id=_prediction_run_id(campaign),
        command_type=ScientificCommandType.PREDICTION_COMMIT.value,
        aggregate_type="prediction_campaign",
        aggregate_id=campaign.campaign_sha256,
        idempotency_key=key,
        source_event_key=source_event_key,
        input={
            "campaign_sha256": campaign.campaign_sha256,
            "commitment_sha256": campaign.commitment_sha256,
            "commitment_receipt_sha256": committed.receipt_sha256,
            "ledger_receipt_sha256": committed.ledger.receipt_sha256,
        },
        principal=principal,
        event_type="prediction_committed",
    )

    def apply(session):
        _store_world_model_snapshot(
            session,
            campaign.source_causal_campaign.world_model_snapshot,
        )
        return ScientificMutation(
            result=result,
            event_projection={
                "campaign_sha256": campaign.campaign_sha256,
                "commitment_sha256": campaign.commitment_sha256,
                "commitment_receipt_sha256": committed.receipt_sha256,
                "disposition": campaign.disposition.value,
                "observation_access": "none",
            },
        )

    receipt = (store or ScientificTransitionStore()).execute(
        spec,
        apply,
        now=committed_at,
    )
    return TransactionalScientificCommit(committed=committed, transaction=receipt)


def commit_observation_validation_transactionally(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: ObservationValidationCampaign,
    committed_at: datetime,
    principal: str,
    idempotency_key: str | None = None,
    source_event_key: str | None = None,
    store: ScientificTransitionStore | None = None,
) -> TransactionalScientificCommit[CommittedObservationValidationCampaign]:
    """Commit an independently validated observation without mutating belief in this command."""

    committed = commit_observation_validation_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=committed_at,
    )
    result = _archive_result(
        committed=committed,
        disposition=campaign.disposition.value,
    )
    key = idempotency_key or f"validation:{campaign.campaign_sha256}"
    spec = ScientificCommandSpec(
        run_id=_validation_run_id(campaign),
        command_type=ScientificCommandType.OBSERVATION_VALIDATION_COMMIT.value,
        aggregate_type="observation_validation",
        aggregate_id=campaign.campaign_sha256,
        idempotency_key=key,
        source_event_key=source_event_key,
        input={
            "campaign_sha256": campaign.campaign_sha256,
            "commitment_receipt_sha256": committed.receipt_sha256,
            "ledger_receipt_sha256": committed.ledger.receipt_sha256,
            "observation_receipt_sha256": campaign.request.observation_receipt.receipt_sha256,
        },
        principal=principal,
        event_type="observation_validation_committed",
    )
    receipt = (store or ScientificTransitionStore()).execute(
        spec,
        lambda _session: ScientificMutation(
            result=result,
            event_projection={
                "campaign_sha256": campaign.campaign_sha256,
                "commitment_receipt_sha256": committed.receipt_sha256,
                "observation_receipt_sha256": (campaign.request.observation_receipt.receipt_sha256),
                "disposition": campaign.disposition.value,
                "valid_for_belief_update": bool(
                    campaign.probe is not None and campaign.probe.valid_for_belief_update
                ),
            },
        ),
        now=committed_at,
    )
    return TransactionalScientificCommit(committed=committed, transaction=receipt)


def commit_world_belief_update_transactionally(
    *,
    archive: ContentAddressedResponseArchive,
    campaign: WorldBeliefUpdateCampaign,
    committed_at: datetime,
    principal: str,
    idempotency_key: str | None = None,
    source_event_key: str | None = None,
    store: ScientificTransitionStore | None = None,
) -> TransactionalScientificCommit[CommittedWorldBeliefUpdateCampaign]:
    """Commit one belief update and its posterior snapshot, never the raw observation."""

    committed = commit_world_belief_update_campaign(
        archive=archive,
        campaign=campaign,
        committed_at=committed_at,
    )
    validation = campaign.request.committed_validation.campaign
    run_id = _validation_run_id(validation)
    source_snapshot = _validation_prediction(validation).source_causal_campaign.world_model_snapshot
    snapshot = campaign.updated_world_model_snapshot
    if snapshot is not None and snapshot.question.run_id != run_id:
        raise ValueError("belief-update snapshot changed its run identity")
    result = {
        **_archive_result(
            committed=committed,
            disposition=campaign.disposition.value,
        ),
        "source_belief_state_sha256": campaign.request.source_belief_state_sha256,
        "updated_snapshot_sha256": None if snapshot is None else snapshot.snapshot_sha256,
        "updated_belief_state_sha256": (
            None if snapshot is None else snapshot.belief_state.belief_state_sha256
        ),
    }
    key = idempotency_key or f"belief:{campaign.campaign_sha256}"
    spec = ScientificCommandSpec(
        run_id=run_id,
        command_type=ScientificCommandType.BELIEF_UPDATE_COMMIT.value,
        aggregate_type="belief_update",
        aggregate_id=campaign.campaign_sha256,
        idempotency_key=key,
        source_event_key=source_event_key,
        input={
            "campaign_sha256": campaign.campaign_sha256,
            "commitment_receipt_sha256": committed.receipt_sha256,
            "validation_commitment_receipt_sha256": (
                campaign.request.committed_validation.receipt_sha256
            ),
            "source_belief_state_sha256": campaign.request.source_belief_state_sha256,
            "updated_snapshot_sha256": None if snapshot is None else snapshot.snapshot_sha256,
        },
        principal=principal,
        event_type="belief_update_committed",
    )

    def apply(session):
        _store_world_model_snapshot(session, source_snapshot)
        if snapshot is not None:
            _store_world_model_snapshot(session, snapshot)
        return ScientificMutation(
            result=result,
            event_projection={
                "campaign_sha256": campaign.campaign_sha256,
                "commitment_receipt_sha256": committed.receipt_sha256,
                "validation_commitment_receipt_sha256": (
                    campaign.request.committed_validation.receipt_sha256
                ),
                "source_belief_state_sha256": campaign.request.source_belief_state_sha256,
                "updated_belief_state_sha256": (
                    None if snapshot is None else snapshot.belief_state.belief_state_sha256
                ),
                "updated_snapshot_sha256": (None if snapshot is None else snapshot.snapshot_sha256),
                "disposition": campaign.disposition.value,
                "observation_access": "validated_artifact_only",
            },
        )

    receipt = (store or ScientificTransitionStore()).execute(spec, apply, now=committed_at)
    return TransactionalScientificCommit(committed=committed, transaction=receipt)


__all__ = [
    "TransactionalScientificCommit",
    "commit_observation_validation_transactionally",
    "commit_prediction_transactionally",
    "commit_world_belief_update_transactionally",
]
