from __future__ import annotations

from datetime import timedelta
from typing import Any

import aletheia.epistemics as e

from .f9s2_fixtures import StepClock, digest


class StaticCausalAuthor:
    def __init__(self, manifest: e.CausalContractAuthorManifest, output: object) -> None:
        self._manifest = manifest
        self.output = output
        self.calls = 0
        self.received: dict[str, object] | None = None

    @property
    def manifest(self) -> e.CausalContractAuthorManifest:
        return self._manifest

    async def author(self, **inputs: object) -> object:
        self.calls += 1
        self.received = dict(inputs)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


class StaticCausalReviewer:
    def __init__(self, manifest: e.CausalAssumptionReviewerManifest, output: object) -> None:
        self._manifest = manifest
        self.output = output
        self.calls = 0
        self.received: dict[str, object] | None = None

    @property
    def manifest(self) -> e.CausalAssumptionReviewerManifest:
        return self._manifest

    async def review(self, **inputs: object) -> object:
        self.calls += 1
        self.received = dict(inputs)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def build_causal_manifests(
    source_campaign: e.HypothesisGenerationCampaign,
    *,
    author_principal: str | None = None,
    reviewer_principal: str | None = None,
) -> tuple[
    e.CausalAuditPolicy,
    e.CausalContractAuthorManifest,
    e.CausalAssumptionReviewerManifest,
]:
    frozen_at = source_campaign.generated_at + timedelta(minutes=10)
    author = e.CausalContractAuthorManifest(
        author_id="f9s3-deterministic-causal-contract-author-v1",
        runtime=e.CausalAdapterRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s3:author-adapter-code"),
        parser_sha256=digest("f9s3:author-parser"),
        output_schema_sha256=e.CAUSAL_CONTRACT_OUTPUT_SCHEMA_SHA256,
        author_principal_sha256=author_principal or digest("f9s3:author-principal"),
        transport_policy="none",
        frozen_at=frozen_at,
    )
    reviewer = e.CausalAssumptionReviewerManifest(
        reviewer_id="f9s3-independent-identification-reviewer-v1",
        runtime=e.CausalAdapterRuntime.DETERMINISTIC,
        adapter_code_sha256=digest("f9s3:reviewer-adapter-code"),
        parser_sha256=digest("f9s3:reviewer-parser"),
        output_schema_sha256=e.CAUSAL_REVIEW_OUTPUT_SCHEMA_SHA256,
        reviewer_principal_sha256=(
            reviewer_principal or digest("f9s3:reviewer-principal")
        ),
        transport_policy="none",
        frozen_at=frozen_at,
    )
    policy = e.CausalAuditPolicy(
        policy_id="f9s3-backdoor-identification-audit-policy-v1",
        harness_principal_sha256=digest("f9s3:trusted-harness-principal"),
        frozen_at=frozen_at,
    )
    return policy, author, reviewer


def _roles(*roles: e.CausalVariableRole) -> tuple[e.CausalVariableRole, ...]:
    return tuple(sorted(roles, key=lambda item: item.value))


def _assumption(
    *,
    assumption_id: str,
    kind: e.IdentificationAssumptionKind,
    hypothesis_ids: tuple[str, ...],
    variable_ids: tuple[str, ...],
    grounding: tuple[str, ...],
) -> e.IdentificationAssumption:
    return e.IdentificationAssumption(
        assumption_id=assumption_id,
        kind=kind,
        statement=f"The {kind.value.replace('_', ' ')} condition holds for this estimand.",
        risk_if_violated=f"Violation invalidates the {kind.value} identification argument.",
        applies_to_hypothesis_ids=hypothesis_ids,
        variable_ids=tuple(sorted(variable_ids)),
        grounding_claim_sha256s=tuple(sorted(grounding)),
    )


def _edge(
    *,
    edge_id: str,
    source: str,
    target: str,
    grounding: tuple[str, ...],
) -> e.CausalEdge:
    return e.CausalEdge(
        edge_id=edge_id,
        source_variable_id=source,
        target_variable_id=target,
        mechanism=f"{source} changes {target} under the stated temporal mechanism.",
        assumption_ids=("assumption.exchangeability", "assumption.temporal_order"),
        grounding_claim_sha256s=tuple(sorted(grounding)),
    )


def build_causal_contract_batch(
    *,
    source_campaign: e.HypothesisGenerationCampaign,
    request: e.CausalContractRequest,
    author_manifest: e.CausalContractAuthorManifest,
    latent_confounding: bool = False,
    omit_batch_adjustment: bool = False,
    strategy: e.IdentificationStrategy = e.IdentificationStrategy.BACKDOOR_ADJUSTMENT,
) -> e.CausalContractBatch:
    snapshot = source_campaign.world_model_snapshot
    assert snapshot is not None
    candidate = source_campaign.request.candidate_claim_sha256
    accepted = source_campaign.direction_gate.novelty_decision.coverage.prior_art_resolution.accepted
    prior = next(
        item.relation.prior_claim_sha256
        for item in accepted
        if item.relation.candidate_claim_sha256 == candidate
    )
    prediction = snapshot.predictions[0]
    hypothesis_ids = tuple(sorted(item.hypothesis_id for item in snapshot.hypotheses))
    variables = [
        e.CausalVariable(
            variable_id="batch_context",
            label="Batch context",
            definition="Observed batch composition that can affect assignment and response.",
            roles=_roles(e.CausalVariableRole.CONFOUNDER, e.CausalVariableRole.COVARIATE),
            value_kind=e.CausalValueKind.CATEGORICAL,
            observability=e.CausalObservability.OBSERVED,
            intervenability=e.CausalIntervenability.INDIRECT,
            observable_id="batch.context",
            measurement_protocol_sha256=digest("f9s3:batch-measurement-protocol"),
            grounding_claim_sha256s=tuple(sorted((candidate, prior))),
        ),
        e.CausalVariable(
            variable_id="endpoint_measurement",
            label="Measured response class",
            definition="Observed endpoint used by every F9-S2 discriminating prediction.",
            roles=_roles(e.CausalVariableRole.MEASUREMENT),
            value_kind=e.CausalValueKind.CATEGORICAL,
            observability=e.CausalObservability.OBSERVED,
            intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
            observable_id=prediction.observable_id,
            measurement_protocol_sha256=prediction.measurement_protocol_sha256,
            grounding_claim_sha256s=(candidate,),
        ),
        e.CausalVariable(
            variable_id="intervention",
            label="Candidate intervention",
            definition="The controlled intervention whose total effect is under study.",
            roles=_roles(e.CausalVariableRole.EXPOSURE),
            value_kind=e.CausalValueKind.BINARY,
            observability=e.CausalObservability.OBSERVED,
            intervenability=e.CausalIntervenability.DIRECT,
            observable_id="intervention.assignment",
            measurement_protocol_sha256=digest("f9s3:assignment-protocol"),
            grounding_claim_sha256s=(candidate,),
        ),
        e.CausalVariable(
            variable_id="mediator_alternative",
            label="Alternative pathway mediator",
            definition="Latent mediator representing the prior-art-linked alternative pathway.",
            roles=_roles(e.CausalVariableRole.MEDIATOR),
            value_kind=e.CausalValueKind.CONTINUOUS,
            observability=e.CausalObservability.LATENT,
            intervenability=e.CausalIntervenability.INDIRECT,
            grounding_claim_sha256s=(prior,),
        ),
        e.CausalVariable(
            variable_id="mediator_primary",
            label="Primary pathway mediator",
            definition="Latent mediator representing the candidate primary pathway.",
            roles=_roles(e.CausalVariableRole.MEDIATOR),
            value_kind=e.CausalValueKind.CONTINUOUS,
            observability=e.CausalObservability.LATENT,
            intervenability=e.CausalIntervenability.INDIRECT,
            grounding_claim_sha256s=(candidate,),
        ),
        e.CausalVariable(
            variable_id="true_response",
            label="True response",
            definition="Latent construct measured by the preregistered endpoint.",
            roles=_roles(e.CausalVariableRole.OUTCOME),
            value_kind=e.CausalValueKind.CATEGORICAL,
            observability=e.CausalObservability.LATENT,
            intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
            grounding_claim_sha256s=(candidate,),
        ),
    ]
    if latent_confounding:
        variables.append(
            e.CausalVariable(
                variable_id="unmeasured_context",
                label="Unmeasured context",
                definition="A latent common cause of intervention assignment and response.",
                roles=_roles(e.CausalVariableRole.CONFOUNDER),
                value_kind=e.CausalValueKind.CATEGORICAL,
                observability=e.CausalObservability.LATENT,
                intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
                grounding_claim_sha256s=(prior,),
            )
        )
    variables.sort(key=lambda item: item.variable_id)
    shared_variable_ids = (
        "batch_context",
        "intervention",
        "true_response",
    )
    assumptions = [
        _assumption(
            assumption_id="assumption.consistency",
            kind=e.IdentificationAssumptionKind.CONSISTENCY,
            hypothesis_ids=hypothesis_ids,
            variable_ids=("intervention", "true_response"),
            grounding=(candidate,),
        ),
        _assumption(
            assumption_id="assumption.exchangeability",
            kind=e.IdentificationAssumptionKind.EXCHANGEABILITY,
            hypothesis_ids=hypothesis_ids,
            variable_ids=shared_variable_ids,
            grounding=tuple(sorted((candidate, prior))),
        ),
        _assumption(
            assumption_id="assumption.measurement_validity",
            kind=e.IdentificationAssumptionKind.MEASUREMENT_VALIDITY,
            hypothesis_ids=hypothesis_ids,
            variable_ids=("endpoint_measurement", "true_response"),
            grounding=(candidate,),
        ),
        _assumption(
            assumption_id="assumption.no_interference",
            kind=e.IdentificationAssumptionKind.NO_INTERFERENCE,
            hypothesis_ids=hypothesis_ids,
            variable_ids=("intervention", "true_response"),
            grounding=(candidate,),
        ),
        _assumption(
            assumption_id="assumption.positivity",
            kind=e.IdentificationAssumptionKind.POSITIVITY,
            hypothesis_ids=hypothesis_ids,
            variable_ids=("batch_context", "intervention"),
            grounding=tuple(sorted((candidate, prior))),
        ),
        _assumption(
            assumption_id="assumption.temporal_order",
            kind=e.IdentificationAssumptionKind.TEMPORAL_ORDER,
            hypothesis_ids=hypothesis_ids,
            variable_ids=(
                "intervention",
                "mediator_alternative",
                "mediator_primary",
                "true_response",
            ),
            grounding=tuple(sorted((candidate, prior))),
        ),
    ]
    if request.proposed_evidence_kind is e.CausalEvidenceKind.SIMULATION_INTERVENTION:
        assumptions.append(
            _assumption(
                assumption_id="assumption.model_correctness",
                kind=e.IdentificationAssumptionKind.MODEL_CORRECTNESS,
                hypothesis_ids=hypothesis_ids,
                variable_ids=("intervention", "true_response"),
                grounding=(candidate,),
            )
        )
    assumptions.sort(key=lambda item: item.assumption_id)

    graph_items: list[e.HypothesisCausalGraph] = []
    for hypothesis in snapshot.hypotheses:
        common_edges = [
            _edge(
                edge_id=f"edge.{hypothesis.hypothesis_id}.batch_to_intervention",
                source="batch_context",
                target="intervention",
                grounding=tuple(sorted((candidate, prior))),
            ),
            _edge(
                edge_id=f"edge.{hypothesis.hypothesis_id}.batch_to_response",
                source="batch_context",
                target="true_response",
                grounding=tuple(sorted((candidate, prior))),
            ),
        ]
        if hypothesis.role is e.HypothesisRole.PRIMARY:
            common_edges.extend(
                (
                    _edge(
                        edge_id=f"edge.{hypothesis.hypothesis_id}.intervention_to_primary",
                        source="intervention",
                        target="mediator_primary",
                        grounding=(candidate,),
                    ),
                    _edge(
                        edge_id=f"edge.{hypothesis.hypothesis_id}.primary_to_response",
                        source="mediator_primary",
                        target="true_response",
                        grounding=(candidate,),
                    ),
                )
            )
            graph_grounding = (candidate,)
        elif hypothesis.role is e.HypothesisRole.ALTERNATIVE:
            common_edges.extend(
                (
                    _edge(
                        edge_id=f"edge.{hypothesis.hypothesis_id}.intervention_to_alternative",
                        source="intervention",
                        target="mediator_alternative",
                        grounding=tuple(sorted((candidate, prior))),
                    ),
                    _edge(
                        edge_id=f"edge.{hypothesis.hypothesis_id}.alternative_to_response",
                        source="mediator_alternative",
                        target="true_response",
                        grounding=(prior,),
                    ),
                )
            )
            graph_grounding = tuple(sorted((candidate, prior)))
        else:
            graph_grounding = (candidate,)
        common_edges.sort(key=lambda item: item.edge_id)
        latent = ()
        if latent_confounding:
            latent = (
                e.LatentConfounder(
                    confounder_id=f"latent.{hypothesis.hypothesis_id}.context",
                    variable_id="unmeasured_context",
                    affected_variable_ids=("intervention", "true_response"),
                    assumption_id="assumption.exchangeability",
                    grounding_claim_sha256s=(prior,),
                ),
            )
        graph_items.append(
            e.HypothesisCausalGraph(
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_version_sha256=hypothesis.hypothesis_sha256,
                edges=tuple(common_edges),
                latent_confounders=latent,
                grounding_claim_sha256s=graph_grounding,
            )
        )
    graph_items.sort(key=lambda item: item.hypothesis_id)
    contract = e.CausalContract(
        world_model_snapshot_sha256=snapshot.snapshot_sha256,
        question_sha256=snapshot.question.question_sha256,
        variables=tuple(variables),
        assumptions=tuple(assumptions),
        measurement_processes=(
            e.MeasurementProcess(
                process_id="measurement.outcome",
                construct_variable_id="true_response",
                indicator_variable_id="endpoint_measurement",
                measurement_protocol_sha256=prediction.measurement_protocol_sha256,
                error_model_sha256=digest("f9s3:measurement-error-model"),
                validity_assumption_id="assumption.measurement_validity",
            ),
        ),
        estimand=e.CausalEstimand(
            estimand_id="estimand.total_effect",
            exposure_variable_id="intervention",
            outcome_variable_id="true_response",
            intervention_levels=("control", "active"),
            effect_scale=e.CausalEffectScale.DISTRIBUTION_SHIFT,
            target_population_sha256=digest("f9s3:target-population"),
            identification_strategy=strategy,
            adjustment_variable_ids=("batch_context",) if not omit_batch_adjustment else (),
            proposed_evidence_kind=request.proposed_evidence_kind,
        ),
        outcome_measurement_process_id="measurement.outcome",
        hypothesis_graphs=tuple(graph_items),
    )
    return e.CausalContractBatch(
        request_sha256=request.request_sha256,
        author_manifest_sha256=author_manifest.manifest_sha256,
        contract=contract,
        completed_at=request.issued_at + timedelta(hours=1),
    )


def build_causal_review_batch(
    *,
    contract_batch: e.CausalContractBatch,
    reviewer_manifest: e.CausalAssumptionReviewerManifest,
    decisions: dict[str, e.AssumptionReviewDecision] | None = None,
    confidences: dict[str, float] | None = None,
) -> e.CausalAssumptionReviewBatch:
    decisions = decisions or {}
    confidences = confidences or {}
    completed_at = contract_batch.completed_at + timedelta(hours=1)
    reviews = tuple(
        e.CausalAssumptionReview(
            assumption_id=item.assumption_id,
            assumption_sha256=item.assumption_sha256,
            decision=decisions.get(item.assumption_id, e.AssumptionReviewDecision.ACCEPT),
            confidence=confidences.get(item.assumption_id, 0.97),
            rationale_sha256=digest(f"f9s3:{item.assumption_id}:review-rationale"),
            evidence_claim_sha256s=(
                item.grounding_claim_sha256s
                if decisions.get(item.assumption_id, e.AssumptionReviewDecision.ACCEPT)
                is e.AssumptionReviewDecision.ACCEPT
                else ()
            ),
            completed_at=completed_at,
        )
        for item in contract_batch.contract.assumptions
    )
    return e.CausalAssumptionReviewBatch(
        causal_contract_batch_sha256=contract_batch.batch_sha256,
        reviewer_manifest_sha256=reviewer_manifest.manifest_sha256,
        reviews=reviews,
        completed_at=completed_at,
    )


def build_f9s3_fixture(
    source_campaign: e.HypothesisGenerationCampaign,
    *,
    latent_confounding: bool = False,
    omit_batch_adjustment: bool = False,
    strategy: e.IdentificationStrategy = e.IdentificationStrategy.BACKDOOR_ADJUSTMENT,
    evidence_kind: e.CausalEvidenceKind = e.CausalEvidenceKind.CONTROLLED_INTERVENTION,
    decisions: dict[str, e.AssumptionReviewDecision] | None = None,
    confidences: dict[str, float] | None = None,
) -> dict[str, Any]:
    policy, author_manifest, reviewer_manifest = build_causal_manifests(source_campaign)
    request = e.build_causal_contract_request(
        request_id="f9s3-causal-contract-request-v1",
        source_campaign=source_campaign,
        proposed_evidence_kind=evidence_kind,
        policy=policy,
        author_manifest=author_manifest,
        reviewer_manifest=reviewer_manifest,
        issued_at=source_campaign.generated_at + timedelta(hours=1),
    )
    contract_batch = build_causal_contract_batch(
        source_campaign=source_campaign,
        request=request,
        author_manifest=author_manifest,
        latent_confounding=latent_confounding,
        omit_batch_adjustment=omit_batch_adjustment,
        strategy=strategy,
    )
    review_batch = build_causal_review_batch(
        contract_batch=contract_batch,
        reviewer_manifest=reviewer_manifest,
        decisions=decisions,
        confidences=confidences,
    )
    return {
        "source_campaign": source_campaign,
        "policy": policy,
        "author_manifest": author_manifest,
        "reviewer_manifest": reviewer_manifest,
        "request": request,
        "contract_batch": contract_batch,
        "review_batch": review_batch,
        "author": StaticCausalAuthor(author_manifest, contract_batch),
        "reviewer": StaticCausalReviewer(reviewer_manifest, review_batch),
        "clock": StepClock(review_batch.completed_at + timedelta(hours=1)),
    }


__all__ = [
    "StaticCausalAuthor",
    "StaticCausalReviewer",
    "build_causal_contract_batch",
    "build_causal_manifests",
    "build_causal_review_batch",
    "build_f9s3_fixture",
]
