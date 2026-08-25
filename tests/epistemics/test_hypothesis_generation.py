from __future__ import annotations

import asyncio
import math
from datetime import timedelta

import pytest
from pydantic import ValidationError

import aletheia.epistemics as e
import aletheia.knowledge as k
from aletheia.db import create_all
from aletheia.memory.service import create_run
from knowledge.f8s5_fixtures import (
    build_f8s5_direction_fixture,
    build_f8s5_live_fixture,
)

from .f9s2_fixtures import (
    StaticDeduplicator,
    StaticGenerator,
    StepClock,
    build_deduplication_batch,
    build_f9s2_fixture,
    build_manifests,
    digest,
    revalidate,
    stable_question_id,
)


@pytest.fixture(scope="module")
def direction_gates(tmp_path_factory):
    return {
        novelty_kind: build_f8s5_direction_fixture(
            asyncio.run(
                build_f8s5_live_fixture(
                    tmp_path_factory.mktemp(f"f9s2-{novelty_kind}"),
                    novelty_kind=novelty_kind,
                )
            )
        )["gate"]
        for novelty_kind in ("strong", "known")
    }


async def _run(parts, campaign_id: str = "campaign:f9s2:test") -> e.HypothesisGenerationCampaign:
    return await e.run_competing_hypothesis_generation(
        campaign_id=campaign_id,
        direction_gate=parts["gate"],
        policy=parts["policy"],
        request=parts["request"],
        generator=parts["generator"],
        deduplicator=parts["deduplicator"],
        clock=parts["clock"],
    )


def _replace_hypothesis(
    batch: e.HypothesisGenerationBatch,
    local_hypothesis_id: str,
    **updates: object,
) -> e.HypothesisGenerationBatch:
    changed = tuple(
        revalidate(e.HypothesisDraft, item, **updates)
        if item.local_hypothesis_id == local_hypothesis_id
        else item
        for item in batch.hypotheses
    )
    return revalidate(
        e.HypothesisGenerationBatch,
        batch,
        hypotheses=tuple(sorted(changed, key=lambda item: item.local_hypothesis_id)),
    )


def _install_outputs(
    parts: dict[str, object],
    batch: e.HypothesisGenerationBatch,
    deduplication: object | None = None,
) -> None:
    deduplication = deduplication or build_deduplication_batch(
        generation_batch=batch,
        deduplicator_manifest=parts["deduplicator_manifest"],
    )
    parts["generation_batch"] = batch
    parts["deduplication_batch"] = deduplication
    parts["generator"] = StaticGenerator(parts["generator_manifest"], batch)
    parts["deduplicator"] = StaticDeduplicator(parts["deduplicator_manifest"], deduplication)
    completed_at = (
        deduplication.completed_at
        if isinstance(deduplication, e.HypothesisDeduplicationBatch)
        else batch.completed_at
    )
    parts["clock"] = StepClock(completed_at + timedelta(hours=1))


@pytest.mark.asyncio
async def test_f8_grounded_generation_emits_closed_competing_world_model(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])

    campaign = await _run(parts, "campaign:f9s2:ready")
    snapshot = campaign.world_model_snapshot

    assert campaign.disposition is e.HypothesisGenerationDisposition.READY
    assert campaign.blockers == ()
    assert snapshot is not None
    assert len(campaign.generation_batch.hypotheses) == 3
    assert len(campaign.duplicate_resolutions) == 3
    assert len(campaign.discrimination_edges) == math.comb(3, 2)
    assert len(snapshot.hypotheses) == 3
    assert {item.role for item in snapshot.hypotheses} == {
        e.HypothesisRole.NULL,
        e.HypothesisRole.PRIMARY,
        e.HypothesisRole.ALTERNATIVE,
    }
    assert all(item.disposition is e.DuplicateDisposition.KEPT for item in campaign.duplicate_resolutions)
    assert all(
        item.probability == pytest.approx(1 / 3)
        for item in snapshot.belief_state.hypotheses
    )
    assert sum(item.probability for item in snapshot.belief_state.hypotheses) == pytest.approx(1.0)
    assert parts["generator"].calls == 1
    assert parts["deduplicator"].calls == 1


@pytest.mark.asyncio
async def test_generation_receives_only_exact_f8_claim_inputs_and_no_tools(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    campaign = await _run(parts, "campaign:f9s2:input-boundary")
    gate = parts["gate"]
    request = parts["request"]
    received = parts["generator"].received

    assert campaign.disposition is e.HypothesisGenerationDisposition.READY
    assert request.observation_access == "none"
    assert parts["generator_manifest"].tool_names == ()
    assert parts["deduplicator_manifest"].tool_names == ()
    assert received is not None
    assert received["request"] == request
    assert received["candidate_claim"].claim_sha256 == request.candidate_claim_sha256
    assert tuple(item.claim_sha256 for item in received["prior_claims"]) == tuple(
        item.claim_sha256
        for item in gate.novelty_decision.coverage.claim_graph_bundle.graph.claims
        if item.origin is k.ClaimOrigin.PRIOR_ART
    )
    assert tuple(item.relation_sha256 for item in received["accepted_prior_art_relations"]) == (
        request.accepted_prior_art_relation_sha256s
    )


def test_frozen_schema_and_manifest_forbid_ambient_tool_authority(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    generator = parts["generator_manifest"]

    assert generator.output_schema_sha256 == e.HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256
    assert (
        parts["deduplicator_manifest"].output_schema_sha256
        == e.HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256
    )
    with pytest.raises(ValidationError, match="cannot receive tool authority"):
        revalidate(e.HypothesisGeneratorManifest, generator, tool_names=("literature.search",))


def test_generator_and_semantic_reviewer_must_be_independent(direction_gates) -> None:
    gate = direction_gates["strong"]
    shared = digest("f9s2:shared-principal")
    policy, generator, deduplicator = build_manifests(
        gate,
        generator_principal=shared,
        reviewer_principal=shared,
    )

    with pytest.raises(ValueError, match="must be independent"):
        e.build_hypothesis_generation_request(
            request_id="f9s2-non-independent-request",
            run_id="8" * 32,
            question_id=stable_question_id("f9s2:non-independent-question"),
            direction_gate=gate,
            policy=policy,
            generator_manifest=generator,
            deduplicator_manifest=deduplicator,
            issued_at=gate.decided_at + timedelta(hours=1),
        )


def test_unauthorized_f8_direction_cannot_issue_a_generation_request(direction_gates) -> None:
    gate = direction_gates["known"]
    policy, generator, deduplicator = build_manifests(gate)

    assert gate.experiment_authorized is False
    with pytest.raises(ValueError, match="authorized F8 direction"):
        e.build_hypothesis_generation_request(
            request_id="f9s2-rejected-direction-request",
            run_id="7" * 32,
            question_id=stable_question_id("f9s2:rejected-direction-question"),
            direction_gate=gate,
            policy=policy,
            generator_manifest=generator,
            deduplicator_manifest=deduplicator,
            issued_at=gate.decided_at + timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_exact_f8_evidence_rebinding_fails_before_generator_call(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    parts["request"] = revalidate(
        e.HypothesisGenerationRequest,
        parts["request"],
        candidate_claim_sha256=digest("f9s2:forged-candidate"),
    )

    with pytest.raises(ValueError, match="changed exact candidate_claim_sha256"):
        await _run(parts, "campaign:f9s2:rebound")
    assert parts["generator"].calls == 0
    assert parts["deduplicator"].calls == 0


@pytest.mark.asyncio
async def test_raw_semantic_duplicate_is_retained_with_explicit_canonical_mapping(
    direction_gates,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"], duplicate_alternative=True)

    campaign = await _run(parts, "campaign:f9s2:explicit-duplicate")
    duplicate = next(
        item
        for item in campaign.duplicate_resolutions
        if item.disposition is e.DuplicateDisposition.DUPLICATE
    )

    assert campaign.disposition is e.HypothesisGenerationDisposition.READY
    assert len(campaign.generation_batch.hypotheses) == 4
    assert {item.local_hypothesis_id for item in campaign.generation_batch.hypotheses} == {
        "alternative_a",
        "alternative_b",
        "null_h0",
        "primary_a",
    }
    assert duplicate.local_hypothesis_id == "alternative_b"
    assert duplicate.canonical_local_hypothesis_id == "alternative_a"
    assert len(duplicate.supporting_judgment_sha256s) == 1
    assert len(campaign.world_model_snapshot.hypotheses) == 3


@pytest.mark.asyncio
async def test_semantic_equivalence_cannot_merge_scientific_roles(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    pair = ("alternative_a", "null_h0")
    deduplication = build_deduplication_batch(
        generation_batch=parts["generation_batch"],
        deduplicator_manifest=parts["deduplicator_manifest"],
        relations={pair: e.SemanticHypothesisRelation.EQUIVALENT},
    )
    _install_outputs(parts, parts["generation_batch"], deduplication)

    campaign = await _run(parts, "campaign:f9s2:cross-role-duplicate")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_DUPLICATES
    assert campaign.world_model_snapshot is None
    assert any(item.startswith("semantic_duplicate_crosses_roles:") for item in campaign.blockers)


@pytest.mark.parametrize(
    ("relation", "confidence"),
    [
        (e.SemanticHypothesisRelation.UNCERTAIN, 0.99),
        (e.SemanticHypothesisRelation.DISTINCT, 0.50),
    ],
)
@pytest.mark.asyncio
async def test_uncertain_or_low_confidence_semantic_pair_blocks_admission(
    direction_gates,
    relation,
    confidence,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    pair = ("alternative_a", "null_h0")
    deduplication = build_deduplication_batch(
        generation_batch=parts["generation_batch"],
        deduplicator_manifest=parts["deduplicator_manifest"],
        relations={pair: relation},
        confidences={pair: confidence},
    )
    _install_outputs(parts, parts["generation_batch"], deduplication)

    campaign = await _run(parts, "campaign:f9s2:unresolved-semantic-pair")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_DUPLICATES
    assert f"semantic_pair_unresolved:{pair[0]}:{pair[1]}" in campaign.blockers
    assert campaign.world_model_snapshot is None


@pytest.mark.asyncio
async def test_exact_normalized_duplicate_overrides_inconsistent_reviewer(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    alternative = next(
        item
        for item in parts["generation_batch"].hypotheses
        if item.local_hypothesis_id == "alternative_a"
    )
    batch = _replace_hypothesis(
        parts["generation_batch"],
        "primary_a",
        statement=alternative.statement.upper(),
        mechanism=alternative.mechanism.upper(),
    )
    _install_outputs(parts, batch)

    campaign = await _run(parts, "campaign:f9s2:exact-duplicate-contradiction")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_DUPLICATES
    assert any(
        item.startswith("deduplicator_contradicts_exact_duplicate:")
        for item in campaign.blockers
    )


@pytest.mark.parametrize("case", ["unknown", "missing_prior", "descriptive_question"])
@pytest.mark.asyncio
async def test_grounding_failures_never_emit_world_model(direction_gates, case) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    batch = parts["generation_batch"]
    alternative = next(
        item for item in batch.hypotheses if item.local_hypothesis_id == "alternative_a"
    )
    candidate_sha256 = parts["request"].candidate_claim_sha256
    if case == "unknown":
        batch = _replace_hypothesis(
            batch,
            "alternative_a",
            grounding_claim_sha256s=(digest("f9s2:unknown-grounding"),),
        )
    elif case == "missing_prior":
        assumption = revalidate(
            e.AssumptionDraft,
            alternative.assumptions[0],
            grounding_claim_sha256s=(candidate_sha256,),
        )
        batch = _replace_hypothesis(
            batch,
            "alternative_a",
            grounding_claim_sha256s=(candidate_sha256,),
            assumptions=(assumption,),
        )
    else:
        batch = revalidate(
            e.HypothesisGenerationBatch,
            batch,
            question=e.QuestionDraft(
                statement=batch.question.statement,
                kind=e.ResearchQuestionKind.DESCRIPTIVE,
            ),
        )
    _install_outputs(parts, batch)

    campaign = await _run(parts, f"campaign:f9s2:grounding:{case}")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_GROUNDING
    assert campaign.blockers
    assert campaign.world_model_snapshot is None


@pytest.mark.asyncio
async def test_every_kept_pair_requires_bidirectional_same_protocol_predictions(
    direction_gates,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    primary = next(
        item
        for item in parts["generation_batch"].hypotheses
        if item.local_hypothesis_id == "primary_a"
    )
    changed_prediction = revalidate(
        e.PredictionDraft,
        primary.predictions[0],
        measurement_protocol_sha256=digest("f9s2:different-protocol"),
    )
    batch = _replace_hypothesis(
        parts["generation_batch"],
        "primary_a",
        predictions=(changed_prediction,),
    )
    _install_outputs(parts, batch)

    campaign = await _run(parts, "campaign:f9s2:non-discriminating")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_DISCRIMINATION
    assert len(campaign.discrimination_edges) == 1
    assert any(
        item.startswith("no_pairwise_discriminating_prediction:")
        for item in campaign.blockers
    )
    assert campaign.world_model_snapshot is None


@pytest.mark.parametrize("mutation", ["missing_pair", "reordered_pairs"])
@pytest.mark.asyncio
async def test_incomplete_or_noncanonical_deduplication_output_is_sanitized_failure(
    direction_gates,
    mutation,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    raw = parts["deduplication_batch"].model_dump(mode="python")
    judgments = list(raw["judgments"])
    raw["judgments"] = judgments[:-1] if mutation == "missing_pair" else list(reversed(judgments))
    parts["deduplicator"] = StaticDeduplicator(parts["deduplicator_manifest"], raw)

    campaign = await _run(parts, f"campaign:f9s2:invalid-dedup:{mutation}")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_GENERATION
    assert campaign.failure.kind is e.HypothesisGenerationFailureKind.DEDUPLICATOR_OUTPUT_INVALID
    assert campaign.failure.raw_output_sha256 is not None
    assert campaign.generation_batch == parts["generation_batch"]
    assert campaign.deduplication_batch is None


@pytest.mark.parametrize("stage", ["generation", "deduplication"])
@pytest.mark.asyncio
async def test_adapter_cannot_backdate_harness_with_future_completion_timestamp(
    direction_gates,
    stage,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    future = parts["clock"].current + timedelta(hours=1)
    if stage == "generation":
        batch = revalidate(
            e.HypothesisGenerationBatch,
            parts["generation_batch"],
            completed_at=future,
        )
        parts["generator"] = StaticGenerator(parts["generator_manifest"], batch)
        expected = e.HypothesisGenerationFailureKind.GENERATOR_OUTPUT_INVALID
    else:
        deduplication = revalidate(
            e.HypothesisDeduplicationBatch,
            parts["deduplication_batch"],
            completed_at=future,
        )
        parts["deduplicator"] = StaticDeduplicator(
            parts["deduplicator_manifest"], deduplication
        )
        expected = e.HypothesisGenerationFailureKind.DEDUPLICATOR_OUTPUT_INVALID

    campaign = await _run(parts, f"campaign:f9s2:future-time:{stage}")

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_GENERATION
    assert campaign.failure.kind is expected
    assert campaign.world_model_snapshot is None


@pytest.mark.parametrize("stage", ["generator_exception", "invalid_generator", "deduplicator_exception"])
@pytest.mark.asyncio
async def test_adapter_failures_are_hash_only_and_never_leak_raw_details(
    direction_gates,
    stage,
) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    secret = f"do-not-retain-secret-{stage}"
    if stage == "generator_exception":
        parts["generator"] = StaticGenerator(parts["generator_manifest"], RuntimeError(secret))
    elif stage == "invalid_generator":
        parts["generator"] = StaticGenerator(
            parts["generator_manifest"], {"untrusted_raw_output": secret}
        )
    else:
        parts["deduplicator"] = StaticDeduplicator(
            parts["deduplicator_manifest"], RuntimeError(secret)
        )

    campaign = await _run(parts, f"campaign:f9s2:failure:{stage}")
    serialized = campaign.model_dump_json()

    assert campaign.disposition is e.HypothesisGenerationDisposition.BLOCKED_GENERATION
    assert campaign.failure is not None
    assert len(campaign.failure.error_detail_sha256) == 64
    assert secret not in serialized
    assert campaign.world_model_snapshot is None
    if stage.startswith("generator"):
        assert parts["deduplicator"].calls == 0


@pytest.mark.asyncio
async def test_campaign_decision_and_snapshot_cannot_be_forged(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    campaign = await _run(parts, "campaign:f9s2:unforgeable")
    raw = campaign.model_dump(mode="python")
    raw.update(
        disposition=e.HypothesisGenerationDisposition.BLOCKED_GROUNDING,
        blockers=("caller_asserted_blocker",),
        world_model_snapshot=None,
    )

    with pytest.raises(ValidationError, match="not mechanically derived"):
        e.HypothesisGenerationCampaign.model_validate(raw)


@pytest.mark.asyncio
async def test_failed_campaign_revalidates_its_retained_generation_batch(direction_gates) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    parts["deduplicator"] = StaticDeduplicator(
        parts["deduplicator_manifest"], RuntimeError("review transport stopped")
    )
    campaign = await _run(parts, "campaign:f9s2:failed-retained-batch")
    raw = campaign.model_dump(mode="python")
    raw["generation_batch"]["request_sha256"] = digest("f9s2:another-request")

    with pytest.raises(ValidationError, match="bound to another request/generator"):
        e.HypothesisGenerationCampaign.model_validate(raw)


@pytest.mark.asyncio
async def test_campaign_archive_round_trip_and_tamper_detection(direction_gates, tmp_path) -> None:
    parts = build_f9s2_fixture(direction_gates["strong"])
    campaign = await _run(parts, "campaign:f9s2:archive")
    archive = k.ContentAddressedResponseArchive(tmp_path / "f9s2-campaign-archive")

    committed = e.commit_hypothesis_generation_campaign(archive=archive, campaign=campaign)
    loaded = e.load_hypothesis_generation_campaign(archive=archive, ledger=committed.ledger)

    assert loaded == campaign
    target = archive.root / committed.ledger.relative_path
    target.chmod(0o600)
    target.write_bytes(b"tampered campaign")
    with pytest.raises(k.ResponseArchiveCorruption):
        e.load_hypothesis_generation_campaign(archive=archive, ledger=committed.ledger)


@pytest.mark.asyncio
async def test_only_ready_campaign_can_persist_exact_f9s1_snapshot(direction_gates) -> None:
    create_all()
    run_id = create_run(goal="F9-S2 generated competing world model")
    ready_parts = build_f9s2_fixture(direction_gates["strong"], run_id=run_id)
    ready = await _run(ready_parts, "campaign:f9s2:persistence-ready")

    receipt = e.persist_ready_world_model(ready)
    loaded = e.get_world_model_snapshot(receipt.snapshot_sha256)

    assert loaded == ready.world_model_snapshot
    blocked_parts = build_f9s2_fixture(direction_gates["strong"])
    blocked_parts["generator"] = StaticGenerator(
        blocked_parts["generator_manifest"], RuntimeError("generator unavailable")
    )
    blocked = await _run(blocked_parts, "campaign:f9s2:persistence-blocked")
    with pytest.raises(ValueError, match="only a ready"):
        e.persist_ready_world_model(blocked)
