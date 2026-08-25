from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aletheia.knowledge as k
from .f8s3_fixtures import sha
from .f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture
from .test_schema_spike import _time


RESULT_RECEIPT_KEY = bytes.fromhex(sha("f8s6-benchmark-result-receipt-key-v1"))


def _method(identity: str) -> k.MethodEntity:
    return k.MethodEntity(
        method_id=f"method:{identity}",
        canonical_name=f"F8-S6 {identity}",
        specification_sha256=sha(f"f8s6:{identity}:specification"),
        implementation_sha256=sha(f"f8s6:{identity}:implementation"),
    )


def build_protocol(
    *,
    protocol_id: str,
    method_id: str,
    dataset: k.DatasetVersion,
    metric: k.MetricDefinition,
    resource_budget: k.ResourceBudgetSignature,
    frozen_at: datetime,
    evaluation_date: datetime,
    **updates: object,
) -> k.ProtocolSignature:
    raw: dict[str, object] = {
        "protocol_id": protocol_id,
        "method": _method(method_id),
        "task_definition_sha256": sha("f8s6:task-definition"),
        "dataset": dataset,
        "split_policy_sha256": sha("f8s6:split-policy"),
        "split_content_sha256": sha("f8s6:split-content"),
        "grouping_policy_sha256": sha("f8s6:grouping-policy"),
        "leakage_policy_sha256": sha("f8s6:leakage-policy"),
        "preprocessing_sha256": sha("f8s6:preprocessing"),
        "exclusions_sha256": sha("f8s6:exclusions"),
        "metric": metric,
        "uncertainty_policy_sha256": sha("f8s6:uncertainty-policy"),
        "statistical_test_sha256": k.SOTA_STATISTICAL_POLICY_SHA256,
        "resource_budget": resource_budget,
        "external_resources_sha256": sha("f8s6:external-resources"),
        "pretraining_sha256": sha("f8s6:pretraining"),
        "evaluation_date": evaluation_date,
        "frozen_at": frozen_at,
    }
    raw.update(updates)
    return k.ProtocolSignature.model_validate(raw)


def build_replicates(
    method_id: str,
    scores: tuple[float, ...],
    *,
    partition_namespace: str = "paired-v1",
) -> tuple[k.BenchmarkReplicateScore, ...]:
    return tuple(
        k.BenchmarkReplicateScore(
            ordinal=index,
            replicate_id=f"replicate:{index:02d}",
            evaluation_partition_sha256=sha(f"f8s6:{partition_namespace}:partition:{index:02d}"),
            score=score,
            execution_receipt_sha256=sha(f"f8s6:{method_id}:execution:{index:02d}"),
            prediction_artifact_sha256=sha(f"f8s6:{method_id}:prediction:{index:02d}"),
        )
        for index, score in enumerate(scores)
    )


def issue_result(
    *,
    result_id: str,
    protocol: k.ProtocolSignature,
    evaluator: k.SOTAEvaluatorManifest,
    scores: tuple[float, ...],
    method_id: str,
    partition_namespace: str = "paired-v1",
) -> k.SignedBenchmarkResultReceipt:
    return k.issue_benchmark_result_receipt(
        result_id=result_id,
        protocol=protocol,
        replicates=build_replicates(
            method_id,
            scores,
            partition_namespace=partition_namespace,
        ),
        evaluator_manifest=evaluator,
        receipt_key=RESULT_RECEIPT_KEY,
        completed_at=_time("2025-08-12T00:00:00Z"),
    )


async def build_f8s6_fixture_async(tmp_path) -> dict[str, Any]:
    live = await build_f8s5_live_fixture(tmp_path / "f8s5", novelty_kind="strong")
    direction = build_f8s5_direction_fixture(live)
    gate = direction["gate"]

    dataset = k.DatasetVersion(
        dataset_id="f8s6-frontier-benchmark",
        canonical_name="F8-S6 Frontier Benchmark",
        version_id="2025.08-frozen",
        content_sha256=sha("f8s6:dataset-content"),
        schema_sha256=sha("f8s6:dataset-schema"),
        license_id="CC-BY-4.0",
        source_url="https://example.org/f8s6/frontier-benchmark",
        released_at=_time("2025-01-01T00:00:00Z"),
        observed_at=_time("2025-02-01T00:00:00Z"),
    )
    metric = k.MetricDefinition(
        metric_id="f8s6-error-rate",
        canonical_name="Frozen paired error rate",
        formula_sha256=sha("f8s6:error-rate-formula"),
        aggregation_sha256=k.SOTA_REPLICATE_AGGREGATION_POLICY_SHA256,
        direction=k.MetricDirection.LOWER_IS_BETTER,
        reporting_unit="fraction",
        valid_minimum=0.0,
        valid_maximum=2.0,
    )
    budget = k.ResourceBudgetSignature(
        compute_policy_sha256=sha("f8s6:compute-policy"),
        data_budget_sha256=sha("f8s6:data-budget"),
        hardware_sha256=sha("f8s6:hardware"),
        accelerator_hours=10.0,
        wall_clock_hours=12.0,
        maximum_cost_usd=100.0,
    )
    evaluator = k.SOTAEvaluatorManifest(
        evaluator_id="f8s6-independent-evaluator-v1",
        evaluator_code_sha256=sha("f8s6:evaluator-code"),
        score_parser_sha256=sha("f8s6:score-parser"),
        aggregation_policy_sha256=k.SOTA_REPLICATE_AGGREGATION_POLICY_SHA256,
        statistical_policy_sha256=k.SOTA_STATISTICAL_POLICY_SHA256,
        minimum_replicates=10,
        receipt_key_id="f8s6-benchmark-key-v1",
        frozen_at=_time("2025-08-10T00:30:00Z"),
    )

    references = []
    reference_protocols = []
    corpus = gate.novelty_decision.coverage.ingestion_bundle.corpus
    corpus_spans_by_paper = {span.paper_snapshot_sha256: span.span_sha256 for span in corpus.spans}
    for index in range(1, 4):
        protocol = build_protocol(
            protocol_id=f"f8s6-reference-protocol-{index}",
            method_id=f"reference-{index}",
            dataset=dataset,
            metric=metric,
            resource_budget=budget,
            frozen_at=_time(f"2025-08-0{5 + index}T00:00:00Z"),
            evaluation_date=_time("2025-08-09T00:00:00Z"),
        )
        reference_protocols.append(protocol)
        references.append(
            k.SOTAReferenceEntry(
                reference_id=f"reference:{index:02d}",
                kind=(
                    k.SOTAReferenceKind.OFFICIAL_LEADERBOARD
                    if index == 1
                    else k.SOTAReferenceKind.PEER_REVIEWED_RESULT
                    if index == 2
                    else k.SOTAReferenceKind.STRONG_BASELINE
                ),
                protocol=protocol,
                source_paper_snapshot_sha256=corpus.papers[index - 1].snapshot_sha256,
                result_evidence_span_sha256s=(
                    corpus_spans_by_paper[corpus.papers[index - 1].snapshot_sha256],
                ),
                selection_evidence_sha256=sha(f"f8s6:reference-selection:{index}"),
                independent_reviewer_principal_sha256=sha(f"f8s6:reference-reviewer:{index}"),
                review_receipt_sha256=sha(f"f8s6:reference-review:{index}"),
                selected_at=_time("2025-08-10T01:00:00Z"),
            )
        )

    registry = k.build_sota_reference_registry(
        registry_id="f8s6-sealed-reference-registry-v1",
        direction_gate=gate,
        selection_protocol_sha256=sha("f8s6:reference-selection-protocol"),
        selector_reviewer_principal_sha256s=tuple(
            sorted((sha("f8s6:selector:a"), sha("f8s6:selector:b")))
        ),
        references=tuple(references),
        evidence_cutoff=_time("2025-08-10T02:00:00Z"),
        sealed_at=_time("2025-08-10T03:00:00Z"),
    )
    policy = k.SOTAComparisonPolicy(
        policy_id="f8s6-comparison-policy-v1",
        minimum_references=3,
        minimum_replicates=10,
        minimum_practical_improvement=0.05,
        aggregation_policy_sha256=k.SOTA_REPLICATE_AGGREGATION_POLICY_SHA256,
        statistical_policy_sha256=k.SOTA_STATISTICAL_POLICY_SHA256,
        evaluator_manifest_sha256=evaluator.manifest_sha256,
        frozen_at=_time("2025-08-10T04:00:00Z"),
    )
    candidate_protocol = build_protocol(
        protocol_id="f8s6-candidate-protocol-v1",
        method_id="candidate",
        dataset=dataset,
        metric=metric,
        resource_budget=budget,
        frozen_at=_time("2025-08-10T05:00:00Z"),
        evaluation_date=_time("2025-08-11T00:00:00Z"),
    )
    candidate_scores = tuple(0.79 + index * 0.002 for index in range(10))
    candidate_result = issue_result(
        result_id="f8s6-result-candidate",
        protocol=candidate_protocol,
        evaluator=evaluator,
        scores=candidate_scores,
        method_id="candidate",
    )
    reference_results = tuple(
        issue_result(
            result_id=f"f8s6-result-reference-{index}",
            protocol=protocol,
            evaluator=evaluator,
            scores=tuple(score + 0.10 * index for score in candidate_scores),
            method_id=f"reference-{index}",
        )
        for index, protocol in enumerate(reference_protocols, start=1)
    )
    campaign = k.build_sota_evaluation_campaign(
        campaign_id="f8s6-sota-campaign-v1",
        direction_gate=gate,
        registry=registry,
        policy=policy,
        evaluator_manifest=evaluator,
        candidate_protocol=candidate_protocol,
        candidate_result=candidate_result,
        reference_results=reference_results,
        receipt_key=RESULT_RECEIPT_KEY,
        generated_at=_time("2025-08-13T00:00:00Z"),
    )
    return {
        "live": live,
        "direction": direction,
        "gate": gate,
        "dataset": dataset,
        "metric": metric,
        "budget": budget,
        "evaluator": evaluator,
        "reference_protocols": tuple(reference_protocols),
        "references": tuple(references),
        "registry": registry,
        "policy": policy,
        "candidate_protocol": candidate_protocol,
        "candidate_result": candidate_result,
        "reference_results": reference_results,
        "campaign": campaign,
        "receipt_key": RESULT_RECEIPT_KEY,
    }


def build_f8s6_fixture(tmp_path) -> dict[str, Any]:
    return asyncio.run(build_f8s6_fixture_async(tmp_path))
