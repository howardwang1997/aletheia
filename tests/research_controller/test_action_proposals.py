from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aletheia.research_controller.action_proposal_service import (
    ActionProposalMaterializationService,
    WriteOnceActionProposalSpool,
)
from aletheia.research_controller.action_proposals import (
    ActionProposalDraft,
    ActionProposalError,
    ActionProposalStepAdapter,
    ActionProposalTarget,
    ActionProposalTargetLifecycle,
    ControllerActionProposalRequest,
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
from aletheia.research_kernel.commands import ResearchScopeBinding
from aletheia.research_kernel.schemas import (
    ActionKind,
    EvidenceKind,
    EvidenceRef,
    EventType,
    KernelObjectKind,
    KernelObjectRef,
    canonical_json_bytes,
)

NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
QUEST_ID = "qst_" + "1" * 32
BRANCH_ID = "rbr_" + "2" * 32
SLOT_ID = "sos_" + "3" * 32


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding() -> ControllerStepAuthorityBinding:
    return ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.ACTION_PROPOSAL,
        principal_id="service:action-proposal",
        key_id=None,
        policy_sha256=_sha("proposal-policy"),
        service_manifest_sha256=_sha("proposal-service"),
        externally_deployed=True,
    )


def _wakeup() -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "4" * 32,
        quest_id=QUEST_ID,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:action-proposal",
        source_sha256=_sha("launch-source"),
    )


def _projection(step: ControllerStep) -> ControllerRecoveryProjection:
    values: dict[str, object] = {
        "quest_id": QUEST_ID,
        "action_sha256": _sha("source-action"),
        "scientific_slot_id": None,
        "audited_stream_version": 7,
        "audited_tail_event_sha256": _sha("tail"),
        "audited_snapshot_sha256": _sha("snapshot"),
        "action_authorized": True,
        "compilation_disposition": CompilationDisposition.ACCEPTED,
        "scientific_execution_authorization_registered": False,
        "execution_terminal_observed": False,
        "validation_committed": False,
        "admission_committed": False,
        "observation_incorporated": False,
        "continuation_committed": False,
        "blocker_codes": (),
    }
    if step is ControllerStep.PROPOSE_ACTION:
        values.update(
            action_sha256=None,
            action_authorized=False,
            compilation_disposition=CompilationDisposition.MISSING,
        )
    elif step is ControllerStep.PROPOSE_REDESIGN:
        values.update(compilation_disposition=CompilationDisposition.BLOCKED)
    elif step is ControllerStep.PROPOSE_FOLLOWUP:
        values.update(
            scientific_slot_id=SLOT_ID,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
            admission_committed=True,
            observation_incorporated=True,
            continuation_committed=True,
        )
    else:  # pragma: no cover - fixture supports only this module's closed proposal steps
        raise AssertionError(step)
    projection = ControllerRecoveryProjection.model_validate(values)
    assert plan_recovery_tick(projection).step is step
    return projection


def _request(step: ControllerStep) -> ControllerActionProposalRequest:
    projection = _projection(step)
    required_kind = {
        ControllerStep.PROPOSE_ACTION: None,
        ControllerStep.PROPOSE_REDESIGN: ActionKind.REFINE,
        ControllerStep.PROPOSE_FOLLOWUP: ActionKind.FORK,
    }[step]
    evidence = (
        ()
        if required_kind is None
        else (
            EvidenceRef(
                kind=(
                    EvidenceKind.OBJECTION
                    if step is ControllerStep.PROPOSE_REDESIGN
                    else EvidenceKind.CONTRADICTION
                ),
                object_sha256=_sha(f"receipt:{step.value}"),
                object_id=f"receipt:{step.value}",
            ),
        )
    )
    question = KernelObjectRef(
        object_kind=KernelObjectKind.QUESTION,
        object_id="question:proposal",
        object_sha256=_sha("question"),
        quest_id=QUEST_ID,
    )
    target = ActionProposalTarget(
        branch_id=BRANCH_ID,
        branch_lifecycle=ActionProposalTargetLifecycle.ACTIVE,
        question_ref=question,
        allowed_action_kinds=(required_kind or ActionKind.DISCRIMINATE,),
    )
    return ControllerActionProposalRequest(
        wakeup_sha256=_wakeup().wakeup_sha256,
        recovery_projection_sha256=projection.projection_sha256,
        plan_sha256=plan_recovery_tick(projection).plan_sha256,
        step=step,
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        expected_stream_version=projection.audited_stream_version,
        expected_tail_event_sha256=projection.audited_tail_event_sha256,
        expected_snapshot_sha256=projection.audited_snapshot_sha256,
        charter_ref=KernelObjectRef(
            object_kind=KernelObjectKind.CHARTER,
            object_id="charter:proposal",
            object_sha256=_sha("charter"),
            quest_id=QUEST_ID,
        ),
        targets=(target,),
        required_action_kind=required_kind,
        required_evidence_refs=evidence,
        allowed_alternative_action_refs=(),
        source_action_sha256=(projection.action_sha256 if required_kind is not None else None),
        source_receipt_sha256=(evidence[0].object_sha256 if evidence else None),
        latest_event_committed_at=NOW,
    )


def _draft(
    request: ControllerActionProposalRequest,
    *,
    action_id: str = "action:provider-proposal",
) -> ActionProposalDraft:
    target = request.targets[0]
    return ActionProposalDraft(
        request_sha256=request.request_sha256,
        action_id=action_id,
        target_sha256=target.target_sha256,
        kind=request.required_action_kind or ActionKind.DISCRIMINATE,
        epistemic_purpose="Resolve the exact audited scientific uncertainty.",
        candidate_outcomes=("negative", "positive"),
        cost_receipt_sha256=_sha("cost"),
        risk_receipt_sha256=_sha("risk"),
        alternative_action_refs=(),
        requested_authority_class="scientific-measurement",
        proposed_at=NOW + timedelta(seconds=1),
    )


class _Context:
    def __init__(self, request: ControllerActionProposalRequest) -> None:
        self.request = request
        self.calls = 0

    def load_request(self, *, wakeup, projection, plan):
        self.calls += 1
        assert wakeup == _wakeup()
        assert projection.projection_sha256 == self.request.recovery_projection_sha256
        assert plan.plan_sha256 == self.request.plan_sha256
        return self.request


class _Provider:
    def __init__(self, *, action_id: str = "action:provider-proposal") -> None:
        self.action_id = action_id
        self.calls = 0

    def propose_action(self, request):
        self.calls += 1
        return _draft(request, action_id=self.action_id)


def _service(tmp_path, step: ControllerStep, provider=None):
    request = _request(step)
    context = _Context(request)
    provider = provider or _Provider()
    spool = WriteOnceActionProposalSpool(tmp_path / "proposal-spool", authority_binding=_binding())
    service = ActionProposalMaterializationService(
        context_source=context,
        provider=provider,
        submissions=spool,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    return service, context, provider, spool


@pytest.mark.parametrize(
    ("step", "kind"),
    (
        (ControllerStep.PROPOSE_ACTION, ActionKind.DISCRIMINATE),
        (ControllerStep.PROPOSE_REDESIGN, ActionKind.REFINE),
        (ControllerStep.PROPOSE_FOLLOWUP, ActionKind.FORK),
    ),
)
def test_three_proposal_steps_materialize_powerless_exact_commands(tmp_path, step, kind) -> None:
    service, _, provider, _ = _service(tmp_path, step)
    projection = _projection(step)
    plan = plan_recovery_tick(projection)
    manifest = ControllerStepAdapterManifest(
        step=step,
        adapter_code_sha256=_sha(f"adapter:{step.value}"),
        adapter_config_sha256=_sha(f"config:{step.value}"),
        authorities=(_binding(),),
        prepared_at=NOW,
    )
    receipt = ActionProposalStepAdapter(manifest=manifest, proposals=service).execute(
        wakeup=_wakeup(),
        projection=projection,
        plan=plan,
    )
    submission = service.materialize_and_submit(
        wakeup=_wakeup(),
        projection=projection,
        plan=plan,
    )

    assert receipt.disposition is ControllerStepDisposition.AWAITING_AUTHORITY
    assert set(receipt.result_artifact_sha256s) == {
        submission.action.object_sha256,
        submission.command_proposal.proposal_sha256,
        submission.submission_sha256,
    }
    assert submission.action.kind is kind
    assert submission.action.evidence_refs == submission.request.required_evidence_refs
    assert submission.action.basis_tail_event_sha256 == projection.audited_tail_event_sha256
    assert submission.command_proposal.event_type is EventType.ACTION_PROPOSED
    assert submission.awaiting_independent_kernel_authority
    assert not submission.kernel_command_signed
    assert not submission.kernel_state_mutated
    assert provider.calls == 1


def test_exact_retry_freshly_reloads_spool_without_reinvoking_provider(tmp_path) -> None:
    service, context, provider, spool = _service(tmp_path, ControllerStep.PROPOSE_ACTION)
    projection = _projection(ControllerStep.PROPOSE_ACTION)
    plan = plan_recovery_tick(projection)

    first = service.materialize_and_submit(wakeup=_wakeup(), projection=projection, plan=plan)
    restarted = ActionProposalMaterializationService(
        context_source=context,
        provider=provider,
        submissions=WriteOnceActionProposalSpool(
            spool.root,
            authority_binding=_binding(),
        ),
        clock=lambda: NOW + timedelta(seconds=10),
    ).materialize_and_submit(wakeup=_wakeup(), projection=projection, plan=plan)

    assert restarted == first
    assert provider.calls == 1
    assert context.calls == 2


def test_concurrent_variant_providers_converge_on_one_first_winner(tmp_path) -> None:
    step = ControllerStep.PROPOSE_ACTION
    request = _request(step)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    class Provider:
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def propose_action(self, supplied):
            barrier.wait(timeout=5)
            return _draft(supplied, action_id=f"action:concurrent-{self.suffix}")

    spool = WriteOnceActionProposalSpool(tmp_path / "spool", authority_binding=_binding())

    def run(suffix: str) -> None:
        try:
            service = ActionProposalMaterializationService(
                context_source=_Context(request),
                provider=Provider(suffix),
                submissions=spool,
                clock=lambda: NOW + timedelta(seconds=2),
            )
            projection = _projection(step)
            results.append(
                service.materialize_and_submit(
                    wakeup=_wakeup(), projection=projection, plan=plan_recovery_tick(projection)
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(suffix,)) for suffix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0].action.action_id in {"action:concurrent-a", "action:concurrent-b"}
    assert spool.load(request_sha256=request.request_sha256) == results[0]


def test_spool_fresh_read_rejects_mutable_or_tampered_submission(tmp_path) -> None:
    service, _, _, spool = _service(tmp_path, ControllerStep.PROPOSE_ACTION)
    projection = _projection(ControllerStep.PROPOSE_ACTION)
    submission = service.materialize_and_submit(
        wakeup=_wakeup(), projection=projection, plan=plan_recovery_tick(projection)
    )
    target = (
        spool.root
        / "requests"
        / submission.request.request_sha256[:2]
        / f"{submission.request.request_sha256}.json"
    )
    target.chmod(0o600)

    with pytest.raises(ActionProposalError):
        spool.load(request_sha256=submission.request.request_sha256)


def test_provider_cannot_add_graph_authority_fields_or_escape_required_kind(tmp_path) -> None:
    request = _request(ControllerStep.PROPOSE_REDESIGN)
    payload = _draft(request).model_dump(mode="python")
    payload["quest_id"] = "qst_" + "f" * 32
    with pytest.raises(ValidationError):
        ActionProposalDraft.model_validate(payload)

    class WrongKindProvider:
        def propose_action(self, supplied):
            return _draft(supplied).model_copy(update={"kind": ActionKind.STOP})

    service, _, _, _ = _service(
        tmp_path,
        ControllerStep.PROPOSE_REDESIGN,
        provider=WrongKindProvider(),
    )
    projection = _projection(ControllerStep.PROPOSE_REDESIGN)
    with pytest.raises(ActionProposalError):
        service.materialize_and_submit(
            wakeup=_wakeup(), projection=projection, plan=plan_recovery_tick(projection)
        )


def test_adapter_rejects_a_stale_tick_instead_of_reusing_a_submission(tmp_path) -> None:
    step = ControllerStep.PROPOSE_ACTION
    service, _, _, _ = _service(tmp_path, step)
    projection = _projection(step)
    plan = plan_recovery_tick(projection)
    stale = plan.model_copy(update={"audited_stream_version": plan.audited_stream_version + 1})
    manifest = ControllerStepAdapterManifest(
        step=step,
        adapter_code_sha256=_sha("adapter"),
        adapter_config_sha256=_sha("config"),
        authorities=(_binding(),),
        prepared_at=NOW,
    )

    with pytest.raises(ControllerStepExecutionError):
        ActionProposalStepAdapter(manifest=manifest, proposals=service).execute(
            wakeup=_wakeup(), projection=projection, plan=stale
        )


def test_submission_bytes_are_canonical_and_contain_no_signature_or_private_key(tmp_path) -> None:
    service, _, _, spool = _service(tmp_path, ControllerStep.PROPOSE_ACTION)
    projection = _projection(ControllerStep.PROPOSE_ACTION)
    submission = service.materialize_and_submit(
        wakeup=_wakeup(), projection=projection, plan=plan_recovery_tick(projection)
    )
    payload = canonical_json_bytes(submission)

    assert spool.load(request_sha256=submission.request.request_sha256) == submission
    assert b"private_key" not in payload
    assert b"signature" not in payload
    assert b'"kernel_command_signed":false' in payload
