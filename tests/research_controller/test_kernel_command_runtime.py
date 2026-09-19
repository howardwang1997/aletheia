"""Contract tests for the two ARL-2 kernel-command signing services (PR B8)."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.research_controller.contracts import ControllerStep
from aletheia.research_controller.external_rpc import (
    ControllerWorkerRPCBlocked,
    ControllerWorkerRPCClient,
    ControllerWorkerRPCOperation,
    ControllerWorkerRPCServicePin,
    controller_worker_rpc_key_id,
)
from aletheia.research_controller.external_rpc_server import (
    ActionKernelCommandRPCPayload,
    ControllerWorkerRPCService,
    ControllerWorkerRPCServiceBlocked,
    TransitionKernelCommandRPCPayload,
)
from aletheia.research_controller.kernel_authority import ControllerKernelPolicyAssignment
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_controller_kernel_command_runtime import (
    build_action_kernel_command_rpc_service,
    build_transition_kernel_command_rpc_service,
)
from aletheia.research_controller_rpc_runtime import (
    ControllerWorkerRPCProcessError,
    ControllerWorkerRPCServerDeployment,
    build_controller_worker_rpc_server_runtime,
)
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchScopeBinding,
)
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.schemas import canonical_json_bytes

_TEST_ROOT = Path(__file__).resolve().parent
_KERNEL_TESTS = Path(__file__).resolve().parents[1] / "research_kernel"
for _path in (_TEST_ROOT, _KERNEL_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_commands import (  # noqa: E402
    _PRIVATE_KEYS as COMMAND_PRIVATE_KEYS,
    _authority as command_authority,
    _role_key as command_role_key,
)
from test_kernel_command_authority import (  # noqa: E402
    _authorization_proposal,
    _fork_decision,
    _incorporated_event,
    _receipt,
    _submission,
    _transition_proposal,
)
from test_action_proposals import NOW, QUEST_ID  # noqa: E402
from test_independent_admission_rpc_runtime import _DirectTransport  # noqa: E402


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fixture(tmp_path: Path, *, domain: str) -> SimpleNamespace:
    """One pinned kernel-command service deployment: pin, key, config, manifest."""

    repository_root = Path(__file__).resolve().parents[2]
    factory = (repository_root / "aletheia/research_controller_kernel_command_runtime.py").resolve()
    authority_source = (
        repository_root / "aletheia/research_controller/kernel_authority.py"
    ).resolve()

    roots = {
        "socket": (tmp_path / f"{domain}-command-socket"),
        "config": (tmp_path / f"{domain}-command-config"),
        "receipt": (tmp_path / f"{domain}-command-receipt-secret"),
        "command": (tmp_path / f"{domain}-command-signing-secret"),
    }
    for label, path in roots.items():
        mode = 0o750 if label == "socket" else 0o700
        path.mkdir(mode=mode)
        path.chmod(mode)

    trust_root, policy = command_authority(quest_id=QUEST_ID)
    kernel_key = command_role_key(policy, ResearchAuthorizationRole.ORDINARY)
    assignment = ControllerKernelPolicyAssignment(
        quest_id=QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=QUEST_ID),
        authorization_policy=policy,
    )
    receipt_private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(f"{domain}-command-rpc-receipt".encode()).digest()
    )
    receipt_public_hex = receipt_private_key.public_key().public_bytes_raw().hex()
    process_uid = os.geteuid()
    process_gid = os.getegid()
    prepared_at = NOW
    is_transition = domain == "transition"
    operation = (
        ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND
        if is_transition
        else ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND
    )
    binding = ControllerStepAuthorityBinding(
        role=(
            ControllerStepAuthorityRole.TRANSITION_KERNEL_COMMAND
            if is_transition
            else ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND
        ),
        principal_id=kernel_key.principal_id,
        key_id=kernel_key.key_id,
        policy_sha256=policy.policy_sha256,
        service_manifest_sha256=_sha(f"{domain}-command-service-manifest"),
        externally_deployed=True,
    )
    pin = ControllerWorkerRPCServicePin(
        service_principal_id=f"principal.kernel.{domain}-command-service",
        service_manifest_sha256=_sha(f"{domain}-command-service-manifest"),
        service_policy_sha256=_sha(f"{domain}-command-service-policy"),
        operations=(operation,),
        authority_binding_sha256s=(binding.binding_sha256,),
        socket_path=str(roots["socket"] / f"{domain}-command.sock"),
        socket_owner_uid=process_uid,
        socket_group_gid=process_gid,
        socket_mode=0o660,
        peer_uid=process_uid,
        peer_gid=process_gid,
        receipt_key_id=controller_worker_rpc_key_id(receipt_public_hex),
        receipt_public_key_ed25519_hex=receipt_public_hex,
        valid_from=prepared_at - timedelta(minutes=1),
        expires_at=prepared_at + timedelta(hours=1),
        connect_timeout_seconds=2.0,
        max_request_bytes=4 * 1024**2,
        max_response_bytes=4 * 1024**2,
    )
    kernel_private_key = COMMAND_PRIVATE_KEYS[ResearchAuthorizationRole.ORDINARY]
    key_path = (roots["command"] / "kernel-command.key").resolve()
    key_path.write_bytes(kernel_private_key)
    key_path.chmod(0o400)
    config = {
        "controller_id": "rctl_" + "9" * 32,
        "controller_manifest_sha256": _sha("controller-manifest"),
        "worker_process_principal_id": "principal.controller.worker-process",
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "prepared_at": prepared_at.isoformat().replace("+00:00", "Z"),
        "authorization_key_id": kernel_key.key_id,
        "kernel_authority_source_path": str(authority_source),
        "kernel_authority_source_sha256": hashlib.sha256(authority_source.read_bytes()).hexdigest(),
        "trust_root": trust_root.model_dump(mode="json"),
        "policy_assignments": [assignment.model_dump(mode="json")],
        "command_signing_key": {
            "path": str(key_path),
            "file_sha256": hashlib.sha256(kernel_private_key).hexdigest(),
            "key_id": kernel_key.key_id,
            "owner_uid": process_uid,
            "group_gid": process_gid,
            "file_mode": 0o400,
        },
    }
    config_path = (roots["config"] / f"{domain}-command.json").resolve()
    config_path.write_bytes(canonical_json_bytes(config))
    receipt_key_path = (roots["receipt"] / "receipt.key").resolve()
    receipt_key_path.write_bytes(receipt_private_key.private_bytes_raw())
    receipt_key_path.chmod(0o400)
    socket_metadata = roots["socket"].stat()
    deployment = ControllerWorkerRPCServerDeployment(
        service_pin=pin,
        controller_id=config["controller_id"],
        controller_manifest_sha256=config["controller_manifest_sha256"],
        worker_process_principal_id=config["worker_process_principal_id"],
        worker_peer_uid=process_uid + 1,
        worker_peer_gid=process_gid,
        process_uid=process_uid,
        process_gid=process_gid,
        socket_parent_path=str(roots["socket"]),
        socket_parent_owner_uid=socket_metadata.st_uid,
        socket_parent_owner_gid=socket_metadata.st_gid,
        socket_parent_mode=stat.S_IMODE(socket_metadata.st_mode),
        socket_parent_device_id=socket_metadata.st_dev,
        socket_parent_inode=socket_metadata.st_ino,
        receipt_private_key_path=str(receipt_key_path),
        receipt_private_key_sha256=hashlib.sha256(receipt_key_path.read_bytes()).hexdigest(),
        reviewed_code_root=str(repository_root),
        composition_factory_module="aletheia.research_controller_kernel_command_runtime",
        composition_factory_attribute=(
            "build_transition_kernel_command_rpc_service"
            if is_transition
            else "build_action_kernel_command_rpc_service"
        ),
        composition_factory_source_path=str(factory),
        composition_factory_source_sha256=hashlib.sha256(factory.read_bytes()).hexdigest(),
        composition_config_path=str(config_path),
        composition_config_file_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        prepared_at=prepared_at,
    )
    return SimpleNamespace(
        deployment=deployment,
        config=config,
        config_path=config_path,
        pin=pin,
        binding=binding,
        kernel_key=kernel_key,
        key_path=key_path,
        receipt_key_path=receipt_key_path,
    )


def _build(fx: SimpleNamespace, *, domain: str):
    builder = (
        build_transition_kernel_command_rpc_service
        if domain == "transition"
        else build_action_kernel_command_rpc_service
    )
    return builder(deployment=fx.deployment, configuration_bytes=fx.config_path.read_bytes())


def _action_payload() -> ActionKernelCommandRPCPayload:
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    return ActionKernelCommandRPCPayload(
        proposal=_authorization_proposal(
                submission,
                expected_stream_version=submission.request.expected_stream_version + 1,
                expected_tail_event_sha256="b" * 64,
            ),
        submitted=submission,
    )


def _transition_payload() -> tuple[TransitionKernelCommandRPCPayload, object]:
    submission = _submission(ControllerStep.PROPOSE_FOLLOWUP)
    receipt = _receipt()
    event = _incorporated_event(action_id=submission.action.action_id)
    decision = _fork_decision(submission, receipt, event)
    payload = TransitionKernelCommandRPCPayload(
        proposal=_transition_proposal(submission, decision),
        submitted=submission,
        receipt=receipt,
        incorporated_event=event,
    )
    return payload, decision


def test_action_service_signs_and_resigns_the_exact_submitted_proposal(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, domain="action")
    handlers = _build(fx, domain="action")

    assert handlers.operations == (ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,)
    payload = _action_payload()
    operation = ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND
    command = handlers.handler_for(operation)(payload)

    assert command.proposal_sha256 == payload.proposal.proposal_sha256
    assert command.idempotency_key == (
        f"action:{payload.submitted.action.object_ref.object_sha256}"
    )
    assert command.source_event_key == (
        f"action-proposal:{payload.submitted.action.object_ref.object_sha256}"
    )
    assert command.principal_id == fx.kernel_key.principal_id
    assert command.authorized_at == payload.proposal.proposed_at
    # Ed25519 is deterministic and authorized_at is the proposal's own time,
    # so a resend signs byte-identical bytes
    assert handlers.handler_for(operation)(payload) == command


def test_action_service_round_trips_through_the_operation_closed_wire(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, domain="action")
    handlers = _build(fx, domain="action")
    rpc_service = ControllerWorkerRPCService(
        pin=fx.deployment.service_pin,
        controller_id=fx.deployment.controller_id,
        controller_manifest_sha256=fx.deployment.controller_manifest_sha256,
        worker_process_principal_id=fx.deployment.worker_process_principal_id,
        handlers=handlers,
        receipt_private_key=fx.receipt_key_path.read_bytes(),
        clock=lambda: fx.deployment.prepared_at,
    )
    client = ControllerWorkerRPCClient(
        pin=fx.deployment.service_pin,
        controller_id=fx.deployment.controller_id,
        controller_manifest_sha256=fx.deployment.controller_manifest_sha256,
        worker_process_principal_id=fx.deployment.worker_process_principal_id,
        transport=_DirectTransport(rpc_service),
        clock=lambda: fx.deployment.prepared_at,
    )
    payload = _action_payload()

    command = client.call(
        ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,
        payload={
            "proposal": payload.proposal.model_dump(mode="json"),
            "submitted": payload.submitted.model_dump(mode="json"),
        },
        result_type=AuthorizedResearchCommand,
    )

    assert command.idempotency_key == (
        f"action:{payload.submitted.action.object_ref.object_sha256}"
    )
    with pytest.raises(ControllerWorkerRPCBlocked) as raised:
        client.call(
            ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,
            payload={
                "proposal": payload.proposal.model_copy(
                    update={"proposed_by_principal_id": fx.kernel_key.principal_id}
                ).model_dump(mode="json"),
                "submitted": payload.submitted.model_dump(mode="json"),
            },
            result_type=AuthorizedResearchCommand,
        )
    assert raised.value.blocker_codes == ("kernel_command_authority_refusal",)


def test_action_service_signs_the_submission_carried_admission_command(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path, domain="action")
    handlers = _build(fx, domain="action")
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    action_sha = submission.action.object_ref.object_sha256

    command = handlers.handler_for(ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND)(
        ActionKernelCommandRPCPayload(
            proposal=submission.command_proposal,
            submitted=submission,
        )
    )

    # the admission command the submission carries is signed under its own
    # idempotency identity; the store admits the action through this event
    # before any authorization resolves
    assert command.proposal_sha256 == submission.command_proposal.proposal_sha256
    assert command.idempotency_key == f"action-proposed:{action_sha}"
    assert command.source_event_key == f"action-proposal:{action_sha}"
    assert command.principal_id == fx.kernel_key.principal_id


def test_a_refused_action_proposal_is_one_signed_blocker(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="action")
    handlers = _build(fx, domain="action")
    payload = _action_payload()
    rebound = payload.proposal.payload.model_copy(update={"action_id": "action:rebound"})
    refused = ActionKernelCommandRPCPayload(
        proposal=payload.proposal.model_copy(update={"payload": rebound}),
        submitted=payload.submitted,
    )

    with pytest.raises(ControllerWorkerRPCServiceBlocked) as raised:
        handlers.handler_for(ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND)(refused)
    assert raised.value.blocker_codes == ("kernel_command_authority_refusal",)

    own = ActionKernelCommandRPCPayload(
        proposal=payload.proposal.model_copy(
            update={"proposed_by_principal_id": fx.kernel_key.principal_id}
        ),
        submitted=payload.submitted,
    )
    with pytest.raises(ControllerWorkerRPCServiceBlocked):
        handlers.handler_for(ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND)(own)


def test_transition_service_signs_the_receipt_forced_transition(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="transition")
    handlers = _build(fx, domain="transition")

    assert handlers.operations == (ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,)
    payload, decision = _transition_payload()
    command = handlers.handler_for(ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND)(payload)

    assert command.idempotency_key == f"transition:{decision.decision_sha256}"
    assert command.source_event_key == f"continuation:{payload.receipt.receipt_sha256}"
    assert command.principal_id == fx.kernel_key.principal_id
    assert (
        handlers.handler_for(ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND)(payload)
        == command
    )


def test_transition_service_refuses_proposals_without_a_decision(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="transition")
    handlers = _build(fx, domain="transition")
    action = _action_payload()
    undecidable = TransitionKernelCommandRPCPayload(
        proposal=action.proposal,
        submitted=action.submitted,
        receipt=_receipt(),
        incorporated_event=_incorporated_event(action_id=action.submitted.action.action_id),
    )

    with pytest.raises(ControllerWorkerRPCServiceBlocked) as raised:
        handlers.handler_for(ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND)(undecidable)
    assert raised.value.blocker_codes == ("kernel_command_authority_refusal",)


def test_each_service_binds_exactly_its_own_operation(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="action")
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **fx.deployment.model_dump(mode="python", exclude={"runtime_id"}),
            # service_id is derived from the pin payload, so the drifted dump
            # must leave it behind and let revalidation derive the new one
            "service_pin": fx.pin.model_copy(
                update={"operations": (ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,)}
            ).model_dump(mode="python", exclude={"service_id"}),
        }
    )
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_action_kernel_command_rpc_service(
            deployment=drifted,
            configuration_bytes=fx.config_path.read_bytes(),
        )


def test_transport_receipt_key_reuse_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="action")
    rebound = {
        **fx.config,
        "command_signing_key": {
            **fx.config["command_signing_key"],
            "file_sha256": fx.deployment.receipt_private_key_sha256,
        },
    }
    config_path = fx.config_path.with_name("receipt-reuse.json")
    config_path.write_bytes(canonical_json_bytes(rebound))
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_action_kernel_command_rpc_service(
            deployment=fx.deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_signing_key_custody_drift_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="action")
    fx.key_path.chmod(0o600)
    with pytest.raises(ValueError, match="unsafe file custody"):
        build_action_kernel_command_rpc_service(
            deployment=fx.deployment,
            configuration_bytes=fx.config_path.read_bytes(),
        )


def test_authority_source_lookalike_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="action")
    lookalike = fx.config_path.with_name("kernel_authority.py")
    lookalike.write_bytes(Path(fx.config["kernel_authority_source_path"]).read_bytes())
    rebound = {
        **fx.config,
        "kernel_authority_source_path": str(lookalike),
    }
    config_path = fx.config_path.with_name("lookalike.json")
    config_path.write_bytes(canonical_json_bytes(rebound))
    with pytest.raises(ValueError, match="not the reviewed module"):
        build_action_kernel_command_rpc_service(
            deployment=fx.deployment,
            configuration_bytes=config_path.read_bytes(),
        )


def test_config_identity_drift_fails_closed(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, domain="transition")
    rebound = {**fx.config, "controller_id": "rctl_" + "8" * 32}
    config_path = fx.config_path.with_name("identity-drift.json")
    config_path.write_bytes(canonical_json_bytes(rebound))
    with pytest.raises(ValueError, match="differs from deployment or authority"):
        build_transition_kernel_command_rpc_service(
            deployment=fx.deployment,
            configuration_bytes=config_path.read_bytes(),
        )


@pytest.mark.parametrize("domain", ("action", "transition"))
def test_guarded_runtime_loads_each_factory(tmp_path: Path, domain: str) -> None:
    fx = _fixture(tmp_path, domain=domain)
    runtime = build_controller_worker_rpc_server_runtime(
        fx.deployment, clock=lambda: fx.deployment.prepared_at
    )
    assert runtime.deployment == fx.deployment
    assert not Path(fx.deployment.service_pin.socket_path).exists()


@pytest.mark.parametrize("domain", ("action", "transition"))
def test_guarded_runtime_rejects_factory_source_drift(tmp_path: Path, domain: str) -> None:
    fx = _fixture(tmp_path, domain=domain)
    drifted = ControllerWorkerRPCServerDeployment.model_validate(
        {
            **fx.deployment.model_dump(mode="python", exclude={"runtime_id"}),
            "composition_factory_source_sha256": _sha(f"drifted-{domain}-factory"),
        }
    )
    with pytest.raises(ControllerWorkerRPCProcessError, match="byte pin"):
        build_controller_worker_rpc_server_runtime(drifted)
