from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .test_schema_spike import _time


RECEIPT_KEY = bytes.fromhex(sha("f8s5-calibration-receipt-key-v1"))


def _variant(
    *,
    split: k.NoveltyCalibrationSplit,
    case_index: int,
    variant_index: int,
    kind: k.NoveltyPerturbationKind,
) -> k.NoveltyCalibrationVariant:
    identity = f"f8s5:{split.value}:{case_index:03d}:{variant_index}"
    return k.NoveltyCalibrationVariant(
        variant_id=f"variant:{split.value}:{case_index:03d}:{variant_index}",
        kind=kind,
        candidate_claim_sha256=sha(f"{identity}:claim"),
        graph_bundle_sha256=sha(f"{identity}:graph"),
        search_protocol_sha256=sha(f"{identity}:search-protocol"),
        perturbation_evidence_sha256=sha(f"{identity}:perturbation-evidence"),
    )


def _case(
    *,
    split: k.NoveltyCalibrationSplit,
    case_index: int,
) -> k.NoveltyCalibrationCase:
    variants = tuple(
        _variant(
            split=split,
            case_index=case_index,
            variant_index=variant_index,
            kind=kind,
        )
        for variant_index, kind in enumerate(
            (
                k.NoveltyPerturbationKind.BASE,
                k.NoveltyPerturbationKind.CLAIM_PARAPHRASE,
                k.NoveltyPerturbationKind.QUERY_SYNONYM,
            )
        )
    )
    is_holdout = split is k.NoveltyCalibrationSplit.TEMPORAL_HOLDOUT
    return k.NoveltyCalibrationCase(
        case_id=f"case:{split.value}:{case_index:03d}",
        split=split,
        domain=("materials" if case_index % 2 else "machine_learning"),
        temporal_cutoff=_time("2025-01-01T00:00:00Z" if is_holdout else "2024-01-01T00:00:00Z"),
        corpus_snapshot_sha256=sha(f"f8s5:{split.value}:corpus"),
        candidate_author_principal_sha256s=(sha(f"f8s5:{split.value}:{case_index:03d}:author"),),
        variants=variants,
        input_evidence_sha256=sha(f"f8s5:{split.value}:{case_index:03d}:input-evidence"),
        frozen_at=_time("2025-06-01T00:00:00Z" if is_holdout else "2024-06-01T00:00:00Z"),
    )


def _label(case: k.NoveltyCalibrationCase, case_index: int) -> k.NoveltyCalibrationLabel:
    strong = case_index >= 30
    nearest = sha(f"f8s5:{case.split.value}:{case_index:03d}:known-prior")
    adjudicators = tuple(
        sorted(
            (
                sha(f"f8s5:{case.split.value}:{case_index:03d}:adjudicator:a"),
                sha(f"f8s5:{case.split.value}:{case_index:03d}:adjudicator:b"),
            )
        )
    )
    return k.NoveltyCalibrationLabel(
        case_sha256=case.case_sha256,
        expected_prior_claim_sha256s=(nearest,),
        expected_nearest_prior_claim_sha256=nearest,
        expected_seed_paper_sha256s=(sha(f"f8s5:{case.split.value}:{case_index:03d}:seed-paper"),),
        expected_classification=(
            k.NoveltyClassification.NOVEL_METHOD
            if strong
            else k.NoveltyClassification.INCREMENTAL_EXTENSION
        ),
        expert_adjudicator_principal_sha256s=adjudicators,
        adjudication_receipt_sha256=sha(f"f8s5:{case.split.value}:{case_index:03d}:adjudication"),
        labeled_at=case.frozen_at,
    )


def relation_for_label(
    label: k.NoveltyCalibrationLabel,
    *,
    identity: str,
) -> k.CalibrationRelationView:
    component = (
        k.DifferenceComponent.METHOD
        if label.expected_classification is k.NoveltyClassification.NOVEL_METHOD
        else k.DifferenceComponent.CONDITION
    )
    return k.CalibrationRelationView(
        rank=1,
        prior_claim_sha256=label.expected_nearest_prior_claim_sha256,
        relation=k.PriorArtRelationType.EXTENSION,
        difference_components=(component,),
        relation_sha256=sha(f"{identity}:relation"),
    )


def resign_receipt(
    receipt: k.SignedCalibrationTrialReceipt,
    **payload_updates: object,
) -> k.SignedCalibrationTrialReceipt:
    raw = receipt.payload.model_dump(mode="python")
    raw.update(payload_updates)
    payload = k.CalibrationTrialPayload.model_validate(raw)
    return k.SignedCalibrationTrialReceipt.sign(
        payload=payload,
        key_id=receipt.key_id,
        key=RECEIPT_KEY,
    )


def build_f8s5_fixture() -> dict[str, Any]:
    policy = k.NoveltyCalibrationPolicy(
        policy_id="f8s5-novelty-calibration-policy-v1",
        frozen_at=_time("2023-12-01T00:00:00Z"),
    )
    cases = tuple(
        _case(split=split, case_index=case_index)
        for split in k.NoveltyCalibrationSplit
        for case_index in range(40)
    )
    labels = tuple(
        _label(case, case_index)
        for case in cases
        for case_index in (int(case.case_id.rsplit(":", 1)[1]),)
    )
    system_manifest_sha256 = sha("f8s5:system-manifest")
    evaluator_manifest = k.CalibrationEvaluatorManifest(
        evaluator_id="f8s5-independent-calibration-evaluator-v1",
        evaluator_code_sha256=sha("f8s5:evaluator-code"),
        relation_view_parser_sha256=sha("f8s5:relation-view-parser"),
        classification_policy_sha256=k.NOVELTY_CLASSIFICATION_POLICY_SHA256,
        receipt_key_id="f8s5-calibration-key-v1",
        frozen_at=_time("2025-06-15T00:00:00Z"),
    )
    suite = k.build_novelty_calibration_suite(
        suite_id="f8s5-known-answer-temporal-suite-v1",
        policy=policy,
        system_manifest_sha256=system_manifest_sha256,
        cases=cases,
        labels=labels,
        holdout_custody_manifest_sha256=sha("f8s5:holdout-custody-manifest"),
        sealed_at=_time("2025-07-01T00:00:00Z"),
    )
    labels_by_case = {label.case_sha256: label for label in labels}
    receipts: list[k.SignedCalibrationTrialReceipt] = []
    for case in suite.cases:
        label = labels_by_case[case.case_sha256]
        for variant in case.variants:
            identity = f"f8s5:{case.case_id}:{variant.variant_id}"
            relation = relation_for_label(label, identity=identity)
            payload = k.CalibrationTrialPayload(
                trial_id=f"trial:{case.case_id}:{variant.variant_id}",
                case_sha256=case.case_sha256,
                variant_sha256=variant.variant_sha256,
                split=case.split,
                system_manifest_sha256=system_manifest_sha256,
                evaluator_manifest_sha256=evaluator_manifest.manifest_sha256,
                candidate_claim_sha256=variant.candidate_claim_sha256,
                prior_art_resolution_sha256=sha(f"{identity}:resolution"),
                search_session_sha256=sha(f"{identity}:search-session"),
                outcome=k.CalibrationTrialOutcome.SUCCESS,
                relations=(relation,),
                predicted_classification=label.expected_classification,
                search_hit_paper_sha256s=label.expected_seed_paper_sha256s,
                completed_at=_time("2025-07-02T00:00:00Z"),
            )
            receipts.append(
                k.SignedCalibrationTrialReceipt.sign(
                    payload=payload,
                    key_id=evaluator_manifest.receipt_key_id,
                    key=RECEIPT_KEY,
                )
            )
    report = k.build_novelty_calibration_report(
        report_id="f8s5-calibration-report-v1",
        suite=suite,
        evaluator_manifest=evaluator_manifest,
        labels=labels,
        trial_receipts=tuple(receipts),
        receipt_key=RECEIPT_KEY,
        generated_at=_time("2025-07-03T00:00:00Z"),
    )
    return {
        "policy": policy,
        "cases": cases,
        "labels": labels,
        "suite": suite,
        "system_manifest_sha256": system_manifest_sha256,
        "evaluator_manifest": evaluator_manifest,
        "receipts": tuple(receipts),
        "report": report,
        "receipt_key": RECEIPT_KEY,
    }


class LiveStepClock:
    def __init__(self) -> None:
        self.current = _time("2025-08-03T00:00:00Z")

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


async def build_f8s5_live_fixture(
    tmp_path,
    *,
    novelty_kind: str = "incremental",
) -> dict[str, Any]:
    from .f8s2_fixtures import (
        build_adapters,
        build_citation_policy,
        build_manifests,
        build_protocol,
        build_term_set,
    )
    from .f8s4_fixtures import (
        build_executor,
        build_f8s4_fixture,
        build_review,
    )

    calibration = build_f8s5_fixture()
    prior_fixture = await build_f8s4_fixture()
    first_prior = prior_fixture["prior_claims"][0]
    if novelty_kind == "strong":
        prior_fixture["matcher"].relation_specs[first_prior.claim_sha256] = (
            k.PriorArtRelationType.EXTENSION,
            0.97,
            0.96,
            k.DifferenceComponent.METHOD,
        )
    elif novelty_kind == "known":
        prior_fixture["matcher"].relation_specs[first_prior.claim_sha256] = (
            k.PriorArtRelationType.EQUIVALENT,
            0.97,
            None,
            None,
        )
    elif novelty_kind != "incremental":
        raise ValueError("novelty_kind must be incremental, strong, or known")
    execution = await build_executor(prior_fixture).execute(
        protocol=prior_fixture["protocol"],
        execution_id="f8s5-live-prior-art-execution",
    )
    reviews = tuple(
        build_review(
            execution=execution,
            candidate=next(
                candidate
                for candidate in execution.relation_candidates
                if candidate.relation_candidate_sha256 == task.relation_candidate_sha256
            ),
        )
        for task in execution.review_queue.tasks
    )
    resolution = k.resolve_prior_art_matching(
        execution=execution,
        reviews=reviews,
        resolution_id="resolution:f8s5:live-prior-art",
        resolved_at=_time("2025-01-09T00:00:00Z"),
    )
    ingestion_bundle = prior_fixture["claim_fixture"]["bundle"]
    graph_bundle = prior_fixture["graph_bundle"]
    corpus = ingestion_bundle.corpus
    candidate_claims = tuple(
        target.candidate_claim_sha256 for target in resolution.execution.protocol.targets
    )
    seed_papers = tuple(
        sorted(
            {
                claim.source_paper_snapshot_sha256
                for claim in graph_bundle.graph.claims
                if claim.origin is k.ClaimOrigin.PRIOR_ART
                and claim.source_paper_snapshot_sha256 is not None
            }
        )
    )

    protocol_raw = build_protocol(max_queries=2_000).model_dump(mode="python")
    protocol_raw.update(
        protocol_id="f8s5-live-search-protocol-v1",
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        cutoff_time=corpus.cutoff_time,
        candidate_claim_sha256s=candidate_claims,
        seed_paper_snapshot_sha256s=seed_papers,
        frozen_at=_time("2025-08-01T00:00:00Z"),
    )
    search_protocol = k.SearchProtocol.model_validate(protocol_raw)
    citation_policy_raw = build_citation_policy(consecutive_saturated_rounds=1).model_dump(
        mode="python"
    )
    citation_policy_raw.update(
        policy_id="f8s5-live-citation-policy-v1",
        frozen_at=_time("2025-08-01T00:00:00Z"),
    )
    citation_policy = k.CitationTraversalPolicy.model_validate(citation_policy_raw)
    search_plan = k.build_search_execution_plan(
        plan_id="f8s5-live-search-plan-v1",
        protocol=search_protocol,
        term_set=build_term_set(),
        adapters=build_manifests(),
        frozen_at=_time("2025-08-01T01:00:00Z"),
        citation_traversal_policy_sha256=citation_policy.policy_sha256,
    )
    adapters = build_adapters(search_plan)
    neighbor = sha("f8s5-live-citation-neighbor")
    graph: dict[tuple[str, k.QueryFamily], tuple[str, ...]] = {}
    for seed in seed_papers:
        graph[(seed, k.QueryFamily.CITATION_BACKWARD)] = (seed, neighbor)
        graph[(seed, k.QueryFamily.CITATION_FORWARD)] = (seed,)
    graph[(neighbor, k.QueryFamily.CITATION_BACKWARD)] = ()
    graph[(neighbor, k.QueryFamily.CITATION_FORWARD)] = ()
    for adapter in adapters.values():
        adapter.citation_graph = graph
    search_executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "f8s5-live-search"),
        adapters=adapters,
        clock=LiveStepClock(),
    )
    initial = await search_executor.execute(
        plan=search_plan,
        execution_id="f8s5-live-search-initial",
    )
    campaign = await k.run_citation_traversal(
        campaign_id="f8s5-live-citation-campaign",
        policy=citation_policy,
        initial_execution=initial,
        executor=search_executor,
    )
    contradictory_relations = tuple(
        item.relation.relation_sha256
        for item in resolution.accepted
        if item.relation.relation is k.PriorArtRelationType.CONTRADICTION
    )
    correction_report = k.ContradictionCorrectionReport(
        report_id="f8s5-live-correction-report",
        corpus_snapshot_sha256=corpus.snapshot_sha256,
        claim_graph_sha256=graph_bundle.graph.graph_sha256,
        checked_paper_snapshot_sha256s=seed_papers,
        contradictory_prior_art_relation_sha256s=contradictory_relations,
        correction_retraction_check_complete=True,
        generated_at=_time("2025-08-05T00:00:00Z"),
    )
    coverage = k.build_calibrated_novelty_coverage_assessment(
        assessment_id="f8s5-live-calibrated-coverage",
        calibration_report=calibration["report"],
        calibration_receipt_key=RECEIPT_KEY,
        ingestion_bundle=ingestion_bundle,
        claim_graph_bundle=graph_bundle,
        prior_art_resolution=resolution,
        correction_report=correction_report,
        campaign=campaign,
        policy_frozen_at=_time("2025-08-02T00:00:00Z"),
        generated_at=_time("2025-08-06T00:00:00Z"),
    )
    return {
        **calibration,
        "prior_fixture": prior_fixture,
        "prior_art_execution": execution,
        "prior_art_resolution": resolution,
        "ingestion_bundle": ingestion_bundle,
        "graph_bundle": graph_bundle,
        "search_protocol": search_protocol,
        "search_plan": search_plan,
        "campaign": campaign,
        "correction_report": correction_report,
        "coverage": coverage,
    }


def build_f8s5_direction_fixture(
    fixture: dict[str, Any],
    *,
    coverage: k.CalibratedNoveltyCoverageAssessment | None = None,
    roles: tuple[str, ...] = ("domain_expert", "research_librarian"),
    verdicts: tuple[k.NoveltyReviewVerdict, ...] | None = None,
) -> dict[str, Any]:
    coverage = coverage or fixture["coverage"]
    candidate = fixture["prior_fixture"]["candidate"]
    authorship = k.build_candidate_authorship_manifest(
        manifest_id="f8s5-candidate-authorship-v1",
        coverage=coverage,
        candidate_claim_sha256s=(candidate.claim_sha256,),
        author_principal_sha256s=(sha("f8s5:candidate-author"),),
        authorship_evidence_sha256=sha("f8s5:authorship-evidence"),
        frozen_at=_time("2025-01-10T00:00:00Z"),
    )
    package = k.build_novelty_evidence_package(
        package_id="f8s5-reviewed-novelty-package-v1",
        coverage=coverage,
        authorship_manifest=authorship,
        candidate_claim_sha256=candidate.claim_sha256,
        temporal_limitations=(
            "The conclusion is limited to public evidence observed by the frozen 2024 cutoff."
        ),
        model_prior_limitations=(
            "Model pretraining may contain uncited related work; only corpus-grounded relations count."
        ),
        contamination_disclosure=(
            "The candidate author was excluded from both independent novelty reviews."
        ),
        assembled_at=_time("2025-08-07T00:00:00Z"),
    )
    verdicts = verdicts or tuple(k.NoveltyReviewVerdict.CONFIRM_EVIDENCE_PACKAGE for _ in roles)
    reviews = tuple(
        k.CalibratedNoveltyReview(
            review_id=f"review:f8s5:{index:02d}",
            evidence_package_sha256=package.package_sha256,
            reviewer_principal_sha256=sha(f"f8s5:reviewer:{index:02d}"),
            reviewer_credential_sha256=sha(f"f8s5:reviewer-credential:{index:02d}"),
            reviewer_role=role,
            verdict=verdict,
            rationale_sha256=sha(f"f8s5:review-rationale:{index:02d}"),
            attestation_receipt_sha256=sha(f"f8s5:review-attestation:{index:02d}"),
            reviewed_at=_time("2025-08-08T00:00:00Z"),
        )
        for index, (role, verdict) in enumerate(
            zip(roles, verdicts, strict=True),
            start=1,
        )
    )
    decision = k.build_reviewed_novelty_decision(
        decision_id="f8s5-reviewed-novelty-decision-v1",
        assessment_id="f8s5-novelty-assessment-v1",
        coverage=coverage,
        calibration_receipt_key=RECEIPT_KEY,
        authorship_manifest=authorship,
        evidence_package=package,
        independent_reviews=reviews,
        generated_at=_time("2025-08-09T00:00:00Z"),
    )
    gate = k.build_research_direction_gate(
        gate_id="f8s5-research-direction-gate-v1",
        novelty_decision=decision,
        calibration_receipt_key=RECEIPT_KEY,
        decided_at=_time("2025-08-10T00:00:00Z"),
    )
    return {
        "coverage": coverage,
        "authorship": authorship,
        "package": package,
        "reviews": reviews,
        "decision": decision,
        "gate": gate,
    }
