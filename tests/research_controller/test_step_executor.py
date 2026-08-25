from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aletheia.durable_tasks.contracts import RetryPolicy
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerWakeup,
    ControllerWakeupKind,
    ResearchControllerManifest,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAdapterSetManifest,
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
    DedicatedControllerStepExecutor,
)

NOW = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
WORKER_PROCESS_PRINCIPAL = "principal:controller-worker"
ACTIVE_STEPS = (
    ControllerStep.PROPOSE_ACTION,
    ControllerStep.COMPILE_PROTOCOL,
    ControllerStep.PROPOSE_REDESIGN,
    ControllerStep.REGISTER_EXECUTION,
    ControllerStep.COMMIT_VALIDATION,
    ControllerStep.COMMIT_ADMISSION,
    ControllerStep.DERIVE_CONTINUATION,
    ControllerStep.PROPOSE_FOLLOWUP,
)
SIGNED_ROLES = {
    ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
    ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
    ControllerStepAuthorityRole.DATABASE_ATTESTATION,
    ControllerStepAuthorityRole.KERNEL_COMMAND,
}
STEP_ROLES = {
    ControllerStep.PROPOSE_ACTION: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.COMPILE_PROTOCOL: (ControllerStepAuthorityRole.PROTOCOL_COMPILATION,),
    ControllerStep.PROPOSE_REDESIGN: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.REGISTER_EXECUTION: (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    ControllerStep.COMMIT_VALIDATION: (
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    ),
    ControllerStep.COMMIT_ADMISSION: (
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    ),
    ControllerStep.DERIVE_CONTINUATION: (ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,),
    ControllerStep.PROPOSE_FOLLOWUP: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
}


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _wakeup() -> ControllerWakeup:
    return ControllerWakeup(
        registration_id="rcr_" + "1" * 32,
        quest_id="qst_" + "2" * 32,
        source_kind=ControllerWakeupKind.LAUNCH,
        source_key="launch:step-executor",
        source_sha256=_sha("launch-source"),
    )


def _projection(step: ControllerStep) -> ControllerRecoveryProjection:
    values: dict[str, object] = {
        "quest_id": _wakeup().quest_id,
        "action_sha256": _sha("action"),
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
    elif step is ControllerStep.AWAIT_ACTION_AUTHORIZATION:
        values.update(
            action_authorized=False, compilation_disposition=CompilationDisposition.MISSING
        )
    elif step is ControllerStep.COMPILE_PROTOCOL:
        values.update(compilation_disposition=CompilationDisposition.MISSING)
    elif step is ControllerStep.PROPOSE_REDESIGN:
        values.update(compilation_disposition=CompilationDisposition.BLOCKED)
    elif step is ControllerStep.REGISTER_EXECUTION:
        pass
    elif step is ControllerStep.AWAIT_EXECUTION:
        values.update(
            scientific_slot_id="sos_" + "3" * 32,
            scientific_execution_authorization_registered=True,
        )
    elif step is ControllerStep.COMMIT_VALIDATION:
        values.update(
            scientific_slot_id="sos_" + "3" * 32,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
        )
    elif step is ControllerStep.COMMIT_ADMISSION:
        values.update(
            scientific_slot_id="sos_" + "3" * 32,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
        )
    elif step is ControllerStep.DERIVE_CONTINUATION:
        values.update(
            scientific_slot_id="sos_" + "3" * 32,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
            admission_committed=True,
            observation_incorporated=True,
        )
    elif step is ControllerStep.PROPOSE_FOLLOWUP:
        values.update(
            scientific_slot_id="sos_" + "3" * 32,
            scientific_execution_authorization_registered=True,
            execution_terminal_observed=True,
            validation_committed=True,
            admission_committed=True,
            observation_incorporated=True,
            continuation_committed=True,
        )
    elif step is ControllerStep.BLOCKED:
        values.update(
            action_sha256=None,
            action_authorized=False,
            compilation_disposition=CompilationDisposition.MISSING,
            blocker_codes=("operator_policy_blocked",),
        )
    else:  # pragma: no cover - test helper is exhaustive over the closed enum
        raise AssertionError(f"unsupported controller step fixture: {step.value}")
    projection = ControllerRecoveryProjection.model_validate(values)
    assert plan_recovery_tick(projection).step is step
    return projection


def _binding(
    role: ControllerStepAuthorityRole,
    *,
    principal_id: str | None = None,
    key_id: str | None = None,
) -> ControllerStepAuthorityBinding:
    signed = role in SIGNED_ROLES
    return ControllerStepAuthorityBinding(
        role=role,
        principal_id=principal_id or f"authority:{role.value}",
        key_id=(key_id or f"key:{role.value}") if signed else None,
        policy_sha256=_sha(f"policy:{role.value}"),
        service_manifest_sha256=_sha(f"service:{role.value}"),
        externally_deployed=signed,
    )


def _manifest(
    step: ControllerStep,
    *,
    authorities: tuple[ControllerStepAuthorityBinding, ...] | None = None,
) -> ControllerStepAdapterManifest:
    default_roles = STEP_ROLES.get(step, (ControllerStepAuthorityRole.ACTION_PROPOSAL,))
    return ControllerStepAdapterManifest(
        step=step,
        adapter_code_sha256=_sha(f"adapter-code:{step.value}"),
        adapter_config_sha256=_sha(f"adapter-config:{step.value}"),
        authorities=authorities or tuple(_binding(role) for role in default_roles),
        prepared_at=NOW,
    )


class _Adapter:
    def __init__(
        self,
        step: ControllerStep,
        *,
        manifest: ControllerStepAdapterManifest | None = None,
    ) -> None:
        self.manifest = manifest or _manifest(step)
        self.calls: list[tuple[ControllerWakeup, ControllerRecoveryProjection, object]] = []
        self.receipt_override: ControllerStepReceipt | None = None

    def execute(self, *, wakeup, projection, plan) -> ControllerStepReceipt:
        self.calls.append((wakeup, projection, plan))
        if self.receipt_override is not None:
            return self.receipt_override
        proposal = plan.step in {
            ControllerStep.PROPOSE_ACTION,
            ControllerStep.PROPOSE_REDESIGN,
            ControllerStep.PROPOSE_FOLLOWUP,
        }
        admission = plan.step is ControllerStep.COMMIT_ADMISSION
        return ControllerStepReceipt(
            wakeup_sha256=wakeup.wakeup_sha256,
            plan_sha256=plan.plan_sha256,
            disposition=(
                ControllerStepDisposition.AWAITING_AUTHORITY
                if proposal
                else ControllerStepDisposition.COMPLETED
            ),
            result_artifact_sha256s=(_sha(f"result:{plan.step.value}"),),
            blocker_codes=(),
            signed_kernel_command_committed=admission,
            independent_observation_admission_committed=admission,
        )


def _adapters() -> tuple[_Adapter, ...]:
    return tuple(_Adapter(step) for step in ACTIVE_STEPS)


def _controller_manifest() -> ResearchControllerManifest:
    return ResearchControllerManifest(
        controller_key="principal:controller-registration",
        controller_code_sha256=_sha("controller-code"),
        controller_policy_sha256=_sha("controller-policy"),
        capability_catalog_sha256=_sha("capability-catalog"),
        protocol_registry_policy_sha256=_sha("protocol-policy"),
        scientific_bridge_policy_sha256=_sha("bridge-policy"),
        worker_manifest_sha256=_sha("worker-manifest"),
        retry_policy=RetryPolicy(
            max_attempts=3,
            lease_seconds=60,
            heartbeat_interval_seconds=10,
        ),
        prepared_at=NOW,
    )


def _adapter_set(adapters: tuple[_Adapter, ...]) -> ControllerStepAdapterSetManifest:
    controller = _controller_manifest()
    return ControllerStepAdapterSetManifest(
        controller_id=controller.controller_id,
        controller_manifest_sha256=controller.manifest_sha256,
        worker_manifest_sha256=controller.worker_manifest_sha256,
        worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
        adapters=tuple(
            sorted(
                (item.manifest for item in adapters),
                key=lambda item: item.step.value,
            )
        ),
        prepared_at=NOW,
    )


def _executor(adapters: tuple[_Adapter, ...]) -> DedicatedControllerStepExecutor:
    return DedicatedControllerStepExecutor(
        controller_manifest=_controller_manifest(),
        worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
        manifest=_adapter_set(adapters),
        adapters=adapters,
    )


def test_adapter_manifest_is_step_specific_closed_and_self_identifying() -> None:
    manifest = _manifest(ControllerStep.COMMIT_ADMISSION)
    assert manifest.adapter_id == f"csa_{manifest.manifest_sha256[:32]}"
    assert (
        tuple(item.role for item in manifest.authorities)
        == STEP_ROLES[ControllerStep.COMMIT_ADMISSION]
    )
    assert all(item.private_key_loaded_in_worker is False for item in manifest.authorities)

    with pytest.raises(ValidationError, match="passive controller steps"):
        _manifest(ControllerStep.AWAIT_EXECUTION)
    with pytest.raises(ValidationError, match="wrong authority closure"):
        _manifest(
            ControllerStep.COMMIT_VALIDATION,
            authorities=(_binding(ControllerStepAuthorityRole.INDEPENDENT_VALIDATION),),
        )
    with pytest.raises(ValidationError, match="independently deployed"):
        ControllerStepAuthorityBinding(
            role=ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            principal_id="authority:validator",
            key_id="key:validator",
            policy_sha256=_sha("validator-policy"),
            service_manifest_sha256=_sha("validator-service"),
            externally_deployed=False,
        )
    with pytest.raises(ValidationError):
        ControllerStepAdapterManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "private_signing_key_loaded_in_worker": True,
            }
        )

    adapters = _adapters()
    adapter_set = _adapter_set(adapters)
    assert adapter_set.adapter_set_id == f"css_{adapter_set.manifest_sha256[:32]}"
    assert tuple(item.step for item in adapter_set.adapters) == tuple(
        sorted(ACTIVE_STEPS, key=lambda item: item.value)
    )


def test_active_steps_route_to_exact_adapter_with_same_recovery_projection() -> None:
    adapters = _adapters()
    executor = _executor(adapters)

    for step in ACTIVE_STEPS:
        projection = _projection(step)
        plan = plan_recovery_tick(projection)
        receipt = executor.execute(
            wakeup=_wakeup(),
            projection=projection,
            plan=plan,
        )
        called = next(item for item in adapters if item.manifest.step is step)
        assert called.calls == [(_wakeup(), projection, plan)]
        assert receipt.result_artifact_sha256s == (_sha(f"result:{step.value}"),)

    assert sum(len(item.calls) for item in adapters) == len(ACTIVE_STEPS)


@pytest.mark.parametrize(
    ("step", "disposition"),
    (
        (ControllerStep.AWAIT_ACTION_AUTHORIZATION, ControllerStepDisposition.AWAITING_AUTHORITY),
        (ControllerStep.AWAIT_EXECUTION, ControllerStepDisposition.AWAITING_EXTERNAL_RESULT),
        (ControllerStep.BLOCKED, ControllerStepDisposition.BLOCKED),
    ),
)
def test_passive_steps_never_call_an_authority_adapter(
    step: ControllerStep,
    disposition: ControllerStepDisposition,
) -> None:
    adapters = _adapters()
    receipt = _executor(adapters).execute(
        wakeup=_wakeup(),
        projection=_projection(step),
        plan=plan_recovery_tick(_projection(step)),
    )
    assert receipt.disposition is disposition
    assert receipt.result_artifact_sha256s == ()
    assert receipt.blocker_codes == (
        ("operator_policy_blocked",) if step is ControllerStep.BLOCKED else ()
    )
    assert all(not item.calls for item in adapters)


def test_composition_requires_exhaustive_adapters_and_stable_shared_authorities() -> None:
    complete = _adapters()
    with pytest.raises(ValueError, match="exhaustive"):
        DedicatedControllerStepExecutor(
            controller_manifest=_controller_manifest(),
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=_adapter_set(complete),
            adapters=complete[:-1],
        )

    adapters = list(_adapters())
    redesign_index = ACTIVE_STEPS.index(ControllerStep.PROPOSE_REDESIGN)
    adapters[redesign_index] = _Adapter(
        ControllerStep.PROPOSE_REDESIGN,
        manifest=_manifest(
            ControllerStep.PROPOSE_REDESIGN,
            authorities=(
                _binding(
                    ControllerStepAuthorityRole.ACTION_PROPOSAL,
                    principal_id="authority:rebound-proposer",
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="rebound across step adapters"):
        _executor(tuple(adapters))


def test_composition_binds_the_actual_controller_worker_deployment() -> None:
    adapters = _adapters()
    adapter_set = _adapter_set(adapters)
    controller = _controller_manifest()
    another_controller = ResearchControllerManifest.model_validate(
        {
            **controller.model_dump(mode="python", exclude={"controller_id"}),
            "worker_manifest_sha256": _sha("another-worker-manifest"),
        }
    )

    with pytest.raises(ValueError, match="actual controller worker deployment"):
        DedicatedControllerStepExecutor(
            controller_manifest=another_controller,
            worker_process_principal_id=WORKER_PROCESS_PRINCIPAL,
            manifest=adapter_set,
            adapters=adapters,
        )
    with pytest.raises(ValueError, match="actual controller worker deployment"):
        DedicatedControllerStepExecutor(
            controller_manifest=controller,
            worker_process_principal_id="principal:another-worker",
            manifest=adapter_set,
            adapters=adapters,
        )


def test_sensitive_authorities_cannot_share_principals_or_keys() -> None:
    adapters = list(_adapters())
    validation_index = ACTIVE_STEPS.index(ControllerStep.COMMIT_VALIDATION)
    adapters[validation_index] = _Adapter(
        ControllerStep.COMMIT_VALIDATION,
        manifest=_manifest(
            ControllerStep.COMMIT_VALIDATION,
            authorities=(
                _binding(ControllerStepAuthorityRole.DATABASE_ATTESTATION),
                _binding(
                    ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
                    principal_id="authority:execution_authorization",
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="distinct principals and keys"):
        _executor(tuple(adapters))

    execution_index = ACTIVE_STEPS.index(ControllerStep.REGISTER_EXECUTION)
    adapters = list(_adapters())
    adapters[execution_index] = _Adapter(
        ControllerStep.REGISTER_EXECUTION,
        manifest=_manifest(
            ControllerStep.REGISTER_EXECUTION,
            authorities=(
                _binding(
                    ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
                    principal_id=WORKER_PROCESS_PRINCIPAL,
                ),
            ),
        ),
    )
    with pytest.raises(ValidationError, match="cannot be a signed scientific authority"):
        _adapter_set(tuple(adapters))

    adapters = list(_adapters())
    adapters[execution_index] = _Adapter(
        ControllerStep.REGISTER_EXECUTION,
        manifest=_manifest(
            ControllerStep.REGISTER_EXECUTION,
            authorities=(
                _binding(
                    ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
                    principal_id=_controller_manifest().controller_key,
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="operational principals"):
        _executor(tuple(adapters))


def test_stale_plan_and_post_composition_manifest_rebind_fail_before_execution() -> None:
    adapters = _adapters()
    executor = _executor(adapters)
    projection = _projection(ControllerStep.COMPILE_PROTOCOL)
    stale_plan = plan_recovery_tick(_projection(ControllerStep.REGISTER_EXECUTION))
    with pytest.raises(ControllerStepExecutionError, match="exact recovery projection"):
        executor.execute(wakeup=_wakeup(), projection=projection, plan=stale_plan)
    assert all(not item.calls for item in adapters)

    compile_adapter = next(
        item for item in adapters if item.manifest.step is ControllerStep.COMPILE_PROTOCOL
    )
    compile_adapter.manifest = _manifest(ControllerStep.COMPILE_PROTOCOL).model_copy(
        update={"adapter_config_sha256": _sha("rebound-config")}
    )
    with pytest.raises(ControllerStepExecutionError, match="changed after composition"):
        executor.execute(
            wakeup=_wakeup(),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
    assert not compile_adapter.calls


def test_active_nonretryable_blocker_is_typed_without_claiming_authority() -> None:
    adapters = _adapters()
    adapter = next(
        item for item in adapters if item.manifest.step is ControllerStep.REGISTER_EXECUTION
    )
    projection = _projection(ControllerStep.REGISTER_EXECUTION)
    plan = plan_recovery_tick(projection)
    adapter.receipt_override = ControllerStepReceipt(
        wakeup_sha256=_wakeup().wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=ControllerStepDisposition.BLOCKED,
        result_artifact_sha256s=(),
        blocker_codes=("qualification_policy_rejected",),
    )

    receipt = _executor(adapters).execute(
        wakeup=_wakeup(),
        projection=projection,
        plan=plan,
    )

    assert receipt.disposition is ControllerStepDisposition.BLOCKED
    assert receipt.blocker_codes == ("qualification_policy_rejected",)
    assert receipt.signed_kernel_command_committed is False


@pytest.mark.parametrize(
    ("step", "receipt", "message"),
    (
        (
            ControllerStep.COMPILE_PROTOCOL,
            ControllerStepReceipt(
                wakeup_sha256=_wakeup().wakeup_sha256,
                plan_sha256=plan_recovery_tick(
                    _projection(ControllerStep.COMPILE_PROTOCOL)
                ).plan_sha256,
                disposition=ControllerStepDisposition.AWAITING_EXTERNAL_RESULT,
                result_artifact_sha256s=(_sha("compile-request"),),
                blocker_codes=(),
            ),
            "must complete",
        ),
        (
            ControllerStep.PROPOSE_ACTION,
            ControllerStepReceipt(
                wakeup_sha256=_wakeup().wakeup_sha256,
                plan_sha256=plan_recovery_tick(
                    _projection(ControllerStep.PROPOSE_ACTION)
                ).plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(_sha("proposal"),),
                blocker_codes=(),
            ),
            "must wait for a separately signed Kernel command",
        ),
        (
            ControllerStep.COMMIT_ADMISSION,
            ControllerStepReceipt(
                wakeup_sha256=_wakeup().wakeup_sha256,
                plan_sha256=plan_recovery_tick(
                    _projection(ControllerStep.COMMIT_ADMISSION)
                ).plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(_sha("admission"),),
                blocker_codes=(),
                signed_kernel_command_committed=True,
            ),
            "atomically commit",
        ),
        (
            ControllerStep.DERIVE_CONTINUATION,
            ControllerStepReceipt(
                wakeup_sha256=_wakeup().wakeup_sha256,
                plan_sha256=plan_recovery_tick(
                    _projection(ControllerStep.DERIVE_CONTINUATION)
                ).plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(),
                blocker_codes=(),
            ),
            "artifact result",
        ),
    ),
)
def test_active_receipt_semantics_fail_closed(
    step: ControllerStep,
    receipt: ControllerStepReceipt,
    message: str,
) -> None:
    adapters = _adapters()
    adapter = next(item for item in adapters if item.manifest.step is step)
    adapter.receipt_override = receipt
    projection = _projection(step)
    with pytest.raises(ControllerStepExecutionError, match=message):
        _executor(adapters).execute(
            wakeup=_wakeup(),
            projection=projection,
            plan=plan_recovery_tick(projection),
        )
