from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from aletheia.research.sota_claims import (
    SOTAWriteupDisposition,
    screen_auditable_sota_campaign,
)
from aletheia.scheduler.driver import ExperimentDriver
from aletheia.knowledge.sota_evaluation import _one_sided_sign_p_value
from .f8s3_fixtures import sha
from .f8s5_fixtures import build_f8s5_direction_fixture
from .f8s6_fixtures import (
    RESULT_RECEIPT_KEY,
    build_f8s6_fixture,
    build_protocol,
    issue_result,
)
from .test_schema_spike import _time


@pytest.fixture(scope="module")
def f8s6(tmp_path_factory):
    return build_f8s6_fixture(tmp_path_factory.mktemp("f8s6"))


def _revalidate(model_type, model, **updates):
    raw = model.model_dump(mode="python")
    raw.update(updates)
    return model_type.model_validate(raw)


def _build_campaign(f8s6, **updates):
    values = {
        "campaign_id": "f8s6-test-campaign",
        "direction_gate": f8s6["gate"],
        "registry": f8s6["registry"],
        "policy": f8s6["policy"],
        "evaluator_manifest": f8s6["evaluator"],
        "candidate_protocol": f8s6["candidate_protocol"],
        "candidate_result": f8s6["candidate_result"],
        "reference_results": f8s6["reference_results"],
        "receipt_key": RESULT_RECEIPT_KEY,
        "generated_at": _time("2025-08-13T00:00:00Z"),
    }
    values.update(updates)
    return k.build_sota_evaluation_campaign(**values)


def _candidate_protocol(f8s6, **updates):
    raw = f8s6["candidate_protocol"].model_dump(mode="python")
    raw.update(updates)
    return k.ProtocolSignature.model_validate(raw)


def _candidate_result(f8s6, protocol, scores=None):
    return issue_result(
        result_id="f8s6-test-candidate-result",
        protocol=protocol,
        evaluator=f8s6["evaluator"],
        scores=scores
        or tuple(replicate.score for replicate in f8s6["candidate_result"].payload.replicates),
        method_id="test-candidate",
    )


def test_full_f8s5_to_f8s6_closure_confirms_every_sealed_reference(f8s6) -> None:
    campaign = f8s6["campaign"]

    assert campaign.direction_gate.experiment_authorized is True
    assert campaign.registry.direction_gate_sha256 == campaign.direction_gate.gate_sha256
    assert campaign.verdict is k.SOTACampaignVerdict.CONFIRMED
    assert campaign.headline_sota_allowed is True
    assert campaign.claim_ceiling is k.SOTAClaimCeiling.MODERATE
    assert len(campaign.rows) == 3
    assert all(row.conclusion is k.SOTARowConclusion.BEATS_REFERENCE for row in campaign.rows)
    assert all(row.wins == 10 and row.losses == 0 and row.ties == 0 for row in campaign.rows)
    assert all(row.one_sided_p_value == pytest.approx(1 / 1024) for row in campaign.rows)
    assert all(row.holm_adjusted_p_value == pytest.approx(3 / 1024) for row in campaign.rows)


def test_evaluation_date_difference_is_disclosed_but_not_blocking(f8s6) -> None:
    row = f8s6["campaign"].rows[0]

    assert row.comparison.comparability.status is k.ComparabilityStatus.COMPATIBLE
    assert tuple(
        (mismatch.dimension, mismatch.blocking)
        for mismatch in row.comparison.comparability.mismatches
    ) == ((k.ProtocolDimension.EVALUATION_DATE, False),)


def test_reference_registry_requires_at_least_three_presealed_references(f8s6) -> None:
    raw = f8s6["registry"].model_dump(mode="python")
    raw["references"] = raw["references"][:2]

    with pytest.raises(ValidationError, match="at least 3 items"):
        k.SOTAReferenceRegistry.model_validate(raw)


def test_candidate_authors_cannot_select_or_review_references(f8s6) -> None:
    author = f8s6["registry"].candidate_author_principal_sha256s[0]
    raw = f8s6["registry"].model_dump(mode="python")
    raw["selector_reviewer_principal_sha256s"] = tuple(sorted((author, sha("f8s6:other-selector"))))
    with pytest.raises(ValidationError, match="cannot select"):
        k.SOTAReferenceRegistry.model_validate(raw)

    references = list(f8s6["references"])
    references[0] = _revalidate(
        k.SOTAReferenceEntry,
        references[0],
        independent_reviewer_principal_sha256=author,
    )
    raw = f8s6["registry"].model_dump(mode="python")
    raw["references"] = tuple(references)
    with pytest.raises(ValidationError, match="cannot review"):
        k.SOTAReferenceRegistry.model_validate(raw)


def test_future_reference_is_rejected_from_the_registry(f8s6) -> None:
    protocol = _revalidate(
        k.ProtocolSignature,
        f8s6["reference_protocols"][0],
        evaluation_date=_time("2025-08-10T02:30:00Z"),
    )
    reference = _revalidate(
        k.SOTAReferenceEntry,
        f8s6["references"][0],
        protocol=protocol,
        selected_at=_time("2025-08-10T02:45:00Z"),
    )
    raw = f8s6["registry"].model_dump(mode="python")
    references = list(raw["references"])
    references[0] = reference
    raw["references"] = tuple(references)

    with pytest.raises(ValidationError, match="future or post-sealing"):
        k.SOTAReferenceRegistry.model_validate(raw)


def test_reference_paper_and_result_spans_must_close_into_the_bound_corpus(f8s6) -> None:
    references = list(f8s6["references"])
    references[0] = _revalidate(
        k.SOTAReferenceEntry,
        references[0],
        source_paper_snapshot_sha256=sha("f8s6:outside-corpus-paper"),
    )
    with pytest.raises(ValueError, match="source paper lies outside"):
        k.build_sota_reference_registry(
            registry_id="f8s6-outside-paper-registry",
            direction_gate=f8s6["gate"],
            selection_protocol_sha256=sha("f8s6:outside-paper-policy"),
            selector_reviewer_principal_sha256s=tuple(
                sorted((sha("f8s6:selector:a"), sha("f8s6:selector:b")))
            ),
            references=tuple(references),
            evidence_cutoff=_time("2025-08-10T02:00:00Z"),
            sealed_at=_time("2025-08-10T03:00:00Z"),
        )

    references = list(f8s6["references"])
    references[0] = _revalidate(
        k.SOTAReferenceEntry,
        references[0],
        result_evidence_span_sha256s=(sha("f8s6:outside-paper-span"),),
    )
    with pytest.raises(ValueError, match="span lies outside"):
        k.build_sota_reference_registry(
            registry_id="f8s6-outside-span-registry",
            direction_gate=f8s6["gate"],
            selection_protocol_sha256=sha("f8s6:outside-span-policy"),
            selector_reviewer_principal_sha256s=tuple(
                sorted((sha("f8s6:selector:a"), sha("f8s6:selector:b")))
            ),
            references=tuple(references),
            evidence_cutoff=_time("2025-08-10T02:00:00Z"),
            sealed_at=_time("2025-08-10T03:00:00Z"),
        )


def test_evaluator_is_exact_frozen_and_has_no_tool_authority(f8s6) -> None:
    with pytest.raises(ValidationError, match="tool authority"):
        _revalidate(
            k.SOTAEvaluatorManifest,
            f8s6["evaluator"],
            tool_names=("web_search",),
        )

    with pytest.raises(ValidationError, match="another aggregation/statistical policy"):
        _revalidate(
            k.SOTAEvaluatorManifest,
            f8s6["evaluator"],
            statistical_policy_sha256=sha("f8s6:alternate-statistics"),
        )


def test_receipt_issuance_enforces_key_replicate_and_aggregation_floors(f8s6) -> None:
    replicates = f8s6["candidate_result"].payload.replicates
    with pytest.raises(ValueError, match="at least 32 bytes"):
        k.issue_benchmark_result_receipt(
            result_id="short-key",
            protocol=f8s6["candidate_protocol"],
            replicates=replicates,
            evaluator_manifest=f8s6["evaluator"],
            receipt_key=b"short",
            completed_at=_time("2025-08-12T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="too few frozen replicates"):
        k.issue_benchmark_result_receipt(
            result_id="too-few-replicates",
            protocol=f8s6["candidate_protocol"],
            replicates=replicates[:9],
            evaluator_manifest=f8s6["evaluator"],
            receipt_key=RESULT_RECEIPT_KEY,
            completed_at=_time("2025-08-12T00:00:00Z"),
        )

    metric = _revalidate(
        k.MetricDefinition,
        f8s6["metric"],
        aggregation_sha256=sha("f8s6:alternate-aggregation"),
    )
    protocol = _candidate_protocol(f8s6, metric=metric)
    with pytest.raises(ValueError, match="another replicate aggregation"):
        k.issue_benchmark_result_receipt(
            result_id="wrong-aggregation",
            protocol=protocol,
            replicates=replicates,
            evaluator_manifest=f8s6["evaluator"],
            receipt_key=RESULT_RECEIPT_KEY,
            completed_at=_time("2025-08-12T00:00:00Z"),
        )


def test_failed_receipt_hashes_error_detail_without_leaking_raw_text(f8s6) -> None:
    secret = "raw-provider-token-must-not-enter-artifact"
    receipt = k.issue_failed_benchmark_result_receipt(
        result_id="f8s6-hashed-failure",
        protocol=f8s6["reference_protocols"][0],
        evaluator_manifest=f8s6["evaluator"],
        receipt_key=RESULT_RECEIPT_KEY,
        failure=RuntimeError(secret),
        completed_at=_time("2025-08-12T00:00:00Z"),
    )

    assert receipt.payload.outcome is k.BenchmarkResultOutcome.ERROR
    assert receipt.payload.replicates == ()
    assert receipt.payload.aggregate_score is None
    assert receipt.payload.failure_detail_sha256 is not None
    assert secret not in receipt.model_dump_json()


@pytest.mark.parametrize(
    ("dimension", "updates"),
    (
        ("split", {"split_content_sha256": sha("f8s6:changed-split")}),
        ("preprocessing", {"preprocessing_sha256": sha("f8s6:changed-preprocessing")}),
    ),
)
def test_blocking_protocol_mismatch_suppresses_every_headline(
    f8s6,
    dimension,
    updates,
) -> None:
    protocol = _candidate_protocol(f8s6, **updates)
    result = _candidate_result(f8s6, protocol)
    campaign = _build_campaign(
        f8s6,
        candidate_protocol=protocol,
        candidate_result=result,
    )

    assert campaign.verdict is k.SOTACampaignVerdict.BLOCKED_EVIDENCE
    assert campaign.headline_sota_allowed is False
    assert campaign.claim_ceiling is k.SOTAClaimCeiling.NONE
    assert all(row.conclusion is k.SOTARowConclusion.NON_COMPARABLE for row in campaign.rows)
    assert all(dimension in blocker for blocker in campaign.blockers)


def test_same_dataset_name_with_different_bytes_is_non_comparable(f8s6) -> None:
    dataset = _revalidate(
        k.DatasetVersion,
        f8s6["dataset"],
        content_sha256=sha("f8s6:changed-dataset-bytes"),
    )
    protocol = _candidate_protocol(f8s6, dataset=dataset)
    campaign = _build_campaign(
        f8s6,
        candidate_protocol=protocol,
        candidate_result=_candidate_result(f8s6, protocol),
    )

    assert campaign.verdict is k.SOTACampaignVerdict.BLOCKED_EVIDENCE
    assert all(
        row.comparison.comparability.mismatches[0].dimension is k.ProtocolDimension.DATASET
        for row in campaign.rows
    )


def test_metric_and_resource_budget_mismatches_are_non_comparable(f8s6) -> None:
    metric = _revalidate(
        k.MetricDefinition,
        f8s6["metric"],
        formula_sha256=sha("f8s6:changed-metric-formula"),
    )
    metric_protocol = _candidate_protocol(f8s6, metric=metric)
    metric_campaign = _build_campaign(
        f8s6,
        candidate_protocol=metric_protocol,
        candidate_result=_candidate_result(f8s6, metric_protocol),
    )
    assert all(
        any(
            mismatch.dimension is k.ProtocolDimension.METRIC
            for mismatch in row.comparison.comparability.mismatches
        )
        for row in metric_campaign.rows
    )

    budget = _revalidate(
        k.ResourceBudgetSignature,
        f8s6["budget"],
        maximum_cost_usd=101.0,
    )
    budget_protocol = _candidate_protocol(f8s6, resource_budget=budget)
    budget_campaign = _build_campaign(
        f8s6,
        candidate_protocol=budget_protocol,
        candidate_result=_candidate_result(f8s6, budget_protocol),
    )
    assert all(
        any(
            mismatch.dimension is k.ProtocolDimension.RESOURCE_BUDGET
            for mismatch in row.comparison.comparability.mismatches
        )
        for row in budget_campaign.rows
    )


def test_one_unbeaten_reference_blocks_the_global_sota_claim(f8s6) -> None:
    candidate_scores = tuple(
        replicate.score for replicate in f8s6["candidate_result"].payload.replicates
    )
    weaker_reference = issue_result(
        result_id="f8s6-reference-outperforms-candidate",
        protocol=f8s6["reference_protocols"][0],
        evaluator=f8s6["evaluator"],
        scores=tuple(score - 0.10 for score in candidate_scores),
        method_id="reference-outperforms-candidate",
    )
    results = (weaker_reference, *f8s6["reference_results"][1:])
    campaign = _build_campaign(f8s6, reference_results=results)

    assert campaign.rows[0].conclusion is k.SOTARowConclusion.DOES_NOT_BEAT_REFERENCE
    assert campaign.verdict is k.SOTACampaignVerdict.NOT_DEMONSTRATED
    assert campaign.headline_sota_allowed is False
    assert campaign.claim_ceiling is k.SOTAClaimCeiling.COMPARATIVE_ONLY


def test_holm_correction_blocks_three_nominally_significant_rows(f8s6) -> None:
    candidate_scores = (0.8,) * 12
    reference_scores = (0.9,) * 10 + (0.79,) * 2
    candidate = issue_result(
        result_id="f8s6-holm-candidate",
        protocol=f8s6["candidate_protocol"],
        evaluator=f8s6["evaluator"],
        scores=candidate_scores,
        method_id="holm-candidate",
    )
    references = tuple(
        issue_result(
            result_id=f"f8s6-holm-reference-{index}",
            protocol=protocol,
            evaluator=f8s6["evaluator"],
            scores=reference_scores,
            method_id=f"holm-reference-{index}",
        )
        for index, protocol in enumerate(f8s6["reference_protocols"], start=1)
    )
    campaign = _build_campaign(
        f8s6,
        candidate_result=candidate,
        reference_results=references,
    )

    assert all(row.one_sided_p_value == pytest.approx(79 / 4096) for row in campaign.rows)
    assert all(row.one_sided_p_value < 0.05 for row in campaign.rows)
    assert all(row.holm_adjusted_p_value == pytest.approx(237 / 4096) for row in campaign.rows)
    assert all(row.statistically_significant is False for row in campaign.rows)
    assert campaign.verdict is k.SOTACampaignVerdict.NOT_DEMONSTRATED


def test_exact_sign_tail_remains_defined_at_the_10k_repeat_schema_limit() -> None:
    p_value = _one_sided_sign_p_value(5000, 5000)

    assert 0.5 < p_value < 0.51


def test_practical_improvement_floor_blocks_tiny_but_consistent_gain(f8s6) -> None:
    candidate_scores = (0.8,) * 10
    candidate = issue_result(
        result_id="f8s6-small-effect-candidate",
        protocol=f8s6["candidate_protocol"],
        evaluator=f8s6["evaluator"],
        scores=candidate_scores,
        method_id="small-effect-candidate",
    )
    references = tuple(
        issue_result(
            result_id=f"f8s6-small-effect-reference-{index}",
            protocol=protocol,
            evaluator=f8s6["evaluator"],
            scores=(0.81,) * 10,
            method_id=f"small-effect-reference-{index}",
        )
        for index, protocol in enumerate(f8s6["reference_protocols"], start=1)
    )
    campaign = _build_campaign(
        f8s6,
        candidate_result=candidate,
        reference_results=references,
    )

    assert all(row.statistically_significant is True for row in campaign.rows)
    assert all(row.practically_significant is False for row in campaign.rows)
    assert campaign.verdict is k.SOTACampaignVerdict.NOT_DEMONSTRATED


def test_missing_or_reordered_reference_results_fail_closed(f8s6) -> None:
    with pytest.raises(ValueError, match="one result for every sealed reference"):
        _build_campaign(f8s6, reference_results=f8s6["reference_results"][:2])

    with pytest.raises(ValueError, match="another protocol/evaluator"):
        _build_campaign(
            f8s6,
            reference_results=tuple(reversed(f8s6["reference_results"])),
        )


def test_invalid_result_signature_fails_before_comparison(f8s6) -> None:
    tampered = f8s6["candidate_result"].model_copy(
        update={"hmac_sha256": sha("f8s6:tampered-signature")}
    )

    with pytest.raises(ValueError, match="signature is invalid"):
        _build_campaign(f8s6, candidate_result=tampered)


def test_explicit_evaluator_error_blocks_evidence_without_fabricated_score(f8s6) -> None:
    failed = k.issue_failed_benchmark_result_receipt(
        result_id="f8s6-reference-evaluator-error",
        protocol=f8s6["reference_protocols"][0],
        evaluator_manifest=f8s6["evaluator"],
        receipt_key=RESULT_RECEIPT_KEY,
        failure=TimeoutError("reference runner timed out"),
        completed_at=_time("2025-08-12T00:00:00Z"),
    )
    campaign = _build_campaign(
        f8s6,
        reference_results=(failed, *f8s6["reference_results"][1:]),
    )

    assert campaign.rows[0].conclusion is k.SOTARowConclusion.RESULT_ERROR
    assert campaign.rows[0].comparison is None
    assert campaign.verdict is k.SOTACampaignVerdict.BLOCKED_EVIDENCE
    assert campaign.claim_ceiling is k.SOTAClaimCeiling.NONE


def test_unpaired_partitions_and_reused_artifacts_are_rejected(f8s6) -> None:
    scores = tuple(replicate.score for replicate in f8s6["reference_results"][0].payload.replicates)
    unpaired = issue_result(
        result_id="f8s6-unpaired-reference",
        protocol=f8s6["reference_protocols"][0],
        evaluator=f8s6["evaluator"],
        scores=scores,
        method_id="unpaired-reference",
        partition_namespace="different-partitions",
    )
    with pytest.raises(ValueError, match="same paired frozen replicates"):
        _build_campaign(
            f8s6,
            reference_results=(unpaired, *f8s6["reference_results"][1:]),
        )

    reused = issue_result(
        result_id="f8s6-reused-artifact-reference",
        protocol=f8s6["reference_protocols"][0],
        evaluator=f8s6["evaluator"],
        scores=scores,
        method_id="candidate",
    )
    with pytest.raises(ValueError, match="cannot reuse execution/prediction artifacts"):
        _build_campaign(
            f8s6,
            reference_results=(reused, *f8s6["reference_results"][1:]),
        )


def test_scores_outside_metric_range_are_rejected_at_campaign_binding(f8s6) -> None:
    result = issue_result(
        result_id="f8s6-out-of-range-candidate",
        protocol=f8s6["candidate_protocol"],
        evaluator=f8s6["evaluator"],
        scores=(2.1,) * 10,
        method_id="out-of-range-candidate",
    )

    with pytest.raises(ValueError, match="outside the metric range"):
        _build_campaign(f8s6, candidate_result=result)


def test_campaign_rows_and_headline_cannot_be_forged(f8s6) -> None:
    raw = f8s6["campaign"].model_dump(mode="python")
    raw["rows"] = tuple(reversed(raw["rows"]))
    with pytest.raises(ValidationError, match="matrix rows are not derived"):
        k.SOTAEvaluationCampaign.model_validate(raw)

    raw = f8s6["campaign"].model_dump(mode="python")
    raw["headline_sota_allowed"] = False
    with pytest.raises(ValidationError, match="decision/headline is not mechanically derived"):
        k.SOTAEvaluationCampaign.model_validate(raw)


def test_campaign_archive_round_trip_reverifies_result_signatures(f8s6, tmp_path) -> None:
    archive = k.ContentAddressedResponseArchive(tmp_path / "sota-archive")
    committed = k.commit_sota_evaluation_campaign(
        archive=archive,
        campaign=f8s6["campaign"],
    )
    loaded = k.load_sota_evaluation_campaign(
        archive=archive,
        ledger=committed.ledger,
        receipt_key=RESULT_RECEIPT_KEY,
    )

    assert loaded == f8s6["campaign"]
    with pytest.raises(ValueError, match="signature is invalid"):
        k.load_sota_evaluation_campaign(
            archive=archive,
            ledger=committed.ledger,
            receipt_key=bytes.fromhex(sha("f8s6:wrong-archive-key")),
        )


def test_unauthorized_direction_cannot_open_a_sota_campaign(f8s6) -> None:
    blocked = build_f8s5_direction_fixture(
        f8s6["live"],
        roles=(),
        verdicts=(),
    )["gate"]

    with pytest.raises(ValueError, match="authorized research direction"):
        k.build_sota_reference_registry(
            registry_id="f8s6-blocked-direction-registry",
            direction_gate=blocked,
            selection_protocol_sha256=sha("f8s6:blocked-selection-policy"),
            selector_reviewer_principal_sha256s=tuple(
                sorted((sha("f8s6:selector:x"), sha("f8s6:selector:y")))
            ),
            references=f8s6["references"],
            evidence_cutoff=_time("2025-08-10T02:00:00Z"),
            sealed_at=_time("2025-08-10T03:00:00Z"),
        )


def test_higher_is_better_metric_is_direction_normalized(f8s6) -> None:
    metric = _revalidate(
        k.MetricDefinition,
        f8s6["metric"],
        direction=k.MetricDirection.HIGHER_IS_BETTER,
    )
    candidate_protocol = build_protocol(
        protocol_id="f8s6-higher-candidate",
        method_id="higher-candidate",
        dataset=f8s6["dataset"],
        metric=metric,
        resource_budget=f8s6["budget"],
        frozen_at=_time("2025-08-10T05:00:00Z"),
        evaluation_date=_time("2025-08-11T00:00:00Z"),
    )
    reference_protocols = tuple(
        build_protocol(
            protocol_id=f"f8s6-higher-reference-{index}",
            method_id=f"higher-reference-{index}",
            dataset=f8s6["dataset"],
            metric=metric,
            resource_budget=f8s6["budget"],
            frozen_at=_time("2025-08-08T00:00:00Z"),
            evaluation_date=_time("2025-08-09T00:00:00Z"),
        )
        for index in range(1, 4)
    )
    references = tuple(
        _revalidate(
            k.SOTAReferenceEntry,
            entry,
            protocol=protocol,
        )
        for entry, protocol in zip(f8s6["references"], reference_protocols, strict=True)
    )
    registry_raw = f8s6["registry"].model_dump(mode="python")
    registry_raw["references"] = references
    registry = k.SOTAReferenceRegistry.model_validate(registry_raw)
    candidate_result = issue_result(
        result_id="f8s6-higher-result-candidate",
        protocol=candidate_protocol,
        evaluator=f8s6["evaluator"],
        scores=(1.0,) * 10,
        method_id="higher-result-candidate",
    )
    reference_results = tuple(
        issue_result(
            result_id=f"f8s6-higher-result-reference-{index}",
            protocol=protocol,
            evaluator=f8s6["evaluator"],
            scores=(0.8,) * 10,
            method_id=f"higher-result-reference-{index}",
        )
        for index, protocol in enumerate(reference_protocols, start=1)
    )
    campaign = _build_campaign(
        f8s6,
        registry=registry,
        candidate_protocol=candidate_protocol,
        candidate_result=candidate_result,
        reference_results=reference_results,
    )

    assert campaign.verdict is k.SOTACampaignVerdict.CONFIRMED
    assert all(row.mean_favorable_delta == pytest.approx(0.2) for row in campaign.rows)


def test_writeup_gate_authorizes_only_the_exact_signed_campaign_result(f8s6) -> None:
    campaign = f8s6["campaign"]
    decision = screen_auditable_sota_campaign(
        campaign=campaign,
        receipt_key=RESULT_RECEIPT_KEY,
        expected_candidate_protocol_sha256=campaign.candidate_protocol.protocol_sha256,
        headline_metric=campaign.candidate_protocol.metric.metric_id,
        headline_score=campaign.candidate_result.payload.aggregate_score,
    )

    assert decision.disposition is SOTAWriteupDisposition.AUTHORIZED
    assert decision.headline_authorized is True
    assert decision.claim_status == "supported"
    assert decision.claim_strength == "moderate"
    assert decision.campaign_sha256 == campaign.campaign_sha256
    assert decision.comparison_row_sha256s == tuple(row.row_sha256 for row in campaign.rows)


def test_writeup_decision_campaign_and_disposition_bits_cannot_be_forged(f8s6) -> None:
    campaign = f8s6["campaign"]
    decision = screen_auditable_sota_campaign(
        campaign=campaign,
        receipt_key=RESULT_RECEIPT_KEY,
        expected_candidate_protocol_sha256=campaign.candidate_protocol.protocol_sha256,
        headline_metric=campaign.candidate_protocol.metric.metric_id,
        headline_score=campaign.candidate_result.payload.aggregate_score,
    )
    raw = decision.model_dump(mode="python")
    raw["campaign_sha256"] = None
    with pytest.raises(ValidationError, match="campaign evidence must be complete"):
        type(decision).model_validate(raw)

    raw = decision.model_dump(mode="python")
    raw.update(
        disposition=SOTAWriteupDisposition.NOT_DEMONSTRATED,
        headline_authorized=False,
        claim_status="refuted",
        claim_strength="weak",
        reason_codes=("forged_unbeaten_reference",),
    )
    with pytest.raises(ValidationError, match="unbeaten-reference evidence"):
        type(decision).model_validate(raw)


@pytest.mark.parametrize(
    ("updates", "reason"),
    (
        (
            {"expected_candidate_protocol_sha256": sha("f8s6:other-protocol")},
            "candidate_protocol_identity_mismatch",
        ),
        ({"headline_metric": "another-metric"}, "headline_metric_identity_mismatch"),
        ({"headline_score": 0.123}, "headline_score_receipt_mismatch"),
        ({"contribution_type": "paradigm"}, "sota_irrelevant_to_contribution:paradigm"),
    ),
)
def test_writeup_gate_blocks_rebinding_and_nonperformance_headlines(
    f8s6,
    updates,
    reason,
) -> None:
    campaign = f8s6["campaign"]
    values = {
        "campaign": campaign,
        "receipt_key": RESULT_RECEIPT_KEY,
        "expected_candidate_protocol_sha256": campaign.candidate_protocol.protocol_sha256,
        "headline_metric": campaign.candidate_protocol.metric.metric_id,
        "headline_score": campaign.candidate_result.payload.aggregate_score,
    }
    values.update(updates)
    decision = screen_auditable_sota_campaign(**values)

    assert decision.disposition is SOTAWriteupDisposition.BLOCKED
    assert decision.headline_authorized is False
    assert decision.claim_status == "unverified"
    assert decision.claim_strength == "weak"
    assert reason in decision.reason_codes


def test_writeup_gate_turns_an_unbeaten_reference_into_a_refuted_claim(f8s6) -> None:
    candidate_scores = tuple(
        replicate.score for replicate in f8s6["candidate_result"].payload.replicates
    )
    result = issue_result(
        result_id="f8s6-writeup-unbeaten-reference",
        protocol=f8s6["reference_protocols"][0],
        evaluator=f8s6["evaluator"],
        scores=tuple(score - 0.1 for score in candidate_scores),
        method_id="writeup-unbeaten-reference",
    )
    campaign = _build_campaign(
        f8s6,
        reference_results=(result, *f8s6["reference_results"][1:]),
    )
    decision = screen_auditable_sota_campaign(
        campaign=campaign,
        receipt_key=RESULT_RECEIPT_KEY,
        expected_candidate_protocol_sha256=campaign.candidate_protocol.protocol_sha256,
        headline_metric=campaign.candidate_protocol.metric.metric_id,
        headline_score=campaign.candidate_result.payload.aggregate_score,
    )

    assert decision.disposition is SOTAWriteupDisposition.NOT_DEMONSTRATED
    assert decision.claim_status == "refuted"
    assert decision.claim_strength == "weak"
    assert "did not demonstrate superiority" in decision.claim_text


def test_writeup_gate_reverifies_receipts_instead_of_trusting_campaign_shape(f8s6) -> None:
    raw = f8s6["campaign"].model_dump(mode="python")
    raw["candidate_result"]["hmac_sha256"] = sha("f8s6:writeup-tampered-signature")
    tampered = k.SOTAEvaluationCampaign.model_validate(raw)

    with pytest.raises(ValueError, match="signature is invalid"):
        screen_auditable_sota_campaign(
            campaign=tampered,
            receipt_key=RESULT_RECEIPT_KEY,
            expected_candidate_protocol_sha256=tampered.candidate_protocol.protocol_sha256,
            headline_metric=tampered.candidate_protocol.metric.metric_id,
            headline_score=tampered.candidate_result.payload.aggregate_score,
        )


def test_experiment_driver_consumes_audited_campaign_without_legacy_fallback(f8s6) -> None:
    campaign = f8s6["campaign"]
    driver = ExperimentDriver(
        "f8s6-writeup-driver",
        dry_run=True,
        auditable_sota_campaign_fn=lambda request: (campaign, RESULT_RECEIPT_KEY),
    )
    claim = asyncio.run(
        driver._resolve_auditable_sota_claim(
            headline_metric=campaign.candidate_protocol.metric.metric_id,
            headline_score=campaign.candidate_result.payload.aggregate_score,
            candidate_protocol_sha256=campaign.candidate_protocol.protocol_sha256,
            experiment_id="f8s6-experiment",
        )
    )

    assert claim["headline_authorized"] is True
    assert claim["status"] == "supported"
    assert claim["strength"] == "moderate"
    assert claim["evidence"][0]["evidence_ref"] == campaign.campaign_sha256
    assert len(claim["evidence"]) == 5


def test_experiment_driver_provider_error_is_weak_unverified_and_does_not_fallback(
    f8s6,
) -> None:
    campaign = f8s6["campaign"]
    driver = ExperimentDriver(
        "f8s6-writeup-driver-error",
        dry_run=True,
        auditable_sota_campaign_fn=lambda request: campaign,
    )
    claim = asyncio.run(
        driver._resolve_auditable_sota_claim(
            headline_metric=campaign.candidate_protocol.metric.metric_id,
            headline_score=campaign.candidate_result.payload.aggregate_score,
            candidate_protocol_sha256=campaign.candidate_protocol.protocol_sha256,
            experiment_id="f8s6-experiment-error",
        )
    )

    assert claim["headline_authorized"] is False
    assert claim["status"] == "unverified"
    assert claim["strength"] == "weak"
    assert "auditable_sota_provider_error:TypeError" in claim["claim_text"]
