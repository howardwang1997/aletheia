"""Author the ARL-2 preregistered discriminator predictions (roadmap S3).

One :class:`PredictionVersionV2` per active hypothesis per discriminator, over
the identical ``(observable_spec, measurement_protocol, outcome_space)`` triple
with differing committed outcome bins and mutual ``discriminates_from`` —
exactly the shape :meth:`WorldModelSnapshotV2.assert_hypothesis_discrimination`
then verifies mechanically.  The measurement-context shas arrive as inputs: they
are authored with the frozen round-1 template, and this module refuses to invent
them.  Pure functions, caller-supplied timestamps, no I/O.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import Field

from aletheia.protocols.world_models import (
    HypothesisLifecycle,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.continuation import exact_outcome_bin_prediction_sha256

_DISCRIMINATOR_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"


class HypothesisOutcomeBin(ControllerModel):
    """One hypothesis's committed bin in one discriminator's outcome space."""

    hypothesis_ref: str = Field(min_length=1, max_length=128)
    outcome_bin_id: str = Field(pattern=r"^[a-z][a-z0-9_.:/-]{1,127}$")


class MeasurementBinding(ControllerModel):
    """One discriminator's measurement context and per-hypothesis bin map."""

    discriminator_id: str = Field(pattern=_DISCRIMINATOR_ID_PATTERN)
    observable_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measurement_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_space_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bins: tuple[HypothesisOutcomeBin, ...] = Field(min_length=2)
    semantic_delta: str = Field(min_length=1, max_length=4_000)


def _content_id(prefix: str, *parts: str) -> str:
    return prefix + hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]


def build_predictions(
    *,
    bindings: tuple[MeasurementBinding, ...],
    world_model: WorldModelSnapshotV2,
    authored_at: datetime,
    principal_id: str,
) -> tuple[PredictionVersionV2, ...]:
    """Build the closed prediction set over the snapshot's active hypotheses.

    Fails closed when a binding does not cover every active hypothesis exactly
    once, when two hypotheses share a bin (no discrimination), or when a binding
    targets a rejected or unknown hypothesis.
    """

    scope_sha256 = world_model.graph_scope.graph_scope_sha256
    active = {
        item.hypothesis_id: item.hypothesis_sha256
        for item in world_model.hypotheses
        if item.lifecycle is HypothesisLifecycle.ACTIVE
    }
    active_ids = set(active)
    # Resolve manifest refs (h1_plane_doping, ...) to active hypothesis hashes by
    # rebuilding the content-derived ids exactly as campaign/candidates.py does.
    resolved: dict[str, str] = {}
    for ref in {bin_.hypothesis_ref for binding in bindings for bin_ in binding.bins}:
        candidate = _content_id("hyp_", "arl2", ref)
        if candidate not in active:
            raise ValueError(
                f"discriminator binding targets a non-active hypothesis: {ref}"
            )
        resolved[ref] = active[candidate]

    predictions: list[PredictionVersionV2] = []
    for binding in bindings:
        refs = [bin_.hypothesis_ref for bin_ in binding.bins]
        if len(set(refs)) != len(refs) or {
            _content_id("hyp_", "arl2", ref) for ref in refs
        } != active_ids:
            raise ValueError(
                "discriminator binding must cover every active hypothesis exactly once"
            )
        bins = {bin_.hypothesis_ref: bin_.outcome_bin_id for bin_ in binding.bins}
        if len(set(bins.values())) != len(bins):
            raise ValueError(
                f"discriminator {binding.discriminator_id} assigns one bin to two hypotheses"
            )
        for ref, outcome_bin_id in bins.items():
            hypothesis_sha256 = resolved[ref]
            predictions.append(
                PredictionVersionV2(
                    prediction_id=_content_id(
                        "pred_", "arl2", binding.discriminator_id, ref
                    ),
                    version=1,
                    graph_scope_sha256=scope_sha256,
                    hypothesis_sha256=hypothesis_sha256,
                    observable_spec_sha256=binding.observable_spec_sha256,
                    measurement_protocol_sha256=binding.measurement_protocol_sha256,
                    outcome_space_sha256=binding.outcome_space_sha256,
                    predicted_outcome_sha256=exact_outcome_bin_prediction_sha256(
                        observable_spec_sha256=binding.observable_spec_sha256,
                        measurement_protocol_sha256=binding.measurement_protocol_sha256,
                        outcome_space_sha256=binding.outcome_space_sha256,
                        outcome_bin_id=outcome_bin_id,
                    ),
                    discriminates_from_hypothesis_sha256s=tuple(
                        sorted(
                            other_sha
                            for other_ref, other_sha in resolved.items()
                            if other_ref != ref
                        )
                    ),
                    semantic_delta=binding.semantic_delta,
                    authored_by_principal_id=principal_id,
                    authored_at=authored_at,
                )
            )
    ordered = tuple(
        sorted(
            predictions,
            key=lambda item: (item.prediction_id, item.version, item.prediction_sha256),
        )
    )
    if not ordered:
        raise ValueError("preregistration requires at least one discriminator binding")
    return ordered


def attach_predictions(
    world_model: WorldModelSnapshotV2,
    predictions: tuple[PredictionVersionV2, ...],
) -> WorldModelSnapshotV2:
    """Return the snapshot with its preregistered prediction set attached.

    The round-1 registered model is complete only with its predictions; both
    halves are authored before quest launch, so this stays one registration act
    (same version, same identities) and re-runs every snapshot validator.
    """

    payload = json.loads(world_model.model_dump_json())
    payload["predictions"] = [
        json.loads(item.model_dump_json())
        for item in sorted(
            predictions,
            key=lambda item: (item.prediction_id, item.version, item.prediction_sha256),
        )
    ]
    return WorldModelSnapshotV2.model_validate(payload)
