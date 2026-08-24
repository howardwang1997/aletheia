from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from aletheia.execution.runtime_contracts import (
    EngineeringQualificationBundle,
    QualificationAuthorityVerifier,
    VerifiedEngineeringQualification,
    WorkerNodeEnrollment,
    WorkerNodeEnrollmentMessage,
    WorkerNodeManifest,
    artifact_output_tree_sha256,
    issue_engineering_qualification_grant,
)
from aletheia.execution.runtime_v2_contracts import (
    AcceptedQualificationTerminalSubmission,
    AcceptedRuntimeTermination,
    QualificationTerminalSubmission,
)
from aletheia.execution.schemas import (
    ArtifactCustodyMode,
    ArtifactManifest,
    ArtifactManifestEntry,
    ArtifactVerifiedReceipt,
    InfrastructureAttempt,
    NetworkPolicy,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    ResearchActionProposal,
    ResearchEvent,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    CommittedObservationAdmission,
    CommittedObservationValidationReceipt,
    EngineeringQualificationCustodyVerificationPort,
    ObservationAdmissionCommitPort,
    ObservationAdmissionDecision,
    ObservationAdmissionDisposition,
    ObservationAdmissionPolicy,
    ObservationDatabaseAuthorityPin,
    ObservationValidationCampaignVerificationPort,
    ObservationValidationReceipt,
    RawRunCustodyVerificationPort,
    RawRunEnvelope,
    ResearchActionAuthorityVerificationPort,
    ScientificActionProtocolBinding,
    ScientificBridgeAuthorityPin,
    ScientificBridgeRole,
    ScientificBridgeVerificationError,
    ScientificExecutionAuthorization,
    ScientificObservationArtifactBinding,
    ScientificObservationOutcome,
    ScientificOutcomeBinMapping,
    VerifiedArtifactCustodyProjection,
    VerifiedExecutionAuthorityProjection,
    VerifiedObservationValidationCampaignProjection,
    VerifiedRawRunCustodyProjection,
    commit_observation_admission,
    commit_observation_validation_receipt,
    engineering_qualification_admission_sha256,
    issue_admission_issuance_challenge,
    issue_observation_admission_decision,
    issue_observation_validation_receipt,
    issue_scientific_execution_authorization,
    issue_validation_issuance_challenge,
    scientific_bridge_key_id,
    verify_committed_observation_admission,
    verify_committed_observation_validation_receipt,
    verify_observation_admission_decision,
    verify_observation_validation_receipt,
    verify_scientific_execution_authorization,
    verify_scientific_execution_authorization_historical,
    verify_validation_issuance_challenge,
)

_EXECUTION_TESTS = Path(__file__).resolve().parents[1] / "execution"
sys.path.insert(0, str(_EXECUTION_TESTS))
from test_runtime_contracts import (  # noqa: E402
    NOW,
    PRIVATE_KEY as QUALIFICATION_PRIVATE_KEY,
    QualificationCase,
    _AuthorityResolver,
    _Resolver,
    _intermediate_qualification_case,
)

EXECUTION_AUTHORITY_PRIVATE_KEY = bytes(range(32, 64))
VALIDATOR_PRIVATE_KEY = bytes(range(64, 96))
ADMISSION_PRIVATE_KEY = bytes(range(96, 128))
NODE_PRIVATE_KEY = bytes(range(128, 160))
DATABASE_PRIVATE_KEY = bytes(range(160, 192))


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _public_key_hex(private_key: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _pin(
    role: ScientificBridgeRole,
    private_key: bytes,
    principal_id: str,
    policy_label: str,
) -> ScientificBridgeAuthorityPin:
    public_key = _public_key_hex(private_key)
    return ScientificBridgeAuthorityPin(
        role=role,
        policy_sha256=_digest(policy_label),
        principal_id=principal_id,
        key_id=scientific_bridge_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(days=2),
    )


def _database_pin() -> ObservationDatabaseAuthorityPin:
    public_key = _public_key_hex(DATABASE_PRIVATE_KEY)
    return ObservationDatabaseAuthorityPin(
        policy_sha256=_digest("scientific-observation-database-policy"),
        principal_id="principal:scientific-observation-database",
        key_id=scientific_bridge_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(days=2),
        expires_at=NOW + timedelta(days=2),
    )


def _worker_manifest(qualification: QualificationCase) -> WorkerNodeManifest:
    intent = qualification.bundle.intent
    public_key = _public_key_hex(NODE_PRIVATE_KEY)
    return WorkerNodeManifest(
        node_id="node:scientific-bridge-fixture",
        site_id="site:scientific-bridge-fixture",
        principal_id="principal:scientific-bridge-node",
        agent_version="test-v1",
        agent_implementation_sha256=_digest("scientific-bridge-node-agent"),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="docker-test",
        sandbox_policy_sha256=_digest("scientific-bridge-sandbox-policy"),
        resource_class_ids=intent.resource_request.accepted_resource_class_ids,
        allowed_data_classifications=tuple(
            sorted({item.data_classification for item in intent.expected_artifacts})
        ),
        network_policies=(NetworkPolicy.NONE,),
        egress_policy_sha256=_digest("scientific-bridge-egress-policy"),
        node_signing_key_id=scientific_bridge_key_id(public_key),
        node_signing_public_key_ed25519_hex=public_key,
        key_valid_from=NOW - timedelta(days=2),
        key_expires_at=NOW + timedelta(days=2),
        frozen_at=NOW - timedelta(days=1),
    )


def _qualification_for_manifest(
    qualification: QualificationCase,
    manifest: WorkerNodeManifest,
) -> QualificationCase:
    original = qualification.bundle
    quote = type(original.cost_quote).model_validate(
        {
            **original.cost_quote.model_dump(mode="python"),
            "permitted_node_manifest_sha256s": (manifest.manifest_sha256,),
            "selected_node_manifest_sha256": manifest.manifest_sha256,
        }
    )
    bundle = EngineeringQualificationBundle(
        compilation_request=original.compilation_request,
        compilation_result=original.compilation_result,
        work_order=original.work_order,
        intent=original.intent,
        prior_execution_receipt=original.prior_execution_receipt,
        input_artifact_verified_receipt_sha256s=(original.input_artifact_verified_receipt_sha256s),
        budget_authorization=original.budget_authorization,
        cost_quote=quote,
    )
    authority_resolver = _AuthorityResolver(bundle)
    grant = issue_engineering_qualification_grant(
        bundle,
        pin=qualification.pin,
        artifact_resolver=_Resolver((qualification.resolution,)),
        authority_resolver=authority_resolver,
        private_key=QUALIFICATION_PRIVATE_KEY,
        authorized_at=qualification.grant.message.authorized_at,
        expires_at=qualification.grant.message.expires_at,
    )
    return QualificationCase(
        request=qualification.request,
        result=qualification.result,
        bundle=bundle,
        grant=grant,
        pin=qualification.pin,
        authority_resolver=authority_resolver,
        resolution=qualification.resolution,
        observed_at=qualification.observed_at,
    )


@dataclass
class RecordingActionAuthority:
    calls: list[tuple[str, datetime]] = field(default_factory=list)
    fail: bool = False
    returned_binding_sha256: str | None = None

    def verify_action_protocol_binding(
        self,
        *,
        binding: ScientificActionProtocolBinding,
        observed_at: datetime,
    ) -> str:
        self.calls.append((binding.binding_sha256, observed_at))
        if self.fail:
            raise RuntimeError("action authority unavailable")
        return self.returned_binding_sha256 or binding.binding_sha256


@dataclass
class RecordingQualificationCustody:
    online_calls: list[tuple[str, str, datetime]] = field(default_factory=list)
    admission_calls: list[tuple[str, str, str, datetime]] = field(default_factory=list)
    fail_online: bool = False
    fail_admission: bool = False
    rebound_admission: bool = False

    @staticmethod
    def _verified(
        *,
        bundle,
        grant,
        verified_at: datetime,
        rebound: bool = False,
    ) -> VerifiedEngineeringQualification:
        prior_sha256 = (
            bundle.prior_execution_receipt.execution_receipt_sha256
            if bundle.prior_execution_receipt is not None
            else None
        )
        return VerifiedEngineeringQualification(
            grant_sha256=_digest("rebound-grant") if rebound else grant.grant_sha256,
            bundle_sha256=bundle.bundle_sha256,
            intent_sha256=bundle.intent.intent_sha256,
            execution_id=bundle.intent.execution_id,
            infrastructure_attempt_id=(
                bundle.intent.infrastructure_attempt.infrastructure_attempt_id
            ),
            input_artifact_verified_receipt_sha256s=(
                bundle.input_artifact_verified_receipt_sha256s
            ),
            prior_execution_receipt_sha256=prior_sha256,
            budget_authorization_sha256=bundle.budget_authorization.authorization_sha256,
            cost_quote_sha256=bundle.cost_quote.quote_sha256,
            verified_at=verified_at,
        )

    def verify_engineering_qualification_custody(
        self,
        *,
        bundle,
        grant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification:
        self.online_calls.append((bundle.bundle_sha256, grant.grant_sha256, observed_at))
        if self.fail_online:
            raise RuntimeError("qualification custody unavailable")
        return self._verified(bundle=bundle, grant=grant, verified_at=observed_at)

    def verify_qualification_admission(
        self,
        *,
        qualification_admission_sha256: str,
        bundle,
        grant,
        observed_at: datetime,
    ) -> VerifiedEngineeringQualification:
        self.admission_calls.append(
            (
                qualification_admission_sha256,
                bundle.bundle_sha256,
                grant.grant_sha256,
                observed_at,
            )
        )
        if self.fail_admission:
            raise RuntimeError("qualification admission archive unavailable")
        return self._verified(
            bundle=bundle,
            grant=grant,
            verified_at=observed_at,
            rebound=self.rebound_admission,
        )


@dataclass
class RecordingRawRunCustody:
    worker_manifest: WorkerNodeManifest
    worker_enrollment: WorkerNodeEnrollment
    calls: list[tuple[str, datetime]] = field(default_factory=list)
    fail: bool = False
    retroactive_lineage: bool = False
    returned_raw_run_sha256: str | None = None

    def verify_raw_run_custody(
        self,
        *,
        raw_run: RawRunEnvelope,
        observed_at: datetime,
    ) -> VerifiedRawRunCustodyProjection:
        self.calls.append((raw_run.raw_run_sha256, observed_at))
        if self.fail:
            raise RuntimeError("raw run custody unavailable")
        if self.retroactive_lineage:
            raise RuntimeError("SEA was registered after qualification admission or launch")
        authorization = raw_run.scientific_authorization.message
        grant = authorization.qualification_grant.message
        accepted = raw_run.accepted_runtime_termination
        terminal = raw_run.accepted_terminal_submission
        submission = raw_run.terminal_submission
        quote = authorization.qualification_bundle.cost_quote

        def authority(principal_id: str, key_id: str, policy_sha256: str):
            return VerifiedExecutionAuthorityProjection(
                principal_id=principal_id,
                key_id=key_id,
                policy_sha256=policy_sha256,
            )

        node_authority = authority(
            self.worker_manifest.principal_id,
            self.worker_manifest.node_signing_key_id,
            _digest("scientific-bridge-node-execution-policy"),
        )
        projection = VerifiedRawRunCustodyProjection(
            raw_run_sha256=raw_run.raw_run_sha256,
            scientific_execution_authorization_sha256=(
                raw_run.scientific_authorization.authorization_sha256
            ),
            scientific_slot_id=authorization.scientific_slot_id,
            qualification_admission_sha256=raw_run.qualification_admission_sha256,
            sea_registration_sha256=_digest("scientific-bridge-sea-registration"),
            sea_registered_at=authorization.authorized_at + timedelta(seconds=1),
            qualification_admitted_at=authorization.authorized_at + timedelta(seconds=2),
            resource_reservation_sha256=_digest("scientific-bridge-resource-reservation"),
            resource_reserved_at=authorization.authorized_at + timedelta(seconds=3),
            runtime_launch_sha256=_digest("scientific-bridge-runtime-launch"),
            runtime_launched_at=authorization.authorized_at + timedelta(seconds=4),
            terminal_submission_sha256=submission.terminal_submission_sha256,
            terminal_acceptance_sha256=terminal.accepted_terminal_submission_sha256,
            terminal_accepted_at=terminal.accepted_at,
            cost_quote_sha256=quote.quote_sha256,
            quoted_worker_node_manifest=self.worker_manifest,
            terminal_worker_node_manifest=self.worker_manifest,
            worker_node_enrollment=self.worker_enrollment,
            allocator_authority=authority(
                quote.quoted_by_principal_id,
                _digest("scientific-bridge-allocator-key"),
                quote.pricing_policy_sha256,
            ),
            qualification_authority=authority(
                grant.authorized_by_principal_id,
                grant.authorization_key_id,
                grant.qualification_authority_policy_sha256,
            ),
            node_enrollment_authority=authority(
                self.worker_enrollment.message.enrolled_by_principal_id,
                self.worker_enrollment.message.enrollment_authority_key_id,
                self.worker_enrollment.message.node_enrollment_policy_sha256,
            ),
            node_execution_authority=node_authority,
            runtime_control_authority=authority(
                accepted.accepted_by_principal_id,
                accepted.acceptance_key_id,
                accepted.runtime_control_policy_sha256,
            ),
            terminal_submission_authority=node_authority,
            terminal_acceptance_authority=authority(
                terminal.accepted_by_principal_id,
                terminal.acceptance_key_id,
                terminal.runtime_control_policy_sha256,
            ),
            artifact_manifest_sha256=raw_run.artifact_manifest.manifest_sha256,
            output_tree_sha256=submission.output_tree_sha256,
            artifact_verified_receipt_sha256s=(submission.artifact_verified_receipt_sha256s),
            fresh_artifacts=tuple(
                VerifiedArtifactCustodyProjection(
                    artifact_key=receipt.artifact.artifact_key,
                    content_sha256=receipt.artifact.content_sha256,
                    artifact_verified_receipt_sha256=(receipt.verified_receipt_sha256),
                    authority=authority(
                        receipt.verifier_principal_id,
                        _digest("scientific-bridge-artifact-verifier-key"),
                        _digest("scientific-bridge-artifact-verifier-policy"),
                    ),
                )
                for receipt in raw_run.artifact_verified_receipts
            ),
            verified_at=observed_at,
        )
        if self.returned_raw_run_sha256 is not None:
            return projection.model_copy(update={"raw_run_sha256": self.returned_raw_run_sha256})
        return projection


@dataclass
class RecordingValidationCampaignCustody:
    calls: list[tuple[str, str, str, str, datetime]] = field(default_factory=list)
    projection: VerifiedObservationValidationCampaignProjection | None = None
    fail: bool = False

    def verify_observation_validation_campaign(
        self,
        *,
        campaign_sha256: str,
        raw_run: RawRunEnvelope,
        expected_validator_manifest_sha256: str,
        expected_observation_validation_policy_sha256: str,
        observed_at: datetime,
    ) -> VerifiedObservationValidationCampaignProjection:
        self.calls.append(
            (
                campaign_sha256,
                raw_run.raw_run_sha256,
                expected_validator_manifest_sha256,
                expected_observation_validation_policy_sha256,
                observed_at,
            )
        )
        if self.fail:
            raise RuntimeError("validation campaign archive unavailable")
        if self.projection is None:
            raise RuntimeError("validation campaign projection is absent")
        return self.projection


@dataclass(frozen=True)
class BridgeCase:
    qualification: QualificationCase
    worker_manifest: WorkerNodeManifest
    worker_enrollment: WorkerNodeEnrollment
    binding: ScientificActionProtocolBinding
    execution_pin: ScientificBridgeAuthorityPin
    validator_pin: ScientificBridgeAuthorityPin
    admission_pin: ScientificBridgeAuthorityPin
    database_pin: ObservationDatabaseAuthorityPin
    authorization: ScientificExecutionAuthorization
    qualification_authority: QualificationAuthorityVerifier
    action_authority: RecordingActionAuthority
    qualification_custody: RecordingQualificationCustody
    raw_run_custody: RecordingRawRunCustody
    validation_campaign_custody: RecordingValidationCampaignCustody
    qualification_admission_sha256: str


def _bridge_case() -> BridgeCase:
    qualification = _intermediate_qualification_case(include_producer_lineage=True)
    worker_manifest = _worker_manifest(qualification)
    qualification = _qualification_for_manifest(qualification, worker_manifest)
    worker_enrollment = WorkerNodeEnrollment(
        message=WorkerNodeEnrollmentMessage(
            node_manifest_sha256=worker_manifest.manifest_sha256,
            node_id=worker_manifest.node_id,
            site_id=worker_manifest.site_id,
            principal_id=worker_manifest.principal_id,
            node_signing_key_id=worker_manifest.node_signing_key_id,
            node_signing_public_key_ed25519_hex=(
                worker_manifest.node_signing_public_key_ed25519_hex
            ),
            node_enrollment_policy_sha256=_digest("scientific-bridge-node-enrollment-policy"),
            enrolled_by_principal_id="principal:scientific-bridge-node-enrollment",
            enrollment_authority_key_id=_digest("scientific-bridge-node-enrollment-key"),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
        ),
        signature_ed25519_hex="4" * 128,
    )
    bundle = qualification.bundle
    protocol = bundle.compilation_request.protocol
    scope = protocol.graph_scope
    action = ResearchActionProposal(
        action_id="action:scientific-bridge-test",
        quest_id=bundle.intent.quest_id,
        charter_ref=KernelObjectRef(
            object_kind=KernelObjectKind.CHARTER,
            object_id="charter:scientific-bridge-test",
            object_sha256=_digest("scientific-bridge-charter"),
            quest_id=bundle.intent.quest_id,
        ),
        question_ref=scope.question_ref,
        basis_tail_event_sha256=_digest("scientific-bridge-basis-tail"),
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="Run the preregistered discriminating observation.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        cost_receipt_sha256=_digest("scientific-bridge-action-cost"),
        risk_receipt_sha256=_digest("scientific-bridge-action-risk"),
        requested_authority_class="authority:scientific-execution",
        proposed_by_principal_id="principal:scientific-action-proposer",
        proposed_at=protocol.authored_at - timedelta(minutes=3),
    )
    proposed = ResearchEvent(
        quest_id=action.quest_id,
        sequence=10,
        parent_event_sha256=action.basis_tail_event_sha256,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=action.object_ref,
            branch_id=scope.branch_id,
        ),
        command_sha256=_digest("scientific-bridge-proposal-command"),
        principal_id=action.proposed_by_principal_id,
        authorization_receipt_sha256=_digest("scientific-bridge-proposal-authorization"),
        committed_at=protocol.authored_at - timedelta(minutes=2),
    )
    authorized = ResearchEvent(
        quest_id=action.quest_id,
        sequence=11,
        parent_event_sha256=proposed.event_sha256,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=action.action_id,
            branch_id=scope.branch_id,
        ),
        command_sha256=_digest("scientific-bridge-action-authorization-command"),
        principal_id="principal:research-action-authorizer",
        authorization_receipt_sha256=_digest("scientific-bridge-action-authorization"),
        committed_at=protocol.authored_at - timedelta(minutes=1),
    )
    node = next(
        item for item in bundle.work_order.nodes if item.node_id == bundle.intent.work_order_node_id
    )
    binding = ScientificActionProtocolBinding(
        action=action,
        action_proposed_event=proposed,
        action_authorized_event=authorized,
        authorized_graph_snapshot_sha256=scope.graph_snapshot_sha256,
        compilation_request=bundle.compilation_request,
        compilation_result=bundle.compilation_result,
        compilation_receipt=bundle.compilation_result.receipt,
        work_order=bundle.work_order,
        work_order_node=node,
        replicate_slot=bundle.intent.replicate_slot,
        bound_at=protocol.authored_at,
    )
    execution_pin = _pin(
        ScientificBridgeRole.EXECUTION_AUTHORIZER,
        EXECUTION_AUTHORITY_PRIVATE_KEY,
        "principal:scientific-execution-authority",
        "scientific-execution-authority-policy",
    )
    validator_pin = _pin(
        ScientificBridgeRole.OBSERVATION_VALIDATOR,
        VALIDATOR_PRIVATE_KEY,
        "principal:fixture_validator",
        "scientific-observation-validator-policy",
    )
    admission_pin = _pin(
        ScientificBridgeRole.OBSERVATION_ADMITTER,
        ADMISSION_PRIVATE_KEY,
        "principal:fixture_claim_approver",
        "scientific-observation-admission-authority-policy",
    )
    database_pin = _database_pin()
    validator_manifest_sha256 = _digest("scientific-bridge-validator-manifest")
    observation_validation_policy_sha256 = _digest(
        "scientific-bridge-observation-validation-policy"
    )
    admission_policy = ObservationAdmissionPolicy(
        policy_id="scientific-bridge-admission-policy-v1",
        validator_manifest_sha256=validator_manifest_sha256,
        observation_validation_policy_sha256=observation_validation_policy_sha256,
        analysis_outcome_space_sha256=protocol.analysis_plan.outcome_space_sha256,
        outcome_bin_mappings=(
            ScientificOutcomeBinMapping(
                outcome_bin_id="outcome.inconclusive",
                outcome=ScientificObservationOutcome.INCONCLUSIVE,
            ),
            ScientificOutcomeBinMapping(
                outcome_bin_id="outcome.negative",
                outcome=ScientificObservationOutcome.NEGATIVE,
            ),
            ScientificOutcomeBinMapping(
                outcome_bin_id="outcome.positive",
                outcome=ScientificObservationOutcome.POSITIVE,
            ),
        ),
        frozen_at=protocol.authored_at - timedelta(days=1),
    )
    protocol_step = next(item for item in protocol.steps if item.step_id == node.protocol_step_id)
    observable_output_binding = node.observable_output_bindings[0]
    observable = next(
        item
        for item in protocol.observables
        if item.observable_sha256 == observable_output_binding.observable_spec_sha256
    )
    data_port = next(
        item
        for item in protocol.data_ports
        if item.port_id == observable_output_binding.output_port_id
    )
    observation_artifact = next(
        item
        for item in node.expected_artifacts
        if item.artifact_key == observable_output_binding.output_port_id
    )
    scientific_observation_artifact_binding = ScientificObservationArtifactBinding(
        work_order_node=node,
        protocol_step=protocol_step,
        observable_output_binding=observable_output_binding,
        observable=observable,
        data_port=data_port,
        expected_artifact=observation_artifact,
        observation_namespace_sha256=_digest("scientific-bridge-observation-namespace"),
        selection_campaign_sha256=_digest("scientific-bridge-selection-campaign"),
        prediction_campaign_sha256=_digest("scientific-bridge-prediction-campaign"),
        prediction_commitment_sha256=_digest("scientific-bridge-prediction-commitment"),
    )
    qualification_authority = QualificationAuthorityVerifier(qualification.pin)
    action_authority = RecordingActionAuthority()
    qualification_custody = RecordingQualificationCustody()
    raw_run_custody = RecordingRawRunCustody(
        worker_manifest=worker_manifest,
        worker_enrollment=worker_enrollment,
    )
    validation_campaign_custody = RecordingValidationCampaignCustody()
    authorization = issue_scientific_execution_authorization(
        action_protocol_binding=binding,
        qualification_bundle=bundle,
        qualification_grant=qualification.grant,
        validator_manifest_sha256=validator_manifest_sha256,
        observation_validation_policy_sha256=observation_validation_policy_sha256,
        admission_policy=admission_policy,
        scientific_observation_artifact_binding=(scientific_observation_artifact_binding),
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        execution_authority_pin=execution_pin,
        validator_authority_pin=validator_pin,
        admission_authority_pin=admission_pin,
        private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
        authorized_at=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=9),
        observation_admission_deadline=NOW + timedelta(hours=2),
    )
    verified = RecordingQualificationCustody._verified(
        bundle=bundle,
        grant=qualification.grant,
        verified_at=NOW + timedelta(minutes=5),
    )
    return BridgeCase(
        qualification=qualification,
        worker_manifest=worker_manifest,
        worker_enrollment=worker_enrollment,
        binding=binding,
        execution_pin=execution_pin,
        validator_pin=validator_pin,
        admission_pin=admission_pin,
        database_pin=database_pin,
        authorization=authorization,
        qualification_authority=qualification_authority,
        action_authority=action_authority,
        qualification_custody=qualification_custody,
        raw_run_custody=raw_run_custody,
        validation_campaign_custody=validation_campaign_custody,
        qualification_admission_sha256=engineering_qualification_admission_sha256(verified),
    )


def _verify_authorization(
    case: BridgeCase, authorization: ScientificExecutionAuthorization
) -> None:
    verify_scientific_execution_authorization(
        authorization=authorization,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        observed_at=NOW + timedelta(minutes=6),
    )


def _raw_run(
    case: BridgeCase,
    disposition: str = "process_succeeded",
    *,
    runtime_ended_at_override: datetime | None = None,
    omit_artifacts: bool = False,
    artifact_entry_updates: dict[str, object] | None = None,
    add_undeclared_output: bool = False,
    artifact_verified_at_override: datetime | None = None,
    terminal_submitted_at_override: datetime | None = None,
    runtime_control_principal_id: str = "principal:runtime-control",
    runtime_control_key_id: str | None = None,
    runtime_control_policy_sha256: str | None = None,
    accepted_terminal_principal_id: str | None = None,
    accepted_terminal_key_id: str | None = None,
    accepted_terminal_policy_sha256: str | None = None,
    qualification_admission_sha256: str | None = None,
) -> RawRunEnvelope:
    intent = case.qualification.bundle.intent
    runtime_key_id = runtime_control_key_id or _digest("runtime-control-key")
    runtime_policy_sha256 = runtime_control_policy_sha256 or _digest("runtime-control-policy")
    if runtime_ended_at_override is not None:
        runtime_ended_at = runtime_ended_at_override
    elif disposition == "timeout":
        runtime_ended_at = NOW + timedelta(minutes=61)
    else:
        runtime_ended_at = NOW + timedelta(minutes=20)
    hard_deadline = NOW + timedelta(hours=1)
    artifact_submission_deadline = NOW + timedelta(minutes=90)
    accepted = AcceptedRuntimeTermination(
        challenge_sha256=_digest(f"runtime-challenge:{disposition}"),
        attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        runtime_preparation_sha256=_digest("runtime-preparation"),
        node_runtime_launch_receipt_sha256=_digest("node-launch-receipt"),
        runtime_launch_authorization_request_sha256=_digest("launch-authorization-request"),
        runtime_launch_authorization_sha256=_digest("launch-authorization"),
        node_runtime_termination_receipt_sha256=_digest("node-termination-receipt"),
        inspection_sequence=1,
        runtime_identity_sha256=_digest("runtime-identity"),
        runtime_inspection_evidence_sha256=_digest("runtime-inspection"),
        engine_terminal_journal_sha256=_digest("runtime-terminal-journal"),
        fencing_epoch=1,
        lease_token_sha256=_digest("runtime-lease-token"),
        runtime_ended_at=runtime_ended_at,
        exit_code=1 if disposition == "process_failed" else 0,
        hard_deadline=hard_deadline,
        artifact_submission_deadline=artifact_submission_deadline,
        proof_signed_at=runtime_ended_at + timedelta(seconds=1),
        proof_expires_at=runtime_ended_at + timedelta(minutes=10),
        accepted_at=runtime_ended_at + timedelta(seconds=2),
        billable_ended_at=runtime_ended_at + timedelta(seconds=2),
        runtime_control_policy_sha256=runtime_policy_sha256,
        accepted_by_principal_id=runtime_control_principal_id,
        acceptance_key_id=runtime_key_id,
        signature_ed25519_hex="1" * 128,
    )
    if disposition == "invalid_output" or omit_artifacts:
        entries: tuple[ArtifactManifestEntry, ...] = ()
    else:
        entries = tuple(
            ArtifactManifestEntry(
                expected_artifact_id=item.expected_artifact_id,
                artifact_key=item.artifact_key,
                role=item.role,
                content_sha256=_digest(f"raw-run-content:{item.artifact_key}"),
                bytes=1_024,
                media_type=item.media_type,
                schema_sha256=item.schema_sha256,
                quarantine_ref=f"quarantine/{item.artifact_key}",
            )
            for item in intent.expected_artifacts
        )
        if artifact_entry_updates is not None:
            first = entries[0]
            entries = (
                ArtifactManifestEntry.model_validate(
                    {**first.model_dump(mode="python"), **artifact_entry_updates}
                ),
                *entries[1:],
            )
        if add_undeclared_output:
            entries = (
                *entries,
                ArtifactManifestEntry(
                    expected_artifact_id="art_" + "9" * 32,
                    artifact_key="undeclared.output",
                    role=intent.expected_artifacts[0].role,
                    content_sha256=_digest("undeclared-output"),
                    bytes=1,
                    media_type=intent.expected_artifacts[0].media_type,
                    schema_sha256=intent.expected_artifacts[0].schema_sha256,
                    quarantine_ref="quarantine/undeclared.output",
                ),
            )
        entries = tuple(sorted(entries, key=lambda item: item.artifact_key))
    manifest = ArtifactManifest(
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        replicate_slot_id=intent.replicate_slot.replicate_slot_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        entries=entries,
        produced_at=runtime_ended_at,
    )
    receipts = tuple(
        ArtifactVerifiedReceipt(
            artifact_manifest_sha256=manifest.manifest_sha256,
            producer_attempt_id=manifest.infrastructure_attempt_id,
            artifact=entry,
            custody_mode=ArtifactCustodyMode.CENTRAL_REHASH,
            verifier_principal_id="principal:artifact-verifier",
            object_store_id="store:scientific-bridge-test",
            final_object_ref=f"objects/{entry.content_sha256}",
            final_object_version="generation-1",
            verified_at=(artifact_verified_at_override or runtime_ended_at + timedelta(minutes=1)),
        )
        for entry in entries
    )
    receipt_hashes = tuple(sorted(item.verified_receipt_sha256 for item in receipts))
    terminal_submission = QualificationTerminalSubmission(
        node_manifest_sha256=case.worker_manifest.manifest_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        attempt_id=accepted.attempt_id,
        node_inventory_sha256=_digest("runtime-node-inventory"),
        resource_lease_sha256=_digest("runtime-resource-lease"),
        fencing_epoch=accepted.fencing_epoch,
        lease_token_sha256=accepted.lease_token_sha256,
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        artifact_manifest_sha256=manifest.manifest_sha256,
        output_tree_sha256=artifact_output_tree_sha256(manifest),
        artifact_verified_receipt_sha256s=receipt_hashes,
        disposition=disposition,
        submitted_at=(terminal_submitted_at_override or runtime_ended_at + timedelta(minutes=2)),
        signing_key_id=case.worker_manifest.node_signing_key_id,
        signature_ed25519_hex="2" * 128,
    )
    accepted_terminal = AcceptedQualificationTerminalSubmission(
        attempt_id=accepted.attempt_id,
        node_manifest_sha256=terminal_submission.node_manifest_sha256,
        terminal_submission_sha256=terminal_submission.terminal_submission_sha256,
        accepted_runtime_termination_sha256=accepted.accepted_termination_sha256,
        artifact_manifest_sha256=manifest.manifest_sha256,
        output_tree_sha256=terminal_submission.output_tree_sha256,
        artifact_verified_receipt_sha256s=receipt_hashes,
        disposition=disposition,
        node_submitted_at=terminal_submission.submitted_at,
        artifact_submission_deadline=accepted.artifact_submission_deadline,
        accepted_at=runtime_ended_at + timedelta(minutes=3),
        runtime_control_policy_sha256=(accepted_terminal_policy_sha256 or runtime_policy_sha256),
        accepted_by_principal_id=(accepted_terminal_principal_id or runtime_control_principal_id),
        acceptance_key_id=(accepted_terminal_key_id or runtime_key_id),
        signature_ed25519_hex="3" * 128,
    )
    return RawRunEnvelope(
        scientific_authorization=case.authorization,
        qualification_admission_sha256=(
            qualification_admission_sha256 or case.qualification_admission_sha256
        ),
        accepted_runtime_termination=accepted,
        terminal_submission=terminal_submission,
        accepted_terminal_submission=accepted_terminal,
        artifact_manifest=manifest,
        artifact_verified_receipts=receipts,
        assembled_at=runtime_ended_at + timedelta(minutes=4),
    )


def _issue_validation_receipt(
    case: BridgeCase,
    *,
    raw_run: RawRunEnvelope,
    validation_campaign_sha256: str | None,
    validated_at: datetime,
) -> ObservationValidationReceipt:
    challenge = issue_validation_issuance_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=validation_campaign_sha256,
        nonce_sha256=_digest(
            f"validation-challenge:{raw_run.raw_run_sha256}:{validated_at.isoformat()}"
        ),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=validated_at,
        expires_at=validated_at + timedelta(minutes=10),
    )
    return issue_observation_validation_receipt(
        raw_run=raw_run,
        validation_campaign_sha256=validation_campaign_sha256,
        issuance_challenge=challenge,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        private_key=VALIDATOR_PRIVATE_KEY,
    )


def _blocked_receipt(
    case: BridgeCase,
    disposition: str,
) -> ObservationValidationReceipt:
    raw_run = _raw_run(case, disposition)
    return _issue_validation_receipt(
        case,
        raw_run=raw_run,
        validation_campaign_sha256=None,
        validated_at=raw_run.assembled_at + timedelta(minutes=1),
    )


def _validated_receipt(
    case: BridgeCase,
    *,
    outcome_bin_id: str = "outcome.negative",
    disposition: BridgeValidationDisposition = (BridgeValidationDisposition.VALIDATED_CONFIRMATION),
    blocker_codes: tuple[str, ...] = (),
) -> ObservationValidationReceipt:
    raw_run = _raw_run(case)
    campaign_sha256 = _digest(f"validation-campaign:{outcome_bin_id}")
    observation_entry = next(
        item
        for item in raw_run.artifact_manifest.entries
        if item.artifact_key == case.authorization.message.observation_artifact_key
    )
    projection = VerifiedObservationValidationCampaignProjection(
        observation_staging_receipt_sha256=_digest(f"observation-staging:{outcome_bin_id}"),
        validation_request_sha256=_digest(f"validation-request:{outcome_bin_id}"),
        campaign_sha256=campaign_sha256,
        committed_campaign_sha256=_digest(f"committed-validation-campaign:{outcome_bin_id}"),
        validation_batch_sha256=_digest(f"validation-batch:{outcome_bin_id}"),
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        observation_validation_policy_sha256=(
            case.authorization.message.observation_validation_policy_sha256
        ),
        observation_namespace_sha256=(
            case.authorization.message.scientific_observation_artifact_binding.observation_namespace_sha256
        ),
        protocol_sha256=(
            case.authorization.message.action_protocol_binding.compilation_request.protocol.protocol_sha256
        ),
        selection_campaign_sha256=(
            case.authorization.message.scientific_observation_artifact_binding.selection_campaign_sha256
        ),
        prediction_campaign_sha256=(
            case.authorization.message.scientific_observation_artifact_binding.prediction_campaign_sha256
        ),
        prediction_commitment_sha256=(
            case.authorization.message.scientific_observation_artifact_binding.prediction_commitment_sha256
        ),
        prediction_commitment_receipt_sha256=_digest(
            f"prediction-commitment-receipt:{outcome_bin_id}"
        ),
        observation_receipt_sha256=_digest(f"observation-receipt:{outcome_bin_id}"),
        namespace_seal_sha256=_digest(f"namespace-seal:{outcome_bin_id}"),
        raw_run_sha256=raw_run.raw_run_sha256,
        scientific_observation_artifact_binding_sha256=(
            case.authorization.message.scientific_observation_artifact_binding.binding_sha256
        ),
        artifact_verified_receipt_sha256=next(
            item.verified_receipt_sha256
            for item in raw_run.artifact_verified_receipts
            if item.artifact.artifact_key == case.authorization.message.observation_artifact_key
        ),
        raw_observation_content_sha256=observation_entry.content_sha256,
        outcome_bin_id=outcome_bin_id,
        disposition=disposition,
        blocker_codes=blocker_codes,
        generated_at=raw_run.assembled_at + timedelta(minutes=1),
        committed_at=raw_run.assembled_at + timedelta(seconds=90),
    )
    case.validation_campaign_custody.projection = projection
    return _issue_validation_receipt(
        case,
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
        validated_at=raw_run.assembled_at + timedelta(minutes=2),
    )


def _commit_validation(
    case: BridgeCase,
    receipt: ObservationValidationReceipt,
) -> CommittedObservationValidationReceipt:
    return commit_observation_validation_receipt(
        receipt=receipt,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        registered_at=receipt.message.validated_at + timedelta(seconds=1),
        committed_at=receipt.message.validated_at + timedelta(seconds=2),
    )


def _issue_admission_decision(
    case: BridgeCase,
    *,
    receipt: ObservationValidationReceipt,
    disposition: ObservationAdmissionDisposition,
    reason_codes: tuple[str, ...],
    decided_at: datetime | None = None,
) -> tuple[ObservationAdmissionDecision, CommittedObservationValidationReceipt]:
    committed_validation = _commit_validation(case, receipt)
    issued_at = decided_at or receipt.message.validated_at + timedelta(minutes=1)
    challenge = issue_admission_issuance_challenge(
        committed_validation_receipt=committed_validation,
        nonce_sha256=_digest(
            f"admission-challenge:{committed_validation.committed_receipt_sha256}:"
            f"{issued_at.isoformat()}"
        ),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=10),
    )
    decision = issue_observation_admission_decision(
        committed_validation_receipt=committed_validation,
        issuance_challenge=challenge,
        disposition=disposition,
        reason_codes=reason_codes,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        private_key=ADMISSION_PRIVATE_KEY,
    )
    return decision, committed_validation


def _commit_admission(
    case: BridgeCase,
    decision: ObservationAdmissionDecision,
) -> CommittedObservationAdmission:
    return commit_observation_admission(
        decision=decision,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        registered_at=decision.message.decided_at + timedelta(seconds=1),
        committed_at=decision.message.decided_at + timedelta(seconds=2),
    )


def test_signed_authorization_closes_action_protocol_qualification_and_one_slot() -> None:
    case = _bridge_case()
    _verify_authorization(case, case.authorization)

    message = case.authorization.message
    assert message.action_protocol_binding.binding_sha256 == case.binding.binding_sha256
    assert message.scientific_slot_id == case.binding.scientific_slot_id
    assert message.qualification_bundle.qualification_only is True
    assert message.qualification_bundle.scientific_admission_allowed is False
    assert message.qualification_grant.message.qualification_only is True
    assert message.qualification_grant.message.scientific_admission_allowed is False
    assert message.source_qualification_only is True
    assert message.source_scientific_admission_allowed is False
    assert message.direct_artifact_admission_allowed is False
    artifact_binding = message.scientific_observation_artifact_binding
    assert artifact_binding.work_order_node == case.binding.work_order_node
    assert artifact_binding.artifact_key == message.observation_artifact_key
    assert artifact_binding.binding_sha256
    assert "observation_artifact_key" not in type(message).model_fields
    assert message.action_protocol_binding.replicate_slot.slot_count == 1
    assert message.qualification_bundle.intent.infrastructure_attempt.attempt_number == 1
    assert "action_to_protocol_binding_receipt_sha256" not in type(message).model_fields
    assert case.action_authority.calls
    assert case.qualification_custody.online_calls


def test_online_authorization_expires_but_historical_chain_remains_verifiable() -> None:
    case = _bridge_case()
    action_calls = len(case.action_authority.calls)
    qualification_calls = len(case.qualification_custody.online_calls)
    with pytest.raises(ScientificBridgeVerificationError, match="not live"):
        verify_scientific_execution_authorization(
            authorization=case.authorization,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            observed_at=case.authorization.message.expires_at,
        )
    assert len(case.action_authority.calls) == action_calls
    assert len(case.qualification_custody.online_calls) == qualification_calls

    verified = verify_scientific_execution_authorization_historical(
        authorization=case.authorization,
        qualification_authority=case.qualification_authority,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        observed_at=NOW + timedelta(hours=1),
    )
    assert verified == case.authorization


def test_validator_and_admitter_pins_must_be_active_when_sea_is_signed() -> None:
    case = _bridge_case()
    future_validator = ScientificBridgeAuthorityPin.model_validate(
        {
            **case.validator_pin.model_dump(mode="python"),
            "valid_from": case.authorization.message.authorized_at + timedelta(seconds=1),
        }
    )
    with pytest.raises(ScientificBridgeVerificationError, match="were not active"):
        verify_scientific_execution_authorization_historical(
            authorization=case.authorization,
            qualification_authority=case.qualification_authority,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=future_validator,
            admission_authority_pin=case.admission_pin,
            observed_at=NOW + timedelta(minutes=6),
        )


def test_binding_rejects_arbitrary_protocol_snapshot_and_reused_action_authority() -> None:
    case = _bridge_case()
    with pytest.raises(ValidationError, match="graph scope"):
        ScientificActionProtocolBinding.model_validate(
            {
                **case.binding.model_dump(mode="python"),
                "authorized_graph_snapshot_sha256": _digest("attacker-graph-snapshot"),
            }
        )

    forged_proposal_principal = case.binding.action_proposed_event.model_copy(
        update={"principal_id": case.binding.action_authorized_event.principal_id}
    )
    with pytest.raises(ValidationError, match="lineage"):
        ScientificActionProtocolBinding.model_validate(
            {
                **case.binding.model_dump(mode="python"),
                "action_proposed_event": forged_proposal_principal,
            }
        )

    reused_authorizer = case.binding.action_authorized_event.model_copy(
        update={"principal_id": case.binding.action.proposed_by_principal_id}
    )
    with pytest.raises(ValidationError, match="proposer and action authorizer"):
        ScientificActionProtocolBinding.model_validate(
            {
                **case.binding.model_dump(mode="python"),
                "action_authorized_event": reused_authorizer,
            }
        )


def test_bridge_principals_keys_and_policies_are_externally_pinned_and_separate() -> None:
    case = _bridge_case()
    wrong_key = bytes(range(1, 33))
    wrong_pin = _pin(
        ScientificBridgeRole.EXECUTION_AUTHORIZER,
        wrong_key,
        case.execution_pin.principal_id,
        "scientific-execution-authority-policy",
    )
    with pytest.raises(ScientificBridgeVerificationError, match="external pin"):
        verify_scientific_execution_authorization(
            authorization=case.authorization,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            execution_authority_pin=wrong_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            observed_at=NOW + timedelta(minutes=6),
        )

    reused_principal_pin = ScientificBridgeAuthorityPin.model_validate(
        {
            **case.validator_pin.model_dump(mode="python"),
            "principal_id": case.execution_pin.principal_id,
        }
    )
    with pytest.raises(ScientificBridgeVerificationError, match="principals must be separate"):
        verify_scientific_execution_authorization(
            authorization=case.authorization,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=reused_principal_pin,
            admission_authority_pin=case.admission_pin,
            observed_at=NOW + timedelta(minutes=6),
        )


def test_authorization_signature_and_qualification_only_literals_fail_closed() -> None:
    case = _bridge_case()
    forged_signature = case.authorization.model_copy(update={"signature_ed25519_hex": "f" * 128})
    with pytest.raises(ScientificBridgeVerificationError, match="signature is invalid"):
        _verify_authorization(case, forged_signature)

    forged_grant_message = case.authorization.message.qualification_grant.message.model_copy(
        update={"scientific_admission_allowed": True}
    )
    forged_grant = case.authorization.message.qualification_grant.model_copy(
        update={"message": forged_grant_message}
    )
    forged_message = case.authorization.message.model_copy(
        update={"qualification_grant": forged_grant}
    )
    with pytest.raises(ScientificBridgeVerificationError, match="structural validation"):
        _verify_authorization(
            case, case.authorization.model_copy(update={"message": forged_message})
        )


def test_phase_one_rejects_retry_attempt_and_multiple_scientific_slots() -> None:
    case = _bridge_case()
    retry_attempt = InfrastructureAttempt(
        replicate_slot_id=case.qualification.bundle.intent.replicate_slot.replicate_slot_id,
        attempt_number=2,
        previous_attempt_id="iat_" + "1" * 32,
        prior_confirmed_failure_receipt_sha256=_digest("confirmed-prior-failure"),
        prior_failure_category="infrastructure",
    )
    retry_intent = case.qualification.bundle.intent.model_copy(
        update={"infrastructure_attempt": retry_attempt}
    )
    retry_bundle = case.qualification.bundle.model_copy(update={"intent": retry_intent})
    with pytest.raises((ValidationError, ScientificBridgeVerificationError), match="retry"):
        issue_scientific_execution_authorization(
            action_protocol_binding=case.binding,
            qualification_bundle=retry_bundle,
            qualification_grant=case.qualification.grant,
            validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
            observation_validation_policy_sha256=(
                case.authorization.message.observation_validation_policy_sha256
            ),
            admission_policy=case.authorization.message.admission_policy,
            scientific_observation_artifact_binding=(
                case.authorization.message.scientific_observation_artifact_binding
            ),
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
            authorized_at=NOW + timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=9),
            observation_admission_deadline=NOW + timedelta(hours=2),
        )

    multiple_slot = case.binding.replicate_slot.model_copy(update={"slot_count": 2})
    with pytest.raises(ValidationError, match="exact WorkOrder projection|exactly one"):
        ScientificActionProtocolBinding.model_validate(
            {
                **case.binding.model_dump(mode="python"),
                "replicate_slot": multiple_slot,
            }
        )

    short_window_message = case.authorization.message.model_copy(
        update={"observation_admission_deadline": case.qualification.bundle.intent.deadline}
    )
    with pytest.raises(ScientificBridgeVerificationError, match="structural"):
        verify_scientific_execution_authorization_historical(
            authorization=case.authorization.model_copy(update={"message": short_window_message}),
            qualification_authority=case.qualification_authority,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            observed_at=NOW + timedelta(minutes=6),
        )


def test_raw_run_closes_exact_terminal_and_artifact_contracts_without_claiming_custody() -> None:
    case = _bridge_case()
    raw_run = _raw_run(case)
    assert raw_run.accepted_terminal_submission.disposition == "process_succeeded"
    assert raw_run.source_qualification_only is True
    assert raw_run.source_scientific_admission_allowed is False
    assert raw_run.executor_reported_scientific_outcome_trusted is False
    assert "custody_verified" not in RawRunEnvelope.model_fields

    forged_terminal = raw_run.accepted_terminal_submission.model_copy(
        update={"artifact_manifest_sha256": _digest("other-manifest")}
    )
    with pytest.raises(ValidationError, match="exact terminal submission"):
        RawRunEnvelope.model_validate(
            {
                **raw_run.model_dump(mode="python"),
                "accepted_terminal_submission": forged_terminal,
            }
        )


@pytest.mark.parametrize(
    ("entry_updates", "expected_error"),
    [
        ({"expected_artifact_id": "art_" + "8" * 32}, "frozen expectation"),
        ({"media_type": "application/octet-stream"}, "frozen expectation"),
        ({"schema_sha256": _digest("wrong-observation-schema")}, "frozen expectation"),
        ({"bytes": 8_388_609}, "aggregate artifact quota"),
    ],
)
def test_raw_run_rejects_artifacts_outside_frozen_intent(
    entry_updates: dict[str, object],
    expected_error: str,
) -> None:
    case = _bridge_case()
    with pytest.raises(ValidationError, match=expected_error):
        _raw_run(case, artifact_entry_updates=entry_updates)


def test_raw_run_rejects_undeclared_output_and_missing_required_success() -> None:
    case = _bridge_case()
    with pytest.raises(ValidationError, match="undeclared output"):
        _raw_run(case, add_undeclared_output=True)
    with pytest.raises(ValidationError, match="disposition"):
        _raw_run(case, "process_succeeded", omit_artifacts=True)


def test_raw_run_rejects_retroactive_authorization_and_authority_time_inversions() -> None:
    case = _bridge_case()
    with pytest.raises(ValidationError, match="custody times"):
        _raw_run(
            case,
            runtime_ended_at_override=case.authorization.message.authorized_at
            - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="custody times"):
        _raw_run(
            case,
            terminal_submitted_at_override=NOW + timedelta(minutes=20, seconds=1),
        )


def test_raw_run_does_not_order_cross_clock_artifact_verifier_evidence() -> None:
    case = _bridge_case()
    raw_run = _raw_run(
        case,
        "process_failed",
        artifact_verified_at_override=NOW + timedelta(hours=1, minutes=20),
    )
    assert all(
        receipt.verified_at > raw_run.terminal_submission.submitted_at
        for receipt in raw_run.artifact_verified_receipts
    )
    receipt = _issue_validation_receipt(
        case,
        raw_run=raw_run,
        validation_campaign_sha256=None,
        validated_at=raw_run.assembled_at + timedelta(minutes=1),
    )
    assert receipt.message.disposition is BridgeValidationDisposition.BLOCKED_EXECUTION
    assert case.raw_run_custody.calls


def test_raw_run_closes_runtime_control_authority_and_scientific_independence() -> None:
    case = _bridge_case()
    with pytest.raises(ValidationError, match="changed authority"):
        _raw_run(
            case,
            accepted_terminal_principal_id="principal:other-runtime-control",
        )
    for kwargs in (
        {"runtime_control_principal_id": case.validator_pin.principal_id},
        {"runtime_control_key_id": case.validator_pin.key_id},
        {"runtime_control_policy_sha256": case.admission_pin.policy_sha256},
    ):
        with pytest.raises(ValidationError, match="reuses runtime-control"):
            _raw_run(case, **kwargs)


@pytest.mark.parametrize("terminal", ["process_failed", "timeout", "invalid_output"])
def test_terminal_failure_is_blocked_execution_never_scientific_negative(
    terminal: str,
) -> None:
    case = _bridge_case()
    receipt = _blocked_receipt(case, terminal)
    assert receipt.message.disposition is BridgeValidationDisposition.BLOCKED_EXECUTION
    assert receipt.message.outcome is None
    assert receipt.message.scientific_observation_sha256 is None
    assert receipt.message.validation_campaign_projection is None
    assert receipt.message.blocker_codes == (f"qualification_terminal:{terminal}",)
    assert case.validation_campaign_custody.calls == []
    assert case.qualification_custody.admission_calls[-1][0] == (
        case.qualification_admission_sha256
    )
    assert (
        case.qualification_custody.admission_calls[-1][3] > case.authorization.message.authorized_at
    )
    assert len(case.action_authority.calls) >= 2

    forged_message = receipt.message.model_copy(
        update={"outcome": ScientificObservationOutcome.NEGATIVE}
    )
    with pytest.raises(ScientificBridgeVerificationError, match="structural validation"):
        verify_observation_validation_receipt(
            receipt=receipt.model_copy(update={"message": forged_message}),
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            observed_at=receipt.message.validated_at,
        )


def test_blocked_validation_still_respects_observation_deadline() -> None:
    case = _bridge_case()
    raw_run = _raw_run(case, "process_failed")
    with pytest.raises(ScientificBridgeVerificationError, match="outside.*window"):
        _issue_validation_receipt(
            case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=case.authorization.message.observation_admission_deadline
            + timedelta(seconds=1),
        )


def test_observation_admission_deadline_is_half_open_at_every_bridge_stage() -> None:
    case = _bridge_case()
    deadline = case.authorization.message.observation_admission_deadline
    raw_run = _raw_run(case, "process_failed")

    with pytest.raises(ValidationError, match="custody times"):
        RawRunEnvelope.model_validate(
            {
                **raw_run.model_dump(mode="python"),
                "assembled_at": deadline,
            }
        )

    with pytest.raises(ScientificBridgeVerificationError, match="outside.*window"):
        _issue_validation_receipt(
            case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=deadline,
        )

    receipt = _blocked_receipt(case, "process_failed")
    with pytest.raises(ScientificBridgeVerificationError, match="outside.*window"):
        _issue_admission_decision(
            case,
            receipt=receipt,
            disposition=ObservationAdmissionDisposition.REJECTED,
            reason_codes=("validation_blocked",),
            decided_at=deadline,
        )


def test_qualification_admission_hash_is_verified_by_engineering_custody_port() -> None:
    case = _bridge_case()
    raw_run = _raw_run(
        case,
        "process_failed",
        qualification_admission_sha256=_digest("fabricated-qualification-admission"),
    )
    with pytest.raises(ScientificBridgeVerificationError, match="admission hash"):
        _issue_validation_receipt(
            case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=raw_run.assembled_at + timedelta(minutes=1),
        )
    assert case.qualification_custody.admission_calls[-1][0] == (
        raw_run.qualification_admission_sha256
    )


def test_validation_signature_uses_external_validator_pin() -> None:
    case = _bridge_case()
    receipt = _blocked_receipt(case, "process_failed")
    verify_observation_validation_receipt(
        receipt=receipt,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        observed_at=receipt.message.validated_at,
    )
    forged = receipt.model_copy(update={"signature_ed25519_hex": "e" * 128})
    with pytest.raises(ScientificBridgeVerificationError, match="signature is invalid"):
        verify_observation_validation_receipt(
            receipt=forged,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            observed_at=receipt.message.validated_at,
        )


def test_database_challenge_is_external_separate_and_exactly_scoped() -> None:
    case = _bridge_case()
    raw_run = _raw_run(case, "process_failed")
    issued_at = raw_run.assembled_at + timedelta(minutes=1)
    challenge = issue_validation_issuance_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=None,
        nonce_sha256=_digest("validation-db-challenge-exact-scope"),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=10),
    )
    assert (
        verify_validation_issuance_challenge(
            challenge=challenge,
            raw_run=raw_run,
            expected_validation_campaign_sha256=None,
            database_authority_pin=case.database_pin,
            observed_at=issued_at,
        )
        == challenge
    )

    rebound_message = challenge.message.model_copy(
        update={"raw_run_sha256": _digest("rebound-raw-run-in-challenge")}
    )
    with pytest.raises(ScientificBridgeVerificationError, match="rebound"):
        issue_observation_validation_receipt(
            raw_run=raw_run,
            validation_campaign_sha256=None,
            issuance_challenge=challenge.model_copy(update={"message": rebound_message}),
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            private_key=VALIDATOR_PRIVATE_KEY,
        )

    reused_principal_pin = ObservationDatabaseAuthorityPin.model_validate(
        {
            **case.database_pin.model_dump(mode="python"),
            "principal_id": case.validator_pin.principal_id,
        }
    )
    with pytest.raises(ScientificBridgeVerificationError, match="must be independent"):
        issue_validation_issuance_challenge(
            raw_run=raw_run,
            validation_campaign_sha256=None,
            nonce_sha256=_digest("validation-db-challenge-reused-principal"),
            database_authority_pin=reused_principal_pin,
            private_key=DATABASE_PRIVATE_KEY,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=10),
        )


def test_validation_proposal_requires_exact_database_commit_before_admission() -> None:
    case = _bridge_case()
    receipt = _blocked_receipt(case, "process_failed")
    committed = _commit_validation(case, receipt)
    assert receipt.authority_state == "proposal"
    assert receipt.scientific_authority_conferred is False
    assert committed.message.receipt == receipt
    assert committed.message.persisted is True
    assert committed.message.historical_validation_authority is True
    assert (
        verify_committed_observation_validation_receipt(
            committed_receipt=committed,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            observed_at=committed.message.committed_at,
        )
        == committed
    )
    forged = committed.model_copy(update={"signature_ed25519_hex": "d" * 128})
    with pytest.raises(ScientificBridgeVerificationError, match="signature is invalid"):
        verify_committed_observation_validation_receipt(
            committed_receipt=forged,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            observed_at=committed.message.committed_at,
        )


def test_verified_campaign_projection_can_produce_signed_negative_admission() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case)
    projection = receipt.message.validation_campaign_projection
    assert projection is not None
    assert receipt.message.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
    assert receipt.message.outcome is ScientificObservationOutcome.NEGATIVE
    assert receipt.message.scientific_observation_sha256 is not None
    assert len(case.validation_campaign_custody.calls) >= 2
    assert case.validation_campaign_custody.calls[-1][2:] == (
        case.authorization.message.validator_manifest_sha256,
        case.authorization.message.observation_validation_policy_sha256,
        receipt.message.validated_at,
    )

    action_calls_before_decision = len(case.action_authority.calls)
    decision, committed_validation = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    assert decision.message.committed_validation_receipt == committed_validation
    assert decision.message.disposition is ObservationAdmissionDisposition.ADMITTED
    assert (
        decision.message.admitted_observation_sha256
        == receipt.message.scientific_observation_sha256
    )
    assert len(case.action_authority.calls) > action_calls_before_decision


def test_only_atomic_database_commit_confers_scientific_admission_authority() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(case, outcome_bin_id="outcome.negative")
    decision, committed_validation = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    committed_admission = _commit_admission(case, decision)

    assert committed_validation.message.persisted is True
    assert decision.authority_state == "proposal"
    assert decision.scientific_authority_conferred is False
    assert decision.message.persistence_committed is False
    assert committed_admission.message.scientific_slot_was_empty is True
    assert committed_admission.message.transaction_was_atomic is True
    assert committed_admission.message.persistence_committed is True
    assert committed_admission.message.scientific_authority_conferred is True
    assert (
        committed_admission.message.exact_registered_validation_receipt_sha256
        == receipt.receipt_sha256
    )
    assert (
        verify_committed_observation_admission(
            committed_admission=committed_admission,
            qualification_authority=case.qualification_authority,
            action_authority=case.action_authority,
            qualification_custody=case.qualification_custody,
            raw_run_custody=case.raw_run_custody,
            validation_campaign_custody=case.validation_campaign_custody,
            execution_authority_pin=case.execution_pin,
            validator_authority_pin=case.validator_pin,
            admission_authority_pin=case.admission_pin,
            database_authority_pin=case.database_pin,
            observed_at=committed_admission.message.committed_at,
        )
        == committed_admission
    )


def test_scientifically_rejected_campaign_never_projects_an_observation_outcome() -> None:
    case = _bridge_case()
    receipt = _validated_receipt(
        case,
        disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
        blocker_codes=("protocol:material_deviation",),
    )
    assert receipt.message.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC
    assert receipt.message.outcome is None
    assert receipt.message.scientific_observation_sha256 is None
    assert receipt.message.validation_campaign_projection is not None
    assert receipt.message.validation_campaign_projection.validation_batch_sha256 is not None


def test_full_verifiers_call_ports_and_fail_closed_on_adapter_errors() -> None:
    case = _bridge_case()
    case.action_authority.fail = True
    with pytest.raises(ScientificBridgeVerificationError, match="online.*failed closed"):
        _verify_authorization(case, case.authorization)
    case.action_authority.fail = False
    case.qualification_custody.fail_online = True
    with pytest.raises(ScientificBridgeVerificationError, match="online.*failed closed"):
        _verify_authorization(case, case.authorization)

    action_case = _bridge_case()
    action_raw_run = _raw_run(action_case, "process_failed")
    action_case.action_authority.fail = True
    with pytest.raises(ScientificBridgeVerificationError, match="receipt failed closed"):
        _issue_validation_receipt(
            action_case,
            raw_run=action_raw_run,
            validation_campaign_sha256=None,
            validated_at=action_raw_run.assembled_at + timedelta(minutes=1),
        )

    qualification_case = _bridge_case()
    qualification_raw_run = _raw_run(qualification_case, "process_failed")
    qualification_case.qualification_custody.fail_admission = True
    with pytest.raises(ScientificBridgeVerificationError, match="receipt failed closed"):
        _issue_validation_receipt(
            qualification_case,
            raw_run=qualification_raw_run,
            validation_campaign_sha256=None,
            validated_at=qualification_raw_run.assembled_at + timedelta(minutes=1),
        )

    raw_case = _bridge_case()
    raw_case.raw_run_custody.fail = True
    raw_run = _raw_run(raw_case, "process_failed")
    with pytest.raises(ScientificBridgeVerificationError, match="receipt failed closed"):
        _issue_validation_receipt(
            raw_case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=raw_run.assembled_at + timedelta(minutes=1),
        )

    raw_case.raw_run_custody.fail = False
    raw_case.raw_run_custody.retroactive_lineage = True
    with pytest.raises(ScientificBridgeVerificationError, match="receipt failed closed"):
        _issue_validation_receipt(
            raw_case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=raw_run.assembled_at + timedelta(minutes=1),
        )

    campaign_case = _bridge_case()
    campaign_case.validation_campaign_custody.fail = True
    with pytest.raises(ScientificBridgeVerificationError, match="receipt failed closed"):
        _validated_receipt(campaign_case)


def test_full_verifiers_reject_rebound_port_outputs() -> None:
    case = _bridge_case()
    case.action_authority.returned_binding_sha256 = _digest("rebound-binding")
    with pytest.raises(ScientificBridgeVerificationError, match="another action-protocol"):
        _verify_authorization(case, case.authorization)

    qualification_case = _bridge_case()
    qualification_case.qualification_custody.rebound_admission = True
    qualification_raw_run = _raw_run(qualification_case, "process_failed")
    with pytest.raises(ScientificBridgeVerificationError, match="rebound verification"):
        _issue_validation_receipt(
            qualification_case,
            raw_run=qualification_raw_run,
            validation_campaign_sha256=None,
            validated_at=qualification_raw_run.assembled_at + timedelta(minutes=1),
        )

    raw_case = _bridge_case()
    raw_case.raw_run_custody.returned_raw_run_sha256 = _digest("rebound-raw-run")
    raw_run = _raw_run(raw_case, "process_failed")
    with pytest.raises(ScientificBridgeVerificationError, match="rebound lineage"):
        _issue_validation_receipt(
            raw_case,
            raw_run=raw_run,
            validation_campaign_sha256=None,
            validated_at=raw_run.assembled_at + timedelta(minutes=1),
        )

    projection_case = _bridge_case()
    receipt = _validated_receipt(projection_case)
    assert projection_case.validation_campaign_custody.projection is not None
    projection_case.validation_campaign_custody.projection = (
        projection_case.validation_campaign_custody.projection.model_copy(
            update={"validation_batch_sha256": _digest("rebound-validation-batch")}
        )
    )
    with pytest.raises(ScientificBridgeVerificationError, match="projection differs"):
        verify_observation_validation_receipt(
            receipt=receipt,
            qualification_authority=projection_case.qualification_authority,
            action_authority=projection_case.action_authority,
            qualification_custody=projection_case.qualification_custody,
            raw_run_custody=projection_case.raw_run_custody,
            validation_campaign_custody=(projection_case.validation_campaign_custody),
            execution_authority_pin=projection_case.execution_pin,
            validator_authority_pin=projection_case.validator_pin,
            admission_authority_pin=projection_case.admission_pin,
            database_authority_pin=projection_case.database_pin,
            observed_at=receipt.message.validated_at,
        )


def test_admission_rejects_blocked_execution_and_exposes_empty_slot_precondition() -> None:
    case = _bridge_case()
    receipt = _blocked_receipt(case, "process_failed")
    decision, _ = _issue_admission_decision(
        case,
        receipt=receipt,
        disposition=ObservationAdmissionDisposition.REJECTED,
        reason_codes=("validation_blocked",),
    )
    verify_observation_admission_decision(
        decision=decision,
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        validation_campaign_custody=case.validation_campaign_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
        observed_at=decision.message.decided_at,
    )
    assert decision.message.admitted_observation_sha256 is None
    assert decision.message.slot_precondition == "must_be_empty"
    assert decision.message.maximum_admissions_per_scientific_slot == 1
    assert decision.message.persistence_committed is False
    with pytest.raises(ValidationError, match="only an admitted proposal"):
        _commit_admission(case, decision)

    with pytest.raises((ValidationError, ValueError), match="validated observation"):
        _issue_admission_decision(
            case,
            receipt=receipt,
            disposition=ObservationAdmissionDisposition.ADMITTED,
            reason_codes=(),
        )


def test_adapter_ports_do_not_themselves_claim_database_or_cas_authority() -> None:
    ports = (
        ResearchActionAuthorityVerificationPort,
        EngineeringQualificationCustodyVerificationPort,
        RawRunCustodyVerificationPort,
        ObservationValidationCampaignVerificationPort,
        ObservationAdmissionCommitPort,
    )
    assert all(item.__doc__ and item.__doc__.startswith("Port for") for item in ports)
    assert all("Future adapter" not in item.__doc__ for item in ports)
