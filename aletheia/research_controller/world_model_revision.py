"""Legal world-model revision for ARL-2 (roadmap S5, revision author).

One admitted-observation receipt becomes the next :class:`WorldModelSnapshotV2`
here.  The belief update is the ported ``memory/belief.py`` Bayesian mutator
under ``BeliefUpdateBasis.VALIDATED_OBSERVATION`` binding
``source_observation_receipt_sha256``; hypothesis retirements bump the
lifecycle with a ``revision_parent_sha256`` chain; predictions, assumptions,
and limitations are sealed, carried with only their graph-scope stamp
re-bound to the current graph view.
``verify_authored_revision_v2`` re-derives revision legality from the parent
snapshot alone, so an offline-authored revision is verified, never trusted:
identity, seals, lifecycle transitions, and the belief basis's shape are all
checked against the parent.  The receipt's provenance (that the cited
observation was admitted, and against this parent) and the posterior values
are enforced where the admission registry is reachable, at the
campaign-loop wiring seam, not by these pure functions.
Pure functions, no I/O; every identity is content-derived and every timestamp
comes from the caller.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime

from pydantic import Field

from aletheia.memory.belief import Credence, mean, update
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.world_models import (
    AssumptionVersionV2,
    BeliefStateVersionV2,
    BeliefUpdateBasis,
    HypothesisBeliefV2,
    HypothesisLifecycle,
    HypothesisVersionV2,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_controller.contracts import ControllerModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

REVISION_UPDATE_RULE_TEXT = (
    "A revision folds exactly one admitted-observation receipt into the parent "
    "snapshot: every active hypothesis's Beta credence moves by one count "
    "through the ported memory/belief.py mutator under "
    "BeliefUpdateBasis.VALIDATED_OBSERVATION, the posteriors are renormalized "
    "to sum to one with the legacy campaign convention, hypothesis retirements "
    "bump lifecycle with a revision-parent chain, and predictions, assumptions, "
    "and limitations are sealed: only their graph-scope stamp may be re-bound "
    "to the current graph view."
)
REVISION_UPDATE_RULE_SHA256 = hashlib.sha256(REVISION_UPDATE_RULE_TEXT.encode("utf-8")).hexdigest()


class AdmittedObservationBinding(ControllerModel):
    """One hypothesis's verdict inside a single admitted-observation receipt."""

    hypothesis_sha256: str = Field(pattern=_SHA256_PATTERN)
    holds: bool
    observation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)


def _normalize_probabilities(values: list[float]) -> list[float]:
    """Renormalize to sum to one, porting the legacy campaign convention
    (``epistemics/belief_update.py::_normalize``): divide by the exact sum,
    then fold the floating-point residue into the last entry so the total is
    exactly 1.0."""
    total = math.fsum(values)
    normalized = [value / total for value in values]
    normalized[-1] += 1.0 - math.fsum(normalized)
    return normalized


_SCOPE_FREE_FIELDS = frozenset({"graph_scope_sha256"})
_TRANSITION_FIELDS = frozenset(
    {
        "version",
        "revision_parent_sha256",
        "lifecycle",
        "semantic_delta",
        "authored_by_principal_id",
        "authored_at",
    }
)


def _scope_free(entry) -> dict:
    """A member's content with the graph-scope stamp removed.

    The graph view legally moves between rounds (every authorized action
    commits a new snapshot), so carried members are compared modulo their
    scope stamp; every other field is sealed.
    """
    return {
        key: value
        for key, value in entry.model_dump(mode="python").items()
        if key not in _SCOPE_FREE_FIELDS
    }


def _restamped(entry, scope_sha256: str):
    payload = entry.model_dump(mode="python")
    if payload.get("graph_scope_sha256") == scope_sha256:
        return entry
    return type(entry).model_validate({**payload, "graph_scope_sha256": scope_sha256})


def _repoint_prediction(prediction, scope_sha256: str, remap: dict[str, str]):
    payload = {
        **prediction.model_dump(mode="python"),
        "graph_scope_sha256": scope_sha256,
        "hypothesis_sha256": remap.get(prediction.hypothesis_sha256, prediction.hypothesis_sha256),
        "discriminates_from_hypothesis_sha256s": tuple(
            remap.get(hypothesis_sha256, hypothesis_sha256)
            for hypothesis_sha256 in prediction.discriminates_from_hypothesis_sha256s
        ),
    }
    if payload == prediction.model_dump(mode="python"):
        return prediction
    return PredictionVersionV2.model_validate(payload)


def _repoint_assumption(assumption, scope_sha256: str, remap: dict[str, str]):
    payload = {
        **assumption.model_dump(mode="python"),
        "graph_scope_sha256": scope_sha256,
        "applies_to_hypothesis_sha256s": tuple(
            remap.get(hypothesis_sha256, hypothesis_sha256)
            for hypothesis_sha256 in assumption.applies_to_hypothesis_sha256s
        ),
    }
    if payload == assumption.model_dump(mode="python"):
        return assumption
    return AssumptionVersionV2.model_validate(payload)


def revise_world_model_v2(
    *,
    parent: WorldModelSnapshotV2,
    observations: tuple[AdmittedObservationBinding, ...],
    continuation_receipt_sha256: str,
    retire_hypothesis_ids: tuple[str, ...] = (),
    graph_scope: ProtocolScope | None = None,
    authored_at: datetime,
    principal_id: str,
) -> WorldModelSnapshotV2:
    """Author the next snapshot from one admitted-observation receipt.

    Fails closed unless the bindings share one observation receipt, cover
    every parent-active hypothesis exactly once, and every retirement targets a
    parent-active, unreferenced lineage.  The posterior of an updated
    hypothesis is the mean of its mass-2 Beta credence after one count;
    retired hypotheses carry their prior onto their new version hash.  A
    caller-supplied ``graph_scope`` re-stamps the child and every carried
    member to the current graph view (the view moves with every authorized
    action); the default inherits the parent's scope.  Deterministic:
    identical inputs give the byte-identical snapshot.
    """
    if not observations:
        raise ValueError("revision requires at least one admitted observation")
    if re.fullmatch(_SHA256_PATTERN, continuation_receipt_sha256) is None:
        raise ValueError("continuation receipt is not a canonical sha256")
    receipts = {item.observation_receipt_sha256 for item in observations}
    if len(receipts) != 1:
        raise ValueError("admitted observations do not share one observation receipt")
    receipt_sha256 = next(iter(receipts))

    if parent.belief_state is None:
        raise ValueError("parent snapshot carries no belief state to revise")
    priors = {
        belief.hypothesis_sha256: belief.probability
        for belief in parent.belief_state.hypothesis_beliefs
    }
    active = {
        item.hypothesis_id: item
        for item in parent.hypotheses
        if item.lifecycle is HypothesisLifecycle.ACTIVE
    }
    bound_hashes = sorted(item.hypothesis_sha256 for item in observations)
    if bound_hashes != sorted(item.hypothesis_sha256 for item in active.values()):
        raise ValueError("admitted observations must cover every active hypothesis exactly once")
    retire = tuple(sorted(set(retire_hypothesis_ids)))
    if any(hypothesis_id not in active for hypothesis_id in retire):
        raise ValueError("retirement targets a hypothesis that is not active")
    # predictions and assumptions are sealed, and the snapshot is closed over
    # hypothesis hashes: retiring a referenced hypothesis would dangle the
    # reference, so such a retirement cannot be authored here at all
    referenced: set[str] = {
        hypothesis_sha256
        for prediction in parent.predictions
        for hypothesis_sha256 in (
            prediction.hypothesis_sha256,
            *prediction.discriminates_from_hypothesis_sha256s,
        )
    }
    referenced.update(
        hypothesis_sha256
        for assumption in parent.assumptions
        for hypothesis_sha256 in assumption.applies_to_hypothesis_sha256s
    )
    if any(active[hypothesis_id].hypothesis_sha256 in referenced for hypothesis_id in retire):
        raise ValueError("retirement breaks a sealed prediction or assumption binding")
    if authored_at < parent.authored_at:
        raise ValueError("revision cannot predate its parent snapshot")

    holds_by_hash = {item.hypothesis_sha256: item.holds for item in observations}
    scope = graph_scope if graph_scope is not None else parent.graph_scope
    scope_sha = scope.graph_scope_sha256
    child_entries: list[HypothesisVersionV2] = []
    probabilities_by_hash: dict[str, float] = {}
    # re-stamping changes every hypothesis hash, so sealed references must be
    # re-pointed from the parent hashes to the child hashes
    remap: dict[str, str] = {}
    for entry in parent.hypotheses:
        if entry.hypothesis_id in retire:
            retired = HypothesisVersionV2.model_validate(
                {
                    **entry.model_dump(mode="python"),
                    "version": entry.version + 1,
                    "revision_parent_sha256": entry.hypothesis_sha256,
                    "lifecycle": HypothesisLifecycle.RETIRED,
                    "graph_scope_sha256": scope_sha,
                    "semantic_delta": (f"retirement after admitted observation {receipt_sha256}"),
                    "authored_by_principal_id": principal_id,
                    "authored_at": authored_at,
                }
            )
            child_entries.append(retired)
            remap[entry.hypothesis_sha256] = retired.hypothesis_sha256
            probabilities_by_hash[retired.hypothesis_sha256] = priors[entry.hypothesis_sha256]
            continue
        carried = _restamped(entry, scope_sha)
        child_entries.append(carried)
        remap[entry.hypothesis_sha256] = carried.hypothesis_sha256
        if entry.lifecycle is HypothesisLifecycle.ACTIVE:
            prior = priors[entry.hypothesis_sha256]
            posterior = mean(
                update(
                    Credence(2.0 * prior, 2.0 * (1.0 - prior)),
                    holds=holds_by_hash[entry.hypothesis_sha256],
                    confirm_split=True,
                )
            )
            probabilities_by_hash[carried.hypothesis_sha256] = posterior
        else:
            probabilities_by_hash[carried.hypothesis_sha256] = priors[entry.hypothesis_sha256]

    ordered_hashes = sorted(probabilities_by_hash)
    normalized = _normalize_probabilities(
        [probabilities_by_hash[hypothesis_sha256] for hypothesis_sha256 in ordered_hashes]
    )
    belief_state = BeliefStateVersionV2(
        belief_id=parent.belief_state.belief_id,
        version=parent.belief_state.version + 1,
        revision_parent_sha256=parent.belief_state.belief_state_sha256,
        graph_scope_sha256=scope_sha,
        hypothesis_beliefs=tuple(
            HypothesisBeliefV2(hypothesis_sha256=hypothesis_sha256, probability=probability)
            for hypothesis_sha256, probability in zip(ordered_hashes, normalized)
        ),
        update_basis=BeliefUpdateBasis.VALIDATED_OBSERVATION,
        source_observation_receipt_sha256=receipt_sha256,
        update_rule_sha256=REVISION_UPDATE_RULE_SHA256,
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )
    retired_note = f"; retired {' and '.join(retire)}" if retire else ""
    return WorldModelSnapshotV2(
        graph_scope=scope,
        world_model_id=parent.world_model_id,
        version=parent.version + 1,
        revision_parent_sha256=parent.world_model_sha256,
        hypotheses=tuple(
            sorted(
                child_entries,
                key=lambda item: (item.hypothesis_id, item.version, item.hypothesis_sha256),
            )
        ),
        assumptions=tuple(
            _repoint_assumption(item, scope_sha, remap) for item in parent.assumptions
        ),
        predictions=tuple(
            _repoint_prediction(item, scope_sha, remap) for item in parent.predictions
        ),
        belief_state=belief_state,
        model_limitations=parent.model_limitations,
        semantic_delta=(
            f"admitted-observation revision binding observation receipt "
            f"{receipt_sha256} and continuation receipt "
            f"{continuation_receipt_sha256}{retired_note}"
        ),
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )


def verify_authored_revision_v2(
    *,
    parent: WorldModelSnapshotV2,
    child: WorldModelSnapshotV2,
) -> WorldModelSnapshotV2:
    """Verify revision legality from the parent snapshot alone; return child.

    The illegal classes fail with distinct messages: a revision bound to the
    wrong parent snapshot, a belief update without its observation basis and
    pinned rule, a mutated sealed prediction, assumption, or model limitation,
    and a retirement that dangles a sealed member's reference.  Carried
    members are sealed modulo their graph-scope stamp (the graph view legally
    moves between rounds); every other field must be unchanged.  Lineage
    drops, additions, and illegal lifecycle bumps fail the same way: the child
    must be exactly the parent plus one legal revision.  Structural legality
    only: the observation receipt's provenance and the posterior values bind
    at the observation-admission seam, not here.
    """
    if (
        child.world_model_id != parent.world_model_id
        or child.version != parent.version + 1
        or child.revision_parent_sha256 != parent.world_model_sha256
    ):
        raise ValueError("world-model revision does not resolve its exact parent snapshot")
    if child.authored_at < parent.authored_at:
        raise ValueError("world-model revision predates its parent snapshot")
    parent_current = {item.hypothesis_id: item for item in parent.hypotheses}
    child_current = {item.hypothesis_id: item for item in child.hypotheses}
    if set(parent_current) != set(child_current):
        raise ValueError("world-model revision changed the hypothesis lineages")
    # re-stamping under a moved graph view changes every hypothesis hash; seal
    # comparisons translate the child's references back to parent hashes
    reverse = {
        child_current[hypothesis_id].hypothesis_sha256: parent_current[
            hypothesis_id
        ].hypothesis_sha256
        for hypothesis_id in parent_current
    }

    def prediction_view(entry) -> dict:
        payload = _scope_free(entry)
        payload["hypothesis_sha256"] = reverse.get(
            payload["hypothesis_sha256"], payload["hypothesis_sha256"]
        )
        payload["discriminates_from_hypothesis_sha256s"] = tuple(
            reverse.get(hypothesis_sha256, hypothesis_sha256)
            for hypothesis_sha256 in payload["discriminates_from_hypothesis_sha256s"]
        )
        return payload

    def assumption_view(entry) -> dict:
        payload = _scope_free(entry)
        payload["applies_to_hypothesis_sha256s"] = tuple(
            reverse.get(hypothesis_sha256, hypothesis_sha256)
            for hypothesis_sha256 in payload["applies_to_hypothesis_sha256s"]
        )
        return payload

    if [prediction_view(item) for item in child.predictions] != [
        _scope_free(item) for item in parent.predictions
    ]:
        raise ValueError("world-model revision mutated a sealed prediction")
    if [assumption_view(item) for item in child.assumptions] != [
        _scope_free(item) for item in parent.assumptions
    ]:
        raise ValueError("world-model revision mutated a sealed assumption")
    if child.model_limitations != parent.model_limitations:
        raise ValueError("world-model revision mutated a sealed model limitation")
    parent_belief = parent.belief_state
    child_belief = child.belief_state
    if (
        parent_belief is None
        or child_belief is None
        or child_belief.belief_id != parent_belief.belief_id
        or child_belief.version != parent_belief.version + 1
        or child_belief.revision_parent_sha256 != parent_belief.belief_state_sha256
        or child_belief.update_basis is not BeliefUpdateBasis.VALIDATED_OBSERVATION
        or child_belief.source_observation_receipt_sha256 is None
        or child_belief.update_rule_sha256 != REVISION_UPDATE_RULE_SHA256
    ):
        raise ValueError("world-model revision carries an unbound belief basis")
    # a retirement may not dangle a sealed member's reference: the constraint
    # the author enforces is re-derived here over the parent's own hashes, so
    # a hand-authored child cannot retire a referenced hypothesis even when
    # its sealed members are re-pointed to the retired entry's new hash
    referenced = {
        hypothesis_sha256
        for prediction in parent.predictions
        for hypothesis_sha256 in (
            prediction.hypothesis_sha256,
            *prediction.discriminates_from_hypothesis_sha256s,
        )
    }
    referenced.update(
        hypothesis_sha256
        for assumption in parent.assumptions
        for hypothesis_sha256 in assumption.applies_to_hypothesis_sha256s
    )
    for hypothesis_id, parent_entry in parent_current.items():
        child_entry = child_current[hypothesis_id]
        if child_entry.version == parent_entry.version:
            if _scope_free(child_entry) != _scope_free(parent_entry):
                raise ValueError("world-model revision mutated a carried hypothesis version")
            continue
        if (
            child_entry.version != parent_entry.version + 1
            or child_entry.revision_parent_sha256 != parent_entry.hypothesis_sha256
            or parent_entry.lifecycle is not HypothesisLifecycle.ACTIVE
            or child_entry.lifecycle is not HypothesisLifecycle.RETIRED
        ):
            raise ValueError("world-model revision transitions a hypothesis illegally")
        if parent_entry.hypothesis_sha256 in referenced:
            raise ValueError("world-model revision retires a hypothesis a sealed member references")
        parent_payload = _scope_free(parent_entry)
        child_payload = _scope_free(child_entry)
        if any(
            child_payload[key] != parent_payload[key]
            for key in parent_payload
            if key not in _TRANSITION_FIELDS
        ):
            raise ValueError("world-model revision transitions a hypothesis illegally")
    return child


def assert_revision_belief_basis(world_model: WorldModelSnapshotV2) -> None:
    """Self-contained compile gate: a version>1 snapshot must carry a belief
    state that declares its observation basis and revision chain.  Version 1
    passes (the prior needs no observation receipt)."""
    if world_model.version == 1:
        return
    belief = world_model.belief_state
    if (
        belief is None
        or belief.version < 2
        or belief.revision_parent_sha256 is None
        or belief.update_basis is not BeliefUpdateBasis.VALIDATED_OBSERVATION
        or belief.source_observation_receipt_sha256 is None
    ):
        raise ValueError("world-model revision carries an unbound belief basis")


def verify_round_split_binding(
    *,
    sealed_group_ids: tuple[str, ...],
    spent_group_ids: tuple[str, ...],
    unspent_group_ids: tuple[str, ...],
    bound_group_ids: tuple[str, ...],
) -> None:
    """Re-derive round disjointness from group identities alone.

    The sealed confirmation groups must partition exactly into spent and
    unspent, and a round may only bind groups still unspent.  Every input must
    be unique and canonically ordered; duplicates or unordered input fail
    closed rather than normalizing silently.
    """
    sealed = _canonical_group_ids(sealed_group_ids, "sealed")
    spent = _canonical_group_ids(spent_group_ids, "spent")
    unspent = _canonical_group_ids(unspent_group_ids, "unspent")
    bound = _canonical_group_ids(bound_group_ids, "bound")
    if set(spent) | set(unspent) != set(sealed):
        raise ValueError("round split does not partition its sealed groups")
    if set(spent) & set(unspent):
        raise ValueError("round split assigns one group to two spend states")
    if not bound:
        raise ValueError("round binds no confirmation groups")
    if not set(bound) <= set(unspent):
        raise ValueError("round binds groups outside its unspent confirmation split")


def _canonical_group_ids(group_ids: tuple[str, ...], label: str) -> tuple[str, ...]:
    for group_id in group_ids:
        if not isinstance(group_id, str) or not group_id:
            raise ValueError(f"{label} group ids must be nonempty strings")
    if tuple(group_ids) != tuple(sorted(set(group_ids))):
        raise ValueError(f"{label} group ids must be unique and canonically ordered")
    return tuple(group_ids)
