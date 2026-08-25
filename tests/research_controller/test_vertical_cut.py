from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    CommittedObservationAdmission,
    ObservationAdmissionDisposition,
    ScientificActionProtocolBinding,
    ScientificObservationOutcome,
    issue_scientific_execution_authorization,
)
from aletheia.protocols.compiler import (
    ProtocolCompilationRequest,
    compile_protocol,
    verify_compilation,
)
from aletheia.protocols.schemas import (
    ProtocolBlockerCode,
    ProtocolCompilationResult,
    ProtocolIR,
)
from aletheia.protocols.world_models import (
    HypothesisLifecycle,
    HypothesisVersionV2,
    PredictionVersionV2,
    WorldModelSnapshotV2,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
    HypothesisPredictionAssessment,
    PredictionFit,
    ScientificObservationProjection,
    derive_continuation_v2,
)
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
    ResearchControllerService,
)
from aletheia.research_kernel.commands import (
    ResearchCommandProposal,
    authorize_research_proposal,
    verify_research_command_authorization,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationKey,
    ResearchAuthorizationRole,
    ed25519_key_id,
    ed25519_public_key_hex,
)
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    BranchLifecycle,
    ResearchStateGraph,
    reduce_event,
    replay,
)
from aletheia.research_kernel.schemas import (
    ActivateCommittedPayload,
    ActivateDirective,
    ActionAuthorizedPayload,
    ActionKind,
    ActionProposedPayload,
    CharterActivatedPayload,
    EvidenceKind,
    EvidenceRef,
    EventType,
    ForkCommittedPayload,
    ForkDirective,
    ObservationIncorporatedPayload,
    RefineCommittedPayload,
    RefineDirective,
    ResearchActionProposal,
    TransitionDecision,
    canonical_sha256,
)

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "protocols",
    _TESTS / "execution",
    _TESTS / "observations",
    _TESTS / "research_kernel",
):
    sys.path.insert(0, str(_fixture_dir))

from fixtures import ProtocolFixture, fixture_by_name  # noqa: E402
import test_runtime_contracts as runtime_fixture_support  # noqa: E402
import test_scientific_bridge as bridge_fixture_support  # noqa: E402
from test_commands import (  # noqa: E402
    _AT as COMMAND_AT,
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
)
from test_reducer import (  # noqa: E402
    Scenario,
    _admit_problem_and_question,
    _charter as kernel_charter,
    _propose as propose_kernel_action,
)
from test_scientific_bridge import (  # noqa: E402
    _bridge_case,
    _commit_admission,
    _issue_admission_decision,
    _validated_receipt,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _f9_enriched_grouped_fixture(
    *,
    graph_scope=None,
    hypothesis_labels: tuple[str, ...] = ("a", "b"),
    protocol_authored_at=None,
) -> ProtocolFixture:
    """Build an accepted, exactly graph-scoped F9-v2 model over the grouped DAG."""

    base = fixture_by_name("grouped_regression")
    protocol = base.request.protocol
    graph_scope = graph_scope or protocol.graph_scope
    graph_scope_sha256 = graph_scope.graph_scope_sha256

    objective = type(protocol.objective).model_validate(
        {
            **protocol.objective.model_dump(mode="python"),
            "graph_scope_sha256": graph_scope_sha256,
        }
    )
    design_space = type(protocol.design_space).model_validate(
        {
            **protocol.design_space.model_dump(mode="python"),
            "graph_scope_sha256": graph_scope_sha256,
        }
    )
    method = type(protocol.method).model_validate(
        {
            **protocol.method.model_dump(mode="python"),
            "graph_scope_sha256": graph_scope_sha256,
        }
    )
    observables = tuple(
        type(item).model_validate(
            {
                **item.model_dump(mode="python"),
                "graph_scope_sha256": graph_scope_sha256,
            }
        )
        for item in protocol.observables
    )
    observable_hashes = {
        old.observable_sha256: new.observable_sha256
        for old, new in zip(protocol.observables, observables, strict=True)
    }
    analysis_plan = type(protocol.analysis_plan).model_validate(
        {
            **protocol.analysis_plan.model_dump(mode="python"),
            "primary_endpoint_sha256s": tuple(
                observable_hashes.get(item, item)
                for item in protocol.analysis_plan.primary_endpoint_sha256s
            ),
            "secondary_endpoint_sha256s": tuple(
                observable_hashes.get(item, item)
                for item in protocol.analysis_plan.secondary_endpoint_sha256s
            ),
        }
    )
    epistemic_payload = protocol.epistemic_contract.model_dump(mode="python")
    epistemic_payload["graph_scope_sha256"] = graph_scope_sha256
    if "observable_spec_sha256s" in epistemic_payload:
        epistemic_payload["observable_spec_sha256s"] = tuple(
            observable_hashes.get(item, item)
            for item in epistemic_payload["observable_spec_sha256s"]
        )
    epistemic_contract = type(protocol.epistemic_contract).model_validate(epistemic_payload)
    claim_contract = type(protocol.claim_contract).model_validate(
        {
            **protocol.claim_contract.model_dump(mode="python"),
            "graph_scope_sha256": graph_scope_sha256,
        }
    )
    controls = tuple(
        type(item).model_validate(
            {
                **item.model_dump(mode="python"),
                "observable_spec_sha256s": tuple(
                    observable_hashes.get(value, value) for value in item.observable_spec_sha256s
                ),
            }
        )
        for item in protocol.controls
    )

    hypotheses = tuple(
        sorted(
            (
                HypothesisVersionV2(
                    hypothesis_id=f"hyp_{_digest(f'vertical-hypothesis:{label}')[:32]}",
                    version=1,
                    graph_scope_sha256=graph_scope_sha256,
                    lifecycle=HypothesisLifecycle.ACTIVE,
                    statement=statement,
                    explanatory_model=f"Closed local simulator model {label}.",
                    rationale_sha256=_digest(f"vertical-hypothesis-rationale:{label}"),
                    semantic_delta="Initial local vertical-cut hypothesis.",
                    authored_by_principal_id=protocol.authored_by_principal_id,
                    authored_at=protocol.authored_at,
                )
                for label in hypothesis_labels
                for statement in (f"The grouped contrast follows mechanism {label.upper()}.",)
            ),
            key=lambda item: (item.hypothesis_id, item.version, item.hypothesis_sha256),
        )
    )
    observable = observables[0]
    predictions = tuple(
        sorted(
            (
                PredictionVersionV2(
                    prediction_id=(
                        f"pred_{_digest(f'vertical-prediction:{hypothesis.hypothesis_id}')[:32]}"
                    ),
                    version=1,
                    graph_scope_sha256=graph_scope_sha256,
                    hypothesis_sha256=hypothesis.hypothesis_sha256,
                    observable_spec_sha256=observable.observable_sha256,
                    measurement_protocol_sha256=method.method_contract_sha256,
                    outcome_space_sha256=analysis_plan.outcome_space_sha256,
                    predicted_outcome_sha256=_digest(
                        f"vertical-predicted-outcome:{hypothesis.hypothesis_id}"
                    ),
                    discriminates_from_hypothesis_sha256s=tuple(
                        sorted(item.hypothesis_sha256 for item in hypotheses if item != hypothesis)
                    ),
                    semantic_delta="Initial frozen discriminating prediction.",
                    authored_by_principal_id=protocol.authored_by_principal_id,
                    authored_at=protocol.authored_at,
                )
                for hypothesis in hypotheses
            ),
            key=lambda item: (item.prediction_id, item.version, item.prediction_sha256),
        )
    )
    world_model = WorldModelSnapshotV2(
        graph_scope=graph_scope,
        world_model_id=f"wm_{_digest('vertical-world-model:' + ':'.join(hypothesis_labels))[:32]}",
        version=1,
        hypotheses=hypotheses,
        predictions=predictions,
        causal_structure_sha256=_digest("vertical-causal-structure"),
        model_limitations=("This is a closed simulator acceptance fixture, not a discovery.",),
        semantic_delta="Initial graph-scoped F9-v2 acceptance snapshot.",
        authored_by_principal_id=protocol.authored_by_principal_id,
        authored_at=protocol.authored_at,
    )

    contract_hashes = {
        protocol.design_space.design_space_sha256: design_space.design_space_sha256,
        protocol.method.method_sha256: method.method_sha256,
        protocol.epistemic_contract.contract_sha256: epistemic_contract.contract_sha256,
        canonical_sha256(protocol.analysis_plan): canonical_sha256(analysis_plan),
        **{
            canonical_sha256(old): canonical_sha256(new)
            for old, new in zip(protocol.controls, controls, strict=True)
        },
    }
    steps = tuple(
        type(step).model_validate(
            {
                **step.model_dump(mode="python"),
                "contract_bindings": tuple(
                    sorted(
                        (
                            binding.model_copy(
                                update={
                                    "contract_sha256": contract_hashes.get(
                                        binding.contract_sha256,
                                        binding.contract_sha256,
                                    )
                                }
                            )
                            for binding in step.contract_bindings
                        ),
                        key=lambda item: f"{item.contract_kind.value}:{item.contract_sha256}",
                    )
                ),
            }
        )
        for step in protocol.steps
    )
    enriched_protocol = ProtocolIR.model_validate(
        {
            **protocol.model_dump(mode="python"),
            "graph_scope": graph_scope,
            "objective": objective,
            "design_space": design_space,
            "method": method,
            "epistemic_contract": epistemic_contract,
            "world_model": world_model,
            "observables": observables,
            "observable_output_bindings": tuple(
                item.model_copy(
                    update={
                        "observable_spec_sha256": observable_hashes[item.observable_spec_sha256]
                    }
                )
                for item in protocol.observable_output_bindings
            ),
            "controls": controls,
            "analysis_plan": analysis_plan,
            "steps": steps,
            "claim_contract": claim_contract,
            "authored_at": protocol_authored_at or protocol.authored_at,
        }
    )
    request = ProtocolCompilationRequest(
        protocol=enriched_protocol,
        capability_catalog=base.request.capability_catalog,
        resource_catalog=base.request.resource_catalog,
        compiler_implementation_sha256=base.request.compiler_implementation_sha256,
    )
    return ProtocolFixture(
        name=base.name,
        request=request,
        expected_dependency_steps=base.expected_dependency_steps,
    )


class _DurableVerticalProjection(BaseModel):
    """Serialized receipt/ledger projection shared by two controller process instances."""

    model_config = ConfigDict(extra="forbid")

    kernel_state: ResearchStateGraph
    source_action: ResearchActionProposal
    compilation_request: ProtocolCompilationRequest
    compilation_result: ProtocolCompilationResult
    scientific_execution_authorization_sha256: str
    validation_receipt_sha256: str
    committed_admission: CommittedObservationAdmission
    observation: ScientificObservationProjection
    assessments: tuple[HypothesisPredictionAssessment, ...]
    continuation: ContinuationReceipt | None = None
    followup_action: ResearchActionProposal | None = None
    followup_command_proposal: ResearchCommandProposal | None = None

    @model_validator(mode="after")
    def _receipt_chain_is_exact(self) -> "_DurableVerticalProjection":
        protocol = self.compilation_request.protocol
        world_model = protocol.world_model
        admission_decision = self.committed_admission.message.decision.message
        validation = admission_decision.committed_validation_receipt.message.receipt.message
        authorization = validation.raw_run.scientific_authorization
        action_binding = authorization.message.action_protocol_binding
        applied = tuple(
            item
            for item in self.kernel_state.actions
            if item.action_ref.object_id == self.source_action.action_id
        )
        if (
            not self.compilation_result.report.accepted
            or self.compilation_result.work_order is None
            or self.compilation_result.receipt.protocol_sha256 != protocol.protocol_sha256
            or action_binding.compilation_request != self.compilation_request
            or action_binding.compilation_result != self.compilation_result
            or authorization.authorization_sha256 != self.scientific_execution_authorization_sha256
            or action_binding.action != self.source_action
            or self.kernel_state.quest_id != self.source_action.quest_id
            or world_model is None
            or self.observation.scientific_slot_id != admission_decision.scientific_slot_id
            or self.observation.committed_admission_sha256
            != self.committed_admission.committed_admission_sha256
            or self.observation.scientific_observation_sha256
            != admission_decision.admitted_observation_sha256
            or self.observation.outcome != validation.outcome
            or len(applied) != 1
            or applied[0].lifecycle is not ActionLifecycle.APPLIED
            or applied[0].observation_evidence_ref != self.observation_evidence_ref
        ):
            raise ValueError("vertical-cut durable receipt chain is not exact")
        return self

    @property
    def observation_evidence_ref(self):
        return ObservationIncorporatedPayload(
            branch_id=self.compilation_request.protocol.graph_scope.branch_id,
            action_id=self.source_action.action_id,
            scientific_slot_id=self.observation.scientific_slot_id,
            committed_admission_sha256=self.observation.committed_admission_sha256,
            scientific_observation_sha256=self.observation.scientific_observation_sha256,
            outcome=self.observation.outcome.value,
            source_world_model_sha256=(
                self.compilation_request.protocol.world_model.world_model_sha256
            ),
        ).evidence_ref


class _DurableVerticalLedger:
    def __init__(self, projection: _DurableVerticalProjection) -> None:
        self.projection = projection

    def load(self, wakeup: ControllerWakeup) -> ControllerRecoveryProjection:
        state = self.projection.kernel_state
        action = next(
            item
            for item in state.actions
            if item.action_ref.object_id == self.projection.source_action.action_id
        )
        if wakeup.quest_id != state.quest_id:
            raise ValueError("vertical controller wakeup belongs to another Quest")
        return ControllerRecoveryProjection(
            quest_id=wakeup.quest_id,
            action_sha256=action.action_ref.object_sha256,
            scientific_slot_id=self.projection.observation.scientific_slot_id,
            audited_stream_version=state.stream_version,
            audited_tail_event_sha256=state.tail_event_sha256,
            audited_snapshot_sha256=state.snapshot_sha256,
            action_authorized=action.lifecycle
            in {ActionLifecycle.AUTHORIZED, ActionLifecycle.APPLIED},
            compilation_disposition=CompilationDisposition.ACCEPTED,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
            admission_committed=True,
            observation_incorporated=action.lifecycle is ActionLifecycle.APPLIED,
            continuation_committed=self.projection.continuation is not None,
            blocker_codes=(),
        )

    def serialize(self) -> str:
        return self.projection.model_dump_json()

    @classmethod
    def restore(cls, value: str) -> "_DurableVerticalLedger":
        return cls(_DurableVerticalProjection.model_validate_json(value))


class _VerticalStepExecutor:
    def __init__(self, ledger: _DurableVerticalLedger) -> None:
        self._ledger = ledger

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan,
    ) -> ControllerStepReceipt:
        assert projection.projection_sha256 == plan.projection_sha256
        durable_projection = self._ledger.projection
        if plan.step is ControllerStep.DERIVE_CONTINUATION:
            world_model = durable_projection.compilation_request.protocol.world_model
            assert world_model is not None
            continuation = derive_continuation_v2(
                world_model=world_model,
                observation=durable_projection.observation,
                assessments=durable_projection.assessments,
            )
            self._ledger.projection = durable_projection.model_copy(
                update={"continuation": continuation}
            )
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(continuation.receipt_sha256,),
                blocker_codes=(),
            )

        if plan.step is not ControllerStep.PROPOSE_FOLLOWUP:
            raise AssertionError(f"unexpected vertical controller step: {plan.step.value}")
        continuation = durable_projection.continuation
        assert continuation is not None
        state = durable_projection.kernel_state
        world_model = durable_projection.compilation_request.protocol.world_model
        assert world_model is not None
        assert state.tail_event_sha256 is not None and state.charter_ref is not None
        continuation_evidence = EvidenceRef(
            kind=EvidenceKind.INCONCLUSIVE,
            object_sha256=world_model.world_model_sha256,
        )
        followup = ResearchActionProposal(
            action_id="action:vertical-hypothesis-fork",
            quest_id=durable_projection.source_action.quest_id,
            charter_ref=state.charter_ref,
            question_ref=durable_projection.source_action.question_ref,
            basis_tail_event_sha256=state.tail_event_sha256,
            kind=continuation.proposed_action_kind,
            epistemic_purpose=(
                "Fork the all-model miss into a new discriminating hypothesis family."
            ),
            candidate_outcomes=("discriminating_negative", "discriminating_support"),
            evidence_refs=tuple(
                sorted(
                    (continuation_evidence, durable_projection.observation_evidence_ref),
                    key=lambda item: (
                        item.kind.value,
                        item.object_sha256,
                        item.object_id or "",
                    ),
                )
            ),
            cost_receipt_sha256=_digest("vertical-followup-cost"),
            risk_receipt_sha256=_digest("vertical-followup-risk"),
            requested_authority_class="transition",
            proposed_by_principal_id="controller:vertical-proposal",
            proposed_at=(
                durable_projection.committed_admission.message.committed_at + timedelta(seconds=10)
            ),
        )
        command_proposal = ResearchCommandProposal(
            quest_id=followup.quest_id,
            scope_binding=(
                durable_projection.compilation_request.protocol.graph_scope.scope_binding
            ),
            expected_stream_version=state.stream_version,
            expected_tail_event_sha256=state.tail_event_sha256,
            event_type=EventType.ACTION_PROPOSED,
            payload=ActionProposedPayload(
                action_ref=followup.object_ref,
                branch_id=durable_projection.compilation_request.protocol.graph_scope.branch_id,
            ),
            proposed_by_principal_id=followup.proposed_by_principal_id,
            proposed_at=followup.proposed_at,
        )
        self._ledger.projection = durable_projection.model_copy(
            update={
                "followup_action": followup,
                "followup_command_proposal": command_proposal,
            }
        )
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=ControllerStepDisposition.AWAITING_AUTHORITY,
            result_artifact_sha256s=tuple(
                sorted((followup.object_sha256, command_proposal.proposal_sha256))
            ),
            blocker_codes=(),
        )


class _LegacyDriverSentinel(ModuleType):
    def __init__(self) -> None:
        super().__init__("aletheia.scheduler.driver")
        self.accesses: list[str] = []

    def __getattr__(self, name: str):
        self.accesses.append(name)
        raise AssertionError(f"vertical controller touched legacy driver symbol: {name}")


def test_restart_safe_measurement_redesign_negative_fork_vertical_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_fixture = _f9_enriched_grouped_fixture()

    original_fixture_by_name = runtime_fixture_support.fixture_by_name
    active_runtime_fixtures: dict[str, ProtocolFixture] = {}

    def _runtime_fixture(name: str):
        if name in active_runtime_fixtures:
            return active_runtime_fixtures[name]
        return original_fixture_by_name(name)

    monkeypatch.setattr(runtime_fixture_support, "fixture_by_name", _runtime_fixture)
    legacy_driver = _LegacyDriverSentinel()
    monkeypatch.setitem(sys.modules, "aletheia.scheduler.driver", legacy_driver)
    initial_scope = base_fixture.request.protocol.graph_scope
    kernel = Scenario(
        quest_id=initial_scope.scope_binding.quest_id,
        root_branch_id=initial_scope.branch_id,
    )
    kernel.charter = kernel_charter(kernel.quest_id)
    kernel.add_object(kernel.charter)
    kernel.commit(
        EventType.CHARTER_ACTIVATED,
        CharterActivatedPayload(
            charter_ref=kernel.charter.object_ref,
            root_branch_id=kernel.root_branch_id,
        ),
    )
    _, _, question = _admit_problem_and_question(kernel)
    child_authorization_private_key = b"\x25" * 32
    child_authorization_public_key = ed25519_public_key_hex(child_authorization_private_key)
    child_authorization_key = ResearchAuthorizationKey(
        key_id=ed25519_key_id(child_authorization_public_key),
        principal_id="agent:vertical-child-authorizer",
        role=ResearchAuthorizationRole.ORDINARY,
        public_key_ed25519_hex=child_authorization_public_key,
        valid_from=COMMAND_AT - timedelta(days=1),
        expires_at=COMMAND_AT + timedelta(days=10),
    )
    trust_root, authorization_policy = command_authority(
        quest_id=kernel.quest_id,
        extra_keys=(child_authorization_key,),
    )
    ordinary_key = next(
        item
        for item in authorization_policy.keys
        if item.role is ResearchAuthorizationRole.ORDINARY
        and item.key_id != child_authorization_key.key_id
    )

    def commit_signed(
        case: Scenario,
        *,
        event_type: EventType,
        payload,
        idempotency_key: str,
        source_event_key: str,
        proposed_by_principal_id: str,
        proposed_at,
        authorized_at,
        committed_at,
        admitted_object=None,
        resolved_action: ResearchActionProposal | None = None,
        authorization_key=None,
        authorization_private_key: bytes | None = None,
    ):
        assert case.charter is not None
        signing_key = authorization_key or ordinary_key
        signing_private_key = (
            authorization_private_key or COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY]
        )
        proposal = ResearchCommandProposal(
            quest_id=case.quest_id,
            scope_binding=initial_scope.scope_binding,
            expected_stream_version=case.state.stream_version,
            expected_tail_event_sha256=case.state.tail_event_sha256,
            event_type=event_type,
            payload=payload,
            proposed_by_principal_id=proposed_by_principal_id,
            proposed_at=proposed_at,
        )
        command = authorize_research_proposal(
            proposal,
            idempotency_key=idempotency_key,
            source_event_key=source_event_key,
            authorization_policy=authorization_policy,
            trust_root=trust_root,
            authorization_key_id=signing_key.key_id,
            private_key=signing_private_key,
            authorized_at=authorized_at,
        )
        assert (
            verify_research_command_authorization(
                command,
                authorization_policy=authorization_policy,
                trust_root=trust_root,
                committed_at=committed_at,
                active_charter=case.charter,
                admitted_object=admitted_object,
                resolved_action=resolved_action,
            )
            is ResearchAuthorizationRole.ORDINARY
        )
        event = command.to_event(
            sequence=case.state.stream_version + 1,
            parent_event_sha256=case.state.tail_event_sha256,
            committed_at=committed_at,
        )
        case.state = reduce_event(case.state, event, case.objects)
        case.events.append(event)
        return proposal, command, event

    def set_runtime_phase(phase_at) -> None:
        """Shift the real qualification/bridge fixtures without backdating later runs."""

        monkeypatch.setattr(runtime_fixture_support, "NOW", phase_at)
        monkeypatch.setattr(bridge_fixture_support, "NOW", phase_at)
        signed_defaults = dict(runtime_fixture_support._signed_case.__kwdefaults__ or {})
        signed_defaults.update(
            {
                "quote_at": phase_at,
                "grant_at": phase_at + timedelta(minutes=1),
                "grant_expires_at": phase_at + timedelta(minutes=10),
            }
        )
        monkeypatch.setattr(
            runtime_fixture_support._signed_case,
            "__kwdefaults__",
            signed_defaults,
        )

    def bridge_for_authorized_action(
        *,
        fixture: ProtocolFixture,
        action: ResearchActionProposal,
        proposed_event,
        authorized_event,
    ):
        """Issue a real SEA over the exact events committed in this one Kernel ledger."""

        active_runtime_fixtures["grouped_regression"] = fixture
        base = _bridge_case()
        request = fixture.request
        result = compile_protocol(request)
        assert base.qualification.bundle.compilation_request == request
        assert base.qualification.bundle.compilation_result == result
        template_binding = base.binding
        binding = ScientificActionProtocolBinding(
            action=action,
            action_proposed_event=proposed_event,
            action_authorized_event=authorized_event,
            authorized_graph_snapshot_sha256=(request.protocol.graph_scope.graph_snapshot_sha256),
            compilation_request=request,
            compilation_result=result,
            compilation_receipt=result.receipt,
            work_order=template_binding.work_order,
            work_order_node=template_binding.work_order_node,
            replicate_slot=template_binding.replicate_slot,
            bound_at=request.protocol.authored_at,
        )
        template = base.authorization.message
        authorization = issue_scientific_execution_authorization(
            action_protocol_binding=binding,
            qualification_bundle=base.qualification.bundle,
            qualification_grant=base.qualification.grant,
            validator_manifest_sha256=template.validator_manifest_sha256,
            observation_validation_policy_sha256=(template.observation_validation_policy_sha256),
            admission_policy=template.admission_policy,
            scientific_observation_artifact_binding=(
                template.scientific_observation_artifact_binding
            ),
            qualification_authority=base.qualification_authority,
            action_authority=base.action_authority,
            qualification_custody=base.qualification_custody,
            execution_authority_pin=base.execution_pin,
            validator_authority_pin=base.validator_pin,
            admission_authority_pin=base.admission_pin,
            private_key=bridge_fixture_support.EXECUTION_AUTHORITY_PRIVATE_KEY,
            authorized_at=template.authorized_at,
            expires_at=template.expires_at,
            observation_admission_deadline=template.observation_admission_deadline,
        )
        return replace(base, binding=binding, authorization=authorization)

    # The measurement gap is discovered while attempting one real typed action on the root
    # branch.  Its proposed/authorized events become part of this same authoritative ledger;
    # the compiler receipt below is therefore scoped to the graph after that authorization.
    assert kernel.state.tail_event_sha256 is not None
    blocker_action = ResearchActionProposal(
        action_id="action:vertical-p1-measurement-gap",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=kernel.state.tail_event_sha256,
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="Attempt the preregistered measurement before its observable repair.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        evidence_refs=(
            EvidenceRef(
                kind=EvidenceKind.INCONCLUSIVE,
                object_sha256=_digest("vertical-p1-measurement-gap"),
            ),
        ),
        cost_receipt_sha256=_digest("vertical-p1-cost"),
        risk_receipt_sha256=_digest("vertical-p1-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id=ordinary_key.principal_id,
        proposed_at=COMMAND_AT + timedelta(milliseconds=100),
    )
    kernel.add_object(blocker_action)
    _, blocker_proposal_command, blocker_proposal_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=blocker_action.object_ref,
            branch_id=kernel.root_branch_id,
        ),
        idempotency_key="vertical:p1-blocker-proposal",
        source_event_key="vertical:p1-measurement-gap",
        proposed_by_principal_id=blocker_action.proposed_by_principal_id,
        proposed_at=blocker_action.proposed_at,
        authorized_at=COMMAND_AT + timedelta(milliseconds=200),
        committed_at=COMMAND_AT + timedelta(milliseconds=300),
        admitted_object=blocker_action,
        resolved_action=blocker_action,
    )
    _, blocker_authorization_command, blocker_authorization_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=blocker_action.action_id,
            branch_id=kernel.root_branch_id,
        ),
        idempotency_key="vertical:p1-blocker-authorization",
        source_event_key="vertical:p1-blocker-proposal",
        proposed_by_principal_id="controller:vertical-p1-authorization",
        proposed_at=COMMAND_AT + timedelta(milliseconds=400),
        authorized_at=COMMAND_AT + timedelta(milliseconds=500),
        committed_at=COMMAND_AT + timedelta(milliseconds=600),
        resolved_action=blocker_action,
        authorization_key=child_authorization_key,
        authorization_private_key=child_authorization_private_key,
    )
    blocker_action_snapshot = next(
        item for item in kernel.state.actions if item.action_ref == blocker_action.object_ref
    )
    assert blocker_action_snapshot.lifecycle is ActionLifecycle.AUTHORIZED
    assert blocker_proposal_command.authorization_receipt_sha256
    assert blocker_authorization_command.authorization_receipt_sha256
    assert blocker_authorization_event.sequence == blocker_proposal_event.sequence + 1

    blocker_scope = type(initial_scope).model_validate(
        {
            **initial_scope.model_dump(mode="python"),
            "question_ref": question.object_ref,
            "graph_snapshot_sha256": kernel.state.snapshot_sha256,
        }
    )
    blocker_fixture = _f9_enriched_grouped_fixture(
        graph_scope=blocker_scope,
        protocol_authored_at=COMMAND_AT + timedelta(seconds=1),
    )
    blocked_payload = blocker_fixture.request.model_dump(mode="python")
    blocked_payload["protocol"]["observable_output_bindings"] = ()
    blocked_request = ProtocolCompilationRequest.model_validate(blocked_payload)
    blocked = compile_protocol(blocked_request)
    assert blocked.work_order is None
    assert not blocked.report.accepted
    assert ProtocolBlockerCode.OBSERVABLE_MISSING in {item.code for item in blocked.report.blockers}
    assert blocked_request.protocol.graph_scope.graph_snapshot_sha256 == (
        kernel.state.snapshot_sha256
    )
    verify_compilation(blocked_request, blocked)
    assert blocked.receipt.work_order_sha256 is None
    assert blocked.receipt.typecheck_report_sha256 == blocked.report.report_sha256
    assert blocked.receipt.blocker_sha256s == tuple(
        sorted(item.blocker_sha256 for item in blocked.report.blockers)
    )

    # The compiler blocker is not resolved by silently swapping requests: it causes a typed,
    # signed REFINE proposal whose transition command is its authorization and atomic commit.
    assert kernel.state.tail_event_sha256 is not None
    blocker_evidence = EvidenceRef(
        kind=EvidenceKind.INCONCLUSIVE,
        object_sha256=blocked.receipt.receipt_sha256,
    )
    redesign_action = ResearchActionProposal(
        action_id="action:vertical-observable-redesign",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=kernel.state.tail_event_sha256,
        kind=ActionKind.REFINE,
        epistemic_purpose="Repair the compiler-proven missing observable binding.",
        candidate_outcomes=("accepted_redesign", "blocked"),
        evidence_refs=(blocker_evidence,),
        cost_receipt_sha256=_digest("vertical-redesign-cost"),
        risk_receipt_sha256=_digest("vertical-redesign-risk"),
        requested_authority_class="transition",
        proposed_by_principal_id="controller:vertical-redesign",
        proposed_at=COMMAND_AT + timedelta(seconds=3),
    )
    kernel.add_object(redesign_action)
    _, redesign_proposal_command, redesign_proposal_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=redesign_action.object_ref,
            branch_id=kernel.root_branch_id,
        ),
        idempotency_key="vertical:redesign-proposal",
        source_event_key="vertical:compiler-blocker",
        proposed_by_principal_id=redesign_action.proposed_by_principal_id,
        proposed_at=redesign_action.proposed_at,
        authorized_at=COMMAND_AT + timedelta(seconds=4),
        committed_at=COMMAND_AT + timedelta(seconds=5),
        admitted_object=redesign_action,
        resolved_action=redesign_action,
    )
    redesign_branch_id = f"rbr_{_digest('vertical-redesign-branch')[:32]}"
    redesign_decision = TransitionDecision(
        transition_id="transition:vertical-observable-redesign",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        source_graph_sha256=kernel.state.snapshot_sha256,
        selected_action_ref=redesign_action.object_ref,
        directive=RefineDirective(
            source_branch_id=kernel.root_branch_id,
            child_branch_id=redesign_branch_id,
        ),
        evidence_refs=(blocker_evidence,),
        evidence_event_sha256s=(redesign_proposal_event.event_sha256,),
        budget_receipt_sha256=redesign_action.cost_receipt_sha256,
        risk_receipt_sha256=redesign_action.risk_receipt_sha256,
        policy_receipt_sha256=_digest("vertical-redesign-policy"),
        reason_codes=("observable_missing",),
        rationale="The typed compiler blocker requires a branch-scoped observable redesign.",
        decided_by_principal_id=ordinary_key.principal_id,
        decided_at=COMMAND_AT + timedelta(seconds=6),
    )
    _, redesign_commit_command, redesign_commit_event = commit_signed(
        kernel,
        event_type=EventType.REFINE_COMMITTED,
        payload=RefineCommittedPayload(decision=redesign_decision),
        idempotency_key="vertical:redesign-commit",
        source_event_key="vertical:redesign-proposal",
        proposed_by_principal_id="controller:vertical-redesign",
        proposed_at=COMMAND_AT + timedelta(seconds=6),
        authorized_at=COMMAND_AT + timedelta(seconds=7),
        committed_at=COMMAND_AT + timedelta(seconds=8),
        resolved_action=redesign_action,
    )
    redesign_snapshot = next(
        item for item in kernel.state.actions if item.action_ref == redesign_action.object_ref
    )
    redesign_branches = {item.branch_id: item for item in kernel.state.branches}
    assert redesign_snapshot.lifecycle is ActionLifecycle.APPLIED
    assert redesign_branches[kernel.root_branch_id].lifecycle is (BranchLifecycle.SUPERSEDED)
    assert redesign_branches[redesign_branch_id].lifecycle is BranchLifecycle.ACTIVE
    assert redesign_proposal_command.authorization_receipt_sha256
    assert redesign_commit_command.authorization_receipt_sha256
    assert redesign_commit_event.event_type is EventType.REFINE_COMMITTED
    assert replay(kernel.events, kernel.objects) == kernel.state

    assert kernel.state.tail_event_sha256 is not None
    source_action = ResearchActionProposal(
        action_id="action:vertical-p2-discrimination",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=kernel.state.tail_event_sha256,
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="Execute the observable redesign against the frozen p2 model.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        evidence_refs=(blocker_evidence,),
        cost_receipt_sha256=_digest("vertical-p2-cost"),
        risk_receipt_sha256=_digest("vertical-p2-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id=ordinary_key.principal_id,
        proposed_at=COMMAND_AT + timedelta(seconds=9),
    )
    kernel.add_object(source_action)
    _, source_proposal_command, source_proposal_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=source_action.object_ref,
            branch_id=redesign_branch_id,
        ),
        idempotency_key="vertical:p2-proposal",
        source_event_key="vertical:redesign-commit",
        proposed_by_principal_id=source_action.proposed_by_principal_id,
        proposed_at=source_action.proposed_at,
        authorized_at=COMMAND_AT + timedelta(seconds=10),
        committed_at=COMMAND_AT + timedelta(seconds=11),
        admitted_object=source_action,
        resolved_action=source_action,
    )
    _, source_authorization_command, source_authorization_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=source_action.action_id,
            branch_id=redesign_branch_id,
        ),
        idempotency_key="vertical:p2-authorization",
        source_event_key="vertical:p2-proposal",
        proposed_by_principal_id="controller:vertical-p2-authorization",
        proposed_at=COMMAND_AT + timedelta(seconds=12),
        authorized_at=COMMAND_AT + timedelta(seconds=13),
        committed_at=COMMAND_AT + timedelta(seconds=14),
        resolved_action=source_action,
        authorization_key=child_authorization_key,
        authorization_private_key=child_authorization_private_key,
    )
    source_snapshot = next(
        item for item in kernel.state.actions if item.action_ref == source_action.object_ref
    )
    assert source_snapshot.lifecycle is ActionLifecycle.AUTHORIZED

    p2_scope = type(initial_scope).model_validate(
        {
            **initial_scope.model_dump(mode="python"),
            "branch_id": redesign_branch_id,
            "question_ref": question.object_ref,
            "graph_snapshot_sha256": kernel.state.snapshot_sha256,
        }
    )
    enriched_fixture = _f9_enriched_grouped_fixture(
        graph_scope=p2_scope,
        protocol_authored_at=COMMAND_AT + timedelta(seconds=15),
    )
    accepted_request = enriched_fixture.request
    accepted = compile_protocol(accepted_request)
    assert accepted.report.accepted
    assert accepted.work_order is not None
    assert accepted_request.protocol.protocol_sha256 != blocked_request.protocol.protocol_sha256
    assert accepted_request.protocol.graph_scope.branch_id == redesign_branch_id
    assert accepted_request.protocol.graph_scope.graph_snapshot_sha256 == (
        kernel.state.snapshot_sha256
    )
    verify_compilation(accepted_request, accepted)

    set_runtime_phase(COMMAND_AT)
    bridge = bridge_for_authorized_action(
        fixture=enriched_fixture,
        action=source_action,
        proposed_event=source_proposal_event,
        authorized_event=source_authorization_event,
    )
    assert bridge.binding.action == source_action
    assert bridge.binding.action_proposed_event == source_proposal_event
    assert bridge.binding.action_authorized_event == source_authorization_event
    assert bridge.binding.compilation_request == accepted_request
    assert bridge.binding.compilation_result == accepted
    validation = _validated_receipt(bridge, outcome_bin_id="outcome.negative")
    assert validation.message.raw_run.accepted_terminal_submission.disposition == (
        "process_succeeded"
    )
    assert validation.message.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
    assert validation.message.outcome is ScientificObservationOutcome.NEGATIVE
    decision, committed_validation = _issue_admission_decision(
        bridge,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    committed_admission = _commit_admission(bridge, decision)
    assert decision.message.committed_validation_receipt == committed_validation
    assert decision.message.maximum_admissions_per_scientific_slot == 1
    assert committed_admission.message.scientific_slot_was_empty is True

    protocol = accepted_request.protocol
    world_model = protocol.world_model
    assert world_model is not None
    observation_payload = ObservationIncorporatedPayload(
        branch_id=redesign_branch_id,
        action_id=source_action.action_id,
        scientific_slot_id=decision.message.scientific_slot_id,
        committed_admission_sha256=committed_admission.committed_admission_sha256,
        scientific_observation_sha256=decision.message.admitted_observation_sha256,
        outcome=validation.message.outcome.value,
        source_world_model_sha256=world_model.world_model_sha256,
    )
    observation_proposed_at = committed_admission.message.committed_at + timedelta(seconds=1)
    _, observation_command, observation_event = commit_signed(
        kernel,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=observation_payload,
        idempotency_key="vertical:observation-incorporation",
        source_event_key="vertical:committed-admission",
        proposed_by_principal_id="bridge:independent-observation-admission",
        proposed_at=observation_proposed_at,
        authorized_at=observation_proposed_at + timedelta(seconds=1),
        committed_at=observation_proposed_at + timedelta(seconds=2),
        resolved_action=source_action,
    )
    assert observation_command.authorization_receipt_sha256
    assert committed_admission.message.committed_at < observation_event.committed_at
    applied = next(
        item for item in kernel.state.actions if item.action_ref == source_action.object_ref
    )
    assert applied.lifecycle is ActionLifecycle.APPLIED
    assert applied.observation_evidence_ref == observation_payload.evidence_ref
    assert replay(kernel.events, kernel.objects) == kernel.state

    duplicate = deepcopy(kernel)
    second_action = propose_kernel_action(
        duplicate,
        question,
        redesign_branch_id,
        ActionKind.DISCRIMINATE,
        "duplicate-vertical-slot",
    )
    duplicate.commit(
        EventType.ACTION_AUTHORIZED,
        ActionAuthorizedPayload(
            action_id=second_action.action_id,
            branch_id=redesign_branch_id,
        ),
    )
    with pytest.raises(ValueError, match="scientific slot already has"):
        duplicate.commit(
            EventType.OBSERVATION_INCORPORATED,
            observation_payload.model_copy(update={"action_id": second_action.action_id}),
        )

    predictions = tuple(sorted(world_model.predictions, key=lambda item: item.hypothesis_sha256))
    observation = ScientificObservationProjection(
        scientific_slot_id=observation_payload.scientific_slot_id,
        committed_admission_sha256=observation_payload.committed_admission_sha256,
        scientific_observation_sha256=observation_payload.scientific_observation_sha256,
        source_world_model_sha256=observation_payload.source_world_model_sha256,
        outcome=ScientificObservationOutcome(observation_payload.outcome),
        observable_spec_sha256=predictions[0].observable_spec_sha256,
        measurement_protocol_sha256=predictions[0].measurement_protocol_sha256,
        outcome_space_sha256=predictions[0].outcome_space_sha256,
        observed_outcome_sha256=_digest("vertical-observed-all-model-miss"),
    )
    assessments = tuple(
        HypothesisPredictionAssessment(
            hypothesis_sha256=prediction.hypothesis_sha256,
            prediction_sha256=prediction.prediction_sha256,
            prediction_fit=PredictionFit.OUT_OF_SUPPORT,
            fit_rule_sha256=_digest("vertical-fit-rule"),
            assessment_artifact_sha256=_digest(
                f"vertical-assessment:{prediction.prediction_sha256}"
            ),
        )
        for prediction in predictions
    )
    durable = _DurableVerticalLedger(
        _DurableVerticalProjection(
            kernel_state=kernel.state,
            source_action=source_action,
            compilation_request=accepted_request,
            compilation_result=accepted,
            scientific_execution_authorization_sha256=(bridge.authorization.authorization_sha256),
            validation_receipt_sha256=validation.receipt_sha256,
            committed_admission=committed_admission,
            observation=observation,
            assessments=assessments,
        )
    )
    wakeup = ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id=kernel.quest_id,
        source_kind=ControllerWakeupKind.KERNEL_OUTBOX,
        source_key=observation_event.event_id,
        source_sha256=observation_event.event_sha256,
        source_stream_version=observation_event.sequence,
    )
    before_restart = ResearchControllerService(
        recovery=durable,
        executor=_VerticalStepExecutor(durable),
    ).tick(wakeup)
    assert before_restart.plan.step is ControllerStep.DERIVE_CONTINUATION
    assert before_restart.step_receipt.independent_observation_admission_committed is False
    assert durable.projection.continuation is not None
    assert (
        durable.projection.continuation.disposition
        is ContinuationDisposition.HYPOTHESIS_SET_FORK_REQUIRED
    )
    assert durable.projection.continuation.proposed_action_kind is ActionKind.FORK

    serialized_ledger = durable.serialize()
    restarted_ledger = _DurableVerticalLedger.restore(serialized_ledger)
    after_restart = ResearchControllerService(
        recovery=restarted_ledger,
        executor=_VerticalStepExecutor(restarted_ledger),
    ).tick(wakeup)
    assert after_restart.plan.step is ControllerStep.PROPOSE_FOLLOWUP
    assert after_restart.step_receipt.disposition is (ControllerStepDisposition.AWAITING_AUTHORITY)
    followup = restarted_ledger.projection.followup_action
    followup_proposal = restarted_ledger.projection.followup_command_proposal
    assert followup is not None and followup_proposal is not None
    assert followup.kind is ActionKind.FORK
    assert observation_payload.evidence_ref in followup.evidence_refs
    assert (
        EvidenceRef(
            kind=EvidenceKind.INCONCLUSIVE,
            object_sha256=world_model.world_model_sha256,
        )
        in followup.evidence_refs
    )
    continuation = restarted_ledger.projection.continuation
    assert continuation is not None
    assert (
        continuation.world_model_snapshot_sha256
        == observation.source_world_model_sha256
        == observation_payload.source_world_model_sha256
        == world_model.world_model_sha256
    )
    assert after_restart.step_receipt.direct_kernel_mutation_used is False
    assert after_restart.step_receipt.legacy_optimize_used is False

    followup_command = authorize_research_proposal(
        followup_proposal,
        idempotency_key="vertical:hypothesis-fork-followup",
        source_event_key="vertical:continuation-receipt",
        authorization_policy=authorization_policy,
        trust_root=trust_root,
        authorization_key_id=ordinary_key.key_id,
        private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
        authorized_at=followup.proposed_at + timedelta(seconds=1),
    )
    followup_committed_at = followup.proposed_at + timedelta(seconds=2)
    assert (
        verify_research_command_authorization(
            followup_command,
            authorization_policy=authorization_policy,
            trust_root=trust_root,
            committed_at=followup_committed_at,
            active_charter=kernel.charter,
            admitted_object=followup,
            resolved_action=followup,
        )
        is ResearchAuthorizationRole.ORDINARY
    )
    followup_event = followup_command.to_event(
        sequence=restarted_ledger.projection.kernel_state.stream_version + 1,
        parent_event_sha256=restarted_ledger.projection.kernel_state.tail_event_sha256,
        committed_at=followup_committed_at,
    )
    followup_objects = {**kernel.objects, followup.object_sha256: followup}
    followed_state = reduce_event(
        restarted_ledger.projection.kernel_state,
        followup_event,
        followup_objects,
    )
    followed = next(
        item for item in followed_state.actions if item.action_ref == followup.object_ref
    )
    assert followed.lifecycle is ActionLifecycle.PROPOSED

    kernel.add_object(followup)
    kernel.state = followed_state
    kernel.events.append(followup_event)
    fork_child_ids = tuple(
        sorted(
            (
                f"rbr_{_digest('vertical-hypothesis-child-a')[:32]}",
                f"rbr_{_digest('vertical-hypothesis-child-b')[:32]}",
            )
        )
    )
    fork_decision = TransitionDecision(
        transition_id="transition:vertical-hypothesis-fork",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        source_graph_sha256=kernel.state.snapshot_sha256,
        selected_action_ref=followup.object_ref,
        directive=ForkDirective(
            source_branch_id=protocol.graph_scope.branch_id,
            child_branch_ids=fork_child_ids,
        ),
        evidence_refs=followup.evidence_refs,
        evidence_event_sha256s=(observation_event.event_sha256,),
        budget_receipt_sha256=followup.cost_receipt_sha256,
        risk_receipt_sha256=followup.risk_receipt_sha256,
        policy_receipt_sha256=_digest("vertical-fork-policy"),
        reason_codes=("all_active_hypotheses_out_of_support",),
        rationale=(
            "The admitted observation misses every active hypothesis in the exact "
            f"F9-v2 snapshot {world_model.world_model_sha256}."
        ),
        decided_by_principal_id=ordinary_key.principal_id,
        decided_at=followup_event.committed_at + timedelta(seconds=1),
    )
    _, fork_command, fork_event = commit_signed(
        kernel,
        event_type=EventType.FORK_COMMITTED,
        payload=ForkCommittedPayload(decision=fork_decision),
        idempotency_key="vertical:hypothesis-fork-commit",
        source_event_key="vertical:hypothesis-fork-followup",
        proposed_by_principal_id="controller:vertical-fork-decision",
        proposed_at=fork_decision.decided_at,
        authorized_at=fork_decision.decided_at + timedelta(seconds=1),
        committed_at=fork_decision.decided_at + timedelta(seconds=2),
        resolved_action=followup,
    )
    forked_action = next(
        item for item in kernel.state.actions if item.action_ref == followup.object_ref
    )
    forked_branches = {item.branch_id: item for item in kernel.state.branches}
    assert fork_command.authorization_receipt_sha256
    assert fork_event.event_type is EventType.FORK_COMMITTED
    assert forked_action.lifecycle is ActionLifecycle.APPLIED
    assert all(
        forked_branches[branch_id].lifecycle is BranchLifecycle.ADMITTED
        for branch_id in fork_child_ids
    )

    selected_child_id = fork_child_ids[0]
    assert kernel.state.tail_event_sha256 is not None
    activate_action = ResearchActionProposal(
        action_id="action:vertical-activate-selected-child",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=kernel.state.tail_event_sha256,
        kind=ActionKind.ACTIVATE,
        epistemic_purpose="Activate the selected hypothesis-family branch for discrimination.",
        candidate_outcomes=("activated", "not_activated"),
        evidence_refs=followup.evidence_refs,
        cost_receipt_sha256=_digest("vertical-activate-cost"),
        risk_receipt_sha256=_digest("vertical-activate-risk"),
        requested_authority_class="transition",
        proposed_by_principal_id="controller:vertical-child-selection",
        proposed_at=fork_event.committed_at + timedelta(seconds=1),
    )
    kernel.add_object(activate_action)
    _, activate_proposal_command, activate_proposal_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=activate_action.object_ref,
            branch_id=selected_child_id,
        ),
        idempotency_key="vertical:activate-child-proposal",
        source_event_key="vertical:hypothesis-fork-commit",
        proposed_by_principal_id=activate_action.proposed_by_principal_id,
        proposed_at=activate_action.proposed_at,
        authorized_at=activate_action.proposed_at + timedelta(seconds=1),
        committed_at=activate_action.proposed_at + timedelta(seconds=2),
        admitted_object=activate_action,
        resolved_action=activate_action,
    )
    activate_decision = TransitionDecision(
        transition_id="transition:vertical-activate-selected-child",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        source_graph_sha256=kernel.state.snapshot_sha256,
        selected_action_ref=activate_action.object_ref,
        directive=ActivateDirective(branch_id=selected_child_id),
        evidence_refs=followup.evidence_refs,
        evidence_event_sha256s=tuple(
            sorted((fork_event.event_sha256, activate_proposal_event.event_sha256))
        ),
        budget_receipt_sha256=activate_action.cost_receipt_sha256,
        risk_receipt_sha256=activate_action.risk_receipt_sha256,
        policy_receipt_sha256=_digest("vertical-activate-policy"),
        reason_codes=("selected_discriminating_child",),
        rationale="Activate one admitted child before proposing work on that branch.",
        decided_by_principal_id=ordinary_key.principal_id,
        decided_at=activate_proposal_event.committed_at + timedelta(seconds=1),
    )
    _, activate_command, activate_event = commit_signed(
        kernel,
        event_type=EventType.ACTIVATE_COMMITTED,
        payload=ActivateCommittedPayload(decision=activate_decision),
        idempotency_key="vertical:activate-child-commit",
        source_event_key="vertical:activate-child-proposal",
        proposed_by_principal_id="controller:vertical-child-selection",
        proposed_at=activate_decision.decided_at,
        authorized_at=activate_decision.decided_at + timedelta(seconds=1),
        committed_at=activate_decision.decided_at + timedelta(seconds=2),
        resolved_action=activate_action,
    )
    activated_action = next(
        item for item in kernel.state.actions if item.action_ref == activate_action.object_ref
    )
    activated_branch = next(
        item for item in kernel.state.branches if item.branch_id == selected_child_id
    )
    assert activate_proposal_command.authorization_receipt_sha256
    assert activate_command.authorization_receipt_sha256
    assert activate_event.event_type is EventType.ACTIVATE_COMMITTED
    assert activated_action.lifecycle is ActionLifecycle.APPLIED
    assert activated_branch.lifecycle is BranchLifecycle.ACTIVE

    child_evidence = followup.evidence_refs
    assert kernel.state.tail_event_sha256 is not None
    child_followup = ResearchActionProposal(
        action_id="action:vertical-child-p3-discrimination",
        quest_id=kernel.quest_id,
        charter_ref=kernel.charter.object_ref,
        question_ref=question.object_ref,
        basis_tail_event_sha256=kernel.state.tail_event_sha256,
        kind=ActionKind.DISCRIMINATE,
        epistemic_purpose="Discriminate the compiler-accepted child-scoped p3 model.",
        candidate_outcomes=("inconclusive", "negative", "positive"),
        evidence_refs=child_evidence,
        cost_receipt_sha256=_digest("vertical-child-discriminate-cost"),
        risk_receipt_sha256=_digest("vertical-child-discriminate-risk"),
        requested_authority_class="analysis",
        proposed_by_principal_id=ordinary_key.principal_id,
        proposed_at=activate_event.committed_at + timedelta(seconds=1),
    )
    kernel.add_object(child_followup)
    _, child_proposal_command, child_proposal_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_PROPOSED,
        payload=ActionProposedPayload(
            action_ref=child_followup.object_ref,
            branch_id=selected_child_id,
        ),
        idempotency_key="vertical:child-p3-proposal",
        source_event_key="vertical:activate-child-commit",
        proposed_by_principal_id=child_followup.proposed_by_principal_id,
        proposed_at=child_followup.proposed_at,
        authorized_at=child_followup.proposed_at + timedelta(seconds=1),
        committed_at=child_followup.proposed_at + timedelta(seconds=2),
        admitted_object=child_followup,
        resolved_action=child_followup,
    )
    _, child_authorization_command, child_authorization_event = commit_signed(
        kernel,
        event_type=EventType.ACTION_AUTHORIZED,
        payload=ActionAuthorizedPayload(
            action_id=child_followup.action_id,
            branch_id=selected_child_id,
        ),
        idempotency_key="vertical:child-p3-authorization",
        source_event_key="vertical:child-p3-proposal",
        proposed_by_principal_id="controller:vertical-child-discrimination",
        proposed_at=child_proposal_event.committed_at + timedelta(seconds=1),
        authorized_at=child_proposal_event.committed_at + timedelta(seconds=2),
        committed_at=child_proposal_event.committed_at + timedelta(seconds=3),
        resolved_action=child_followup,
        authorization_key=child_authorization_key,
        authorization_private_key=child_authorization_private_key,
    )
    child_snapshot = next(
        item for item in kernel.state.actions if item.action_ref == child_followup.object_ref
    )
    assert child_followup.kind is ActionKind.DISCRIMINATE
    assert child_snapshot.branch_id == selected_child_id
    assert child_snapshot.lifecycle is ActionLifecycle.AUTHORIZED
    assert child_proposal_command.authorization_receipt_sha256
    assert child_authorization_command.authorization_receipt_sha256
    assert child_proposal_event.event_type is EventType.ACTION_PROPOSED
    assert child_authorization_event.event_type is EventType.ACTION_AUTHORIZED
    assert replay(kernel.events, kernel.objects) == kernel.state

    second_runtime_phase = child_authorization_event.committed_at + timedelta(seconds=1)
    child_scope = type(protocol.graph_scope).model_validate(
        {
            **protocol.graph_scope.model_dump(mode="python"),
            "branch_id": selected_child_id,
            "question_ref": question.object_ref,
            "graph_snapshot_sha256": kernel.state.snapshot_sha256,
        }
    )
    p3_fixture = _f9_enriched_grouped_fixture(
        graph_scope=child_scope,
        hypothesis_labels=("a", "b", "p3"),
        protocol_authored_at=second_runtime_phase,
    )
    p3_request = p3_fixture.request
    p3_compilation = compile_protocol(p3_request)
    assert p3_compilation.report.accepted
    assert p3_compilation.work_order is not None
    verify_compilation(p3_request, p3_compilation)
    p3_world_model = p3_request.protocol.world_model
    assert p3_world_model is not None
    assert len(p3_world_model.hypotheses) == 3
    assert len(p3_world_model.predictions) == 3
    p3_targets = tuple(sorted(item.hypothesis_sha256 for item in p3_world_model.hypotheses))
    p3_world_model.assert_hypothesis_discrimination(p3_targets)
    assert p3_request.protocol.graph_scope.branch_id == selected_child_id
    assert p3_request.protocol.graph_scope.graph_snapshot_sha256 == kernel.state.snapshot_sha256

    set_runtime_phase(second_runtime_phase)
    second_bridge = bridge_for_authorized_action(
        fixture=p3_fixture,
        action=child_followup,
        proposed_event=child_proposal_event,
        authorized_event=child_authorization_event,
    )
    second_binding = second_bridge.authorization.message.action_protocol_binding
    assert second_binding.action == child_followup
    assert second_binding.action_proposed_event == child_proposal_event
    assert second_binding.action_authorized_event == child_authorization_event
    assert second_binding.compilation_request == p3_request
    assert second_binding.compilation_result == p3_compilation

    second_validation = _validated_receipt(
        second_bridge,
        outcome_bin_id="outcome.positive",
    )
    assert second_validation.message.raw_run.accepted_terminal_submission.disposition == (
        "process_succeeded"
    )
    assert second_validation.message.disposition is (
        BridgeValidationDisposition.VALIDATED_CONFIRMATION
    )
    assert second_validation.message.outcome is ScientificObservationOutcome.POSITIVE
    second_decision, second_committed_validation = _issue_admission_decision(
        second_bridge,
        receipt=second_validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    second_committed_admission = _commit_admission(second_bridge, second_decision)
    assert second_decision.message.committed_validation_receipt == second_committed_validation
    assert second_committed_admission.message.scientific_slot_was_empty is True

    first_intent = bridge.authorization.message.qualification_bundle.intent
    second_intent = second_bridge.authorization.message.qualification_bundle.intent
    assert second_intent.execution_id != first_intent.execution_id
    assert (
        second_intent.infrastructure_attempt.infrastructure_attempt_id
        != first_intent.infrastructure_attempt.infrastructure_attempt_id
    )
    assert second_decision.message.scientific_slot_id != decision.message.scientific_slot_id
    assert (
        second_bridge.authorization.authorization_sha256
        != bridge.authorization.authorization_sha256
    )
    assert (
        second_committed_admission.committed_admission_sha256
        != committed_admission.committed_admission_sha256
    )

    second_observation_payload = ObservationIncorporatedPayload(
        branch_id=selected_child_id,
        action_id=child_followup.action_id,
        scientific_slot_id=second_decision.message.scientific_slot_id,
        committed_admission_sha256=(second_committed_admission.committed_admission_sha256),
        scientific_observation_sha256=(second_decision.message.admitted_observation_sha256),
        outcome=second_validation.message.outcome.value,
        source_world_model_sha256=p3_world_model.world_model_sha256,
    )
    second_observation_proposed_at = second_committed_admission.message.committed_at + timedelta(
        seconds=1
    )
    _, second_observation_command, second_observation_event = commit_signed(
        kernel,
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=second_observation_payload,
        idempotency_key="vertical:child-p3-observation-incorporation",
        source_event_key="vertical:child-p3-committed-admission",
        proposed_by_principal_id="bridge:independent-child-observation-admission",
        proposed_at=second_observation_proposed_at,
        authorized_at=second_observation_proposed_at + timedelta(seconds=1),
        committed_at=second_observation_proposed_at + timedelta(seconds=2),
        resolved_action=child_followup,
    )
    applied_child = next(
        item for item in kernel.state.actions if item.action_ref == child_followup.object_ref
    )
    assert second_observation_command.authorization_receipt_sha256
    assert second_committed_admission.message.committed_at < second_observation_event.committed_at
    assert applied_child.lifecycle is ActionLifecycle.APPLIED
    assert applied_child.observation_evidence_ref == second_observation_payload.evidence_ref
    assert replay(kernel.events, kernel.objects) == kernel.state

    scientific_slots = {
        item.object_id
        for item in kernel.state.evidence_refs
        if item.object_id is not None and item.object_id.startswith("sos_")
    }
    assert scientific_slots == {
        observation_payload.scientific_slot_id,
        second_observation_payload.scientific_slot_id,
    }

    controller_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(
            (Path(__file__).resolve().parents[2] / "aletheia" / "research_controller").glob("*.py")
        )
    )
    assert "aletheia.scheduler.driver" not in controller_sources
    assert "ExperimentDriver" not in controller_sources
    assert "_optimize(" not in controller_sources
    assert legacy_driver.accesses == []
