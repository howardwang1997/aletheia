"""Revision-authoring tests: golden vector, seal, fail-closed coverage."""

from __future__ import annotations

import hashlib
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "protocols"))

from fixtures import fixture_by_name  # noqa: E402

from aletheia.protocols.world_models import (  # noqa: E402
    AssumptionDisposition,
    AssumptionScope,
    AssumptionVersionV2,
    BeliefStateVersionV2,
    BeliefUpdateBasis,
    HypothesisBeliefV2,
    HypothesisLifecycle,
    HypothesisVersionV2,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_controller.world_model_revision import (  # noqa: E402
    REVISION_UPDATE_RULE_SHA256,
    AdmittedObservationBinding,
    assert_revision_belief_basis,
    revise_world_model_v2,
    verify_authored_revision_v2,
    verify_round_split_binding,
)

_T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_PI = "user:pi"
_OBSERVATION_RECEIPT = "a" * 64
_CONTINUATION_RECEIPT = "b" * 64

# Priors: h1 0.3, h2 0.3, h0 0.4, h3 (already retired) 0.0.  The sealed
# prediction binds h1 against h2, so h0 is the one active hypothesis a legal
# retirement can remove; h1 and h2 are referenced and cannot be retired.
_PARENT_SNAPSHOT_SHA256 = "183aa6a6d11a0e1f6a7fabd51c7808d3d1bd268336a532493287fe9bb3227321"
_REVISED_SNAPSHOT_SHA256 = "84fdcad2cca78d619132d81abf29ea01d7ef1a0bf71a58c0ac5317a433536857"
_REVISED_BELIEF_SHA256 = "78ac09d675969f44107f829d5dc5c7bd9c444a7a6b6490f3d9735eb6bf6bc657"
_RETIRED_SNAPSHOT_SHA256 = "ee7ac788f17bddec695d108ebdbd38eabb766317242d8779c87470aff0e4e855"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _scope_sha256() -> str:
    return fixture_by_name("grouped_regression").request.protocol.graph_scope.graph_scope_sha256


def _hyp(label: str, lifecycle: HypothesisLifecycle) -> HypothesisVersionV2:
    return HypothesisVersionV2(
        hypothesis_id=f"hyp_{_digest(f'arl2-revision:{label}')[:32]}",
        version=1,
        graph_scope_sha256=_scope_sha256(),
        lifecycle=lifecycle,
        statement=f"statement {label}",
        explanatory_model=f"model {label}",
        rationale_sha256=_digest(f"rationale {label}"),
        semantic_delta=f"initial {label}",
        authored_by_principal_id=_PI,
        authored_at=_T0,
    )


def _prediction(target: HypothesisVersionV2, other: HypothesisVersionV2) -> PredictionVersionV2:
    return PredictionVersionV2(
        prediction_id=f"pred_{_digest('arl2-revision:prediction')[:32]}",
        version=1,
        graph_scope_sha256=_scope_sha256(),
        hypothesis_sha256=target.hypothesis_sha256,
        observable_spec_sha256=_digest("observable"),
        measurement_protocol_sha256=_digest("measurement"),
        outcome_space_sha256=_digest("outcome-space"),
        predicted_outcome_sha256=_digest("outcome.positive"),
        discriminates_from_hypothesis_sha256s=(other.hypothesis_sha256,),
        semantic_delta="initial frozen prediction",
        authored_by_principal_id=_PI,
        authored_at=_T0,
    )


def _assumption() -> AssumptionVersionV2:
    return AssumptionVersionV2(
        assumption_id=f"asm_{_digest('arl2-revision:assumption')[:32]}",
        version=1,
        graph_scope_sha256=_scope_sha256(),
        scope=AssumptionScope.GLOBAL,
        applies_to_hypothesis_sha256s=(),
        statement="the registered feature space is composition-averaged",
        violation_consequence="predictions over plane-specific doping lose their binding",
        disposition=AssumptionDisposition.UNRESOLVED,
        semantic_delta="initial global assumption",
        authored_by_principal_id=_PI,
        authored_at=_T0,
    )


def _parent() -> WorldModelSnapshotV2:
    h1 = _hyp("h1", HypothesisLifecycle.ACTIVE)
    h2 = _hyp("h2", HypothesisLifecycle.ACTIVE)
    h0 = _hyp("h0", HypothesisLifecycle.ACTIVE)
    h3 = _hyp("h3", HypothesisLifecycle.RETIRED)
    priors = {"statement h1": 0.3, "statement h2": 0.3, "statement h0": 0.4, "statement h3": 0.0}
    beliefs = tuple(
        sorted(
            (
                HypothesisBeliefV2(
                    hypothesis_sha256=item.hypothesis_sha256, probability=priors[item.statement]
                )
                for item in (h1, h2, h0, h3)
            ),
            key=lambda item: item.hypothesis_sha256,
        )
    )
    return WorldModelSnapshotV2(
        graph_scope=fixture_by_name("grouped_regression").request.protocol.graph_scope,
        world_model_id=f"wm_{_digest('arl2-revision:world-model')[:32]}",
        version=1,
        hypotheses=tuple(
            sorted(
                (h1, h2, h0, h3),
                key=lambda item: (item.hypothesis_id, item.version, item.hypothesis_sha256),
            )
        ),
        assumptions=(_assumption(),),
        predictions=(_prediction(h1, h2),),
        belief_state=BeliefStateVersionV2(
            belief_id=f"blf_{_digest('arl2-revision:belief')[:32]}",
            version=1,
            graph_scope_sha256=_scope_sha256(),
            hypothesis_beliefs=beliefs,
            update_basis=BeliefUpdateBasis.PRIOR,
            source_observation_receipt_sha256=None,
            update_rule_sha256=REVISION_UPDATE_RULE_SHA256,
            authored_by_principal_id=_PI,
            authored_at=_T0,
        ),
        model_limitations=("single material family",),
        semantic_delta="initial registration",
        authored_by_principal_id=_PI,
        authored_at=_T0,
    )


def _observations(
    parent: WorldModelSnapshotV2,
    *,
    h1_holds: bool,
    h2_holds: bool,
    h0_holds: bool,
):
    by_statement = {item.statement: item for item in parent.hypotheses}
    return tuple(
        AdmittedObservationBinding(
            hypothesis_sha256=by_statement[f"statement {label}"].hypothesis_sha256,
            holds=holds,
            observation_receipt_sha256=_OBSERVATION_RECEIPT,
        )
        for label, holds in (("h1", h1_holds), ("h2", h2_holds), ("h0", h0_holds))
    )


def _revise(
    parent,
    *,
    retire=(),
    h1_holds=True,
    h2_holds=True,
    h0_holds=True,
    continuation=_CONTINUATION_RECEIPT,
    scope=None,
):
    return revise_world_model_v2(
        parent=parent,
        observations=_observations(parent, h1_holds=h1_holds, h2_holds=h2_holds, h0_holds=h0_holds),
        continuation_receipt_sha256=continuation,
        retire_hypothesis_ids=retire,
        graph_scope=scope,
        authored_at=_T1,
        principal_id=_PI,
    )


def _moved_scope(parent):
    return parent.graph_scope.model_copy(
        update={"graph_snapshot_sha256": _digest("moved-graph-snapshot")}
    )


def _retire_target(parent: WorldModelSnapshotV2) -> HypothesisVersionV2:
    return next(item for item in parent.hypotheses if item.statement == "statement h0")


def test_revision_golden_vector_is_frozen() -> None:
    parent = _parent()
    child = _revise(parent)
    retired = _revise(parent, retire=(_retire_target(parent).hypothesis_id,))
    assert parent.world_model_sha256 == _PARENT_SNAPSHOT_SHA256
    assert child.world_model_sha256 == _REVISED_SNAPSHOT_SHA256
    assert child.belief_state.belief_state_sha256 == _REVISED_BELIEF_SHA256
    assert retired.world_model_sha256 == _RETIRED_SNAPSHOT_SHA256


def test_posterior_moves_only_admitted_verdicts_and_seals_the_rest() -> None:
    parent = _parent()
    # mixed verdicts: raw posteriors sum to exactly one, so this pins the
    # ported Beta math itself
    child = _revise(parent, h1_holds=True, h2_holds=False, h0_holds=False)
    assert child.version == parent.version + 1
    assert child.revision_parent_sha256 == parent.world_model_sha256
    assert child.world_model_id == parent.world_model_id
    probabilities = {
        belief.hypothesis_sha256: belief.probability
        for belief in child.belief_state.hypothesis_beliefs
    }
    by_statement = {item.statement: item for item in parent.hypotheses}
    assert probabilities[by_statement["statement h1"].hypothesis_sha256] == pytest.approx(8 / 15)
    assert probabilities[by_statement["statement h2"].hypothesis_sha256] == pytest.approx(1 / 5)
    assert probabilities[by_statement["statement h0"].hypothesis_sha256] == pytest.approx(4 / 15)
    assert probabilities[by_statement["statement h3"].hypothesis_sha256] == 0.0
    assert math.fsum(probabilities.values()) == pytest.approx(1.0, abs=1e-15)
    # every hypothesis entry is carried byte-identical: the update lives in the
    # belief state, not in the hypotheses
    assert child.hypotheses == parent.hypotheses
    assert child.predictions == parent.predictions
    assert child.assumptions == parent.assumptions
    assert child.model_limitations == parent.model_limitations
    belief = child.belief_state
    assert belief.version == parent.belief_state.version + 1
    assert belief.revision_parent_sha256 == parent.belief_state.belief_state_sha256
    assert belief.update_basis is BeliefUpdateBasis.VALIDATED_OBSERVATION
    assert belief.source_observation_receipt_sha256 == _OBSERVATION_RECEIPT
    assert belief.update_rule_sha256 == REVISION_UPDATE_RULE_SHA256
    assert verify_authored_revision_v2(parent=parent, child=child) is child
    assert_revision_belief_basis(child)


def test_all_hold_verdicts_renormalize_with_the_legacy_convention() -> None:
    parent = _parent()
    child = _revise(parent)  # every active hypothesis holds
    probabilities = {
        belief.hypothesis_sha256: belief.probability
        for belief in child.belief_state.hypothesis_beliefs
    }
    by_statement = {item.statement: item for item in parent.hypotheses}
    # raw posteriors 8/15 + 8/15 + 9/15 sum to 5/3, so the carried legacy
    # renormalization divides through and folds the residue into the last entry
    assert probabilities[by_statement["statement h1"].hypothesis_sha256] == pytest.approx(8 / 25)
    assert probabilities[by_statement["statement h2"].hypothesis_sha256] == pytest.approx(8 / 25)
    assert probabilities[by_statement["statement h0"].hypothesis_sha256] == pytest.approx(9 / 25)
    assert probabilities[by_statement["statement h3"].hypothesis_sha256] == 0.0
    assert math.fsum(probabilities.values()) == pytest.approx(1.0, abs=1e-15)
    assert verify_authored_revision_v2(parent=parent, child=child) is child


def test_revision_is_deterministic_and_receipt_bound() -> None:
    parent = _parent()
    assert _revise(parent) == _revise(parent)
    other_continuation = _revise(parent, continuation="c" * 64)
    assert other_continuation != _revise(parent)
    assert other_continuation.world_model_sha256 != _revise(parent).world_model_sha256
    probabilities = {
        belief.hypothesis_sha256: belief.probability
        for belief in other_continuation.belief_state.hypothesis_beliefs
    }
    assert tuple(sorted(probabilities.values())) == pytest.approx([0.0, 8 / 25, 8 / 25, 9 / 25])


def test_revision_rebinds_to_the_current_graph_view() -> None:
    parent = _parent()
    scope = _moved_scope(parent)
    child = _revise(parent, scope=scope)
    assert child.graph_scope == scope
    assert all(item.graph_scope_sha256 == scope.graph_scope_sha256 for item in child.hypotheses)
    assert all(item.graph_scope_sha256 == scope.graph_scope_sha256 for item in child.predictions)
    assert all(item.graph_scope_sha256 == scope.graph_scope_sha256 for item in child.assumptions)
    assert child.belief_state.graph_scope_sha256 == scope.graph_scope_sha256
    # the posterior math is scope-independent
    probabilities = {
        belief.hypothesis_sha256: belief.probability
        for belief in child.belief_state.hypothesis_beliefs
    }
    assert tuple(sorted(probabilities.values())) == pytest.approx([0.0, 8 / 25, 8 / 25, 9 / 25])
    assert verify_authored_revision_v2(parent=parent, child=child) is child
    # a re-scoped child is still sealed: mutating a carried member fails
    tampered = child.model_copy(
        update={
            "hypotheses": (
                child.hypotheses[0].model_copy(update={"statement": "tampered"}),
                *child.hypotheses[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="mutated a carried hypothesis version"):
        verify_authored_revision_v2(parent=parent, child=tampered)


def test_retirement_bumps_lifecycle_with_parent_chain() -> None:
    parent = _parent()
    source = _retire_target(parent)
    child = _revise(parent, h1_holds=True, h2_holds=False, retire=(source.hypothesis_id,))
    retired_entry = next(
        item for item in child.hypotheses if item.hypothesis_id == source.hypothesis_id
    )
    assert retired_entry.version == 2
    assert retired_entry.lifecycle is HypothesisLifecycle.RETIRED
    assert retired_entry.revision_parent_sha256 == source.hypothesis_sha256
    probabilities = {
        belief.hypothesis_sha256: belief.probability
        for belief in child.belief_state.hypothesis_beliefs
    }
    # h1 posterior 8/15, h2 posterior 3/15, and the retired prior 0.4 = 6/15
    # renormalize over the raw sum 17/15
    assert probabilities[retired_entry.hypothesis_sha256] == pytest.approx(6 / 17)
    carried_h1 = next(item for item in child.hypotheses if item.statement == "statement h1")
    carried_h2 = next(item for item in child.hypotheses if item.statement == "statement h2")
    assert probabilities[carried_h1.hypothesis_sha256] == pytest.approx(8 / 17)
    assert probabilities[carried_h2.hypothesis_sha256] == pytest.approx(3 / 17)
    assert math.fsum(probabilities.values()) == pytest.approx(1.0, abs=1e-15)
    assert verify_authored_revision_v2(parent=parent, child=child) is child


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("empty", dict(observations=())),
        (
            "mixed-receipts",
            dict(
                observations=_observations(_parent(), h1_holds=True, h2_holds=True, h0_holds=True)
                + (
                    AdmittedObservationBinding(
                        hypothesis_sha256=_digest("unbound"),
                        holds=True,
                        observation_receipt_sha256="c" * 64,
                    ),
                )
            ),
        ),
        (
            "partial-coverage",
            dict(
                observations=_observations(_parent(), h1_holds=True, h2_holds=True, h0_holds=True)[
                    :1
                ]
            ),
        ),
        (
            "duplicate-binding",
            dict(
                observations=_observations(_parent(), h1_holds=True, h2_holds=True, h0_holds=True)
                + _observations(_parent(), h1_holds=True, h2_holds=True, h0_holds=True)[:1]
            ),
        ),
        ("unknown-retire", dict(retire=("hyp_" + "0" * 32,))),
        (
            "retire-breaks-prediction",
            dict(
                retire=(
                    next(
                        item for item in _parent().hypotheses if item.statement == "statement h1"
                    ).hypothesis_id,
                )
            ),
        ),
        ("predates-parent", dict(authored_at=_T0 - timedelta(seconds=1))),
        ("no-belief-state", dict(strip_belief=True)),
        ("bad-continuation", dict(continuation="not-a-sha")),
    ],
)
def test_revise_fails_closed(label, kwargs) -> None:
    parent = _parent()
    if kwargs.pop("strip_belief", False):
        parent = parent.model_copy(update={"belief_state": None})
    observations = kwargs.pop("observations", None)
    retire = kwargs.pop("retire", ())
    authored_at = kwargs.pop("authored_at", _T1)
    continuation = kwargs.pop("continuation", _CONTINUATION_RECEIPT)
    if observations is None:
        observations = _observations(parent, h1_holds=True, h2_holds=True, h0_holds=True)
    with pytest.raises(ValueError):
        revise_world_model_v2(
            parent=parent,
            observations=observations,
            continuation_receipt_sha256=continuation,
            retire_hypothesis_ids=retire,
            authored_at=authored_at,
            principal_id=_PI,
        )


def _illegal_children(parent: WorldModelSnapshotV2, child: WorldModelSnapshotV2):
    retired_child = _revise(parent, retire=(_retire_target(parent).hypothesis_id,))
    return {
        "wrong-parent": child.model_copy(update={"revision_parent_sha256": "c" * 64}),
        "wrong-identity": child.model_copy(
            update={"world_model_id": f"wm_{_digest('other-world-model')[:32]}"}
        ),
        "unbound-basis": child.model_copy(
            update={
                "belief_state": child.belief_state.model_copy(
                    update={
                        "update_basis": BeliefUpdateBasis.MODEL_REVISION,
                        "source_observation_receipt_sha256": None,
                    }
                )
            }
        ),
        "wrong-rule": child.model_copy(
            update={
                "belief_state": child.belief_state.model_copy(
                    update={"update_rule_sha256": "d" * 64}
                )
            }
        ),
        "predates": child.model_copy(update={"authored_at": _T0 - timedelta(seconds=1)}),
        "mutated-prediction": child.model_copy(
            update={
                "predictions": (
                    child.predictions[0].model_copy(
                        update={"predicted_outcome_sha256": _digest("outcome.negative")}
                    ),
                )
            }
        ),
        "mutated-assumption": child.model_copy(
            update={
                "assumptions": (child.assumptions[0].model_copy(update={"statement": "tampered"}),)
            }
        ),
        "mutated-hypothesis": child.model_copy(
            update={
                "hypotheses": (
                    child.hypotheses[0].model_copy(update={"statement": "tampered"}),
                    *child.hypotheses[1:],
                )
            }
        ),
        "dropped-lineage": child.model_copy(update={"hypotheses": child.hypotheses[:3]}),
        "illegal-lifecycle": retired_child.model_copy(
            update={
                "hypotheses": tuple(
                    item.model_copy(update={"revision_parent_sha256": "e" * 64})
                    if item.version == 2
                    else item
                    for item in retired_child.hypotheses
                )
            }
        ),
    }


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("wrong-parent", "exact parent snapshot"),
        ("wrong-identity", "exact parent snapshot"),
        ("unbound-basis", "unbound belief basis"),
        ("wrong-rule", "unbound belief basis"),
        ("predates", "predates its parent"),
        ("mutated-prediction", "mutated a sealed prediction"),
        ("mutated-assumption", "mutated a sealed assumption"),
        ("mutated-hypothesis", "mutated a carried hypothesis version"),
        ("dropped-lineage", "changed the hypothesis lineages"),
        ("illegal-lifecycle", "transitions a hypothesis illegally"),
    ],
)
def test_verify_rejects_each_illegal_class(label, message) -> None:
    parent = _parent()
    child = _revise(parent)
    bad = _illegal_children(parent, child)[label]
    with pytest.raises(ValueError, match=message):
        verify_authored_revision_v2(parent=parent, child=bad)


def test_assert_revision_belief_basis_gates_version_two_only() -> None:
    parent = _parent()
    child = _revise(parent)
    assert_revision_belief_basis(parent)  # version 1 needs no observation receipt
    assert_revision_belief_basis(child)
    stripped = child.model_copy(
        update={
            "belief_state": child.belief_state.model_copy(
                update={
                    "update_basis": BeliefUpdateBasis.PRIOR,
                    "source_observation_receipt_sha256": None,
                }
            )
        }
    )
    with pytest.raises(ValueError, match="unbound belief basis"):
        assert_revision_belief_basis(stripped)


@pytest.mark.parametrize(
    ("label", "kwargs", "message"),
    [
        (
            "partition",
            dict(
                sealed_group_ids=("g1", "g2", "g3"),
                spent_group_ids=("g1",),
                unspent_group_ids=("g2", "g3"),
                bound_group_ids=("g2",),
            ),
            None,
        ),
        (
            "first-round-empty-spent",
            dict(
                sealed_group_ids=("g1", "g2"),
                spent_group_ids=(),
                unspent_group_ids=("g1", "g2"),
                bound_group_ids=("g1",),
            ),
            None,
        ),
        (
            "spend-overlap",
            dict(
                sealed_group_ids=("g1", "g2"),
                spent_group_ids=("g1", "g2"),
                unspent_group_ids=("g2",),
                bound_group_ids=("g2",),
            ),
            "two spend states",
        ),
        (
            "bound-spent",
            dict(
                sealed_group_ids=("g1", "g2"),
                spent_group_ids=("g1",),
                unspent_group_ids=("g2",),
                bound_group_ids=("g1", "g2"),
            ),
            "outside its unspent confirmation split",
        ),
        (
            "missing-group",
            dict(
                sealed_group_ids=("g1", "g2", "g3"),
                spent_group_ids=("g1",),
                unspent_group_ids=("g2",),
                bound_group_ids=("g2",),
            ),
            "does not partition its sealed groups",
        ),
        (
            "empty-bound",
            dict(
                sealed_group_ids=("g1",),
                spent_group_ids=(),
                unspent_group_ids=("g1",),
                bound_group_ids=(),
            ),
            "binds no confirmation groups",
        ),
        (
            "unordered",
            dict(
                sealed_group_ids=("g2", "g1"),
                spent_group_ids=(),
                unspent_group_ids=("g1", "g2"),
                bound_group_ids=("g1",),
            ),
            "canonically ordered",
        ),
        (
            "duplicate",
            dict(
                sealed_group_ids=("g1", "g1"),
                spent_group_ids=(),
                unspent_group_ids=("g1",),
                bound_group_ids=("g1",),
            ),
            "canonically ordered",
        ),
    ],
)
def test_round_split_binding(label, kwargs, message) -> None:
    if message is None:
        verify_round_split_binding(**kwargs)
    else:
        with pytest.raises(ValueError, match=message):
            verify_round_split_binding(**kwargs)
