"""Deterministic scientific-execution authorization for one exact controller tick.

The service signs only deployment-frozen qualification material after re-auditing the current
Research Kernel/CAS action and the append-only protocol-compilation registry.  Exact templates
freeze the authorization times, so Ed25519 retries produce byte-identical authorizations without
creating a second mutable scientific ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.orm import Session

from aletheia.db import session_scope
from aletheia.execution.runtime_contracts import (
    EngineeringQualificationBundle,
    EngineeringQualificationGrant,
    QualificationAuthorityPin,
    QualificationAuthorityVerifier,
)
from aletheia.observations.adapters import PostgreSQLResearchActionAuthorityAdapter
from aletheia.observations.scientific_bridge import (
    EngineeringQualificationCustodyVerificationPort,
    ObservationAdmissionPolicy,
    ResearchActionAuthorityVerificationPort,
    ScientificActionProtocolBinding,
    ScientificBridgeAuthorityPin,
    ScientificBridgeRole,
    ScientificExecutionAuthorization,
    ScientificExecutionAuthorizationMessage,
    ScientificObservationArtifactBinding,
    issue_scientific_execution_authorization,
    verify_scientific_execution_authorization,
)
from aletheia.observations.store import (
    ProtocolCompilationWrite,
    get_protocol_compilation_by_action,
)
from aletheia.protocols.compiler import ProtocolCompilationRequest, verify_compilation
from aletheia.protocols.schemas import ProtocolCompilationResult
from aletheia.research_controller.contracts import (
    CompilationDisposition,
    ControllerModel,
    ControllerRecoveryProjection,
    ControllerStep,
    ControllerTickPlan,
    ControllerWakeup,
    plan_recovery_tick,
)
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import canonical_sha256
from aletheia.research_store.store import ResearchKernelStore, ResearchReplayAudit

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ExecutionAuthorizationServiceError(RuntimeError):
    """The signer could not prove or sign one exact current execution authorization."""


class FrozenScientificExecutionAuthorizationTemplate(ControllerModel):
    """Complete unsigned SEA material for one exact authorized action and compilation."""

    schema_name: Literal["aletheia.frozen_scientific_execution_authorization_template"] = (
        "aletheia.frozen_scientific_execution_authorization_template"
    )
    schema_version: Literal[1] = 1
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_protocol_binding: ScientificActionProtocolBinding
    qualification_bundle: EngineeringQualificationBundle
    qualification_grant: EngineeringQualificationGrant
    validator_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    observation_validation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    admission_policy: ObservationAdmissionPolicy
    scientific_observation_artifact_binding: ScientificObservationArtifactBinding
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    observation_admission_deadline: AwareDatetime

    @model_validator(mode="after")
    def _template_is_exact(self) -> "FrozenScientificExecutionAuthorizationTemplate":
        binding = self.action_protocol_binding
        if (
            self.action_sha256 != binding.action.object_sha256
            or self.qualification_bundle.compilation_request != binding.compilation_request
            or self.qualification_bundle.compilation_result != binding.compilation_result
            or self.qualification_bundle.work_order != binding.work_order
        ):
            raise ValueError("execution authorization template escaped its action compilation")
        return self

    @property
    def template_sha256(self) -> str:
        return canonical_sha256(self)


class FrozenScientificExecutionAuthorizationCatalog(ControllerModel):
    """Deployment-owned exact-action SEA catalog and public authority closure."""

    schema_name: Literal["aletheia.frozen_scientific_execution_authorization_catalog"] = (
        "aletheia.frozen_scientific_execution_authorization_catalog"
    )
    schema_version: Literal[1] = 1
    issuer_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualification_authority_pin: QualificationAuthorityPin
    execution_authority_pin: ScientificBridgeAuthorityPin
    validator_authority_pin: ScientificBridgeAuthorityPin
    admission_authority_pin: ScientificBridgeAuthorityPin
    templates: tuple[FrozenScientificExecutionAuthorizationTemplate, ...] = Field(
        min_length=1,
        max_length=128,
    )
    unlisted_action_fallback_allowed: Literal[False] = False
    dynamic_template_mutation_allowed: Literal[False] = False
    qualification_signing_key_loaded: Literal[False] = False
    validation_signing_key_loaded: Literal[False] = False
    admission_signing_key_loaded: Literal[False] = False
    kernel_signing_key_loaded: Literal[False] = False

    @model_validator(mode="after")
    def _catalog_is_closed(self) -> "FrozenScientificExecutionAuthorizationCatalog":
        execution = self.execution_authority_pin
        validator = self.validator_authority_pin
        admission = self.admission_authority_pin
        if (
            execution.role is not ScientificBridgeRole.EXECUTION_AUTHORIZER
            or validator.role is not ScientificBridgeRole.OBSERVATION_VALIDATOR
            or admission.role is not ScientificBridgeRole.OBSERVATION_ADMITTER
        ):
            raise ValueError("execution authorization catalog has rebound bridge roles")
        bridge_principals = (
            execution.principal_id,
            validator.principal_id,
            admission.principal_id,
            self.qualification_authority_pin.principal_id,
        )
        bridge_keys = (
            execution.key_id,
            validator.key_id,
            admission.key_id,
            self.qualification_authority_pin.key_id,
        )
        bridge_policies = (
            execution.policy_sha256,
            validator.policy_sha256,
            admission.policy_sha256,
            self.qualification_authority_pin.policy_sha256,
        )
        if (
            len(set(bridge_principals)) != len(bridge_principals)
            or len(set(bridge_keys)) != len(bridge_keys)
            or len(set(bridge_policies)) != len(bridge_policies)
        ):
            raise ValueError("execution, validation, admission, and qualification roles overlap")
        keys = tuple(
            (item.action_sha256, item.compilation_sha256, item.template_sha256)
            for item in self.templates
        )
        if keys != tuple(sorted(set(keys))) or len(
            {item.action_sha256 for item in self.templates}
        ) != len(self.templates):
            raise ValueError(
                "execution authorization templates must be canonical and one per action"
            )
        for item in self.templates:
            if (
                not execution.active_at(item.authorized_at)
                or item.expires_at > execution.active_until
                or not validator.active_at(item.authorized_at)
                or item.observation_admission_deadline > validator.active_until
                or not admission.active_at(item.authorized_at)
                or item.observation_admission_deadline > admission.active_until
                or not self.qualification_authority_pin.active_at(item.authorized_at)
                or item.expires_at > self.qualification_authority_pin.active_until
            ):
                raise ValueError("execution authorization template outlives a pinned authority")
            ScientificExecutionAuthorizationMessage(
                scientific_slot_id=item.action_protocol_binding.scientific_slot_id,
                action_protocol_binding=item.action_protocol_binding,
                qualification_bundle=item.qualification_bundle,
                qualification_grant=item.qualification_grant,
                validator_manifest_sha256=item.validator_manifest_sha256,
                observation_validation_policy_sha256=(item.observation_validation_policy_sha256),
                admission_policy=item.admission_policy,
                scientific_observation_artifact_binding=(
                    item.scientific_observation_artifact_binding
                ),
                execution_authority_policy_sha256=execution.policy_sha256,
                authorized_by_principal_id=execution.principal_id,
                authorization_key_id=execution.key_id,
                validator_authority_policy_sha256=validator.policy_sha256,
                validator_principal_id=validator.principal_id,
                validator_key_id=validator.key_id,
                admission_authority_policy_sha256=admission.policy_sha256,
                admission_principal_id=admission.principal_id,
                admission_key_id=admission.key_id,
                authorized_at=item.authorized_at,
                expires_at=item.expires_at,
                observation_admission_deadline=item.observation_admission_deadline,
            )
        return self

    @property
    def catalog_sha256(self) -> str:
        return canonical_sha256(self)


class ScientificExecutionAuthorizationSourcePort(
    ResearchActionAuthorityVerificationPort,
    Protocol,
):
    """Fresh Kernel/CAS and compilation-registry proof for one issuance tick."""

    def verify_execution_authorization_source(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
        binding: ScientificActionProtocolBinding,
        expected_compilation_sha256: str,
        observed_at: datetime,
    ) -> ProtocolCompilationWrite: ...


SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


class PostgreSQLScientificExecutionAuthorizationSource:
    """Re-audit the exact current Kernel head and registered accepted compilation."""

    def __init__(
        self,
        *,
        kernel_store: ResearchKernelStore,
        sessions: SessionScopeFactory = session_scope,
    ) -> None:
        if not callable(getattr(kernel_store, "audit_in_session", None)) or not callable(sessions):
            raise TypeError("execution authorization source dependencies are invalid")
        self._kernel_store = kernel_store
        self._sessions = sessions
        self._action_authority = PostgreSQLResearchActionAuthorityAdapter(kernel_store)

    def verify_action_protocol_binding(
        self,
        *,
        binding: ScientificActionProtocolBinding,
        observed_at: datetime,
    ) -> str:
        return self._action_authority.verify_action_protocol_binding(
            binding=binding,
            observed_at=observed_at,
        )

    def verify_execution_authorization_source(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
        binding: ScientificActionProtocolBinding,
        expected_compilation_sha256: str,
        observed_at: datetime,
    ) -> ProtocolCompilationWrite:
        try:
            if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
                raise ValueError("execution authorization observation time must be UTC")
            binding = ScientificActionProtocolBinding.model_validate(
                binding.model_dump(mode="python")
            )
            binding_sha256 = self.verify_action_protocol_binding(
                binding=binding,
                observed_at=observed_at,
            )
            if binding_sha256 != binding.binding_sha256:
                raise ValueError("action authority returned another binding")
            with self._sessions() as session:
                audit = ResearchReplayAudit.model_validate(
                    self._kernel_store.audit_in_session(
                        session,
                        projection.quest_id,
                        expected_scope_binding=(
                            binding.compilation_request.protocol.graph_scope.scope_binding
                        ),
                    ).model_dump(mode="python")
                )
                self._verify_current_audit(
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                    binding=binding,
                    audit=audit,
                    observed_at=observed_at,
                )
                write = get_protocol_compilation_by_action(
                    session,
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                )
                if write is None:
                    raise ValueError("accepted protocol compilation is absent")
                request = ProtocolCompilationRequest.model_validate(write.request_json)
                result = ProtocolCompilationResult.model_validate(write.result_json)
                expected = ProtocolCompilationWrite.from_contract(
                    quest_id=projection.quest_id,
                    action_sha256=projection.action_sha256,
                    request=request,
                    result=result,
                    registered_at=write.registered_at,
                )
                verify_compilation(request, result)
                if (
                    write != expected
                    or write.compilation_sha256 != expected_compilation_sha256
                    or request != binding.compilation_request
                    or result != binding.compilation_result
                    or result.work_order != binding.work_order
                    or write.registered_at < binding.bound_at
                    or write.registered_at > observed_at
                ):
                    raise ValueError("registered compilation differs from the SEA template")
            return write
        except ExecutionAuthorizationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - PostgreSQL/CAS authority fails closed
            raise ExecutionAuthorizationServiceError(
                "execution authorization source verification failed closed"
            ) from exc

    @staticmethod
    def _verify_current_audit(
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
        binding: ScientificActionProtocolBinding,
        audit: ResearchReplayAudit,
        observed_at: datetime,
    ) -> None:
        state = audit.state
        action_states = tuple(
            item for item in state.actions if item.action_ref == binding.action.object_ref
        )
        if (
            wakeup.quest_id != projection.quest_id
            or binding.action.quest_id != projection.quest_id
            or binding.action.object_sha256 != projection.action_sha256
            or audit.quest_id != projection.quest_id
            or state.quest_id != projection.quest_id
            or state.terminal
            or state.stream_version != projection.audited_stream_version
            or state.tail_event_sha256 != projection.audited_tail_event_sha256
            or state.snapshot_sha256 != projection.audited_snapshot_sha256
            or len(audit.events) != len(audit.verified_snapshot_sha256s)
            or not audit.events
            or not audit.verified_snapshot_sha256s
            or audit.events[-1] != binding.action_authorized_event
            or audit.events[-1].committed_at > observed_at
            or tuple(audit.verified_snapshot_sha256s)[-1]
            != binding.authorized_graph_snapshot_sha256
            or action_states == ()
            or len(action_states) != 1
            or action_states[0].lifecycle is not ActionLifecycle.AUTHORIZED
            or action_states[0].branch_id
            != binding.compilation_request.protocol.graph_scope.branch_id
            or action_states[0].proposed_event_sha256 != binding.action_proposed_event.event_sha256
            or action_states[0].decided_event_sha256 != binding.action_authorized_event.event_sha256
            or plan_recovery_tick(projection) != plan
            or plan.step is not ControllerStep.REGISTER_EXECUTION
        ):
            raise ExecutionAuthorizationServiceError(
                "Kernel audit differs from the exact execution-authorization tick"
            )


class FrozenScientificExecutionAuthorizationIssuer:
    """Issue one retry-stable SEA from exact, pre-reviewed scientific material."""

    def __init__(
        self,
        *,
        source: ScientificExecutionAuthorizationSourcePort,
        qualification_authority: QualificationAuthorityVerifier,
        qualification_custody: EngineeringQualificationCustodyVerificationPort,
        catalog: FrozenScientificExecutionAuthorizationCatalog,
        authority_binding: ControllerStepAuthorityBinding,
        private_key: bytes,
        implementation_sha256: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not callable(getattr(source, "verify_execution_authorization_source", None))
            or not callable(getattr(source, "verify_action_protocol_binding", None))
            or not isinstance(qualification_authority, QualificationAuthorityVerifier)
            or not callable(
                getattr(qualification_custody, "verify_engineering_qualification_custody", None)
            )
        ):
            raise TypeError("execution authorization service dependencies are invalid")
        catalog = FrozenScientificExecutionAuthorizationCatalog.model_validate(
            catalog.model_dump(mode="python")
        )
        binding = ControllerStepAuthorityBinding.model_validate(
            authority_binding.model_dump(mode="python")
        )
        execution = catalog.execution_authority_pin
        try:
            public_key = (
                Ed25519PrivateKey.from_private_bytes(private_key)
                .public_key()
                .public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
                .hex()
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("execution authorization private key is invalid") from exc
        if (
            implementation_sha256 != catalog.issuer_implementation_sha256
            or qualification_authority.pin != catalog.qualification_authority_pin
            or binding.role is not ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION
            or not binding.externally_deployed
            or binding.principal_id != execution.principal_id
            or binding.key_id != execution.key_id
            or binding.policy_sha256 != execution.policy_sha256
            or public_key != execution.public_key_ed25519_hex
        ):
            raise ValueError("execution authorization signer differs from its frozen authority")
        self._source = source
        self._qualification_authority = qualification_authority
        self._qualification_custody = qualification_custody
        self._catalog = catalog
        self._private_key = bytes(private_key)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.authority_binding = binding

    def _template(
        self,
        projection: ControllerRecoveryProjection,
    ) -> FrozenScientificExecutionAuthorizationTemplate:
        matches = tuple(
            item
            for item in self._catalog.templates
            if item.action_sha256 == projection.action_sha256
        )
        if len(matches) != 1:
            raise ExecutionAuthorizationServiceError(
                "no unique frozen execution authorization template exists for this action"
            )
        return matches[0]

    @staticmethod
    def _verify_source_write(
        *,
        template: FrozenScientificExecutionAuthorizationTemplate,
        candidate: ProtocolCompilationWrite,
        observed_at: datetime,
    ) -> ProtocolCompilationWrite:
        try:
            write = ProtocolCompilationWrite.model_validate(candidate.model_dump(mode="python"))
            request = ProtocolCompilationRequest.model_validate(write.request_json)
            result = ProtocolCompilationResult.model_validate(write.result_json)
            binding = template.action_protocol_binding
            expected = ProtocolCompilationWrite.from_contract(
                quest_id=binding.action.quest_id,
                action_sha256=template.action_sha256,
                request=request,
                result=result,
                registered_at=write.registered_at,
            )
            verify_compilation(request, result)
            if (
                write != expected
                or write.compilation_sha256 != template.compilation_sha256
                or request != binding.compilation_request
                or result != binding.compilation_result
                or result.work_order != binding.work_order
                or write.registered_at < binding.bound_at
                or write.registered_at > observed_at
            ):
                raise ValueError("execution authorization source returned another compilation")
            return write
        except ExecutionAuthorizationServiceError:
            raise
        except Exception as exc:
            raise ExecutionAuthorizationServiceError(
                "execution authorization source returned invalid compilation custody"
            ) from exc

    def issue_scientific_execution_authorization(
        self,
        *,
        wakeup: ControllerWakeup,
        projection: ControllerRecoveryProjection,
        plan: ControllerTickPlan,
    ) -> ScientificExecutionAuthorization:
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
                or not projection.action_authorized
                or projection.compilation_disposition is not CompilationDisposition.ACCEPTED
                or projection.scientific_execution_authorization_registered
            ):
                raise ExecutionAuthorizationServiceError(
                    "execution authorization received another controller state"
                )
            observed_at = self._clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
                raise ExecutionAuthorizationServiceError(
                    "execution authorization service clock must be UTC"
                )
            template = self._template(projection)
            if not template.authorized_at <= observed_at < template.expires_at:
                raise ExecutionAuthorizationServiceError(
                    "frozen execution authorization is outside its issuance window"
                )
            first = self._verify_source_write(
                template=template,
                candidate=self._source.verify_execution_authorization_source(
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                    binding=template.action_protocol_binding,
                    expected_compilation_sha256=template.compilation_sha256,
                    observed_at=observed_at,
                ),
                observed_at=observed_at,
            )
            catalog = self._catalog
            signed = issue_scientific_execution_authorization(
                action_protocol_binding=template.action_protocol_binding,
                qualification_bundle=template.qualification_bundle,
                qualification_grant=template.qualification_grant,
                validator_manifest_sha256=template.validator_manifest_sha256,
                observation_validation_policy_sha256=(
                    template.observation_validation_policy_sha256
                ),
                admission_policy=template.admission_policy,
                scientific_observation_artifact_binding=(
                    template.scientific_observation_artifact_binding
                ),
                qualification_authority=self._qualification_authority,
                action_authority=self._source,
                qualification_custody=self._qualification_custody,
                execution_authority_pin=catalog.execution_authority_pin,
                validator_authority_pin=catalog.validator_authority_pin,
                admission_authority_pin=catalog.admission_authority_pin,
                private_key=self._private_key,
                authorized_at=template.authorized_at,
                expires_at=template.expires_at,
                observation_admission_deadline=template.observation_admission_deadline,
            )
            verify_scientific_execution_authorization(
                authorization=signed,
                qualification_authority=self._qualification_authority,
                action_authority=self._source,
                qualification_custody=self._qualification_custody,
                execution_authority_pin=catalog.execution_authority_pin,
                validator_authority_pin=catalog.validator_authority_pin,
                admission_authority_pin=catalog.admission_authority_pin,
                observed_at=observed_at,
            )
            second = self._verify_source_write(
                template=template,
                candidate=self._source.verify_execution_authorization_source(
                    wakeup=wakeup,
                    projection=projection,
                    plan=plan,
                    binding=template.action_protocol_binding,
                    expected_compilation_sha256=template.compilation_sha256,
                    observed_at=observed_at,
                ),
                observed_at=observed_at,
            )
            if first != second:
                raise ExecutionAuthorizationServiceError(
                    "execution authorization source changed while signing"
                )
            return signed
        except ExecutionAuthorizationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - all authority failures are fail closed
            raise ExecutionAuthorizationServiceError(
                "scientific execution authorization failed closed"
            ) from exc


__all__ = [
    "ExecutionAuthorizationServiceError",
    "FrozenScientificExecutionAuthorizationCatalog",
    "FrozenScientificExecutionAuthorizationIssuer",
    "FrozenScientificExecutionAuthorizationTemplate",
    "PostgreSQLScientificExecutionAuthorizationSource",
    "ScientificExecutionAuthorizationSourcePort",
]
