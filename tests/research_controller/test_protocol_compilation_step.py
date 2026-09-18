from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aletheia.observations.persistence import ResearchProtocolCompilationRecord
from aletheia.observations.store import (
    get_protocol_compilation_by_action,
    get_protocol_compilation_by_protocol_version,
)
from aletheia.protocols.base import ProtocolScope
from aletheia.protocols.compiler import ProtocolCompilationRequest
from aletheia.protocols.data_registration import DatasetRoundSplitPolicyV1
from aletheia.protocols.schemas import (
    CallerParameterBinding,
    DesignSpaceVersion,
    ProtocolActionCategory,
    ProtocolCompilationResult,
    ProtocolIR,
    caller_parameter_manifest_sha256,
)
from aletheia.protocols.world_models import (
    BeliefStateVersionV2,
    BeliefUpdateBasis,
    HypothesisBeliefV2,
    HypothesisLifecycle,
    WorldModelSnapshotV2,
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
    AuthorizedProtocolCompilationContext,
    DurableProtocolCompilationService,
    PreparedProtocolCompilation,
    ProtocolCompilationPolicyPin,
    ProtocolCompilationStepAdapter,
    ProtocolCompilationStepError,
    ProtocolCompilationUnavailable,
    RoundSplitBindingPolicyV1,
    RoundSplitTemplateBindingV1,
)
from aletheia.research_controller.service import ControllerStepDisposition
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)
from aletheia.research_controller.world_model_revision import (
    REVISION_UPDATE_RULE_SHA256,
    AdmittedObservationBinding,
    revise_world_model_v2,
)
from aletheia.research_kernel.schemas import (
    ActionAuthorizedPayload,
    ActionKind,
    EventType,
    canonical_json_bytes,
)

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
from test_reducer import _propose  # noqa: E402
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


_OBSERVATION_RECEIPT = "a" * 64
_CONTINUATION_RECEIPT = "b" * 64


def _prior_world_model(protocol: ProtocolIR) -> WorldModelSnapshotV2:
    """The F9 fixture registers its world model with no belief state; a
    revision needs a prior to move, so a version-1 uniform prior is attached
    (schema-legal: exact coverage, exact scope stamp, sums to one)."""
    world_model = protocol.world_model
    assert world_model is not None and world_model.belief_state is None
    share = 1.0 / len(world_model.hypotheses)
    belief = BeliefStateVersionV2(
        belief_id="blf_" + _sha("prior-belief")[:32],
        version=1,
        graph_scope_sha256=protocol.graph_scope.graph_scope_sha256,
        hypothesis_beliefs=tuple(
            sorted(
                (
                    HypothesisBeliefV2(hypothesis_sha256=item.hypothesis_sha256, probability=share)
                    for item in world_model.hypotheses
                ),
                key=lambda item: item.hypothesis_sha256,
            )
        ),
        update_basis=BeliefUpdateBasis.PRIOR,
        source_observation_receipt_sha256=None,
        update_rule_sha256=REVISION_UPDATE_RULE_SHA256,
        authored_by_principal_id=world_model.authored_by_principal_id,
        authored_at=world_model.authored_at,
    )
    return world_model.model_copy(update={"belief_state": belief})


def _embedding(
    request: ProtocolCompilationRequest,
    *,
    version: int,
    revision_parent_sha256: str | None,
    world_model: WorldModelSnapshotV2,
    authored_at,
) -> ProtocolCompilationRequest:
    protocol = ProtocolIR.model_validate(
        {
            **request.protocol.model_dump(mode="python"),
            "version": version,
            "revision_parent_sha256": revision_parent_sha256,
            "authored_at": authored_at,
            "world_model": world_model.model_dump(mode="python"),
        }
    )
    return ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
        compiler_implementation_sha256=request.compiler_implementation_sha256,
    )


def _observations(world_model: WorldModelSnapshotV2) -> tuple[AdmittedObservationBinding, ...]:
    return tuple(
        AdmittedObservationBinding(
            hypothesis_sha256=item.hypothesis_sha256,
            holds=True,
            observation_receipt_sha256=_OBSERVATION_RECEIPT,
        )
        for item in world_model.hypotheses
        if item.lifecycle is HypothesisLifecycle.ACTIVE
    )


def _next_authorized(case, question, label):
    action = _propose(
        case,
        question,
        case.root_branch_id,
        ActionKind.DISCRIMINATE,
        label,
    )
    authorized = case.commit(
        EventType.ACTION_AUTHORIZED,
        ActionAuthorizedPayload(action_id=action.action_id, branch_id=case.root_branch_id),
    )
    return action, authorized


def test_v1_protocol_embedding_unbound_v2_world_model_fails_closed() -> None:
    case, question, action, authorized = _authorized_case()
    base = _request(case, question, authorized)
    prior = _prior_world_model(base.protocol)
    child = revise_world_model_v2(
        parent=prior,
        observations=_observations(prior),
        continuation_receipt_sha256=_CONTINUATION_RECEIPT,
        authored_at=prior.authored_at + timedelta(seconds=1),
        principal_id=prior.authored_by_principal_id,
    )
    # strip the basis: schema-legal round trip (PRIOR needs no receipt), so
    # only the compile gate can catch a version-2 snapshot with no bound
    # observation receipt inside a version-1 protocol
    stripped = child.model_copy(
        update={
            "belief_state": child.belief_state.model_copy(
                update={
                    "update_basis": BeliefUpdateBasis.PRIOR,
                    "source_observation_receipt_sha256": None,
                }
            )
        }
    )
    request = _embedding(
        base,
        version=1,
        revision_parent_sha256=None,
        world_model=stripped,
        authored_at=base.protocol.authored_at + timedelta(seconds=2),
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service, _policy_pin = _service(case, action, request, engine)
    projection = _projection(case, action)

    with pytest.raises(ProtocolCompilationStepError, match="unbound belief basis"):
        service.compile_and_register(
            wakeup=_wakeup(case),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord)) == 0
        )


def test_legal_world_model_revision_registers_as_protocol_version_two() -> None:
    case, question, action, authorized = _authorized_case()
    base_first = _request(case, question, authorized)
    prior_world = _prior_world_model(base_first.protocol)
    first = _embedding(
        base_first,
        version=1,
        revision_parent_sha256=None,
        world_model=prior_world,
        authored_at=base_first.protocol.authored_at,
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service_first, _first_policy = _service(case, action, first, engine)
    projection_first = _projection(case, action)
    write1 = service_first.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_first,
        plan=plan_recovery_tick(projection_first),
    )
    assert write1.protocol_version == 1

    second_action, second_authorized = _next_authorized(case, question, "revision-context")
    base_second = _request(case, question, second_authorized)
    # every authorized action commits a new graph snapshot, so the revision
    # must re-bind to the current view rather than carry the parent's stamp
    assert (
        base_second.protocol.graph_scope.graph_snapshot_sha256
        != base_first.protocol.graph_scope.graph_snapshot_sha256
    )
    v2_authored = max(
        first.protocol.authored_at + timedelta(seconds=1),
        base_second.protocol.authored_at,
    )
    child = revise_world_model_v2(
        parent=prior_world,
        observations=_observations(prior_world),
        continuation_receipt_sha256=_CONTINUATION_RECEIPT,
        graph_scope=base_second.protocol.graph_scope,
        authored_at=v2_authored,
        principal_id=base_second.protocol.authored_by_principal_id,
    )
    second = _embedding(
        base_second,
        version=2,
        revision_parent_sha256=write1.protocol_sha256,
        world_model=child,
        authored_at=v2_authored,
    )
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": second_action.object_sha256},
        )
        session.commit()
    service_second, second_policy = _service(case, second_action, second, engine)
    projection_second = _projection(case, second_action)

    write2 = service_second.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_second,
        plan=plan_recovery_tick(projection_second),
    )
    assert write2.protocol_version == 2
    assert write2.revision_parent_sha256 == write1.protocol_sha256
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord)) == 2
        )

    # durable replay of a version-2 row re-runs both gate layers through the
    # existing-row branch without re-invoking the provider
    restarted = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_FailProvider(),
        preparation_verifier=_Verifier(second),
        compilation_policy=second_policy,
        authority_binding=_binding(second_policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: second.protocol.authored_at + timedelta(seconds=10),
    ).compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_second,
        plan=plan_recovery_tick(projection_second),
    )
    assert restarted == write2


@pytest.mark.parametrize("tamper", ("parent", "prediction"))
def test_illegal_world_model_revision_against_registered_parent_rolls_back(tamper) -> None:
    case, question, action, authorized = _authorized_case()
    base_first = _request(case, question, authorized)
    prior_world = _prior_world_model(base_first.protocol)
    first = _embedding(
        base_first,
        version=1,
        revision_parent_sha256=None,
        world_model=prior_world,
        authored_at=base_first.protocol.authored_at,
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service_first, _first_policy = _service(case, action, first, engine)
    projection_first = _projection(case, action)
    write1 = service_first.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_first,
        plan=plan_recovery_tick(projection_first),
    )

    second_action, second_authorized = _next_authorized(case, question, "revision-context")
    base_second = _request(case, question, second_authorized)
    v2_authored = max(
        first.protocol.authored_at + timedelta(seconds=1),
        base_second.protocol.authored_at,
    )
    child = revise_world_model_v2(
        parent=prior_world,
        observations=_observations(prior_world),
        continuation_receipt_sha256=_CONTINUATION_RECEIPT,
        graph_scope=base_second.protocol.graph_scope,
        authored_at=v2_authored,
        principal_id=base_second.protocol.authored_by_principal_id,
    )
    if tamper == "parent":
        child = child.model_copy(update={"revision_parent_sha256": "e" * 64})
    else:
        # swap in the other frozen prediction's outcome hash: schema-legal and
        # compiler-legal, but the sealed member changed against the registry
        # parent
        child = child.model_copy(
            update={
                "predictions": (
                    child.predictions[0].model_copy(
                        update={
                            "predicted_outcome_sha256": child.predictions[
                                1
                            ].predicted_outcome_sha256
                        }
                    ),
                    *child.predictions[1:],
                )
            }
        )
    second = _embedding(
        base_second,
        version=2,
        revision_parent_sha256=write1.protocol_sha256,
        world_model=child,
        authored_at=v2_authored,
    )
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": second_action.object_sha256},
        )
        session.commit()
    service_second, _second_policy = _service(case, second_action, second, engine)
    projection_second = _projection(case, second_action)

    with pytest.raises(ProtocolCompilationStepError, match="illegal against its registered parent"):
        service_second.compile_and_register(
            wakeup=_wakeup(case),
            projection=projection_second,
            plan=plan_recovery_tick(projection_second),
        )
    with Session(engine) as session:
        rows = session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord))
        assert rows == 1
        assert (
            get_protocol_compilation_by_action(
                session,
                quest_id=case.quest_id,
                action_sha256=action.object_sha256,
            )
            == write1
        )


def test_same_identity_world_model_reset_fails_closed() -> None:
    case, question, action, authorized = _authorized_case()
    base_first = _request(case, question, authorized)
    prior_world = _prior_world_model(base_first.protocol)
    first = _embedding(
        base_first,
        version=1,
        revision_parent_sha256=None,
        world_model=prior_world,
        authored_at=base_first.protocol.authored_at,
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service_first, _first_policy = _service(case, action, first, engine)
    projection_first = _projection(case, action)
    write1 = service_first.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_first,
        plan=plan_recovery_tick(projection_first),
    )

    second_action, second_authorized = _next_authorized(case, question, "revision-context")
    base_second = _request(case, question, second_authorized)
    v2_authored = max(
        first.protocol.authored_at + timedelta(seconds=1),
        base_second.protocol.authored_at,
    )
    child = revise_world_model_v2(
        parent=prior_world,
        observations=_observations(prior_world),
        continuation_receipt_sha256=_CONTINUATION_RECEIPT,
        graph_scope=base_second.protocol.graph_scope,
        authored_at=v2_authored,
        principal_id=base_second.protocol.authored_by_principal_id,
    )
    second = _embedding(
        base_second,
        version=2,
        revision_parent_sha256=write1.protocol_sha256,
        world_model=child,
        authored_at=v2_authored,
    )
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": second_action.object_sha256},
        )
        session.commit()
    service_second, _second_policy = _service(case, second_action, second, engine)
    projection_second = _projection(case, second_action)
    write2 = service_second.compile_and_register(
        wakeup=_wakeup(case),
        projection=projection_second,
        plan=plan_recovery_tick(projection_second),
    )
    assert write2.protocol_version == 2

    # a fresh version-1 prior under the SAME world_model_id is a belief
    # rollback dressed as a founding: the gate re-derives the lineage and
    # refuses it even though the snapshot itself is schema-legal
    third_action, third_authorized = _next_authorized(case, question, "reset-context")
    base_third = _request(case, question, third_authorized)
    reset_world = _prior_world_model(base_third.protocol)
    assert reset_world.world_model_id == child.world_model_id
    v3_authored = max(
        second.protocol.authored_at + timedelta(seconds=1),
        base_third.protocol.authored_at,
    )
    third = _embedding(
        base_third,
        version=3,
        revision_parent_sha256=write2.protocol_sha256,
        world_model=reset_world,
        authored_at=v3_authored,
    )
    with Session(engine) as session:
        session.execute(
            text("INSERT INTO research_kernel_objects VALUES (:action)"),
            {"action": third_action.object_sha256},
        )
        session.commit()
    service_third, _third_policy = _service(case, third_action, third, engine)
    projection_third = _projection(case, third_action)

    with pytest.raises(ProtocolCompilationStepError, match="illegal against its registered parent"):
        service_third.compile_and_register(
            wakeup=_wakeup(case),
            projection=projection_third,
            plan=plan_recovery_tick(projection_third),
        )
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ResearchProtocolCompilationRecord)) == 2
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


def _round_split_binding(action_sha256: str) -> RoundSplitBindingPolicyV1:
    split_policy = DatasetRoundSplitPolicyV1(
        holdout_percent=20, salt="arl2-cuprate-diagnostic-2026-09"
    )
    return RoundSplitBindingPolicyV1(
        dataset_content_sha256=_sha("registered-content"),
        split_policy_sha256=split_policy.policy_sha256,
        round_index=1,
        sealed_group_ids_sha256=_sha("round-one-groups"),
        template_bindings=(
            RoundSplitTemplateBindingV1(
                action_sha256=action_sha256,
                bound_batch_group_ids_sha256=_sha("round-one-batch"),
                spent_group_ids_sha256=_sha("round-one-spent"),
                unspent_group_ids_sha256=_sha("round-one-unspent"),
            ),
        ),
    )


def _round_split_parameter_values(binding: RoundSplitBindingPolicyV1) -> dict[str, str]:
    row = binding.template_bindings[0]
    return {
        "dataset_content_sha256": binding.dataset_content_sha256,
        "round_bound_batch_group_ids": row.bound_batch_group_ids_sha256,
        "round_sealed_group_ids": binding.sealed_group_ids_sha256,
        "round_spent_group_ids": row.spent_group_ids_sha256,
        "round_unspent_group_ids": row.unspent_group_ids_sha256,
    }


def _with_round_split_parameters(
    request: ProtocolCompilationRequest, values: dict[str, str]
) -> ProtocolCompilationRequest:
    protocol = request.protocol
    merged = tuple(
        sorted(
            tuple(protocol.caller_parameter_bindings)
            + tuple(
                CallerParameterBinding(parameter_id=parameter_id, value_sha256=value)
                for parameter_id, value in values.items()
            ),
            key=lambda item: item.parameter_id,
        )
    )
    dump = protocol.model_dump(mode="python")
    # The compiler pins caller parameters to exactly the caller-mutable design
    # factors, bound on the scientific executor step alone (typecheck.py:645-662),
    # so the round-split ids enter the protocol as authored covariate factors.
    factors = list(dump["design_space"]["factors"])
    factors.extend(
        {
            "factor_id": parameter_id,
            "factor_kind": "covariate",
            "value_schema": factors[0]["value_schema"],
            "assignment_rule_sha256": _sha(f"assignment:{parameter_id}"),
            "caller_mutable": True,
        }
        for parameter_id in values
    )
    design_space = DesignSpaceVersion.model_validate(
        {**dump["design_space"], "factors": sorted(factors, key=lambda item: item["factor_id"])}
    )
    steps = []
    for step in dump["steps"]:
        contracts = [
            {**binding, "contract_sha256": design_space.design_space_sha256}
            if binding["contract_kind"] == "design_space"
            else binding
            for binding in step["contract_bindings"]
        ]
        parameter_ids = list(step["caller_parameter_ids"])
        if step["role"] == "scientific_executor":
            parameter_ids = sorted(set(parameter_ids) | set(values))
        steps.append(
            {**step, "contract_bindings": contracts, "caller_parameter_ids": parameter_ids}
        )
    protocol = ProtocolIR.model_validate(
        {
            **dump,
            "design_space": design_space.model_dump(mode="python"),
            "steps": steps,
            "caller_parameter_bindings": [item.model_dump(mode="python") for item in merged],
            "caller_parameter_manifest_sha256": caller_parameter_manifest_sha256(merged),
        }
    )
    return ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=request.capability_catalog,
        resource_catalog=request.resource_catalog,
        compiler_implementation_sha256=request.compiler_implementation_sha256,
    )


def test_round_split_bound_protocol_registers() -> None:
    case, question, action, authorized = _authorized_case()
    binding = _round_split_binding(action.object_sha256)
    request = _with_round_split_parameters(
        _request(case, question, authorized), _round_split_parameter_values(binding)
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    policy = _policy(request).model_copy(update={"round_split_binding": binding})
    assert policy.round_split_binding is not None
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)
    write = service.compile_and_register(
        wakeup=_wakeup(case), projection=projection, plan=plan_recovery_tick(projection)
    )
    assert ProtocolCompilationResult.model_validate(write.result_json).report.accepted


@pytest.mark.parametrize(
    ("tamper", "cause"),
    [
        ("drop_parameter", "round split requires exactly one round_bound_batch_group_ids"),
        ("wrong_batch", "disagrees at round_bound_batch_group_ids"),
        ("wrong_dataset", "disagrees at dataset_content_sha256"),
        ("foreign_action", "round split policy has no row for the authorized action"),
    ],
)
def test_round_split_rebinding_fails_closed(tamper: str, cause: str) -> None:
    case, question, action, authorized = _authorized_case()
    row_action = action.object_sha256
    if tamper == "foreign_action":
        row_action = _sha("some-other-action")
    binding = _round_split_binding(row_action)
    values = _round_split_parameter_values(binding)
    if tamper == "drop_parameter":
        values = {
            key: value for key, value in values.items() if key != "round_bound_batch_group_ids"
        }
    elif tamper == "wrong_batch":
        values = {**values, "round_bound_batch_group_ids": _sha("tampered-batch")}
    elif tamper == "wrong_dataset":
        values = {**values, "dataset_content_sha256": _sha("tampered-dataset")}
    request = _with_round_split_parameters(_request(case, question, authorized), values)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    policy = _policy(request).model_copy(update={"round_split_binding": binding})
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)
    with pytest.raises(
        ProtocolCompilationStepError,
        match="round split is not bound to its card-derived partition",
    ) as record:
        service.compile_and_register(
            wakeup=_wakeup(case), projection=projection, plan=plan_recovery_tick(projection)
        )
    assert cause in str(record.value.__cause__)


def test_round_split_policy_rows_must_be_canonical() -> None:
    row = RoundSplitTemplateBindingV1(
        action_sha256=_sha("duplicate-row"),
        bound_batch_group_ids_sha256=_sha("batch"),
        spent_group_ids_sha256=_sha("spent"),
        unspent_group_ids_sha256=_sha("unspent"),
    )
    with pytest.raises(ValidationError, match="unique and canonical"):
        RoundSplitBindingPolicyV1(
            dataset_content_sha256=_sha("registered-content"),
            split_policy_sha256=_sha("split-policy"),
            round_index=1,
            sealed_group_ids_sha256=_sha("round-one-groups"),
            template_bindings=(row, row),
        )


# ---------------------------------------------------------------------------
# merged round-split channel (Q13(b)): the campaign request's bindings ride
# the compile context beside a binding-less policy pin
# ---------------------------------------------------------------------------


def _round_split_binding_round_two(
    action_sha256: str, round_one: RoundSplitBindingPolicyV1
) -> RoundSplitBindingPolicyV1:
    """Round 2 mirrors the commissioned request: it spends round 1's bound
    batch and its sealed set is the round-2 bound batch."""
    round_one_bound = round_one.template_bindings[0].bound_batch_group_ids_sha256
    sealed = _sha("round-two-groups")
    return RoundSplitBindingPolicyV1(
        dataset_content_sha256=round_one.dataset_content_sha256,
        split_policy_sha256=round_one.split_policy_sha256,
        round_index=2,
        sealed_group_ids_sha256=sealed,
        template_bindings=(
            RoundSplitTemplateBindingV1(
                action_sha256=action_sha256,
                bound_batch_group_ids_sha256=sealed,
                spent_group_ids_sha256=round_one_bound,
                unspent_group_ids_sha256=sealed,
            ),
        ),
    )


@pytest.mark.parametrize("which", ["first", "second"])
def test_merged_round_split_protocol_registers(which: str) -> None:
    case, question, action, authorized = _authorized_case()
    first = _round_split_binding(action.object_sha256)
    second = _round_split_binding_round_two(action.object_sha256, first)
    chosen = first if which == "first" else second
    request = _with_round_split_parameters(
        _request(case, question, authorized), _round_split_parameter_values(chosen)
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    policy = _policy(request)
    assert policy.round_split_binding is None
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        campaign_round_split_bindings=(first, second),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)
    write = service.compile_and_register(
        wakeup=_wakeup(case), projection=projection, plan=plan_recovery_tick(projection)
    )
    assert ProtocolCompilationResult.model_validate(write.result_json).report.accepted


@pytest.mark.parametrize(
    "parameter_id",
    ["dataset_content_sha256", "round_spent_group_ids"],
)
def test_merged_round_split_tampering_fails_closed(parameter_id: str) -> None:
    case, question, action, authorized = _authorized_case()
    first = _round_split_binding(action.object_sha256)
    second = _round_split_binding_round_two(action.object_sha256, first)
    values = {
        **_round_split_parameter_values(first),
        parameter_id: _sha("tampered-merged-value"),
    }
    request = _with_round_split_parameters(_request(case, question, authorized), values)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    policy = _policy(request)
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=policy,
        authority_binding=_binding(policy),
        campaign_round_split_bindings=(first, second),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)
    with pytest.raises(
        ProtocolCompilationStepError,
        match="round split is not bound to its card-derived partition",
    ) as record:
        service.compile_and_register(
            wakeup=_wakeup(case), projection=projection, plan=plan_recovery_tick(projection)
        )
    assert "no unique row" in str(record.value.__cause__)


def test_merged_round_split_ambiguous_match_fails_closed() -> None:
    case, question, action, authorized = _authorized_case()
    first = _round_split_binding(action.object_sha256)
    # same five-tuple under both rounds: the gate must count two matches and
    # refuse rather than pick one silently
    second = RoundSplitBindingPolicyV1.model_validate(
        {**first.model_dump(mode="python"), "round_index": 2}
    )
    request = _with_round_split_parameters(
        _request(case, question, authorized), _round_split_parameter_values(first)
    )
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    service = DurableProtocolCompilationService(
        kernel_store=_Kernel(case),
        object_archive=_Archive(case),
        provider=_Provider(request),
        preparation_verifier=_Verifier(request),
        compilation_policy=_policy(request),
        authority_binding=_binding(_policy(request)),
        campaign_round_split_bindings=(first, second),
        sessions=_sessions(engine),
        database_clock=lambda _session: request.protocol.authored_at + timedelta(seconds=3),
    )
    projection = _projection(case, action)
    with pytest.raises(
        ProtocolCompilationStepError,
        match="round split is not bound to its card-derived partition",
    ):
        service.compile_and_register(
            wakeup=_wakeup(case), projection=projection, plan=plan_recovery_tick(projection)
        )


def test_merged_channel_service_rejects_bound_policies_and_foreign_round_sets() -> None:
    case, question, action, authorized = _authorized_case()
    first = _round_split_binding(action.object_sha256)
    second = _round_split_binding_round_two(action.object_sha256, first)
    request = _request(case, question, authorized)
    engine = sqlite_observation_engine()
    _seed(engine, quest_id=case.quest_id, action_sha256=action.object_sha256)
    bound_policy = _policy(request).model_copy(update={"round_split_binding": first})
    with pytest.raises(ValueError, match="binding-less policy"):
        DurableProtocolCompilationService(
            kernel_store=_Kernel(case),
            object_archive=_Archive(case),
            provider=_Provider(request),
            preparation_verifier=_Verifier(request),
            compilation_policy=bound_policy,
            authority_binding=_binding(bound_policy),
            campaign_round_split_bindings=(first, second),
            sessions=_sessions(engine),
        )
    with pytest.raises(ValueError, match="rounds 1 and 2"):
        DurableProtocolCompilationService(
            kernel_store=_Kernel(case),
            object_archive=_Archive(case),
            provider=_Provider(request),
            preparation_verifier=_Verifier(request),
            compilation_policy=_policy(request),
            authority_binding=_binding(_policy(request)),
            campaign_round_split_bindings=(first,),
            sessions=_sessions(engine),
        )


def _merged_context(bindings, *, bound_policy=None) -> AuthorizedProtocolCompilationContext:
    case, question, action, authorized = _authorized_case()
    request = _request(case, question, authorized)
    policy = _policy(request)
    if bound_policy is not None:
        policy = policy.model_copy(update={"round_split_binding": bound_policy})
    projection = _projection(case, action)
    wakeup = _wakeup(case)
    plan = plan_recovery_tick(projection)
    proposed = tuple(
        event for event in case.events if event.event_type is EventType.ACTION_PROPOSED
    )
    assert len(proposed) == 1
    return AuthorizedProtocolCompilationContext(
        wakeup_sha256=wakeup.wakeup_sha256,
        recovery_projection_sha256=projection.projection_sha256,
        plan_sha256=plan.plan_sha256,
        quest_id=case.quest_id,
        expected_stream_version=projection.audited_stream_version,
        expected_tail_event_sha256=projection.audited_tail_event_sha256,
        expected_snapshot_sha256=projection.audited_snapshot_sha256,
        action=action,
        action_proposed_event=proposed[0],
        action_authorized_event=authorized,
        graph_scope=request.protocol.graph_scope,
        compilation_policy=policy,
        latest_event_committed_at=authorized.committed_at,
        campaign_round_split_bindings=bindings,
    )


def test_merged_channel_context_rejects_bound_policy_and_foreign_round_sets() -> None:
    case, question, action, authorized = _authorized_case()
    first = _round_split_binding(action.object_sha256)
    second = _round_split_binding_round_two(action.object_sha256, first)
    assert _merged_context((first, second)).campaign_round_split_bindings is not None
    with pytest.raises(ValidationError, match="binding-less policy"):
        _merged_context((first, second), bound_policy=first)
    with pytest.raises(ValidationError, match="rounds 1 and 2"):
        _merged_context((first,))
    # the same round twice is a foreign round set even though each binding is
    # individually valid; a five-tuple duplicated across BOTH rounds stays
    # legal here and the compile gate refuses it instead
    with pytest.raises(ValidationError, match="rounds 1 and 2"):
        _merged_context((first, first))


def test_merged_channel_field_keeps_old_context_canonical_bytes() -> None:
    context = _merged_context(None)
    # canonical dumps exclude None optionals, so a context authored before the
    # merged channel carries byte-identical canonical bytes afterwards
    assert b"campaign_round_split_bindings" not in canonical_json_bytes(context)
    explicit = context.model_copy(update={"campaign_round_split_bindings": None})
    assert canonical_json_bytes(explicit) == canonical_json_bytes(context)
