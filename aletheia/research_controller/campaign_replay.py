"""Pure replay verification and grading adapters for the ARL-2 question campaign.

The campaign bundle is the exported custody of one finished campaign: the registered
dataset card and its CSV bytes, the ``as_of``-pinned kernel event stream, the
card-derived round-split binding policy of each round, each round's staged
bound-batch rows, and each round's experiment-selection receipt.  Everything this
module concludes is re-derived from those bytes alone; it reads no database, no
filesystem, and no clock, and fails closed on any disagreement.

Split-ledger conventions (DESIGN_B8 decisions D5/D6, PI-approved 2026-09-16):

- ``sealed(i)`` is round ``i``'s OWN partition of the card's formula groups.  The
  two partitions are disjoint by the split formula and neither is ever the full
  group set.
- The campaign spent ledger is the union of the staged batches whose action
  reached OBSERVATION_INCORPORATED.  A compiled-but-unadmitted batch stays
  unspent; there is no table, only the hash-closed event stream.
- The caller parameters a round's protocol pins (and the policy rows carry)
  record the RAW campaign ledger at that round's authoring time: round 1 pins the
  empty ledger, round 2 pins round 1's admitted batch.
- The tuple-level closure ``verify_round_split_binding`` runs per round on the
  round-scoped projection ``spent = ledger ∩ sealed(i)``, ``unspent =
  sealed(i) − ledger``, so its ``spent ∪ unspent == sealed`` invariant holds per
  round while the raw cross-round ledger rides the pinned set hashes.

The two grading adapters (decision D13) sit at the call boundary to the scheduler's
pure scorers: kernel events plus per-round grade records become the ``score_k2``
event and credence tables, and an admitted diagnostic result plus its AnalysisPlan
become the ``classify_outcome`` demonstration dict.  Both mapping tables are
module-level data, reviewable in one place.
"""

from __future__ import annotations

import csv as csv_module
import hashlib
import io
import json
import math
from collections import Counter
from typing import Any, Literal, Mapping

from pydantic import AwareDatetime, Field, model_validator

from aletheia.memory.belief import WEAK_PRIOR_MAX_MASS
from aletheia.protocols.data_registration import (
    RegisteredDatasetV1,
    enumerate_formula_groups,
    partition_group_rounds,
    verify_dataset_card,
    verify_group_partition,
)
from aletheia.protocols.schemas import AnalysisPlan
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.continuation_stop import count_rounds_observed
from aletheia.research_controller.experiment_selection import (
    ExperimentSelectionReceipt,
    SplitState,
)
from aletheia.research_controller.external_rpc import CuprateDiagnosticResult
from aletheia.research_controller.protocol_compilation_step import (
    RoundSplitBindingPolicyV1,
)
from aletheia.research_controller.world_model_revision import verify_round_split_binding
from aletheia.research_kernel.schemas import (
    ActionProposedPayload,
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
    canonical_json_bytes,
    canonical_sha256,
)
from aletheia.execution.cuprate.exact_content import combined_outcome_bin_id
from aletheia.scheduler.outcome import classify_outcome

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BUNDLE_KIND_PATTERN = r"^[a-z0-9:_.-]{1,128}$"

DATASET_CARD_KIND = "campaign:dataset_card"
DATASET_CONTENT_KIND = "campaign:dataset_content"
KERNEL_STREAM_KIND = "campaign:kernel_event_stream"
ROUND_SPLIT_POLICY_KIND_PREFIX = "campaign:round_split_policy:round-"
STAGED_ROUND_ROWS_KIND_PREFIX = "campaign:staged_round_rows:round-"
SELECTION_RECEIPT_KIND_PREFIX = "campaign:selection_receipt:round-"


class CampaignBundleEntryV1(ControllerModel):
    """One object in the exported campaign bundle, indexed by kind."""

    schema_name: Literal["aletheia.campaign_bundle_entry"] = "aletheia.campaign_bundle_entry"
    schema_version: Literal[1] = 1
    object_kind: str = Field(pattern=_BUNDLE_KIND_PATTERN)
    object_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_length: int = Field(ge=1)
    canonical_json: bool

    @property
    def entry_sha256(self) -> str:
        return canonical_sha256(self)


class CampaignBundleManifestV1(ControllerModel):
    """The canonical-sha index of one exported campaign bundle."""

    schema_name: Literal["aletheia.campaign_bundle_manifest"] = "aletheia.campaign_bundle_manifest"
    schema_version: Literal[1] = 1
    quest_id: str = Field(pattern=r"^qst_[0-9a-f]{32}$")
    as_of: AwareDatetime
    entries: tuple[CampaignBundleEntryV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _entries_are_canonical(self) -> CampaignBundleManifestV1:
        kinds = tuple(item.object_kind for item in self.entries)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("bundle entries must be unique and canonical by kind")
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self)

    def entry_for(self, object_kind: str) -> CampaignBundleEntryV1 | None:
        for entry in self.entries:
            if entry.object_kind == object_kind:
                return entry
        return None

    def require_entry(self, object_kind: str) -> CampaignBundleEntryV1:
        entry = self.entry_for(object_kind)
        if entry is None:
            raise ValueError(f"campaign bundle does not carry {object_kind}")
        return entry


class StagedRowV1(ControllerModel):
    """One staged data row's split-relevant projection: group id and target."""

    schema_name: Literal["aletheia.staged_round_row"] = "aletheia.staged_round_row"
    schema_version: Literal[1] = 1
    composition: str = Field(min_length=1, max_length=255)
    target: float

    @model_validator(mode="after")
    def _target_is_finite(self) -> StagedRowV1:
        if not math.isfinite(self.target):
            raise ValueError("staged row target must be finite")
        return self


class StagedRoundRowsV1(ControllerModel):
    """The concrete bound-batch rows staged for one round's execution.

    Rows are the split-relevant projection of the staged ``input.dataset_rows``
    artifact (D10): concrete group ids exist here and in replay's re-derivation
    only, while templates and policy rows carry set hashes.  The
    ``staged_artifact_sha256`` field is the custody pointer to the full artifact
    the projection came from.
    """

    schema_name: Literal["aletheia.staged_round_rows"] = "aletheia.staged_round_rows"
    schema_version: Literal[1] = 1
    round_index: int = Field(ge=1, le=2)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    staged_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    rows: tuple[StagedRowV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rows_are_canonical(self) -> StagedRoundRowsV1:
        ordered = tuple(sorted((row.composition, row.target) for row in self.rows))
        if ordered != tuple((row.composition, row.target) for row in self.rows):
            raise ValueError("staged rows must be sorted by composition and target")
        if len(set(ordered)) != len(ordered):
            raise ValueError("staged rows must be unique")
        return self

    @property
    def group_ids(self) -> tuple[str, ...]:
        return tuple(sorted({row.composition for row in self.rows}))


class RoundSplitLedgerV1(ControllerModel):
    """The re-derived split facts of one staged round, as the verifier reports them."""

    schema_name: Literal["aletheia.round_split_ledger"] = "aletheia.round_split_ledger"
    schema_version: Literal[1] = 1
    round_index: int = Field(ge=1, le=2)
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    bound_group_ids: tuple[str, ...] = Field(min_length=1)
    spent_group_ids: tuple[str, ...]
    unspent_group_ids: tuple[str, ...]
    admitted: bool


class CampaignReplayReportV1(ControllerModel):
    """What the pure bundle verifier re-derived, for the driver and the receipt."""

    schema_name: Literal["aletheia.campaign_replay_report"] = "aletheia.campaign_replay_report"
    schema_version: Literal[1] = 1
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    dataset_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    rounds_observed: int = Field(ge=0)
    admitted_observation_events: int = Field(ge=0)
    terminal_stop_committed: Literal[True]
    campaign_spent_group_ids: tuple[str, ...]
    ledgers: tuple[RoundSplitLedgerV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ledgers_are_canonical(self) -> CampaignReplayReportV1:
        keys = tuple((item.round_index, item.action_sha256) for item in self.ledgers)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("replay ledgers must be unique and canonical")
        return self


class RoundGradeRecordV1(ControllerModel):
    """One admitted round's graded facts feeding the K2 event and credence tables.

    ``demonstration`` is the :func:`observation_to_classify_demo` output for the
    round's admitted diagnostic; the typed reason re-derives from it through
    ``classify_outcome`` at the adapter boundary, so a bundle and its grade
    records cannot disagree about why a round ended.  ``computed`` and
    ``exploration_applied`` are Literals: a grade record cannot assert a verdict
    the harness did not compute on the explore-to-confirm path.
    """

    schema_name: Literal["aletheia.round_grade_record"] = "aletheia.round_grade_record"
    schema_version: Literal[1] = 1
    round_index: int = Field(ge=1, le=2)
    action_id: str = Field(min_length=3, max_length=128)
    question_key: str = Field(min_length=1, max_length=255)
    outcome: Literal["positive", "negative", "inconclusive"]
    demonstration: dict[str, Any]
    computed: Literal[True] = True
    exploration_applied: Literal[True] = True
    predicted_p_holds: float = Field(ge=0.0, le=1.0)
    prior_alpha: float = Field(gt=0.0)
    prior_beta: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _outcome_matches_demonstration(self) -> RoundGradeRecordV1:
        holds = self.demonstration.get("holds")
        expected = None if holds is None else ("positive" if holds else "negative")
        if self.outcome != expected and self.outcome != "inconclusive":
            raise ValueError("grade outcome disagrees with its demonstration verdict")
        if self.outcome == "inconclusive" and holds is not None:
            raise ValueError("an inconclusive grade cannot carry a boolean verdict")
        return self

    @property
    def realized(self) -> float | None:
        if self.outcome == "positive":
            return 1.0
        if self.outcome == "negative":
            return 0.0
        return None


def _set_sha(group_ids: tuple[str, ...]) -> str:
    """Set-hash convention: canonical sha over the sorted-unique id list."""

    return canonical_sha256(list(group_ids))


def kernel_stream_bytes(events: tuple[ResearchEvent, ...]) -> bytes:
    """Canonical export bytes of the audited kernel event stream."""

    return canonical_json_bytes(
        [event.model_dump(mode="json", exclude_none=True) for event in events]
    )


def kernel_stream_sha256(events: tuple[ResearchEvent, ...]) -> str:
    return hashlib.sha256(kernel_stream_bytes(events)).hexdigest()


def _round_partitions(
    card: RegisteredDatasetV1, csv_bytes: bytes
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Enumerate the card's groups and partition them by the card's split formula."""

    csv_text = csv_bytes.decode("utf-8")
    groups = enumerate_formula_groups(csv_text, card.column_manifest)
    round_one, round_two = partition_group_rounds(card.content_sha256, card.split_policy, groups)
    verify_group_partition(card.content_sha256, card.split_policy, groups, round_one, round_two)
    return round_one, round_two


def sealed_groups_for_round(
    card: RegisteredDatasetV1, csv_bytes: bytes, *, round_index: int
) -> tuple[str, ...]:
    """The round's OWN partition (D5): never the other round's, never the union."""

    if round_index not in (1, 2):
        raise ValueError("round split partitions cover rounds 1 and 2 only")
    round_one, round_two = _round_partitions(card, csv_bytes)
    return round_one if round_index == 1 else round_two


def verify_pre_registered_round_splits(
    *,
    card: RegisteredDatasetV1,
    csv_bytes: bytes,
    round_split_bindings: tuple[RoundSplitBindingPolicyV1, ...],
) -> None:
    """D8's pre-registration check: re-derive each sealed round from card bytes alone.

    The bundle verifier re-runs the same equality inside verify_campaign_bundle
    once events and a manifest exist; this entry point is the driver's register-time
    guarantee that the pinned split bindings already bind the pinned dataset, before
    the campaign commits anything.
    """

    if tuple(binding.round_index for binding in round_split_bindings) != (1, 2):
        raise ValueError("pre-registered round splits cover rounds 1 and 2 only")
    round_one, round_two = _round_partitions(card, csv_bytes)
    sealed_by_round = {1: round_one, 2: round_two}
    for binding in round_split_bindings:
        sealed = sealed_by_round[binding.round_index]
        if binding.dataset_content_sha256 != card.content_sha256:
            raise ValueError("round-split policy binds a different dataset identity")
        if binding.split_policy_sha256 != card.split_policy.policy_sha256:
            raise ValueError("round-split policy binds a different split policy")
        if binding.sealed_group_ids_sha256 != _set_sha(sealed):
            raise ValueError("round-split policy sealed set differs from the card partition")


def _card_row_pairs(card: RegisteredDatasetV1, csv_bytes: bytes) -> Counter[tuple[str, float]]:
    """Multiset of (composition, target) pairs of the registered content.

    Rows whose target fails to parse or is not finite are skipped, matching
    ``enumerate_formula_groups`` exactly, so the multiset the staged rows are
    checked against covers the same rows the partitions were derived from.
    """

    reader = csv_module.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    composition = card.column_manifest.composition_column
    target = card.column_manifest.target_column
    pairs: Counter[tuple[str, float]] = Counter()
    for record in reader:
        try:
            target_value = float(record.get(target) or "")
        except (TypeError, ValueError):
            continue
        if not math.isfinite(target_value):
            continue
        pairs[((record.get(composition) or "").strip(), target_value)] += 1
    return pairs


def build_round_split_state(
    *,
    card: RegisteredDatasetV1,
    csv_bytes: bytes,
    round_index: int,
    spent_group_ids: tuple[str, ...],
) -> SplitState:
    """Derive the round's selection split state (D9): ``unspent = sealed − spent``.

    ``spent_group_ids`` is the raw campaign ledger; groups from another round's
    partition do not subtract from this round's unspent set.
    """

    if tuple(spent_group_ids) != tuple(sorted(set(spent_group_ids))):
        raise ValueError("spent group ids must be unique and canonically ordered")
    sealed = sealed_groups_for_round(card, csv_bytes, round_index=round_index)
    unspent = tuple(sorted(set(sealed) - set(spent_group_ids)))
    if not unspent:
        raise ValueError("round split state has no unspent groups left to bind")
    return SplitState(round_index=round_index, unspent_group_ids=unspent)


def campaign_spent_groups(
    *,
    events: tuple[ResearchEvent, ...],
    staged_rounds: tuple[StagedRoundRowsV1, ...],
) -> tuple[str, ...]:
    """The raw campaign spent ledger (D6): staged batches whose action was admitted.

    Admission is OBSERVATION_INCORPORATED in the exported stream.  The join runs
    through ACTION_PROPOSED because the incorporation payload carries the action
    id while staged rows carry the action sha.
    """

    admitted_action_ids = {
        event.payload.action_id
        for event in events
        if event.event_type is EventType.OBSERVATION_INCORPORATED
        and isinstance(event.payload, ObservationIncorporatedPayload)
    }
    action_sha_by_id = {
        event.payload.action_ref.object_id: event.payload.action_ref.object_sha256
        for event in events
        if event.event_type is EventType.ACTION_PROPOSED
        and isinstance(event.payload, ActionProposedPayload)
    }
    spent: set[str] = set()
    for staged in staged_rounds:
        action_id = next(
            (aid for aid, sha in action_sha_by_id.items() if sha == staged.action_sha256),
            None,
        )
        if action_id is not None and action_id in admitted_action_ids:
            spent |= set(staged.group_ids)
    return tuple(sorted(spent))


def verify_campaign_bundle(
    *,
    card: RegisteredDatasetV1,
    csv_bytes: bytes,
    events: tuple[ResearchEvent, ...],
    round_split_bindings: tuple[RoundSplitBindingPolicyV1, ...],
    staged_rounds: tuple[StagedRoundRowsV1, ...],
    manifest: CampaignBundleManifestV1,
    objects: Mapping[str, bytes],
) -> CampaignReplayReportV1:
    """Re-derive the whole campaign from bundle bytes; fail closed on any mismatch."""

    if not events:
        raise ValueError("campaign bundle carries an empty kernel event stream")
    if manifest.quest_id != events[0].quest_id or any(
        event.quest_id != manifest.quest_id for event in events
    ):
        raise ValueError("campaign bundle stream and manifest disagree on the quest")
    sequences = tuple(event.sequence for event in events)
    if sequences != tuple(sorted(set(sequences))):
        raise ValueError("campaign bundle stream is not strictly ordered by sequence")

    # 1. Every manifest entry resolves to supplied bytes of the pinned identity.
    for entry in manifest.entries:
        payload = objects.get(entry.object_sha256)
        if payload is None:
            raise ValueError(f"bundle object {entry.object_kind} is not supplied")
        if hashlib.sha256(payload).hexdigest() != entry.object_sha256:
            raise ValueError(f"bundle object {entry.object_kind} bytes do not match its sha")
        if len(payload) != entry.byte_length:
            raise ValueError(f"bundle object {entry.object_kind} byte count differs")

    # 2. Typed artifacts are the manifest's pinned bytes.
    if manifest.require_entry(DATASET_CARD_KIND).object_sha256 != canonical_sha256(card):
        raise ValueError("the supplied dataset card is not the bundle's pinned card")
    if (
        manifest.require_entry(DATASET_CONTENT_KIND).object_sha256
        != hashlib.sha256(csv_bytes).hexdigest()
    ):
        raise ValueError("the supplied dataset bytes are not the bundle's pinned content")
    if manifest.require_entry(KERNEL_STREAM_KIND).object_sha256 != kernel_stream_sha256(events):
        raise ValueError("the supplied kernel stream is not the bundle's pinned stream")
    bindings_by_round: dict[int, RoundSplitBindingPolicyV1] = {}
    for binding in round_split_bindings:
        kind = f"{ROUND_SPLIT_POLICY_KIND_PREFIX}round-{binding.round_index:03d}"
        if binding.round_index in bindings_by_round:
            raise ValueError("one round-split binding policy per round")
        if manifest.require_entry(kind).object_sha256 != canonical_sha256(binding):
            raise ValueError(
                f"round {binding.round_index} binding is not the bundle's pinned policy"
            )
        bindings_by_round[binding.round_index] = binding
    staged_by_round: dict[int, list[StagedRoundRowsV1]] = {}
    for staged in staged_rounds:
        kind = f"{STAGED_ROUND_ROWS_KIND_PREFIX}round-{staged.round_index:03d}"
        if manifest.require_entry(kind).object_sha256 != canonical_sha256(staged):
            raise ValueError(
                f"round {staged.round_index} staged rows are not the bundle's pinned rows"
            )
        staged_by_round.setdefault(staged.round_index, []).append(staged)
    orphan_rounds = sorted(set(bindings_by_round) - set(staged_by_round))
    if orphan_rounds:
        raise ValueError(f"bound rounds {orphan_rounds} stage no rows in the bundle")
    unbound_rounds = sorted(set(staged_by_round) - set(bindings_by_round))
    if unbound_rounds:
        raise ValueError(f"staged rounds {unbound_rounds} carry no binding policy")

    # 3. The card's own audit re-derives from the pinned bytes.
    verify_dataset_card(card, csv_bytes)

    # 4. The card-derived partitions re-derive and match the pinned policy facts.
    round_one, round_two = _round_partitions(card, csv_bytes)
    sealed_by_round = {1: round_one, 2: round_two}
    card_pairs = _card_row_pairs(card, csv_bytes)
    action_sha_by_id = {
        event.payload.action_ref.object_id: event.payload.action_ref.object_sha256
        for event in events
        if event.event_type is EventType.ACTION_PROPOSED
        and isinstance(event.payload, ActionProposedPayload)
    }
    admitted_action_ids = {
        event.payload.action_id
        for event in events
        if event.event_type is EventType.OBSERVATION_INCORPORATED
        and isinstance(event.payload, ObservationIncorporatedPayload)
    }

    ledgers: list[RoundSplitLedgerV1] = []
    spent: set[str] = set()
    for round_index in sorted(bindings_by_round):
        binding = bindings_by_round[round_index]
        sealed = sealed_by_round[round_index]
        if binding.dataset_content_sha256 != card.content_sha256:
            raise ValueError("round-split policy binds a different dataset identity")
        if binding.split_policy_sha256 != card.split_policy.policy_sha256:
            raise ValueError("round-split policy binds a different split policy")
        if binding.sealed_group_ids_sha256 != _set_sha(sealed):
            raise ValueError("round-split policy sealed set differs from the card partition")
        for staged in sorted(staged_by_round[round_index], key=lambda item: item.action_sha256):
            if staged.action_sha256 not in action_sha_by_id.values():
                raise ValueError("staged round's action is absent from the exported stream")
            row = next(
                (
                    item
                    for item in binding.template_bindings
                    if item.action_sha256 == staged.action_sha256
                ),
                None,
            )
            if row is None:
                raise ValueError("round-split policy has no row for the staged action")
            batch = staged.group_ids
            staged_pairs = Counter((row_.composition, row_.target) for row_ in staged.rows)
            if any(staged_pairs[pair] > card_pairs[pair] for pair in staged_pairs):
                raise ValueError("staged rows contain pairs the registered card does not carry")
            ledger_before = tuple(sorted(spent))
            unspent = tuple(sorted(set(sealed) - spent))
            if row.bound_batch_group_ids_sha256 != _set_sha(batch):
                raise ValueError("staged batch differs from its pinned bound-batch set hash")
            if row.spent_group_ids_sha256 != _set_sha(ledger_before):
                raise ValueError("policy row spent set differs from the re-derived ledger")
            if row.unspent_group_ids_sha256 != _set_sha(unspent):
                raise ValueError("policy row unspent set differs from the re-derived ledger")
            verify_round_split_binding(
                sealed_group_ids=sealed,
                spent_group_ids=tuple(sorted(spent & set(sealed))),
                unspent_group_ids=unspent,
                bound_group_ids=batch,
            )
            receipt_kind = f"{SELECTION_RECEIPT_KIND_PREFIX}round-{round_index:03d}"
            receipt_entry = manifest.entry_for(receipt_kind)
            if receipt_entry is not None:
                receipt = ExperimentSelectionReceipt.model_validate(
                    json.loads(objects[receipt_entry.object_sha256])
                )
                if (
                    receipt.split_state.round_index != round_index
                    or receipt.split_state.unspent_group_ids != unspent
                ):
                    raise ValueError("selection receipt disagrees with the re-derived split state")
            staged_action_ids = {
                aid for aid, sha in action_sha_by_id.items() if sha == staged.action_sha256
            }
            admitted = not staged_action_ids.isdisjoint(admitted_action_ids)
            if admitted:
                spent |= set(batch)
            ledgers.append(
                RoundSplitLedgerV1(
                    round_index=round_index,
                    action_sha256=staged.action_sha256,
                    bound_group_ids=batch,
                    spent_group_ids=ledger_before,
                    unspent_group_ids=unspent,
                    admitted=admitted,
                )
            )

    # 5. The stream is terminal and the as_of pin is the final stop commitment.
    stop_events = tuple(event for event in events if event.event_type is EventType.STOP_COMMITTED)
    if not stop_events:
        raise ValueError("campaign bundle stream is not terminal")
    if manifest.as_of != stop_events[-1].committed_at:
        raise ValueError("bundle as_of pin is not the final stop commitment time")
    if any(event.committed_at > manifest.as_of for event in events):
        raise ValueError("campaign bundle carries events beyond its as_of pin")

    genesis = events[0]
    if genesis.event_type is not EventType.CHARTER_ACTIVATED:
        raise ValueError("campaign bundle stream does not open with a charter activation")
    root_branch_id = genesis.payload.root_branch_id
    return CampaignReplayReportV1(
        manifest_sha256=manifest.manifest_sha256,
        dataset_content_sha256=card.content_sha256,
        rounds_observed=count_rounds_observed(events, branch_id=root_branch_id),
        admitted_observation_events=len(admitted_action_ids),
        terminal_stop_committed=True,
        campaign_spent_group_ids=tuple(sorted(spent)),
        ledgers=tuple(sorted(ledgers, key=lambda item: (item.round_index, item.action_sha256))),
    )


# --- grading adapters (D13) ----------------------------------------------------------------

#: The scored-event vocabulary ``score_k2`` reads (scheduler/k2_acceptance.py).
SCORED_EVENT_VOCABULARY = frozenset(
    {
        "experiment",
        "demonstration",
        "belief_prior",
        "belief_prediction",
        "belief_update",
        "campaign_reason",
        "campaign_plan",
        "campaign_finished",
    }
)

#: Mapping of every kernel ``EventType`` to the scored event it feeds, or ``None``
#: when the type is campaign framing or action lifecycle with no scored
#: counterpart.  Committed transitions are the plan/finished backbone;
#: observation incorporations carry the per-round verdicts.  The completeness
#: test enumerates the EventType enum against this table, because a missing row
#: would silently downgrade an honest FULL run to PARTIAL.
KERNEL_EVENT_SCORE_TABLE: tuple[tuple[EventType, str | None], ...] = (
    (EventType.CHARTER_ACTIVATED, None),
    (EventType.CHARTER_REVISED, None),
    (EventType.OPPORTUNITY_RECORDED, None),
    (EventType.PROBLEM_ADMITTED, None),
    (EventType.QUESTION_ADMITTED, None),
    (EventType.ACTION_PROPOSED, None),
    (EventType.ACTION_AUTHORIZED, None),
    (EventType.ACTION_REJECTED, None),
    (EventType.ACTION_SUPERSEDED, None),
    (EventType.OBSERVATION_INCORPORATED, "demonstration"),
    (EventType.CONTINUE_COMMITTED, "campaign_plan"),
    (EventType.ACTIVATE_COMMITTED, "campaign_plan"),
    (EventType.REFINE_COMMITTED, "campaign_plan"),
    (EventType.FORK_COMMITTED, "campaign_plan"),
    (EventType.BACKTRACK_COMMITTED, "campaign_plan"),
    (EventType.PAUSE_COMMITTED, "campaign_plan"),
    (EventType.STOP_COMMITTED, "campaign_finished"),
)

_D1_EXCLUDES = "family_excess_ci_excludes_zero"
_D2_CONCENTRATES = "error_concentrates_at_extremes"

#: The preregistered d1 x d2 disposition table (KEYSTONE_A §3, DESIGN_B8 §5).
FOUR_WAY_DISPOSITIONS: tuple[tuple[str, str, str], ...] = (
    (_D1_EXCLUDES, _D2_CONCENTRATES, "h1_direction_supported"),
    (_D1_EXCLUDES, "no_stratification_structure", "family_excess_without_doping_structure"),
    ("family_excess_ci_includes_zero", _D2_CONCENTRATES, "structure_without_family_excess"),
    ("family_excess_ci_includes_zero", "no_stratification_structure", "h2_null"),
)


def four_way_disposition(result: CuprateDiagnosticResult) -> str:
    """Mechanical d1 x d2 disposition of one admitted diagnostic result."""

    for d1_bin, d2_bin, disposition in FOUR_WAY_DISPOSITIONS:
        if (
            result.d1_matched_control.outcome_bin == d1_bin
            and result.d2_doping_stratification.outcome_bin == d2_bin
        ):
            return disposition
    raise ValueError("cuprate diagnostic result carries an unknown outcome bin")


def observation_to_classify_demo(
    *,
    result: CuprateDiagnosticResult,
    plan: AnalysisPlan,
    supported_if: Mapping[str, Any],
    n_confirm: int,
) -> dict[str, Any]:
    """Build the ``classify_outcome`` demonstration dict from one admitted result.

    ``supported_if`` is the preregistered decision rule content; its canonical
    sha must equal the plan's ``positive_decision_rule_sha256``, so the grading
    bar is the plan's own pin, not an adapter invention.  Field mapping:
    ``test_statistic`` is the matched-control excess, ``test_triggers`` is the
    doping-stratification bin, ``control_silent`` is the matched control's
    survival, and ``detail`` is the mechanical combined bin id.
    """

    rule = dict(supported_if)
    if canonical_sha256(rule) != plan.positive_decision_rule_sha256:
        raise ValueError("supported-if rule is not the plan's pinned positive decision rule")
    d1 = result.d1_matched_control
    d2 = result.d2_doping_stratification
    return {
        "holds": d1.outcome_bin == _D1_EXCLUDES and d2.outcome_bin == _D2_CONCENTRATES,
        "detail": combined_outcome_bin_id(result),
        "audit_refuted": False,
        "preregistration": {"supported_if": rule},
        "test_statistic": d1.excess_over_matched,
        "test_triggers": d2.outcome_bin == _D2_CONCENTRATES,
        "control_silent": d1.survives,
        "probes": {"clean": True},
        "n_confirm": n_confirm,
    }


def _credence_mean(alpha: float, beta: float) -> float:
    return alpha / (alpha + beta)


def kernel_events_to_score_k2_tables(
    *,
    events: tuple[ResearchEvent, ...],
    grade_records: tuple[RoundGradeRecordV1, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Adapt the kernel event stream plus grade records to ``score_k2``'s tables.

    Every kernel event is routed through ``KERNEL_EVENT_SCORE_TABLE``; the belief
    rows (prior, prediction, update), the per-round reasons, and the final
    synthesis come from the grade records, folded exactly as the scheduler's
    credence mutator does: one beta-binomial count per boolean verdict, nothing
    on an inconclusive round.
    """

    table = dict(KERNEL_EVENT_SCORE_TABLE)
    unmapped = sorted(
        {event.event_type for event in events if event.event_type not in table},
        key=lambda item: item.value,
    )
    if unmapped:
        raise ValueError(f"kernel events carry types outside the adapter table: {unmapped}")

    records_by_action: dict[str, RoundGradeRecordV1] = {}
    for record in grade_records:
        if record.action_id in records_by_action:
            raise ValueError("grade records must bind each action once")
        records_by_action[record.action_id] = record
    admitted_ids = [
        event.payload.action_id
        for event in events
        if event.event_type is EventType.OBSERVATION_INCORPORATED
        and isinstance(event.payload, ObservationIncorporatedPayload)
    ]
    ungraded = sorted(set(admitted_ids) - set(records_by_action))
    if ungraded:
        raise ValueError(f"admitted observations without grade records: {ungraded}")
    extra = sorted(set(records_by_action) - set(admitted_ids))
    if extra:
        raise ValueError(f"grade records without admitted observations: {extra}")

    rows: list[dict[str, Any]] = []
    credence: dict[str, tuple[float, float, int]] = {}
    surprises: list[float] = []
    admissions = 0
    for event in events:
        scored = table[event.event_type]
        if event.event_type is EventType.OBSERVATION_INCORPORATED:
            payload = event.payload
            assert isinstance(payload, ObservationIncorporatedPayload)
            admissions += 1
            record = records_by_action[payload.action_id]
            question_key = record.question_key
            if question_key not in credence:
                alpha, beta = record.prior_alpha, record.prior_beta
                credence[question_key] = (alpha, beta, 0)
                rows.append(
                    {
                        "type": "belief_prior",
                        "payload": {
                            "question_key": question_key,
                            "alpha": alpha,
                            "beta": beta,
                            "mean": _credence_mean(alpha, beta),
                            "weak_prior": alpha + beta < WEAK_PRIOR_MAX_MASS,
                        },
                    }
                )
            rows.append(
                {
                    "type": "belief_prediction",
                    "payload": {
                        "question_key": question_key,
                        "predicted_p_holds": record.predicted_p_holds,
                    },
                }
            )
            rows.append(
                {
                    "type": "experiment",
                    "payload": {"exp_id": payload.action_id, "round": record.round_index},
                }
            )
            realized = record.realized
            if realized is not None:
                rows.append(
                    {
                        "type": "demonstration",
                        "payload": {
                            **record.demonstration,
                            "exp_id": payload.action_id,
                            "round": record.round_index,
                            "computed": record.computed,
                            "exploration_applied": record.exploration_applied,
                            "holds": realized == 1.0,
                        },
                    }
                )
                alpha, beta, updates = credence[question_key]
                alpha, beta = (
                    alpha + (1.0 if realized == 1.0 else 0.0),
                    beta + (0.0 if realized == 1.0 else 1.0),
                )
                updates += 1
                credence[question_key] = (alpha, beta, updates)
                surprise = abs(record.predicted_p_holds - realized)
                surprises.append(surprise)
                rows.append(
                    {
                        "type": "belief_update",
                        "payload": {
                            "round": record.round_index,
                            "exp_id": payload.action_id,
                            "question_key": question_key,
                            "alpha": alpha,
                            "beta": beta,
                            "mean": _credence_mean(alpha, beta),
                            "n_updates": updates,
                            "weak_prior": alpha + beta < WEAK_PRIOR_MAX_MASS,
                            "predicted_p_holds": record.predicted_p_holds,
                            "realized": realized,
                            "surprise": surprise,
                        },
                    }
                )
            outcome = classify_outcome(record.demonstration if realized is not None else None)
            rows.append(
                {
                    "type": "campaign_reason",
                    "payload": {
                        "round": record.round_index,
                        "exp_id": payload.action_id,
                        "reason": outcome["reason"],
                        "recoverable": outcome["recoverable"],
                        "detail": outcome["detail"],
                    },
                }
            )
        elif scored == "campaign_plan":
            rows.append(
                {
                    "type": "campaign_plan",
                    "payload": {
                        "round": admissions,
                        "continue": event.event_type is not EventType.PAUSE_COMMITTED,
                        "rationale": f"kernel {event.event_type.value} committed",
                        "candidates": [],
                    },
                }
            )
        elif scored == "campaign_finished":
            rows.append(
                {
                    "type": "campaign_finished",
                    "payload": {
                        "experiments": admissions,
                        "calibration": (sum(surprises) / len(surprises) if surprises else None),
                        "n_belief_updates": len(surprises),
                    },
                }
            )
    credences = [
        {
            "question_key": question_key,
            "alpha": alpha,
            "beta": beta,
            "n_updates": updates,
        }
        for question_key, (alpha, beta, updates) in sorted(credence.items())
    ]
    return rows, credences


__all__ = [
    "CampaignBundleEntryV1",
    "CampaignBundleManifestV1",
    "CampaignReplayReportV1",
    "DATASET_CARD_KIND",
    "DATASET_CONTENT_KIND",
    "FOUR_WAY_DISPOSITIONS",
    "KERNEL_EVENT_SCORE_TABLE",
    "KERNEL_STREAM_KIND",
    "ROUND_SPLIT_POLICY_KIND_PREFIX",
    "RoundGradeRecordV1",
    "RoundSplitLedgerV1",
    "SCORED_EVENT_VOCABULARY",
    "SELECTION_RECEIPT_KIND_PREFIX",
    "STAGED_ROUND_ROWS_KIND_PREFIX",
    "StagedRowV1",
    "StagedRoundRowsV1",
    "build_round_split_state",
    "campaign_spent_groups",
    "four_way_disposition",
    "kernel_events_to_score_k2_tables",
    "kernel_stream_bytes",
    "kernel_stream_sha256",
    "observation_to_classify_demo",
    "sealed_groups_for_round",
    "verify_campaign_bundle",
]
