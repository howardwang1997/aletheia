"""Fail-closed production routing for one durable controller step.

The controller service computes a deterministic plan from one audited recovery projection.  This
module passes that exact projection to one step-specific adapter, verifies the adapter's frozen
authority manifest, and constrains the returned operational receipt.  It deliberately provides no
generic callback that could combine proposal, execution, validation, admission, and Kernel signing
authority behind one object.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.research_controller.contracts import (
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    ResearchControllerManifest,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_kernel.schemas import canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$"
_ADAPTER_ID_PATTERN = r"^csa_[0-9a-f]{32}$"
_ADAPTER_SET_ID_PATTERN = r"^css_[0-9a-f]{32}$"
_CONTROLLER_ID_PATTERN = r"^rctl_[0-9a-f]{32}$"


class ControllerStepExecutionError(RuntimeError):
    """A production step adapter or its authority/result binding failed closed."""


class ControllerStepAuthorityRole(str, Enum):
    """Narrow external or deterministic authority used by one controller step."""

    ACTION_PROPOSAL = "action_proposal"
    PROTOCOL_COMPILATION = "protocol_compilation"
    EXECUTION_AUTHORIZATION = "execution_authorization"
    INDEPENDENT_VALIDATION = "independent_validation"
    INDEPENDENT_ADMISSION = "independent_admission"
    DATABASE_ATTESTATION = "database_attestation"
    KERNEL_COMMAND = "kernel_command"
    CONTINUATION_ASSESSMENT = "continuation_assessment"


_SIGNED_EXTERNAL_ROLES = frozenset(
    {
        ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    }
)

_ACTIVE_STEP_ROLES = {
    ControllerStep.PROPOSE_ACTION: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.COMPILE_PROTOCOL: (ControllerStepAuthorityRole.PROTOCOL_COMPILATION,),
    ControllerStep.PROPOSE_REDESIGN: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
    ControllerStep.REGISTER_EXECUTION: (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    ControllerStep.COMMIT_VALIDATION: tuple(
        sorted(
            (
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            ),
            key=lambda item: item.value,
        )
    ),
    ControllerStep.COMMIT_ADMISSION: tuple(
        sorted(
            (
                ControllerStepAuthorityRole.DATABASE_ATTESTATION,
                ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
                ControllerStepAuthorityRole.KERNEL_COMMAND,
            ),
            key=lambda item: item.value,
        )
    ),
    ControllerStep.DERIVE_CONTINUATION: (ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,),
    ControllerStep.PROPOSE_FOLLOWUP: (ControllerStepAuthorityRole.ACTION_PROPOSAL,),
}

_PASSIVE_STEPS = frozenset(
    {
        ControllerStep.AWAIT_ACTION_AUTHORIZATION,
        ControllerStep.AWAIT_EXECUTION,
        ControllerStep.BLOCKED,
    }
)


class ControllerStepAuthorityBinding(ControllerModel):
    """Deployment pin declaring that one authority endpoint is external to worker key custody."""

    schema_name: Literal["aletheia.controller_step_authority_binding"] = (
        "aletheia.controller_step_authority_binding"
    )
    schema_version: Literal[1] = 1
    role: ControllerStepAuthorityRole
    principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    key_id: str | None = Field(default=None, pattern=_IDENTITY_PATTERN)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    service_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    externally_deployed: bool
    private_key_loaded_in_worker: Literal[False] = False

    @model_validator(mode="after")
    def _signed_roles_are_external(self) -> "ControllerStepAuthorityBinding":
        signed = self.role in _SIGNED_EXTERNAL_ROLES
        if signed != (self.key_id is not None):
            raise ValueError("only signed controller authorities carry a key id")
        if signed and not self.externally_deployed:
            raise ValueError("signed controller authority must be independently deployed")
        return self

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self)


class ControllerStepAdapterManifest(ControllerModel):
    """Exact code/config/authority closure for one active controller step only."""

    schema_name: Literal["aletheia.controller_step_adapter_manifest"] = (
        "aletheia.controller_step_adapter_manifest"
    )
    schema_version: Literal[1] = 1
    adapter_id: str | None = Field(default=None, pattern=_ADAPTER_ID_PATTERN)
    step: ControllerStep
    adapter_code_sha256: str = Field(pattern=_SHA256_PATTERN)
    adapter_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    authorities: tuple[ControllerStepAuthorityBinding, ...] = Field(min_length=1, max_length=8)
    prepared_at: AwareDatetime
    catch_all_callback_allowed: Literal[False] = False
    private_signing_key_loaded_in_worker: Literal[False] = False
    direct_kernel_mutation_allowed: Literal[False] = False
    direct_observation_admission_allowed: Literal[False] = False
    legacy_optimize_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _step_and_identity_are_exact(self) -> "ControllerStepAdapterManifest":
        expected_roles = _ACTIVE_STEP_ROLES.get(self.step)
        if expected_roles is None:
            raise ValueError("passive controller steps cannot install an execution adapter")
        roles = tuple(item.role for item in self.authorities)
        if roles != tuple(sorted(set(roles), key=lambda item: item.value)):
            raise ValueError("controller step authorities must be unique and canonical")
        if roles != expected_roles:
            raise ValueError("controller step adapter has the wrong authority closure")
        expected_id = f"csa_{self.manifest_sha256[:32]}"
        if self.adapter_id is not None and self.adapter_id != expected_id:
            raise ValueError("controller step adapter id differs from its manifest")
        object.__setattr__(self, "adapter_id", expected_id)
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"adapter_id"}))


class ControllerStepAdapterSetManifest(ControllerModel):
    """Deployment-frozen exhaustive adapter set for one controller manifest."""

    schema_name: Literal["aletheia.controller_step_adapter_set_manifest"] = (
        "aletheia.controller_step_adapter_set_manifest"
    )
    schema_version: Literal[1] = 1
    adapter_set_id: str | None = Field(default=None, pattern=_ADAPTER_SET_ID_PATTERN)
    controller_id: str = Field(pattern=_CONTROLLER_ID_PATTERN)
    controller_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    worker_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    worker_process_principal_id: str = Field(pattern=_IDENTITY_PATTERN)
    adapters: tuple[ControllerStepAdapterManifest, ...] = Field(min_length=8, max_length=8)
    prepared_at: AwareDatetime
    partial_adapter_set_allowed: Literal[False] = False
    runtime_rebinding_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _set_is_exact(self) -> "ControllerStepAdapterSetManifest":
        steps = tuple(item.step for item in self.adapters)
        expected_steps = tuple(sorted(_ACTIVE_STEP_ROLES, key=lambda item: item.value))
        if steps != expected_steps:
            raise ValueError("controller step adapter set must be exhaustive and canonical")
        if len({item.adapter_id for item in self.adapters}) != len(self.adapters):
            raise ValueError("controller step adapter set contains duplicate identities")
        if self.controller_id != f"rctl_{self.controller_manifest_sha256[:32]}":
            raise ValueError("controller id differs from its pinned controller manifest")
        signed_principals = {
            binding.principal_id
            for adapter in self.adapters
            for binding in adapter.authorities
            if binding.role in _SIGNED_EXTERNAL_ROLES
        }
        if self.worker_process_principal_id in signed_principals:
            raise ValueError("controller worker process cannot be a signed scientific authority")
        expected_id = f"css_{self.manifest_sha256[:32]}"
        if self.adapter_set_id is not None and self.adapter_set_id != expected_id:
            raise ValueError("controller step adapter set id differs from its manifest")
        object.__setattr__(self, "adapter_set_id", expected_id)
        return self

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude={"adapter_set_id"}))


class ControllerStepAdapterPort(Protocol):
    """One deployment adapter bound to one active step manifest."""

    manifest: ControllerStepAdapterManifest

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt: ...


def _validated_inputs(
    *,
    wakeup: ControllerWakeup,
    projection: ControllerRecoveryProjection,
    plan: ControllerTickPlan,
) -> tuple[ControllerWakeup, ControllerRecoveryProjection, ControllerTickPlan]:
    try:
        wakeup = ControllerWakeup.model_validate(wakeup.model_dump(mode="python"))
        projection = ControllerRecoveryProjection.model_validate(
            projection.model_dump(mode="python")
        )
        plan = ControllerTickPlan.model_validate(plan.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ControllerStepExecutionError("controller step inputs are invalid") from exc
    expected = plan_recovery_tick(projection)
    if (
        projection.quest_id != wakeup.quest_id
        or expected != plan
        or plan.projection_sha256 != projection.projection_sha256
        or plan.audited_stream_version != projection.audited_stream_version
        or plan.audited_tail_event_sha256 != projection.audited_tail_event_sha256
        or plan.audited_snapshot_sha256 != projection.audited_snapshot_sha256
        or plan.blocker_codes != projection.blocker_codes
    ):
        raise ControllerStepExecutionError(
            "controller step plan differs from its exact recovery projection"
        )
    return wakeup, projection, plan


def _passive_receipt(
    *,
    wakeup: ControllerWakeup,
    plan: ControllerTickPlan,
) -> ControllerStepReceipt:
    if plan.step is ControllerStep.AWAIT_ACTION_AUTHORIZATION:
        disposition = ControllerStepDisposition.AWAITING_AUTHORITY
        blockers: tuple[str, ...] = ()
    elif plan.step is ControllerStep.AWAIT_EXECUTION:
        disposition = ControllerStepDisposition.AWAITING_EXTERNAL_RESULT
        blockers = ()
    elif plan.step is ControllerStep.BLOCKED:
        disposition = ControllerStepDisposition.BLOCKED
        blockers = plan.blocker_codes
    else:  # pragma: no cover - caller checks the closed passive set
        raise ControllerStepExecutionError("unknown passive controller step")
    return ControllerStepReceipt(
        wakeup_sha256=wakeup.wakeup_sha256,
        plan_sha256=plan.plan_sha256,
        disposition=disposition,
        result_artifact_sha256s=(),
        blocker_codes=blockers,
    )


def _validate_active_receipt(
    *,
    wakeup: ControllerWakeup,
    plan: ControllerTickPlan,
    candidate: object,
) -> ControllerStepReceipt:
    try:
        receipt = ControllerStepReceipt.model_validate(candidate.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ControllerStepExecutionError(
            "controller step adapter returned an invalid receipt"
        ) from exc
    if receipt.wakeup_sha256 != wakeup.wakeup_sha256 or receipt.plan_sha256 != plan.plan_sha256:
        raise ControllerStepExecutionError(
            "controller step receipt differs from its exact wakeup or plan"
        )
    if receipt.disposition is ControllerStepDisposition.BLOCKED:
        if (
            receipt.signed_kernel_command_committed
            or receipt.independent_observation_admission_committed
        ):
            raise ControllerStepExecutionError(
                "a blocked controller step cannot claim committed authority"
            )
        return receipt
    if not receipt.result_artifact_sha256s or receipt.blocker_codes:
        raise ControllerStepExecutionError(
            "non-blocked active controller step requires an exact artifact result"
        )

    proposal_step = plan.step in {
        ControllerStep.PROPOSE_ACTION,
        ControllerStep.PROPOSE_REDESIGN,
        ControllerStep.PROPOSE_FOLLOWUP,
    }
    if proposal_step:
        if (
            receipt.disposition is not ControllerStepDisposition.AWAITING_AUTHORITY
            or receipt.signed_kernel_command_committed
            or receipt.independent_observation_admission_committed
        ):
            raise ControllerStepExecutionError(
                "an action proposal must wait for a separately signed Kernel command"
            )
        return receipt

    if receipt.disposition is not ControllerStepDisposition.COMPLETED:
        raise ControllerStepExecutionError(
            "a non-proposal active controller step must complete or fail its durable task"
        )
    if plan.step is ControllerStep.COMMIT_ADMISSION:
        if not (
            receipt.signed_kernel_command_committed
            and receipt.independent_observation_admission_committed
        ):
            raise ControllerStepExecutionError(
                "completed admission must atomically commit observation and Kernel authority"
            )
    elif (
        receipt.signed_kernel_command_committed
        or receipt.independent_observation_admission_committed
    ):
        raise ControllerStepExecutionError(
            "this controller step cannot claim Kernel or observation-admission authority"
        )
    return receipt


class DedicatedControllerStepExecutor:
    """Route every active step to one exact adapter and implement only passive waits locally."""

    def __init__(
        self,
        *,
        controller_manifest: ResearchControllerManifest,
        worker_process_principal_id: str,
        manifest: ControllerStepAdapterSetManifest,
        adapters: tuple[ControllerStepAdapterPort, ...],
    ) -> None:
        try:
            controller_manifest = ResearchControllerManifest.model_validate(
                controller_manifest.model_dump(mode="python")
            )
            adapter_set_manifest = ControllerStepAdapterSetManifest.model_validate(
                manifest.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("controller step adapter set manifest is invalid") from exc
        if (
            adapter_set_manifest.controller_id != controller_manifest.controller_id
            or adapter_set_manifest.controller_manifest_sha256
            != controller_manifest.manifest_sha256
            or adapter_set_manifest.worker_manifest_sha256
            != controller_manifest.worker_manifest_sha256
            or adapter_set_manifest.worker_process_principal_id != worker_process_principal_id
        ):
            raise ValueError(
                "controller step adapter set differs from the actual controller worker deployment"
            )
        by_step: dict[
            ControllerStep,
            tuple[ControllerStepAdapterPort, ControllerStepAdapterManifest],
        ] = {}
        adapter_ids: set[str] = set()
        role_bindings: dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding] = {}
        for adapter in adapters:
            if not callable(getattr(adapter, "execute", None)):
                raise TypeError("controller step adapter does not implement execute")
            try:
                supplied_manifest = adapter.manifest
                adapter_manifest = ControllerStepAdapterManifest.model_validate(
                    supplied_manifest.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise TypeError("controller step adapter manifest is invalid") from exc
            if supplied_manifest != adapter_manifest:
                raise ValueError("controller step adapter manifest changed during composition")
            if adapter_manifest.step in by_step or adapter_manifest.adapter_id in adapter_ids:
                raise ValueError("controller step adapter identity or step is duplicated")
            by_step[adapter_manifest.step] = (adapter, adapter_manifest)
            adapter_ids.add(adapter_manifest.adapter_id)
            for binding in adapter_manifest.authorities:
                previous = role_bindings.get(binding.role)
                if previous is not None and previous != binding:
                    raise ValueError(
                        "one controller authority role was rebound across step adapters"
                    )
                role_bindings[binding.role] = binding

        expected_steps = frozenset(_ACTIVE_STEP_ROLES)
        if frozenset(by_step) != expected_steps:
            missing = sorted(item.value for item in expected_steps - frozenset(by_step))
            extra = sorted(item.value for item in frozenset(by_step) - expected_steps)
            raise ValueError(
                f"controller step adapters must be exhaustive; missing={missing}, extra={extra}"
            )
        observed_manifests = tuple(
            item[1] for _step, item in sorted(by_step.items(), key=lambda pair: pair[0].value)
        )
        if observed_manifests != adapter_set_manifest.adapters:
            raise ValueError("controller step adapters differ from the deployment-pinned set")
        self._verify_authority_separation(
            role_bindings,
            worker_process_principal_id=worker_process_principal_id,
            controller_principal_id=controller_manifest.controller_key,
        )
        self.manifest = adapter_set_manifest
        self._adapters = by_step

    @staticmethod
    def _verify_authority_separation(
        bindings: dict[ControllerStepAuthorityRole, ControllerStepAuthorityBinding],
        *,
        worker_process_principal_id: str,
        controller_principal_id: str,
    ) -> None:
        expected_roles = frozenset(role for roles in _ACTIVE_STEP_ROLES.values() for role in roles)
        if frozenset(bindings) != expected_roles:
            raise ValueError("controller step authority closure is incomplete")
        sensitive = tuple(
            bindings[role] for role in sorted(_SIGNED_EXTERNAL_ROLES, key=lambda item: item.value)
        )
        if len({item.principal_id for item in sensitive}) != len(sensitive) or len(
            {item.key_id for item in sensitive}
        ) != len(sensitive):
            raise ValueError(
                "Kernel, execution, validation, admission, and database authorities must use "
                "distinct principals and keys"
            )
        non_sensitive = tuple(
            item for role, item in bindings.items() if role not in _SIGNED_EXTERNAL_ROLES
        )
        if {item.principal_id for item in sensitive} & {
            item.principal_id for item in non_sensitive
        }:
            raise ValueError("signed controller authorities cannot reuse worker-local principals")
        sensitive_principals = {item.principal_id for item in sensitive}
        if {worker_process_principal_id, controller_principal_id} & sensitive_principals:
            raise ValueError(
                "controller operational principals cannot be signed scientific authorities"
            )

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        wakeup, projection, plan = _validated_inputs(
            wakeup=wakeup,
            projection=projection,
            plan=plan,
        )
        if plan.step in _PASSIVE_STEPS:
            return _passive_receipt(wakeup=wakeup, plan=plan)
        entry = self._adapters.get(plan.step)
        if entry is None:  # pragma: no cover - constructor proves exhaustiveness
            raise ControllerStepExecutionError("active controller step has no exact adapter")
        adapter, frozen_manifest = entry
        if getattr(adapter, "manifest", None) != frozen_manifest:
            raise ControllerStepExecutionError(
                "controller step adapter manifest changed after composition"
            )
        candidate = adapter.execute(
            wakeup=wakeup,
            projection=projection,
            plan=plan,
        )
        return _validate_active_receipt(
            wakeup=wakeup,
            plan=plan,
            candidate=candidate,
        )


__all__ = [
    "ControllerStepAdapterManifest",
    "ControllerStepAdapterPort",
    "ControllerStepAdapterSetManifest",
    "ControllerStepAuthorityBinding",
    "ControllerStepAuthorityRole",
    "ControllerStepExecutionError",
    "DedicatedControllerStepExecutor",
]
