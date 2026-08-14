from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from aletheia.reproducibility.manifest import content_sha256


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "knowledge_boundary_spike.v1.json"
FIXTURE_FILE_SHA256 = "c58b1364ab99d9c4f184b5177051fe045dc5a832b91165e82a49c0e2e38c8d5c"
EXPECTED_SNAPSHOT_SHA256 = "857a235f99695acad0144728bf9c3f8ae62d920d77d24a3611060f196966c3a6"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _revalidate(model_type: type[Any], model: Any, **updates: Any) -> Any:
    data = model.model_dump(mode="python")
    data.update(updates)
    return model_type.model_validate(data)


def _paper(raw: dict[str, Any]) -> k.PaperSnapshot:
    text = raw["text"]
    return k.PaperSnapshot(
        canonical_id=raw["canonical_id"],
        version_id=raw["version_id"],
        title=raw["title"],
        authors=tuple(raw["authors"]),
        venue="Synthetic Evidence Venue",
        publication_type=raw["publication_type"],
        first_public_at=_time(raw["first_public_at"]),
        version_public_at=_time(raw["version_public_at"]),
        observed_at=_time(raw["observed_at"]),
        source_urls=(f"https://fixture.invalid/{raw['key']}/{raw['version_id']}",),
        metadata_sha256=_sha(f"metadata:{raw['key']}:{raw['version_id']}"),
        text_availability=raw["text_availability"],
        text_content_sha256=_sha(f"text:{text}"),
        license_id="synthetic-fixture-only",
        license_terms_sha256=_sha("synthetic fixture license"),
        peer_review_status=(
            "not_peer_reviewed" if raw["publication_type"] == "preprint" else "peer_reviewed"
        ),
    )


def _span(raw: dict[str, Any], paper: k.PaperSnapshot) -> k.SourceSpan:
    text = raw["text"]
    return k.SourceSpan(
        span_id=f"span:{raw['key']}",
        paper_snapshot_sha256=paper.snapshot_sha256,
        text_scope=raw["span_scope"],
        locator=k.SpanLocator(
            section=raw["span_section"],
            char_start=0,
            char_end=len(text),
            normalized_span_sha256=_sha(f"normalized:{' '.join(text.split())}"),
        ),
        exact_text_sha256=_sha(f"exact:{text}"),
        normalized_text_sha256=_sha(f"normalized-text:{' '.join(text.split())}"),
        text_bytes=len(text.encode("utf-8")),
        extraction_method="manual",
        extraction_confidence=0.99,
        verification_status="second_model_verified",
        reviewer_principal_sha256=_sha("span-reviewer"),
        reviewed_at=_time("2024-12-27T00:00:00Z"),
        extracted_at=_time("2024-12-26T00:00:00Z"),
    )


def _novelty_package_sha256(
    *,
    policy: k.NoveltyPolicy,
    corpus: k.CorpusSnapshot,
    session: k.SearchSession,
    coverage: k.CoverageReport,
    graph: k.AtomicClaimGraph,
    candidate_claim_sha256s: tuple[str, ...],
    relations: tuple[k.PriorArtRelation, ...],
) -> str:
    return content_sha256(
        {
            "policy_sha256": policy.policy_sha256,
            "corpus_snapshot_sha256": corpus.snapshot_sha256,
            "search_session_sha256": session.session_sha256,
            "coverage_report_sha256": coverage.report_sha256,
            "claim_graph_sha256": graph.graph_sha256,
            "candidate_claim_sha256s": candidate_claim_sha256s,
            "nearest_prior_art_sha256s": [relation.relation_sha256 for relation in relations],
            "temporal_cutoff": corpus.cutoff_time.isoformat(),
        }
    )


def _build_bundle() -> dict[str, Any]:
    raw = _fixture()
    times = {name: _time(value) for name, value in raw["times"].items()}

    sources = tuple(
        k.CorpusSourceVersion(
            source_id=item["source_id"],
            snapshot_id=item["snapshot_id"],
            snapshot_sha256=_sha(f"source:{item['snapshot_id']}"),
            updated_through=_time(item["updated_through"]),
            retrieved_at=_time(item["retrieved_at"]),
            license_id=item["license_id"],
            terms_sha256=_sha(f"terms:{item['license_id']}"),
        )
        for item in raw["sources"]
    )
    paper_rows = {item["key"]: item for item in raw["papers"]}
    papers = {key: _paper(item) for key, item in paper_rows.items()}
    included_keys = [item["key"] for item in raw["papers"] if item["included"]]
    spans = {key: _span(paper_rows[key], papers[key]) for key in included_keys}
    corpus = k.CorpusSnapshot(
        snapshot_id="fixture-corpus-2025-cutoff",
        version="1",
        cutoff_time=times["cutoff"],
        temporal_mode="contemporaneous",
        sources=sources,
        papers=tuple(papers[key] for key in included_keys),
        spans=tuple(spans[key] for key in included_keys),
        license_policy_sha256=_sha("fixture-license-policy-v1"),
        frozen_at=times["corpus_frozen"],
    )

    metric_raw = raw["metric"]
    metric = k.MetricDefinition(
        metric_id=metric_raw["metric_id"],
        canonical_name=metric_raw["canonical_name"],
        aliases=("Accuracy on Synthetic Drift Fixture",),
        formula_sha256=_sha("correct / total"),
        aggregation_sha256=_sha("macro mean over frozen groups"),
        direction=metric_raw["direction"],
        reporting_unit=metric_raw["reporting_unit"],
        valid_minimum=metric_raw["valid_minimum"],
        valid_maximum=metric_raw["valid_maximum"],
    )

    candidate_raw = raw["candidate_claim"]
    candidate_claim = k.AtomicClaim(
        claim_id="candidate:calibration-method",
        origin="candidate",
        subject=candidate_raw["subject"],
        relation=candidate_raw["relation"],
        object=candidate_raw["object"],
        qualifiers=tuple(candidate_raw["qualifiers"]),
        population=candidate_raw["population"],
        conditions=tuple(candidate_raw["conditions"]),
        direction=candidate_raw["direction"],
        claim_type=candidate_raw["claim_type"],
        quantitative_effect=k.QuantitativeEffect(
            estimate=raw["scores"]["candidate"],
            unit="fraction",
            metric_definition_sha256=metric.metric_sha256,
            uncertainty_type="none_reported",
            sample_size=120,
        ),
        candidate_artifact_sha256=_sha("synthetic-candidate-artifact-v1"),
        asserted_at=times["candidate_asserted"],
    )
    prior_claims: dict[str, k.AtomicClaim] = {}
    for index, item in enumerate(raw["prior_claims"], start=1):
        prior_claims[item["paper_key"]] = k.AtomicClaim(
            claim_id=f"prior:{item['paper_key']}",
            origin="prior_art",
            subject=item["subject"],
            relation=item["relation"],
            object=item["object"],
            qualifiers=("fixture-extracted",),
            population="synthetic sensor streams",
            conditions=("reported protocol",),
            direction="positive",
            claim_type="methodological",
            quantitative_effect=k.QuantitativeEffect(
                estimate=0.70 + index / 100,
                unit="fraction",
                metric_definition_sha256=metric.metric_sha256,
                uncertainty_type="none_reported",
                sample_size=100,
            ),
            source_paper_snapshot_sha256=papers[item["paper_key"]].snapshot_sha256,
            asserted_at=_time("2024-12-28T00:00:00Z"),
        )

    required_query_families = tuple(
        item
        for item in raw["query_families"]
        if item not in {"citation_backward", "citation_forward"}
    )
    search_protocol = k.SearchProtocol(
        protocol_id="fixture-search-protocol-v1",
        objective="Audit the candidate calibration method against pre-cutoff prior art.",
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        cutoff_time=corpus.cutoff_time,
        candidate_claim_sha256s=(candidate_claim.claim_sha256,),
        required_query_families=required_query_families,
        planned_source_ids=tuple(item["source_id"] for item in raw["sources"]),
        seed_paper_snapshot_sha256s=tuple(papers[key].snapshot_sha256 for key in included_keys),
        max_queries=20,
        max_results_per_query=10,
        saturation_rule=k.SaturationRule(
            minimum_rounds=2,
            maximum_rounds=3,
            marginal_new_relevant_fraction=0.05,
            consecutive_saturated_rounds=2,
        ),
        perturbation_plan_sha256=_sha("fixture-query-perturbations-v1"),
        query_planner_sha256=_sha("deterministic-query-planner-v1"),
        frozen_at=times["search_protocol_frozen"],
    )
    queries: list[k.SearchQueryRecord] = []
    for index, family in enumerate(raw["query_families"]):
        response_sha256 = _sha(f"query-response:{index}:{family}")
        queries.append(
            k.SearchQueryRecord(
                query_id=f"query:{index:02d}:{family}",
                family=family,
                source_id=raw["sources"][index % len(raw["sources"])]["source_id"],
                query_text=f"synthetic calibration {family}",
                filters_sha256=_sha(f"filters:{family}"),
                round_index=0 if index < 6 else 1,
                executed_at=times["search_started"] + timedelta(minutes=index),
                outcome="success",
                hits=(
                    k.SearchHit(
                        rank=1,
                        paper_snapshot_sha256=papers[
                            included_keys[index % len(included_keys)]
                        ].snapshot_sha256,
                        provider_record_id=f"fixture-record-{index}",
                        retrieval_score=1.0 - index / 100,
                    ),
                ),
                response_sha256=response_sha256,
            )
        )
    search_session = k.SearchSession(
        session_id="fixture-search-session-v1",
        protocol_sha256=search_protocol.protocol_sha256,
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        queries=tuple(queries),
        started_at=times["search_started"],
        ended_at=times["search_ended"],
        stopping_reason="saturation",
        stopping_evidence_sha256=_sha("two saturated search rounds"),
        replay_cache_sha256s=tuple(
            query.response_sha256 for query in queries if query.response_sha256
        ),
    )

    requirements = tuple(
        k.CoverageRequirement(
            signal=signal,
            direction=(
                "maximum" if signal is k.CoverageSignalName.UNCOVERED_SOURCE_FRACTION else "minimum"
            ),
            threshold=(0.20 if signal is k.CoverageSignalName.UNCOVERED_SOURCE_FRACTION else 0.80),
            hard=True,
            rationale=f"Synthetic hard threshold for {signal.value}.",
        )
        for signal in k.CoverageSignalName
    )
    coverage_policy = k.CoveragePolicy(
        policy_id="fixture-coverage-policy-v1",
        requirements=requirements,
        minimum_nearest_prior_art=3,
        minimum_independent_reviewers=2,
        frozen_at=times["search_protocol_frozen"],
    )
    coverage_signals = tuple(
        k.CoverageSignalResult(
            signal=signal,
            observed=(0.05 if signal is k.CoverageSignalName.UNCOVERED_SOURCE_FRACTION else 0.95),
            status="pass",
            evidence_sha256=_sha(f"coverage-evidence:{signal.value}"),
            detail=f"Synthetic measured result for {signal.value}.",
        )
        for signal in k.CoverageSignalName
    )
    coverage_report = k.CoverageReport(
        report_id="fixture-coverage-report-v1",
        policy=coverage_policy,
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        search_session_sha256=search_session.session_sha256,
        signals=coverage_signals,
        verdict="coverage_sufficient",
        hard_failure_signals=(),
        generated_at=times["coverage_generated"],
    )

    evidence_edges = tuple(
        k.ClaimEvidenceEdge(
            claim_sha256=prior_claims[key].claim_sha256,
            source_span_sha256=spans[key].span_sha256,
            relation="supports",
            extraction_confidence=0.95,
            reviewer_status="human_verified",
            reviewer_principal_sha256=_sha(f"claim-reviewer:{key}"),
            reviewed_at=times["coverage_generated"] + timedelta(minutes=30),
        )
        for key in included_keys
    )
    claim_graph = k.AtomicClaimGraph(
        graph_id="fixture-claim-graph-v1",
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        claims=(candidate_claim, *(prior_claims[key] for key in included_keys)),
        evidence_edges=evidence_edges,
        extraction_policy_sha256=_sha("fixture-claim-extraction-policy-v1"),
        frozen_at=times["claim_graph_frozen"],
    )

    relations: list[k.PriorArtRelation] = []
    for index, item in enumerate(raw["prior_claims"], start=1):
        key = item["paper_key"]
        difference = k.ComponentDifference(
            component=item["difference_component"],
            candidate_value=f"candidate {item['difference_component']}",
            prior_value=f"{key} {item['difference_component']}",
            difference=f"Synthetic component-wise difference from {key}.",
            evidence_span_sha256s=(spans[key].span_sha256,),
        )
        relations.append(
            k.PriorArtRelation(
                candidate_claim_sha256=candidate_claim.claim_sha256,
                prior_claim_sha256=prior_claims[key].claim_sha256,
                relation=item["relation_type"],
                rank=index,
                retrieval_signals=k.RetrievalSignals(
                    lexical=0.90 - index / 100,
                    embedding=0.85 - index / 100,
                    citation=0.70 - index / 100,
                ),
                differences=(difference,),
                evidence_span_sha256s=(spans[key].span_sha256,),
                matcher_manifest_sha256=_sha("fixture-prior-art-matcher-v1"),
                reviewer_status="human_verified",
                reviewer_principal_sha256=_sha(f"relation-reviewer:{key}"),
                reviewed_at=times["relation_reviewed"],
                blocks_strong_novelty=False,
            )
        )
    relation_tuple = tuple(relations)
    novelty_policy = k.NoveltyPolicy(
        policy_id="fixture-novelty-policy-v1",
        minimum_nearest_prior_art=3,
        minimum_independent_reviewers=2,
        frozen_at=times["coverage_generated"],
    )
    candidate_claim_sha256s = (candidate_claim.claim_sha256,)
    evidence_package_sha256 = _novelty_package_sha256(
        policy=novelty_policy,
        corpus=corpus,
        session=search_session,
        coverage=coverage_report,
        graph=claim_graph,
        candidate_claim_sha256s=candidate_claim_sha256s,
        relations=relation_tuple,
    )
    candidate_authors = (_sha("candidate-author"),)
    reviews = (
        k.NoveltyReview(
            reviewer_principal_sha256=_sha("independent-domain-reviewer"),
            reviewer_role="domain_expert",
            evidence_package_sha256=evidence_package_sha256,
            verdict="confirm_evidence_package",
            rationale_sha256=_sha("domain-review-rationale"),
            reviewed_at=times["novelty_reviewed"],
        ),
        k.NoveltyReview(
            reviewer_principal_sha256=_sha("independent-method-reviewer"),
            reviewer_role="methodologist",
            evidence_package_sha256=evidence_package_sha256,
            verdict="confirm_evidence_package",
            rationale_sha256=_sha("method-review-rationale"),
            reviewed_at=times["novelty_reviewed"] + timedelta(minutes=1),
        ),
    )
    novelty_assessment = k.NoveltyAssessment(
        assessment_id="fixture-novelty-assessment-v1",
        policy=novelty_policy,
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        search_session_sha256=search_session.session_sha256,
        coverage_report_sha256=coverage_report.report_sha256,
        coverage_verdict=coverage_report.verdict,
        claim_graph_sha256=claim_graph.graph_sha256,
        candidate_claim_sha256s=candidate_claim_sha256s,
        candidate_author_principal_sha256s=candidate_authors,
        nearest_prior_art=relation_tuple,
        classification="novel_method",
        exact_differences=tuple(relation.differences[0] for relation in relation_tuple),
        temporal_cutoff=corpus.cutoff_time,
        temporal_limitations="Only evidence public and observed by the frozen cutoff is included.",
        model_prior_limitations="Model pretraining may contain later facts; it cannot add evidence.",
        contamination_disclosure="All fixture identities and text are synthetic.",
        evidence_package_sha256=evidence_package_sha256,
        reviews=reviews,
        strong_novelty_eligible=True,
        claim_strength_ceiling="moderate",
        assessed_at=times["novelty_assessed"],
    )

    dataset_raw = raw["dataset"]
    dataset = k.DatasetVersion(
        dataset_id=dataset_raw["dataset_id"],
        canonical_name=dataset_raw["canonical_name"],
        aliases=("SDF",),
        version_id=dataset_raw["version_id"],
        content_sha256=_sha("synthetic-drift-dataset-bytes-v2024.12"),
        schema_sha256=_sha("synthetic-drift-dataset-schema-v1"),
        license_id="CC0-1.0",
        source_url="https://fixture.invalid/datasets/synthetic-drift",
        released_at=_time(dataset_raw["released_at"]),
        observed_at=_time(dataset_raw["observed_at"]),
    )
    resource_budget = k.ResourceBudgetSignature(
        compute_policy_sha256=_sha("fixed-compute-policy"),
        data_budget_sha256=_sha("fixed-data-budget"),
        hardware_sha256=_sha("synthetic-accelerator"),
        accelerator_hours=4.0,
        wall_clock_hours=8.0,
        maximum_cost_usd=25.0,
    )
    common_protocol = {
        "task_definition_sha256": _sha("synthetic drift task v1"),
        "dataset": dataset,
        "split_policy_sha256": _sha("grouped frozen 70/15/15 split"),
        "split_content_sha256": _sha("split membership bytes v1"),
        "grouping_policy_sha256": _sha("group by sensor identity"),
        "leakage_policy_sha256": _sha("no sensor identity crosses splits"),
        "preprocessing_sha256": _sha("standardize from training statistics"),
        "exclusions_sha256": _sha("exclude corrupt fixture rows only"),
        "metric": metric,
        "uncertainty_policy_sha256": _sha("paired bootstrap 95 percent"),
        "statistical_test_sha256": _sha("two-sided paired permutation"),
        "resource_budget": resource_budget,
        "external_resources_sha256": _sha("no external resources"),
        "pretraining_sha256": _sha("no task-specific pretraining"),
    }
    candidate_protocol = k.ProtocolSignature(
        protocol_id="candidate-protocol-v1",
        method=k.MethodEntity(
            method_id="candidate-method-v1",
            canonical_name="Stability-Regularized Nonlinear Calibration",
            aliases=("SRNC",),
            specification_sha256=_sha("candidate method specification"),
            implementation_sha256=_sha("candidate implementation bytes"),
        ),
        **common_protocol,
        evaluation_date=_time("2025-01-01T09:00:00Z"),
        frozen_at=_time("2024-12-31T18:00:00Z"),
    )
    reference_method = k.MethodEntity(
        method_id="reference-method-v1",
        canonical_name="Adaptive Calibration Reference",
        aliases=("ACR",),
        specification_sha256=_sha("reference method specification"),
        implementation_sha256=_sha("reference implementation bytes"),
    )
    reference_protocol = k.ProtocolSignature(
        protocol_id="reference-protocol-v1",
        method=reference_method,
        **common_protocol,
        evaluation_date=_time("2024-12-20T00:00:00Z"),
        frozen_at=_time("2024-12-16T00:00:00Z"),
    )
    split_mismatch_protocol = k.ProtocolSignature(
        protocol_id="reference-protocol-split-mismatch-v1",
        method=reference_method,
        **{
            **common_protocol,
            "split_content_sha256": _sha("different split membership bytes"),
        },
        evaluation_date=_time("2024-12-20T00:00:00Z"),
        frozen_at=_time("2024-12-16T00:00:00Z"),
    )
    compatible_sota = k.build_sota_comparison(
        comparison_id="candidate-vs-reference-compatible",
        candidate=candidate_protocol,
        reference=reference_protocol,
        candidate_score=raw["scores"]["candidate"],
        reference_score=raw["scores"]["reference"],
        assessed_at=times["sota_assessed"],
        generated_at=times["sota_generated"],
    )
    mismatch_sota = k.build_sota_comparison(
        comparison_id="candidate-vs-reference-split-mismatch",
        candidate=candidate_protocol,
        reference=split_mismatch_protocol,
        candidate_score=raw["scores"]["candidate"],
        reference_score=raw["scores"]["split_mismatch_reference"],
        assessed_at=times["sota_assessed"],
        generated_at=times["sota_generated"],
    )
    correction_report = k.ContradictionCorrectionReport(
        report_id="fixture-contradiction-correction-v1",
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        claim_graph_sha256=claim_graph.graph_sha256,
        checked_paper_snapshot_sha256s=tuple(papers[key].snapshot_sha256 for key in included_keys),
        correction_retraction_check_complete=True,
        generated_at=times["correction_reported"],
    )
    bundle = k.KnowledgeBoundarySnapshot(
        snapshot_id="fixture-knowledge-boundary-v1",
        corpus=corpus,
        search_protocol=search_protocol,
        search_session=search_session,
        coverage_report=coverage_report,
        claim_graph=claim_graph,
        prior_art_relations=relation_tuple,
        novelty_assessment=novelty_assessment,
        protocol_signatures=(
            candidate_protocol,
            reference_protocol,
            split_mismatch_protocol,
        ),
        sota_comparisons=(compatible_sota, mismatch_sota),
        contradiction_correction_report=correction_report,
        frozen_at=times["snapshot_frozen"],
    )
    return {
        "raw": raw,
        "times": times,
        "papers": papers,
        "spans": spans,
        "candidate_claim": candidate_claim,
        "prior_claims": prior_claims,
        "bundle": bundle,
    }


def test_fixture_builds_a_deterministic_frozen_knowledge_boundary() -> None:
    first = _build_bundle()["bundle"]
    second = _build_bundle()["bundle"]

    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_FILE_SHA256
    assert first.snapshot_sha256 == second.snapshot_sha256 == EXPECTED_SNAPSHOT_SHA256
    assert first.novelty_assessment.strong_novelty_eligible is True
    assert first.novelty_assessment.claim_strength_ceiling is k.NoveltyClaimCeiling.MODERATE
    assert first.coverage_report.verdict is k.CoverageVerdict.SUFFICIENT
    with pytest.raises(ValidationError, match="frozen"):
        first.corpus.papers[0].title = "silently mutated title"
    with pytest.raises(ValidationError, match="Extra inputs"):
        k.PaperSnapshot.model_validate(
            {**first.corpus.papers[0].model_dump(mode="python"), "authority": "tool_admin"}
        )


def test_temporal_holdout_rejects_a_future_publication() -> None:
    built = _build_bundle()
    corpus = built["bundle"].corpus
    future = built["papers"]["future_holdout"]

    with pytest.raises(ValidationError, match="published after cutoff"):
        _revalidate(k.CorpusSnapshot, corpus, papers=(*corpus.papers, future))


def test_reconstructed_post_cutoff_observation_requires_as_of_evidence() -> None:
    corpus = _build_bundle()["bundle"].corpus
    paper = _revalidate(
        k.PaperSnapshot,
        corpus.papers[0],
        observed_at=_time("2025-02-01T00:00:00Z"),
    )

    with pytest.raises(ValidationError, match="as-of evidence"):
        _revalidate(
            k.CorpusSnapshot,
            corpus,
            temporal_mode="reconstructed",
            papers=(paper, *corpus.papers[1:]),
        )


def test_source_spans_cannot_invent_full_text_or_tool_authority() -> None:
    built = _build_bundle()
    corpus = built["bundle"].corpus
    injection_span = built["spans"]["prior_a"]
    abstract_paper = built["papers"]["prior_b"]
    forged_full_text_span = _revalidate(
        k.SourceSpan,
        built["spans"]["prior_b"],
        text_scope="full_text",
    )

    assert injection_span.content_trust == "untrusted_literature_data"
    assert "text" not in k.SourceSpan.model_fields
    with pytest.raises(ValidationError, match="full-text span"):
        _revalidate(
            k.CorpusSnapshot,
            corpus,
            spans=(
                injection_span,
                forged_full_text_span,
                built["spans"]["prior_c"],
            ),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        k.SourceSpan.model_validate(
            {
                **injection_span.model_dump(mode="python"),
                "tool_authority": "elevated",
            }
        )
    assert abstract_paper.text_availability is k.TextAvailability.ABSTRACT


def test_every_prior_claim_requires_exact_span_evidence() -> None:
    graph = _build_bundle()["bundle"].claim_graph

    with pytest.raises(ValidationError, match="every prior-art claim"):
        _revalidate(
            k.AtomicClaimGraph,
            graph,
            evidence_edges=graph.evidence_edges[:-1],
        )


def test_retrieval_failure_forces_insufficient_and_indeterminate_novelty() -> None:
    bundle = _build_bundle()["bundle"]
    failed_query = _revalidate(
        k.SearchQueryRecord,
        bundle.search_session.queries[0],
        outcome="error",
        hits=(),
        response_sha256=None,
        error_class="FixtureSourceUnavailable",
        error_detail_sha256=_sha("fixture source outage"),
    )
    queries = (failed_query, *bundle.search_session.queries[1:])
    failed_session = _revalidate(
        k.SearchSession,
        bundle.search_session,
        queries=queries,
        stopping_reason="hard_failure",
        replay_cache_sha256s=tuple(
            query.response_sha256 for query in queries if query.response_sha256
        ),
    )
    failed_signal = _revalidate(
        k.CoverageSignalResult,
        bundle.coverage_report.signals[0],
        observed=0.0,
        status="fail",
    )
    coverage_signals = (failed_signal, *bundle.coverage_report.signals[1:])
    failed_coverage = _revalidate(
        k.CoverageReport,
        bundle.coverage_report,
        search_session_sha256=failed_session.session_sha256,
        signals=coverage_signals,
        verdict="coverage_insufficient",
        hard_failure_signals=(failed_signal.signal,),
    )
    novelty = bundle.novelty_assessment
    evidence_package = _novelty_package_sha256(
        policy=novelty.policy,
        corpus=bundle.corpus,
        session=failed_session,
        coverage=failed_coverage,
        graph=bundle.claim_graph,
        candidate_claim_sha256s=novelty.candidate_claim_sha256s,
        relations=bundle.prior_art_relations,
    )
    indeterminate = _revalidate(
        k.NoveltyAssessment,
        novelty,
        search_session_sha256=failed_session.session_sha256,
        coverage_report_sha256=failed_coverage.report_sha256,
        coverage_verdict="coverage_insufficient",
        classification="indeterminate_due_to_coverage",
        evidence_package_sha256=evidence_package,
        reviews=(),
        strong_novelty_eligible=False,
        claim_strength_ceiling="speculative",
    )
    degraded = _revalidate(
        k.KnowledgeBoundarySnapshot,
        bundle,
        search_session=failed_session,
        coverage_report=failed_coverage,
        novelty_assessment=indeterminate,
    )

    assert degraded.coverage_report.verdict is k.CoverageVerdict.INSUFFICIENT
    assert (
        degraded.novelty_assessment.classification
        is k.NoveltyClassification.INDETERMINATE_DUE_TO_COVERAGE
    )
    with pytest.raises(ValidationError, match="insufficient coverage forces"):
        _revalidate(
            k.NoveltyAssessment,
            indeterminate,
            classification="novel_method",
        )


def test_equivalent_prior_art_blocks_strong_novelty() -> None:
    novelty = _build_bundle()["bundle"].novelty_assessment
    original = novelty.nearest_prior_art[0]
    equivalent = _revalidate(
        k.PriorArtRelation,
        original,
        relation="equivalent",
        differences=(),
        blocks_strong_novelty=True,
    )
    relations = (equivalent, *novelty.nearest_prior_art[1:])
    package = content_sha256(
        {
            "policy_sha256": novelty.policy.policy_sha256,
            "corpus_snapshot_sha256": novelty.corpus_snapshot_sha256,
            "search_session_sha256": novelty.search_session_sha256,
            "coverage_report_sha256": novelty.coverage_report_sha256,
            "claim_graph_sha256": novelty.claim_graph_sha256,
            "candidate_claim_sha256s": novelty.candidate_claim_sha256s,
            "nearest_prior_art_sha256s": [item.relation_sha256 for item in relations],
            "temporal_cutoff": novelty.temporal_cutoff.isoformat(),
        }
    )
    reviews = tuple(
        _revalidate(k.NoveltyReview, review, evidence_package_sha256=package)
        for review in novelty.reviews
    )

    with pytest.raises(ValidationError, match="strong-novelty eligibility"):
        _revalidate(
            k.NoveltyAssessment,
            novelty,
            nearest_prior_art=relations,
            evidence_package_sha256=package,
            reviews=reviews,
        )


def test_candidate_author_cannot_review_novelty_package() -> None:
    novelty = _build_bundle()["bundle"].novelty_assessment
    self_review = _revalidate(
        k.NoveltyReview,
        novelty.reviews[0],
        reviewer_principal_sha256=novelty.candidate_author_principal_sha256s[0],
    )

    with pytest.raises(ValidationError, match="cannot review their own"):
        _revalidate(
            k.NoveltyAssessment,
            novelty,
            reviews=(self_review, novelty.reviews[1]),
        )


def test_novelty_evidence_package_hash_is_self_validating() -> None:
    novelty = _build_bundle()["bundle"].novelty_assessment

    with pytest.raises(ValidationError, match="evidence-package hash"):
        _revalidate(
            k.NoveltyAssessment,
            novelty,
            evidence_package_sha256=_sha("forged-evidence-package"),
        )


def test_sota_delta_exists_only_for_compatible_protocols() -> None:
    compatible, split_mismatch = _build_bundle()["bundle"].sota_comparisons

    assert compatible.comparability.status is k.ComparabilityStatus.COMPATIBLE
    assert [item.dimension for item in compatible.comparability.mismatches] == [
        k.ProtocolDimension.EVALUATION_DATE
    ]
    assert compatible.raw_delta == pytest.approx(0.04)
    assert compatible.headline_verdict == "beats_reference"
    assert compatible.headline_delta_allowed is True

    assert split_mismatch.comparability.status is k.ComparabilityStatus.NON_COMPARABLE
    assert k.ProtocolDimension.SPLIT in {
        item.dimension for item in split_mismatch.comparability.mismatches
    }
    assert split_mismatch.raw_delta is None
    assert split_mismatch.candidate_outperforms is None
    assert split_mismatch.headline_verdict == "non_comparable"
    assert split_mismatch.headline_delta_allowed is False


def test_non_comparable_protocol_cannot_forge_a_better_number_headline() -> None:
    comparison = _build_bundle()["bundle"].sota_comparisons[1]

    with pytest.raises(ValidationError, match="cannot emit a SOTA delta"):
        _revalidate(
            k.SOTAComparison,
            comparison,
            raw_delta=0.05,
            favorable_delta=0.05,
            candidate_outperforms=True,
            headline_delta_allowed=True,
            headline_verdict="beats_reference",
        )


def test_bundle_rejects_search_hits_outside_the_frozen_corpus() -> None:
    bundle = _build_bundle()["bundle"]
    query = bundle.search_session.queries[0]
    forged_hit = _revalidate(
        k.SearchHit,
        query.hits[0],
        paper_snapshot_sha256=_sha("paper-outside-frozen-corpus"),
    )
    forged_query = _revalidate(k.SearchQueryRecord, query, hits=(forged_hit,))
    forged_session = _revalidate(
        k.SearchSession,
        bundle.search_session,
        queries=(forged_query, *bundle.search_session.queries[1:]),
    )

    with pytest.raises(ValidationError, match="outside the frozen corpus"):
        _revalidate(
            k.KnowledgeBoundarySnapshot,
            bundle,
            search_session=forged_session,
        )


def test_passed_correction_coverage_requires_a_complete_report() -> None:
    bundle = _build_bundle()["bundle"]
    incomplete = _revalidate(
        k.ContradictionCorrectionReport,
        bundle.contradiction_correction_report,
        correction_retraction_check_complete=False,
    )

    with pytest.raises(ValidationError, match="complete correction report"):
        _revalidate(
            k.KnowledgeBoundarySnapshot,
            bundle,
            contradiction_correction_report=incomplete,
        )
