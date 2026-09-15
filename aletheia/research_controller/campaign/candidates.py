"""Author the ARL-2 round-1 world model from the reviewed candidate manifest.

Keystone B (control-plane wiring), authoring half of roadmap S2.  The candidate
manifest is a PI-reviewed JSON file; this module is the pure function that turns
it into a closed :class:`WorldModelSnapshotV2`: the two active hypotheses, the
three retained rejections as RETIRED entries whose ``rationale_sha256`` binds the
rejection texts verbatim, and the preregistered prior belief state.  No LLM, no
network, no database; every identity is content-derived and every timestamp
comes from the caller.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.world_models import (
    BeliefStateVersionV2,
    BeliefUpdateBasis,
    HypothesisBeliefV2,
    HypothesisLifecycle,
    HypothesisVersionV2,
    WorldModelSnapshotV2,
)

# The preregistered prior: maximum entropy over the two active explanations,
# zero credence for the rejections retained on the record.  The manifest itself
# states that the loop must re-derive and re-test both explanations rather than
# import the 2026-06 verdict, so any tilt would smuggle that verdict back in.
# The snapshot contract requires the belief state to cover every current
# hypothesis version, rejections included; zeros satisfy both sum-to-one and
# retention.  Authored here, not in the manifest, so the numbers are frozen in
# reviewed code rather than an editable artifact.
PREREGISTERED_PRIORS: Mapping[str, float] = MappingProxyType(
    {
        "h1_plane_doping": 0.5,
        "h2_complexity_ad": 0.5,
        "meta_local_structure_dressing": 0.0,
        "within_cell_variance_bayes_floor": 0.0,
        "aleatoric_triple_matching": 0.0,
    }
)

PREREGISTERED_UPDATE_RULE_TEXT = (
    "Belief updates use the ported memory/belief.py Bayesian mutator under "
    "BeliefUpdateBasis.VALIDATED_OBSERVATION only: each admitted observation "
    "moves one Beta credence per hypothesis prediction by one count, and no "
    "other input moves a credence.  Non-evaluated, degraded, or "
    "non-confirm-split rounds never move the belief."
)

UPDATE_RULE_SHA256 = hashlib.sha256(
    PREREGISTERED_UPDATE_RULE_TEXT.encode("utf-8")
).hexdigest()

# Limitations restated from the manifest statements themselves: H1's statement
# fixes the composition-averaged feature space and the single material family.
PREREGISTERED_MODEL_LIMITATIONS = (
    "single material family: the registered dataset covers multi-alkaline-earth cuprates only",
    "the registered feature space is composition-averaged; plane-specific doping is not directly observed",
)

_MANIFEST_VERSION = 1


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]


def _hypothesis(
    entry: dict,
    *,
    lifecycle: HypothesisLifecycle,
    scope_sha256: str,
    principal_id: str,
    authored_at: datetime,
) -> HypothesisVersionV2:
    return HypothesisVersionV2(
        hypothesis_id=_content_id("hyp_", "arl2", entry["id"]),
        version=1,
        graph_scope_sha256=scope_sha256,
        lifecycle=lifecycle,
        statement=entry["statement"],
        explanatory_model=entry["explanatory_model"],
        rationale_sha256=_text_sha256(entry["rationale"]),
        semantic_delta=f"initial {'active' if lifecycle is HypothesisLifecycle.ACTIVE else 'rejected'} registration of {entry['id']} from the reviewed candidate manifest",
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )


def build_world_model(
    manifest: dict,
    *,
    graph_scope: ProtocolScope,
    authored_at: datetime,
    principal_id: str,
) -> WorldModelSnapshotV2:
    """Build the closed round-1 snapshot from the reviewed candidate manifest.

    Fails closed if the manifest's entry identities no longer match the
    preregistered prior keys: that means the reviewed artifact and the frozen
    prior have drifted apart, and neither may silently win.
    """

    if manifest.get("manifest_version") != _MANIFEST_VERSION:
        raise ValueError("candidate manifest version is not supported")
    entries = {entry["id"]: entry for entry in manifest.get("active", ())}
    rejections = {entry["id"]: entry for entry in manifest.get("retained_rejections", ())}
    if set(entries) & set(rejections):
        raise ValueError("candidate manifest assigns one identity to two lifecycles")
    if set(entries) | set(rejections) != set(PREREGISTERED_PRIORS):
        raise ValueError(
            "candidate manifest identities do not match the preregistered prior"
        )

    scope_sha256 = graph_scope.graph_scope_sha256
    hypotheses = tuple(
        sorted(
            (
                *(
                    _hypothesis(
                        entry,
                        lifecycle=HypothesisLifecycle.ACTIVE,
                        scope_sha256=scope_sha256,
                        principal_id=principal_id,
                        authored_at=authored_at,
                    )
                    for entry in entries.values()
                ),
                *(
                    _hypothesis(
                        entry,
                        lifecycle=HypothesisLifecycle.RETIRED,
                        scope_sha256=scope_sha256,
                        principal_id=principal_id,
                        authored_at=authored_at,
                    )
                    for entry in rejections.values()
                ),
            ),
            key=lambda item: (item.hypothesis_id, item.version, item.hypothesis_sha256),
        )
    )
    by_entry_id = {
        entry_id: next(
            hypothesis
            for hypothesis in hypotheses
            if hypothesis.hypothesis_id == _content_id("hyp_", "arl2", entry_id)
        )
        for entry_id in (*entries, *rejections)
    }
    beliefs = tuple(
        sorted(
            (
                HypothesisBeliefV2(
                    hypothesis_sha256=by_entry_id[entry_id].hypothesis_sha256,
                    probability=PREREGISTERED_PRIORS[entry_id],
                )
                for entry_id in by_entry_id
            ),
            key=lambda item: item.hypothesis_sha256,
        )
    )
    belief_state = BeliefStateVersionV2(
        belief_id=_content_id("blf_", "arl2-prior", scope_sha256),
        version=1,
        graph_scope_sha256=scope_sha256,
        hypothesis_beliefs=beliefs,
        update_basis=BeliefUpdateBasis.PRIOR,
        source_observation_receipt_sha256=None,
        update_rule_sha256=UPDATE_RULE_SHA256,
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )
    snapshot = WorldModelSnapshotV2(
        graph_scope=graph_scope,
        world_model_id=_content_id("wm_", "arl2-world-model", scope_sha256),
        version=1,
        hypotheses=hypotheses,
        assumptions=(),
        predictions=(),
        belief_state=belief_state,
        model_limitations=PREREGISTERED_MODEL_LIMITATIONS,
        semantic_delta=(
            "initial candidate registration: two active explanations, three "
            "retained rejections, maximum-entropy prior"
        ),
        authored_by_principal_id=principal_id,
        authored_at=authored_at,
    )
    return snapshot
