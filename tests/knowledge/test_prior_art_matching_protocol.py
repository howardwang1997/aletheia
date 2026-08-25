from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s4_fixtures import build_executor, build_f8s4_fixture


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


@pytest.mark.asyncio
async def test_protocol_is_bound_to_exact_reviewed_graph_and_prior_pool() -> None:
    fixture = await build_f8s4_fixture()
    protocol = fixture["protocol"]
    graph = fixture["graph_bundle"].graph

    assert protocol.claim_graph_bundle_sha256 == fixture["graph_bundle"].bundle_sha256
    assert protocol.claim_graph_sha256 == graph.graph_sha256
    assert tuple(target.ordinal for target in protocol.targets) == (0,)
    assert protocol.targets[0].candidate_claim_sha256 == fixture["candidate"].claim_sha256
    assert protocol.prior_claim_sha256s == tuple(
        sorted(claim.claim_sha256 for claim in fixture["prior_claims"])
    )
    assert protocol.required_channels == tuple(k.PriorArtRecallChannel)
    assert protocol.minimum_relation_channels == 2


@pytest.mark.asyncio
async def test_protocol_requires_all_four_recall_channels_in_canonical_order() -> None:
    fixture = await build_f8s4_fixture()
    manifests = fixture["recall_manifests"]

    with pytest.raises(ValidationError, match="exact required-channel order"):
        _revalidate(
            k.PriorArtMatchingProtocol,
            fixture["protocol"],
            recall_manifests=(manifests[1], manifests[0], *manifests[2:]),
        )


@pytest.mark.asyncio
async def test_embedding_recall_manifest_requires_frozen_model_identity() -> None:
    fixture = await build_f8s4_fixture()
    embedding = fixture["recall_manifests"][1]

    with pytest.raises(ValidationError, match="requires a frozen model identity"):
        _revalidate(
            k.RecallChannelManifest,
            embedding,
            model_identity_sha256=None,
        )


@pytest.mark.asyncio
async def test_recall_and_matcher_boundaries_have_no_tool_authority() -> None:
    fixture = await build_f8s4_fixture()

    assert all(not manifest.tool_names for manifest in fixture["recall_manifests"])
    assert fixture["matcher_manifest"].tool_names == ()
    with pytest.raises(ValidationError, match="cannot receive tool authority"):
        _revalidate(
            k.PriorArtMatcherManifest,
            fixture["matcher_manifest"],
            tool_names=("web_search",),
        )


@pytest.mark.asyncio
async def test_matcher_manifest_freezes_complete_relation_and_difference_schema() -> None:
    fixture = await build_f8s4_fixture()
    manifest = fixture["matcher_manifest"]

    assert manifest.judgment_schema_sha256 == k.PRIOR_ART_JUDGMENT_SCHEMA_SHA256
    assert manifest.supported_relations == tuple(k.PriorArtRelationType)
    assert manifest.supported_difference_components == tuple(k.DifferenceComponent)
    with pytest.raises(ValidationError, match="another judgment schema"):
        _revalidate(
            k.PriorArtMatcherManifest,
            manifest,
            judgment_schema_sha256=sha("wrong-prior-art-schema"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", tuple(k.PriorArtRelationType))
async def test_executor_preserves_all_six_prior_art_relation_types(
    relation: k.PriorArtRelationType,
) -> None:
    fixture = await build_f8s4_fixture()
    first_prior = fixture["prior_claims"][0]
    _, confidence, difference_confidence, component = fixture["matcher"].relation_specs[
        first_prior.claim_sha256
    ]
    fixture["matcher"].relation_specs[first_prior.claim_sha256] = (
        relation,
        confidence,
        difference_confidence,
        component,
    )

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"],
        execution_id=f"f8s4-relation-{relation.value}",
    )
    nearest = execution.relation_candidates[0]
    assert nearest.relation.relation is relation
    assert nearest.relation.blocks_strong_novelty is (
        relation
        in {
            k.PriorArtRelationType.EQUIVALENT,
            k.PriorArtRelationType.SUBSUMES,
            k.PriorArtRelationType.SPECIAL_CASE,
        }
    )
    assert bool(nearest.relation.differences) is (relation is not k.PriorArtRelationType.EQUIVALENT)


def test_recall_result_rejects_noncanonical_score_order() -> None:
    channel = k.PriorArtRecallChannel.LEXICAL
    with pytest.raises(ValidationError, match="canonical score/hash order"):
        k.PriorArtRecallResult(
            query_sha256=sha("query"),
            channel=channel,
            hits=(
                k.PriorArtRecallHit(
                    prior_claim_sha256=sha("lower"),
                    channel=channel,
                    rank=1,
                    score=0.2,
                    score_evidence_sha256=sha("lower-score"),
                ),
                k.PriorArtRecallHit(
                    prior_claim_sha256=sha("higher"),
                    channel=channel,
                    rank=2,
                    score=0.9,
                    score_evidence_sha256=sha("higher-score"),
                ),
            ),
            examined_prior_claims=2,
            exhaustive=True,
            truncated=False,
        )
