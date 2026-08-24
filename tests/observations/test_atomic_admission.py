from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations import coordinator as coordinator_module
from aletheia.observations.coordinator import (
    AtomicObservationAdmissionError,
    ObservationAdmissionVerificationContext,
    PostgreSQLAtomicObservationAdmissionCoordinator,
)
from aletheia.observations.scientific_bridge import (
    ObservationAdmissionDisposition,
)
from aletheia.observations.store import AppendReceipt
from aletheia.research_kernel.commands import authorize_research_proposal
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.reducer import (
    ActionLifecycle,
    ActionSnapshot,
    ResearchStateGraph,
)
from aletheia.research_store.store import ResearchCommandReceipt, ResearchReplayAudit

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "research_controller",
    _TESTS / "observations",
    _TESTS / "research_kernel",
):
    sys.path.insert(0, str(_fixture_dir))

from test_commands import (  # noqa: E402
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
    _role_key as command_role_key,
)
from test_scientific_bridge import (  # noqa: E402
    DATABASE_PRIVATE_KEY,
    _bridge_case,
    _issue_admission_decision,
    _validated_receipt,
)
from test_vertical_cut import (  # noqa: E402
    _f9_enriched_grouped_fixture,
    runtime_fixture_support,
)


class _KernelAuthority:
    def authorize_observation_incorporation(
        self,
        *,
        proposal,
        committed_admission,
        idempotency_key,
        source_event_key,
    ):
        del committed_admission
        trust_root, policy = command_authority(quest_id=proposal.quest_id)
        key = command_role_key(policy, ResearchAuthorizationRole.ORDINARY)
        return authorize_research_proposal(
            proposal,
            idempotency_key=idempotency_key,
            source_event_key=source_event_key,
            authorization_policy=policy,
            trust_root=trust_root,
            authorization_key_id=key.key_id,
            private_key=COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY],
            authorized_at=proposal.proposed_at + timedelta(seconds=1),
        )


class _KernelStore:
    def __init__(self, audit: ResearchReplayAudit) -> None:
        self.audit = audit
        self.commands = []
        self.receipt = None

    def audit_in_session(self, _session, quest_id, *, expected_scope_binding=None):
        assert quest_id == self.audit.quest_id
        assert expected_scope_binding == self.audit.scope_binding
        return self.audit

    def commit_in_session(self, _session, command):
        self.commands.append(command)
        event = command.to_event(
            sequence=command.expected_stream_version + 1,
            parent_event_sha256=command.expected_tail_event_sha256,
            committed_at=command.authorized_at + timedelta(seconds=1),
        )
        self.receipt = ResearchCommandReceipt(
            command_id=command.command_id,
            quest_id=command.quest_id,
            scope_binding=command.scope_binding,
            idempotency_key=command.idempotency_key,
            source_event_key=command.source_event_key,
            command_sha256=command.command_sha256,
            expected_stream_version=command.expected_stream_version,
            expected_tail_event_sha256=command.expected_tail_event_sha256,
            result_stream_version=event.sequence,
            result_event_sha256=event.event_sha256,
            result_event_id=event.event_id,
            result_snapshot_sha256="f" * 64,
            outbox_id=f"rko_{event.event_sha256[:32]}",
            principal_id=command.principal_id,
            authorization_trust_root_sha256=command.authorization_trust_root_sha256,
            authorization_policy_sha256=command.authorization_policy_sha256,
            authorization_receipt_sha256=command.authorization_receipt_sha256,
            committed_at=event.committed_at,
            created=True,
        )
        return self.receipt

    def load_command_receipt_for_event_in_session(self, _session, *, quest_id, result_event_sha256):
        assert self.receipt is not None
        assert quest_id == self.receipt.quest_id
        assert result_event_sha256 == self.receipt.result_event_sha256
        return self.receipt.model_copy(update={"created": False})


class _TransactionProbe:
    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0

    @contextmanager
    def scope(self):
        try:
            yield object()
        except Exception:
            self.rolled_back += 1
            raise
        else:
            self.committed += 1


def _case(monkeypatch: pytest.MonkeyPatch):
    enriched = _f9_enriched_grouped_fixture()
    original_fixture_by_name = runtime_fixture_support.fixture_by_name

    def fixture_by_name(name: str):
        if name == "grouped_regression":
            return enriched
        return original_fixture_by_name(name)

    monkeypatch.setattr(runtime_fixture_support, "fixture_by_name", fixture_by_name)
    bridge = _bridge_case()
    validation = _validated_receipt(bridge, outcome_bin_id="outcome.negative")
    decision, _ = _issue_admission_decision(
        bridge,
        receipt=validation,
        disposition=ObservationAdmissionDisposition.ADMITTED,
        reason_codes=(),
    )
    binding = bridge.authorization.message.action_protocol_binding
    state = ResearchStateGraph(
        quest_id=binding.action.quest_id,
        stream_version=binding.action_authorized_event.sequence,
        tail_event_sha256=binding.action_authorized_event.event_sha256,
        actions=(
            ActionSnapshot(
                action_ref=binding.action.object_ref,
                branch_id=binding.action_authorized_event.payload.branch_id,
                kind=binding.action.kind,
                lifecycle=ActionLifecycle.AUTHORIZED,
                proposed_event_sha256=binding.action_proposed_event.event_sha256,
                decided_event_sha256=binding.action_authorized_event.event_sha256,
            ),
        ),
    )
    audit = ResearchReplayAudit(
        quest_id=binding.action.quest_id,
        scope_binding=binding.compilation_request.protocol.graph_scope.scope_binding,
        events=(binding.action_proposed_event, binding.action_authorized_event),
        state=state,
        verified_snapshot_sha256s=("a" * 64, state.snapshot_sha256),
    )
    verification = ObservationAdmissionVerificationContext(
        qualification_authority=bridge.qualification_authority,
        action_authority=bridge.action_authority,
        qualification_custody=bridge.qualification_custody,
        raw_run_custody=bridge.raw_run_custody,
        validation_campaign_custody=bridge.validation_campaign_custody,
        execution_authority_pin=bridge.execution_pin,
        validator_authority_pin=bridge.validator_pin,
        admission_authority_pin=bridge.admission_pin,
        database_authority_pin=bridge.database_pin,
        database_private_key=DATABASE_PRIVATE_KEY,
    )
    return bridge, decision, audit, verification


def test_admission_kernel_event_and_outbox_share_one_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bridge, decision, audit, verification = _case(monkeypatch)
    transaction = _TransactionProbe()
    kernel = _KernelStore(audit)
    recorded = []
    monkeypatch.setattr(
        coordinator_module,
        "get_observation_admission_by_decision",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_observation_admission_by_slot",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        coordinator_module,
        "record_observation_admission",
        lambda _session, write: (
            recorded.append(write)
            or AppendReceipt(identity_sha256=write.committed_admission_sha256, created=True)
        ),
    )
    times = iter(
        (
            decision.message.decided_at + timedelta(seconds=1),
            decision.message.decided_at + timedelta(seconds=2),
        )
    )
    coordinator = PostgreSQLAtomicObservationAdmissionCoordinator(
        kernel_store=kernel,
        kernel_authority=_KernelAuthority(),
        verification=verification,
        controller_principal_id="controller:observation-admission",
        session_scope_factory=transaction.scope,
        database_clock=lambda _session: next(times),
    )

    receipt = coordinator.commit_and_incorporate(decision)

    assert receipt.created is True
    assert transaction.committed == 1
    assert transaction.rolled_back == 0
    assert len(kernel.commands) == 1
    assert len(recorded) == 1
    assert recorded[0].incorporated_event_sha256 == receipt.kernel_receipt.result_event_sha256
    assert recorded[0].committed_admission_sha256 == (
        receipt.committed_admission.committed_admission_sha256
    )
    assert receipt.incorporation_payload.source_world_model_sha256 == (
        decision.message.committed_validation_receipt.message.receipt.message.raw_run.scientific_authorization.message.action_protocol_binding.compilation_request.protocol.world_model.world_model_sha256
    )

    monkeypatch.setattr(
        coordinator_module,
        "get_observation_admission_by_decision",
        lambda *_args, **_kwargs: recorded[0],
    )
    replayed = coordinator.commit_and_incorporate(decision)
    assert replayed.created is False
    assert replayed.committed_admission == receipt.committed_admission
    assert replayed.kernel_receipt.result_event_sha256 == (
        receipt.kernel_receipt.result_event_sha256
    )
    assert len(kernel.commands) == 1


def test_failure_after_kernel_staging_rolls_back_the_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bridge, decision, audit, verification = _case(monkeypatch)
    transaction = _TransactionProbe()
    kernel = _KernelStore(audit)
    monkeypatch.setattr(
        coordinator_module,
        "get_observation_admission_by_decision",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_observation_admission_by_slot",
        lambda *_args, **_kwargs: None,
    )

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("injected admission-row failure")

    monkeypatch.setattr(coordinator_module, "record_observation_admission", fail_record)
    times = iter(
        (
            decision.message.decided_at + timedelta(seconds=1),
            decision.message.decided_at + timedelta(seconds=2),
        )
    )
    coordinator = PostgreSQLAtomicObservationAdmissionCoordinator(
        kernel_store=kernel,
        kernel_authority=_KernelAuthority(),
        verification=verification,
        controller_principal_id="controller:observation-admission",
        session_scope_factory=transaction.scope,
        database_clock=lambda _session: next(times),
    )

    with pytest.raises(AtomicObservationAdmissionError, match="failed closed"):
        coordinator.commit_and_incorporate(decision)

    assert len(kernel.commands) == 1
    assert transaction.committed == 0
    assert transaction.rolled_back == 1
