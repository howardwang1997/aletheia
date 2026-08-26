from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aletheia.observations.store import (
    ContinuationReceiptWrite,
    ProtocolCompilationWrite,
)
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    compile_protocol,
)
from aletheia.protocols.schemas import ProtocolIR
from aletheia.research_controller import action_proposal_service as service_module
from aletheia.research_controller.action_proposal_service import (
    PostgreSQLActionProposalContextSource,
)
from aletheia.research_controller.action_proposals import ActionProposalError
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    CharterActivatedPayload,
    EvidenceKind,
    EventType,
    ObservationIncorporatedPayload,
)
from aletheia.research_store.store import ResearchReplayAudit

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "protocols",
    _TESTS / "research_kernel",
    _TESTS / "research_controller",
):
    sys.path.insert(0, str(_fixture_dir))

from fixtures import fixture_by_name  # noqa: E402
from test_reducer import (  # noqa: E402
    Scenario,
    _admit_problem_and_question,
    _charter,
    _propose,
)
from test_vertical_cut import _f9_enriched_grouped_fixture  # noqa: E402


def _started_case() -> tuple[Scenario, object]:
    base_scope = fixture_by_name("grouped_regression").request.protocol.graph_scope
    case = Scenario(
        quest_id=base_scope.scope_binding.quest_id,
        root_branch_id=base_scope.branch_id,
    )
    case.charter = _charter(case.quest_id)
    case.add_object(case.charter)
    case.commit(
        EventType.CHARTER_ACTIVATED,
        CharterActivatedPayload(
            charter_ref=case.charter.object_ref,
            root_branch_id=case.root_branch_id,
        ),
    )
    _, _, question = _admit_problem_and_question(case)
    return case, question


def _audit(case: Scenario) -> ResearchReplayAudit:
    scope = fixture_by_name("grouped_regression").request.protocol.graph_scope.scope_binding
    return ResearchReplayAudit(
        quest_id=case.quest_id,
        scope_binding=scope,
        events=tuple(case.events),
        state=case.state,
        verified_snapshot_sha256s=tuple(case.state.snapshot_sha256 for _event in case.events),
    )


def _projection(
    case: Scenario,
    *,
    action_sha256: str | None,
    compilation: CompilationDisposition,
    slot: str | None = None,
    continuation: bool = False,
) -> ControllerRecoveryProjection:
    downstream = slot is not None
    return ControllerRecoveryProjection(
        quest_id=case.quest_id,
        action_sha256=action_sha256,
        scientific_slot_id=slot,
        audited_stream_version=case.state.stream_version,
        audited_tail_event_sha256=case.state.tail_event_sha256,
        audited_snapshot_sha256=case.state.snapshot_sha256,
        action_authorized=action_sha256 is not None,
        compilation_disposition=compilation,
        scientific_execution_authorization_registered=downstream,
        execution_terminal_observed=downstream,
        validation_committed=downstream,
        admission_committed=downstream,
        observation_incorporated=downstream,
        continuation_committed=continuation,
        blocker_codes=(),
    )


def _wakeup(case: Scenario) -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "7" * 32,
        quest_id=case.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:proposal-context",
        source_sha256="8" * 64,
    )


class _Kernel:
    def __init__(self, audit: ResearchReplayAudit) -> None:
        self.audit = audit

    def audit_in_session(self, _session, quest_id):
        assert quest_id == self.audit.quest_id
        return self.audit


class _Archive:
    def __init__(self, case: Scenario) -> None:
        self.case = case

    def load_object(self, ref):
        return SimpleNamespace(payload=self.case.objects[ref.object_sha256])


@contextmanager
def _session_scope():
    yield object()


def _source(monkeypatch: pytest.MonkeyPatch, case: Scenario):
    monkeypatch.setattr(service_module, "session_scope", _session_scope)
    return PostgreSQLActionProposalContextSource(
        kernel_store=_Kernel(_audit(case)),
        object_archive=_Archive(case),
    )


def test_initial_request_is_rebuilt_from_current_nonterminal_kernel_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, question = _started_case()
    projection = _projection(
        case,
        action_sha256=None,
        compilation=CompilationDisposition.MISSING,
    )
    plan = plan_recovery_tick(projection)
    request = _source(monkeypatch, case).load_request(
        wakeup=_wakeup(case), projection=projection, plan=plan
    )

    assert plan.step is ControllerStep.PROPOSE_ACTION
    assert request.charter_ref == case.charter.object_ref
    assert len(request.targets) == 1
    assert request.targets[0].question_ref == question.object_ref
    assert request.targets[0].branch_id == case.root_branch_id
    assert ActionKind.ACTIVATE not in request.targets[0].allowed_action_kinds
    assert request.required_evidence_refs == ()
    assert not request.direct_kernel_mutation_allowed
    assert not request.signing_key_available


def _authorized_case():
    case, question = _started_case()
    action = _propose(
        case,
        question,
        case.root_branch_id,
        ActionKind.DISCRIMINATE,
        "proposal-context",
    )
    authorized = case.commit(
        EventType.ACTION_AUTHORIZED,
        ActionAuthorizedPayload(
            action_id=action.action_id,
            branch_id=case.root_branch_id,
        ),
    )
    return case, question, action, authorized


def _blocked_compilation(case, question, action, authorized):
    base_scope = fixture_by_name("grouped_regression").request.protocol.graph_scope
    graph_scope = ProtocolScope(
        scope_binding=base_scope.scope_binding,
        scope_node_id=base_scope.scope_node_id,
        branch_id=case.root_branch_id,
        question_ref=question.object_ref,
        graph_snapshot_sha256=case.state.snapshot_sha256,
    )
    enriched = _f9_enriched_grouped_fixture(
        graph_scope=graph_scope,
        protocol_authored_at=authorized.committed_at + timedelta(seconds=1),
    )
    blocked_protocol = ProtocolIR.model_validate(
        {
            **enriched.request.protocol.model_dump(mode="python"),
            "observables": (),
            "observable_output_bindings": (),
        }
    )
    request = ProtocolCompilationRequest(
        protocol=blocked_protocol,
        capability_catalog=enriched.request.capability_catalog,
        resource_catalog=enriched.request.resource_catalog,
        compiler_implementation_sha256=enriched.request.compiler_implementation_sha256,
    )
    result = compile_protocol(request)
    assert not result.report.accepted
    return ProtocolCompilationWrite.from_contract(
        quest_id=case.quest_id,
        action_sha256=action.object_sha256,
        request=request,
        result=result,
        registered_at=request.protocol.authored_at + timedelta(seconds=1),
    )


def test_redesign_request_recomputes_blocked_compilation_and_binds_objection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, question, action, authorized = _authorized_case()
    compilation = _blocked_compilation(case, question, action, authorized)
    monkeypatch.setattr(
        service_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: compilation,
    )
    projection = _projection(
        case,
        action_sha256=action.object_sha256,
        compilation=CompilationDisposition.BLOCKED,
    )
    plan = plan_recovery_tick(projection)
    request = _source(monkeypatch, case).load_request(
        wakeup=_wakeup(case), projection=projection, plan=plan
    )

    assert plan.step is ControllerStep.PROPOSE_REDESIGN
    assert request.required_action_kind is ActionKind.REFINE
    assert request.source_action_sha256 == action.object_sha256
    assert request.source_receipt_sha256 == compilation.receipt_sha256
    assert request.required_evidence_refs[0].kind is EvidenceKind.OBJECTION
    assert request.required_evidence_refs[0].object_sha256 == compilation.receipt_sha256
    assert request.targets[0].allowed_action_kinds == (ActionKind.REFINE,)

    rebound = compilation.model_copy(update={"action_sha256": "f" * 64})
    monkeypatch.setattr(
        service_module,
        "get_protocol_compilation_by_action",
        lambda _session, **_kwargs: rebound,
    )
    with pytest.raises(ActionProposalError):
        _source(monkeypatch, case).load_request(
            wakeup=_wakeup(case), projection=projection, plan=plan
        )


def test_followup_request_binds_incorporated_observation_and_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case, _question, action, _authorized = _authorized_case()
    slot = "sos_" + "9" * 32
    observation = ObservationIncorporatedPayload(
        branch_id=case.root_branch_id,
        action_id=action.action_id,
        scientific_slot_id=slot,
        committed_admission_sha256="a" * 64,
        scientific_observation_sha256="b" * 64,
        outcome="negative",
        source_world_model_sha256="c" * 64,
    )
    incorporated = case.commit(EventType.OBSERVATION_INCORPORATED, observation)
    receipt = ContinuationReceipt(
        world_model_snapshot_sha256=observation.source_world_model_sha256,
        observation_projection_sha256="d" * 64,
        scientific_slot_id=slot,
        assessments=(),
        disposition=ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED,
        reason_codes=("all_active_hypotheses_out_of_support",),
        proposed_action_kind=ActionKind.FORK,
    )
    row = ContinuationReceiptWrite(
        receipt_sha256=receipt.receipt_sha256,
        quest_id=case.quest_id,
        action_sha256=action.object_sha256,
        scientific_slot_id=slot,
        world_model_snapshot_sha256=receipt.world_model_snapshot_sha256,
        observation_projection_sha256=receipt.observation_projection_sha256,
        scientific_observation_sha256=observation.scientific_observation_sha256,
        committed_admission_sha256=observation.committed_admission_sha256,
        disposition=receipt.disposition.value,
        receipt_json=receipt.model_dump(mode="json"),
        recorded_at=incorporated.committed_at + timedelta(seconds=1),
    )
    monkeypatch.setattr(
        service_module,
        "get_continuation_receipt_by_slot",
        lambda _session, **_kwargs: row,
    )
    projection = _projection(
        case,
        action_sha256=action.object_sha256,
        compilation=CompilationDisposition.ACCEPTED,
        slot=slot,
        continuation=True,
    )
    assert next(
        item for item in case.state.actions if item.action_ref == action.object_ref
    ).lifecycle is (ActionLifecycle.APPLIED)
    plan = plan_recovery_tick(projection)
    request = _source(monkeypatch, case).load_request(
        wakeup=_wakeup(case), projection=projection, plan=plan
    )

    assert plan.step is ControllerStep.PROPOSE_FOLLOWUP
    assert request.required_action_kind is ActionKind.FORK
    assert request.source_receipt_sha256 == receipt.receipt_sha256
    assert {item.kind for item in request.required_evidence_refs} == {
        EvidenceKind.NEGATIVE,
        EvidenceKind.CONTRADICTION,
    }
    assert {item.object_sha256 for item in request.required_evidence_refs} == {
        observation.committed_admission_sha256,
        receipt.receipt_sha256,
    }

    rebound = row.model_copy(update={"scientific_observation_sha256": "e" * 64})
    monkeypatch.setattr(
        service_module,
        "get_continuation_receipt_by_slot",
        lambda _session, **_kwargs: rebound,
    )
    with pytest.raises(ActionProposalError):
        _source(monkeypatch, case).load_request(
            wakeup=_wakeup(case), projection=projection, plan=plan
        )
