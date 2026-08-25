from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s3_fixtures import build_f8s3_fixture, sha


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


def test_frozen_claim_protocol_is_deterministic_and_closes_every_span() -> None:
    first = build_f8s3_fixture()
    second = build_f8s3_fixture()
    protocol = first["protocol"]

    assert protocol.protocol_sha256 == second["protocol"].protocol_sha256
    assert protocol.output_schema_sha256 == k.CLAIM_OUTPUT_SCHEMA_SHA256
    assert protocol.content_normalizer_sha256 == k.CANONICAL_TEXT_NORMALIZER_SHA256
    assert [target.ordinal for target in protocol.targets] == [0, 1, 2]
    assert {target.source_span_sha256 for target in protocol.targets} == {
        span.span_sha256 for span in first["spans"].values()
    }
    assert protocol.required_evidence_relations == tuple(k.ClaimEvidenceRelation)
    assert protocol.review_kinds == tuple(k.ClaimReviewKind)


def test_extractor_manifests_freeze_schema_model_and_zero_tool_authority() -> None:
    fixture = build_f8s3_fixture()
    model_manifest, deterministic_manifest = fixture["manifests"]

    assert model_manifest.runtime is k.ClaimExtractorRuntime.MODEL
    assert model_manifest.instruction_sha256
    assert model_manifest.model_identity_sha256
    assert model_manifest.tool_names == ()
    assert model_manifest.tool_policy == "none"
    assert deterministic_manifest.runtime is k.ClaimExtractorRuntime.DETERMINISTIC

    with pytest.raises(ValidationError, match="tool authority"):
        _revalidate(k.ClaimExtractorManifest, model_manifest, tool_names=("shell",))
    with pytest.raises(ValidationError, match="exact structured schema"):
        _revalidate(
            k.ClaimExtractorManifest,
            model_manifest,
            output_schema_sha256=sha("forged-output-schema"),
        )
    with pytest.raises(ValidationError, match="requires frozen instruction/model"):
        _revalidate(k.ClaimExtractorManifest, model_manifest, instruction_sha256=None)
    with pytest.raises(ValidationError, match="cannot declare a model"):
        _revalidate(
            k.ClaimExtractorManifest,
            deterministic_manifest,
            model_identity_sha256=sha("smuggled-model"),
        )


def test_protocol_rejects_skipped_duplicate_and_unknown_targets() -> None:
    protocol = build_f8s3_fixture()["protocol"]
    first = protocol.targets[0]

    with pytest.raises(ValidationError, match="contiguous ordinals"):
        _revalidate(
            k.ClaimExtractionProtocol,
            protocol,
            targets=(first, _revalidate(k.ClaimExtractionTarget, protocol.targets[1], ordinal=2)),
        )
    with pytest.raises(ValidationError, match="only once"):
        _revalidate(
            k.ClaimExtractionProtocol,
            protocol,
            targets=(
                first,
                _revalidate(
                    k.ClaimExtractionTarget,
                    protocol.targets[1],
                    source_span_sha256=first.source_span_sha256,
                ),
                protocol.targets[2],
            ),
        )
    with pytest.raises(ValidationError, match="unknown extractor"):
        _revalidate(
            k.ClaimExtractionProtocol,
            protocol,
            targets=(
                _revalidate(
                    k.ClaimExtractionTarget,
                    first,
                    extractor_manifest_sha256=sha("unknown-extractor"),
                ),
                *protocol.targets[1:],
            ),
        )


def test_protocol_cannot_drop_refuting_or_qualifying_evidence_relations() -> None:
    protocol = build_f8s3_fixture()["protocol"]

    with pytest.raises(ValidationError, match="preserve every evidence relation"):
        _revalidate(
            k.ClaimExtractionProtocol,
            protocol,
            required_evidence_relations=(k.ClaimEvidenceRelation.SUPPORTS,),
        )
    with pytest.raises(ValidationError, match="human or independent second model"):
        _revalidate(
            k.ClaimExtractionProtocol,
            protocol,
            review_kinds=(k.ClaimReviewKind.HUMAN,),
        )


def test_structured_output_has_no_quote_tool_or_authority_fields() -> None:
    forbidden = {"source_text", "evidence_quote", "tool_authority", "tool_calls"}

    assert forbidden.isdisjoint(k.StructuredClaimDraft.model_fields)
    assert forbidden.isdisjoint(k.StructuredClaimBatch.model_fields)
    with pytest.raises(ValidationError, match="Extra inputs"):
        k.StructuredClaimDraft.model_validate(
            {
                **next(iter(build_f8s3_fixture()["drafts"].values()))[0],
                "tool_authority": "elevated",
            }
        )


def test_quantitative_draft_requires_explicit_separate_grounding_confidence() -> None:
    draft = next(iter(build_f8s3_fixture()["drafts"].values()))[0]

    with pytest.raises(ValidationError, match="separate grounding confidence"):
        k.StructuredClaimDraft.model_validate({**draft, "quantitative_grounding_confidence": None})
    with pytest.raises(ValidationError, match="Field required"):
        k.StructuredClaimDraft.model_validate(
            {
                **draft,
                "quantitative_effect": {
                    key: value
                    for key, value in draft["quantitative_effect"].items()
                    if key != "unit"
                },
            }
        )
