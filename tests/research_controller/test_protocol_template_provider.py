from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.research_controller.protocol_compilation_step import (
    AuthorizedProtocolCompilationContext,
    ProtocolCompilationStepError,
    ProtocolCompilationUnavailable,
)
from aletheia.research_controller.protocol_template_provider import (
    FrozenProtocolCompilationTemplate,
    FrozenProtocolTemplateProvider,
    FrozenProtocolTemplateProviderPolicyPin,
)
from aletheia.research_kernel.schemas import EventType, canonical_json_bytes, canonical_sha256

_TEST_ROOT = Path(__file__).resolve().parent
if str(_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_TEST_ROOT))

from test_protocol_compilation_step import (  # noqa: E402
    _authorized_case,
    _policy,
    _projection,
    _request,
    _wakeup,
)
from aletheia.research_controller.contracts import plan_recovery_tick  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(*, rebound_action_sha256: str | None = None):
    case, question, action, authorized = _authorized_case()
    request = _request(case, question, authorized)
    compilation_policy = _policy(request)
    projection = _projection(case, action)
    wakeup = _wakeup(case)
    plan = plan_recovery_tick(projection)
    proposed = tuple(
        event for event in case.events if event.event_type is EventType.ACTION_PROPOSED
    )
    assert len(proposed) == 1
    context = AuthorizedProtocolCompilationContext(
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
        compilation_policy=compilation_policy,
        latest_event_committed_at=authorized.committed_at,
    )
    implementation = (
        Path(__file__).resolve().parents[2]
        / "aletheia/research_controller/protocol_template_provider.py"
    ).resolve()
    implementation_sha256 = hashlib.sha256(implementation.read_bytes()).hexdigest()
    template = FrozenProtocolCompilationTemplate(
        action_sha256=rebound_action_sha256 or action.object_sha256,
        action_kind=action.kind,
        request_sha256=canonical_sha256(request),
        request=request,
    )
    provider_policy = FrozenProtocolTemplateProviderPolicyPin(
        provider_implementation_sha256=implementation_sha256,
        compilation_policy_sha256=compilation_policy.policy_sha256,
        prepared_by_principal_id=request.protocol.authored_by_principal_id,
        templates=(template,),
    )
    provider = FrozenProtocolTemplateProvider(
        policy=provider_policy,
        compilation_policy=compilation_policy,
        implementation_sha256=implementation_sha256,
        clock=lambda: request.protocol.authored_at + timedelta(seconds=1),
    )
    return context, request, compilation_policy, provider_policy, provider


def test_exact_template_is_prepared_and_freshly_reverified() -> None:
    context, request, compilation_policy, provider_policy, provider = _fixture()

    prepared = provider.prepare_protocol(context)
    restarted = FrozenProtocolTemplateProvider(
        policy=provider_policy,
        compilation_policy=compilation_policy,
        implementation_sha256=provider_policy.provider_implementation_sha256,
        clock=lambda: request.protocol.authored_at + timedelta(days=1),
    )

    assert prepared.context_sha256 == context.context_sha256
    assert prepared.request == request
    assert restarted.verify_prepared_protocol(context=context, prepared=prepared) == prepared
    assert not prepared.execution_started
    assert not prepared.observation_accessed


def test_template_request_or_preparation_rebinding_fails_closed() -> None:
    context, _request_value, _policy_pin, _provider_policy, provider = _fixture()
    prepared = provider.prepare_protocol(context)
    rebound_request = prepared.request.model_copy(
        update={"compiler_implementation_sha256": _sha("rebound-compiler")}
    )

    with pytest.raises(ProtocolCompilationStepError):
        provider.verify_prepared_protocol(
            context=context,
            prepared=prepared.model_copy(update={"request": rebound_request}),
        )
    with pytest.raises(ProtocolCompilationStepError):
        provider.verify_prepared_protocol(
            context=context,
            prepared=prepared.model_copy(
                update={"prepared_by_principal_id": "principal:other-author"}
            ),
        )


def test_unlisted_action_returns_one_canonical_blocker() -> None:
    context, _request_value, _policy_pin, _provider_policy, provider = _fixture(
        rebound_action_sha256="f" * 64
    )

    with pytest.raises(ProtocolCompilationUnavailable) as exc_info:
        provider.prepare_protocol(context)

    assert exc_info.value.blocker_codes == ("protocol_compilation:no_frozen_action_template",)


def test_provider_policy_is_powerless_and_content_addressed() -> None:
    _context, request, policy, provider_policy, _provider = _fixture()
    encoded = canonical_json_bytes(provider_policy)

    assert provider_policy.templates[0].request_sha256 == canonical_sha256(request)
    assert provider_policy.compilation_policy_sha256 == policy.policy_sha256
    assert b'"external_model_callback_allowed":false' in encoded
    assert b'"dynamic_template_mutation_allowed":false' in encoded
    assert b'"execution_access_allowed":false' in encoded
    assert b'"signing_key_available":false' in encoded
    assert b"private_key" not in encoded


def test_provider_rejects_rebound_implementation_or_compilation_policy() -> None:
    _context, _request_value, policy, provider_policy, _provider = _fixture()
    with pytest.raises(ValueError, match="differs from its frozen policies"):
        FrozenProtocolTemplateProvider(
            policy=provider_policy,
            compilation_policy=policy,
            implementation_sha256=_sha("other-source"),
        )
    with pytest.raises(ValueError, match="differs from its frozen policies"):
        FrozenProtocolTemplateProvider(
            policy=provider_policy.model_copy(
                update={"compilation_policy_sha256": _sha("other-policy")}
            ),
            compilation_policy=policy,
            implementation_sha256=provider_policy.provider_implementation_sha256,
        )
