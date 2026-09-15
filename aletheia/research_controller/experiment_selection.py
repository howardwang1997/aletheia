"""Mechanical discriminating-experiment selection for ARL-2 (roadmap S3).

One pure function chooses the round's discriminating experiment from the frozen
prior over preregistered candidate protocols.  Scoring is the ported
``memory/belief.py`` measured information gain only — the legacy campaign loop's
``min(llm, measured)`` cap degenerates to ``measured`` here because no LLM
number enters; that degeneration is the point of the port.  Eligibility is
mechanical: the candidate must bind the snapshot's exact preregistered
predictions for every active hypothesis, its capability must resolve in the
catalog as QUALIFIED, and its bound batch must still hold unspent groups for the
round.  Zero qualifying candidates fail closed: the caller gets a typed error
carrying every candidate's blocker codes, not a proposal.  The receipt is
hash-closed and self-checking; per decision 10 it lives in the campaign bundle
and the deployment template provenance, never on the action proposal.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from aletheia.memory.belief import Credence, normalized_information_gain
from aletheia.protocols.capabilities import CapabilityCatalog, QualificationStatus
from aletheia.protocols.world_models import (
    HypothesisLifecycle,
    WorldModelSnapshotV2,
)
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_kernel.schemas import canonical_sha256

SELECTION_RULE_TEXT = (
    "Eligibility: the candidate protocol binds the snapshot's preregistered "
    "predictions over its exact measurement context for every active "
    "hypothesis, its capability manifest resolves in the catalog as QUALIFIED, "
    "and its bound batch intersects the round's unspent groups.  Score: "
    "EIG(P) = sum over hypotheses of normalized_information_gain over a "
    "mass-2 Beta credence centered on the frozen prior probability, with "
    "p_holds equal to that same prior probability.  Floor: scores below "
    "eig_floor are ineligible.  Choice: maximum EIG; ties break to the "
    "lexicographically smallest protocol_sha256."
)
SELECTION_RULE_SHA256 = hashlib.sha256(SELECTION_RULE_TEXT.encode("utf-8")).hexdigest()


class SelectionBlocker(str, Enum):
    PREDICTIONS_NOT_BOUND = "predictions_not_bound"
    CAPABILITY_NOT_QUALIFIED = "capability_not_qualified"
    BATCH_SPENT_OR_EMPTY = "batch_spent_or_empty"
    BELOW_EIG_FLOOR = "below_eig_floor"


class HypothesisOutcomeBinding(ControllerModel):
    hypothesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predicted_outcome_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectionCandidate(ControllerModel):
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observable_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measurement_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_space_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predicted_outcomes: tuple[HypothesisOutcomeBinding, ...] = Field(min_length=2)
    capability_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_batch_group_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bindings_are_canonical(self) -> "SelectionCandidate":
        ordered = tuple(
            sorted(self.predicted_outcomes, key=lambda item: item.hypothesis_sha256)
        )
        if ordered != self.predicted_outcomes or len(
            {item.hypothesis_sha256 for item in ordered}
        ) != len(ordered):
            raise ValueError("predicted outcomes must bind each hypothesis once, in hash order")
        if tuple(sorted(self.bound_batch_group_ids)) != self.bound_batch_group_ids or len(
            set(self.bound_batch_group_ids)
        ) != len(self.bound_batch_group_ids):
            raise ValueError("bound batch groups must be sorted and unique")
        return self


class SelectionPolicy(ControllerModel):
    eig_floor: float = Field(ge=0.0, le=1.0)
    selection_rule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _pins_the_preregistered_rule(self) -> "SelectionPolicy":
        if self.selection_rule_sha256 != SELECTION_RULE_SHA256:
            raise ValueError("selection policy pins another rule")
        return self


class SplitState(ControllerModel):
    round_index: int = Field(ge=1)
    unspent_group_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _unspent_groups_are_canonical(self) -> "SplitState":
        if tuple(sorted(self.unspent_group_ids)) != self.unspent_group_ids or len(
            set(self.unspent_group_ids)
        ) != len(self.unspent_group_ids):
            raise ValueError("unspent groups must be sorted and unique")
        return self


class CandidateSelectionRecord(ControllerModel):
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eig: float = Field(ge=0.0)
    eligible: bool
    blocker_codes: tuple[SelectionBlocker, ...] = ()

    @model_validator(mode="after")
    def _blockers_match_eligibility(self) -> "CandidateSelectionRecord":
        if tuple(sorted(self.blocker_codes, key=lambda item: item.value)) != self.blocker_codes:
            raise ValueError("blocker codes must be sorted and unique")
        if self.eligible == bool(self.blocker_codes):
            raise ValueError("eligibility must match the blocker record")
        return self


class ExperimentSelectionReceipt(ControllerModel):
    schema_name: Literal["aletheia.experiment_selection_receipt"] = (
        "aletheia.experiment_selection_receipt"
    )
    schema_version: Literal[1] = 1
    selection_policy: SelectionPolicy
    split_state: SplitState
    world_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_records: tuple[CandidateSelectionRecord, ...] = Field(min_length=1)
    chosen_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chosen_eig: float = Field(ge=0.0)
    authored_by_principal_id: str
    authored_at: datetime

    @model_validator(mode="after")
    def _receipt_is_self_checking(self) -> "ExperimentSelectionReceipt":
        ordered = tuple(
            sorted(self.candidate_records, key=lambda item: item.protocol_sha256)
        )
        if ordered != self.candidate_records or len(
            {item.protocol_sha256 for item in ordered}
        ) != len(ordered):
            raise ValueError("candidate records must be sorted by protocol hash, one per protocol")
        by_protocol = {item.protocol_sha256: item for item in self.candidate_records}
        chosen = by_protocol.get(self.chosen_protocol_sha256)
        if chosen is None:
            raise ValueError("chosen protocol is absent from the candidate records")
        if not chosen.eligible:
            raise ValueError("chosen protocol is not eligible")
        if self.chosen_eig != chosen.eig:
            raise ValueError("chosen score does not match the chosen record")
        best = max(item.eig for item in self.candidate_records if item.eligible)
        leaders = min(
            item.protocol_sha256
            for item in self.candidate_records
            if item.eligible and item.eig == best
        )
        if self.chosen_protocol_sha256 != leaders:
            raise ValueError("chosen protocol is not the smallest-hash maximum-score candidate")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self)


class ExperimentSelectionError(ValueError):
    """No candidate qualified; carries every candidate's blocker codes."""

    def __init__(self, records: tuple[CandidateSelectionRecord, ...]) -> None:
        self.records = records
        summary = ", ".join(
            f"{record.protocol_sha256[:12]}="
            + "+".join(code.value for code in record.blocker_codes)
            for record in records
        )
        super().__init__(f"no qualifying discriminating experiment: {summary}")


def _prior_eig(world_model: WorldModelSnapshotV2) -> float:
    """Score one protocol: measured information gain over the frozen prior.

    Zero-credence entries (the retained rejections) contribute exactly zero,
    so the score sums the active hypotheses' expected entropy reduction under
    the round's preregistered belief state.
    """

    return math.fsum(
        normalized_information_gain(
            Credence(
                2.0 * belief.probability,
                2.0 * (1.0 - belief.probability),
            ),
            belief.probability,
        )
        for belief in world_model.belief_state.hypothesis_beliefs
    )


def _binds_snapshot_predictions(
    candidate: SelectionCandidate,
    world_model: WorldModelSnapshotV2,
    active_hashes: tuple[str, ...],
) -> bool:
    """The candidate commits exactly the snapshot's preregistered bins for its triple."""

    triple = (
        candidate.observable_spec_sha256,
        candidate.measurement_protocol_sha256,
        candidate.outcome_space_sha256,
    )
    registered = {
        item.hypothesis_sha256: item.predicted_outcome_sha256
        for item in world_model.predictions
        if (
            item.observable_spec_sha256,
            item.measurement_protocol_sha256,
            item.outcome_space_sha256,
        )
        == triple
    }
    committed = {
        item.hypothesis_sha256: item.predicted_outcome_sha256
        for item in candidate.predicted_outcomes
    }
    if set(committed) != set(active_hashes):
        return False
    return all(registered.get(key) == value for key, value in committed.items())


def select_discriminating_experiment(
    *,
    world_model: WorldModelSnapshotV2,
    candidates: tuple[SelectionCandidate, ...],
    selection_policy: SelectionPolicy,
    split_state: SplitState,
    capability_catalog: CapabilityCatalog,
    authored_at: datetime,
    principal_id: str,
) -> ExperimentSelectionReceipt:
    """Choose the round's discriminating experiment mechanically, or fail closed.

    Every blocker is recorded per candidate; the receipt self-checks the
    choice, so a tampered receipt fails its own validation.
    """

    active_hashes = tuple(
        sorted(
            item.hypothesis_sha256
            for item in world_model.hypotheses
            if item.lifecycle is HypothesisLifecycle.ACTIVE
        )
    )
    if len({item.protocol_sha256 for item in candidates}) != len(candidates):
        raise ValueError("candidate protocols must be unique")

    discrimination_broken = False
    try:
        world_model.assert_hypothesis_discrimination(active_hashes)
    except ValueError:
        discrimination_broken = True

    records: list[CandidateSelectionRecord] = []
    for candidate in candidates:
        blockers: list[SelectionBlocker] = []
        if discrimination_broken or not _binds_snapshot_predictions(
            candidate, world_model, active_hashes
        ):
            blockers.append(SelectionBlocker.PREDICTIONS_NOT_BOUND)
        try:
            manifest = capability_catalog.get_exact(candidate.capability_manifest_sha256)
        except LookupError:
            manifest = None
        if manifest is None or (
            manifest.qualification.status is not QualificationStatus.QUALIFIED
        ):
            blockers.append(SelectionBlocker.CAPABILITY_NOT_QUALIFIED)
        if not set(candidate.bound_batch_group_ids) & set(split_state.unspent_group_ids):
            blockers.append(SelectionBlocker.BATCH_SPENT_OR_EMPTY)
        eig = _prior_eig(world_model)
        if eig < selection_policy.eig_floor:
            blockers.append(SelectionBlocker.BELOW_EIG_FLOOR)
        records.append(
            CandidateSelectionRecord(
                protocol_sha256=candidate.protocol_sha256,
                eig=eig,
                eligible=not blockers,
                blocker_codes=tuple(sorted(blockers, key=lambda item: item.value)),
            )
        )

    ordered = tuple(sorted(records, key=lambda item: item.protocol_sha256))
    eligible = tuple(item for item in ordered if item.eligible)
    if not eligible:
        raise ExperimentSelectionError(ordered)
    best = max(item.eig for item in eligible)
    chosen = min(
        (item for item in eligible if item.eig == best),
        key=lambda item: item.protocol_sha256,
    )
    return ExperimentSelectionReceipt(
        selection_policy=selection_policy,
        split_state=split_state,
        world_model_sha256=world_model.world_model_sha256,
        capability_catalog_sha256=capability_catalog.catalog_sha256,
        candidate_records=ordered,
        chosen_protocol_sha256=chosen.protocol_sha256,
        chosen_eig=chosen.eig,
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )
