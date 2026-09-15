"""ARL-2 campaign authoring: candidate registration and discriminator predictions.

Covers the round-1 registration act: the reviewed candidate manifest becomes a
closed world-model snapshot with the preregistered maximum-entropy prior, then
the discriminator predictions commit exact outcome bins over that snapshot.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aletheia.protocols.world_models import HypothesisLifecycle
from aletheia.research_controller.campaign.candidates import (
    PREREGISTERED_MODEL_LIMITATIONS,
    PREREGISTERED_PRIORS,
    UPDATE_RULE_SHA256,
    build_world_model,
)
from aletheia.research_controller.campaign.predictions import (
    HypothesisOutcomeBin,
    MeasurementBinding,
    attach_predictions,
    build_predictions,
)
from aletheia.research_controller.continuation import exact_outcome_bin_prediction_sha256

_PROTOCOL_FIXTURES = Path(__file__).resolve().parents[1] / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import _scope, digest  # noqa: E402

NOW = datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc)
PRINCIPAL = "principal.arl2.author"


def candidate_manifest() -> dict:
    """A manifest whose entry identities match the preregistered prior."""

    def entry(entry_id: str) -> dict:
        return {
            "id": entry_id,
            "statement": f"registered statement for {entry_id}",
            "explanatory_model": f"registered explanatory model for {entry_id}",
            "rationale": f"reviewed rationale for {entry_id}",
        }

    return {
        "manifest_version": 1,
        "active": tuple(entry(item) for item in ("h1_plane_doping", "h2_complexity_ad")),
        "retained_rejections": tuple(
            entry(item)
            for item in (
                "meta_local_structure_dressing",
                "within_cell_variance_bayes_floor",
                "aleatoric_triple_matching",
            )
        ),
    }


def measurement_binding(
    *,
    discriminator_id: str = "disc.tc_shift",
    h1_bin: str = "bin.positive_shift",
    h2_bin: str = "bin.null_shift",
) -> MeasurementBinding:
    return MeasurementBinding(
        discriminator_id=discriminator_id,
        observable_spec_sha256=digest("observable:tc-shift"),
        measurement_protocol_sha256=digest("measurement:tc-shift"),
        outcome_space_sha256=digest("outcome-space:tc-shift"),
        bins=(
            HypothesisOutcomeBin(hypothesis_ref="h1_plane_doping", outcome_bin_id=h1_bin),
            HypothesisOutcomeBin(hypothesis_ref="h2_complexity_ad", outcome_bin_id=h2_bin),
        ),
        semantic_delta=(
            "plane doping predicts a positive shift; the complexity ordering predicts none"
        ),
    )


def registered_snapshot():
    """The attached round-1 snapshot plus the artifacts used to build it."""

    world_model = build_world_model(
        candidate_manifest(),
        graph_scope=_scope("7"),
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )
    binding = measurement_binding()
    predictions = build_predictions(
        bindings=(binding,),
        world_model=world_model,
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )
    return attach_predictions(world_model, predictions), binding, predictions


def test_registration_covers_the_preregistered_prior() -> None:
    world_model = build_world_model(
        candidate_manifest(),
        graph_scope=_scope("7"),
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )

    assert len(world_model.hypotheses) == len(PREREGISTERED_PRIORS)
    active = {
        item.hypothesis_id: item
        for item in world_model.hypotheses
        if item.lifecycle is HypothesisLifecycle.ACTIVE
    }
    retired = {
        item.hypothesis_id: item
        for item in world_model.hypotheses
        if item.lifecycle is HypothesisLifecycle.RETIRED
    }
    assert len(active) == 2
    assert len(retired) == 3

    beliefs = world_model.belief_state.hypothesis_beliefs
    assert {item.probability for item in beliefs} == {0.0, 0.5}
    assert sum(item.probability for item in beliefs if item.probability == 0.5) == 1.0

    def _entry_id(hypothesis) -> str:
        return next(
            entry_id
            for entry_id in PREREGISTERED_PRIORS
            if hypothesis.hypothesis_id == "hyp_" + digest(f"arl2:{entry_id}")[:32]
        )

    prior_by_sha = {
        item.hypothesis_sha256: PREREGISTERED_PRIORS[_entry_id(item)]
        for item in world_model.hypotheses
    }
    assert {item.hypothesis_sha256 for item in beliefs} == set(prior_by_sha)
    for belief in beliefs:
        assert belief.probability == prior_by_sha[belief.hypothesis_sha256]

    assert world_model.model_limitations == PREREGISTERED_MODEL_LIMITATIONS
    assert world_model.belief_state.update_rule_sha256 == UPDATE_RULE_SHA256
    assert all(item.version == 1 for item in world_model.hypotheses)


def test_registration_is_deterministic_and_timestamp_bound() -> None:
    first = build_world_model(
        candidate_manifest(), graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL
    )
    second = build_world_model(
        candidate_manifest(), graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL
    )
    later = build_world_model(
        candidate_manifest(),
        graph_scope=_scope("7"),
        authored_at=NOW + timedelta(hours=1),
        principal_id=PRINCIPAL,
    )

    assert first.world_model_sha256 == second.world_model_sha256
    assert later.world_model_sha256 != first.world_model_sha256


def test_registration_rejects_manifest_drift() -> None:
    wrong_version = {**candidate_manifest(), "manifest_version": 2}
    with pytest.raises(ValueError, match="candidate manifest version is not supported"):
        build_world_model(
            wrong_version, graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL
        )

    dropped = candidate_manifest()
    dropped["retained_rejections"] = dropped["retained_rejections"][:2]
    with pytest.raises(
        ValueError, match="candidate manifest identities do not match the preregistered prior"
    ):
        build_world_model(
            dropped, graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL
        )

    extra = candidate_manifest()
    unknown = {"id": "h3_unknown", "statement": "s", "explanatory_model": "e", "rationale": "r"}
    extra["active"] = (*extra["active"], unknown)
    with pytest.raises(
        ValueError, match="candidate manifest identities do not match the preregistered prior"
    ):
        build_world_model(extra, graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL)

    overlap = candidate_manifest()
    overlap["active"] = (*overlap["active"], overlap["retained_rejections"][0])
    with pytest.raises(ValueError, match="assigns one identity to two lifecycles"):
        build_world_model(overlap, graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL)


def test_predictions_commit_exact_bins_and_discriminate() -> None:
    snapshot, binding, predictions = registered_snapshot()

    assert len(predictions) == 2
    active_hashes = tuple(
        sorted(
            item.hypothesis_sha256
            for item in snapshot.hypotheses
            if item.lifecycle is HypothesisLifecycle.ACTIVE
        )
    )
    assert {item.hypothesis_sha256 for item in predictions} == set(active_hashes)
    id_to_sha = {item.hypothesis_id: item.hypothesis_sha256 for item in snapshot.hypotheses}
    expected_bins = {
        id_to_sha["hyp_" + digest(f"arl2:{bin_.hypothesis_ref}")[:32]]: bin_.outcome_bin_id
        for bin_ in binding.bins
    }
    for prediction in predictions:
        assert prediction.predicted_outcome_sha256 == exact_outcome_bin_prediction_sha256(
            observable_spec_sha256=binding.observable_spec_sha256,
            measurement_protocol_sha256=binding.measurement_protocol_sha256,
            outcome_space_sha256=binding.outcome_space_sha256,
            outcome_bin_id=expected_bins[prediction.hypothesis_sha256],
        )
        assert prediction.discriminates_from_hypothesis_sha256s == tuple(
            sorted(hash_ for hash_ in active_hashes if hash_ != prediction.hypothesis_sha256)
        )

    snapshot.assert_hypothesis_discrimination(active_hashes)


def test_predictions_are_deterministic() -> None:
    snapshot, binding, first = registered_snapshot()
    second = build_predictions(
        bindings=(binding,),
        world_model=snapshot,
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )
    assert [item.prediction_sha256 for item in first] == [
        item.prediction_sha256 for item in second
    ]


def test_predictions_fail_closed() -> None:
    snapshot = build_world_model(
        candidate_manifest(), graph_scope=_scope("7"), authored_at=NOW, principal_id=PRINCIPAL
    )

    rejected = measurement_binding(
        h1_bin="bin.positive_shift",
        h2_bin="bin.positive_shift",
    )
    with pytest.raises(ValueError, match="assigns one bin to two hypotheses"):
        build_predictions(
            bindings=(rejected,), world_model=snapshot, authored_at=NOW, principal_id=PRINCIPAL
        )

    partial = MeasurementBinding(
        discriminator_id="disc.partial",
        observable_spec_sha256=digest("observable:partial"),
        measurement_protocol_sha256=digest("measurement:partial"),
        outcome_space_sha256=digest("outcome-space:partial"),
        bins=(
            HypothesisOutcomeBin(hypothesis_ref="h1_plane_doping", outcome_bin_id="bin.a"),
            HypothesisOutcomeBin(hypothesis_ref="meta_local_structure_dressing", outcome_bin_id="bin.b"),
        ),
        semantic_delta="one active target and one retained rejection",
    )
    with pytest.raises(ValueError, match="targets a non-active hypothesis"):
        build_predictions(
            bindings=(partial,), world_model=snapshot, authored_at=NOW, principal_id=PRINCIPAL
        )

    single = MeasurementBinding(
        discriminator_id="disc.single",
        observable_spec_sha256=digest("observable:single"),
        measurement_protocol_sha256=digest("measurement:single"),
        outcome_space_sha256=digest("outcome-space:single"),
        bins=(
            HypothesisOutcomeBin(hypothesis_ref="h1_plane_doping", outcome_bin_id="bin.a"),
            HypothesisOutcomeBin(hypothesis_ref="h1_plane_doping", outcome_bin_id="bin.b"),
        ),
        semantic_delta="binds one active hypothesis twice instead of covering both",
    )
    with pytest.raises(ValueError, match="must cover every active hypothesis exactly once"):
        build_predictions(
            bindings=(single,), world_model=snapshot, authored_at=NOW, principal_id=PRINCIPAL
        )

    with pytest.raises(ValueError, match="at least one discriminator binding"):
        build_predictions(bindings=(), world_model=snapshot, authored_at=NOW, principal_id=PRINCIPAL)


def test_attached_snapshot_revalidates_as_one_registration_act() -> None:
    snapshot, _binding, predictions = registered_snapshot()

    assert snapshot.predictions == predictions
    payload = snapshot.model_dump_json()
    revived = type(snapshot).model_validate_json(payload)
    assert revived.world_model_sha256 == snapshot.world_model_sha256
