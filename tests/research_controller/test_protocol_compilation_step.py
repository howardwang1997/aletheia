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

from aletheia.observations.persistence import ResearchProtocolCompilationRecord
from aletheia.observations.store import (
    get_protocol_compilation_by_action,
    get_protocol_compilation_by_protocol_version,
)
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.compiler import ProtocolCompilationRequest
from aletheia.protocols.schemas import (
    ProtocolActionCategory,
    ProtocolCompilationResult,
    ProtocolIR,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    plan_recovery_tick,
)
from aletheia.research_controller.protocol_compilation_step import (
    ActionProtocolCategoryPolicy,
    DurableProtocolCompilationService,
    PreparedProtocolCompilation,
    ProtocolCompilationPolicyPin,
    ProtocolCompilationStepAdapter,
    ProtocolCompilationStepError,
    ProtocolCompilationUnavailable,
)
from aletheia.research_controller.service import ControllerStepDisposition
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_kernel.schemas import ActionKind

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "observations",
    _TESTS / "protocols",
    _TESTS / "research_controller",
    _TESTS / "research_kernel",
):
    sys.path.insert(0, str(_fixture_dir))

from fixtures import fixture_by_name  # noqa: E402
from persistence_test_support import sqlite_observation_engine  # noqa: E402
from test_action_proposal_context import _audit, _authorized_case  # noqa: E402
from test_vertical_cut import _f9_enriched_grouped_fixture  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _projection(case, action) -> ControllerRecoveryProjection:
    projection = ControllerRecoveryProjection(
        quest_id=case.quest_id,
        action_sha256=action.object_sha256,
        scientific_slot_id=None,
        audited_stream_version=case.state.stream_version,
        audited_tail_event_sha256=case.state.tail_event_sha256,
        audited_snapshot_sha256=case.state.snapshot_sha256,
        action_authorized=True,
        compilation_disposition=CompilationDisposition.MISSING,
        scientific_execution_authorization_registered=False,
        execution_terminal_observed=False,
        validation_committed=False,
        admission_committed=False,
        observation_incorporated=False,
        continuation_committed=False,
        blocker_codes=(),
    )
    assert plan_recovery_tick(projection).step is ControllerStep.COMPILE_PROTOCOL
    return projection


def _wakeup(case) -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "3" * 32,
        quest_id=case.quest_id,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:protocol-compilation",
        source_sha256=_sha("compile-launch"),
    )


def _request(case, question, authorized, *, blocked: bool = False):
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
        protocol_authored_at=max(
            authorized.committed_at + timedelta(seconds=1),
            fixture_by_name("grouped_regression").request.protocol.authored_at,
        ),
    )
    if not blocked:
        return enriched.request
    protocol = ProtocolIR.model_validate(
        {
            **enriched.request.protocol.model_dump(mode="python"),
            "observables": (),
            "observable_output_bindings": (),
        }
    )
    return ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=enriched.request.capability_catalog,
        resource_catalog=enriched.request.resource_catalog,
        compiler_implementation_sha256=enriched.request.compiler_implementation_sha256,
    )


def _policy(request: ProtocolCompilationRequest) -> ProtocolCompilationPolicyPin:
    return ProtocolCompilationPolicyPin(
        capability_catalog_sha256=request.capability_catalog.catalog_sha256,
        resource_catalog_sha256=request.resource_catalog.catalog_sha256,
        compiler_implementation_sha256=request.compiler_implementation_sha256,
        allowed_protocol_author_principal_ids=(request.protocol.authored_by_principal_id,),
        action_category_policies=(
            ActionProtocolCategoryPolicy(
                action_kind=ActionKind.DISCRIMINATE,
                allowed_categories=(ProtocolActionCategory.DETERMINISTIC_ANALYSIS,),
            ),
        ),
        world_model_required_action_kinds=(ActionKind.DISCRIMINATE,),
    )


def _binding(policy: ProtocolCompilationPolicyPin) -> ControllerStepAuthorityBinding:
    return ControllerStepAuthorityBinding(
        role=ControllerStepAuthorityRole.PROTOCOL_COMPILATION,
        principal_id="service:canonical-protocol-compiler",
        key_id=None,
        policy_sha256=policy.policy_sha256,
        service_manifest_sha256=_sha("protocol-compiler-service"),
        externally_deployed=False,
    )


class _Kernel:
    def __init__(self, case) -> None:
        self.audit = _audit(case)

    def audit_in_session(self, _session, quest_id):
        assert quest_id == self.audit.quest_id
        return self.audit


class _Archive:
    def __init__(self, case) -> None:
        self.case = case

    def load_object(self, ref):
        return SimpleNamespace(payload=self.case.objects[ref.object_sha256])


class _Provider:
    def __init__(self, request: ProtocolCompilationRequest) -> None:
        self.request = request
        self.calls = 0

    def prepare_protocol(self, context):
        self.calls += 1
        return PreparedProtocolCompilation(
            context_sha256=context.context_sha256,
            request=self.request,
            prepared_by_principal_id=self.request.protocol.authored_by_principal_id,
            prepared_at=self.request.protocol.authored_at + timedelta(seconds=1),
        )


class _FailProvider:
    def prepare_protocol(self, _context):
        raise AssertionError("exact retry must not reinvoke the protocol provider")


class _Verifier:
    def __init__(self, request: ProtocolCompilationRequest) -> None:
        self.request = request

    def verify_prepared_protocol(self, *, context, prepared):
        frozen = PreparedProtocolCompilation.model_validate(prepared.model_dump(mode="python"))
        if frozen.context_sha256 != context.context_sha256 or frozen.request != self.request:
            raise ProtocolCompilationStepError("test verifier rejected protocol preparation")
        return frozen


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


def _seed(engine, *, quest_id: str, action_sha256: str) -> None:
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_quest_streams VALUES (:quest)"),
            {"quest": quest_id},
        )
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": action_sha256},
        )
        session.commit()


def _service(case, action, request, engine, provider=None):
    policy = _policy(request)
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=provider or _Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    return service, policy


def test_accepted_compilation_is_registered_and_restart_returns_exact_row() -> None:
    case, question, action, authorized = _authorized_case()
    request = _request(case, question, authorized)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    provider = _Provider(request)
    service, policy = _service(case, action, request, engine, provider)
    projection = _projection(case, action)
    plan = plan_recovery_tick(projection)

    first = service.compile_and_register(wakeup=_wakeup(case), projection=projection, plan=plan)
    restarted = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_FailProvider(),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=10),
    ).compile_and_register(wakeup=_wakeup(case), projection=projection, plan=plan)

    assert first == restarted
    assert provider.calls == 1
    result = ProtocolCompilationResult.model_validate(first.result_json)
    assert result.report.accepted
    assert result.work_order is not None
    with Session(engine) as session:
        assert (
            get_protocol_compilation_by_action(
                session,
                quest_id=case.quest_id,
                action_sha256=action.object_sha256,
            )
            == first
        )
        assert (
            get_protocol_compilation_by_protocol_version(
                session,
                quest_id=case.quest_id,
                protocol_id=request.protocol.protocol_id,
                protocol_version=request.protocol.version,
            )
            == first
        )

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.COMPILE_PROTOCOL,
        adapter_code_sha256=_sha("compile-adapter"),
        adapter_config_sha256=_sha("compile-config"),
        authorities=(_binding(policy),),
        prepared_at=request.protocol.authored_at,
    )
    receipt = ProtocolCompilationStepAdapter(
        manifest=manifest,
        compilations=service,
    ).execute(wakeup=_wakeup(case), projection=projection, plan=plan)
    assert receipt.disposition is ControllerStepDisposition.COMPLETED
    assert set(receipt.result_artifact_sha256s) == {
        first.compilation_sha256,
        first.request_sha256,
        first.result_sha256,
        first.receipt_sha256,
    }
    assert not receipt.signed_kernel_command_committed
    assert not receipt.independent_observation_admission_committed


def test_blocked_compilation_is_a_durable_result_not_an_execution_failure() -> None:
    case, question, action, authorized = _authorized_case()
    request = _request(case, question, authorized, blocked=True)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service, _policy_pin = _service(case, action, request, engine)
    projection = _projection(case, action)

    write = service.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection,
        plan=plan_recovery_tick(projection),
    )
    result = ProtocolCompilationResult.model_validate(write.result_json)

    assert not result.report.accepted
    assert result.work_order is None
    assert result.receipt.blocker_sha256s
    assert write.receipt_sha256 == result.receipt.receipt_sha256


@pytest.mark.parametrize("tamper", ("compiler", "category", "world_model"))
def test_policy_or_graph_rebinding_is_rejected_before_registry_write(tamper: str) -> None:
    case, question, action, authorized = _authorized_case()
    request = _request(case, question, authorized)
    if tamper == "compiler":
        request = request.model_copy(update={"compiler_implementation_sha256": "f" * 64})
    elif tamper == "category":
        objective = request.protocol.objective.model_copy(
            update={"action_category": ProtocolActionCategory.EVIDENCE_SYNTHESIS}
        )
        request = request.model_copy(
            update={"protocol": request.protocol.model_copy(update={"objective": objective})}
        )
    else:
        request = request.model_copy(
            update={"protocol": request.protocol.model_copy(update={"world_model": None})}
        )
    trusted = _request(case, question, authorized)
    policy = _policy(trusted)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: trusted.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)

    with pytest.raises(ProtocolCompilationStepError):
        service.compile_and_register(
            wakeup=_wakeup(case),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord)) == 0
        )


def test_revision_without_exact_contiguous_parent_rolls_back() -> None:
    case, question, action, authorized = _authorized_case()
    first = _request(case, question, authorized)
    revised_protocol = ProtocolIR.model_validate(
        {
            **first.protocol.model_dump(mode="python"),
            "version": 2,
            "revision_parent_sha256": "e" * 64,
            "authored_at": first.protocol.authored_at + timedelta(seconds=1),
        }
    )
    request = ProtocolCompilationRequest(
        protocol=revised_protocol,
        capability_catalog=first.capability_catalog,
        resource_catalog=first.resource_catalog,
        compiler_implementation_sha256=first.compiler_implementation_sha256,
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service, _policy_pin = _service(case, action, request, engine)
    projection = _projection(case, action)

    with pytest.raises(ProtocolCompilationStepError, match="revision"):
        service.compile_and_register(
            wakeup=_wakeup(case),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord)) == 0
        )


def test_unavailable_registry_maps_to_typed_blocker() -> None:
    case, _question, action, _authorized = _authorized_case()
    projection = _projection(case, action)
    plan = plan_recovery_tick(projection)
    policy = ProtocolCompilationPolicyPin(
        capability_catalog_sha256="1" * 64,
        resource_catalog_sha256="2" * 64,
        compiler_implementation_sha256="3" * 64,
        allowed_protocol_author_principal_ids=("principal:protocol-author",),
        action_category_policies=(
            ActionProtocolCategoryPolicy(
                action_kind=ActionKind.DISCRIMINATE,
                allowed_categories=(ProtocolActionCategory.DETERMINISTIC_ANALYSIS,),
            ),
        ),
        world_model_required_action_kinds=(),
    )

    class Unavailable:
        authority_binding = _binding(policy)

        def compile_and_register(self, **_kwargs):
            raise ProtocolCompilationUnavailable(("protocol_compilation:no_protocol",))

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.COMPILE_PROTOCOL,
        adapter_code_sha256=_sha("unavailable-adapter"),
        adapter_config_sha256=_sha("unavailable-config"),
        authorities=(_binding(policy),),
        prepared_at=case.events[-1].committed_at,
    )
    receipt = ProtocolCompilationStepAdapter(
        manifest=manifest,
        compilations=Unavailable(),
    ).execute(wakeup=_wakeup(case), projection=projection, plan=plan)

    assert receipt.disposition is ControllerStepDisposition.BLOCKED
    assert receipt.blocker_codes == ("protocol_compilation:no_protocol",)


def test_adapter_wraps_untyped_service_corruption() -> None:
    case, _question, action, _authorized = _authorized_case()
    projection = _projection(case, action)
    policy = ProtocolCompilationPolicyPin(
        capability_catalog_sha256="1" * 64,
        resource_catalog_sha256="2" * 64,
        compiler_implementation_sha256="3" * 64,
        allowed_protocol_author_principal_ids=("principal:protocol-author",),
        action_category_policies=(
            ActionProtocolCategoryPolicy(
                action_kind=ActionKind.DISCRIMINATE,
                allowed_categories=(ProtocolActionCategory.DETERMINISTIC_ANALYSIS,),
            ),
        ),
        world_model_required_action_kinds=(),
    )

    class Corrupt:
        authority_binding = _binding(policy)

        def compile_and_register(self, **_kwargs):
            return object()

    manifest = ControllerStepAdapterManifest(
        step=ControllerStep.COMPILE_PROTOCOL,
        adapter_code_sha256=_sha("corrupt-adapter"),
        adapter_config_sha256=_sha("corrupt-config"),
        authorities=(_binding(policy),),
        prepared_at=case.events[-1].committed_at,
    )
    with pytest.raises(ControllerStepExecutionError):
        ProtocolCompilationStepAdapter(
            manifest=manifest,
            compilations=Corrupt(),
        ).execute(
            wakeup=_wakeup(case),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
