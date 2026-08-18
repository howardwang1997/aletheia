from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from aletheia.db import REPO_ROOT, create_all
from aletheia.domains.materials.phonon_commissioning import (
    PhononCommissioningEvidenceError,
    StructureSignalEvidenceReceipt,
    apply_phonon_quest_commissioning,
    audit_phonon_quest_commissioning,
    build_phonon_quest_commissioning_manifest,
    local_artifact_identity,
    verify_commissioning_artifacts,
)
from aletheia.epistemics.schemas import HypothesisRole
from aletheia.programs import GraphNodeState, ProgramGraphStore

FROZEN_AT = datetime(2026, 8, 18, 1, 2, 3, tzinfo=timezone.utc)


def _identity(name: str):
    return local_artifact_identity(REPO_ROOT / name)


def _evidence(*, bad_dataset_hash: bool = False) -> StructureSignalEvidenceReceipt:
    dataset = _identity("pyproject.toml")
    if bad_dataset_hash:
        dataset = dataset.model_copy(update={"sha256": "0" * 64})
    return StructureSignalEvidenceReceipt(
        dataset_file=dataset,
        plan_file=_identity("environment.yml"),
        result_file=_identity("README.md"),
        dataset_ref="test_phonons",
        source_uri="https://example.invalid/test-phonons",
        license_expression="CC0-1.0",
        license_uri="https://example.invalid/license",
        structure_column="structure",
        target_column="last phdos peak",
        target_quantity_kind_id="phonon.last_phdos_peak_frequency",
        target_unit_ucum="cm-1",
        row_count=1265,
        protocol_sha256="1" * 64,
        implementation_sha256="2" * 64,
        plan_sha256="3" * 64,
        dataset_receipt_sha256="4" * 64,
        result_sha256="5" * 64,
        result_disposition="robust_aligned_structure_signal",
        result_completed_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
    )


def _manifest(*, bad_dataset_hash: bool = False):
    return build_phonon_quest_commissioning_manifest(
        _evidence(bad_dataset_hash=bad_dataset_hash),
        prepared_at=FROZEN_AT,
        command_principal="pytest:phonon-commissioning",
        identity_namespace=f"test-phonon-{uuid.uuid4().hex}",
    )


def test_blueprint_closes_questions_hypotheses_resources_and_claim_ceiling() -> None:
    manifest = _manifest()

    assert len(manifest.campaigns) == 3
    assert len(manifest.runs) == 3
    assert len(manifest.world_models) == 2
    assert len(manifest.budgets) == 5
    assert manifest.data_role.role.value == "exploration"
    assert manifest.data_asset.profile_json["external_validation"] is False
    assert manifest.quest.resource_boundary["outward_actions_allowed"] is False
    assert manifest.program.knowledge_boundary["mechanism_status"] == "unresolved"
    assert (
        manifest.program.knowledge_boundary["external_replication_status"]
        == "not_yet_attempted"
    )
    assert all(item.allocation_forbidden for item in manifest.external_corpus_candidates)
    assert {item.status for item in manifest.external_corpus_candidates} == {
        "candidate_requires_lineage_and_target_audit",
        "excluded_same_source_lineage",
        "candidate_for_distinct_property_only",
    }
    for world in manifest.world_models:
        assert {item.role for item in world.hypotheses} == {
            HypothesisRole.NULL,
            HypothesisRole.PRIMARY,
            HypothesisRole.ALTERNATIVE,
        }
        assert len(world.assumptions) == len(world.hypotheses)
        assert len(world.predictions) == len(world.hypotheses)


def test_artifact_drift_is_rejected_before_database_mutation() -> None:
    manifest = _manifest(bad_dataset_hash=True)
    with pytest.raises(PhononCommissioningEvidenceError, match="artifact changed"):
        verify_commissioning_artifacts(manifest)


def test_apply_is_exactly_replay_safe_and_auditable() -> None:
    create_all()
    manifest = _manifest()

    first = apply_phonon_quest_commissioning(manifest)
    second = apply_phonon_quest_commissioning(manifest)
    audited = audit_phonon_quest_commissioning(manifest)

    assert first.created_object_count > 0
    assert second.created_object_count == 0
    assert second.replayed_object_count == first.created_object_count
    assert audited.graph_sha256 == first.graph_sha256 == second.graph_sha256
    assert audited.quest_id == manifest.quest.node_id
    assert audited.durable_blockers == manifest.durable_blockers

    graph = ProgramGraphStore().get_quest(manifest.quest.node_id)
    states = {item.node_id: item.state for item in graph.nodes}
    assert states[manifest.quest.node_id] is GraphNodeState.ACTIVE
    assert states[manifest.program.node_id] is GraphNodeState.ACTIVE
    assert states[manifest.initial_active_campaign_id] is GraphNodeState.ACTIVE
    assert sum(state is GraphNodeState.PLANNED for state in states.values()) == 2
    assert len(graph.data_allocations) == 1
    assert len(graph.budget_allocations) == 10
