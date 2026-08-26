from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from aletheia.observations.scientific_bridge import ScientificActionProtocolBinding
from aletheia.observations.store import (
    ProtocolCompilationWrite,
    register_protocol_compilation,
)
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.compiler import ProtocolCompilationRequest, compile_protocol
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.execution_authorization_service import (
    ExecutionAuthorizationServiceError,
    FrozenScientificExecutionAuthorizationCatalog,
    FrozenScientificExecutionAuthorizationIssuer,
    FrozenScientificExecutionAuthorizationTemplate,
    PostgreSQLScientificExecutionAuthorizationSource,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    BranchLifecycle,
    BranchSnapshot,
    ObjectAdmission,
    ResearchStateGraph,
)
from aletheia.research_store.store import ResearchReplayAudit

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_root in (
    _TESTS / "execution",
    _TESTS / "observations",
    _TESTS / "protocols",
    _TESTS / "research_controller",
):
    sys.path.insert(0, str(_fixture_root))
from persistence_test_support import sqlite_observation_engine  # noqa: E402
from test_runtime_contracts import _slot  # noqa: E402
from test_scientific_bridge import (  # noqa: E402
    EXECUTION_AUTHORITY_PRIVATE_KEY,
    _bridge_case,
)
from test_vertical_cut import _f9_enriched_grouped_fixture  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


class _Source:
    def __init__(self, write: ProtocolCompilationWrite) -> None:
        self.write = write
        self.source_calls: list[dict[str, object]] = []
        self.action_calls: list[dict[str, object]] = []
        self.rebound_after_first = False

    def verify_action_protocol_binding(self, *, binding, observed_at):
        self.action_calls.append({"binding": binding, "observed_at": observed_at})
        return binding.binding_sha256

    def verify_execution_authorization_source(self, **scope):
        self.source_calls.append(scope)
        if self.rebound_after_first and len(self.source_calls) > 1:
            return self.write.model_copy(
                update={"registered_at": self.write.registered_at + timedelta(seconds=1)}
            )
        return self.write


def _case():
    bridge = _bridge_case()
    message = bridge.authorization.message
    binding = message.action_protocol_binding
    write = ProtocolCompilationWrite.from_contract(
        quest_id=binding.action.quest_id,
        action_sha256=binding.action.object_sha256,
        request=binding.compilation_request,
        result=binding.compilation_result,
        registered_at=binding.bound_at + timedelta(seconds=1),
    )
    template = FrozenScientificExecutionAuthorizationTemplate(
        action_sha256=binding.action.object_sha256,
        compilation_sha256=write.compilation_sha256,
        action_protocol_binding=binding,
        qualification_bundle=message.qualification_bundle,
        qualification_grant=message.qualification_grant,
        validator_manifest_sha256=message.validator_manifest_sha256,
        observation_validation_policy_sha256=(message.observation_validation_policy_sha256),
        admission_policy=message.admission_policy,
        scientific_observation_artifact_binding=(message.scientific_observation_artifact_binding),
        authorized_at=message.authorized_at,
        expires_at=message.expires_at,
        observation_admission_deadline=message.observation_admission_deadline,
    )
    implementation_sha256 = _sha("execution-authorization-issuer-implementation")
    catalog = FrozenScientificExecutionAuthorizationCatalog(
        issuer_implementation_sha256=implementation_sha256,
        qualification_authority_pin=bridge.qualification.pin,
        execution_authority_pin=bridge.execution_pin,
        validator_authority_pin=bridge.validator_pin,
        admission_authority_pin=bridge.admission_pin,
        templates=(template,),
    )
    authority_binding = ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        principal_id=bridge.execution_pin.principal_id,
        key_id=bridge.execution_pin.key_id,
        policy_sha256=bridge.execution_pin.policy_sha256,
        service_manifest_sha256=_sha("execution-authorization-service"),
        externally_deployed=True,
    )
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id=binding.action.quest_id,
        source_kind=ControllerWakeupKind.KERNEL_OUTBOX,
        source_key=f"rko_{binding.action_authorized_event.event_sha256[:32]}",
        source_sha256=binding.action_authorized_event.event_sha256,
        source_stream_version=binding.action_authorized_event.sequence,
    )
    projection = ControllerRecoveryProjection(
        quest_id=wakeup.quest_id,
        action_sha256=binding.action.object_sha256,
        scientific_slot_id=None,
        audited_stream_version=binding.action_authorized_event.sequence,
        audited_tail_event_sha256=binding.action_authorized_event.event_sha256,
        audited_snapshot_sha256=binding.authorized_graph_snapshot_sha256,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.ACCEPTED,
        scientific_execution_authorization_registered=False,
        execution_terminal_observed=False,
        validation_committed=False,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )
    plan = plan_recovery_tick(projection)
    assert plan.step is ControllerStep.REGISTER_EXECUTION
    source = _Source(write)
    service = FrozenScientificExecutionAuthorizationIssuer(
        source=source,
        qualification_authority=bridge.qualification_authority,
        qualification_custody=bridge.qualification_custody,
        catalog=catalog,
        authority_binding=authority_binding,
        private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
        implementation_sha256=implementation_sha256,
        clock=lambda: message.authorized_at + timedelta(seconds=30),
    )
    return bridge, source, service, wakeup, projection, plan, catalog, authority_binding


def test_frozen_execution_authorization_is_exact_and_retry_stable() -> None:
    bridge, source, service, wakeup, projection, plan, _catalog, _binding = _case()

    first = service.issue_scientific_execution_authorization(
        wakeup=wakeup,
        projection=projection,
        plan=plan,
    )
    second = service.issue_scientific_execution_authorization(
        wakeup=wakeup,
        projection=projection,
        plan=plan,
    )

    assert first == second == bridge.authorization
    assert first.authorization_sha256 == bridge.authorization.authorization_sha256
    assert len(source.source_calls) == 4
    assert len(source.action_calls) == 6
    assert all(
        call["expected_compilation_sha256"] == source.write.compilation_sha256
        for call in source.source_calls
    )


def test_execution_authorization_rejects_stale_tick_or_source_drift() -> None:
    _bridge, source, service, wakeup, projection, plan, _catalog, _binding = _case()
    source.rebound_after_first = True
    with pytest.raises(ExecutionAuthorizationServiceError, match="changed while signing"):
        service.issue_scientific_execution_authorization(
            wakeup=wakeup,
            projection=projection,
            plan=plan,
        )

    _bridge, _source, service, wakeup, projection, _plan, _catalog, _binding = _case()
    stale = projection.model_copy(update={"action_sha256": "f" * 64})
    with pytest.raises(ExecutionAuthorizationServiceError, match="no unique frozen"):
        service.issue_scientific_execution_authorization(
            wakeup=wakeup,
            projection=stale,
            plan=plan_recovery_tick(stale),
        )


def test_execution_authorization_rejects_expired_template_and_rebound_signer() -> None:
    bridge, source, _service, wakeup, projection, plan, catalog, binding = _case()
    with pytest.raises(ExecutionAuthorizationServiceError, match="issuance window"):
        FrozenScientificExecutionAuthorizationIssuer(
            source=source,
            qualification_authority=bridge.qualification_authority,
            qualification_custody=bridge.qualification_custody,
            catalog=catalog,
            authority_binding=binding,
            private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
            implementation_sha256=catalog.issuer_implementation_sha256,
            clock=lambda: bridge.authorization.message.expires_at,
        ).issue_scientific_execution_authorization(
            wakeup=wakeup,
            projection=projection,
            plan=plan,
        )

    rebound = binding.model_copy(update={"policy_sha256": _sha("rebound-policy")})
    with pytest.raises(ValueError, match="frozen authority"):
        FrozenScientificExecutionAuthorizationIssuer(
            source=source,
            qualification_authority=bridge.qualification_authority,
            qualification_custody=bridge.qualification_custody,
            catalog=catalog,
            authority_binding=rebound,
            private_key=EXECUTION_AUTHORITY_PRIVATE_KEY,
            implementation_sha256=catalog.issuer_implementation_sha256,
        )


def test_catalog_rejects_unordered_or_rebound_templates() -> None:
    _bridge, _source, _service, _wakeup, _projection, _plan, catalog, _binding = _case()
    template = catalog.templates[0]
    rebound = template.model_copy(update={"action_sha256": "f" * 64})
    with pytest.raises(ValueError, match="escaped its action"):
        FrozenScientificExecutionAuthorizationCatalog(
            **catalog.model_dump(mode="python", exclude={"templates"}),
            templates=(rebound,),
        )
    with pytest.raises(ValueError, match="canonical and one per action"):
        FrozenScientificExecutionAuthorizationCatalog(
            **catalog.model_dump(mode="python", exclude={"templates"}),
            templates=(template, template),
        )


class _Kernel:
    def __init__(self, audit) -> None:
        self._audit = audit

    def audit(self, quest_id, *, expected_scope_binding=None):
        assert quest_id == self._audit.quest_id
        assert expected_scope_binding == self._audit.scope_binding
        return self._audit

    def audit_in_session(self, _session, quest_id, *, expected_scope_binding=None):
        return self.audit(quest_id, expected_scope_binding=expected_scope_binding)


@contextmanager
def _transaction(engine):
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def test_postgresql_source_replays_exact_kernel_and_compilation_registry() -> None:
    bridge = _bridge_case()
    original = bridge.authorization.message.action_protocol_binding
    action = original.action
    proposed = original.action_proposed_event
    authorized = original.action_authorized_event
    branch_id = original.compilation_request.protocol.graph_scope.branch_id
    state = ResearchStateGraph(
        quest_id=action.quest_id,
        stream_version=authorized.sequence,
        tail_event_sha256=authorized.event_sha256,
        event_ids=(proposed.event_id, authorized.event_id),
        event_sha256s=(proposed.event_sha256, authorized.event_sha256),
        charter_ref=action.charter_ref,
        charter_history=(action.charter_ref,),
        branches=(
            BranchSnapshot(
                branch_id=branch_id,
                origin_event_sha256=action.basis_tail_event_sha256,
                head_event_sha256=authorized.event_sha256,
                lifecycle=BranchLifecycle.ACTIVE,
                question_refs=(action.question_ref,),
                action_ids=(action.action_id,),
            ),
        ),
        questions=(
            ObjectAdmission(
                object_ref=action.question_ref,
                branch_id=branch_id,
                admitted_event_sha256=action.basis_tail_event_sha256,
            ),
        ),
        actions=(
            ActionSnapshot(
                action_ref=action.object_ref,
                branch_id=branch_id,
                kind=action.kind,
                lifecycle=ActionLifecycle.AUTHORIZED,
                proposed_event_sha256=proposed.event_sha256,
                decided_event_sha256=authorized.event_sha256,
            ),
        ),
    )
    original_request = original.compilation_request
    graph_scope = ProtocolScope.model_validate(
        {
            **original_request.protocol.graph_scope.model_dump(mode="python"),
            "graph_snapshot_sha256": state.snapshot_sha256,
        }
    )
    request = _f9_enriched_grouped_fixture(
        graph_scope=graph_scope,
        protocol_authored_at=original_request.protocol.authored_at,
    )
    request = ProtocolCompilationRequest.model_validate(request.request.model_dump(mode="python"))
    result = compile_protocol(request)
    assert result.work_order is not None
    work_order = result.work_order
    node = next(item for item in work_order.nodes if item.scientific_replicate_count == 1)
    binding = ScientificActionProtocolBinding(
        action=action,
        action_proposed_event=proposed,
        action_authorized_event=authorized,
        authorized_graph_snapshot_sha256=state.snapshot_sha256,
        compilation_request=request,
        compilation_result=result,
        compilation_receipt=result.receipt,
        work_order=work_order,
        work_order_node=node,
        replicate_slot=_slot(work_order, node),
        bound_at=request.protocol.authored_at,
    )
    write = ProtocolCompilationWrite.from_contract(
        quest_id=action.quest_id,
        action_sha256=action.object_sha256,
        request=request,
        result=result,
        registered_at=request.protocol.authored_at + timedelta(seconds=1),
    )
    engine = sqlite_observation_engine()
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"),
            {"quest": action.quest_id},
        )
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": action.object_sha256},
        )
        register_protocol_compilation(session, write)
        session.commit()
    projection = ControllerRecoveryProjection(
        quest_id=action.quest_id,
        action_sha256=action.object_sha256,
        scientific_slot_id=None,
        audited_stream_version=state.stream_version,
        audited_tail_event_sha256=state.tail_event_sha256,
        audited_snapshot_sha256=state.snapshot_sha256,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.ACCEPTED,
        scientific_execution_authorization_registered=False,
        execution_terminal_observed=False,
        validation_committed=False,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )
    plan = plan_recovery_tick(projection)
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "3" * 32,
        quest_id=action.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:execution-authorization-source",
        source_sha256=_sha("execution-authorization-source-launch"),
    )
    source = PostgreSQLScientificExecutionAuthorizationSource(
        kernel_store=_Kernel(
            ResearchReplayAudit(
                quest_id=action.quest_id,
                scope_binding=request.protocol.graph_scope.scope_binding,
                events=(proposed, authorized),
                state=state,
                verified_snapshot_sha256s=(state.snapshot_sha256, state.snapshot_sha256),
            )
        ),
        sessions=lambda: _transaction(engine),
    )

    loaded = source.verify_execution_authorization_source(
        wakeup=wakeup,
        projection=projection,
        plan=plan,
        binding=binding,
        expected_compilation_sha256=write.compilation_sha256,
        observed_at=write.registered_at + timedelta(seconds=1),
    )

    assert loaded == write
    with pytest.raises(ExecutionAuthorizationServiceError, match="Kernel audit differs"):
        source.verify_execution_authorization_source(
            wakeup=wakeup,
            projection=projection.model_copy(update={"audited_snapshot_sha256": "f" * 64}),
            plan=plan,
            binding=binding,
            expected_compilation_sha256=write.compilation_sha256,
            observed_at=write.registered_at + timedelta(seconds=1),
        )
