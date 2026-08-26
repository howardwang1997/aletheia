from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aletheia.observations.persistence import ResearchContinuationReceiptRecord
from aletheia.observations.scientific_bridge import ObservationAdmissionDisposition
from aletheia.observations.store import (
    ObservationAdmissionWrite,
    ObservationIssuanceChallengeWrite,
    ObservationValidationReceiptWrite,
    ProtocolCompilationWrite,
    ScientificExecutionAuthorizationWrite,
    get_continuation_receipt_by_slot,
    record_observation_admission,
    record_observation_issuance_challenge,
    record_observation_validation_receipt,
    register_protocol_compilation,
    register_scientific_execution_authorization,
)
from aletheia.research_controller.continuation import (
    OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
    ContinuationDisposition,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    PredictionFit,
    project_admitted_scientific_observation,
)
from aletheia.research_controller.continuation_step import (
    ContinuationAssessmentPolicyPin,
    ContinuationAssessmentStepAdapter,
    ContinuationAssessmentStepError,
    ContinuationAssessmentUnavailable,
    DurableContinuationAssessmentService,
    PreparedContinuationAssessment,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.service import ControllerStepDisposition
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    ResearchStateGraph,
)
from aletheia.research_kernel.schemas import (
    EventType,
    ObservationIncorporatedPayload,
    ResearchEvent,
)
from aletheia.research_store.store import ResearchReplayAudit

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "observations",
    _TESTS / "protocols",
    _TESTS / "research_controller",
):
    sys.path.insert(0, str(_fixture_dir))

from persistence_test_support import sqlite_observation_engine  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    _bridge_case,
    _commit_admission,
    _issue_admission_decision,
    _validated_receipt,
)
from test_vertical_cut import (  # noqa: E402
    _f9_enriched_grouped_fixture,
    runtime_fixture_support,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _source(monkeypatch: pytest.MonkeyPatch):
    enriched = _f9_enriched_grouped_fixture()
    original = runtime_fixture_support.fixture_by_name

    def fixture_by_name(name: str):
        return enriched if name == "grouped_regression" else original(name)

    monkeypatch.setattr(runtime_fixture_support, "fixture_by_name", fixture_by_name)
    case = _bridge_case()
    binding = case.binding
    validation = _validated_receipt(case, outcome_bin_id="outcome.negative")
    decision, committed_validation = _issue_admission_decision(
        case,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    committed_admission = _commit_admission(case, decision)
    world_model = binding.compilation_request.protocol.world_model
    assert world_model is not None
    payload = ObservationIncorporatedPayload(
        branch_id=binding.compilation_request.protocol.graph_scope.branch_id,
        action_id=binding.action.action_id,
        scientific_slot_id=decision.message.scientific_slot_id,
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        scientific_observation_sha256=decision.message.admitted_observation_sha256,
        outcome=validation.message.outcome.value,
        source_world_model_sha256=world_model.world_model_sha256,
    )
    event = ResearchEvent(
        quest_id=binding.action.quest_id,
        sequence=binding.action_authorized_event.sequence + 1,
        parent_event_sha256=binding.action_authorized_event.event_sha256,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=payload,
        command_sha256=_sha("continuation-incorporation-command"),
        principal_id="kernel:continuation-observation-authority",
        authorization_receipt_sha256=_sha("continuation-incorporation-authorization"),
        committed_at=binding.action_authorized_event.committed_at + timedelta(seconds=1),
    )
    action_state = ActionSnapshot(
        action_ref=binding.action.object_ref,
        branch_id=payload.branch_id,
        kind=binding.action.kind,
        lifecycle=ActionLifecycle.APPLIED,
        proposed_event_sha256=binding.action_proposed_event.event_sha256,
        decided_event_sha256=event.event_sha256,
        observation_evidence_ref=payload.evidence_ref,
    )
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=event.sequence,
        tail_event_sha256=event.event_sha256,
        event_ids=(
            binding.action_proposed_event.event_id,
            binding.action_authorized_event.event_id,
            event.event_id,
        ),
        event_sha256s=(
            binding.action_proposed_event.event_sha256,
            binding.action_authorized_event.event_sha256,
            event.event_sha256,
        ),
        charter_ref=binding.action.charter_ref,
        charter_history=(binding.action.charter_ref,),
        actions=(action_state,),
        evidence_refs=(payload.evidence_ref,),
    )
    audit = ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        events=(
            binding.action_proposed_event,
            binding.action_authorized_event,
            event,
        ),
        state=state,
        verified_snapshot_sha256s=(_sha("snapshot-1"), _sha("snapshot-2"), state.snapshot_sha256),
    )
    compilation = ProtocolCompilationWrite.from_contract(
        quest_id=audit.quest_id,
        action_sha256=binding.action.object_sha256,
        request=binding.compilation_request,
        result=binding.compilation_result,
        registered_at=binding.bound_at,
    )
    authorization = ScientificExecutionAuthorizationWrite.from_contract(
        case.authorization,
        registered_at=case.authorization.message.authorized_at,
    )
    validation_write = ObservationValidationReceiptWrite.from_contract(
        committed_validation,
        quest_id=audit.quest_id,
    )
    admission_write = ObservationAdmissionWrite.from_contract(
        committed_admission,
        quest_id=audit.quest_id,
        incorporated_event_sequence=event.sequence,
        incorporated_event_sha256=event.event_sha256,
        incorporated_event_type=EventType.OBSERVATION_INCORPORATED.value,
    )
    return SimpleNamespace(
        case=case,
        audit=audit,
        event=event,
        world_model=world_model,
        committed_validation=committed_validation,
        compilation=compilation,
        authorization=authorization,
        validation_write=validation_write,
        admission_write=admission_write,
        validation_challenge=validation.message.issuance_challenge,
        admission_challenge=decision.message.issuance_challenge,
    )


class _Kernel:
    def __init__(self, audit) -> None:
        self.audit = audit

    def audit_in_session(self, _session, quest_id):
        assert quest_id == self.audit.quest_id
        return self.audit


class _DriftingKernel(_Kernel):
    def __init__(self, audit) -> None:
        super().__init__(audit)
        self.calls = 0

    def audit_in_session(self, _session, quest_id):
        self.calls += 1
        if self.calls == 1:
            return super().audit_in_session(_session, quest_id)
        drifted_state = self.audit.state.model_copy(
            update={"stream_version": self.audit.state.stream_version + 1}
        )
        return self.audit.model_copy(update={"state": drifted_state})


class _Archive:
    def __init__(self, source) -> None:
        self.source = source

    def load_object(self, ref):
        action = self.source.case.binding.action
        assert ref == action.object_ref
        return SimpleNamespace(payload=action)


class _Provider:
    def __init__(self, source, policy, *, tamper: str | None = None, partial: bool = False) -> None:
        self.source = source
        self.policy = policy
        self.tamper = tamper
        self.partial = partial
        self.calls = 0
        self.contexts = []

    def assess_continuation(self, context):
        self.calls += 1
        self.contexts.append(context)
        predictions = tuple(
            sorted(
                context.world_model.predictions,
                key=lambda item: (item.hypothesis_sha256, item.prediction_sha256),
            )
        )
        if self.partial:
            predictions = predictions[:1]
        assessments = tuple(
            HypothesisPredictionAssessment(
                hypothesis_sha256=item.hypothesis_sha256,
                prediction_sha256=item.prediction_sha256,
                prediction_fit=PredictionFit.OUT_OF_SUPPORT,
                fit_rule_sha256=(
                    _sha("untrusted-fit-rule")
                    if self.tamper == "fit_rule"
                    else self.policy.allowed_fit_rule_sha256s[0]
                ),
                assessment_artifact_sha256=_sha(f"assessment:{index}"),
            )
            for index, item in enumerate(predictions)
        )
        return PreparedContinuationAssessment(
            context_sha256=(
                _sha("rebound-context") if self.tamper == "context" else context.context_sha256
            ),
            assessments=assessments,
            assessment_implementation_sha256=(
                _sha("rebound-implementation")
                if self.tamper == "implementation"
                else self.policy.assessment_implementation_sha256
            ),
            assessed_by_principal_id=(
                self.source.case.authorization.message.validator_principal_id
                if self.tamper == "principal"
                else self.policy.allowed_assessor_principal_ids[0]
            ),
            assessed_at=context.latest_event_committed_at + timedelta(seconds=1),
        )


class _FailProvider:
    def assess_continuation(self, _context):
        raise AssertionError("exact retry must not reinvoke the continuation assessor")


def _policy() -> ContinuationAssessmentPolicyPin:
    return ContinuationAssessmentPolicyPin(
        assessment_implementation_sha256=_sha("continuation-assessor-implementation"),
        observed_outcome_identity_policy_sha256=OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
        allowed_assessor_principal_ids=("service:continuation-assessor",),
        allowed_fit_rule_sha256s=(_sha("continuation-fit-rule"),),
    )


def _binding(policy: ContinuationAssessmentPolicyPin) -> ControllerStepAuthorityBinding:
    return ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,
        principal_id="service:continuation-assessor",
        key_id=None,
        policy_sha256=policy.policy_sha256,
        service_manifest_sha256=_sha("continuation-assessor-service"),
        externally_deployed=True,
    )


def _wakeup(source) -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "9" * 32,
        quest_id=source.audit.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:continuation-assessment",
        source_sha256=_sha("continuation-launch"),
    )


def _projection(source) -> ControllerRecoveryProjection:
    return ControllerRecoveryProjection(
        quest_id=source.audit.quest_id,
        action_sha256=source.case.binding.action.object_sha256,
        scientific_slot_id=source.authorization.scientific_slot_id,
        audited_stream_version=source.audit.state.stream_version,
        audited_tail_event_sha256=source.audit.state.tail_event_sha256,
        audited_snapshot_sha256=source.audit.state.snapshot_sha256,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.ACCEPTED,
        scientific_execution_authorization_registered=True,
        execution_terminal_observed=True,
        validation_committed=True,
        admission_committed=True,
        observation_incorporated=True,
        continuation_committed=False,
        blocker_codes=(),
    )


@contextmanager
def _transaction(engine):
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def _sessions(engine):
    return lambda: _transaction(engine)


def _seed(engine, source) -> None:
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"),
            {"quest": source.audit.quest_id},
        )
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": source.case.binding.action.object_sha256},
        )
        session.execute(
            text("INSERT INTO execution_qualification_admissions VALUES (:admission)"),
            {
                "admission": source.committed_validation.message.receipt.message.raw_run.qualification_admission_sha256
            },
        )
        session.execute(
            text(
                "INSERT INTO research_kernel_events VALUES (:quest, :sequence, :event, :event_type)"
            ),
            [
                {
                    "quest": source.audit.quest_id,
                    "sequence": event.sequence,
                    "event": event.event_sha256,
                    "event_type": event.event_type.value,
                }
                for event in (
                    source.case.binding.action_authorized_event,
                    source.event,
                )
            ],
        )
        register_protocol_compilation(session, source.compilation)
        register_scientific_execution_authorization(session, source.authorization)
        record_observation_issuance_challenge(
            session,
            ObservationIssuanceChallengeWrite.from_contract(
                source.validation_challenge,
                quest_id=source.audit.quest_id,
                authorization_sha256=source.authorization.authorization_sha256,
                recorded_at=source.validation_challenge.message.issued_at,
            ),
        )
        record_observation_validation_receipt(session, source.validation_write)
        record_observation_issuance_challenge(
            session,
            ObservationIssuanceChallengeWrite.from_contract(
                source.admission_challenge,
                quest_id=source.audit.quest_id,
                authorization_sha256=source.authorization.authorization_sha256,
                recorded_at=source.admission_challenge.message.issued_at,
            ),
        )
        record_observation_admission(session, source.admission_write)
        session.flush()
        violations = session.execute(text("PRAGMA foreign_key_check")).all()
        assert not violations, violations
        session.commit()


def _service(source, engine, provider=None):
    policy = _policy()
    provider = provider or _Provider(source, policy)
    service = DurableContinuationAssessmentService(
        kernel_store=_Kernel(source.audit),
        object_archive=_Archive(source),
        provider=provider,
        assessment_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: source.event.committed_at + timedelta(seconds=3),
    )
    return service, policy, provider


def test_continuation_is_durable_and_restart_reuses_exact_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    engine = sqlite_observation_engine()
    _seed(engine, source)
    service, policy, provider = _service(source, engine)
    projection = _projection(source)
    plan = plan_recovery_tick(projection)

    first = service.derive_and_register(
        wakeup=_wakeup(source),
        projection=projection,
        plan=plan,
    )
    restarted = DurableContinuationAssessmentService(
        kernel_store=_Kernel(source.audit),
        object_archive=_Archive(source),
        provider=_FailProvider(),
        assessment_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: source.event.committed_at + timedelta(seconds=10),
    ).derive_and_register(
        wakeup=_wakeup(source),
        projection=projection,
        plan=plan,
    )

    assert first == restarted
    assert provider.calls == 1
    receipt = ContinuationReceipt.model_validate(first.receipt_json)
    context = provider.contexts[0]
    assert receipt.disposition is ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED
    assert receipt.assessment_provenance is not None
    assert receipt.assessment_provenance.assessment_source_sha256 == (
        context.assessment_source_sha256
    )
    assert first.observation_projection_sha256 == context.observation.projection_sha256
    expected_observation = project_admitted_scientific_observation(
        incorporation=source.event.payload,
        committed_validation=source.committed_validation,
    )
    assert context.observation == expected_observation
    with Session(engine) as session:
        assert (
            get_continuation_receipt_by_slot(
                session,
                quest_id=source.audit.quest_id,
                scientific_slot_id=source.authorization.scientific_slot_id,
            )
            == first
        )

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.DERIVE_CONTINUATION,
        adapter_code_sha256=_sha("continuation-step-adapter"),
        adapter_config_sha256=_sha("continuation-step-config"),
        authorities=(_binding(policy),),
        prepared_at=source.event.committed_at,
    )
    step_receipt = ContinuationAssessmentStepAdapter(
        manifest=manifest,
        assessments=service,
    ).execute(wakeup=_wakeup(source), projection=projection, plan=plan)
    assert step_receipt.disposition is ControllerStepDisposition.COMPLETED
    assert first.receipt_sha256 in step_receipt.result_artifact_sha256s
    assert receipt.assessment_provenance.provenance_sha256 in (step_receipt.result_artifact_sha256s)
    assert not step_receipt.signed_kernel_command_committed
    assert not step_receipt.independent_observation_admission_committed


def test_incomplete_active_hypothesis_assessment_durably_requests_redesign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    engine = sqlite_observation_engine()
    _seed(engine, source)
    policy = _policy()
    provider = _Provider(source, policy, partial=True)
    service, _policy_pin, _provider = _service(source, engine, provider)
    projection = _projection(source)

    write = service.derive_and_register(
        wakeup=_wakeup(source),
        projection=projection,
        plan=plan_recovery_tick(projection),
    )
    receipt = ContinuationReceipt.model_validate(write.receipt_json)

    assert receipt.disposition is ContinuationDisposition.REDESIGN_OBSERVABLE
    assert receipt.reason_codes == ("active_hypothesis_prediction_missing",)


@pytest.mark.parametrize("tamper", ("context", "implementation", "fit_rule", "principal"))
def test_provider_policy_or_authority_rebinding_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    source = _source(monkeypatch)
    engine = sqlite_observation_engine()
    _seed(engine, source)
    policy = _policy()
    provider = _Provider(source, policy, tamper=tamper)
    service, _policy_pin, _provider = _service(source, engine, provider)
    projection = _projection(source)

    with pytest.raises(ContinuationAssessmentStepError):
        service.derive_and_register(
            wakeup=_wakeup(source),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchContinuationReceiptRecord)) == 0
        )


def test_incorporation_rebinding_cannot_create_observation_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    payload = source.event.payload.model_copy(update={"outcome": "positive"})

    with pytest.raises(ValueError, match="differs from signed validation"):
        project_admitted_scientific_observation(
            incorporation=payload,
            committed_validation=source.committed_validation,
        )


def test_second_kernel_audit_rejects_source_drift_before_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    engine = sqlite_observation_engine()
    _seed(engine, source)
    policy = _policy()
    provider = _Provider(source, policy)
    service = DurableContinuationAssessmentService(
        kernel_store=_DriftingKernel(source.audit),
        object_archive=_Archive(source),
        provider=provider,
        assessment_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: source.event.committed_at + timedelta(seconds=3),
    )
    projection = _projection(source)

    with pytest.raises(ContinuationAssessmentStepError):
        service.derive_and_register(
            wakeup=_wakeup(source),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    assert provider.calls == 1
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchContinuationReceiptRecord)) == 0
        )


def test_unavailable_assessor_maps_to_typed_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    projection = _projection(source)
    plan = plan_recovery_tick(projection)
    policy = _policy()

    class Unavailable:
        authority_binding = _binding(policy)

        def derive_and_register(self, **_kwargs):
            raise ContinuationAssessmentUnavailable(("continuation:no_assessor",))

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.DERIVE_CONTINUATION,
        adapter_code_sha256=_sha("unavailable-continuation-adapter"),
        adapter_config_sha256=_sha("unavailable-continuation-config"),
        authorities=(_binding(policy),),
        prepared_at=source.event.committed_at,
    )
    receipt = ContinuationAssessmentStepAdapter(
        manifest=manifest,
        assessments=Unavailable(),
    ).execute(wakeup=_wakeup(source), projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.BLOCKED
    assert receipt.blocker_codes == ("continuation:no_assessor",)


def test_adapter_wraps_untyped_continuation_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(monkeypatch)
    projection = _projection(source)
    policy = _policy()

    class Corrupt:
        authority_binding = _binding(policy)

        def derive_and_register(self, **_kwargs):
            return object()

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.DERIVE_CONTINUATION,
        adapter_code_sha256=_sha("corrupt-continuation-adapter"),
        adapter_config_sha256=_sha("corrupt-continuation-config"),
        authorities=(_binding(policy),),
        prepared_at=source.event.committed_at,
    )
    with pytest.raises(ControllerStepExecutionError):
        ContinuationAssessmentStepAdapter(
            manifest=manifest,
            assessments=Corrupt(),
        ).execute(
            wakeup=_wakeup(source),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
