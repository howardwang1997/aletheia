from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

import aletheia.epistemics as e
from aletheia.knowledge.novelty_decision import ResearchDirectionGate


RUN_ID = "9" * 32


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_question_id(value: str) -> str:
    return f"rq_{digest(value)[:32]}"


def revalidate(model_type, model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return model_type.model_validate(payload)


class StepClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class StaticGenerator:
    def __init__(self, manifest: e.HypothesisGeneratorManifest, output: object) -> None:
        self._manifest = manifest
        self.output = output
        self.calls = 0
        self.received: dict[str, object] | None = None

    @property
    def manifest(self) -> e.HypothesisGeneratorManifest:
        return self._manifest

    async def generate(self, **inputs: object) -> object:
        self.calls += 1
        self.received = dict(inputs)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


class StaticDeduplicator:
    def __init__(self, manifest: e.HypothesisDeduplicatorManifest, output: object) -> None:
        self._manifest = manifest
        self.output = output
        self.calls = 0
        self.received: dict[str, object] | None = None

    @property
    def manifest(self) -> e.HypothesisDeduplicatorManifest:
        return self._manifest

    async def compare(self, **inputs: object) -> object:
        self.calls += 1
        self.received = dict(inputs)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def build_manifests(
    gate: ResearchDirectionGate,
    *,
    generator_principal: str | None = None,
    reviewer_principal: str | None = None,
) -> tuple[
    e.HypothesisGenerationPolicy,
    e.HypothesisGeneratorManifest,
    e.HypothesisDeduplicatorManifest,
]:
    frozen_at = gate.decided_at + timedelta(minutes=10)
    generator = e.HypothesisGeneratorManifest(
        generator_id="f9s2-deterministic-competing-hypothesis-generator-v1",
        runtime=e.HypothesisGeneratorRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s2:generator-adapter-code"),
        parser_sha256=digest("f9s2:generator-parser"),
        output_schema_sha256=e.HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256,
        generator_principal_sha256=(
            generator_principal or digest("f9s2:generator-principal")
        ),
        maximum_hypotheses=16,
        transport_policy="none",
        frozen_at=frozen_at,
    )
    deduplicator = e.HypothesisDeduplicatorManifest(
        deduplicator_id="f9s2-independent-semantic-deduplicator-v1",
        runtime=e.HypothesisGeneratorRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s2:deduplicator-adapter-code"),
        parser_sha256=digest("f9s2:deduplicator-parser"),
        output_schema_sha256=e.HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256,
        reviewer_principal_sha256=(
            reviewer_principal or digest("f9s2:deduplicator-principal")
        ),
        semantic_normalizer_sha256=e.SEMANTIC_NORMALIZER_SHA256,
        transport_policy="none",
        frozen_at=frozen_at,
    )
    policy = e.HypothesisGenerationPolicy(
        policy_id="f9s2-competing-hypothesis-admission-policy-v1",
        harness_principal_sha256=digest("f9s2:trusted-harness-principal"),
        frozen_at=frozen_at,
    )
    return policy, generator, deduplicator


def _grounding(gate: ResearchDirectionGate) -> tuple[str, str]:
    candidate_sha256 = gate.novelty_decision.evidence_package.candidate_claim_sha256
    accepted = gate.novelty_decision.coverage.prior_art_resolution.accepted
    if not accepted:
        raise ValueError("F9-S2 fixture requires accepted prior art")
    prior_sha256 = next(
        item.relation.prior_claim_sha256
        for item in accepted
        if item.relation.candidate_claim_sha256 == candidate_sha256
    )
    return candidate_sha256, prior_sha256


def build_generation_batch(
    *,
    gate: ResearchDirectionGate,
    request: e.HypothesisGenerationRequest,
    generator_manifest: e.HypothesisGeneratorManifest,
    duplicate_alternative: bool = False,
) -> e.HypothesisGenerationBatch:
    candidate_sha256, prior_sha256 = _grounding(gate)
    hypothesis_specs: list[tuple[str, e.HypothesisRole, str, str | None, tuple[str, ...], str]] = [
        (
            "alternative_a",
            e.HypothesisRole.ALTERNATIVE,
            "A prior-art-linked batch pathway produces the response.",
            "Batch composition activates pathway B and changes the endpoint.",
            tuple(sorted((candidate_sha256, prior_sha256))),
            "alternative_pattern",
        ),
        (
            "null_h0",
            e.HypothesisRole.NULL,
            "The intervention has no response beyond the preregistered noise process.",
            None,
            (candidate_sha256,),
            "null_pattern",
        ),
        (
            "primary_a",
            e.HypothesisRole.PRIMARY,
            "The candidate mechanism produces the response through pathway A.",
            "The intervention activates mediator A, which changes the endpoint.",
            (candidate_sha256,),
            "primary_pattern",
        ),
    ]
    if duplicate_alternative:
        hypothesis_specs.append(
            (
                "alternative_b",
                e.HypothesisRole.ALTERNATIVE,
                "A prior-art-linked batch pathway produces the response.",
                "Batch composition activates pathway B and changes the endpoint.",
                tuple(sorted((candidate_sha256, prior_sha256))),
                "alternative_pattern",
            )
        )
    hypothesis_specs.sort(key=lambda item: item[0])
    local_ids = tuple(item[0] for item in hypothesis_specs)
    outcome_space = ("alternative_pattern", "null_pattern", "primary_pattern")
    protocol_sha256 = digest("f9s2:shared-measurement-protocol")
    hypotheses: list[e.HypothesisDraft] = []
    for local_id, role, statement, mechanism, grounding, outcome in hypothesis_specs:
        targets = tuple(sorted(set(local_ids) - {local_id}))
        hypotheses.append(
            e.HypothesisDraft(
                local_hypothesis_id=local_id,
                role=role,
                statement=statement,
                mechanism=mechanism,
                rationale_sha256=digest(f"f9s2:{local_id}:rationale"),
                grounding_claim_sha256s=grounding,
                assumptions=(
                    e.AssumptionDraft(
                        local_assumption_id=f"assumption.{local_id}",
                        kind=e.AssumptionKind.MEASUREMENT,
                        statement="The endpoint resolves the preregistered response classes.",
                        risk_if_violated="The mechanism cannot be identified from this endpoint.",
                        grounding_claim_sha256s=grounding,
                    ),
                ),
                predictions=(
                    e.PredictionDraft(
                        local_prediction_id=f"prediction.{local_id}",
                        observable_id="endpoint.response_class",
                        outcome_space=outcome_space,
                        expected_outcome=outcome,
                        direction=(
                            e.PredictionDirection.NO_CHANGE
                            if role is e.HypothesisRole.NULL
                            else e.PredictionDirection.QUALITATIVE
                        ),
                        discriminates_from_local_hypothesis_ids=targets,
                        measurement_protocol_sha256=protocol_sha256,
                    ),
                ),
            )
        )
    return e.HypothesisGenerationBatch(
        request_sha256=request.request_sha256,
        generator_manifest_sha256=generator_manifest.manifest_sha256,
        question=e.QuestionDraft(
            statement="Which causal mechanism explains the candidate response?",
            kind=e.ResearchQuestionKind.MECHANISM,
        ),
        hypotheses=tuple(hypotheses),
        completed_at=request.issued_at + timedelta(hours=1),
    )


def build_deduplication_batch(
    *,
    generation_batch: e.HypothesisGenerationBatch,
    deduplicator_manifest: e.HypothesisDeduplicatorManifest,
    relations: dict[tuple[str, str], e.SemanticHypothesisRelation] | None = None,
    confidences: dict[tuple[str, str], float] | None = None,
) -> e.HypothesisDeduplicationBatch:
    relations = relations or {}
    confidences = confidences or {}
    drafts = {item.local_hypothesis_id: item for item in generation_batch.hypotheses}
    judgments = tuple(
        e.HypothesisPairJudgment(
            left_local_hypothesis_id=left,
            right_local_hypothesis_id=right,
            left_draft_sha256=drafts[left].draft_sha256,
            right_draft_sha256=drafts[right].draft_sha256,
            relation=relations.get((left, right), e.SemanticHypothesisRelation.DISTINCT),
            confidence=confidences.get((left, right), 0.99),
            rationale_sha256=digest(f"f9s2:{left}:{right}:semantic-rationale"),
            completed_at=generation_batch.completed_at + timedelta(hours=1),
        )
        for left, right in combinations(sorted(drafts), 2)
    )
    return e.HypothesisDeduplicationBatch(
        generation_batch_sha256=generation_batch.batch_sha256,
        deduplicator_manifest_sha256=deduplicator_manifest.manifest_sha256,
        judgments=judgments,
        completed_at=generation_batch.completed_at + timedelta(hours=1),
    )


def build_f9s2_fixture(
    gate: ResearchDirectionGate,
    *,
    duplicate_alternative: bool = False,
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    policy, generator_manifest, deduplicator_manifest = build_manifests(gate)
    request = e.build_hypothesis_generation_request(
        request_id="f9s2-grounded-generation-request-v1",
        run_id=run_id,
        question_id=stable_question_id(f"f9s2:question:{run_id}"),
        direction_gate=gate,
        policy=policy,
        generator_manifest=generator_manifest,
        deduplicator_manifest=deduplicator_manifest,
        issued_at=gate.decided_at + timedelta(hours=1),
    )
    generation_batch = build_generation_batch(
        gate=gate,
        request=request,
        generator_manifest=generator_manifest,
        duplicate_alternative=duplicate_alternative,
    )
    relations: dict[tuple[str, str], e.SemanticHypothesisRelation] = {}
    if duplicate_alternative:
        relations[("alternative_a", "alternative_b")] = (
            e.SemanticHypothesisRelation.EQUIVALENT
        )
    deduplication_batch = build_deduplication_batch(
        generation_batch=generation_batch,
        deduplicator_manifest=deduplicator_manifest,
        relations=relations,
    )
    generator = StaticGenerator(generator_manifest, generation_batch)
    deduplicator = StaticDeduplicator(deduplicator_manifest, deduplication_batch)
    return {
        "gate": gate,
        "policy": policy,
        "generator_manifest": generator_manifest,
        "deduplicator_manifest": deduplicator_manifest,
        "request": request,
        "generation_batch": generation_batch,
        "deduplication_batch": deduplication_batch,
        "generator": generator,
        "deduplicator": deduplicator,
        "clock": StepClock(deduplication_batch.completed_at + timedelta(hours=1)),
    }


__all__ = [
    "RUN_ID",
    "StaticDeduplicator",
    "StaticGenerator",
    "StepClock",
    "build_deduplication_batch",
    "build_f9s2_fixture",
    "build_generation_batch",
    "build_manifests",
    "digest",
    "revalidate",
    "stable_question_id",
]
