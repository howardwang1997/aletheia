"""F9-S8 atomic world-model transition and next-round authorization.

The F9-S6 posterior is not considered a scheduler continuation merely because it exists in an
in-memory campaign.  This module closes that gap: it deterministically assembles any append-only
hypothesis revision, persists the complete transition and a typed event in one PostgreSQL
transaction, and only exposes the next snapshot after an independent F9-S7 verdict authorizes the
terminal action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from aletheia.db import session_scope
from aletheia.epistemics.acceptance import (
    CommittedK3AcceptanceCampaign,
    K3AcceptanceCheckKind,
    K3AcceptanceCheckStatus,
    K3AcceptanceDisposition,
    K3RevisionMaterialization,
    K3RoundEvidence,
    K3TerminalAction,
    load_k3_acceptance_campaign,
    revision_materialization_failure_reasons,
)
from aletheia.epistemics.belief_update import (
    CommittedWorldBeliefUpdateCampaign,
    HypothesisRevisionAction,
    WorldBeliefUpdateDisposition,
    WorldRevisionAction,
)
from aletheia.epistemics.causal import CausalWorldModelSource
from aletheia.epistemics.persistence import (
    EpistemicHypothesisVersionRecord,
    EpistemicObjectNotFound,
    EpistemicPersistenceError,
    EpistemicPredictionRecord,
    EpistemicWorldModelTransitionRecord,
    ImmutableEpistemicConflict,
    _parse_stored,
    _payload,
    _require_exact_row,
    _store_hypothesis,
    _store_prediction,
    _store_world_model_snapshot,
    get_world_model_snapshot,
)
from aletheia.epistemics.schemas import (
    Assumption,
    BeliefState,
    BeliefUpdateKind,
    EpistemicModel,
    HypothesisBelief,
    HypothesisVersion,
    Prediction,
    WorldModelSnapshot,
)
from aletheia.events.bus import make_event
from aletheia.events.store import persist_event
from aletheia.memory.ledger import Event
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive
from aletheia.reproducibility.manifest import content_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ACTOR_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$"
_TRANSITION_EVENT_TYPE = "f9_world_model_transition_committed"
_TRANSITION_EVENT_AGENT = "f9-world-model-transition"


class WorldModelTransitionDisposition(str, Enum):
    READY_NEXT_ROUND = "ready_next_round"
    MEASUREMENT_REDESIGN_REQUIRED = "measurement_redesign_required"
    HYPOTHESIS_SET_FORK_REQUIRED = "hypothesis_set_fork_required"


def _selected_source_snapshot(round_evidence: K3RoundEvidence) -> WorldModelSnapshot:
    selection = round_evidence.committed_selection.campaign
    if selection.decision is None or selection.decision.selected_candidate_id is None:
        raise ValueError("world-model transition selection has no selected candidate")
    selected_id = selection.decision.selected_candidate_id
    matches = [item for item in selection.request.candidates if item.candidate_id == selected_id]
    if len(matches) != 1:
        raise ValueError("world-model transition cannot resolve the selected candidate")
    return matches[0].committed_prediction.campaign.source_causal_campaign.world_model_snapshot


def _successful_update(
    round_evidence: K3RoundEvidence,
) -> CommittedWorldBeliefUpdateCampaign:
    if len(round_evidence.committed_updates) != 1:
        raise ValueError("world-model transition requires exactly one committed update")
    update = round_evidence.committed_updates[0]
    if update.campaign.disposition not in {
        WorldBeliefUpdateDisposition.UPDATED_ROBUST,
        WorldBeliefUpdateDisposition.UPDATED_FRAGILE,
    }:
        raise ValueError("world-model transition requires a successful belief update")
    if update.campaign.updated_world_model_snapshot is None:
        raise ValueError("successful world-model update omitted its posterior snapshot")
    return update


def _revalidated(model_type, value, **updates):
    payload = value.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def _assemble_revision_snapshot(
    *,
    posterior: WorldModelSnapshot,
    materializations: tuple[K3RevisionMaterialization, ...],
    persistence_principal_sha256: str,
    persisted_at: datetime,
) -> WorldModelSnapshot:
    """Close hypothesis revisions over assumptions, predictions, and belief bindings."""

    if not materializations:
        return posterior
    revised_by_hypothesis = {
        item.revised_hypothesis.hypothesis_id: item for item in materializations
    }
    hypotheses = tuple(
        sorted(
            (
                revised_by_hypothesis[item.hypothesis_id].revised_hypothesis
                if item.hypothesis_id in revised_by_hypothesis
                else item
                for item in posterior.hypotheses
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    assumptions: list[Assumption] = []
    for assumption in posterior.assumptions:
        revised = revised_by_hypothesis.get(assumption.hypothesis_id)
        if revised is None:
            assumptions.append(assumption)
            continue
        assumptions.append(
            _revalidated(
                Assumption,
                assumption,
                version=assumption.version + 1,
                parent_assumption_sha256=assumption.assumption_sha256,
                hypothesis_version_sha256=revised.revised_hypothesis.hypothesis_sha256,
                author_principal_sha256=persistence_principal_sha256,
                frozen_at=persisted_at,
            )
        )

    revised_predictions = {
        prediction.prediction_id: prediction
        for materialization in materializations
        for prediction in materialization.revised_predictions
    }
    predictions = tuple(
        sorted(
            (
                revised_predictions.get(prediction.prediction_id, prediction)
                for prediction in posterior.predictions
            ),
            key=lambda item: (item.prediction_id, item.version),
        )
    )
    probabilities = {
        item.hypothesis_id: item.probability for item in posterior.belief_state.hypotheses
    }
    belief = BeliefState(
        run_id=posterior.belief_state.run_id,
        belief_lineage_id=posterior.belief_state.belief_lineage_id,
        version=posterior.belief_state.version + 1,
        parent_belief_state_sha256=posterior.belief_state.belief_state_sha256,
        question_id=posterior.belief_state.question_id,
        question_version_sha256=posterior.belief_state.question_version_sha256,
        hypotheses=tuple(
            HypothesisBelief(
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                probability=probabilities[hypothesis.hypothesis_id],
            )
            for hypothesis in hypotheses
        ),
        update_kind=BeliefUpdateKind.HYPOTHESIS_REVISION,
        author_principal_sha256=persistence_principal_sha256,
        frozen_at=persisted_at,
    )
    return WorldModelSnapshot(
        question=posterior.question,
        hypotheses=hypotheses,
        assumptions=tuple(sorted(assumptions, key=lambda item: (item.assumption_id, item.version))),
        predictions=predictions,
        belief_state=belief,
        frozen_at=persisted_at,
    )


def _derive_transition_outputs(
    *,
    round_evidence: K3RoundEvidence,
    materializations: tuple[K3RevisionMaterialization, ...],
    persistence_principal_sha256: str,
    persisted_at: datetime,
) -> tuple[WorldModelTransitionDisposition, WorldModelSnapshot | None]:
    update = _successful_update(round_evidence)
    campaign = update.campaign
    assert campaign.updated_world_model_snapshot is not None
    assert campaign.world_revision is not None
    required = {
        directive.directive_sha256: directive
        for directive in campaign.hypothesis_revisions
        if directive.new_version_required
    }
    supplied = {item.directive_sha256: item for item in materializations}
    if set(supplied) != set(required):
        raise ValueError("world-model transition requires the exact revision materialization set")
    failures = {
        reason
        for directive_sha256, directive in required.items()
        for reason in revision_materialization_failure_reasons(
            round_evidence=round_evidence,
            update=update,
            directive=directive,
            materialization=supplied[directive_sha256],
            persisted_at=persisted_at,
        )
    }
    if failures:
        raise ValueError(
            f"invalid world-model revision materialization: {','.join(sorted(failures))}"
        )
    retires = any(
        directive.action is HypothesisRevisionAction.RETIRE
        for directive in campaign.hypothesis_revisions
    )
    if retires or campaign.world_revision.action is WorldRevisionAction.FORK_HYPOTHESIS_SET:
        return WorldModelTransitionDisposition.HYPOTHESIS_SET_FORK_REQUIRED, None
    snapshot = _assemble_revision_snapshot(
        posterior=campaign.updated_world_model_snapshot,
        materializations=materializations,
        persistence_principal_sha256=persistence_principal_sha256,
        persisted_at=persisted_at,
    )
    if campaign.world_revision.action is WorldRevisionAction.SEEK_NEW_MEASUREMENT_OR_STOP:
        return WorldModelTransitionDisposition.MEASUREMENT_REDESIGN_REQUIRED, snapshot
    return WorldModelTransitionDisposition.READY_NEXT_ROUND, snapshot


class WorldModelTransition(EpistemicModel):
    schema_version: Literal[1] = 1
    transition_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    round_evidence: K3RoundEvidence
    revision_materializations: tuple[K3RevisionMaterialization, ...] = Field(max_length=512)
    next_round_world_model_snapshot: WorldModelSnapshot | None = None
    disposition: WorldModelTransitionDisposition
    persistence_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    persisted_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _transition_is_mechanically_derived(self) -> "WorldModelTransition":
        keys = [
            (item.source_update_receipt_sha256, item.directive_sha256)
            for item in self.revision_materializations
        ]
        if keys != sorted(set(keys)):
            raise ValueError("world-model revision materializations must be unique and canonical")
        update = _successful_update(self.round_evidence)
        if self.persisted_at < update.committed_at:
            raise ValueError("world-model transition predates its committed update")
        expected_disposition, expected_snapshot = _derive_transition_outputs(
            round_evidence=self.round_evidence,
            materializations=self.revision_materializations,
            persistence_principal_sha256=self.persistence_principal_sha256,
            persisted_at=self.persisted_at,
        )
        if (
            self.disposition is not expected_disposition
            or self.next_round_world_model_snapshot != expected_snapshot
        ):
            raise ValueError("world-model transition outputs are not mechanically derived")
        return self

    @property
    def committed_update(self) -> CommittedWorldBeliefUpdateCampaign:
        return _successful_update(self.round_evidence)

    @property
    def source_world_model_snapshot(self) -> WorldModelSnapshot:
        return _selected_source_snapshot(self.round_evidence)

    @property
    def posterior_world_model_snapshot(self) -> WorldModelSnapshot:
        snapshot = self.committed_update.campaign.updated_world_model_snapshot
        assert snapshot is not None
        return snapshot

    @property
    def transition_sha256(self) -> str:
        return content_sha256(self)


class WorldModelTransitionEventProjection(EpistemicModel):
    schema_version: Literal[1] = 1
    event_type: Literal["f9_world_model_transition_committed"] = _TRANSITION_EVENT_TYPE
    transition_sha256: str = Field(pattern=_SHA256_PATTERN)
    transition_id: str = Field(pattern=_ACTOR_ID_PATTERN)
    round_id: str
    source_update_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    posterior_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    next_round_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    disposition: WorldModelTransitionDisposition
    persistence_principal_sha256: str = Field(pattern=_SHA256_PATTERN)
    persisted_at: AwareDatetime

    @property
    def projection_sha256(self) -> str:
        return content_sha256(self)


@dataclass(frozen=True)
class WorldModelTransitionStoreReceipt:
    transition_sha256: str
    event_id: int
    created: bool


def _event_projection(transition: WorldModelTransition) -> WorldModelTransitionEventProjection:
    next_snapshot = transition.next_round_world_model_snapshot
    return WorldModelTransitionEventProjection(
        transition_sha256=transition.transition_sha256,
        transition_id=transition.transition_id,
        round_id=transition.round_evidence.round_id,
        source_update_receipt_sha256=transition.committed_update.receipt_sha256,
        source_snapshot_sha256=transition.source_world_model_snapshot.snapshot_sha256,
        posterior_snapshot_sha256=transition.posterior_world_model_snapshot.snapshot_sha256,
        next_round_snapshot_sha256=(
            None if next_snapshot is None else next_snapshot.snapshot_sha256
        ),
        disposition=transition.disposition,
        persistence_principal_sha256=transition.persistence_principal_sha256,
        persisted_at=transition.persisted_at,
    )


def build_world_model_transition(
    *,
    transition_id: str,
    round_evidence: K3RoundEvidence,
    revision_materializations: tuple[K3RevisionMaterialization, ...],
    persistence_principal_sha256: str,
    persisted_at: datetime,
) -> WorldModelTransition:
    if persisted_at.tzinfo is None or persisted_at.utcoffset() is None:
        raise ValueError("world-model transition time must be timezone-aware")
    materializations = tuple(
        sorted(
            revision_materializations,
            key=lambda item: (item.source_update_receipt_sha256, item.directive_sha256),
        )
    )
    disposition, next_snapshot = _derive_transition_outputs(
        round_evidence=round_evidence,
        materializations=materializations,
        persistence_principal_sha256=persistence_principal_sha256,
        persisted_at=persisted_at,
    )
    return WorldModelTransition(
        transition_id=transition_id,
        round_evidence=round_evidence,
        revision_materializations=materializations,
        next_round_world_model_snapshot=next_snapshot,
        disposition=disposition,
        persistence_principal_sha256=persistence_principal_sha256,
        persisted_at=persisted_at,
    )


def _matching_transition_events(session, transition: WorldModelTransition) -> list[Event]:
    projection = _event_projection(transition).model_dump(mode="json")
    rows = session.scalars(
        select(Event).where(
            Event.event_key == f"f9-world-model-transition:{transition.transition_sha256}",
        )
    ).all()
    return [row for row in rows if row.payload == projection]


def persist_world_model_transition(
    transition: WorldModelTransition,
) -> WorldModelTransitionStoreReceipt:
    """Atomically persist every transition member plus one exact typed event projection."""

    source = transition.source_world_model_snapshot
    posterior = transition.posterior_world_model_snapshot
    update = transition.committed_update
    next_snapshot = transition.next_round_world_model_snapshot
    projection = _event_projection(transition)
    with session_scope() as session:
        _store_world_model_snapshot(session, source)
        _store_world_model_snapshot(session, posterior)
        for materialization in transition.revision_materializations:
            _store_hypothesis(session, materialization.revised_hypothesis)
            for prediction in materialization.revised_predictions:
                _store_prediction(session, prediction)
        if next_snapshot is not None:
            _store_world_model_snapshot(session, next_snapshot)

        inserted = session.scalar(
            postgresql_insert(EpistemicWorldModelTransitionRecord)
            .values(
                transition_sha256=transition.transition_sha256,
                transition_id=transition.transition_id,
                run_id=source.question.run_id,
                question_id=source.question.question_id,
                belief_lineage_id=source.belief_state.belief_lineage_id,
                source_update_receipt_sha256=update.receipt_sha256,
                source_snapshot_sha256=source.snapshot_sha256,
                posterior_snapshot_sha256=posterior.snapshot_sha256,
                next_round_snapshot_sha256=(
                    None if next_snapshot is None else next_snapshot.snapshot_sha256
                ),
                disposition=transition.disposition.value,
                persisted_at=transition.persisted_at,
                payload_json=_payload(transition),
            )
            .on_conflict_do_nothing()
            .returning(EpistemicWorldModelTransitionRecord.transition_sha256)
        )
        session.flush()
        row = session.get(EpistemicWorldModelTransitionRecord, transition.transition_sha256)
        if row is None:
            conflict = session.scalar(
                select(EpistemicWorldModelTransitionRecord).where(
                    or_(
                        EpistemicWorldModelTransitionRecord.transition_id
                        == transition.transition_id,
                        EpistemicWorldModelTransitionRecord.source_update_receipt_sha256
                        == update.receipt_sha256,
                    )
                )
            )
            if conflict is not None:
                raise ImmutableEpistemicConflict(
                    "world-model transition identity is already bound to different content"
                )
            raise EpistemicPersistenceError("could not persist world-model transition")
        stored = _parse_stored(
            WorldModelTransition, row.payload_json, label="world-model transition"
        )
        if stored != transition:
            raise ImmutableEpistemicConflict("persisted world-model transition content conflicts")

        created = inserted is not None
        event_id = persist_event(
            make_event(
                _TRANSITION_EVENT_TYPE,
                run_id=source.question.run_id,
                agent=_TRANSITION_EVENT_AGENT,
                payload=projection.model_dump(mode="json"),
            ),
            event_key=f"f9-world-model-transition:{transition.transition_sha256}",
            session=session,
        )
    return WorldModelTransitionStoreReceipt(
        transition_sha256=transition.transition_sha256,
        event_id=event_id,
        created=created,
    )


def get_world_model_transition(transition_sha256: str) -> WorldModelTransition:
    """Physically reload and revalidate a transition, all objects, and its typed event."""

    with session_scope() as session:
        row = session.get(EpistemicWorldModelTransitionRecord, transition_sha256)
        if row is None:
            raise EpistemicObjectNotFound(f"world-model transition not found: {transition_sha256}")
        try:
            transition = WorldModelTransition.model_validate(row.payload_json)
        except ValidationError as exc:
            raise EpistemicPersistenceError(
                "persisted world-model transition no longer validates"
            ) from exc
        if transition.transition_sha256 != transition_sha256:
            raise ImmutableEpistemicConflict("world-model transition SHA-256 changed")
        expected_columns = {
            "transition_id": transition.transition_id,
            "run_id": transition.source_world_model_snapshot.question.run_id,
            "question_id": transition.source_world_model_snapshot.question.question_id,
            "belief_lineage_id": (
                transition.source_world_model_snapshot.belief_state.belief_lineage_id
            ),
            "source_update_receipt_sha256": transition.committed_update.receipt_sha256,
            "source_snapshot_sha256": transition.source_world_model_snapshot.snapshot_sha256,
            "posterior_snapshot_sha256": (
                transition.posterior_world_model_snapshot.snapshot_sha256
            ),
            "next_round_snapshot_sha256": (
                None
                if transition.next_round_world_model_snapshot is None
                else transition.next_round_world_model_snapshot.snapshot_sha256
            ),
            "disposition": transition.disposition.value,
            "persisted_at": transition.persisted_at,
        }
        if any(getattr(row, key) != value for key, value in expected_columns.items()):
            raise ImmutableEpistemicConflict("world-model transition index columns conflict")
        for materialization in transition.revision_materializations:
            _require_exact_row(
                session,
                record_type=EpistemicHypothesisVersionRecord,
                key=materialization.revised_hypothesis.hypothesis_sha256,
                model_type=HypothesisVersion,
                expected=materialization.revised_hypothesis,
                hash_attribute="hypothesis_sha256",
                label="transition hypothesis revision",
            )
            for prediction in materialization.revised_predictions:
                _require_exact_row(
                    session,
                    record_type=EpistemicPredictionRecord,
                    key=prediction.prediction_sha256,
                    model_type=Prediction,
                    expected=prediction,
                    hash_attribute="prediction_sha256",
                    label="transition prediction revision",
                )
        if len(_matching_transition_events(session, transition)) != 1:
            raise EpistemicPersistenceError(
                "world-model transition does not have one exact typed event projection"
            )

    snapshots = (
        transition.source_world_model_snapshot,
        transition.posterior_world_model_snapshot,
        *(
            ()
            if transition.next_round_world_model_snapshot is None
            else (transition.next_round_world_model_snapshot,)
        ),
    )
    for expected in snapshots:
        if get_world_model_snapshot(expected.snapshot_sha256) != expected:
            raise ImmutableEpistemicConflict("world-model transition snapshot binding conflicts")
    return transition


_MANDATORY_CONTINUATION_CHECKS = frozenset(K3AcceptanceCheckKind) - {
    K3AcceptanceCheckKind.HIGH_BELIEF_DISCRIMINATION,
}


def load_authorized_next_round_source(
    *,
    transition_sha256: str,
    committed_acceptance: CommittedK3AcceptanceCampaign,
    acceptance_archive: ContentAddressedResponseArchive,
    authorized_at: datetime,
) -> CausalWorldModelSource:
    """Load a physical transition and authorize its exact next snapshot for F9-S3."""

    if authorized_at.tzinfo is None or authorized_at.utcoffset() is None:
        raise ValueError("next-round authorization time must be timezone-aware")
    transition = get_world_model_transition(transition_sha256)
    acceptance = committed_acceptance.campaign
    loaded_acceptance = load_k3_acceptance_campaign(
        archive=acceptance_archive,
        ledger=committed_acceptance.ledger,
    )
    if loaded_acceptance != acceptance:
        raise ValueError("physically archived K3 acceptance campaign changed")
    if acceptance.disposition not in {
        K3AcceptanceDisposition.ACCEPTED,
        K3AcceptanceDisposition.PARTIAL_NO_SCIENTIFIC_EXIT,
    }:
        raise ValueError("next round requires an integrity-valid K3 acceptance verdict")
    checks = {item.kind: item.status for item in acceptance.checks}
    if any(
        checks.get(kind) is K3AcceptanceCheckStatus.FAIL or checks.get(kind) is None
        for kind in _MANDATORY_CONTINUATION_CHECKS
    ):
        raise ValueError("next round is blocked by a mandatory K3 acceptance check")
    if (
        checks.get(K3AcceptanceCheckKind.POSITIVE_VALIDATED_UPDATE)
        is not K3AcceptanceCheckStatus.PASS
    ):
        raise ValueError("next round requires one positive validated update")
    if authorized_at < max(committed_acceptance.committed_at, transition.persisted_at):
        raise ValueError("next-round authorization predates its committed evidence")
    final_round = acceptance.request.rounds[-1]
    if final_round.round_sha256 != transition.round_evidence.round_sha256:
        raise ValueError("K3 acceptance final round does not match the persisted transition")
    decision = acceptance.request.committed_evidence_ledger.evidence.terminal_decision
    if decision.action not in {
        K3TerminalAction.CONTINUE_RESEARCH,
        K3TerminalAction.SEEK_NEW_MEASUREMENT,
    }:
        raise ValueError("K3 terminal action does not authorize another research round")
    if decision.source_update_receipt_sha256 != transition.committed_update.receipt_sha256:
        raise ValueError("K3 terminal decision changed the transition update binding")
    evidence = acceptance.request.committed_evidence_ledger.evidence
    if transition.persisted_at > evidence.persisted_at:
        raise ValueError("K3 evidence ledger predates physical world-model persistence")
    if evidence.persistence_principal_sha256 != transition.persistence_principal_sha256:
        raise ValueError("K3 evidence ledger changed the transition persistence principal")
    required_snapshots = {
        transition.source_world_model_snapshot.snapshot_sha256,
        transition.posterior_world_model_snapshot.snapshot_sha256,
    }
    if not required_snapshots.issubset(evidence.persisted_world_snapshot_sha256s):
        raise ValueError("K3 evidence ledger omits a transition snapshot")
    if transition.next_round_world_model_snapshot is None:
        raise ValueError("persisted transition requires a hypothesis-set fork before another round")
    return CausalWorldModelSource(
        snapshot=transition.next_round_world_model_snapshot,
        transition_sha256=transition.transition_sha256,
        acceptance_receipt_sha256=committed_acceptance.receipt_sha256,
        authorized_at=authorized_at,
    )


__all__ = [
    "WorldModelTransition",
    "WorldModelTransitionDisposition",
    "WorldModelTransitionEventProjection",
    "WorldModelTransitionStoreReceipt",
    "build_world_model_transition",
    "get_world_model_transition",
    "load_authorized_next_round_source",
    "persist_world_model_transition",
]
