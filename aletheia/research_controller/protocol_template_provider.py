"""Exact frozen-template provider for powerless protocol compilation.

The provider does not invent a protocol.  A deployment must enumerate complete canonical
``ProtocolCompilationRequest`` values for exact already-authorized action identities.  Missing
templates block; fresh and restarted requests are reconstructed from the same policy bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from aletheia.protocols.compiler import ProtocolCompilationRequest
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_controller.protocol_compilation_step import (
    AuthorizedProtocolCompilationContext,
    PreparedProtocolCompilation,
    ProtocolCompilationPolicyPin,
    ProtocolCompilationStepError,
    ProtocolCompilationUnavailable,
    verify_prepared_protocol,
)
from aletheia.research_kernel.schemas import ActionKind, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PRINCIPAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_:/.-]{0,127}$"


class FrozenProtocolCompilationTemplate(ControllerModel):
    """One complete protocol request prebound to one authorized action object."""

    schema_name: Literal["aletheia.frozen_protocol_compilation_template"] = (
        "aletheia.frozen_protocol_compilation_template"
    )
    schema_version: Literal[1] = 1
    action_sha256: str = Field(pattern=_SHA256_PATTERN)
    action_kind: ActionKind
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    request: ProtocolCompilationRequest

    @model_validator(mode="after")
    def _request_identity_is_exact(self) -> "FrozenProtocolCompilationTemplate":
        if self.request_sha256 != canonical_sha256(self.request):
            raise ValueError("frozen protocol template request identity changed")
        return self

    @property
    def template_sha256(self) -> str:
        return canonical_sha256(self)


class FrozenProtocolTemplateProviderPolicyPin(ControllerModel):
    """Deployment-frozen exact-action template catalog with no generative fallback."""

    schema_name: Literal["aletheia.frozen_protocol_template_provider_policy_pin"] = (
        "aletheia.frozen_protocol_template_provider_policy_pin"
    )
    schema_version: Literal[1] = 1
    provider_implementation_sha256: str = Field(pattern=_SHA256_PATTERN)
    compilation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    prepared_by_principal_id: str = Field(pattern=_PRINCIPAL_PATTERN)
    templates: tuple[FrozenProtocolCompilationTemplate, ...] = Field(
        min_length=1,
        max_length=128,
    )
    unlisted_action_fallback_allowed: Literal[False] = False
    dynamic_template_mutation_allowed: Literal[False] = False
    external_model_callback_allowed: Literal[False] = False
    execution_access_allowed: Literal[False] = False
    signing_key_available: Literal[False] = False

    @model_validator(mode="after")
    def _catalog_is_closed(self) -> "FrozenProtocolTemplateProviderPolicyPin":
        keys = tuple(
            (item.action_sha256, item.action_kind.value, item.request_sha256)
            for item in self.templates
        )
        if keys != tuple(sorted(set(keys))) or len(
            {item.action_sha256 for item in self.templates}
        ) != len(self.templates):
            raise ValueError("frozen protocol templates must be canonical and one per action")
        if any(
            item.request.protocol.authored_by_principal_id != self.prepared_by_principal_id
            for item in self.templates
        ):
            raise ValueError("frozen protocol template author differs from provider policy")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_sha256(self)


class FrozenProtocolTemplateProvider:
    """Powerless exact-catalog provider; unknown actions stop instead of invoking a model."""

    def __init__(
        self,
        *,
        policy: FrozenProtocolTemplateProviderPolicyPin,
        compilation_policy: ProtocolCompilationPolicyPin,
        implementation_sha256: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        policy = FrozenProtocolTemplateProviderPolicyPin.model_validate(
            policy.model_dump(mode="python")
        )
        compilation_policy = ProtocolCompilationPolicyPin.model_validate(
            compilation_policy.model_dump(mode="python")
        )
        if (
            implementation_sha256 != policy.provider_implementation_sha256
            or policy.compilation_policy_sha256 != compilation_policy.policy_sha256
            or policy.prepared_by_principal_id
            not in compilation_policy.allowed_protocol_author_principal_ids
        ):
            raise ValueError("protocol template provider differs from its frozen policies")
        self._policy = policy
        self._compilation_policy = compilation_policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _template(
        self,
        context: AuthorizedProtocolCompilationContext,
    ) -> FrozenProtocolCompilationTemplate:
        matches = tuple(
            item
            for item in self._policy.templates
            if item.action_sha256 == context.action.object_sha256
        )
        if not matches:
            raise ProtocolCompilationUnavailable(
                ("protocol_compilation:no_frozen_action_template",)
            )
        if len(matches) != 1 or matches[0].action_kind is not context.action.kind:
            raise ProtocolCompilationStepError(
                "frozen protocol template changed its exact action identity"
            )
        return matches[0]

    def prepare_protocol(
        self,
        context: AuthorizedProtocolCompilationContext,
    ) -> PreparedProtocolCompilation:
        context = AuthorizedProtocolCompilationContext.model_validate(
            context.model_dump(mode="python")
        )
        prepared_at = self._clock()
        if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
            raise ProtocolCompilationStepError("protocol template provider clock must be aware")
        prepared = PreparedProtocolCompilation(
            context_sha256=context.context_sha256,
            request=self._template(context).request,
            prepared_by_principal_id=self._policy.prepared_by_principal_id,
            prepared_at=prepared_at,
        )
        return self.verify_prepared_protocol(context=context, prepared=prepared)

    def verify_prepared_protocol(
        self,
        *,
        context: AuthorizedProtocolCompilationContext,
        prepared: PreparedProtocolCompilation,
    ) -> PreparedProtocolCompilation:
        try:
            context = AuthorizedProtocolCompilationContext.model_validate(
                context.model_dump(mode="python")
            )
            prepared = verify_prepared_protocol(context=context, prepared=prepared)
            template = self._template(context)
            if (
                context.compilation_policy != self._compilation_policy
                or prepared.request != template.request
                or canonical_sha256(prepared.request) != template.request_sha256
                or prepared.prepared_by_principal_id != self._policy.prepared_by_principal_id
            ):
                raise ValueError("prepared protocol differs from its exact frozen template")
            return prepared
        except (ProtocolCompilationUnavailable, ProtocolCompilationStepError):
            raise
        except Exception as exc:  # noqa: BLE001 - template/provider values fail closed
            raise ProtocolCompilationStepError(
                "frozen protocol template verification failed closed"
            ) from exc


__all__ = [
    "FrozenProtocolCompilationTemplate",
    "FrozenProtocolTemplateProvider",
    "FrozenProtocolTemplateProviderPolicyPin",
]
