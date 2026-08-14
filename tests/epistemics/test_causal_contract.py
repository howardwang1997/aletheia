from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

import aletheia.epistemics as e
import aletheia.knowledge as k
from knowledge.f8s5_fixtures import build_f8s5_direction_fixture, build_f8s5_live_fixture

from .f9s2_fixtures import StaticGenerator, build_f9s2_fixture, digest, revalidate
from .f9s3_fixtures import (
    StaticCausalAuthor,
    StaticCausalReviewer,
    build_causal_manifests,
    build_causal_review_batch,
    build_f9s3_fixture,
)


@pytest.fixture(scope="module")
def source_fixture(tmp_path_factory):
    live = asyncio.run(
        build_f8s5_live_fixture(
            tmp_path_factory.mktemp("f9s3-strong"),
            novelty_kind="strong",
        )
    )
    gate = build_f8s5_direction_fixture(live)["gate"]
    parts = build_f9s2_fixture(gate)
    campaign = asyncio.run(
        e.run_competing_hypothesis_generation(
            campaign_id="campaign:f9s3:source-hypotheses",
            direction_gate=parts["gate"],
            policy=parts["policy"],
            request=parts["request"],
            generator=parts["generator"],
            deduplicator=parts["deduplicator"],
            clock=parts["clock"],
        )
    )
    assert campaign.disposition is e.HypothesisGenerationDisposition.READY
    return {"gate": gate, "campaign": campaign}


async def _run(parts, campaign_id: str = "campaign:f9s3:test") -> e.CausalAuditCampaign:
    return await e.run_causal_identification_audit(
        campaign_id=campaign_id,
        source_campaign=parts["source_campaign"],
        policy=parts["policy"],
        request=parts["request"],
        author=parts["author"],
        reviewer=parts["reviewer"],
        clock=parts["clock"],
    )


def _install_contract(
    parts: dict[str, object],
    contract: e.CausalContract,
    *,
    decisions: dict[str, e.AssumptionReviewDecision] | None = None,
    confidences: dict[str, float] | None = None,
) -> None:
    old = parts["contract_batch"]
    batch = revalidate(e.CausalContractBatch, old, contract=contract)
    review = build_causal_review_batch(
        contract_batch=batch,
        reviewer_manifest=parts["reviewer_manifest"],
        decisions=decisions,
        confidences=confidences,
    )
    parts["contract_batch"] = batch
    parts["review_batch"] = review
    parts["author"] = StaticCausalAuthor(parts["author_manifest"], batch)
    parts["reviewer"] = StaticCausalReviewer(parts["reviewer_manifest"], review)
    parts["clock"].current = review.completed_at + timedelta(hours=1)


def _replace_graph(
    contract: e.CausalContract,
    hypothesis_id: str,
    **updates: object,
) -> e.CausalContract:
    graphs = tuple(
        revalidate(e.HypothesisCausalGraph, item, **updates)
        if item.hypothesis_id == hypothesis_id
        else item
        for item in contract.hypothesis_graphs
    )
    return revalidate(
        e.CausalContract,
        contract,
        hypothesis_graphs=tuple(sorted(graphs, key=lambda item: item.hypothesis_id)),
    )


def _hypothesis_by_role(source_campaign, role):
    return next(
        item
        for item in source_campaign.world_model_snapshot.hypotheses
        if item.role is role
    )


@pytest.mark.asyncio
async def test_exact_f9s2_campaign_produces_backdoor_identified_causal_contract(
    source_fixture,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])

    campaign = await _run(parts, "campaign:f9s3:identified")

    assert campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert campaign.claim_ceiling is e.CausalClaimCeiling.CAUSAL_CANDIDATE
    assert campaign.prediction_planning_authorized is True
    assert campaign.blockers == ()
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.IDENTIFIED
        for item in campaign.graph_audits
    )
    assert all(
        item.status is e.AssumptionResolutionStatus.ACCEPTED
        for item in campaign.assumption_resolutions
    )
    null = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.NULL)
    null_audit = next(item for item in campaign.graph_audits if item.hypothesis_id == null.hypothesis_id)
    assert null_audit.directed_exposure_outcome_path == ()
    assert all(
        item.directed_exposure_outcome_path
        for item in campaign.graph_audits
        if item.hypothesis_id != null.hypothesis_id
    )
    assert parts["author"].calls == 1
    assert parts["reviewer"].calls == 1


@pytest.mark.asyncio
async def test_causal_author_receives_exact_f8_f9_inputs_without_observations_or_tools(
    source_fixture,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    campaign = await _run(parts, "campaign:f9s3:input-boundary")
    received = parts["author"].received

    assert campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert parts["request"].observation_access == "none"
    assert parts["author_manifest"].tool_names == ()
    assert parts["reviewer_manifest"].tool_names == ()
    assert received is not None
    assert received["request"] == parts["request"]
    assert received["source_campaign"] == source_fixture["campaign"]
    assert tuple(sorted(item.claim_sha256 for item in received["claims"])) == (
        parts["request"].input_claim_sha256s
    )


def test_causal_schema_hashes_and_independent_reviewer_are_frozen(source_fixture) -> None:
    source = source_fixture["campaign"]
    shared = digest("f9s3:shared-principal")
    policy, author, reviewer = build_causal_manifests(
        source,
        author_principal=shared,
        reviewer_principal=shared,
    )

    assert author.output_schema_sha256 == e.CAUSAL_CONTRACT_OUTPUT_SCHEMA_SHA256
    assert reviewer.output_schema_sha256 == e.CAUSAL_REVIEW_OUTPUT_SCHEMA_SHA256
    with pytest.raises(ValueError, match="must be independent"):
        e.build_causal_contract_request(
            request_id="f9s3-non-independent-request",
            source_campaign=source,
            proposed_evidence_kind=e.CausalEvidenceKind.CONTROLLED_INTERVENTION,
            policy=policy,
            author_manifest=author,
            reviewer_manifest=reviewer,
            issued_at=source.generated_at + timedelta(hours=1),
        )
    with pytest.raises(ValidationError, match="cannot receive tool authority"):
        revalidate(e.CausalContractAuthorManifest, author, tool_names=("observation.read",))

    model_identity = digest("f9s3:shared-model")
    model_author = revalidate(
        e.CausalContractAuthorManifest,
        author,
        runtime=e.CausalAdapterRuntime.MODEL,
        instruction_sha256=digest("f9s3:author-instruction"),
        model_identity_sha256=model_identity,
        transport_policy="model_transport_only",
    )
    model_reviewer = revalidate(
        e.CausalAssumptionReviewerManifest,
        reviewer,
        reviewer_principal_sha256=digest("f9s3:distinct-reviewer-principal"),
        runtime=e.CausalAdapterRuntime.MODEL,
        instruction_sha256=digest("f9s3:reviewer-instruction"),
        model_identity_sha256=model_identity,
        transport_policy="model_transport_only",
    )
    with pytest.raises(ValueError, match="independent model"):
        e.build_causal_contract_request(
            request_id="f9s3-same-model-request",
            source_campaign=source,
            proposed_evidence_kind=e.CausalEvidenceKind.CONTROLLED_INTERVENTION,
            policy=policy,
            author_manifest=model_author,
            reviewer_manifest=model_reviewer,
            issued_at=source.generated_at + timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_blocked_f9s2_campaign_cannot_issue_causal_request(source_fixture) -> None:
    f9 = build_f9s2_fixture(source_fixture["gate"])
    f9["generator"] = StaticGenerator(f9["generator_manifest"], RuntimeError("stopped"))
    blocked = await e.run_competing_hypothesis_generation(
        campaign_id="campaign:f9s3:blocked-source",
        direction_gate=f9["gate"],
        policy=f9["policy"],
        request=f9["request"],
        generator=f9["generator"],
        deduplicator=f9["deduplicator"],
        clock=f9["clock"],
    )
    policy, author, reviewer = build_causal_manifests(blocked)

    with pytest.raises(ValueError, match="ready F9-S2"):
        e.build_causal_contract_request(
            request_id="f9s3-blocked-source-request",
            source_campaign=blocked,
            proposed_evidence_kind=e.CausalEvidenceKind.CONTROLLED_INTERVENTION,
            policy=policy,
            author_manifest=author,
            reviewer_manifest=reviewer,
            issued_at=blocked.generated_at + timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_request_rebinding_fails_before_causal_author_call(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    parts["request"] = revalidate(
        e.CausalContractRequest,
        parts["request"],
        world_model_snapshot_sha256=digest("f9s3:another-snapshot"),
    )

    with pytest.raises(ValueError, match="changed exact world_model_snapshot_sha256"):
        await _run(parts, "campaign:f9s3:rebound")
    assert parts["author"].calls == 0
    assert parts["reviewer"].calls == 0


@pytest.mark.asyncio
async def test_directed_cycle_blocks_before_assumption_reviewer(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    primary = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.PRIMARY)
    contract = parts["contract_batch"].contract
    graph = next(item for item in contract.hypothesis_graphs if item.hypothesis_id == primary.hypothesis_id)
    edge = e.CausalEdge(
        edge_id=f"edge.{primary.hypothesis_id}.response_to_intervention",
        source_variable_id="true_response",
        target_variable_id="intervention",
        mechanism="The outcome feeds back into the already assigned intervention.",
        assumption_ids=("assumption.exchangeability", "assumption.temporal_order"),
        grounding_claim_sha256s=(parts["request"].input_claim_sha256s[0],),
    )
    contract = _replace_graph(
        contract,
        primary.hypothesis_id,
        edges=tuple(sorted((*graph.edges, edge), key=lambda item: item.edge_id)),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:cycle")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any(item.startswith("causal_cycle:") for item in campaign.blockers)
    assert campaign.prediction_planning_authorized is False
    assert parts["reviewer"].calls == 0


@pytest.mark.asyncio
async def test_undefined_edge_variable_blocks_with_auditable_graph(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    primary = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.PRIMARY)
    contract = parts["contract_batch"].contract
    graph = next(item for item in contract.hypothesis_graphs if item.hypothesis_id == primary.hypothesis_id)
    edge = e.CausalEdge(
        edge_id=f"edge.{primary.hypothesis_id}.undefined",
        source_variable_id="intervention",
        target_variable_id="undefined_endpoint",
        mechanism="This malformed edge references an undeclared endpoint.",
        assumption_ids=("assumption.exchangeability",),
        grounding_claim_sha256s=(parts["request"].input_claim_sha256s[0],),
    )
    contract = _replace_graph(
        contract,
        primary.hypothesis_id,
        edges=tuple(sorted((*graph.edges, edge), key=lambda item: item.edge_id)),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:undefined-variable")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("edge_undefined_variable" in item for item in campaign.blockers)
    assert campaign.graph_audits


@pytest.mark.asyncio
async def test_outcome_measurement_must_match_every_f9s2_prediction(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    changed_protocol = digest("f9s3:unbound-measurement-protocol")
    variables = tuple(
        revalidate(
            e.CausalVariable,
            item,
            measurement_protocol_sha256=changed_protocol,
        )
        if item.variable_id == "endpoint_measurement"
        else item
        for item in contract.variables
    )
    process = revalidate(
        e.MeasurementProcess,
        contract.measurement_processes[0],
        measurement_protocol_sha256=changed_protocol,
    )
    contract = revalidate(
        e.CausalContract,
        contract,
        variables=variables,
        measurement_processes=(process,),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:unobservable-endpoint")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any(
        item.startswith("outcome_measurement_not_bound_to_prediction:")
        for item in campaign.blockers
    )


@pytest.mark.asyncio
async def test_missing_required_identification_assumption_blocks_structure(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    assumptions = tuple(
        item
        for item in contract.assumptions
        if item.kind is not e.IdentificationAssumptionKind.POSITIVITY
    )
    contract = revalidate(e.CausalContract, contract, assumptions=assumptions)
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:missing-positivity")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("missing_identification_assumption" in item for item in campaign.blockers)
    assert parts["reviewer"].calls == 0


@pytest.mark.asyncio
async def test_causal_graph_cannot_rebind_hypothesis_version(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    graph = contract.hypothesis_graphs[0]
    contract = _replace_graph(
        contract,
        graph.hypothesis_id,
        hypothesis_version_sha256=digest("f9s3:wrong-hypothesis-version"),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:hypothesis-rebind")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("changed_hypothesis_version" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_unsupported_identification_strategy_is_not_mislabeled_nonidentified(
    source_fixture,
) -> None:
    parts = build_f9s3_fixture(
        source_fixture["campaign"],
        strategy=e.IdentificationStrategy.GENERAL_ID_ALGORITHM,
    )

    campaign = await _run(parts, "campaign:f9s3:unsupported-id")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert "unsupported_identification_strategy:general_id_algorithm" in campaign.blockers
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.UNSUPPORTED_STRATEGY
        for item in campaign.graph_audits
    )


@pytest.mark.asyncio
async def test_open_observed_backdoor_path_bounds_claim_instead_of_asserting_causality(
    source_fixture,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"], omit_batch_adjustment=True)

    campaign = await _run(parts, "campaign:f9s3:open-observed-backdoor")

    assert campaign.disposition is e.CausalAuditDisposition.READY_BOUNDED
    assert campaign.claim_ceiling is e.CausalClaimCeiling.ASSOCIATION_ONLY
    assert campaign.prediction_planning_authorized is True
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.OPEN_BACKDOOR_PATH
        for item in campaign.graph_audits
    )
    assert all(item.open_backdoor_path for item in campaign.graph_audits)


@pytest.mark.asyncio
async def test_backdoor_gold_case_keeps_collider_closed_until_conditioned(source_fixture) -> None:
    def install_collider(parts, *, condition_on_collider: bool) -> None:
        contract = parts["contract_batch"].contract
        candidate = parts["source_campaign"].request.candidate_claim_sha256
        additions = (
            e.CausalVariable(
                variable_id="collider",
                label="Collider",
                definition="Common effect of two otherwise separate background causes.",
                roles=(e.CausalVariableRole.COVARIATE,),
                value_kind=e.CausalValueKind.CATEGORICAL,
                observability=e.CausalObservability.OBSERVED,
                intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
                observable_id="collider.value",
                measurement_protocol_sha256=digest("f9s3:collider-protocol"),
                grounding_claim_sha256s=(candidate,),
            ),
            e.CausalVariable(
                variable_id="outcome_background",
                label="Outcome background",
                definition="Background cause of the collider and the true response.",
                roles=(e.CausalVariableRole.COVARIATE,),
                value_kind=e.CausalValueKind.CATEGORICAL,
                observability=e.CausalObservability.OBSERVED,
                intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
                observable_id="outcome.background",
                measurement_protocol_sha256=digest("f9s3:background-protocol"),
                grounding_claim_sha256s=(candidate,),
            ),
        )
        graphs = []
        for graph in contract.hypothesis_graphs:
            edges = [item for item in graph.edges if "batch_to_response" not in item.edge_id]
            for suffix, source, target in (
                ("batch_to_collider", "batch_context", "collider"),
                ("background_to_collider", "outcome_background", "collider"),
                ("background_to_response", "outcome_background", "true_response"),
            ):
                edges.append(
                    e.CausalEdge(
                        edge_id=f"edge.{graph.hypothesis_id}.{suffix}",
                        source_variable_id=source,
                        target_variable_id=target,
                        mechanism=f"{source} precedes and changes {target}.",
                        assumption_ids=(
                            "assumption.exchangeability",
                            "assumption.temporal_order",
                        ),
                        grounding_claim_sha256s=(candidate,),
                    )
                )
            graphs.append(
                revalidate(
                    e.HypothesisCausalGraph,
                    graph,
                    edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
                )
            )
        estimand = revalidate(
            e.CausalEstimand,
            contract.estimand,
            adjustment_variable_ids=("collider",) if condition_on_collider else (),
        )
        changed = revalidate(
            e.CausalContract,
            contract,
            variables=tuple(
                sorted((*contract.variables, *additions), key=lambda item: item.variable_id)
            ),
            estimand=estimand,
            hypothesis_graphs=tuple(sorted(graphs, key=lambda item: item.hypothesis_id)),
        )
        _install_contract(parts, changed)

    closed_parts = build_f9s3_fixture(source_fixture["campaign"])
    install_collider(closed_parts, condition_on_collider=False)
    closed = await _run(closed_parts, "campaign:f9s3:collider-closed")

    opened_parts = build_f9s3_fixture(source_fixture["campaign"])
    install_collider(opened_parts, condition_on_collider=True)
    opened = await _run(opened_parts, "campaign:f9s3:collider-opened")

    assert closed.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.IDENTIFIED
        for item in closed.graph_audits
    )
    assert opened.disposition is e.CausalAuditDisposition.READY_BOUNDED
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.OPEN_BACKDOOR_PATH
        for item in opened.graph_audits
    )


@pytest.mark.asyncio
async def test_latent_common_cause_remains_visible_as_open_backdoor_path(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"], latent_confounding=True)

    campaign = await _run(parts, "campaign:f9s3:latent-confounding")

    assert campaign.disposition is e.CausalAuditDisposition.READY_BOUNDED
    assert campaign.claim_ceiling is e.CausalClaimCeiling.ASSOCIATION_ONLY
    assert all("unmeasured_context" in item.open_backdoor_path for item in campaign.graph_audits)


@pytest.mark.asyncio
async def test_descendant_or_latent_adjustment_variable_is_rejected(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    estimand = revalidate(
        e.CausalEstimand,
        contract.estimand,
        adjustment_variable_ids=("mediator_primary",),
    )
    contract = revalidate(e.CausalContract, contract, estimand=estimand)
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:bad-adjustment")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("invalid_adjustment_variable" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_null_graph_cannot_smuggle_an_exposure_outcome_effect(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    null = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.NULL)
    contract = parts["contract_batch"].contract
    graph = next(item for item in contract.hypothesis_graphs if item.hypothesis_id == null.hypothesis_id)
    edge = e.CausalEdge(
        edge_id=f"edge.{null.hypothesis_id}.smuggled_effect",
        source_variable_id="intervention",
        target_variable_id="true_response",
        mechanism="A direct effect contradicts the declared null role.",
        assumption_ids=("assumption.exchangeability", "assumption.temporal_order"),
        grounding_claim_sha256s=(parts["request"].input_claim_sha256s[0],),
    )
    contract = _replace_graph(
        contract,
        null.hypothesis_id,
        edges=tuple(sorted((*graph.edges, edge), key=lambda item: item.edge_id)),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:null-effect")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("null_contains_causal_effect_path" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_mechanism_graph_requires_exposure_outcome_path(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    primary = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.PRIMARY)
    contract = parts["contract_batch"].contract
    graph = next(item for item in contract.hypothesis_graphs if item.hypothesis_id == primary.hypothesis_id)
    retained = tuple(
        item
        for item in graph.edges
        if "intervention_to_primary" not in item.edge_id and "primary_to_response" not in item.edge_id
    )
    contract = _replace_graph(contract, primary.hypothesis_id, edges=retained)
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:no-mechanism-path")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("mechanism_lacks_causal_effect_path" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_alternative_graph_requires_accepted_prior_art_grounding(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    alternative = _hypothesis_by_role(parts["source_campaign"], e.HypothesisRole.ALTERNATIVE)
    contract = _replace_graph(
        parts["contract_batch"].contract,
        alternative.hypothesis_id,
        grounding_claim_sha256s=(parts["source_campaign"].request.candidate_claim_sha256,),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:alternative-grounding")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert any("alternative_prior_grounding_missing" in item for item in campaign.blockers)


@pytest.mark.asyncio
async def test_unknown_variable_grounding_cannot_enter_causal_evidence(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    variables = tuple(
        revalidate(
            e.CausalVariable,
            item,
            grounding_claim_sha256s=(digest("f9s3:invented-claim"),),
        )
        if item.variable_id == "intervention"
        else item
        for item in contract.variables
    )
    contract = revalidate(e.CausalContract, contract, variables=variables)
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:unknown-grounding")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_STRUCTURE
    assert "unknown_variable_grounding:intervention" in campaign.blockers


@pytest.mark.parametrize(
    ("decision", "confidence", "expected_status"),
    [
        (
            e.AssumptionReviewDecision.UNRESOLVED,
            0.97,
            e.AssumptionResolutionStatus.UNRESOLVED,
        ),
        (
            e.AssumptionReviewDecision.ACCEPT,
            0.50,
            e.AssumptionResolutionStatus.LOW_CONFIDENCE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_unresolved_or_low_confidence_assumption_bounds_claim_strength(
    source_fixture,
    decision,
    confidence,
    expected_status,
) -> None:
    assumption_id = "assumption.exchangeability"
    parts = build_f9s3_fixture(
        source_fixture["campaign"],
        decisions={assumption_id: decision},
        confidences={assumption_id: confidence},
    )

    campaign = await _run(parts, "campaign:f9s3:bounded-assumption")

    resolution = next(
        item for item in campaign.assumption_resolutions if item.assumption_id == assumption_id
    )
    assert campaign.disposition is e.CausalAuditDisposition.READY_BOUNDED
    assert campaign.claim_ceiling is e.CausalClaimCeiling.ASSOCIATION_ONLY
    assert campaign.prediction_planning_authorized is True
    assert resolution.status is expected_status


@pytest.mark.asyncio
async def test_rejected_identification_assumption_blocks_prediction_planning(source_fixture) -> None:
    parts = build_f9s3_fixture(
        source_fixture["campaign"],
        decisions={"assumption.measurement_validity": e.AssumptionReviewDecision.REJECT},
    )

    campaign = await _run(parts, "campaign:f9s3:rejected-assumption")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_ASSUMPTIONS
    assert campaign.claim_ceiling is e.CausalClaimCeiling.NONE
    assert campaign.prediction_planning_authorized is False
    assert "rejected_identification_assumption:assumption.measurement_validity" in (
        campaign.blockers
    )


@pytest.mark.parametrize(
    ("evidence_kind", "expected_ceiling"),
    [
        (
            e.CausalEvidenceKind.OBSERVATIONAL_ASSOCIATION,
            e.CausalClaimCeiling.ASSOCIATION_ONLY,
        ),
        (
            e.CausalEvidenceKind.SIMULATION_INTERVENTION,
            e.CausalClaimCeiling.WITHIN_MODEL_CAUSAL_ONLY,
        ),
    ],
)
@pytest.mark.asyncio
async def test_evidence_kind_caps_claim_even_when_graph_is_identified(
    source_fixture,
    evidence_kind,
    expected_ceiling,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"], evidence_kind=evidence_kind)

    campaign = await _run(parts, f"campaign:f9s3:evidence:{evidence_kind.value}")

    assert campaign.disposition is e.CausalAuditDisposition.READY_IDENTIFIED
    assert campaign.claim_ceiling is expected_ceiling


@pytest.mark.asyncio
async def test_conditioned_selection_process_stays_bounded_without_recoverability_proof(
    source_fixture,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    contract = parts["contract_batch"].contract
    hypothesis_ids = tuple(item.hypothesis_id for item in contract.hypothesis_graphs)
    candidate = parts["source_campaign"].request.candidate_claim_sha256
    selection_variable = e.CausalVariable(
        variable_id="selected_sample",
        label="Selected analysis sample",
        definition="Indicator that a unit enters the conditioned analysis sample.",
        roles=(e.CausalVariableRole.SELECTION,),
        value_kind=e.CausalValueKind.BINARY,
        observability=e.CausalObservability.OBSERVED,
        intervenability=e.CausalIntervenability.NOT_INTERVENABLE,
        observable_id="sample.selected",
        measurement_protocol_sha256=digest("f9s3:selection-protocol"),
        grounding_claim_sha256s=(candidate,),
    )
    selection_assumption = e.IdentificationAssumption(
        assumption_id="assumption.selection_exchangeability",
        kind=e.IdentificationAssumptionKind.SELECTION_EXCHANGEABILITY,
        statement="Conditioning on sample inclusion transports to the target population.",
        risk_if_violated="Selection opens a noncausal path and invalidates transport.",
        applies_to_hypothesis_ids=hypothesis_ids,
        variable_ids=("selected_sample", "true_response"),
        grounding_claim_sha256s=(candidate,),
    )
    mechanism = e.SelectionMechanism(
        mechanism_id="selection.analysis_sample",
        selection_variable_id="selected_sample",
        parent_variable_ids=("intervention", "true_response"),
        selection_rule_sha256=digest("f9s3:selection-rule"),
        exchangeability_assumption_id=selection_assumption.assumption_id,
        analysis_conditions_on_selection=True,
    )
    contract = revalidate(
        e.CausalContract,
        contract,
        variables=tuple(sorted((*contract.variables, selection_variable), key=lambda item: item.variable_id)),
        assumptions=tuple(
            sorted((*contract.assumptions, selection_assumption), key=lambda item: item.assumption_id)
        ),
        selection_mechanisms=(mechanism,),
    )
    _install_contract(parts, contract)

    campaign = await _run(parts, "campaign:f9s3:selection-bounded")

    assert campaign.disposition is e.CausalAuditDisposition.READY_BOUNDED
    assert campaign.claim_ceiling is e.CausalClaimCeiling.ASSOCIATION_ONLY
    assert all(
        item.backdoor_status is e.BackdoorAuditStatus.SELECTION_RECOVERABILITY_UNSUPPORTED
        for item in campaign.graph_audits
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "reordered",
        "rebound",
        "outside_evidence",
        "changed_evidence_closure",
    ],
)
@pytest.mark.asyncio
async def test_invalid_assumption_review_is_sanitized_execution_failure(
    source_fixture,
    mutation,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    raw = parts["review_batch"].model_dump(mode="python")
    if mutation == "missing":
        raw["reviews"] = raw["reviews"][:-1]
    elif mutation == "reordered":
        raw["reviews"] = list(reversed(raw["reviews"]))
    elif mutation == "rebound":
        raw["reviews"][0]["assumption_sha256"] = digest("f9s3:wrong-assumption")
    elif mutation == "outside_evidence":
        raw["reviews"][0]["evidence_claim_sha256s"] = (digest("f9s3:outside-evidence"),)
    else:
        original = set(raw["reviews"][0]["evidence_claim_sha256s"])
        known_unrelated = next(
            item for item in parts["request"].input_claim_sha256s if item not in original
        )
        raw["reviews"][0]["evidence_claim_sha256s"] = (known_unrelated,)
    parts["reviewer"] = StaticCausalReviewer(parts["reviewer_manifest"], raw)

    campaign = await _run(parts, f"campaign:f9s3:invalid-review:{mutation}")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is e.CausalAuditFailureKind.REVIEWER_OUTPUT_INVALID
    assert campaign.contract_batch == parts["contract_batch"]
    assert campaign.review_batch is None
    assert campaign.graph_audits


@pytest.mark.parametrize("stage", ["author_error", "invalid_author", "reviewer_error"])
@pytest.mark.asyncio
async def test_adapter_failures_retain_only_hashes(source_fixture, stage) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    secret = f"untrusted-causal-secret-{stage}"
    if stage == "author_error":
        parts["author"] = StaticCausalAuthor(parts["author_manifest"], RuntimeError(secret))
    elif stage == "invalid_author":
        parts["author"] = StaticCausalAuthor(
            parts["author_manifest"], {"raw_untrusted_graph": secret}
        )
    else:
        parts["reviewer"] = StaticCausalReviewer(
            parts["reviewer_manifest"], RuntimeError(secret)
        )

    campaign = await _run(parts, f"campaign:f9s3:failure:{stage}")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_EXECUTION
    assert campaign.failure is not None
    assert secret not in campaign.model_dump_json()
    assert len(campaign.failure.error_detail_sha256) == 64
    if stage.startswith("author"):
        assert parts["reviewer"].calls == 0


@pytest.mark.asyncio
async def test_reviewer_failure_revalidates_retained_causal_contract(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    parts["reviewer"] = StaticCausalReviewer(
        parts["reviewer_manifest"], RuntimeError("review transport stopped")
    )
    campaign = await _run(parts, "campaign:f9s3:retained-contract")
    raw = campaign.model_dump(mode="python")
    raw["contract_batch"]["request_sha256"] = digest("f9s3:another-request")

    with pytest.raises(ValidationError, match="bound to another request/author"):
        e.CausalAuditCampaign.model_validate(raw)


@pytest.mark.parametrize("stage", ["author", "reviewer"])
@pytest.mark.asyncio
async def test_adapter_future_timestamp_is_rejected(source_fixture, stage) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    future = parts["clock"].current + timedelta(hours=1)
    if stage == "author":
        batch = revalidate(
            e.CausalContractBatch,
            parts["contract_batch"],
            completed_at=future,
        )
        parts["author"] = StaticCausalAuthor(parts["author_manifest"], batch)
        expected = e.CausalAuditFailureKind.AUTHOR_OUTPUT_INVALID
    else:
        review = revalidate(
            e.CausalAssumptionReviewBatch,
            parts["review_batch"],
            completed_at=future,
        )
        parts["reviewer"] = StaticCausalReviewer(parts["reviewer_manifest"], review)
        expected = e.CausalAuditFailureKind.REVIEWER_OUTPUT_INVALID

    campaign = await _run(parts, f"campaign:f9s3:future:{stage}")

    assert campaign.disposition is e.CausalAuditDisposition.BLOCKED_EXECUTION
    assert campaign.failure.kind is expected


@pytest.mark.asyncio
async def test_causal_audit_decision_cannot_be_forged(source_fixture) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    campaign = await _run(parts, "campaign:f9s3:unforgeable")
    raw = campaign.model_dump(mode="python")
    raw.update(
        disposition=e.CausalAuditDisposition.READY_BOUNDED,
        claim_ceiling=e.CausalClaimCeiling.ASSOCIATION_ONLY,
        blockers=("caller_asserted_limit",),
    )

    with pytest.raises(ValidationError, match="not mechanically derived"):
        e.CausalAuditCampaign.model_validate(raw)


@pytest.mark.asyncio
async def test_causal_campaign_archive_round_trip_and_tamper_detection(
    source_fixture,
    tmp_path,
) -> None:
    parts = build_f9s3_fixture(source_fixture["campaign"])
    campaign = await _run(parts, "campaign:f9s3:archive")
    archive = k.ContentAddressedResponseArchive(tmp_path / "f9s3-causal-archive")

    committed = e.commit_causal_audit_campaign(archive=archive, campaign=campaign)
    loaded = e.load_causal_audit_campaign(archive=archive, ledger=committed.ledger)

    assert loaded == campaign
    target = archive.root / committed.ledger.relative_path
    target.chmod(0o600)
    target.write_bytes(b"tampered causal campaign")
    with pytest.raises(k.ResponseArchiveCorruption):
        e.load_causal_audit_campaign(archive=archive, ledger=committed.ledger)
