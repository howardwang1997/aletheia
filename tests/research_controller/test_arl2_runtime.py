"""Acceptance tests for the ARL-2 question-campaign driver and its entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import aletheia.arl2_runtime as arl2_runtime_module
from aletheia.arl2_runtime import (
    ARL2KernelCommandServiceSetV1,
    ARL2QuestionCampaignDriver,
    ARL2QuestionCampaignRequestV1,
    ARL2QuestionCampaignRuntimeConfigV1,
    ARL2QuestionCampaignRuntimeDeploymentV1,
    ARL2QuestionCampaignRunReceiptV1,
    ARL2RuntimeError,
    _campaign_transition_proposal,
    action_authorization_proposal,
    execute_arl2_question_campaign_deployment,
    load_arl2_question_campaign_runtime_deployment,
)
from aletheia.research_controller.contracts import (
    ControllerStep,
    ResearchControllerLaunchRequest,
)
from aletheia.research_controller.continuation import (
    ContinuationDisposition,
    ContinuationReceipt,
)
from aletheia.research_controller.continuation_stop import (
    StopGrounds,
    StopTriggerCode,
)
from aletheia.research_controller.external_rpc import ControllerWorkerRPCOperation
from aletheia.research_controller.external_rpc_server import ControllerWorkerRPCService
from aletheia.research_kernel.commands import (
    ResearchCommandProposal,
    authorize_research_proposal,
)
from aletheia.research_kernel.policy import ResearchAuthorizationRole
from aletheia.research_kernel.schemas import (
    ActionKind,
    EventType,
    EvidenceKind,
    EvidenceRef,
    ObservationIncorporatedPayload,
    QuestionAdmittedPayload,
    QuestionKind,
    ResearchEvent,
    ResearchQuestionVersion,
    StopReason,
    canonical_json_bytes,
    canonical_sha256,
)

_TEST_ROOT = Path(__file__).resolve().parent
_KERNEL_TESTS = Path(__file__).resolve().parents[1] / "research_kernel"
for _path in (_TEST_ROOT, _KERNEL_TESTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from test_action_proposals import (  # noqa: E402
    NOW,
    QUEST_ID,
    SLOT_ID,
    _sha,
)
from test_campaign_replay import _fixture as _replay_fixture  # noqa: E402
from test_kernel_command_authority import (  # noqa: E402
    _authorization_proposal,
    _incorporated_event,
    _receipt as _fork_receipt,
    _submission,
)
from test_kernel_command_runtime import (  # noqa: E402
    _build as _build_kernel_commands,
    _fixture as _kernel_command_fixture,
)
from test_store import (  # noqa: E402
    T0,
    _PRIVATE_KEYS as store_private_keys,
    _authorize,
    _problem_command,
    _quest_fixture,
    _role_key,
    _store,
)

_DRIVER_PRINCIPAL = "service:arl2-driver"


def _source_pin(path: Path) -> tuple[str, str]:
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pinned(path: Path, payload: bytes) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path), hashlib.sha256(payload).hexdigest()


class _RoutingTransport:
    """Exchange each request with the one service that owns its operation."""

    def __init__(self, services: dict[str, ControllerWorkerRPCService]) -> None:
        self.services = services
        self.exchanges = 0

    def exchange(self, _pin, request_bytes: bytes) -> bytes:
        operation = json.loads(request_bytes)["operation"]
        self.exchanges += 1
        return self.services[operation].handle(request_bytes)


def _driver_fixture(tmp_path: Path) -> SimpleNamespace:
    """One complete, DB-free ARL-2 deployment: quest, services, config, request."""

    repository_root = Path(__file__).resolve().parents[2]
    package_root = Path(arl2_runtime_module.__file__).resolve().parent
    process_uid = os.geteuid()
    process_gid = os.getegid()

    replay = _replay_fixture()
    card_bytes = canonical_json_bytes(replay.card)
    card_path, card_sha = _write_pinned(tmp_path / "dataset-card.json", card_bytes)
    content_path, content_sha = _write_pinned(tmp_path / "dataset-content.csv", replay.csv_bytes)

    archive, scope, charter, charter_command, root_branch_id, trust_root, policy = _quest_fixture(
        tmp_path / "quest-archive", label="arl2-driver", quest_id=QUEST_ID
    )
    problem, problem_command = _problem_command(
        archive=archive,
        scope=scope,
        charter=charter,
        trust_root=trust_root,
        policy=policy,
        root_branch_id=root_branch_id,
        expected_version=1,
        expected_tail=_sha("charter-event"),
        label="arl2-driver",
    )
    grounding = tuple(
        sorted(
            {
                _sha("arl2-grounding-one"),
                _sha("arl2-grounding-two"),
                _sha("arl2-grounding-three"),
            }
        )
    )
    question = ResearchQuestionVersion(
        question_id=f"question:{'c' * 32}",
        quest_id=QUEST_ID,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind.COMPARATIVE,
        statement="Does plane doping raise the pinned cuprate transition temperature?",
        scope="the two pre-staged rounds of the pinned plane-doping dataset",
        answer_space=("discriminated-plane-effect", "no-discernible-effect"),
        scientific_value="separates the plane-doping channel from measurement noise",
        falsifiability="each round's staged rows fix the discriminating statistic in advance",
        evidence_refs=tuple(
            EvidenceRef(
                kind=EvidenceKind.POLICY,
                object_sha256=sha,
                object_id=f"grounding:{sha[:32]}",
            )
            for sha in grounding
        ),
        semantic_delta="initial question version for the bounded campaign",
        authored_by_principal_id="human:principal-investigator",
        authored_at=T0 + timedelta(seconds=3),
    )
    archive.archive_object(question)
    question_proposal = ResearchCommandProposal(
        quest_id=QUEST_ID,
        scope_binding=scope,
        expected_stream_version=2,
        expected_tail_event_sha256=_sha("problem-event"),
        event_type=EventType.QUESTION_ADMITTED,
        payload=QuestionAdmittedPayload(
            question_ref=question.object_ref,
            branch_id=root_branch_id,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=T0 + timedelta(seconds=2),
    )
    question_command = authorize_research_proposal(
        question_proposal,
        idempotency_key="arl2:question-admission",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role_key(policy, ResearchAuthorizationRole.ORDINARY).key_id,
        private_key=store_private_keys[ResearchAuthorizationRole.ORDINARY],
        authorized_at=T0 + timedelta(seconds=2),
        source_event_key="arl2:question-admission",
    )
    charter_path, charter_sha = _write_pinned(
        tmp_path / "commands" / "charter.json", canonical_json_bytes(charter_command)
    )
    problem_path, problem_sha = _write_pinned(
        tmp_path / "commands" / "problem.json", canonical_json_bytes(problem_command)
    )
    question_path, question_sha = _write_pinned(
        tmp_path / "commands" / "question.json", canonical_json_bytes(question_command)
    )

    action_fx = _kernel_command_fixture(tmp_path, domain="action")
    transition_fx = _kernel_command_fixture(tmp_path, domain="transition")
    service_uid = process_uid + 1

    def _driver_facing_pin(pin):
        # SO_PEERCRED names the SERVICE as the peer, so a driver-facing pin
        # carries the service uid as both peer and socket owner.
        payload = pin.model_dump(mode="python", exclude={"service_id"})
        payload["peer_uid"] = service_uid
        payload["socket_owner_uid"] = service_uid
        return type(pin).model_validate(payload)

    raw_services = ARL2KernelCommandServiceSetV1(
        action_kernel_command=_driver_facing_pin(action_fx.deployment.service_pin),
        transition_kernel_command=_driver_facing_pin(transition_fx.deployment.service_pin),
    )

    archive_stat = os.stat(tmp_path / "quest-archive")
    spool_root = tmp_path / "action-proposal-spool"
    spool_root.mkdir()
    bundle_root = tmp_path / "bundle-output"
    bundle_root.mkdir()
    role_invocations = tuple(
        arl2_runtime_module.ARL2RoleInvocationV1(
            role=role,
            deployment_manifest_path=str(tmp_path / f"role-{role}.json"),
            deployment_manifest_file_sha256=_sha(f"arl2-role-{role}"),
        )
        for role in (
            "kernel_dispatcher",
            "terminal_dispatcher",
            "worker",
            "delivery_reconciler",
        )
    )
    config = ARL2QuestionCampaignRuntimeConfigV1(
        process_principal_id=_DRIVER_PRINCIPAL,
        process_uid=process_uid,
        process_gid=process_gid,
        controller_id=action_fx.deployment.controller_id,
        controller_manifest_sha256=action_fx.deployment.controller_manifest_sha256,
        controller_principal_id="service:research-controller",
        controller_manifest_path=str(tmp_path / "controller-manifest.json"),
        controller_manifest_file_sha256=_sha("arl2-controller-manifest"),
        database_url_sha256=_sha("arl2-database-url"),
        schema_revision="20260916_0001",
        kernel_writer=arl2_runtime_module.ARL2KernelWriterConfigV1(
            trust_root=trust_root,
            cas_root=str(tmp_path / "quest-archive"),
            cas_owner_uid=archive_stat.st_uid,
            cas_group_gid=archive_stat.st_gid,
            cas_device_id=archive_stat.st_dev,
            cas_inode=archive_stat.st_ino,
            max_object_bytes=64 * 1024 * 1024,
        ),
        kernel_command_services=raw_services,
        charter_command_path=charter_path,
        charter_command_file_sha256=charter_sha,
        problem_command_path=problem_path,
        problem_command_file_sha256=problem_sha,
        question_command_path=question_path,
        question_command_file_sha256=question_sha,
        action_proposal_spool_root=str(spool_root),
        runtime_entrypoint_path=str(repository_root / "scripts" / "run-arl2-question-campaign.py"),
        runtime_entrypoint_file_sha256=hashlib.sha256(
            (repository_root / "scripts" / "run-arl2-question-campaign.py").read_bytes()
        ).hexdigest(),
        role_invocations=role_invocations,
        bundle_output_root=str(bundle_root),
        replay_implementation_source_path=str(
            package_root / "research_controller" / "campaign_replay.py"
        ),
        replay_implementation_source_sha256=hashlib.sha256(
            (package_root / "research_controller" / "campaign_replay.py").read_bytes()
        ).hexdigest(),
        data_registration_source_path=str(package_root / "protocols" / "data_registration.py"),
        data_registration_source_sha256=hashlib.sha256(
            (package_root / "protocols" / "data_registration.py").read_bytes()
        ).hexdigest(),
        world_model_revision_source_path=str(
            package_root / "research_controller" / "world_model_revision.py"
        ),
        world_model_revision_source_sha256=hashlib.sha256(
            (package_root / "research_controller" / "world_model_revision.py").read_bytes()
        ).hexdigest(),
        experiment_selection_source_path=str(
            package_root / "research_controller" / "experiment_selection.py"
        ),
        experiment_selection_source_sha256=hashlib.sha256(
            (package_root / "research_controller" / "experiment_selection.py").read_bytes()
        ).hexdigest(),
        campaign_deadline=NOW + timedelta(minutes=30),
        prepared_at=NOW,
    )
    request = ARL2QuestionCampaignRequestV1(
        quest_id=QUEST_ID,
        launch_request=ResearchControllerLaunchRequest(
            program_id="prg_" + "6" * 32,
            quest_id=QUEST_ID,
            idempotency_key="launch:arl2-question-campaign",
            expected_stream_version=3,
            expected_tail_event_sha256=_sha("question-event"),
            expected_snapshot_sha256=_sha("question-snapshot"),
        ),
        question_version=question,
        grounding_object_sha256s=grounding,
        dataset_card_path=card_path,
        dataset_card_file_sha256=card_sha,
        dataset_content_path=content_path,
        dataset_content_file_sha256=content_sha,
        round_split_bindings=replay.bindings,
        staged_rounds=replay.staged_rounds,
    )
    config_path, _config_file_sha = _write_pinned(
        tmp_path / "runtime-configuration.json", canonical_json_bytes(config)
    )
    request_path, _request_file_sha = _write_pinned(
        tmp_path / "campaign-request.json", canonical_json_bytes(request)
    )
    deployment = ARL2QuestionCampaignRuntimeDeploymentV1(
        configuration_path=config_path,
        configuration_file_sha256=_config_file_sha,
        configuration_sha256=config.configuration_sha256,
        request_path=request_path,
        request_file_sha256=_request_file_sha,
        request_sha256=request.request_sha256,
        process_principal_id=_DRIVER_PRINCIPAL,
        process_uid=process_uid,
        process_gid=process_gid,
        prepared_at=NOW,
        campaign_deadline=NOW + timedelta(minutes=30),
    )
    deployment_path, deployment_sha = _write_pinned(
        tmp_path / "runtime-deployment.json", canonical_json_bytes(deployment)
    )

    handlers = {
        "sign_action_command": _build_kernel_commands(action_fx, domain="action"),
        "sign_transition_command": _build_kernel_commands(transition_fx, domain="transition"),
    }
    services = {
        operation: ControllerWorkerRPCService(
            pin=pin,
            controller_id=action_fx.deployment.controller_id,
            controller_manifest_sha256=action_fx.deployment.controller_manifest_sha256,
            worker_process_principal_id=_DRIVER_PRINCIPAL,
            handlers=handlers[operation],
            receipt_private_key=receipt_key.read_bytes(),
            clock=lambda: NOW,
        )
        for operation, pin, receipt_key in (
            (
                "sign_action_command",
                config.kernel_command_services.action_kernel_command,
                action_fx.receipt_key_path,
            ),
            (
                "sign_transition_command",
                config.kernel_command_services.transition_kernel_command,
                transition_fx.receipt_key_path,
            ),
        )
    }
    return SimpleNamespace(
        repository_root=repository_root,
        process_uid=process_uid,
        process_gid=process_gid,
        replay=replay,
        archive=archive,
        scope=scope,
        charter=charter,
        charter_command=charter_command,
        root_branch_id=root_branch_id,
        trust_root=trust_root,
        policy=policy,
        problem=problem,
        problem_command=problem_command,
        question=question,
        question_command=question_command,
        question_proposal=question_proposal,
        problem_path=problem_path,
        question_path=question_path,
        spool_root=spool_root,
        config=config,
        config_path=config_path,
        request=request,
        request_path=request_path,
        deployment=deployment,
        deployment_path=deployment_path,
        deployment_sha256=deployment_sha,
        action_fx=action_fx,
        transition_fx=transition_fx,
        services=services,
        transport=_RoutingTransport(services),
    )


def _config_variant(config: ARL2QuestionCampaignRuntimeConfigV1, **updates) -> object:
    payload = {**config.model_dump(mode="python", exclude={"configuration_id"}), **updates}
    return ARL2QuestionCampaignRuntimeConfigV1.model_validate(payload)


def _request_variant(request: ARL2QuestionCampaignRequestV1, **updates) -> object:
    payload = {**request.model_dump(mode="python", exclude={"request_id"}), **updates}
    return ARL2QuestionCampaignRequestV1.model_validate(payload)


def _driver(fx: SimpleNamespace, **overrides) -> ARL2QuestionCampaignDriver:
    return ARL2QuestionCampaignDriver(
        config=overrides.pop("config", fx.config),
        request=overrides.pop("request", fx.request),
        deployment_sha256=fx.deployment_sha256,
        kernel_store=overrides.pop("kernel_store", SimpleNamespace()),
        kernel_archive=overrides.pop("kernel_archive", fx.archive),
        **overrides,
    )


def _services_payload(services: ARL2KernelCommandServiceSetV1) -> dict:
    """Dump the service set so each pin re-derives its own service id."""

    payload = services.model_dump(mode="python")
    for name in ("action_kernel_command", "transition_kernel_command"):
        payload[name].pop("service_id", None)
    return payload


def test_kernel_command_services_bind_exactly_one_operation_each(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    assert fx.config.kernel_command_services.action_kernel_command.operations == (
        ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,
    )
    assert fx.config.kernel_command_services.transition_kernel_command.operations == (
        ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,
    )
    swapped = _services_payload(fx.config.kernel_command_services)
    swapped["action_kernel_command"]["operations"] = [
        ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND.value
    ]
    rebound = {
        **fx.config.model_dump(mode="python"),
        "kernel_command_services": swapped,
    }
    with pytest.raises(ValueError, match="another operation set"):
        ARL2QuestionCampaignRuntimeConfigV1.model_validate(rebound)


def test_config_refuses_service_pins_facing_the_driver_uid(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    # A pin whose SO_PEERCRED peer is this driver itself is refused, and so is
    # any loosening of the UID-separated, GID-shared socket: all five clauses.
    mutations = {
        "peer uid names the driver": {"peer_uid": fx.process_uid},
        "socket owner is not the peer": {"socket_owner_uid": fx.process_uid},
        "socket group leaves the driver group": {"socket_group_gid": fx.process_gid + 1},
        "peer group leaves the driver group": {"peer_gid": fx.process_gid + 1},
        "socket mode loosens": {"socket_mode": 0o600},
    }
    for mutation in mutations.values():
        services = _services_payload(fx.config.kernel_command_services)
        services["transition_kernel_command"].update(mutation)
        with pytest.raises(ValueError, match="UID-separated, GID-shared socket"):
            _config_variant(fx.config, kernel_command_services=services)


def test_config_refuses_overlapping_custody_roots(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    nested = str(Path(fx.config.kernel_writer.cas_root) / "nested-bundles")
    with pytest.raises(ValueError, match="custody roots overlap"):
        _config_variant(fx.config, bundle_output_root=nested)


def test_request_pins_the_three_pre_signed_activation_events(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    launch = fx.request.launch_request.model_dump(mode="python")
    launch["expected_stream_version"] = 2
    with pytest.raises(ValueError, match="three pre-signed activation events"):
        _request_variant(fx.request, launch_request=launch)
    with pytest.raises(ValueError, match="activation command paths must be distinct"):
        _config_variant(fx.config, problem_command_path=fx.config.charter_command_path)


def test_activation_commands_must_agree_with_the_pinned_question(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    driver = _driver(fx)
    charter, problem, question = driver._activation_commands()
    assert charter.payload.charter_ref.object_sha256 == fx.charter.object_ref.object_sha256
    assert problem.payload.problem_ref.object_sha256 == fx.problem.object_ref.object_sha256
    assert question.payload.question_ref.object_sha256 == fx.question.object_sha256

    other_question = fx.question.model_copy(
        update={"statement": "Does plane doping lower the pinned transition temperature?"}
    )
    driver = _driver(fx, request=_request_variant(fx.request, question_version=other_question))
    with pytest.raises(ARL2RuntimeError, match="question command names another question version"):
        driver._activation_commands()
    other_problem = fx.problem.model_copy(update={"title": "another bounded problem"})
    rebound_question = fx.question.model_copy(update={"problem_ref": other_problem.object_ref})
    driver = _driver(fx, request=_request_variant(fx.request, question_version=rebound_question))
    with pytest.raises(ARL2RuntimeError, match="problem command names another problem version"):
        driver._activation_commands()
    other_charter = fx.charter.model_copy(update={"mission": "another bounded mission"})
    rebound_question = fx.question.model_copy(update={"charter_ref": other_charter.object_ref})
    driver = _driver(fx, request=_request_variant(fx.request, question_version=rebound_question))
    with pytest.raises(ARL2RuntimeError, match="charter command names another charter version"):
        driver._activation_commands()


def test_driver_pins_the_reviewed_implementations(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    _driver(fx)
    with pytest.raises(ARL2RuntimeError, match="byte pin"):
        _driver(
            fx,
            config=_config_variant(
                fx.config,
                replay_implementation_source_sha256=_sha("drifted-replay-implementation"),
            ),
        )
    package_root = Path(arl2_runtime_module.__file__).resolve().parent
    with pytest.raises(ARL2RuntimeError, match="resolves another module"):
        _driver(
            fx,
            config=_config_variant(
                fx.config,
                data_registration_source_path=str(
                    package_root / "research_controller" / "campaign_replay.py"
                ),
            ),
        )


def test_deployment_loads_only_its_pinned_canonical_bytes(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    loaded = load_arl2_question_campaign_runtime_deployment(
        fx.deployment_path, expected_file_sha256=fx.deployment_sha256
    )
    assert loaded == fx.deployment
    assert loaded.deployment_id.startswith("arl2d_")
    with pytest.raises(ARL2RuntimeError, match="byte pin"):
        load_arl2_question_campaign_runtime_deployment(
            fx.deployment_path, expected_file_sha256=_sha("another-deployment")
        )
    drifted = tmp_path / "runtime-deployment-drifted.json"
    deployment_bytes = Path(fx.deployment_path).read_bytes()
    drifted.write_bytes(deployment_bytes + b"\n")
    with pytest.raises(ARL2RuntimeError, match="canonical"):
        load_arl2_question_campaign_runtime_deployment(
            drifted,
            expected_file_sha256=hashlib.sha256(drifted.read_bytes()).hexdigest(),
        )


def test_execute_refuses_foreign_platforms_and_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _driver_fixture(tmp_path)
    monkeypatch.setattr(arl2_runtime_module.sys, "platform", "darwin")
    with pytest.raises(ARL2RuntimeError, match="requires Linux"):
        execute_arl2_question_campaign_deployment(fx.deployment)
    monkeypatch.setattr(arl2_runtime_module.sys, "platform", "linux")
    deployment = ARL2QuestionCampaignRuntimeDeploymentV1.model_validate(
        {
            **fx.deployment.model_dump(mode="python", exclude={"deployment_id"}),
            "process_uid": fx.process_uid + 1,
        }
    )
    with pytest.raises(ARL2RuntimeError, match="process identity differs"):
        execute_arl2_question_campaign_deployment(deployment)


def _role_manifest_config(fx: SimpleNamespace, tmp_path: Path):
    """Re-pin the four role manifests as real files the wake loop can verify."""

    updated = []
    for invocation in fx.config.role_invocations:
        path = tmp_path / f"role-manifest-{invocation.role}.json"
        _write_pinned(path, b"{}")
        updated.append(
            arl2_runtime_module.ARL2RoleInvocationV1(
                role=invocation.role,
                deployment_manifest_path=str(path),
                deployment_manifest_file_sha256=hashlib.sha256(b"{}").hexdigest(),
            )
        )
    return _config_variant(fx.config, role_invocations=tuple(updated))


def test_wake_roles_verifies_the_entrypoint_bytes_first(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    invoked: list[list[str]] = []
    driver = _driver(
        fx,
        config=_config_variant(
            fx.config, runtime_entrypoint_file_sha256=_sha("drifted-entrypoint")
        ),
        runner=invoked.append,
    )
    with pytest.raises(ARL2RuntimeError, match="runtime entrypoint changed or differs"):
        driver._wake_roles()
    assert invoked == []


def test_wake_roles_runs_each_pinned_role_manifest_once(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    invoked: list[list[str]] = []
    config = _role_manifest_config(fx, tmp_path)
    driver = _driver(fx, config=config, runner=invoked.append)
    driver._wake_roles()
    assert len(invoked) == 4
    for command, invocation in zip(invoked, config.role_invocations, strict=True):
        assert command[1] == config.runtime_entrypoint_path
        assert command[3] == invocation.deployment_manifest_path


def test_wake_roles_deadline_moves_with_each_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _driver_fixture(tmp_path)
    timeouts: list[float] = []
    now = {"value": NOW}

    def fake_run(_command, *, capture_output, check, timeout):  # noqa: ANN001
        timeouts.append(timeout)
        now["value"] = now["value"] + timedelta(minutes=10)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(arl2_runtime_module.subprocess, "run", fake_run)
    driver = _driver(fx, config=_role_manifest_config(fx, tmp_path), clock=lambda: now["value"])
    driver._wake_roles()
    # Each invocation gets its own remainder of the 30-minute deadline; a
    # shared remainder (the bug this closes) would log 1800 four times, and
    # the fourth reaches the deadline and clamps to the 1-second floor.
    assert timeouts == [1800.0, 1200.0, 600.0, 1.0]


def test_entrypoint_refuses_without_explicit_apply(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(fx.repository_root / "scripts" / "run-arl2-question-campaign.py"),
            "--deployment-manifest",
            str(fx.deployment_path),
            "--deployment-manifest-sha256",
            fx.deployment_sha256,
        ],
        capture_output=True,
        check=False,
        timeout=90,
        env={**os.environ, "PYTHONPATH": str(fx.repository_root)},
    )
    assert completed.returncode != 0
    assert b"refusing ARL-2 campaign" in completed.stderr + completed.stdout


def test_action_authorization_matches_the_b4_canon() -> None:
    submission = _submission(ControllerStep.PROPOSE_ACTION)
    version, tail = submission.request.expected_stream_version + 1, "b" * 64
    assert action_authorization_proposal(
        submission,
        expected_stream_version=version,
        expected_tail_event_sha256=tail,
    ) == _authorization_proposal(
        submission,
        expected_stream_version=version,
        expected_tail_event_sha256=tail,
    )


def _stop_receipt() -> ContinuationReceipt:
    return ContinuationReceipt(
        world_model_snapshot_sha256=_sha("world-model"),
        observation_projection_sha256=_sha("observation-projection"),
        scientific_slot_id=SLOT_ID,
        assessments=(),
        disposition=ContinuationDisposition.STOP_REQUIRED,
        reason_codes=("stop_policy_round_budget_exhausted",),
        proposed_action_kind=ActionKind.STOP,
        stop_grounds=StopGrounds(
            stop_policy_sha256=_sha("stop-policy"),
            trigger=StopTriggerCode.ROUND_BUDGET_EXHAUSTED,
            stop_reason=StopReason.BUDGET_EXHAUSTED,
            rounds_observed=2,
            reopen_conditions=("new-discriminating-observable", "revised-hypothesis-set"),
        ),
    )


def _refine_receipt(
    snapshot_sha: str = _sha("world-model"),
    projection_sha: str = _sha("observation-projection"),
) -> ContinuationReceipt:
    return ContinuationReceipt(
        world_model_snapshot_sha256=snapshot_sha,
        observation_projection_sha256=projection_sha,
        scientific_slot_id=SLOT_ID,
        assessments=(),
        disposition=ContinuationDisposition.REDESIGN_OBSERVABLE,
        reason_codes=("observable_redesign_required",),
        proposed_action_kind=ActionKind.REFINE,
    )


def test_transitions_carry_only_the_two_receipt_forced_kinds() -> None:
    assert ARL2QuestionCampaignRuntimeConfigV1.model_fields["driver_side_stop_allowed"].default is (
        False
    )
    submission = _submission(ControllerStep.PROPOSE_FOLLOWUP)
    event = _incorporated_event(action_id=submission.action.object_ref.object_id)

    stop_receipt = _stop_receipt()
    stop_proposal = _campaign_transition_proposal(
        submission,
        stop_receipt,
        event,
        expected_stream_version=8,
        expected_tail_event_sha256=_sha("live-head"),
    )
    assert stop_proposal.event_type is EventType.STOP_COMMITTED
    # The transition pins name the caller's live kernel head, never the stale
    # pins the original action submission carried (submission pins: 7 / "tail").
    assert stop_proposal.expected_stream_version == 8
    assert stop_proposal.expected_tail_event_sha256 == _sha("live-head")
    decision = stop_proposal.payload.decision
    assert decision.directive.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert decision.directive.branch_id == submission.target_branch_id
    assert decision.transition_id == (
        "transition:"
        + hashlib.sha256(f"arl2-stop:{stop_receipt.receipt_sha256}".encode()).hexdigest()[:32]
    )

    refine_receipt = _refine_receipt()
    refine_proposal = _campaign_transition_proposal(
        submission,
        refine_receipt,
        event,
        expected_stream_version=8,
        expected_tail_event_sha256=_sha("live-head"),
    )
    assert refine_proposal.event_type is EventType.REFINE_COMMITTED
    assert refine_proposal.payload.decision.directive.source_branch_id == (
        submission.target_branch_id
    )
    assert refine_proposal.payload.decision.directive.child_branch_id == (
        "rbr_"
        + hashlib.sha256(f"arl2-refine-child:{refine_receipt.receipt_sha256}".encode()).hexdigest()[
            :32
        ]
    )

    with pytest.raises(ARL2RuntimeError, match="no other transition disposition"):
        _campaign_transition_proposal(
            submission,
            _fork_receipt(),
            event,
            expected_stream_version=8,
            expected_tail_event_sha256=_sha("live-head"),
        )
    ready = ContinuationReceipt(
        world_model_snapshot_sha256=_sha("world-model"),
        observation_projection_sha256=_sha("observation-projection"),
        scientific_slot_id=SLOT_ID,
        assessments=(),
        disposition=ContinuationDisposition.READY,
        reason_codes=("ready",),
        proposed_action_kind=ActionKind.CONTINUE,
    )
    with pytest.raises(ARL2RuntimeError, match="no other transition disposition"):
        _campaign_transition_proposal(
            submission,
            ready,
            event,
            expected_stream_version=8,
            expected_tail_event_sha256=_sha("live-head"),
        )


def _decoy_observation_event(index: int) -> ResearchEvent:
    """A decoy incorporated observation the receipt lookup must not match."""

    return ResearchEvent(
        quest_id=QUEST_ID,
        sequence=3 + index,
        parent_event_sha256=_sha(f"cas-decoy-parent-{index}"),
        event_type=EventType.OBSERVATION_INCORPORATED,
        payload=ObservationIncorporatedPayload(
            branch_id="rbr_" + f"{index + 1:032x}",
            action_id=f"cas-decoy-action-{index}",
            scientific_slot_id=SLOT_ID if index == 0 else "sos_" + f"{index + 1:032x}",
            committed_admission_sha256=_sha(f"cas-decoy-admission-{index}"),
            scientific_observation_sha256=_sha(f"cas-decoy-observation-{index}"),
            outcome="inconclusive",
            source_world_model_sha256=_sha(f"cas-decoy-world-{index}"),
        ),
        command_sha256=_sha(f"cas-decoy-command-{index}"),
        principal_id="agent:operator",
        authorization_receipt_sha256=_sha(f"cas-decoy-receipt-{index}"),
        committed_at=NOW,
    )


class _CASKernelStore:
    """Kernel-store double enforcing the real CAS, admission, and receipt rules.

    The real store refuses a command unless its pins name the current head
    (research_store store.commit); this double asserts the same equality, so a
    driver proposing a transition on stale pins fails here rather than only in
    production.  It also enforces the admission rule the real store applies to
    every ACTION_AUTHORIZED commit (_resolved_action): the action must already
    sit on the stream through its ACTION_PROPOSED event, so an authorization
    without its admission fails here rather than only in production.  And it
    models the receipt-identity lookup (_find_existing_command): a command
    whose idempotency_key OR source_event_key matches a persisted receipt
    bound to different content fails here rather than only in production —
    the exact collision a second command sharing the first one's source key
    would hit.  The initial head comes from the submission's request pins
    (the canon projection pins 7 / "tail"), standing in for the seven audited
    events a real quest stream would already carry.
    """

    def __init__(self, submission, incorporated: ResearchEvent) -> None:
        self.events = [incorporated, *(_decoy_observation_event(i) for i in range(6))]
        self.head = (
            submission.request.expected_stream_version,
            submission.request.expected_tail_event_sha256,
        )
        self.commits: list[object] = []
        self._receipts: dict[str, object] = {}

    def audit(self, quest_id: str) -> SimpleNamespace:
        return SimpleNamespace(events=tuple(self.events))

    def commit(self, command) -> None:
        expected = (command.expected_stream_version, command.expected_tail_event_sha256)
        assert expected == self.head, f"stale kernel head pin: {expected} != {self.head}"
        for key in (command.idempotency_key, command.source_event_key):
            if key is None:
                continue
            persisted = self._receipts.get(key)
            assert persisted is None or persisted == command, (
                "research command idempotency/source identity is bound to "
                f"different content: {key}"
            )
        if command.event_type is EventType.ACTION_AUTHORIZED:
            admitted = {
                event.payload.action_ref.object_id
                for event in self.events
                if event.event_type is EventType.ACTION_PROPOSED
            }
            assert command.payload.action_id in admitted, (
                "action authorization does not resolve to one admitted action: "
                f"{command.payload.action_id}"
            )
        self.commits.append(command)
        self._receipts[command.idempotency_key] = command
        if command.source_event_key is not None:
            self._receipts[command.source_event_key] = command
        self.events.append(
            ResearchEvent(
                quest_id=command.quest_id,
                sequence=len(self.events) + 1,
                parent_event_sha256=self.head[1],
                event_type=command.event_type,
                payload=command.payload,
                command_sha256=_sha(f"cas-command-{len(self.commits)}"),
                principal_id=command.principal_id,
                authorization_receipt_sha256=command.authorization_receipt_sha256,
                committed_at=NOW,
            )
        )
        self.head = (len(self.events), self.events[-1].event_sha256)


def test_carry_signing_work_round_trips_through_both_services(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    refine_receipt = _refine_receipt()
    second_refine_receipt = _refine_receipt(projection_sha=_sha("projection-two"))
    submission = _submission(ControllerStep.PROPOSE_REDESIGN)
    action_sha = submission.action.object_ref.object_sha256
    (fx.spool_root / "pending").mkdir()
    (fx.spool_root / "pending" / f"{action_sha}.json").write_bytes(canonical_json_bytes(submission))
    incorporated = _incorporated_event(action_id=submission.action.object_ref.object_id)
    store = _CASKernelStore(submission, incorporated)
    carried = {"receipt": refine_receipt}
    driver = _driver(
        fx,
        transport=fx.transport,
        kernel_store=store,
        clock=lambda: NOW,
        receipt_lookup=lambda _quest, _sha256: (
            carried["receipt"],
            _sha("admission"),
            _sha("observation"),
        ),
    )

    # Round 1: the admission commits on the submission pins (7 / tail), the
    # authorization commits on the live head the admission just moved
    # (8 / proposed event), then the transition commits on the live head the
    # authorization just moved (9 / authorized event).
    driver._carry_signing_work(store.audit(QUEST_ID).events)
    assert fx.transport.exchanges == 3
    assert len(store.commits) == 3
    # the action object is staged in the writer archive before the admission
    # commits, so the event's action_ref resolves to authored content
    staged = fx.archive.load_object(submission.action.object_ref)
    assert staged.payload == submission.action
    proposed_command, action_command, transition_command = store.commits
    assert proposed_command.idempotency_key == f"action-proposed:{action_sha}"
    assert proposed_command.event_type is EventType.ACTION_PROPOSED
    # the signed admission is the submission's own command, byte-exact
    assert proposed_command.proposal_sha256 == submission.command_proposal.proposal_sha256
    assert proposed_command.payload == submission.command_proposal.payload
    assert proposed_command.principal_id == fx.action_fx.kernel_key.principal_id
    assert proposed_command.expected_stream_version == 7
    assert proposed_command.expected_tail_event_sha256 == _sha("tail")
    assert action_command.idempotency_key == f"action:{action_sha}"
    assert action_command.principal_id == fx.action_fx.kernel_key.principal_id
    assert action_command.expected_stream_version == 8
    assert action_command.expected_tail_event_sha256 == store.events[7].event_sha256
    assert transition_command.idempotency_key.startswith("transition:")
    assert transition_command.expected_stream_version == 9
    assert transition_command.expected_tail_event_sha256 == store.events[8].event_sha256
    refine_decision = transition_command.payload.decision
    assert refine_decision.transition_id == (
        "transition:"
        + hashlib.sha256(f"arl2-refine:{refine_receipt.receipt_sha256}".encode()).hexdigest()[:32]
    )
    assert refine_decision.directive.source_branch_id == submission.target_branch_id

    # Round 2: all three artifacts are already on the stream; nothing new is
    # signed.
    driver._carry_signing_work(store.audit(QUEST_ID).events)
    assert fx.transport.exchanges == 3
    assert len(store.commits) == 3

    # Round 3: a second, distinct redesign receipt (same world model, so the
    # authority still binds it to the same incorporated event) derives a new
    # transition id and commits exactly one more transition.
    carried["receipt"] = second_refine_receipt
    driver._carry_signing_work(store.audit(QUEST_ID).events)
    assert fx.transport.exchanges == 4
    assert len(store.commits) == 4
    second_decision = store.commits[3].payload.decision
    assert second_decision.transition_id != refine_decision.transition_id
    assert second_decision.transition_id == (
        "transition:"
        + hashlib.sha256(
            f"arl2-refine:{second_refine_receipt.receipt_sha256}".encode()
        ).hexdigest()[:32]
    )


def test_carry_skips_transitions_whose_admission_binds_no_event(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    submission = _submission(ControllerStep.PROPOSE_REDESIGN)
    action_sha = submission.action.object_ref.object_sha256
    (fx.spool_root / "pending").mkdir()
    (fx.spool_root / "pending" / f"{action_sha}.json").write_bytes(canonical_json_bytes(submission))
    incorporated = _incorporated_event(action_id=submission.action.object_ref.object_id)
    store = _CASKernelStore(submission, incorporated)
    driver = _driver(
        fx,
        transport=fx.transport,
        kernel_store=store,
        clock=lambda: NOW,
        receipt_lookup=lambda _quest, _sha256: (
            _refine_receipt(),
            _sha("admission-no-event-names"),
            _sha("observation"),
        ),
    )

    driver._carry_signing_work(store.audit(QUEST_ID).events)

    # The admission and the action still commit; the receipt's admission sha
    # binds no incorporated event on the stream, so no transition is signed.
    assert fx.transport.exchanges == 2
    assert len(store.commits) == 2
    assert store.commits[0].idempotency_key == f"action-proposed:{action_sha}"
    assert store.commits[1].idempotency_key == f"action:{action_sha}"


def test_run_receipt_kind_carries_exactly_its_evidence() -> None:
    common = {
        "quest_id": QUEST_ID,
        "configuration_sha256": _sha("configuration"),
        "deployment_sha256": _sha("deployment"),
        "charter_command_sha256": _sha("charter-command"),
        "problem_command_sha256": _sha("problem-command"),
        "question_command_sha256": _sha("question-command"),
        "registered_object_sha256s": (_sha("question-object"),),
        "finished_at": NOW,
    }
    register_only = ARL2QuestionCampaignRunReceiptV1(kind="register_only", **common)
    assert register_only.receipt_id.startswith("arl2r_")
    with pytest.raises(ValueError, match="carries terminal campaign evidence"):
        ARL2QuestionCampaignRunReceiptV1(
            kind="register_only", kernel_stream_sha256=_sha("stream"), **common
        )
    with pytest.raises(ValueError, match="lacks its terminal campaign evidence"):
        ARL2QuestionCampaignRunReceiptV1(kind="applied", **common)


def test_register_commits_the_three_pre_signed_commands_in_order(tmp_path: Path) -> None:
    from aletheia.db import create_all

    create_all()
    fx = _driver_fixture(tmp_path)
    store = _store(fx.trust_root, fx.policy, archive=fx.archive)
    store.commit(fx.charter_command)
    charter_tail = store.audit(QUEST_ID).events[-1].event_sha256
    problem, problem_command = _problem_command(
        archive=fx.archive,
        scope=fx.scope,
        charter=fx.charter,
        trust_root=fx.trust_root,
        policy=fx.policy,
        root_branch_id=fx.root_branch_id,
        expected_version=1,
        expected_tail=charter_tail,
        label="arl2-register",
    )
    store.commit(problem_command)
    problem_tail = store.audit(QUEST_ID).events[-1].event_sha256
    # The re-derived problem is a new object (the factory rolls a fresh id),
    # so the question and the request pin must move with it: activation
    # refuses a question whose problem_ref names an unpinned problem.
    question = fx.question.model_copy(update={"problem_ref": problem.object_ref})
    fx.archive.archive_object(question)
    question_command = _authorize(
        ResearchCommandProposal(
            quest_id=QUEST_ID,
            scope_binding=fx.scope,
            expected_stream_version=2,
            expected_tail_event_sha256=problem_tail,
            event_type=EventType.QUESTION_ADMITTED,
            payload=QuestionAdmittedPayload(
                question_ref=question.object_ref,
                branch_id=fx.root_branch_id,
            ),
            proposed_by_principal_id="model:planner",
            proposed_at=T0 + timedelta(seconds=2),
        ),
        trust_root=fx.trust_root,
        policy=fx.policy,
        role=ResearchAuthorizationRole.ORDINARY,
        label="arl2-question",
        authorized_at=T0 + timedelta(seconds=2),
    )
    Path(fx.problem_path).write_bytes(canonical_json_bytes(problem_command))
    Path(fx.question_path).write_bytes(canonical_json_bytes(question_command))
    config = _config_variant(
        fx.config,
        problem_command_file_sha256=hashlib.sha256(Path(fx.problem_path).read_bytes()).hexdigest(),
        question_command_file_sha256=hashlib.sha256(
            Path(fx.question_path).read_bytes()
        ).hexdigest(),
    )
    driver = _driver(
        fx,
        config=config,
        request=_request_variant(fx.request, question_version=question),
        kernel_store=store,
        kernel_archive=fx.archive,
        clock=lambda: NOW,
    )

    receipt = driver.register()

    events = store.audit(QUEST_ID).events
    assert [event.event_type for event in events] == [
        EventType.CHARTER_ACTIVATED,
        EventType.PROBLEM_ADMITTED,
        EventType.QUESTION_ADMITTED,
    ]
    assert events[-1].payload.question_ref.object_sha256 == question.object_sha256
    assert receipt.kind == "register_only"
    assert receipt.charter_command_sha256 == fx.charter_command.command_sha256
    assert receipt.problem_command_sha256 == problem_command.command_sha256
    assert receipt.question_command_sha256 == question_command.command_sha256
    assert receipt.registered_object_sha256s == (question.object_sha256,)
    assert receipt.replay_report_sha256 is None

    driver.register()
    assert len(store.audit(QUEST_ID).events) == 3


def test_register_refuses_drifted_or_tampered_request_content(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    commits: list[object] = []
    drifted = _driver(
        fx,
        request=_request_variant(fx.request, dataset_content_file_sha256=_sha("drifted-content")),
        kernel_store=SimpleNamespace(commit=commits.append),
    )
    with pytest.raises(ARL2RuntimeError, match="byte pin"):
        drifted.register()

    # D8 pre-registration: a tampered sealed set passes request validation (the
    # request never sees the csv bytes) and must die at register, pre-commit.
    tampered = _request_variant(
        fx.request,
        round_split_bindings=(
            fx.request.round_split_bindings[0].model_copy(
                update={"sealed_group_ids_sha256": _sha("tampered-sealed-set")}
            ),
            fx.request.round_split_bindings[1],
        ),
    )
    committed = _driver(
        fx,
        request=tampered,
        kernel_store=SimpleNamespace(commit=commits.append),
    )
    with pytest.raises(ValueError, match="sealed set differs from the card partition"):
        committed.register()
    assert commits == []


def test_request_content_passes_the_dataset_card_verifier(tmp_path: Path) -> None:
    from aletheia.protocols.data_registration import RegisteredDatasetV1, verify_dataset_card

    fx = _driver_fixture(tmp_path)
    card = RegisteredDatasetV1.model_validate_json(Path(fx.request.dataset_card_path).read_bytes())
    verify_dataset_card(card, Path(fx.request.dataset_content_path).read_bytes())
    assert canonical_sha256(card) == canonical_sha256(fx.replay.card)


# ---------------------------------------------------------------------------
# Q10(b): the shared-custody writer root (0750) and the worker role uid
# ---------------------------------------------------------------------------


def test_role_invocation_pins_the_worker_process_uid() -> None:
    payload = dict(
        role="worker",
        deployment_manifest_path="/opt/aletheia/worker-deployment.json",
        deployment_manifest_file_sha256=_sha("worker-deployment"),
    )
    arl2_runtime_module.ARL2RoleInvocationV1(**payload, process_uid=2365)
    for refused in (0, 2**31, -1):
        with pytest.raises(ValidationError):
            arl2_runtime_module.ARL2RoleInvocationV1(**payload, process_uid=refused)


def test_wake_roles_prepends_sudo_only_for_the_pinned_worker_uid(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    invoked: list[list[str]] = []
    config = _role_manifest_config(fx, tmp_path)
    pinned_uid = fx.config.process_uid + 5
    updated = []
    for invocation in config.role_invocations:
        if invocation.role == "worker":
            invocation = invocation.model_copy(update={"process_uid": pinned_uid})
        updated.append(invocation)
    config = _config_variant(fx.config, role_invocations=tuple(updated))
    driver = _driver(fx, config=config, runner=invoked.append)
    driver._wake_roles()
    assert len(invoked) == 4
    for command, invocation in zip(invoked, config.role_invocations, strict=True):
        wrapped = [
            sys.executable,
            config.runtime_entrypoint_path,
            "--deployment-manifest",
            invocation.deployment_manifest_path,
            "--deployment-manifest-sha256",
            invocation.deployment_manifest_file_sha256,
            "--once",
        ]
        if invocation.process_uid is None:
            assert command == wrapped
        else:
            assert command == [
                "/usr/bin/sudo",
                "-n",
                "-u",
                f"#{invocation.process_uid}",
                "--preserve-env=ALETHEIA_DATABASE_URL,PYTHONPATH,"
                "PYTHONDONTWRITEBYTECODE",
                *wrapped,
            ]
    assert invoked[2][:4] == ["/usr/bin/sudo", "-n", "-u", f"#{pinned_uid}"]


def test_kernel_writer_composes_the_shared_custody_root(tmp_path: Path) -> None:
    fx = _driver_fixture(tmp_path)
    root = Path(fx.config.kernel_writer.cas_root)
    # the flip mirrors the box procedure: cas.py checks EVERY parent at
    # directory_mode and every read at object_mode, so an owner-only tree
    # moves to shared custody recursively or not at all
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o750 if path.is_dir() else 0o440)
    root.chmod(0o750)
    shared = _config_variant(
        fx.config,
        kernel_writer=fx.config.kernel_writer.model_copy(
            update={"cas_directory_mode": 0o750}
        ),
    )
    archive = _driver(fx, config=shared)._compose_archive()
    assert archive.object_mode == 0o440
    digest = archive._write_once(b"arl2-shared-custody-object")
    stored = next(root.rglob(digest))
    assert stat.S_IMODE(stored.stat().st_mode) == 0o440
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o750
    # the owner-only pin must refuse the widened root instead of reading it
    with pytest.raises(ARL2RuntimeError, match="custody differs"):
        _driver(fx, config=fx.config)._compose_archive()


def test_kernel_writer_config_rejects_modes_outside_the_two_custody_shapes(
    tmp_path: Path,
) -> None:
    fx = _driver_fixture(tmp_path)
    for refused in (0o751, 0o755, 0o770):
        with pytest.raises(ValidationError):
            fx.config.kernel_writer.model_validate(
                {
                    **fx.config.kernel_writer.model_dump(mode="python"),
                    "cas_directory_mode": refused,
                }
            )
