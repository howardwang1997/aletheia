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
    ARL1EvidenceBundleV1,
    ARL1Outcome,
    ARL1ProtocolCampaignEvidenceV1,
    ARL1QualificationError,
    ARL1QualificationPolicyV1,
    ARL1QualificationReceiptV1,
    ARL1SourceVerificationReceiptV1,
    ARL1VerificationSubjectKind,
    build_arl1_protocol_executor_report,
    issue_arl1_qualification,
    verify_arl1_evidence_bundle,
    verify_arl1_qualification,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, compile_protocol
from aletheia.protocols.schemas import ProtocolIR
from aletheia.qualification_campaign import run_qualification_target_campaign
from aletheia.research_kernel.policy import ed25519_key_id, ed25519_public_key_hex

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


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _ExactSourceVerifier:
    def __init__(self, receipts: tuple[ARL1SourceVerificationReceiptV1, ...]) -> None:
        self._by_subject = {(item.subject_kind, item.subject_sha256): item for item in receipts}
        self.calls: list[tuple[ARL1VerificationSubjectKind, str]] = []

    def verify_arl0_integrity(self, *, evidence, policy):
        del policy
        key = (ARL1VerificationSubjectKind.ARL0_INTEGRITY, evidence.integrity_sha256)
        self.calls.append(key)
        return self._by_subject[key]

    def verify_protocol_campaign(self, *, evidence, policy):
        del policy
        key = (ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN, evidence.campaign_sha256)
        self.calls.append(key)
        return self._by_subject[key]


def _protocol_campaign(
    *,
    replicate_count: int = 2,
    not_before: datetime | None = None,
):
    fixture = fixture_by_name("grouped_regression")
    original = fixture.request.protocol
    steps = tuple(
        type(step).model_validate(
            {
                **step.model_dump(mode="python"),
                "scientific_replicate_count": replicate_count,
                "replicate_seed_sha256s": tuple(
                    _sha(f"replicate-{index}-{replicate_index}")
                    for replicate_index in range(replicate_count)
                ),
            }
        )
        for index, step in enumerate(original.steps)
    )
    selected_step = steps[-1]
    resource_budget = type(original.resource_budget).model_validate(
        {
            **original.resource_budget.model_dump(mode="python"),
            "maximum_total_artifact_bytes": (
                original.resource_budget.maximum_total_artifact_bytes * replicate_count
            ),
        }
    )
    protocol = ProtocolIR.model_validate(
        {
            **original.model_dump(mode="python"),
            "steps": steps,
            "resource_budget": resource_budget,
        }
    )
    request = ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=fixture.request.capability_catalog,
        resource_catalog=fixture.request.resource_catalog,
        compiler_implementation_sha256=fixture.request.compiler_implementation_sha256,
    )
    result = compile_protocol(request)
    assert result.work_order is not None
    node = next(
        item for item in result.work_order.nodes if item.protocol_step_id == selected_step.step_id
    )
    started_at = max(
        protocol.authored_at + timedelta(minutes=1),
        (not_before + timedelta(seconds=1)) if not_before is not None else protocol.authored_at,
    )
    completed_at = started_at + timedelta(minutes=1)
    validated_at = completed_at + timedelta(minutes=1)
    admitted_at = validated_at + timedelta(minutes=1)
    incorporated_at = admitted_at + timedelta(minutes=1)
    reported_at = incorporated_at + timedelta(minutes=1)
    reexecutions = tuple(sorted(_sha(f"reexecution-{index}") for index in range(replicate_count)))
    report = build_arl1_protocol_executor_report(
        quest_id=protocol.graph_scope.scope_binding.quest_id,
        question_ref=protocol.graph_scope.question_ref,
        protocol_sha256=protocol.protocol_sha256,
        compilation_receipt_sha256=result.receipt.receipt_sha256,
        work_order_sha256=result.work_order.work_order_sha256,
        work_order_node_id=node.node_id,
        exact_reexecution_evidence_sha256s=reexecutions,
        all_attempts_manifest_sha256=_sha("all-attempts"),
        committed_validation_receipt_sha256=_sha("committed-validation"),
        committed_admission_sha256=_sha("committed-admission"),
        incorporation_event_sha256=_sha("incorporation-event"),
        outcome=ARL1Outcome.NEGATIVE,
        reproduction_receipt_sha256=_sha("reproduction-receipt"),
        source_evidence_archive_manifest_sha256=_sha("campaign-archive"),
        reported_at=reported_at,
    )
    return ARL1ProtocolCampaignEvidenceV1(
        domain_scope="bounded_grouped_regression",
        modality_scope="cpu_computational",
        compilation_request=request,
        compilation_result=result,
        work_order_node_id=node.node_id,
        scientific_slot_id="sos_" + "1" * 32,
        execution_id="execution.arl1.grouped",
        infrastructure_attempt_id="attempt.arl1.grouped.1",
        scientific_execution_authorization_sha256=_sha("sea"),
        qualification_bundle_sha256=_sha("qualification-bundle"),
        qualification_grant_sha256=_sha("qualification-grant"),
        terminal_receipt_sha256=_sha("terminal"),
        artifact_manifest_sha256=_sha("artifact-manifest"),
        validator_manifest_sha256=_sha("validator-manifest"),
        validation_policy_sha256=_sha("validation-policy"),
        committed_validation_receipt_sha256=_sha("committed-validation"),
        committed_admission_sha256=_sha("committed-admission"),
        scientific_observation_sha256=_sha("scientific-observation"),
        incorporation_event_sha256=_sha("incorporation-event"),
        outcome=ARL1Outcome.NEGATIVE,
        exact_reexecution_evidence_sha256s=reexecutions,
        reproduction_receipt_sha256=_sha("reproduction-receipt"),
        all_attempts_manifest_sha256=_sha("all-attempts"),
        all_attempt_count=replicate_count,
        source_evidence_archive_manifest_sha256=_sha("campaign-archive"),
        scientific_execution_authorized_at=started_at - timedelta(minutes=2),
        validator_frozen_at=started_at - timedelta(minutes=1),
        execution_started_at=started_at,
        execution_completed_at=completed_at,
        validated_at=validated_at,
        admitted_at=admitted_at,
        incorporated_at=incorporated_at,
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
    campaign = _protocol_campaign(not_before=target_receipt.completed_at)
    private_key = bytes(range(32))
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
        schema_revision="20260828_0027",
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
                ARL1SourceVerificationReceiptV1(
                    subject_kind=ARL1VerificationSubjectKind.ARL0_INTEGRITY,
                    subject_sha256=integrity.integrity_sha256,
                    verification_policy_sha256=_sha("evidence-verifier-policy"),
                    verified_by_principal_id="arl1-evidence-verifier",
                    verified_at=prepared_at,
                ),
                ARL1SourceVerificationReceiptV1(
                    subject_kind=ARL1VerificationSubjectKind.PROTOCOL_CAMPAIGN,
                    subject_sha256=campaign.campaign_sha256,
                    verification_policy_sha256=_sha("evidence-verifier-policy"),
                    verified_by_principal_id="arl1-evidence-verifier",
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
    expires_at = qualified_at + timedelta(days=1)

    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        qualification_private_key=private_key,
        qualified_at=qualified_at,
        expires_at=expires_at,
    )
    verified = verify_arl1_qualification(
        bundle,
        receipt,
        source_verifier=verifier,
        observed_at=qualified_at + timedelta(seconds=1),
    )

    assert verified == receipt
    assert receipt.message.autonomy_level == "ARL-1 Protocol Executor"
    assert receipt.message.claim_ceiling == "bounded_protocol_execution_engineering"
    assert receipt.message.autonomous_research_design_claimed is False
    assert receipt.message.scientific_validity_claimed is False
    assert receipt.message.independent_replication_claimed is False
    assert receipt.message.scientific_authority_conferred is False
    assert len(verifier.calls) == 4


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
        )


def test_arl1_rejects_source_verification_that_predates_its_evidence(arl1_case) -> None:
    bundle, _private_key, _verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    receipts = tuple(
        item.model_copy(
            update={
                "verified_at": campaign.report.reported_at - timedelta(microseconds=1),
                "receipt_id": None,
            }
        )
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
        def verify_arl0_integrity(self, *, evidence, policy):
            del evidence, policy
            return campaign_receipt

    with pytest.raises(ARL1QualificationError, match="fresh ARL-0 verification differs"):
        verify_arl1_evidence_bundle(
            bundle,
            source_verifier=_SwappedVerifier(tuple(verifier._by_subject.values())),
        )


def test_arl1_receipt_rejects_wrong_signer_expiry_and_signature(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    qualified_at = bundle.prepared_at + timedelta(seconds=1)
    expires_at = qualified_at + timedelta(days=1)
    with pytest.raises(ARL1QualificationError, match="signer"):
        issue_arl1_qualification(
            bundle,
            source_verifier=verifier,
            qualification_private_key=b"x" * 32,
            qualified_at=qualified_at,
            expires_at=expires_at,
        )
    with pytest.raises(ARL1QualificationError, match="validity"):
        issue_arl1_qualification(
            bundle,
            source_verifier=verifier,
            qualification_private_key=private_key,
            qualified_at=qualified_at,
            expires_at=qualified_at + timedelta(days=8),
        )

    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        qualification_private_key=private_key,
        qualified_at=qualified_at,
        expires_at=expires_at,
    )
    tampered = receipt.model_copy(update={"signature_ed25519_hex": "0" * 128, "receipt_id": None})
    with pytest.raises(ARL1QualificationError, match="signature"):
        verify_arl1_qualification(
            bundle,
            tampered,
            source_verifier=verifier,
            observed_at=qualified_at + timedelta(seconds=1),
        )


def test_arl1_verifier_rejects_a_correctly_signed_policy_violating_expiry(arl1_case) -> None:
    bundle, private_key, verifier = arl1_case
    qualified_at = bundle.prepared_at + timedelta(seconds=1)
    receipt = issue_arl1_qualification(
        bundle,
        source_verifier=verifier,
        qualification_private_key=private_key,
        qualified_at=qualified_at,
        expires_at=qualified_at + timedelta(days=1),
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
            observed_at=qualified_at + timedelta(seconds=1),
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
            qualification_private_key=private_key,
            qualified_at=datetime(2026, 8, 28, 1, 2, 3),
            expires_at=datetime(2026, 8, 29, 1, 2, 3),
        )
