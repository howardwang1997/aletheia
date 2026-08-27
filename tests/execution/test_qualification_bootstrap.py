from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

import aletheia.qualification_bootstrap as bootstrap
from aletheia.execution import qualification_deployment as deployment
from aletheia.execution.schemas import canonical_json_bytes
from .test_qualification_deployment import _sha, _spec

NOW = datetime(2026, 8, 27, 3, 4, 5, tzinfo=timezone.utc)
DIRECTORY_PURPOSES = (
    "artifact_store",
    "input_materialization_journal",
    "installer_journal",
    "node_private_keys",
    "node_state",
    "outbox_spool",
    "output_workspace_underlay",
    "quota_backing",
    "quota_socket_parent",
    "quota_state",
    "runtime_journal",
    "service_configs",
    "watchdog_socket_parent",
    "watchdog_state",
    "workspace_source",
)


def _tool(path: str) -> deployment.QualificationExpectedRootExecutable:
    return deployment.QualificationExpectedRootExecutable(
        path=path,
        reviewed_sha256=_sha(path),
        expected_mode=0o555,
    )


def _request(**updates: object) -> bootstrap.QualificationBootstrapRequestV1:
    base_spec = updates.pop("deployment_spec", _spec())
    assert isinstance(base_spec, deployment.QualificationDeploymentSpecV1)
    spec = base_spec.model_copy(
        update={
            "deployment_manifest_sha256": bootstrap.QUALIFICATION_UNFINALIZED_MANIFEST_SHA256,
            "expected_deployment_manifest": base_spec.expected_deployment_manifest.model_copy(
                update={"reviewed_sha256": bootstrap.QUALIFICATION_UNFINALIZED_MANIFEST_SHA256}
            ),
        }
    )
    values: dict[str, object] = {
        "deployment_spec": spec,
        "journal_root": "/var/lib/aletheia/bootstrap-journal",
        "service_config_root": "/etc/aletheia/service-configs-prod",
        "node_private_key_root": "/etc/aletheia/node-private-keys-prod",
        "installer_journal_root": "/var/lib/aletheia/installer-journal",
        "node_user_name": spec.postgresql_allocator_role,
        "outbox_user_name": spec.postgresql_outbox_role,
        "docker_group_name": "docker",
        "docker_group_expected_member_names": (spec.postgresql_allocator_role,),
        "groupadd_executable": _tool("/usr/sbin/groupadd"),
        "useradd_executable": _tool("/usr/sbin/useradd"),
        "nologin_executable": _tool("/usr/sbin/nologin"),
        "postgresql_socket_directory": (
            bootstrap.QualificationBootstrapSocketDirectoryPinV1(
                path=deployment.QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY,
                device=11,
                inode=27,
                owner_uid=0,
                owner_gid=120,
                mode=0o2775,
                parent_chain_sha256=_sha("postgresql-socket-parent"),
            )
        ),
        "requested_at": NOW,
    }
    values.update(updates)
    return bootstrap.QualificationBootstrapRequestV1(**values)


def _principal_observation(
    principal: bootstrap.QualificationBootstrapPrincipalV1,
) -> bootstrap.QualificationBootstrapPrincipalObservation:
    return bootstrap.QualificationBootstrapPrincipalObservation(
        principal=principal,
        observed_user_name=principal.user_name,
        observed_primary_group_name=principal.primary_group_name,
        observed_uid=principal.uid,
        observed_gid=principal.gid,
        observed_supplementary_group_names=principal.supplementary_group_names,
        observed_supplementary_gids=principal.supplementary_gids,
        observed_home_directory=principal.home_directory,
        observed_login_shell=principal.login_shell,
        password_locked=True,
    )


def _directory_observation(
    directory: bootstrap.QualificationBootstrapDirectoryV1,
) -> bootstrap.QualificationBootstrapDirectoryObservation:
    return bootstrap.QualificationBootstrapDirectoryObservation(
        directory=directory,
        device=21,
        inode=1000 + directory.ordinal,
        observed_owner_uid=directory.owner_uid,
        observed_owner_gid=directory.owner_gid,
        observed_mode=directory.mode,
        parent_chain_sha256=_sha(f"parent:{directory.path}"),
    )


class _FakeHost:
    def __init__(self) -> None:
        self.journals: dict[Path, bytes] = {}
        self.principals: dict[str, bootstrap.QualificationBootstrapPrincipalObservation] = {}
        self.directories: dict[str, bootstrap.QualificationBootstrapDirectoryObservation] = {}
        self.principal_mutations = 0
        self.directory_mutations = 0
        self.lock_calls = 0
        self.verify_calls: list[bool] = []
        self.fail_inputs = False

    def assert_linux_root(self) -> None:
        return None

    @contextmanager
    def lock(self):
        self.lock_calls += 1
        yield

    def verify_pinned_inputs(self, *, completed: bool) -> None:
        self.verify_calls.append(completed)
        if self.fail_inputs:
            raise bootstrap.QualificationBootstrapError("pinned input failed")

    def read_journal(self, path: Path) -> bytes | None:
        return self.journals.get(path)

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        existing = self.journals.get(path)
        if existing is not None and existing != payload:
            raise bootstrap.QualificationBootstrapError("journal exact retry differs")
        self.journals[path] = payload

    def ensure_principal(
        self,
        principal: bootstrap.QualificationBootstrapPrincipalV1,
    ) -> bootstrap.QualificationBootstrapPrincipalApplication:
        existing = self.principals.get(principal.user_name)
        if existing is not None:
            return bootstrap.QualificationBootstrapPrincipalApplication(
                observation=existing,
                group_created=False,
                user_created=False,
                command_sha256s=(),
            )
        observed = _principal_observation(principal)
        self.principals[principal.user_name] = observed
        self.principal_mutations += 1
        return bootstrap.QualificationBootstrapPrincipalApplication(
            observation=observed,
            group_created=True,
            user_created=True,
            command_sha256s=(
                _sha(f"groupadd:{principal.user_name}"),
                _sha(f"useradd:{principal.user_name}"),
            ),
        )

    def observe_principal(
        self,
        principal: bootstrap.QualificationBootstrapPrincipalV1,
    ) -> bootstrap.QualificationBootstrapPrincipalObservation:
        try:
            return self.principals[principal.user_name]
        except KeyError as exc:
            raise bootstrap.QualificationBootstrapError("principal missing") from exc

    def ensure_directory(
        self,
        directory: bootstrap.QualificationBootstrapDirectoryV1,
    ) -> bootstrap.QualificationBootstrapDirectoryApplication:
        existing = self.directories.get(directory.path)
        if existing is not None:
            return bootstrap.QualificationBootstrapDirectoryApplication(
                observation=existing,
                created=False,
            )
        observed = _directory_observation(directory)
        self.directories[directory.path] = observed
        self.directory_mutations += 1
        return bootstrap.QualificationBootstrapDirectoryApplication(
            observation=observed,
            created=True,
        )

    def observe_directory(
        self,
        directory: bootstrap.QualificationBootstrapDirectoryV1,
    ) -> bootstrap.QualificationBootstrapDirectoryObservation:
        try:
            return self.directories[directory.path]
        except KeyError as exc:
            raise bootstrap.QualificationBootstrapError("directory missing") from exc


def _clock():
    counter = 0

    def now() -> datetime:
        nonlocal counter
        counter += 1
        return NOW + timedelta(seconds=counter)

    return now


def test_request_and_plan_freeze_exact_peer_identities_and_empty_roots() -> None:
    request = _request()
    plan = bootstrap.build_qualification_bootstrap_plan(request)
    assert request.request_id == f"qbr_{request.identity_sha256[:32]}"
    assert request.file_sha256 == hashlib.sha256(canonical_json_bytes(request)).hexdigest()
    assert plan.plan_id == f"qbp_{plan.identity_sha256[:32]}"
    assert tuple(item.role for item in plan.principals) == ("node", "outbox")
    assert tuple(item.user_name for item in plan.principals) == (
        request.deployment_spec.postgresql_allocator_role,
        request.deployment_spec.postgresql_outbox_role,
    )
    assert plan.principals[0].supplementary_group_names == ("docker",)
    assert plan.principals[1].supplementary_group_names == ()
    assert tuple(item.ordinal for item in plan.directories) == tuple(range(15))
    assert tuple(item.purpose for item in plan.directories) == DIRECTORY_PURPOSES
    assert len({item.path for item in plan.directories}) == 15

    by_purpose = {item.purpose: item for item in plan.directories}
    assert (
        by_purpose["workspace_source"].owner_uid,
        by_purpose["workspace_source"].owner_gid,
        by_purpose["workspace_source"].mode,
    ) == (0, request.deployment_spec.node_gid, 0o1730)
    assert (
        by_purpose["outbox_spool"].owner_uid,
        by_purpose["outbox_spool"].owner_gid,
        by_purpose["outbox_spool"].mode,
    ) == (
        request.deployment_spec.outbox_uid,
        request.deployment_spec.outbox_gid,
        0o700,
    )
    assert all(
        getattr(plan, field) is False
        for field in (
            "configs_published",
            "private_keys_published",
            "postgresql_roles_created",
            "postgresql_acl_applied",
            "services_installed",
            "services_enabled",
            "services_started",
            "deployment_qualified",
            "scientific_admission_allowed",
        )
    )


def test_peer_database_urls_are_local_passwordless_and_role_specific() -> None:
    plan = bootstrap.build_qualification_bootstrap_plan(_request())
    for value, expected_role in (
        (plan.node_peer_database_url, "aletheia_exec_allocator"),
        (plan.outbox_peer_database_url, "aletheia_exec_outbox"),
    ):
        parsed = make_url(value)
        assert parsed.drivername == "postgresql+psycopg"
        assert parsed.username == expected_role
        assert parsed.password is None
        assert parsed.host is None
        assert parsed.port is None
        assert parsed.database == "aletheia_qualification"
        assert parsed.query == {"host": "/run/postgresql"}
        assert "%" not in value


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"node_user_name": "wrong_node"}, "principals or PostgreSQL peer binding"),
        (
            {"docker_group_expected_member_names": ("unexpected",)},
            "principals or PostgreSQL peer binding",
        ),
        ({"service_config_root": "/var/lib/aletheia/node-state"}, "overlaps"),
    ),
)
def test_request_rejects_identity_membership_and_target_rebinding(
    updates: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _request(**updates)


def test_request_rejects_socket_rebind_and_authority_expansion() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="peer binding"):
        bootstrap.QualificationBootstrapRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "postgresql_socket_directory": request.postgresql_socket_directory.model_copy(
                    update={"path": "/run/postgresql-variant"}
                ),
            }
        )
    with pytest.raises(ValidationError):
        bootstrap.QualificationBootstrapRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "publish_configs": True,
            }
        )
    with pytest.raises(ValidationError, match="socket directory overlaps"):
        _request(
            deployment_spec=_spec(
                node_state_root=deployment.QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY
            )
        )


def test_bootstrap_happy_path_and_exact_retry_are_idempotent() -> None:
    request = _request()
    host = _FakeHost()
    receipt = bootstrap.bootstrap_qualification_host(request, host, clock=_clock())
    assert receipt.receipt_id == f"qbx_{receipt.identity_sha256[:32]}"
    assert len(receipt.principal_completions) == 2
    assert len(receipt.directory_completions) == 15
    assert host.principal_mutations == 2
    assert host.directory_mutations == 15
    assert len(host.journals) == 38
    assert host.verify_calls == [False, True]
    assert receipt.configs_published is False
    assert receipt.postgresql_roles_created is False
    assert receipt.services_installed is False
    assert receipt.services_started is False
    assert receipt.deployment_qualified is False
    assert receipt.scientific_admission_allowed is False

    retried = bootstrap.bootstrap_qualification_host(request, host, clock=_clock())
    assert retried == receipt
    assert host.principal_mutations == 2
    assert host.directory_mutations == 15
    assert len(host.journals) == 38
    assert host.verify_calls == [False, True, False, True]


@pytest.mark.parametrize(
    "target_phase",
    ("after_principal_apply:0", "after_directory_apply:0", "after_receipt"),
)
def test_crash_at_each_mutation_boundary_replays_to_one_closed_receipt(
    target_phase: str,
) -> None:
    request = _request()
    host = _FakeHost()
    clock = _clock()
    crashed = False

    def fault(phase: str) -> None:
        nonlocal crashed
        if phase == target_phase and not crashed:
            crashed = True
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        bootstrap.bootstrap_qualification_host(request, host, clock=clock, fault=fault)
    receipt = bootstrap.bootstrap_qualification_host(request, host, clock=clock)
    assert len(receipt.principal_completions) == 2
    assert len(receipt.directory_completions) == 15
    assert host.principal_mutations == 2
    assert host.directory_mutations == 15
    assert bootstrap.bootstrap_qualification_host(request, host, clock=clock) == receipt


def test_active_request_variant_and_noncanonical_completion_fail_closed() -> None:
    request = _request()
    host = _FakeHost()
    bootstrap.bootstrap_qualification_host(request, host, clock=_clock())
    variant = bootstrap.QualificationBootstrapRequestV1.model_validate(
        {
            **request.model_dump(mode="python", exclude={"request_id"}),
            "requested_at": NOW + timedelta(seconds=1),
        }
    )
    with pytest.raises(bootstrap.QualificationBootstrapError, match="journal exact retry"):
        bootstrap.bootstrap_qualification_host(variant, host, clock=_clock())

    receipt_path = next(path for path in host.journals if path.name == "receipt.json")
    host.journals[receipt_path] += b"\n"
    with pytest.raises(bootstrap.QualificationBootstrapError, match="not canonical"):
        bootstrap.bootstrap_qualification_host(request, host, clock=_clock())


@pytest.mark.parametrize("kind", ("principal", "directory"))
def test_completed_live_identity_drift_is_rejected(kind: str) -> None:
    request = _request()
    plan = bootstrap.build_qualification_bootstrap_plan(request)
    host = _FakeHost()
    bootstrap.bootstrap_qualification_host(request, host, clock=_clock())
    if kind == "principal":
        principal = plan.principals[0]
        observed = host.principals[principal.user_name]
        host.principals[principal.user_name] = observed.model_copy(
            update={"observed_login_shell": "/bin/sh"}
        )
        message = "principal changed"
    else:
        directory = plan.directories[0]
        observed = host.directories[directory.path]
        host.directories[directory.path] = observed.model_copy(update={"inode": observed.inode + 1})
        message = "directory changed"
    with pytest.raises(bootstrap.QualificationBootstrapError, match=message):
        bootstrap.bootstrap_qualification_host(request, host, clock=_clock())


def test_pinned_input_failure_precedes_any_journal_or_mutation() -> None:
    host = _FakeHost()
    host.fail_inputs = True
    with pytest.raises(bootstrap.QualificationBootstrapError, match="pinned input failed"):
        bootstrap.bootstrap_qualification_host(_request(), host, clock=_clock())
    assert host.journals == {}
    assert host.principals == {}
    assert host.directories == {}


def test_clock_rollback_fails_closed_and_can_resume() -> None:
    request = _request()
    host = _FakeHost()
    values = iter((NOW - timedelta(seconds=1), *(_clock()() for _ in range(30))))
    with pytest.raises(bootstrap.QualificationBootstrapError, match="clock moved backwards"):
        bootstrap.bootstrap_qualification_host(request, host, clock=lambda: next(values))
    assert host.principal_mutations == 0
    receipt = bootstrap.bootstrap_qualification_host(request, host, clock=_clock())
    assert len(receipt.directory_completions) == 15


def test_request_loader_and_cli_plan_are_canonical_and_non_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _request()
    path = (tmp_path / "request.json").resolve()
    path.write_bytes(canonical_json_bytes(request))
    loaded = bootstrap.load_qualification_bootstrap_request(
        path,
        expected_file_sha256=request.file_sha256,
    )
    assert loaded == request
    assert (
        bootstrap.run_qualification_bootstrap_cli(
            ("--request", str(path), "--request-sha256", request.file_sha256)
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema_name"] == (
        "aletheia.qualification_bootstrap_plan"
    )

    with pytest.raises(bootstrap.QualificationBootstrapError, match="changed or differs"):
        bootstrap.load_qualification_bootstrap_request(
            path,
            expected_file_sha256="0" * 64,
        )
    noncanonical = (tmp_path / "pretty.json").resolve()
    noncanonical.write_text(
        json.dumps(request.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    with pytest.raises(bootstrap.QualificationBootstrapError, match="not canonical"):
        bootstrap.load_qualification_bootstrap_request(
            noncanonical,
            expected_file_sha256=hashlib.sha256(noncanonical.read_bytes()).hexdigest(),
        )


def test_cli_apply_requires_exact_opt_in_before_constructing_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    path = (tmp_path / "request.json").resolve()
    path.write_bytes(canonical_json_bytes(request))
    calls = 0

    def forbidden_host(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("host must not be constructed")

    monkeypatch.setattr(bootstrap, "LinuxQualificationBootstrapHost", forbidden_host)
    with pytest.raises(SystemExit):
        bootstrap.run_qualification_bootstrap_cli(
            (
                "--request",
                str(path),
                "--request-sha256",
                request.file_sha256,
                "--apply",
            )
        )
    assert calls == 0


def test_concrete_host_refuses_non_linux_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = bootstrap.LinuxQualificationBootstrapHost(_request())
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    with pytest.raises(bootstrap.QualificationBootstrapError, match="requires Linux"):
        host.assert_linux_root()


def test_concrete_directory_apply_recovers_safe_interrupted_custody(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = bootstrap.LinuxQualificationBootstrapHost(_request())
    monkeypatch.setattr(host, "_assert_root_parent", lambda _path: None)
    target = (tmp_path / "recovered-root").resolve()
    target.mkdir(mode=0o700)
    directory = bootstrap.QualificationBootstrapDirectoryV1(
        ordinal=0,
        purpose="artifact_store",
        path=str(target),
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        mode=0o755,
    )
    applied = host.ensure_directory(directory)
    assert applied.created is False
    assert applied.observation.observed_mode == 0o755

    child = target / "foreign"
    child.write_text("unexpected", encoding="utf-8")
    with pytest.raises(bootstrap.QualificationBootstrapError, match="not empty"):
        host.ensure_directory(directory)


def test_principal_and_directory_contracts_reject_unsafe_variants() -> None:
    plan = bootstrap.build_qualification_bootstrap_plan(_request())
    principal = plan.principals[0]
    with pytest.raises(ValidationError, match="not canonical"):
        bootstrap.QualificationBootstrapPrincipalV1.model_validate(
            {
                **principal.model_dump(mode="python"),
                "supplementary_group_names": ("docker", "wheel"),
            }
        )
    directory = plan.directories[0]
    with pytest.raises(ValidationError, match="owner-controlled"):
        bootstrap.QualificationBootstrapDirectoryV1.model_validate(
            {**directory.model_dump(mode="python"), "mode": 0o777}
        )
