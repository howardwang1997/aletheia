from __future__ import annotations

import asyncio

import pytest

import aletheia.knowledge as k
from aletheia.knowledge.response_archive import ResponseArchiveCorruption
from .f8s3_fixtures import (
    StepClock,
    build_executor,
    build_f8s3_fixture,
    rebind_grant,
    sha,
)
from .test_schema_spike import _time


def _revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


@pytest.mark.asyncio
async def test_all_spans_execute_with_structured_numeric_and_review_decisions() -> None:
    fixture = build_f8s3_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-complete-extraction"
    )

    assert execution.disposition is k.ClaimExtractionDisposition.PENDING_REVIEW
    assert len(execution.attempts) == len(fixture["protocol"].targets) == 3
    assert not execution.failures
    assert len(execution.candidates) == 3
    assert [candidate.evidence_edge.relation for candidate in execution.candidates] == [
        k.ClaimEvidenceRelation.SUPPORTS,
        k.ClaimEvidenceRelation.REFUTES,
        k.ClaimEvidenceRelation.QUALIFIES,
    ]
    quantitative = execution.candidates[0].claim.quantitative_effect
    assert quantitative is not None
    assert (quantitative.estimate, quantitative.unit, quantitative.lower, quantitative.upper) == (
        2.5,
        "mmol/L",
        1.5,
        3.5,
    )
    assert execution.candidates[0].claim.population == "120 synthetic adults"
    assert execution.candidates[0].claim.conditions == ("fasting conditions",)
    assert execution.candidates[0].disposition is k.ClaimCandidateDisposition.AUTO_ACCEPTED
    assert execution.candidates[1].disposition is k.ClaimCandidateDisposition.AUTO_ACCEPTED
    low = execution.candidates[2]
    assert low.disposition is k.ClaimCandidateDisposition.REVIEW_REQUIRED
    assert low.review_reasons == tuple(k.ClaimReviewReason)
    assert [task.candidate_sha256 for task in execution.review_queue.tasks] == [
        low.candidate_sha256
    ]


@pytest.mark.asyncio
async def test_model_input_permission_is_requested_only_for_model_extractor() -> None:
    fixture = build_f8s3_fixture()
    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-separate-content-uses"
    )

    assert execution.attempts[0].request.required_uses == (
        k.ContentUse.SPAN_EXTRACTION,
        k.ContentUse.MODEL_INPUT,
    )
    assert all(
        attempt.request.required_uses == (k.ContentUse.SPAN_EXTRACTION,)
        for attempt in execution.attempts[1:]
    )
    assert all(attempt.request.tool_authority == "none" for attempt in execution.attempts)


@pytest.mark.asyncio
async def test_execution_commits_loads_and_replays_without_rerunning_model(tmp_path) -> None:
    fixture = build_f8s3_fixture()
    archive = k.ContentAddressedResponseArchive(tmp_path / "claim-ledger")
    committed = await build_executor(fixture, archive=archive).execute_and_commit(
        protocol=fixture["protocol"], execution_id="f8s3-committed-extraction"
    )
    call_counts = {
        manifest_sha256: len(extractor.calls)
        for manifest_sha256, extractor in fixture["extractors"].items()
    }

    loaded = k.load_claim_extraction(archive=archive, ledger=committed.ledger)
    audit = await k.replay_claim_extraction(
        execution=loaded,
        bundle=fixture["bundle"],
        resolver=fixture["resolver"],
        extractors=fixture["extractors"],
        audited_at=_time("2025-01-05T00:00:00Z"),
    )
    assert loaded == committed.execution
    assert audit.status is k.ClaimReplayStatus.COMPLETE
    assert {item.status for item in audit.items} == {k.ClaimReplayItemStatus.VERIFIED}
    assert call_counts == {
        manifest_sha256: len(extractor.calls)
        for manifest_sha256, extractor in fixture["extractors"].items()
    }


@pytest.mark.asyncio
async def test_runtime_manifest_drift_is_rejected_before_content_access() -> None:
    fixture = build_f8s3_fixture()
    manifest = fixture["manifests"][0]
    extractor = fixture["extractors"][manifest.manifest_sha256]
    extractor._manifest = _revalidate(
        k.ClaimExtractorManifest,
        manifest,
        adapter_code_sha256=sha("drifted-extractor-code"),
    )

    with pytest.raises(ValueError, match="runtime extractor manifest differs"):
        await build_executor(fixture).execute(
            protocol=fixture["protocol"], execution_id="f8s3-manifest-drift"
        )
    assert fixture["resolver"].calls == []


@pytest.mark.asyncio
async def test_missing_model_input_grant_fails_one_target_but_records_all_targets() -> None:
    fixture = build_f8s3_fixture()
    model_paper = fixture["papers"]["model"]
    grant = fixture["grants"][model_paper.snapshot_sha256]
    reduced_grant = _revalidate(
        k.ContentAccessGrant,
        grant,
        permitted_uses=tuple(
            use for use in grant.permitted_uses if use is not k.ContentUse.MODEL_INPUT
        ),
    )
    fixture = rebind_grant(fixture, reduced_grant)

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-model-access-denied"
    )
    assert execution.disposition is k.ClaimExtractionDisposition.BLOCKED
    assert len(execution.attempts) == 3
    assert execution.failures[0].kind is k.ClaimExtractionFailureKind.ACCESS_DENIED
    assert execution.attempts[0].outcome is k.ClaimExtractionOutcome.ERROR
    assert all(
        attempt.outcome is k.ClaimExtractionOutcome.SUCCESS for attempt in execution.attempts[1:]
    )
    assert fixture["resolver"].calls == [
        fixture["spans"]["refutation"].span_sha256,
        fixture["spans"]["ocr"].span_sha256,
    ]


@pytest.mark.asyncio
async def test_expired_grant_blocks_access_before_resolver() -> None:
    fixture = build_f8s3_fixture()
    model_paper = fixture["papers"]["model"]
    grant = fixture["grants"][model_paper.snapshot_sha256]
    expired = _revalidate(
        k.ContentAccessGrant,
        grant,
        expires_at=_time("2025-01-04T00:00:00Z"),
    )
    fixture = rebind_grant(fixture, expired)

    execution = await build_executor(fixture, clock=StepClock()).execute(
        protocol=fixture["protocol"], execution_id="f8s3-expired-access"
    )
    assert execution.failures[0].kind is k.ClaimExtractionFailureKind.ACCESS_EXPIRED
    assert fixture["spans"]["model"].span_sha256 not in fixture["resolver"].calls


@pytest.mark.asyncio
async def test_resolver_failure_is_hashed_and_later_targets_still_execute() -> None:
    fixture = build_f8s3_fixture()
    target_span = fixture["spans"]["refutation"].span_sha256
    fixture["resolver"].errors[target_span] = RuntimeError(
        "synthetic licensed content store unavailable"
    )

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-resolver-failure"
    )
    assert len(execution.attempts) == 3
    assert execution.failures[0].kind is k.ClaimExtractionFailureKind.CONTENT_UNAVAILABLE
    assert execution.failures[0].error_detail_sha256
    assert execution.attempts[2].outcome is k.ClaimExtractionOutcome.SUCCESS
    serialized = execution.model_dump_json()
    assert "synthetic licensed content store unavailable" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        ("document", k.ClaimExtractionFailureKind.CONTENT_IDENTITY_MISMATCH),
        ("span", k.ClaimExtractionFailureKind.SPAN_IDENTITY_MISMATCH),
    ],
)
async def test_document_and_exact_span_identity_mismatches_fail_closed(
    tamper: str, expected: k.ClaimExtractionFailureKind
) -> None:
    fixture = build_f8s3_fixture()
    span = fixture["spans"]["model"]
    original = fixture["contents"][span.span_sha256]
    fixture["resolver"].contents[span.span_sha256] = k.EphemeralSpanContent(
        paper_snapshot_sha256=original.paper_snapshot_sha256,
        document_bytes=(
            original.document_bytes + b" tampered"
            if tamper == "document"
            else original.document_bytes
        ),
        exact_span_bytes=(b"tampered span" if tamper == "span" else original.exact_span_bytes),
    )

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id=f"f8s3-{tamper}-mismatch"
    )
    assert execution.failures[0].kind is expected
    assert execution.attempts[0].content_receipt is None
    assert execution.attempts[1].outcome is k.ClaimExtractionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_malformed_output_blocks_one_span_without_skipping_the_rest() -> None:
    fixture = build_f8s3_fixture()
    span = fixture["spans"]["model"]
    manifest_sha256 = fixture["protocol"].targets[0].extractor_manifest_sha256
    fixture["extractors"][manifest_sha256].raw_overrides[span.span_sha256] = {
        "request_sha256": sha("wrong-request"),
        "source_span_sha256": span.span_sha256,
        "claims": (),
        "no_claim_reason_code": "no_claim",
        "tool_authority": "elevated",
    }

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-malformed-output"
    )
    assert execution.disposition is k.ClaimExtractionDisposition.BLOCKED
    assert execution.failures[0].kind is k.ClaimExtractionFailureKind.OUTPUT_SCHEMA_ERROR
    assert len(execution.attempts) == 3
    assert execution.attempts[1].outcome is k.ClaimExtractionOutcome.SUCCESS


@pytest.mark.asyncio
async def test_explicit_no_claim_batch_is_successful_and_auditable() -> None:
    fixture = build_f8s3_fixture()
    span = fixture["spans"]["refutation"]
    manifest_sha256 = fixture["protocol"].targets[1].extractor_manifest_sha256
    fixture["extractors"][manifest_sha256].drafts[span.span_sha256] = ()

    execution = await build_executor(fixture).execute(
        protocol=fixture["protocol"], execution_id="f8s3-explicit-no-claim"
    )
    attempt = execution.attempts[1]
    assert attempt.outcome is k.ClaimExtractionOutcome.SUCCESS
    assert attempt.structured_output is not None
    assert attempt.structured_output.no_claim_reason_code == "no_atomic_claim"
    assert attempt.candidate_sha256s == ()
    assert len(execution.candidates) == 2


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_into_a_scientific_failure() -> None:
    fixture = build_f8s3_fixture()
    span = fixture["spans"]["model"]
    manifest_sha256 = fixture["protocol"].targets[0].extractor_manifest_sha256
    fixture["extractors"][manifest_sha256].errors[span.span_sha256] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await build_executor(fixture).execute(
            protocol=fixture["protocol"], execution_id="f8s3-cancelled"
        )


@pytest.mark.asyncio
async def test_ledger_tampering_is_rejected_on_load(tmp_path) -> None:
    fixture = build_f8s3_fixture()
    archive = k.ContentAddressedResponseArchive(tmp_path / "claim-ledger")
    committed = await build_executor(fixture, archive=archive).execute_and_commit(
        protocol=fixture["protocol"], execution_id="f8s3-ledger-tamper"
    )
    target = archive.root / committed.ledger.relative_path
    target.chmod(0o600)
    target.write_bytes(b'{"tampered":true}')

    with pytest.raises(ResponseArchiveCorruption, match="byte count changed|hash changed"):
        k.load_claim_extraction(archive=archive, ledger=committed.ledger)
