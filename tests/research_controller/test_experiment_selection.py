"""ARL-2 mechanical experiment selection: eligibility, scoring, fail-closed choice.

Covers the selector's mechanical eligibility gates, the measured-only EIG score
over the snapshot's belief state, the smallest-hash tie-break, and the
hash-closed receipt's self-checks.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia.memory.belief import Credence, normalized_information_gain
from aletheia.protocols.capabilities import (
    ArtifactKind,
    CapabilityCatalog,
    CapabilityManifestV2,
)
from aletheia.protocols.claim_contracts import (
    ClaimKind,
    ClaimStrength,
    EpistemicKind,
    EvidenceModality,
)
from aletheia.protocols.schemas import ProtocolPortDirection, ProtocolStepRole
from aletheia.research_controller.experiment_selection import (
    SELECTION_RULE_SHA256,
    ExperimentSelectionError,
    ExperimentSelectionReceipt,
    HypothesisOutcomeBinding,
    SelectionBlocker,
    SelectionCandidate,
    SelectionPolicy,
    SplitState,
    select_discriminating_experiment,
)

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from test_campaign_authoring import (  # noqa: E402
    NOW,
    PRINCIPAL,
    registered_snapshot,
)

_PROTOCOL_FIXTURES = _HERE.parent / "protocols"
sys.path.insert(0, str(_PROTOCOL_FIXTURES))
from fixtures import (  # noqa: E402
    _claim_ceiling,
    _cpu_resource,
    _manifest,
    _PortSpec,
    _resource_request,
    _StepPlan,
    digest,
)


def _qualified_manifest(
    capability_id: str = "capability.arl2_discriminator",
) -> CapabilityManifestV2:
    cpu = _cpu_resource("resource.cpu.arl2")
    ports = {
        "p.in": _PortSpec("p.in", ProtocolPortDirection.INPUT, ArtifactKind.TABLE),
        "p.out": _PortSpec("p.out", ProtocolPortDirection.OUTPUT, ArtifactKind.JSON),
    }
    plan = _StepPlan(
        "step.01_discriminator",
        capability_id,
        "operation.arl2_discriminator",
        ("p.in",),
        ("p.out",),
        (),
        _resource_request(cpu),
        ProtocolStepRole.SCIENTIFIC_EXECUTOR,
    )
    return _manifest(
        plan,
        ports=ports,
        epistemic_kind=EpistemicKind.HYPOTHESIS_DISCRIMINATION,
        claim_ceiling=_claim_ceiling(
            ClaimKind.PREDICTIVE, ClaimStrength.TENTATIVE, EvidenceModality.COMPUTATIONAL
        ),
    )


def _suspended_variant(manifest: CapabilityManifestV2) -> CapabilityManifestV2:
    payload = manifest.model_dump(mode="python")
    payload["qualification"]["status"] = "suspended"
    return CapabilityManifestV2.model_validate(payload)


def _candidate(
    predictions,
    binding,
    *,
    protocol_label: str,
    capability_manifest_sha256: str,
    flipped_outcome: bool = False,
    bound_batch_group_ids: tuple[str, ...] = ("batch.g1",),
) -> SelectionCandidate:
    by_hypothesis = {
        item.hypothesis_sha256: item.predicted_outcome_sha256 for item in predictions
    }
    outcomes = tuple(
        sorted(
            (
                HypothesisOutcomeBinding(
                    hypothesis_sha256=hash_,
                    predicted_outcome_sha256=(
                        digest("flipped") if flipped_outcome else outcome
                    ),
                )
                for hash_, outcome in by_hypothesis.items()
            ),
            key=lambda item: item.hypothesis_sha256,
        )
    )
    return SelectionCandidate(
        protocol_sha256=digest(protocol_label),
        observable_spec_sha256=binding.observable_spec_sha256,
        measurement_protocol_sha256=binding.measurement_protocol_sha256,
        outcome_space_sha256=binding.outcome_space_sha256,
        predicted_outcomes=outcomes,
        capability_manifest_sha256=capability_manifest_sha256,
        bound_batch_group_ids=bound_batch_group_ids,
    )


def _tilted_snapshot(snapshot):
    """The same snapshot with a moved belief state: 0.99/0.01 over the actives."""

    payload = json.loads(snapshot.model_dump_json())
    id_to_sha = {
        item.hypothesis_id: item.hypothesis_sha256 for item in snapshot.hypotheses
    }
    tilted = {
        id_to_sha["hyp_" + digest("arl2:h1_plane_doping")[:32]]: 0.99,
        id_to_sha["hyp_" + digest("arl2:h2_complexity_ad")[:32]]: 0.01,
    }
    for belief in payload["belief_state"]["hypothesis_beliefs"]:
        belief["probability"] = tilted.get(belief["hypothesis_sha256"], 0.0)
    return type(snapshot).model_validate(payload)


def test_selection_chooses_the_bound_qualified_candidate() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    catalog = CapabilityCatalog(manifests=(manifest,))
    candidate = _candidate(
        predictions, binding, protocol_label="protocol:d1", capability_manifest_sha256=manifest.manifest_sha256
    )
    kwargs = dict(
        world_model=snapshot,
        candidates=(candidate,),
        selection_policy=SelectionPolicy(
            eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
        ),
        split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1", "batch.g2")),
        capability_catalog=catalog,
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )
    receipt = select_discriminating_experiment(**kwargs)
    repeat = select_discriminating_experiment(**kwargs)

    assert receipt.chosen_protocol_sha256 == candidate.protocol_sha256
    assert receipt.chosen_eig == pytest.approx(2.0)
    assert receipt.world_model_sha256 == snapshot.world_model_sha256
    assert receipt.capability_catalog_sha256 == catalog.catalog_sha256
    assert receipt.receipt_sha256 == repeat.receipt_sha256

    revived = ExperimentSelectionReceipt.model_validate(json.loads(receipt.model_dump_json()))
    assert revived.receipt_sha256 == receipt.receipt_sha256


def test_selection_tie_breaks_to_the_smallest_protocol_hash() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    catalog = CapabilityCatalog(manifests=(manifest,))
    candidates = tuple(
        _candidate(
            predictions, binding, protocol_label=label, capability_manifest_sha256=manifest.manifest_sha256
        )
        for label in ("protocol:d1", "protocol:d2")
    )
    receipt = select_discriminating_experiment(
        world_model=snapshot,
        candidates=candidates,
        selection_policy=SelectionPolicy(
            eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
        ),
        split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1",)),
        capability_catalog=catalog,
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )

    assert all(record.eligible for record in receipt.candidate_records)
    assert len({record.eig for record in receipt.candidate_records}) == 1
    assert receipt.chosen_protocol_sha256 == min(
        item.protocol_sha256 for item in candidates
    )


def test_selection_scores_the_snapshot_belief_state() -> None:
    snapshot, binding, predictions = registered_snapshot()
    tilted = _tilted_snapshot(snapshot)
    manifest = _qualified_manifest()
    candidate = _candidate(
        predictions,
        binding,
        protocol_label="protocol:d1",
        capability_manifest_sha256=manifest.manifest_sha256,
        bound_batch_group_ids=("batch.g2",),
    )
    receipt = select_discriminating_experiment(
        world_model=tilted,
        candidates=(candidate,),
        selection_policy=SelectionPolicy(
            eig_floor=0.0, selection_rule_sha256=SELECTION_RULE_SHA256
        ),
        split_state=SplitState(round_index=2, unspent_group_ids=("batch.g2",)),
        capability_catalog=CapabilityCatalog(manifests=(manifest,)),
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )

    expected = math.fsum(
        normalized_information_gain(Credence(2.0 * p, 2.0 * (1.0 - p)), p)
        for p in (0.99, 0.01, 0.0, 0.0, 0.0)
    )
    assert receipt.chosen_eig == pytest.approx(expected)
    assert receipt.chosen_eig < 1.0


def test_selection_fails_closed_with_capability_blockers() -> None:
    snapshot, binding, predictions = registered_snapshot()
    suspended = _suspended_variant(_qualified_manifest())
    unknown = _candidate(
        predictions,
        binding,
        protocol_label="protocol:unknown",
        capability_manifest_sha256=digest("capability:unknown"),
    )
    with pytest.raises(ExperimentSelectionError) as excinfo:
        select_discriminating_experiment(
            world_model=snapshot,
            candidates=(unknown,),
            selection_policy=SelectionPolicy(
                eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
            ),
            split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1",)),
            capability_catalog=CapabilityCatalog(manifests=(suspended,)),
            authored_at=NOW,
            principal_id=PRINCIPAL,
        )
    assert SelectionBlocker.CAPABILITY_NOT_QUALIFIED in excinfo.value.records[0].blocker_codes


def test_selection_fails_closed_when_the_batch_is_spent() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    candidate = _candidate(
        predictions,
        binding,
        protocol_label="protocol:d1",
        capability_manifest_sha256=manifest.manifest_sha256,
        bound_batch_group_ids=("batch.g1",),
    )
    with pytest.raises(ExperimentSelectionError) as excinfo:
        select_discriminating_experiment(
            world_model=snapshot,
            candidates=(candidate,),
            selection_policy=SelectionPolicy(
                eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
            ),
            split_state=SplitState(round_index=1, unspent_group_ids=("batch.g9",)),
            capability_catalog=CapabilityCatalog(manifests=(manifest,)),
            authored_at=NOW,
            principal_id=PRINCIPAL,
        )
    assert SelectionBlocker.BATCH_SPENT_OR_EMPTY in excinfo.value.records[0].blocker_codes


def test_selection_fails_closed_below_the_eig_floor() -> None:
    snapshot, binding, predictions = registered_snapshot()
    tilted = _tilted_snapshot(snapshot)
    manifest = _qualified_manifest()
    candidate = _candidate(
        predictions,
        binding,
        protocol_label="protocol:d1",
        capability_manifest_sha256=manifest.manifest_sha256,
        bound_batch_group_ids=("batch.g2",),
    )
    with pytest.raises(ExperimentSelectionError) as excinfo:
        select_discriminating_experiment(
            world_model=tilted,
            candidates=(candidate,),
            selection_policy=SelectionPolicy(
                eig_floor=1.0, selection_rule_sha256=SELECTION_RULE_SHA256
            ),
            split_state=SplitState(round_index=2, unspent_group_ids=("batch.g2",)),
            capability_catalog=CapabilityCatalog(manifests=(manifest,)),
            authored_at=NOW,
            principal_id=PRINCIPAL,
        )
    assert excinfo.value.records[0].blocker_codes == (SelectionBlocker.BELOW_EIG_FLOOR,)


def test_selection_fails_closed_when_predictions_are_not_bound() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    candidate = _candidate(
        predictions,
        binding,
        protocol_label="protocol:d1",
        capability_manifest_sha256=manifest.manifest_sha256,
        flipped_outcome=True,
    )
    with pytest.raises(ExperimentSelectionError) as excinfo:
        select_discriminating_experiment(
            world_model=snapshot,
            candidates=(candidate,),
            selection_policy=SelectionPolicy(
                eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
            ),
            split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1",)),
            capability_catalog=CapabilityCatalog(manifests=(manifest,)),
            authored_at=NOW,
            principal_id=PRINCIPAL,
        )
    assert SelectionBlocker.PREDICTIONS_NOT_BOUND in excinfo.value.records[0].blocker_codes


def test_selection_rejects_duplicate_candidates() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    candidate = _candidate(
        predictions, binding, protocol_label="protocol:d1", capability_manifest_sha256=manifest.manifest_sha256
    )
    with pytest.raises(ValueError, match="candidate protocols must be unique"):
        select_discriminating_experiment(
            world_model=snapshot,
            candidates=(candidate, candidate),
            selection_policy=SelectionPolicy(
                eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
            ),
            split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1",)),
            capability_catalog=CapabilityCatalog(manifests=(manifest,)),
            authored_at=NOW,
            principal_id=PRINCIPAL,
        )


def test_receipt_rejects_tampered_choices() -> None:
    snapshot, binding, predictions = registered_snapshot()
    manifest = _qualified_manifest()
    candidates = tuple(
        _candidate(
            predictions, binding, protocol_label=label, capability_manifest_sha256=manifest.manifest_sha256
        )
        for label in ("protocol:d1", "protocol:d2")
    )
    receipt = select_discriminating_experiment(
        world_model=snapshot,
        candidates=candidates,
        selection_policy=SelectionPolicy(
            eig_floor=0.5, selection_rule_sha256=SELECTION_RULE_SHA256
        ),
        split_state=SplitState(round_index=1, unspent_group_ids=("batch.g1",)),
        capability_catalog=CapabilityCatalog(manifests=(manifest,)),
        authored_at=NOW,
        principal_id=PRINCIPAL,
    )

    rebound = json.loads(receipt.model_dump_json())
    rebound["chosen_protocol_sha256"] = max(
        item.protocol_sha256 for item in candidates
    )
    with pytest.raises(ValidationError, match="not the smallest-hash maximum-score candidate"):
        ExperimentSelectionReceipt.model_validate(rebound)

    absent = json.loads(receipt.model_dump_json())
    absent["chosen_protocol_sha256"] = digest("protocol:absent")
    with pytest.raises(ValidationError, match="absent from the candidate records"):
        ExperimentSelectionReceipt.model_validate(absent)

    rescored = json.loads(receipt.model_dump_json())
    rescored["chosen_eig"] = receipt.chosen_eig + 0.5
    with pytest.raises(ValidationError, match="chosen score does not match"):
        ExperimentSelectionReceipt.model_validate(rescored)


def test_selection_policy_pins_the_preregistered_rule() -> None:
    with pytest.raises(ValidationError, match="selection policy pins another rule"):
        SelectionPolicy(eig_floor=0.5, selection_rule_sha256=digest("rule:other"))
