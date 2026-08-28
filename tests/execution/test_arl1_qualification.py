from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.arl1 import (
    ARL0GateEvidenceV1,
    ARL0GateKind,
    ARL0IntegrityEvidenceV1,
    ARL1AllAttemptsManifestV1,
    ARL1AttemptEvidenceRefV1,
    ARL1EvidenceBundleV1,
    ARL1EvidenceVerifierPinV1,
    ARL1Outcome,
    ARL1ProtocolCampaignEvidenceV1,
    ARL1QualificationError,
    ARL1QualificationPolicyV1,
    ARL1QualificationReceiptV1,
    ARL1QualificationTrustAnchorV1,
    ARL1ReplicateExecutionEvidenceV1,
    ARL1ReproductionReceiptV1,
    ARL1SourceVerificationReceiptV1,
    ARL1VerificationSubjectKind,
    build_arl1_protocol_executor_report,
    issue_arl1_qualification,
    issue_arl1_source_verification_receipt,
    verify_arl1_evidence_bundle,
    verify_arl1_policy_trust_anchor,
    verify_arl1_qualification,
)
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionCampaignRegistrationReceipt,
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.observations.scientific_bridge import ObservationAdmissionDisposition
from aletheia.protocols.compiler import compile_protocol
from aletheia.qualification_campaign import run_qualification_target_campaign
from aletheia.research_kernel.policy import ed25519_key_id, ed25519_public_key_hex
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
)

from .test_qualification_campaign import (
    _FakeCampaignHost,
    _request_and_evidence,
)

_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "arl1_protocol_fixtures",
    Path(__file__).resolve().parents[1] / "protocols" / "fixtures.py",
)
assert _FIXTURE_SPEC is not None and _FIXTURE_SPEC.loader is not None
_FIXTURE_MODULE = importlib.util.module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
fixture_by_name = _FIXTURE_MODULE.fixture_by_name

_OBSERVATION_TESTS = Path(__file__).resolve().parents[1] / "observations"
sys.path.insert(0, str(_OBSERVATION_TESTS))
from test_scientific_bridge import (  # noqa: E402
    ADMISSION_PRIVATE_KEY,
    _commit_admission,
    _commit_validation,
    _issue_admission_decision,
    _replicate_bridge_cases,
    _validated_receipt,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


VERIFIER_PRIVATE_KEY = bytes(range(192, 224))


class _ExactSourceVerifier:
    def __init__(self, receipts: tuple[ARL1SourceVerificationReceiptV1, ...]) -> None:
        self._by_subject = {(item.subject_kind, item.subject_sha256): item for item in receipts}
        self.calls: list[tuple[ARL1VerificationSubjectKind, str]] = []

    def verify_arl0_integrity(self, *, evidence, policy, retained_receipt):
        del policy
        key = (ARL1VerificationSubjectKind.ARL0_INTEGRITY, evidence.integrity_sha256)
        self.calls.append(key)
        assert retained_receipt == self._by_subject[key]
        return retained_receipt

    def verify_protocol_campaign(self, *, evidence, policy, retained_receipt):
        del policy
        key = (ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN, evidence.campaign_sha256)
        self.calls.append(key)
        assert retained_receipt == self._by_subject[key]
        return retained_receipt

    def verify_evidence_archive(self, *, bundle, retained_receipt):
        key = (
            ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
            bundle.evidence_archive_manifest_sha256,
        )
        self.calls.append(key)
        assert retained_receipt == self._by_subject[key]
        return retained_receipt


def _trust(bundle: ARL1EvidenceBundleV1) -> ARL1QualificationTrustAnchorV1:
    return ARL1QualificationTrustAnchorV1.from_policy(bundle.policy)


def _protocol_campaign(
    *,
    replicate_count: int = 2,
    not_before: datetime | None = None,
):
    cases = _replicate_bridge_cases(replicate_count)
    first_case = cases[0]
    binding = first_case.binding
    request = binding.compilation_request
    result = binding.compilation_result
    protocol = request.protocol
    work_order = binding.work_order
    node = binding.work_order_node
    validations = tuple(_validated_receipt(case) for case in cases)
    committed_validations = tuple(
        _commit_validation(case, receipt) for case, receipt in zip(cases, validations, strict=True)
    )
    raw_runs = tuple(item.message.raw_run for item in validations)
    custodies = tuple(
        case.raw_run_custody.verify_raw_run_custody(
            raw_run=raw_run,
            observed_at=committed.message.committed_at,
        )
        for case, raw_run, committed in zip(
            cases,
            raw_runs,
            committed_validations,
            strict=True,
        )
    )
    registration_receipts = tuple(
        AtomicScientificExecutionRegistrationReceipt(
            authorization_sha256=case.authorization.authorization_sha256,
            quest_id=case.binding.action.quest_id,
            scientific_slot_id=case.authorization.message.scientific_slot_id,
            action_sha256=case.binding.action.object_sha256,
            execution_id=case.qualification.bundle.intent.execution_id,
            attempt_id=(
                case.qualification.bundle.intent.infrastructure_attempt.infrastructure_attempt_id
            ),
            qualification_bundle_sha256=case.qualification.bundle.bundle_sha256,
            qualification_grant_sha256=case.qualification.grant.grant_sha256,
            registered_at=custody.sea_registered_at,
            qualification_admission_sha256=raw_run.qualification_admission_sha256,
            resource_reservation_sha256=custody.resource_reservation_sha256,
            reserved_at=custody.resource_reserved_at,
        )
        for case, raw_run, custody in zip(cases, raw_runs, custodies, strict=True)
    )
    campaign_registration = AtomicScientificExecutionCampaignRegistrationReceipt(
        authorizations=tuple(item.authorization for item in cases),
        registration_receipts=registration_receipts,
    )
    replicates = tuple(
        ARL1ReplicateExecutionEvidenceV1(
            authorization=case.authorization,
            registration_receipt=registration,
            raw_run=raw_run,
            raw_run_custody=custody,
            committed_validation=validation,
            outcome=ARL1Outcome.NEGATIVE,
        )
        for case, registration, raw_run, custody, validation in zip(
            cases,
            registration_receipts,
            raw_runs,
            custodies,
            committed_validations,
            strict=True,
        )
    )
    primary = replicates[0]
    decision, primary_validation = _issue_admission_decision(
        first_case,
        receipt=validations[0],
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    assert primary_validation == primary.committed_validation
    committed_admission = _commit_admission(first_case, decision)
    admission_committed_at = committed_admission.message.committed_at
    incorporation_event = ResearchEvent(
        quest_id=binding.action.quest_id,
        sequence=binding.action_authorized_event.sequence + 1,
        parent_event_sha256=binding.action_authorized_event.event_sha256,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=ObservationIncorporatedPayload(
            branch_id=binding.action_proposed_event.payload.branch_id,
            action_id=binding.action.action_id,
            scientific_slot_id=primary.scientific_slot_id,
            committed_admission_sha256=committed_admission.committed_admission_sha256,
            scientific_observation_sha256=primary.scientific_observation_sha256,
            outcome=ARL1Outcome.NEGATIVE.value,
            source_world_model_sha256=_sha("arl1-source-world-model"),
        ),
        command_sha256=_sha("arl1-incorporation-command"),
        principal_id="principal:arl1-kernel-incorporation",
        authorization_receipt_sha256=_sha("arl1-incorporation-authorization"),
        committed_at=admission_committed_at + timedelta(seconds=1),
    )
    validated_at = max(item.message.committed_at for item in committed_validations)
    attempts_manifest = ARL1AllAttemptsManifestV1(
        quest_id=binding.action.quest_id,
        action_sha256=binding.action.object_sha256,
        protocol_sha256=protocol.protocol_sha256,
        work_order_node_sha256=node.node_sha256,
        attempts=tuple(ARL1AttemptEvidenceRefV1.from_replicate(item) for item in replicates),
        retained_at=validated_at + timedelta(seconds=1),
    )
    reproduction = ARL1ReproductionReceiptV1(
        campaign_registration_sha256=campaign_registration.campaign_registration_sha256,
        replicate_evidence_sha256s=tuple(item.evidence_sha256 for item in replicates),
        scientific_slot_ids=tuple(item.scientific_slot_id for item in replicates),
        committed_validation_receipt_sha256s=tuple(
            item.committed_validation.committed_receipt_sha256 for item in replicates
        ),
        scientific_observation_sha256s=tuple(
            item.scientific_observation_sha256 for item in replicates
        ),
        outcome=ARL1Outcome.NEGATIVE,
        reproduced_at=attempts_manifest.retained_at + timedelta(seconds=1),
    )
    if not_before is not None:
        assert min(item.raw_run_custody.runtime_launched_at for item in replicates) > not_before
    reexecutions = tuple(sorted(item.evidence_sha256 for item in replicates))
    reported_at = incorporation_event.committed_at + timedelta(minutes=1)
    report = build_arl1_protocol_executor_report(
        quest_id=protocol.graph_scope.scope_binding.quest_id,
        question_ref=protocol.graph_scope.question_ref,
        protocol_sha256=protocol.protocol_sha256,
        compilation_receipt_sha256=result.receipt.receipt_sha256,
        work_order_sha256=work_order.work_order_sha256,
        work_order_node_id=node.node_id,
        exact_reexecution_evidence_sha256s=reexecutions,
        all_attempts_manifest_sha256=attempts_manifest.manifest_sha256,
        committed_validation_receipt_sha256=(primary.committed_validation.committed_receipt_sha256),
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        incorporation_event_sha256=incorporation_event.event_sha256,
        outcome=ARL1Outcome.NEGATIVE,
        reproduction_receipt_sha256=reproduction.receipt_sha256,
        source_evidence_archive_manifest_sha256=_sha("campaign-archive"),
        reported_at=reported_at,
    )
    return ARL1ProtocolCampaignEvidenceV1(
        domain_scope="bounded_grouped_regression",
        modality_scope="cpu_computational",
        compilation_request=request,
        compilation_result=result,
        work_order_node_id=node.node_id,
        campaign_registration=campaign_registration,
        replicate_executions=replicates,
        all_attempts_manifest=attempts_manifest,
        reproduction_receipt=reproduction,
        committed_admission=committed_admission,
        incorporation_event=incorporation_event,
        scientific_slot_id=primary.scientific_slot_id,
        execution_id=primary.registration_receipt.execution_id,
        infrastructure_attempt_id=primary.registration_receipt.attempt_id,
        scientific_execution_authorization_sha256=(primary.authorization.authorization_sha256),
        qualification_bundle_sha256=primary.registration_receipt.qualification_bundle_sha256,
        qualification_grant_sha256=primary.registration_receipt.qualification_grant_sha256,
        terminal_receipt_sha256=(
            primary.raw_run.accepted_terminal_submission.accepted_terminal_submission_sha256
        ),
        artifact_manifest_sha256=primary.raw_run.artifact_manifest.manifest_sha256,
        validator_manifest_sha256=primary.authorization.message.validator_manifest_sha256,
        validation_policy_sha256=(
            primary.authorization.message.observation_validation_policy_sha256
        ),
        committed_validation_receipt_sha256=(primary.committed_validation.committed_receipt_sha256),
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        scientific_observation_sha256=primary.scientific_observation_sha256,
        incorporation_event_sha256=incorporation_event.event_sha256,
        outcome=ARL1Outcome.NEGATIVE,
        exact_reexecution_evidence_sha256s=reexecutions,
        reproduction_receipt_sha256=reproduction.receipt_sha256,
        all_attempts_manifest_sha256=attempts_manifest.manifest_sha256,
        all_attempt_count=replicate_count,
        source_evidence_archive_manifest_sha256=_sha("campaign-archive"),
        scientific_execution_authorized_at=primary.authorization.message.authorized_at,
        validator_frozen_at=primary.authorization.message.admission_policy.frozen_at,
        execution_started_at=min(item.raw_run_custody.runtime_launched_at for item in replicates),
        execution_completed_at=max(
            item.raw_run.accepted_runtime_termination.runtime_ended_at for item in replicates
        ),
        validated_at=validated_at,
        admitted_at=committed_admission.message.committed_at,
        incorporated_at=incorporation_event.committed_at,
        report=report,
    )


@pytest.fixture
def arl1_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target_request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    host = _FakeCampaignHost(target_request, manifest, preflight)
    target_receipt = run_qualification_target_campaign(
        target_request,
        host,
        clock=lambda: preflight.verified_at + timedelta(seconds=1),
    )
    execution_now = target_receipt.completed_at + timedelta(hours=1)
    bridge_fixture_module = sys.modules[_replicate_bridge_cases.__module__]
    runtime_fixture_module = sys.modules[bridge_fixture_module._signed_case.__module__]
    protocol_fixture_module = sys.modules[bridge_fixture_module.fixture_by_name.__module__]
    monkeypatch.setattr(bridge_fixture_module, "NOW", execution_now)
    monkeypatch.setattr(runtime_fixture_module, "NOW", execution_now)
    monkeypatch.setattr(protocol_fixture_module, "_NOW", execution_now)
    campaign = _protocol_campaign(not_before=target_receipt.completed_at)
    private_key = bytes(range(224, 256))
    verifier_private_key = VERIFIER_PRIVATE_KEY
    verifier_pin = ARL1EvidenceVerifierPinV1(
        verification_policy_sha256=_sha("evidence-verifier-policy"),
        principal_id="arl1-evidence-verifier",
        key_id=ed25519_key_id(ed25519_public_key_hex(verifier_private_key)),
        public_key_ed25519_hex=ed25519_public_key_hex(verifier_private_key),
        valid_from=min(target_request.requested_at, campaign.execution_started_at)
        - timedelta(days=1),
        expires_at=max(target_receipt.completed_at, campaign.report.reported_at)
        + timedelta(days=31),
    )
    policy = ARL1QualificationPolicyV1(
        target_deployment_id=(
            target_request.observer_config.commissioning_request.installation_request.deployment_spec.deployment_id
        ),
        target_observer_pin_sha256=target_request.observer_config.observer_pin.pin_sha256,
        allowed_domain_scopes=(campaign.domain_scope,),
        allowed_modality_scopes=(campaign.modality_scope,),
        minimum_distinct_protocol_campaigns=1,
        evidence_verifier_principal_ids=("arl1-evidence-verifier",),
        evidence_verifier_policy_sha256s=(_sha("evidence-verifier-policy"),),
        evidence_verifier_pins=(verifier_pin,),
        qualification_authority_principal_id="arl1-qualification-authority",
        qualification_authority_key_id=ed25519_key_id(ed25519_public_key_hex(private_key)),
        qualification_authority_public_key_ed25519_hex=ed25519_public_key_hex(private_key),
        frozen_at=min(target_request.requested_at, campaign.execution_started_at)
        - timedelta(days=2),
        valid_from=min(target_request.requested_at, campaign.execution_started_at)
        - timedelta(days=1),
        valid_until=max(target_receipt.completed_at, campaign.report.reported_at)
        + timedelta(days=30),
        maximum_receipt_validity_seconds=7 * 24 * 60 * 60,
    )
    integrity_completed_at = max(
        item.verified_at
        for item in (
            ARL0GateEvidenceV1(
                gate_kind=kind,
                evaluated_scope_sha256=_sha(f"scope:{kind.value}"),
                evidence_artifact_sha256=_sha(f"evidence:{kind.value}"),
                verification_receipt_sha256=_sha(f"verification:{kind.value}"),
                verified_by_principal_id="arl1-evidence-verifier",
                verified_at=campaign.report.reported_at + timedelta(seconds=index),
            )
            for index, kind in enumerate(ARL0GateKind)
        )
    )
    gates = tuple(
        ARL0GateEvidenceV1(
            gate_kind=kind,
            evaluated_scope_sha256=_sha(f"scope:{kind.value}"),
            evidence_artifact_sha256=_sha(f"evidence:{kind.value}"),
            verification_receipt_sha256=_sha(f"verification:{kind.value}"),
            verified_by_principal_id="arl1-evidence-verifier",
            verified_at=campaign.report.reported_at + timedelta(seconds=index),
        )
        for index, kind in enumerate(ARL0GateKind)
    )
    integrity = ARL0IntegrityEvidenceV1(
        source_tree_sha256=_sha("source-tree"),
        environment_lock_sha256=_sha("environment-lock"),
        schema_revision="20260828_0028",
        database_schema_verification_receipt_sha256=_sha("schema-verification"),
        gates=gates,
        evidence_archive_manifest_sha256=_sha("arl0-archive"),
        completed_at=integrity_completed_at,
    )
    prepared_at = max(
        integrity.completed_at,
        target_receipt.completed_at,
        campaign.report.reported_at,
    ) + timedelta(seconds=1)
    verifications = tuple(
        sorted(
            (
                issue_arl1_source_verification_receipt(
                    subject_kind=ARL1VerificationSubjectKind.ARL0_INTEGRITY,
                    subject_sha256=integrity.integrity_sha256,
                    verifier_pin=verifier_pin,
                    verifier_private_key=verifier_private_key,
                    verified_at=prepared_at,
                ),
                issue_arl1_source_verification_receipt(
                    subject_kind=ARL1VerificationSubjectKind.EVIDENCE_ARCHIVE,
                    subject_sha256=_sha("arl1-archive"),
                    verifier_pin=verifier_pin,
                    verifier_private_key=verifier_private_key,
                    verified_at=prepared_at,
                ),
                issue_arl1_source_verification_receipt(
                    subject_kind=ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
                    subject_sha256=campaign.campaign_sha256,
                    verifier_pin=verifier_pin,
                    verifier_private_key=verifier_private_key,
                    verified_at=prepared_at,
                ),
            ),
            key=lambda item: (item.subject_kind.value, item.subject_sha256),
        )
    )
    bundle = ARL1EvidenceBundleV1(
        policy=policy,
        arl0_integrity=integrity,
        target_campaign_request=target_request,
        target_campaign_receipt=target_receipt,
        protocol_campaigns=(campaign,),
        source_verification_receipts=verifications,
        evidence_archive_manifest_sha256=_sha("arl1-archive"),
        prepared_at=prepared_at,
    )
    return bundle, private_key, _ExactSourceVerifier(verifications)


def test_arl1_qualification_replays_sources_signs_and_verifies(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    qualified_at = bundle.prepared_at + timedelta(seconds=1)

    def qualification_clock():
        assert len(verifier.calls) == 3
        return qualified_at

    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        trust_anchor=_trust(bundle),
        qualification_private_key=private_key,
        receipt_validity_seconds=86_400,
        clock=qualification_clock,
    )

    def audit_clock():
        assert len(verifier.calls) == 6
        return qualified_at + timedelta(seconds=1)

    verified = verify_arl1_qualification(
        bundle,
        receipt,
        source_verifier=verifier,
        trust_anchor=_trust(bundle),
        clock=audit_clock,
    )

    assert verified == receipt
    assert receipt.message.autonomy_level == "ARL-1 Protocol Executor"
    assert receipt.message.claim_ceiling == "bounded_protocol_execution_engineering"
    assert receipt.message.autonomous_research_design_claimed is False
    assert receipt.message.scientific_validity_claimed is False
    assert receipt.message.independent_replication_claimed is False
    assert receipt.message.scientific_authority_conferred is False
    assert len(verifier.calls) == 6


def test_arl1_out_of_band_trust_anchor_rejects_self_selected_policy(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    trusted = _trust(bundle)
    variant = ARL1QualificationPolicyV1.model_validate(
        {
            **bundle.policy.model_dump(mode="python"),
            "policy_id": None,
            "allowed_domain_scopes": tuple(
                sorted((*bundle.policy.allowed_domain_scopes, "domain.untrusted-variant"))
            ),
        }
    )

    assert ARL1QualificationTrustAnchorV1.from_policy(variant) != trusted
    with pytest.raises(ARL1QualificationError, match="out-of-band"):
        verify_arl1_policy_trust_anchor(variant, trusted)


def test_arl1_source_verifier_cannot_reuse_an_evaluated_authority_key(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    authorization = bundle.protocol_campaigns[0].replicate_executions[0].authorization.message
    public_key = ed25519_public_key_hex(ADMISSION_PRIVATE_KEY)
    verifier_pin = ARL1EvidenceVerifierPinV1(
        verification_policy_sha256=authorization.admission_authority_policy_sha256,
        principal_id=authorization.admission_principal_id,
        key_id=ed25519_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=bundle.policy.valid_from,
        expires_at=bundle.policy.valid_until,
    )
    overlapping_policy = ARL1QualificationPolicyV1.model_validate(
        {
            **bundle.policy.model_dump(mode="python"),
            "policy_id": None,
            "evidence_verifier_principal_ids": (verifier_pin.principal_id,),
            "evidence_verifier_policy_sha256s": (verifier_pin.verification_policy_sha256,),
            "evidence_verifier_pins": (verifier_pin,),
        }
    )

    with pytest.raises(ValidationError, match="overlaps an evaluated authority"):
        ARL1EvidenceBundleV1.model_validate(
            {**bundle.model_dump(mode="python"), "policy": overlapping_policy}
        )


def test_arl1_report_is_deterministic_and_cannot_drop_limitations() -> None:
    campaign = _protocol_campaign()
    rebuilt = build_arl1_protocol_executor_report(
        **campaign.report.model_dump(
            mode="python",
            exclude={
                "schema_name",
                "schema_version",
                "report_id",
                "claim_ceiling",
                "limitations",
                "autonomous_research_design_claimed",
                "scientific_validity_claimed",
                "independent_replication_claimed",
            },
        )
    )
    assert rebuilt == campaign.report
    with pytest.raises(ValidationError, match="limitations"):
        type(campaign.report).model_validate(
            {**campaign.report.model_dump(mode="python"), "limitations": ()}
        )


def test_arl1_rejects_a_protocol_without_two_preregistered_reexecutions() -> None:
    fixture = fixture_by_name("grouped_regression")
    result = compile_protocol(fixture.request)
    assert result.work_order is not None
    campaign = _protocol_campaign()
    with pytest.raises(ValidationError, match="at least two exact reexecutions"):
        ARL1ProtocolCampaignEvidenceV1.model_validate(
            {
                **campaign.model_dump(mode="python"),
                "campaign_id": None,
                "compilation_request": fixture.request,
                "compilation_result": result,
                "work_order_node_id": result.work_order.nodes[-1].node_id,
                "report": campaign.report.model_copy(
                    update={
                        "protocol_sha256": fixture.request.protocol.protocol_sha256,
                        "compilation_receipt_sha256": result.receipt.receipt_sha256,
                        "work_order_sha256": result.work_order.work_order_sha256,
                        "work_order_node_id": result.work_order.nodes[-1].node_id,
                        "report_id": None,
                    }
                ),
            }
        )


def test_arl1_requires_evidence_for_every_preregistered_reexecution() -> None:
    campaign = _protocol_campaign(replicate_count=3)
    retained = campaign.exact_reexecution_evidence_sha256s[:2]
    report_values = campaign.report.model_dump(
        mode="python",
        exclude={
            "schema_name",
            "schema_version",
            "report_id",
            "claim_ceiling",
            "limitations",
            "autonomous_research_design_claimed",
            "scientific_validity_claimed",
            "independent_replication_claimed",
        },
    )
    report_values["exact_reexecution_evidence_sha256s"] = retained
    report = build_arl1_protocol_executor_report(**report_values)

    with pytest.raises(ValidationError, match="every preregistered exact reexecution"):
        ARL1ProtocolCampaignEvidenceV1.model_validate(
            {
                **campaign.model_dump(mode="python"),
                "campaign_id": None,
                "exact_reexecution_evidence_sha256s": retained,
                "all_attempt_count": len(retained),
                "report": report,
            }
        )


def test_arl1_rejects_post_execution_validator_freezing() -> None:
    campaign = _protocol_campaign()
    with pytest.raises(ValidationError, match="out of order"):
        ARL1ProtocolCampaignEvidenceV1.model_validate(
            {
                **campaign.model_dump(mode="python"),
                "campaign_id": None,
                "validator_frozen_at": campaign.execution_started_at + timedelta(seconds=1),
            }
        )


def test_arl1_source_verification_cannot_be_replayed_for_another_subject(arl1_case) -> None:
    bundle, _private_key, verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    variant = campaign.model_copy(
        update={
            "scientific_observation_sha256": _sha("variant-observation"),
            "campaign_id": None,
        }
    )
    with pytest.raises((ValidationError, ARL1QualificationError)):
        verify_arl1_evidence_bundle(
            bundle.model_copy(update={"protocol_campaigns": (variant,)}),
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
        )


def test_arl1_rejects_source_verification_that_predates_its_evidence(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    verifier_pin = bundle.policy.evidence_verifier_pins[0]
    early_receipt = issue_arl1_source_verification_receipt(
        subject_kind=ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
        subject_sha256=campaign.campaign_sha256,
        verifier_pin=verifier_pin,
        verifier_private_key=VERIFIER_PRIVATE_KEY,
        verified_at=campaign.report.reported_at - timedelta(microseconds=1),
    )
    receipts = tuple(
        early_receipt
        if item.subject_kind is ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN
        else item
        for item in bundle.source_verification_receipts
    )

    with pytest.raises(ValidationError, match="predates its completed evidence"):
        ARL1EvidenceBundleV1.model_validate(
            {
                **bundle.model_dump(mode="python"),
                "source_verification_receipts": receipts,
            }
        )


def test_arl1_requires_target_qualification_before_protocol_execution(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    late_target = type(bundle.target_campaign_receipt).model_validate(
        {
            **bundle.target_campaign_receipt.model_dump(mode="python"),
            "receipt_id": None,
            "completed_at": campaign.execution_started_at + timedelta(seconds=1),
        }
    )

    with pytest.raises(ValidationError, match="outside the qualified scope"):
        ARL1EvidenceBundleV1.model_validate(
            {
                **bundle.model_dump(mode="python"),
                "target_campaign_receipt": late_target,
            }
        )


def test_arl1_source_verifier_cannot_swap_subject_kinds(arl1_case) -> None:
    bundle, _private_key, verifier = arl1_case
    campaign_receipt = next(
        item
        for item in bundle.source_verification_receipts
        if item.subject_kind is ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN
    )

    class _SwappedVerifier(_ExactSourceVerifier):
        def verify_arl0_integrity(self, *, evidence, policy, retained_receipt):
            del evidence, policy, retained_receipt
            return campaign_receipt

    with pytest.raises(ARL1QualificationError, match="fresh ARL-0 verification differs"):
        verify_arl1_evidence_bundle(
            bundle,
            source_verifier=_SwappedVerifier(tuple(verifier._by_subject.values())),
            trust_anchor=_trust(bundle),
        )


def test_arl1_receipt_rejects_wrong_signer_expiry_and_signature(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    qualified_at = bundle.prepared_at + timedelta(seconds=1)
    with pytest.raises(ARL1QualificationError, match="signer"):
        issue_arl1_qualification(
            bundle,
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
            qualification_private_key=b"x" * 32,
            receipt_validity_seconds=86_400,
            clock=lambda: qualified_at,
        )
    with pytest.raises(ARL1QualificationError, match="validity"):
        issue_arl1_qualification(
            bundle,
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
            qualification_private_key=private_key,
            receipt_validity_seconds=8 * 86_400,
            clock=lambda: qualified_at,
        )

    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        trust_anchor=_trust(bundle),
        qualification_private_key=private_key,
        receipt_validity_seconds=86_400,
        clock=lambda: qualified_at,
    )
    tampered = receipt.model_copy(update={"signature_ed25519_hex": "0" * 128, "receipt_id": None})
    with pytest.raises(ARL1QualificationError, match="signature"):
        verify_arl1_qualification(
            bundle,
            tampered,
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
            clock=lambda: qualified_at + timedelta(seconds=1),
        )


def test_arl1_verifier_rejects_a_correctly_signed_policy_violating_expiry(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    qualified_at = bundle.prepared_at + timedelta(seconds=1)
    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        trust_anchor=_trust(bundle),
        qualification_private_key=private_key,
        receipt_validity_seconds=86_400,
        clock=lambda: qualified_at,
    )
    unsigned = ARL1QualificationReceiptV1(
        message=receipt.message.model_copy(update={"expires_at": qualified_at + timedelta(days=8)}),
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.signature_message)
    forged = ARL1QualificationReceiptV1(
        message=unsigned.message,
        signature_ed25519_hex=signature.hex(),
    )

    with pytest.raises(ARL1QualificationError, match="validity differs from policy"):
        verify_arl1_qualification(
            bundle,
            forged,
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
            clock=lambda: qualified_at + timedelta(seconds=1),
        )


def test_arl1_contracts_forbid_synthetic_evidence(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    gate = bundle.arl0_integrity.gates[0]
    with pytest.raises(ValidationError, match="synthetic_evidence"):
        ARL0GateEvidenceV1.model_validate(
            {**gate.model_dump(mode="python"), "synthetic_evidence": True}
        )


def test_arl0_gates_cannot_reuse_evidence_or_verification_receipts(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    gates = list(bundle.arl0_integrity.gates)
    gates[1] = gates[1].model_copy(
        update={
            "evidence_artifact_sha256": gates[0].evidence_artifact_sha256,
            "verification_receipt_sha256": gates[0].verification_receipt_sha256,
        }
    )

    with pytest.raises(ValidationError, match="distinct evidence and verification receipts"):
        ARL0IntegrityEvidenceV1.model_validate(
            {
                **bundle.arl0_integrity.model_dump(mode="python"),
                "gates": gates,
            }
        )


def test_arl1_issuance_rejects_naive_wall_clock(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    with pytest.raises(ARL1QualificationError, match="timezone-aware UTC"):
        issue_arl1_qualification(
            bundle,
            source_verifier=verifier,
            trust_anchor=_trust(bundle),
            qualification_private_key=private_key,
            receipt_validity_seconds=86_400,
            clock=lambda: datetime(2026, 8, 28, 1, 2, 3),
        )
    (ARL1ReplicateExecutionEvidenceV1,)
    (ARL1ReproductionReceiptV1,)
