"""Production REGISTER_EXECUTION adapter with external signing and atomic PR-4 reservation."""

from __future__ import annotations

from typing import Protocol

from aletheia.observations.execution_registration import (
    AtomicScientificExecutionRegistrationReceipt,
    PostgreSQLAtomicScientificExecutionRegistrar,
)
from aletheia.observations.scientific_bridge import ScientificExecutionAuthorization
from aletheia.research_controller.contracts import (
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.service import (
    ControllerStepDisposition,
    ControllerStepReceipt,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAdapterManifest,
    ControllerStepAuthorityRole,
    ControllerStepExecutionError,
)


class ScientificExecutionAuthorizationIssuerPort(Protocol):
    """External signer for one exact controller projection; no key enters the worker."""

    def issue_scientific_execution_authorization(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ScientificExecutionAuthorization: ...


class ScientificExecutionRegistrarPort(Protocol):
    """Atomic SEA/qualification registration seam used by the step adapter."""

    def register_and_reserve(
        self,
        authorization: ScientificExecutionAuthorization,
    ) -> AtomicScientificExecutionRegistrationReceipt: ...


class QualifiedExecutionRegistrationStepAdapter:
    """Execute only REGISTER_EXECUTION through an external SEA issuer and public verifiers."""

    def __init__(
        self,
        *,
        manifest: ControllerStepAdapterManifest,
        issuer: ScientificExecutionAuthorizationIssuerPort,
        registrar: ScientificExecutionRegistrarPort,
    ) -> None:
        try:
            frozen = ControllerStepAdapterManifest.model_validate(
                manifest.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("execution-registration adapter manifest is invalid") from exc
        if frozen.step is not ControllerStep.REGISTER_EXECUTION:
            raise ValueError("execution-registration adapter requires its exact controller step")
        if tuple(item.role for item in frozen.authorities) != (
            ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
        ):
            raise ValueError("execution-registration adapter has another authority closure")
        if not callable(getattr(issuer, "issue_scientific_execution_authorization", None)):
            raise TypeError("execution-registration adapter requires an external SEA issuer")
        if not callable(getattr(registrar, "register_and_reserve", None)):
            raise TypeError("execution-registration adapter requires its atomic registrar")
        self.manifest = frozen
        self._issuer = issuer
        self._registrar = registrar

    def execute(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ControllerStepReceipt:
        try:
            wakeup = ControllerWakeup.model_validate(wakeup.model_dump(mode="python"))
            projection = ControllerRecoveryProjection.model_validate(
                projection.model_dump(mode="python")
            )
            plan = ControllerTickPlan.model_validate(plan.model_dump(mode="python"))
            if (
                plan_recovery_tick(projection) != plan
                or plan.step is not ControllerStep.REGISTER_EXECUTION
                or projection.quest_id != wakeup.quest_id
                or projection.action_sha256 is None
                or projection.scientific_slot_id is not None
                or not projection.action_authorized
                or projection.scientific_execution_authorization_registered
            ):
                raise ControllerStepExecutionError(
                    "execution registration received another controller state"
                )
            authorization = ScientificExecutionAuthorization.model_validate(
                self._issuer.issue_scientific_execution_authorization(
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                ).model_dump(mode="python")
            )
            message = authorization.message
            binding = message.action_protocol_binding
            authority = self.manifest.authorities[0]
            if (
                binding.action.quest_id != wakeup.quest_id
                or binding.action.object_sha256 != projection.action_sha256
                or binding.action_authorized_event.sequence != projection.audited_stream_version
                or binding.action_authorized_event.event_sha256
                != projection.audited_tail_event_sha256
                or binding.authorized_graph_snapshot_sha256 != projection.audited_snapshot_sha256
                or message.authorized_by_principal_id != authority.principal_id
                or message.authorization_key_id != authority.key_id
                or message.execution_authority_policy_sha256 != authority.policy_sha256
            ):
                raise ControllerStepExecutionError(
                    "external execution authorization differs from the audited controller state"
                )
            registration = AtomicScientificExecutionRegistrationReceipt.model_validate(
                self._registrar.register_and_reserve(authorization).model_dump(mode="python")
            )
            intent = message.qualification_bundle.intent
            if (
                registration.authorization_sha256 != authorization.authorization_sha256
                or registration.quest_id != wakeup.quest_id
                or registration.scientific_slot_id != message.scientific_slot_id
                or registration.action_sha256 != projection.action_sha256
                or registration.execution_id != intent.execution_id
                or registration.attempt_id
                != intent.infrastructure_attempt.infrastructure_attempt_id
            ):
                raise ControllerStepExecutionError(
                    "atomic execution registration receipt rebound the signed authorization"
                )
            return ControllerStepReceipt(
                wakeup_sha256=wakeup.wakeup_sha256,
                plan_sha256=plan.plan_sha256,
                disposition=ControllerStepDisposition.COMPLETED,
                result_artifact_sha256s=(registration.receipt_sha256,),
                blocker_codes=(),
            )
        except ControllerStepExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the external authority boundary
            raise ControllerStepExecutionError(
                "qualified execution registration failed closed"
            ) from exc


def build_qualified_execution_registration_step_adapter(
    *,
    manifest: ControllerStepAdapterManifest,
    issuer: ScientificExecutionAuthorizationIssuerPort,
    registrar: PostgreSQLAtomicScientificExecutionRegistrar,
) -> QualifiedExecutionRegistrationStepAdapter:
    """Typed production factory without a generic callback or private signing key."""

    return QualifiedExecutionRegistrationStepAdapter(
        manifest=manifest,
        issuer=issuer,
        registrar=registrar,
    )


__all__ = [
    "QualifiedExecutionRegistrationStepAdapter",
    "ScientificExecutionAuthorizationIssuerPort",
    "ScientificExecutionRegistrarPort",
    "build_qualified_execution_registration_step_adapter",
]
