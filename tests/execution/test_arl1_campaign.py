from __future__ import annotations

from types import SimpleNamespace

import pytest

from aletheia.arl1_campaign import (
    ARL1IndependentValidationCoordinator,
    ARL1ProtocolCampaignError,
    ARL1PrimaryAdmissionCoordinator,
    ARL1ProtocolCampaignRequestV1,
    ARL1ProtocolCampaignService,
)
from aletheia.arl1_verifier import LocalARL1EvidenceArchive
from aletheia.observations.coordinator import AtomicObservationAdmissionReceipt
from aletheia.observations.service import (
    AdmissionChallengeRegistrationReceipt,
    ValidationChallengeRegistrationReceipt,
    ValidationCommitReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_store.store import ResearchCommandReceipt

from .test_arl1_qualification import arl1_case as arl1_case


class _Registrar:
    def __init__(self, campaign) -> None:
        self.campaign = campaign
        self.calls = 0

    def register_and_reserve_campaign(self, authorizations):
        self.calls += 1
        assert authorizations == self.campaign.campaign_registration.authorizations
        return self.campaign.campaign_registration


class _RawRuns:
    def __init__(self, campaign) -> None:
        self.by_slot = {
            item.scientific_slot_id: item.raw_run for item in campaign.replicate_executions
        }

    def load_raw_run(self, *, quest_id, action_sha256, scientific_slot_id):
        del quest_id, action_sha256
        return self.by_slot[scientific_slot_id]


class _Custody:
    def __init__(self, campaign) -> None:
        self.by_run = {
            item.raw_run.raw_run_sha256: item.raw_run_custody
            for item in campaign.replicate_executions
        }

    def verify_raw_run_custody(self, *, raw_run, observed_at):
        projection = self.by_run[raw_run.raw_run_sha256]
        assert observed_at == projection.verified_at
        return projection


class _Validation:
    def __init__(self, campaign) -> None:
        self.by_run = {
            item.raw_run.raw_run_sha256: item.committed_validation
            for item in campaign.replicate_executions
        }

    def commit_or_load_validation(self, *, raw_run):
        return self.by_run[raw_run.raw_run_sha256]


class _Admission:
    def __init__(self, campaign) -> None:
        event = campaign.incorporation_event
        binding = campaign.replicate_executions[0].authorization.message.action_protocol_binding
        committed = campaign.committed_admission
        self.receipt = AtomicObservationAdmissionReceipt(
            committed_admission=committed,
            incorporation_payload=event.payload,
            kernel_receipt=ResearchCommandReceipt(
                command_id="rcm_" + "1" * 32,
                quest_id=event.quest_id,
                scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
                idempotency_key=(
                    f"observation-admission:{committed.message.decision.decision_sha256}"
                ),
                source_event_key=f"scientific-slot:{campaign.scientific_slot_id}",
                command_sha256=event.command_sha256,
                expected_stream_version=event.sequence - 1,
                expected_tail_event_sha256=event.parent_event_sha256,
                result_stream_version=event.sequence,
                result_event_sha256=event.event_sha256,
                result_event_id=event.event_id,
                result_snapshot_sha256="2" * 64,
                outbox_id=f"rko_{event.event_sha256[:32]}",
                principal_id=event.principal_id,
                authorization_trust_root_sha256="3" * 64,
                authorization_policy_sha256="4" * 64,
                authorization_receipt_sha256=event.authorization_receipt_sha256,
                committed_at=event.committed_at,
                created=True,
            ),
            created=True,
        )

    def commit_or_load_admission(self, *, committed_validation):
        assert (
            self.receipt.committed_admission.message.decision.message.committed_validation_receipt
            == committed_validation
        )
        return self.receipt


class _Kernel:
    def __init__(self, campaign) -> None:
        self.campaign = campaign

    def audit(self, quest_id, *, expected_scope_binding=None):
        event = self.campaign.incorporation_event
        assert quest_id == event.quest_id
        return SimpleNamespace(events=(event,))


def _binding(role, *, principal_id, key_id, policy_sha256, service_manifest_sha256):
    return ControllerStepAuthorityBinding(
        role=role,
        principal_id=principal_id,
        key_id=key_id,
        policy_sha256=policy_sha256,
        service_manifest_sha256=service_manifest_sha256,
        externally_deployed=True,
    )


class _DatabaseRPC:
    def __init__(self, campaign, binding) -> None:
        self.authority_binding = binding
        self.validations = {
            item.raw_run.raw_run_sha256: item.committed_validation
            for item in campaign.replicate_executions
        }
        self.committed_by_slot = {}
        self.challenges_issued = 0
        self.admission_challenges_issued = 0
        self.admission_challenge = (
            campaign.committed_admission.message.decision.message.issuance_challenge
        )

    def issue_validation_challenge(self, *, raw_run, validation_campaign_sha256):
        self.challenges_issued += 1
        committed = self.validations[raw_run.raw_run_sha256]
        challenge = committed.message.receipt.message.issuance_challenge
        assert challenge.message.validation_campaign_sha256 == validation_campaign_sha256
        return ValidationChallengeRegistrationReceipt(
            challenge=challenge,
            recorded_at=challenge.message.issued_at,
        )

    def commit_validation(self, receipt):
        committed = self.validations[receipt.message.raw_run.raw_run_sha256]
        assert committed.message.receipt == receipt
        message = committed.message.receipt.message.raw_run.scientific_authorization.message
        self.committed_by_slot[message.scientific_slot_id] = committed
        return ValidationCommitReceipt(committed_validation=committed)

    def load_committed_validation(self, *, quest_id, action_sha256, scientific_slot_id):
        committed = self.committed_by_slot.get(scientific_slot_id)
        if committed is None:
            return None
        message = committed.message.receipt.message.raw_run.scientific_authorization.message
        binding = message.action_protocol_binding
        assert binding.action.quest_id == quest_id
        assert binding.action.object_sha256 == action_sha256
        return committed

    def issue_admission_challenge(self, committed_validation):
        self.admission_challenges_issued += 1
        assert (
            self.admission_challenge.message.committed_validation_receipt_sha256
            == committed_validation.committed_receipt_sha256
        )
        return AdmissionChallengeRegistrationReceipt(
            challenge=self.admission_challenge,
            recorded_at=self.admission_challenge.message.issued_at,
        )


class _ValidatorRPC:
    def __init__(self, campaign, binding) -> None:
        self.authority_binding = binding
        self.receipts = {
            item.raw_run.raw_run_sha256: item.committed_validation.message.receipt
            for item in campaign.replicate_executions
        }

    def prepare_validation_campaign(self, *, raw_run):
        projection = self.receipts[raw_run.raw_run_sha256].message.validation_campaign_projection
        assert projection is not None
        return projection.campaign_sha256

    def issue_validation_receipt(
        self,
        *,
        raw_run,
        validation_campaign_sha256,
        issuance_challenge,
    ):
        receipt = self.receipts[raw_run.raw_run_sha256]
        assert receipt.message.issuance_challenge == issuance_challenge
        assert (
            receipt.message.validation_campaign_projection.campaign_sha256
            == validation_campaign_sha256
        )
        return receipt


class _AdmissionRPC:
    def __init__(self, campaign, binding) -> None:
        self.authority_binding = binding
        self.decision = campaign.committed_admission.message.decision
        self.decisions_issued = 0

    def issue_admission_decision(self, *, committed_validation, issuance_challenge):
        self.decisions_issued += 1
        assert self.decision.message.committed_validation_receipt == committed_validation
        assert self.decision.message.issuance_challenge == issuance_challenge
        return self.decision


class _AtomicRPC:
    def __init__(self, campaign, *, database, admission, kernel) -> None:
        self.database_authority_binding = database
        self.admission_authority_binding = admission
        self.kernel_authority_binding = kernel
        self.receipt = _Admission(campaign).receipt
        self.committed = False
        self.lose_response = False
        self.commits = 0

    def load_committed_admission(self, *, quest_id, action_sha256, scientific_slot_id):
        if not self.committed:
            return None
        return self.receipt.model_copy(
            update={
                "created": False,
                "kernel_receipt": self.receipt.kernel_receipt.model_copy(update={"created": False}),
            }
        )

    def commit_and_incorporate(self, decision):
        assert self.receipt.committed_admission.message.decision == decision
        self.committed = True
        self.commits += 1
        if self.lose_response:
            self.lose_response = False
            raise ConnectionError("committed response was lost")
        return self.receipt


def test_given_protocol_campaign_runs_every_slot_before_primary_admission_and_replays_exactly(
    arl1_case,
    tmp_path,
) -> None:
    bundle, _private_key, _source_verifier = arl1_case
    source_campaign = bundle.protocol_campaigns[0]
    first = source_campaign.replicate_executions[0].authorization.message
    request = ARL1ProtocolCampaignRequestV1(
        domain_scope=source_campaign.domain_scope,
        modality_scope=source_campaign.modality_scope,
        compilation_request=source_campaign.compilation_request,
        compilation_result=source_campaign.compilation_result,
        work_order_node_id=source_campaign.work_order_node_id,
        authorizations=source_campaign.campaign_registration.authorizations,
        primary_scientific_slot_id=source_campaign.scientific_slot_id,
        requested_at=first.authorized_at,
    )
    registrar = _Registrar(source_campaign)
    service = ARL1ProtocolCampaignService(
        registrar=registrar,
        raw_run_source=_RawRuns(source_campaign),
        raw_run_custody=_Custody(source_campaign),
        validation=_Validation(source_campaign),
        admission=_Admission(source_campaign),
        kernel_store=_Kernel(source_campaign),
        archive=LocalARL1EvidenceArchive(tmp_path / "campaign-archive"),
    )

    assert service.register(request) == source_campaign.campaign_registration
    first_receipt = service.execute(request)
    replayed = service.execute(request)

    assert replayed == first_receipt
    assert registrar.calls == 3
    assert len(first_receipt.campaign.replicate_executions) == 2
    assert all(
        item.committed_validation.message.committed_at <= first_receipt.campaign.admitted_at
        for item in first_receipt.campaign.replicate_executions
    )
    assert first_receipt.campaign.scientific_slot_id == request.primary_scientific_slot_id
    assert first_receipt.campaign.observation_incorporated is True
    assert first_receipt.campaign.report.autonomous_research_design_claimed is False


@pytest.mark.parametrize("failure_point", ("none", "lost_response", "archive_failure"))
def test_given_protocol_campaign_uses_keyless_independent_rpc_authority_chain(
    arl1_case,
    tmp_path,
    monkeypatch,
    failure_point,
) -> None:
    bundle, _private_key, _source_verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    primary = next(
        item
        for item in campaign.replicate_executions
        if item.scientific_slot_id == campaign.scientific_slot_id
    )
    authorization = primary.authorization.message
    committed = primary.committed_validation.message
    atomic_receipt = _Admission(campaign).receipt
    database_binding = _binding(
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        principal_id=committed.committed_by_principal_id,
        key_id=committed.commit_key_id,
        policy_sha256=committed.database_authority_policy_sha256,
        service_manifest_sha256="5" * 64,
    )
    validator_binding = _binding(
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        principal_id=authorization.validator_principal_id,
        key_id=authorization.validator_key_id,
        policy_sha256=authorization.validator_authority_policy_sha256,
        service_manifest_sha256=authorization.validator_manifest_sha256,
    )
    admission_binding = _binding(
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        principal_id=authorization.admission_principal_id,
        key_id=authorization.admission_key_id,
        policy_sha256=authorization.admission_authority_policy_sha256,
        service_manifest_sha256="6" * 64,
    )
    kernel_binding = _binding(
        ControllerStepAuthorityRole.KERNEL_COMMAND,
        principal_id=atomic_receipt.kernel_receipt.principal_id,
        key_id="principal.kernel.key",
        policy_sha256=atomic_receipt.kernel_receipt.authorization_policy_sha256,
        service_manifest_sha256="7" * 64,
    )
    database = _DatabaseRPC(campaign, database_binding)
    validator = _ValidatorRPC(campaign, validator_binding)
    admission = _AdmissionRPC(campaign, admission_binding)
    atomic = _AtomicRPC(
        campaign,
        database=database_binding,
        admission=admission_binding,
        kernel=kernel_binding,
    )
    first = campaign.replicate_executions[0].authorization.message
    request = ARL1ProtocolCampaignRequestV1(
        domain_scope=campaign.domain_scope,
        modality_scope=campaign.modality_scope,
        compilation_request=campaign.compilation_request,
        compilation_result=campaign.compilation_result,
        work_order_node_id=campaign.work_order_node_id,
        authorizations=campaign.campaign_registration.authorizations,
        primary_scientific_slot_id=campaign.scientific_slot_id,
        requested_at=first.authorized_at,
    )
    service = ARL1ProtocolCampaignService(
        registrar=_Registrar(campaign),
        raw_run_source=_RawRuns(campaign),
        raw_run_custody=_Custody(campaign),
        validation=ARL1IndependentValidationCoordinator(
            database=database,
            validator=validator,
        ),
        admission=ARL1PrimaryAdmissionCoordinator(
            database=database,
            admission=admission,
            coordinator=atomic,
        ),
        kernel_store=_Kernel(campaign),
        archive=LocalARL1EvidenceArchive(tmp_path / "rpc-campaign-archive"),
    )

    if failure_point == "lost_response":
        atomic.lose_response = True
        with pytest.raises(ARL1ProtocolCampaignError, match="admission failed closed"):
            service.execute(request)
    elif failure_point == "archive_failure":
        with monkeypatch.context() as patch:
            def fail_publication(*args, **kwargs):
                raise OSError("archive publication interrupted")
            patch.setattr("aletheia.arl1_campaign.retain_protocol_campaign_archive", fail_publication)
            with pytest.raises(ARL1ProtocolCampaignError, match="campaign failed closed"):
                service.execute(request)

    receipt = service.execute(request)
    # Reconstruct the coordinator from durable ports, as a restarted driver does.
    service._admission = ARL1PrimaryAdmissionCoordinator(
        database=database, admission=admission, coordinator=atomic,
    )
    assert service.execute(request) == receipt
    assert database.admission_challenges_issued == 1
    assert admission.decisions_issued == 1
    assert atomic.commits == 1

    assert receipt.campaign.campaign_registration.authorizations == request.authorizations
    assert receipt.campaign.committed_admission == campaign.committed_admission
    assert receipt.campaign.incorporation_event == campaign.incorporation_event


def _validation_coordinator(arl1_case, *, database=None):
    bundle, _private_key, _source_verifier = arl1_case
    campaign = bundle.protocol_campaigns[0]
    primary = next(
        item
        for item in campaign.replicate_executions
        if item.scientific_slot_id == campaign.scientific_slot_id
    )
    authorization = primary.authorization.message
    committed = primary.committed_validation.message
    database_binding = _binding(
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        principal_id=committed.committed_by_principal_id,
        key_id=committed.commit_key_id,
        policy_sha256=committed.database_authority_policy_sha256,
        service_manifest_sha256="5" * 64,
    )
    validator_binding = _binding(
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        principal_id=authorization.validator_principal_id,
        key_id=authorization.validator_key_id,
        policy_sha256=authorization.validator_authority_policy_sha256,
        service_manifest_sha256=authorization.validator_manifest_sha256,
    )
    if database is None:
        database = _DatabaseRPC(campaign, database_binding)
    return (
        campaign,
        database,
        ARL1IndependentValidationCoordinator(
            database=database,
            validator=_ValidatorRPC(campaign, validator_binding),
        ),
    )


def test_validation_coordinator_returns_committed_slot_without_issuing_a_new_challenge(
    arl1_case,
) -> None:
    campaign, database, coordinator = _validation_coordinator(arl1_case)
    replicate = campaign.replicate_executions[0]
    message = replicate.authorization.message

    # Simulate the generation-n failure shape: the slot already holds its
    # committed receipt, and the driver's poll loop re-passes it after the
    # issuance-challenge TTL has lapsed.  The committed fact must be returned
    # without any fresh challenge that could never byte-equal it.
    database.committed_by_slot[message.scientific_slot_id] = replicate.committed_validation

    loaded = coordinator.commit_or_load_validation(raw_run=replicate.raw_run)

    assert loaded == replicate.committed_validation
    assert database.challenges_issued == 0


def test_validation_coordinator_rejects_a_loaded_receipt_rebound_to_another_run(
    arl1_case,
) -> None:
    campaign, database, _coordinator = _validation_coordinator(arl1_case)
    first, second = campaign.replicate_executions[0], campaign.replicate_executions[1]
    first_slot = first.authorization.message.scientific_slot_id

    # A corrupt or rebound slot whose stored receipt belongs to another
    # replicate's raw run must fail closed instead of loading as fact.
    database.committed_by_slot[first_slot] = second.committed_validation

    _campaign, _database, coordinator = _validation_coordinator(arl1_case, database=database)
    with pytest.raises(ARL1ProtocolCampaignError, match="rebound"):
        coordinator.commit_or_load_validation(raw_run=first.raw_run)
