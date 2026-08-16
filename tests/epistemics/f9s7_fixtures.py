from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import aletheia.epistemics as e
from aletheia.knowledge.response_archive import ContentAddressedResponseArchive

from .f9s2_fixtures import StepClock, digest, revalidate
from .f9s6_fixtures import FixtureObservationValidator, build_f9s6_fixture


def build_k3_scorer_manifest(*, frozen_at) -> e.K3AcceptanceScorerManifest:
    return e.K3AcceptanceScorerManifest(
        scorer_id="f9s7-independent-k3-acceptance-scorer-v1",
        scorer_code_sha256=digest("f9s7:k3-scorer-code"),
        output_schema_sha256=e.K3_ACCEPTANCE_OUTPUT_SCHEMA_SHA256,
        scorer_principal_sha256=digest("f9s7:k3-scorer-principal"),
        frozen_at=frozen_at,
    )


def _revision_materializations(
    *,
    round_id: str,
    source_snapshot: e.WorldModelSnapshot,
    update: e.CommittedWorldBeliefUpdateCampaign,
    materialized_at,
) -> tuple[e.K3RevisionMaterialization, ...]:
    source_by_id = {item.hypothesis_id: item for item in source_snapshot.hypotheses}
    materializations: list[e.K3RevisionMaterialization] = []
    for directive in update.campaign.hypothesis_revisions:
        if not directive.new_version_required:
            continue
        source = source_by_id[directive.hypothesis_id]
        lifecycle = (
            e.HypothesisLifecycle.NARROWED
            if directive.action is e.HypothesisRevisionAction.NARROW
            else e.HypothesisLifecycle.RETIRED
        )
        narrowed = directive.action is e.HypothesisRevisionAction.NARROW
        revised = e.HypothesisVersion(
            run_id=source.run_id,
            question_id=source.question_id,
            question_version_sha256=source.question_version_sha256,
            hypothesis_id=source.hypothesis_id,
            version=source.version + 1,
            parent_hypothesis_sha256=source.hypothesis_sha256,
            role=source.role,
            lifecycle=lifecycle,
            statement=(
                f"{source.statement} Scope narrowed after validated outcome."
                if narrowed
                else source.statement
            ),
            mechanism=source.mechanism,
            rationale_sha256=digest(
                f"f9s7:revision:{directive.directive_sha256}:{directive.action.value}"
            ),
            author_principal_sha256=digest("f9s7:revision-materializer-principal"),
            frozen_at=materialized_at,
        )
        source_predictions = tuple(
            item
            for item in source_snapshot.predictions
            if item.hypothesis_id == source.hypothesis_id
        )
        revised_predictions: tuple[e.Prediction, ...] = ()
        if narrowed:
            revised_predictions = tuple(
                sorted(
                    (
                        e.Prediction(
                            run_id=prediction.run_id,
                            prediction_id=prediction.prediction_id,
                            version=prediction.version + 1,
                            parent_prediction_sha256=prediction.prediction_sha256,
                            hypothesis_id=prediction.hypothesis_id,
                            hypothesis_version_sha256=revised.hypothesis_sha256,
                            observable_id=prediction.observable_id,
                            outcome_space=prediction.outcome_space,
                            expected_outcome=next(
                                item
                                for item in prediction.outcome_space
                                if item != prediction.expected_outcome
                            ),
                            direction=prediction.direction,
                            discriminates_from_hypothesis_ids=(
                                prediction.discriminates_from_hypothesis_ids
                            ),
                            measurement_protocol_sha256=(prediction.measurement_protocol_sha256),
                            author_principal_sha256=digest(
                                "f9s7:revision-prediction-materializer-principal"
                            ),
                            frozen_at=materialized_at,
                        )
                        for prediction in source_predictions
                    ),
                    key=lambda item: item.prediction_id,
                )
            )
        materializations.append(
            e.K3RevisionMaterialization(
                round_id=round_id,
                source_update_receipt_sha256=update.receipt_sha256,
                directive_sha256=directive.directive_sha256,
                revised_hypothesis=revised,
                revised_predictions=revised_predictions,
                materialized_at=materialized_at,
            )
        )
    return tuple(
        sorted(
            materializations,
            key=lambda item: (item.source_update_receipt_sha256, item.directive_sha256),
        )
    )


def _source_snapshot(parts: dict[str, Any]) -> e.WorldModelSnapshot:
    snapshot = parts[
        "selected_candidate"
    ].committed_prediction.campaign.source_causal_campaign.source_campaign.world_model_snapshot
    assert snapshot is not None
    return snapshot


def build_f9s7_fixture(
    source_campaign: e.CausalAuditCampaign,
    root: Path,
    *,
    outcome_role: e.HypothesisRole = e.HypothesisRole.PRIMARY,
    candidate_specs: tuple[dict[str, object], ...] | None = None,
    validation_overrides: dict[str, object] | None = None,
    update_policy_updates: dict[str, object] | None = None,
    drop_update: bool = False,
    acceptance_policy_updates: dict[str, object] | None = None,
    mechanism_claim_disposition: e.MechanismClaimDisposition | None = None,
    mechanism_claim_ceiling: e.CausalClaimCeiling = e.CausalClaimCeiling.CAUSAL_CANDIDATE,
    terminal_action: e.K3TerminalAction | None = None,
) -> dict[str, Any]:
    fixture_kwargs: dict[str, object] = {"outcome_role": outcome_role}
    if candidate_specs is not None:
        fixture_kwargs["candidate_specs"] = candidate_specs
    parts = build_f9s6_fixture(source_campaign, root / "f9s6", **fixture_kwargs)
    if update_policy_updates:
        parts["update_policy"] = revalidate(
            e.WorldBeliefUpdatePolicy,
            parts["update_policy"],
            **update_policy_updates,
        )

    committed_validation = parts["committed_validation"]
    validation_archive = parts["validation_archive"]
    validation_campaign = parts["validation_campaign"]
    if validation_overrides is not None:
        validation_archive = ContentAddressedResponseArchive(root / "validation-archive-override")
        request = parts["validation_request"]
        validator = FixtureObservationValidator(
            parts["validator_manifest"],
            completed_at=request.issued_at + timedelta(minutes=1),
            overrides=validation_overrides,
        )
        validation_campaign = asyncio.run(
            e.run_observation_validation(
                campaign_id="campaign:f9s7:validation-override",
                policy=parts["validation_policy"],
                request=request,
                validator=validator,
                selection_archive=parts["selection_archive"],
                prediction_archive=parts["prediction_archive"],
                observation_store=parts["observation_store"],
                clock=StepClock(request.issued_at + timedelta(minutes=2)),
            )
        )
        committed_validation = e.commit_observation_validation_campaign(
            archive=validation_archive,
            campaign=validation_campaign,
            committed_at=validation_campaign.generated_at + timedelta(minutes=1),
        )

    update_archive = ContentAddressedResponseArchive(root / "update-archive")
    committed_updates: tuple[e.CommittedWorldBeliefUpdateCampaign, ...] = ()
    update_campaign = None
    if (
        validation_campaign.disposition is e.ObservationValidationDisposition.VALIDATED_CONFIRMATION
        and not drop_update
    ):
        update_request = e.build_world_belief_update_request(
            update_id="f9s7-world-belief-update-v1",
            committed_validation=committed_validation,
            policy=parts["update_policy"],
            validation_archive_custody_sha256=parts["validation_archive_custody_sha256"],
            issued_at=committed_validation.committed_at + timedelta(minutes=1),
        )
        update_campaign = e.run_world_belief_update(
            campaign_id="campaign:f9s7:world-belief-update",
            policy=parts["update_policy"],
            request=update_request,
            validation_archive=validation_archive,
            clock=StepClock(update_request.issued_at + timedelta(minutes=1)),
        )
        committed_update = e.commit_world_belief_update_campaign(
            archive=update_archive,
            campaign=update_campaign,
            committed_at=update_campaign.generated_at + timedelta(minutes=1),
        )
        committed_updates = (committed_update,)

    round_id = "round.f9s7.001"
    round_evidence = e.K3RoundEvidence(
        round_id=round_id,
        ordinal=1,
        committed_selection=parts["committed_selection"],
        committed_validations=(committed_validation,),
        committed_updates=committed_updates,
    )
    source_snapshot = _source_snapshot(parts)
    successful_update = (
        committed_updates[0]
        if committed_updates
        and committed_updates[0].campaign.disposition
        in {
            e.WorldBeliefUpdateDisposition.UPDATED_ROBUST,
            e.WorldBeliefUpdateDisposition.UPDATED_FRAGILE,
        }
        else None
    )
    latest_commit = max(
        parts["committed_selection"].committed_at,
        committed_validation.committed_at,
        *(item.committed_at for item in committed_updates),
    )
    materialized_at = latest_commit + timedelta(minutes=1)
    materializations = (
        _revision_materializations(
            round_id=round_id,
            source_snapshot=source_snapshot,
            update=successful_update,
            materialized_at=materialized_at,
        )
        if successful_update is not None
        else ()
    )

    scorer_manifest = build_k3_scorer_manifest(frozen_at=parts["policy"].frozen_at)
    acceptance_policy = e.K3AcceptancePolicy(
        policy_id="f9s7-k3-acceptance-policy-v1",
        scorer_principal_sha256=scorer_manifest.scorer_principal_sha256,
        frozen_at=parts["policy"].frozen_at,
    )
    if acceptance_policy_updates:
        acceptance_policy = revalidate(
            e.K3AcceptancePolicy,
            acceptance_policy,
            **acceptance_policy_updates,
        )

    claims: tuple[e.MechanismClaimRecord, ...] = ()
    if mechanism_claim_disposition is not None:
        if successful_update is None:
            raise ValueError("mechanism claim fixture requires a successful update")
        audit = successful_update.campaign.audit
        assert audit is not None
        target_id = audit.maximum_hypothesis_ids[0]
        claim_artifact = (
            digest("f9s7:mechanism-claim-artifact")
            if mechanism_claim_disposition is e.MechanismClaimDisposition.ISSUED
            else None
        )
        claims = (
            e.MechanismClaimRecord(
                claim_id="claim.f9s7.mechanism.001",
                round_id=round_id,
                source_update_receipt_sha256=successful_update.receipt_sha256,
                hypothesis_id=target_id,
                requested_ceiling=mechanism_claim_ceiling,
                disposition=mechanism_claim_disposition,
                claim_artifact_sha256=claim_artifact,
                evidence_sha256s=(successful_update.receipt_sha256,),
                decided_at=materialized_at,
            ),
        )

    decision_at = materialized_at + timedelta(minutes=1)
    if successful_update is None:
        source_update_receipt = None
        source_world_revision = None
        decision_evidence = (committed_validation.receipt_sha256,)
        default_action = e.K3TerminalAction.STOP_AND_ARCHIVE
    else:
        assert successful_update.campaign.world_revision is not None
        source_update_receipt = successful_update.receipt_sha256
        source_world_revision = successful_update.campaign.world_revision.directive_sha256
        decision_evidence = tuple(sorted({source_update_receipt, source_world_revision}))
        default_action = e.K3TerminalAction.STOP_AND_ARCHIVE
    terminal_decision = e.K3TerminalDecision(
        decision_id="f9s7-terminal-decision-v1",
        final_round_id=round_id,
        action=terminal_action or default_action,
        source_update_receipt_sha256=source_update_receipt,
        source_world_revision_directive_sha256=source_world_revision,
        reason_codes=("synthetic_acceptance_fixture_complete",),
        evidence_sha256s=decision_evidence,
        decided_by_principal_sha256=digest("f9s7:terminal-decision-principal"),
        decided_at=decision_at,
    )
    persisted_at = decision_at + timedelta(minutes=1)
    evidence_ledger = e.build_k3_evidence_ledger(
        ledger_id="f9s7-evidence-ledger-v1",
        rounds=(round_evidence,),
        revision_materializations=materializations,
        mechanism_claims=claims,
        terminal_decision=terminal_decision,
        persistence_principal_sha256=digest("f9s7:persistence-principal"),
        persisted_at=persisted_at,
    )
    evidence_archive = ContentAddressedResponseArchive(root / "evidence-archive")
    committed_evidence = e.commit_k3_evidence_ledger(
        archive=evidence_archive,
        evidence=evidence_ledger,
        committed_at=persisted_at + timedelta(minutes=1),
    )
    selection_custody = digest("f9s7:selection-archive-custody")
    validation_custody = digest("f9s7:validation-archive-custody")
    update_custody = digest("f9s7:update-archive-custody")
    evidence_custody = digest("f9s7:evidence-archive-custody")
    request = e.build_k3_acceptance_request(
        acceptance_id="f9s7-k3-acceptance-request-v1",
        rounds=(round_evidence,),
        committed_evidence_ledger=committed_evidence,
        scorer_manifest=scorer_manifest,
        policy=acceptance_policy,
        selection_archive_custody_sha256=selection_custody,
        validation_archive_custody_sha256=validation_custody,
        update_archive_custody_sha256=update_custody,
        evidence_archive_custody_sha256=evidence_custody,
        issued_at=committed_evidence.committed_at + timedelta(minutes=1),
    )
    return {
        **parts,
        "validation_archive": validation_archive,
        "validation_campaign": validation_campaign,
        "committed_validation": committed_validation,
        "update_archive": update_archive,
        "update_campaign": update_campaign,
        "committed_updates": committed_updates,
        "round_evidence": round_evidence,
        "revision_materializations": materializations,
        "scorer_manifest": scorer_manifest,
        "acceptance_policy": acceptance_policy,
        "mechanism_claims": claims,
        "terminal_decision": terminal_decision,
        "evidence_ledger": evidence_ledger,
        "evidence_archive": evidence_archive,
        "committed_evidence": committed_evidence,
        "selection_archive_custody_sha256": selection_custody,
        "validation_archive_custody_sha256": validation_custody,
        "update_archive_custody_sha256": update_custody,
        "evidence_archive_custody_sha256": evidence_custody,
        "acceptance_request": request,
        "acceptance_clock": StepClock(request.issued_at + timedelta(minutes=1)),
    }


__all__ = [
    "build_f9s7_fixture",
    "build_k3_scorer_manifest",
]
