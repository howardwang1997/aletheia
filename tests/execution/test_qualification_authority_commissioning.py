from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

import aletheia.qualification_authority_commissioning as commissioning
from aletheia.execution.qualification_deployment import (
    EXPECTED_EXECUTION_SCHEMA_REVISION,
    postgresql_role_privileges_sha256,
    qualification_postgresql_peer_database_url,
)
from aletheia.execution.qualification_node_service import (
    QualificationNodeMutableRootPinV1,
)
from aletheia.execution.qualification_outbox_service import (
    QualificationOutboxSpoolRootPinV1,
    QualificationTerminalOutboxServiceConfigV1,
)
from aletheia.execution.qualification_root_services import (
    QualificationQuotaServiceConfigV2,
    QualificationWatchdogServiceConfigV1,
    QualificationWorkspaceServiceConfigV1,
)
from aletheia.execution.qualification_service_contracts import (
    QualificationServiceDeploymentManifestV1,
    QualificationServiceProcessDeploymentV1,
    QualificationServiceRole,
    qualification_service_process_config_binding_sha256,
)
from aletheia.execution.schemas import canonical_json_bytes
from aletheia.qualification_bootstrap import (
    QUALIFICATION_UNFINALIZED_MANIFEST_SHA256,
    QualificationBootstrapDirectoryObservation,
    bootstrap_qualification_host,
)
from aletheia.qualification_installer import (
    QualificationInstallationRequestV1,
    QualificationInstalledFileObservation,
    QualificationSystemdQuiescenceObservation,
    QualificationSystemdUnitState,
)
from .test_qualification_bootstrap import (
    _FakeHost as _BootstrapHost,
    _clock as _bootstrap_clock,
    _request as _bootstrap_request,
)
from .test_qualification_installer import _request as _installation_request
from .test_qualification_node_service import (
    PRIVATE_KEY,
    RUNTIME_PRIVATE_KEY,
    TRANSPORT_PRIVATE_KEY,
    _fixture as _node_fixture,
)
from .test_qualification_outbox_service import _process_and_config as _outbox_fixture
from .test_qualification_root_services import _configs as _root_configs

NOW = datetime(2026, 8, 27, 5, 0, 0, tzinfo=timezone.utc)
ADMIN_URL = "postgresql+psycopg://root@/aletheia?host=/run/postgresql"


def _replace_process(
    process: QualificationServiceProcessDeploymentV1,
    **updates: object,
) -> QualificationServiceProcessDeploymentV1:
    return QualificationServiceProcessDeploymentV1.model_validate(
        {
            **process.model_dump(mode="python", exclude={"process_id"}),
            **updates,
        }
    )


def _root_pin(
    pin: QualificationNodeMutableRootPinV1,
    observation: QualificationBootstrapDirectoryObservation,
) -> QualificationNodeMutableRootPinV1:
    return QualificationNodeMutableRootPinV1.model_validate(
        {
            **pin.model_dump(mode="python"),
            "path": observation.directory.path,
            "device": observation.device,
            "inode": observation.inode,
            "owner_uid": observation.observed_owner_uid,
            "owner_gid": observation.observed_owner_gid,
            "mode": observation.observed_mode,
            "parent_chain_sha256": observation.parent_chain_sha256,
        }
    )


def _request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    base_installation = _installation_request()
    base_spec = base_installation.deployment_spec
    server_identity = commissioning.QualificationPostgreSQLServerIdentityV1(
        system_identifier="7420193847561029384",
        server_version_num=170002,
        database_name=base_spec.postgresql_database,
        database_oid=16384,
        database_encoding="UTF8",
    )
    seed_spec = base_spec.model_copy(
        update={
            "deployment_manifest_sha256": QUALIFICATION_UNFINALIZED_MANIFEST_SHA256,
            "expected_deployment_manifest": base_spec.expected_deployment_manifest.model_copy(
                update={"reviewed_sha256": QUALIFICATION_UNFINALIZED_MANIFEST_SHA256}
            ),
            "postgresql_server_identity_sha256": server_identity.identity_sha256,
        }
    )
    bootstrap_request = _bootstrap_request(
        deployment_spec=seed_spec,
        service_config_root="/etc/aletheia/services",
        node_private_key_root="/etc/aletheia/keys",
        installer_journal_root=base_installation.journal_root,
    )
    bootstrap_receipt = bootstrap_qualification_host(
        bootstrap_request,
        _BootstrapHost(),
        clock=_bootstrap_clock(),
    )
    observed = {
        item.application.observation.directory.purpose: item.application.observation
        for item in bootstrap_receipt.directory_completions
    }

    root_values = _root_configs(tmp_path / "root-configs")
    workspace_base = root_values[1]
    node_root = tmp_path / "node-config"
    outbox_root = tmp_path / "outbox-config"
    node_root.mkdir()
    outbox_root.mkdir()
    node_process, node_base = _node_fixture(monkeypatch, node_root)
    _outbox_process, outbox_base = _outbox_fixture(monkeypatch, outbox_root)
    assert node_process.deployment_id == seed_spec.deployment_id

    source = observed["workspace_source"]
    target = observed["output_workspace_underlay"]
    workspace_deployment = workspace_base.workspace_deployment.model_copy(
        update={
            "source_root": source.directory.path,
            "source_root_device": source.device,
            "source_root_inode": source.inode,
            "source_root_owner_gid": source.observed_owner_gid,
            "source_root_mode": source.observed_mode,
            "source_root_parent_chain_sha256": source.parent_chain_sha256,
            "target_root": target.directory.path,
            "target_underlay_device": target.device,
            "target_underlay_inode": target.inode,
            "target_underlay_owner_uid": target.observed_owner_uid,
            "target_underlay_owner_gid": target.observed_owner_gid,
            "target_underlay_mode": target.observed_mode,
            "target_parent_chain_sha256": target.parent_chain_sha256,
        }
    )
    quota = node_base.quota_deployment
    quota_backing = observed["quota_backing"]
    quota_state = observed["quota_state"]
    quota_socket = observed["quota_socket_parent"]
    workspace_pin = quota.workspace_root_pin.model_copy(
        update={
            "path": target.directory.path,
            "device": source.device,
            "inode": source.inode,
            "owner_gid": source.observed_owner_gid,
            "mode": source.observed_mode,
            "parent_chain_sha256": target.parent_chain_sha256,
        }
    )
    quota = quota.model_copy(
        update={
            "workspace_root": target.directory.path,
            "workspace_root_pin": workspace_pin,
            "backing_root": quota_backing.directory.path,
            "backing_root_device": quota_backing.device,
            "backing_root_inode": quota_backing.inode,
            "backing_root_mode": quota_backing.observed_mode,
            "backing_root_parent_chain_sha256": quota_backing.parent_chain_sha256,
            "state_root": quota_state.directory.path,
            "state_root_device": quota_state.device,
            "state_root_inode": quota_state.inode,
            "state_root_mode": quota_state.observed_mode,
            "state_root_parent_chain_sha256": quota_state.parent_chain_sha256,
            "socket_parent_device": quota_socket.device,
            "socket_parent_inode": quota_socket.inode,
            "socket_parent_mode": quota_socket.observed_mode,
            "socket_parent_parent_chain_sha256": quota_socket.parent_chain_sha256,
        }
    )
    runtime = observed["runtime_journal"]
    watchdog_state = observed["watchdog_state"]
    watchdog_socket = observed["watchdog_socket_parent"]
    watchdog = node_base.watchdog_deployment.model_copy(
        update={
            "journal_root": runtime.directory.path,
            "journal_root_device": runtime.device,
            "journal_root_inode": runtime.inode,
            "journal_root_mode": runtime.observed_mode,
            "journal_root_parent_chain_sha256": runtime.parent_chain_sha256,
            "state_root": watchdog_state.directory.path,
            "state_root_device": watchdog_state.device,
            "state_root_inode": watchdog_state.inode,
            "state_root_mode": watchdog_state.observed_mode,
            "state_root_parent_chain_sha256": watchdog_state.parent_chain_sha256,
            "socket_parent_device": watchdog_socket.device,
            "socket_parent_inode": watchdog_socket.inode,
            "socket_parent_mode": watchdog_socket.observed_mode,
            "socket_parent_parent_chain_sha256": watchdog_socket.parent_chain_sha256,
        }
    )

    templates = {process.role: process for process in base_installation.service_manifest.processes}
    workspace = QualificationWorkspaceServiceConfigV1(
        deployment_id=seed_spec.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(
            templates[QualificationServiceRole.WORKSPACE]
        ),
        workspace_deployment=workspace_deployment,
    )
    quota_config = QualificationQuotaServiceConfigV2(
        deployment_id=seed_spec.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(
            templates[QualificationServiceRole.QUOTA]
        ),
        oci_policy=node_base.oci_policy,
        runtime_journal_root=runtime.directory.path,
        quota_deployment=quota,
    )
    watchdog_config = QualificationWatchdogServiceConfigV1(
        deployment_id=seed_spec.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(
            templates[QualificationServiceRole.WATCHDOG]
        ),
        oci_policy=node_base.oci_policy,
        watchdog_deployment=watchdog,
    )
    key_parent_sha256 = hashlib.sha256(b"node-key-target-parent").hexdigest()
    node_keys = tuple(
        pin.model_copy(update={"parent_chain_sha256": key_parent_sha256})
        for pin in (
            node_base.node_signing_key,
            node_base.assignment_transport_key,
            node_base.runtime_control_key,
        )
    )
    node_url = qualification_postgresql_peer_database_url(
        seed_spec,
        role_name=seed_spec.postgresql_allocator_role,
    )
    custody = node_base.qualification_custody.model_copy(
        update={
            "artifact_store_root": observed["artifact_store"].directory.path,
            "authority_registry_root": seed_spec.authority_registry_root,
        }
    )
    node_config = type(node_base).model_validate(
        {
            **node_base.model_dump(mode="python"),
            "process_config_binding_sha256": qualification_service_process_config_binding_sha256(
                templates[QualificationServiceRole.NODE]
            ),
            "database_url_sha256": hashlib.sha256(node_url.encode()).hexdigest(),
            "schema_revision": EXPECTED_EXECUTION_SCHEMA_REVISION,
            "postgresql_role": seed_spec.postgresql_allocator_role,
            "qualification_custody": custody,
            "node_signing_key": node_keys[0],
            "assignment_transport_key": node_keys[1],
            "runtime_control_key": node_keys[2],
            "artifact_store_root_pin": _root_pin(
                node_base.artifact_store_root_pin,
                observed["artifact_store"],
            ),
            "node_state_root_pin": _root_pin(
                node_base.node_state_root_pin,
                observed["node_state"],
            ),
            "input_materialization_journal_root_pin": _root_pin(
                node_base.input_materialization_journal_root_pin,
                observed["input_materialization_journal"],
            ),
            "runtime_journal_root_pin": _root_pin(
                node_base.runtime_journal_root_pin,
                observed["runtime_journal"],
            ),
            "image_layout": node_base.image_layout.model_copy(
                update={"layout_root": seed_spec.oci_layout_root}
            ),
            "quota_deployment": quota,
            "watchdog_deployment": watchdog,
        }
    )
    spool = observed["outbox_spool"]
    outbox_url = qualification_postgresql_peer_database_url(
        seed_spec,
        role_name=seed_spec.postgresql_outbox_role,
    )
    outbox_config = QualificationTerminalOutboxServiceConfigV1(
        deployment_id=seed_spec.deployment_id,
        process_config_binding_sha256=qualification_service_process_config_binding_sha256(
            templates[QualificationServiceRole.OUTBOX]
        ),
        database_url_sha256=hashlib.sha256(outbox_url.encode()).hexdigest(),
        schema_revision=EXPECTED_EXECUTION_SCHEMA_REVISION,
        postgresql_role=seed_spec.postgresql_outbox_role,
        spool_root=QualificationOutboxSpoolRootPinV1(
            path=spool.directory.path,
            device=spool.device,
            inode=spool.inode,
            owner_uid=spool.observed_owner_uid,
            owner_gid=spool.observed_owner_gid,
            mode=spool.observed_mode,
            parent_chain_sha256=spool.parent_chain_sha256,
        ),
        poll_milliseconds=outbox_base.poll_milliseconds,
        maximum_source_rows_per_kind=outbox_base.maximum_source_rows_per_kind,
        prepared_at=outbox_base.prepared_at,
    )
    configs = {
        QualificationServiceRole.WORKSPACE: workspace,
        QualificationServiceRole.QUOTA: quota_config,
        QualificationServiceRole.WATCHDOG: watchdog_config,
        QualificationServiceRole.NODE: node_config,
        QualificationServiceRole.OUTBOX: outbox_config,
    }
    processes = tuple(
        _replace_process(
            templates[role],
            composition_config_file_sha256=hashlib.sha256(
                canonical_json_bytes(configs[role])
            ).hexdigest(),
        )
        for role in QualificationServiceRole
    )
    manifest = QualificationServiceDeploymentManifestV1(
        deployment_id=seed_spec.deployment_id,
        processes=processes,
        prepared_at=NOW,
    )
    final_spec = commissioning.finalize_qualification_deployment_spec(
        seed_spec,
        manifest,
    )
    installation_request = QualificationInstallationRequestV1(
        deployment_spec=final_spec,
        service_manifest=manifest,
        journal_root=bootstrap_request.installer_journal_root,
        systemctl_executable=base_installation.systemctl_executable,
        requested_at=NOW + timedelta(minutes=1),
    )
    source_values = (
        ("node_signing", "/root/aletheia-source/node-signing.key", PRIVATE_KEY),
        (
            "assignment_transport",
            "/root/aletheia-source/assignment-transport.key",
            TRANSPORT_PRIVATE_KEY,
        ),
        (
            "runtime_control",
            "/root/aletheia-source/runtime-control.key",
            RUNTIME_PRIVATE_KEY,
        ),
    )
    sources = tuple(
        commissioning.QualificationPrivateKeySourceV1(
            role=role,
            source_path=path,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_parent_chain_sha256=hashlib.sha256(f"source-parent:{path}".encode()).hexdigest(),
            target=target_pin,
        )
        for (role, path, payload), target_pin in zip(source_values, node_keys, strict=True)
    )
    request = commissioning.QualificationAuthorityCommissioningRequestV1(
        bootstrap_request=bootstrap_request,
        bootstrap_receipt=bootstrap_receipt,
        installation_request=installation_request,
        workspace_config=workspace,
        quota_config=quota_config,
        watchdog_config=watchdog_config,
        node_config=node_config,
        outbox_config=outbox_config,
        private_key_sources=sources,
        admin_database_url_sha256=hashlib.sha256(ADMIN_URL.encode()).hexdigest(),
        admin_role="aletheia_commissioner",
        expected_postgresql_server_identity=server_identity,
        requested_at=NOW,
    )
    payloads = {path: payload for _role, path, payload in source_values}
    return request, payloads


class _FakeHost:
    def __init__(self, request, private_payloads: dict[str, bytes]) -> None:
        self.request = request
        self.private_payloads = private_payloads
        self.journals: dict[Path, bytes] = {}
        self.artifacts: dict[str, tuple[bytes, QualificationInstalledFileObservation]] = {}
        self.commission_calls = 0
        self.verify_calls = 0
        self.lock_calls = 0
        self.database_state = None

    def assert_linux_root(self) -> None:
        return None

    @contextmanager
    def lock(self):
        self.lock_calls += 1
        yield

    def verify_bootstrap(self) -> None:
        self.verify_calls += 1

    def observe_systemd(self, unit_names):
        return QualificationSystemdQuiescenceObservation(
            units=tuple(
                QualificationSystemdUnitState(
                    unit_name=name,
                    load_state="not-found",
                    unit_file_state="not-found",
                )
                for name in unit_names
            ),
            observed_at=NOW,
        )

    def read_journal(self, path: Path) -> bytes | None:
        return self.journals.get(path)

    def write_journal_once(self, path: Path, payload: bytes) -> None:
        existing = self.journals.get(path)
        if existing is not None and existing != payload:
            raise commissioning.QualificationAuthorityCommissioningError("journal variant")
        self.journals[path] = payload

    def load_private_key_source(self, source):
        return self.private_payloads[source.source_path]

    def publish_artifact(self, artifact, payload):
        observation = QualificationInstalledFileObservation(
            path=artifact.target_path,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            byte_length=len(payload),
            owner_uid=artifact.owner_uid,
            owner_gid=artifact.owner_gid,
            mode=artifact.mode,
            device=7,
            inode=100 + artifact.ordinal,
        )
        self.artifacts[artifact.target_path] = (payload, observation)
        return observation

    def observe_artifact(self, artifact):
        return self.artifacts[artifact.target_path][1]

    def _state(self, intent):
        spec = self.request.installation_request.deployment_spec
        roles = tuple(
            commissioning.QualificationPostgreSQLRoleProjectionV1(
                role_name=role,
                can_login=role != intent.owner_role,
                connection_limit=(
                    -1 if role == intent.owner_role else intent.application_role_connection_limit
                ),
                role_config=(
                    ("search_path=pg_catalog, public",) if role != intent.owner_role else ()
                ),
                target_privileges_sha256=(
                    postgresql_role_privileges_sha256(spec, role_name=role)
                    if role != intent.owner_role
                    else None
                ),
            )
            for role in sorted((intent.owner_role, intent.allocator_role, intent.outbox_role))
        )
        hba = tuple(
            commissioning.QualificationPostgreSQLHbaPeerRuleV1(
                role_name=role,
                line_number=10 + index,
                database_names=(intent.database_name,),
                user_names=(role,),
            )
            for index, role in enumerate(sorted((intent.allocator_role, intent.outbox_role)))
        )
        return commissioning.QualificationPostgreSQLCommissionedStateV1(
            database_name=intent.database_name,
            database_owner_role=intent.owner_role,
            admin_role=intent.admin_role,
            schema_revision=intent.schema_revision,
            acl_sha256=intent.acl_sha256,
            server_identity=self.request.expected_postgresql_server_identity,
            roles=roles,
            hba_peer_rules=hba,
        )

    def commission_postgresql(self, intent):
        self.commission_calls += 1
        self.database_state = self._state(intent)
        return self.database_state

    def observe_postgresql(self, _intent):
        return self.database_state


class _CommittedDatabaseCrashHost(_FakeHost):
    def commission_postgresql(self, intent):
        state = super().commission_postgresql(intent)
        if self.commission_calls == 1:
            raise RuntimeError("database committed before host returned")
        return state


def _clock():
    counter = 0

    def now() -> datetime:
        nonlocal counter
        counter += 1
        return NOW + timedelta(minutes=2, seconds=counter)

    return now


def test_plan_closes_manifest_delta_eight_artifacts_and_acl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    plan = commissioning.build_qualification_authority_commissioning_plan(request)
    assert request.bootstrap_request.deployment_spec.deployment_manifest_sha256 == (
        QUALIFICATION_UNFINALIZED_MANIFEST_SHA256
    )
    assert request.installation_request.deployment_spec.expected_schema_revision == (
        EXPECTED_EXECUTION_SCHEMA_REVISION
    )
    assert tuple(item.artifact_key for item in plan.artifacts) == commissioning._ARTIFACT_ORDER
    assert tuple(item.artifact_kind for item in plan.artifacts[:3]) == (
        "private_key",
        "private_key",
        "private_key",
    )
    assert all(item.artifact_kind == "service_config" for item in plan.artifacts[3:])
    assert plan.services_installed is False
    assert plan.deployment_qualified is False


def test_commissioning_happy_path_exact_retry_and_authority_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, payloads = _request(monkeypatch, tmp_path)
    host = _FakeHost(request, payloads)
    now = _clock()
    receipt = commissioning.commission_qualification_authority(
        request,
        host,
        clock=now,
    )
    assert len(receipt.artifact_completions) == 8
    assert host.commission_calls == 1
    assert receipt.configs_published is True
    assert receipt.private_keys_published is True
    assert receipt.postgresql_acl_applied is True
    assert receipt.services_installed is False
    assert receipt.services_enabled is False
    assert receipt.services_started is False
    assert receipt.deployment_qualified is False
    assert receipt.scientific_admission_allowed is False
    verified_plan = commissioning.verify_qualification_authority_commissioning_receipt(
        request,
        receipt,
    )
    assert verified_plan.plan_sha256 == receipt.plan_sha256

    retried = commissioning.commission_qualification_authority(
        request,
        host,
        clock=now,
    )
    assert retried == receipt
    assert host.commission_calls == 1
    assert host.verify_calls == 2
    assert host.lock_calls == 2

    rebound_state = receipt.postgresql_completion.commissioned_state.model_copy(
        update={"database_owner_role": request.admin_role}
    )
    rebound_completion = receipt.postgresql_completion.model_copy(
        update={"commissioned_state": rebound_state}
    )
    rebound_receipt = receipt.model_copy(
        update={"receipt_id": None, "postgresql_completion": rebound_completion}
    )
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="PostgreSQL commissioning receipt chain differs",
    ):
        commissioning.verify_qualification_authority_commissioning_receipt(
            request,
            rebound_receipt,
        )


@pytest.mark.parametrize("phase", ("after_artifact:0", "after_artifact:7", "after_postgresql"))
def test_crash_boundaries_resume_without_duplicate_database_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    request, payloads = _request(monkeypatch, tmp_path)
    host = _FakeHost(request, payloads)
    now = _clock()

    def fault(observed: str) -> None:
        if observed == phase:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        commissioning.commission_qualification_authority(
            request,
            host,
            clock=now,
            fault=fault,
        )
    receipt = commissioning.commission_qualification_authority(
        request,
        host,
        clock=now,
    )
    assert len(receipt.artifact_completions) == 8
    assert host.commission_calls == 1


def test_private_key_bytes_must_match_public_authority_identity_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, payloads = _request(monkeypatch, tmp_path)
    payloads[request.private_key_sources[0].source_path] = b"x" * 32
    host = _FakeHost(request, payloads)
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="source bytes differ",
    ):
        commissioning.commission_qualification_authority(request, host, clock=_clock())
    assert host.artifacts == {}
    assert host.commission_calls == 0


def test_database_commit_without_completion_journal_replays_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, payloads = _request(monkeypatch, tmp_path)
    host = _CommittedDatabaseCrashHost(request, payloads)
    now = _clock()
    with pytest.raises(RuntimeError, match="database committed"):
        commissioning.commission_qualification_authority(request, host, clock=now)
    receipt = commissioning.commission_qualification_authority(request, host, clock=now)
    assert receipt.postgresql_completion.commissioned_state == host.database_state
    assert host.commission_calls == 2


def test_request_rejects_final_spec_changes_beyond_manifest_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    installation = QualificationInstallationRequestV1.model_validate(
        {
            **request.installation_request.model_dump(mode="python", exclude={"request_id"}),
            "deployment_spec": request.installation_request.deployment_spec.model_copy(
                update={"maximum_observation_duration_seconds": 11}
            ),
        }
    )
    with pytest.raises(ValidationError, match="changes more than the manifest digest"):
        commissioning.QualificationAuthorityCommissioningRequestV1.model_validate(
            {
                **request.model_dump(mode="python", exclude={"request_id"}),
                "installation_request": installation,
            }
        )


def test_acl_wrapper_extraction_rejects_ambiguous_tail() -> None:
    assert commissioning._acl_transaction_body(b"-- frozen\nBEGIN;\nSELECT 1;\nCOMMIT;\n") == (
        "SELECT 1;\n"
    )
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="ambiguous",
    ):
        commissioning._acl_transaction_body(b"BEGIN;\nSELECT 1;\nCOMMIT;\nSELECT 2;\n")

    statement = commissioning._acl_transaction_statement(
        b"BEGIN;\nSELECT format('%I', 'role');\nCOMMIT;\n"
    )
    assert "format('%%I', 'role')" in str(statement.compile(dialect=postgresql.dialect()))

    rendered = (
        b"BEGIN;\n"
        b"DO $aletheia_column_acl$\n"
        b"BEGIN\n"
        b"  EXECUTE 'REVOKE UPDATE';\n"
        b"END\n"
        b"$aletheia_column_acl$;\n"
        b"DO $aletheia_acl$\n"
        b"BEGIN\n"
        b"  IF EXISTS (SELECT 1) THEN\n"
        b"    RAISE EXCEPTION 'drift';\n"
        b"  END IF;\n"
        b"END\n"
        b"$aletheia_acl$;\n"
        b"COMMIT;\n"
    )
    validation = commissioning._acl_read_only_validation(rendered)
    assert validation.startswith("DO $aletheia_acl$\n")
    assert "$aletheia_column_acl$" not in validation
    assert "EXECUTE 'REVOKE UPDATE'" not in validation
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="not read-only",
    ):
        commissioning._acl_read_only_validation(
            b"DO $aletheia_acl$\nBEGIN\nUPDATE execution_attempts SET status = 'x';\n"
            b"END\n$aletheia_acl$;\n"
        )


class _PrivilegeRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _PrivilegeConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _parameters):
        return _PrivilegeRows(self.rows)


class _RoleRows:
    def __init__(self, *, row=None, rows=()):
        self.row = row
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def __iter__(self):
        return iter(self.rows)


class _RoleConnection:
    def __init__(self, role_name: str):
        self.role_name = role_name
        self.statements: list[str] = []

    def execute(self, statement, _parameters):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM pg_catalog.pg_roles AS role" in sql:
            return _RoleRows(
                row={
                    "rolname": self.role_name,
                    "rolcanlogin": True,
                    "rolsuper": False,
                    "rolcreatedb": False,
                    "rolcreaterole": False,
                    "rolinherit": False,
                    "rolreplication": False,
                    "rolbypassrls": False,
                    "rolconnlimit": 16,
                    "password_is_null": True,
                    "valid_until_is_infinite": True,
                    "role_config": ["search_path=pg_catalog, public"],
                }
            )
        return _RoleRows(rows=())


class _CatalogRows:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def mappings(self):
        return self

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self):
        return iter(self.rows)


class _CatalogConnection:
    def __init__(self, *, spec, routine_definition: str, trigger_definition: str):
        routine = spec.expected_postgresql_routines[0]
        trigger = spec.expected_postgresql_triggers[0]
        sequence = spec.expected_postgresql_sequences[0]
        self.rows = {
            "routine": (
                {
                    "routine_kind": routine.routine_kind,
                    "routine_name": routine.routine_name,
                    "identity_argument_types": list(routine.identity_argument_types),
                    "definition": routine_definition,
                    "language": routine.language,
                    "security_definer": routine.security_definer,
                    "configuration": list(routine.configuration),
                    "volatility": routine.volatility,
                    "owner_role": "live_routine_owner",
                },
            ),
            "trigger": (
                {
                    "table_name": trigger.table_name,
                    "trigger_name": trigger.trigger_name,
                    "function_name": routine.routine_name,
                    "function_identity_argument_types": list(routine.identity_argument_types),
                    "definition": trigger_definition,
                    "enabled": trigger.enabled,
                    "function_schema": "public",
                },
            ),
            "sequence": (
                {
                    **sequence.model_dump(mode="python"),
                    "owner_role": "live_sequence_owner",
                },
            ),
            "relation_owner": (
                {
                    "object_kind": "table",
                    "object_name": trigger.table_name,
                    "owner_role": "live_table_owner",
                },
                {
                    "object_kind": "sequence",
                    "object_name": sequence.sequence_name,
                    "owner_role": "live_sequence_owner",
                },
            ),
            "database_owner": ({"owner_role": "live_database_owner"},),
            "schema_owner": ({"owner_role": "live_schema_owner"},),
        }

    def execute(self, statement, _parameters=None):
        sql = str(statement)
        if "pg_get_functiondef" in sql:
            key = "routine"
        elif "pg_get_triggerdef" in sql:
            key = "trigger"
        elif "FROM pg_catalog.pg_class AS sequence_relation" in sql:
            key = "sequence"
        elif "CASE relation.relkind" in sql:
            key = "relation_owner"
        elif "FROM pg_catalog.pg_database AS database" in sql:
            key = "database_owner"
        elif "FROM pg_catalog.pg_namespace AS namespace" in sql:
            key = "schema_owner"
        else:  # pragma: no cover - a new query must extend this closed fake
            raise AssertionError(sql)
        return _CatalogRows(self.rows[key])


def test_live_execution_catalog_hashes_database_definitions_and_owners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    spec = request.installation_request.deployment_spec
    routine_definition = "CREATE FUNCTION public.live_definition() RETURNS trigger ...\n"
    trigger_definition = "CREATE TRIGGER live_definition BEFORE INSERT ..."
    catalog = commissioning.LinuxQualificationAuthorityCommissioningHost._execution_catalog(
        _CatalogConnection(
            spec=spec,
            routine_definition=routine_definition,
            trigger_definition=trigger_definition,
        ),
        spec,
    )

    assert (
        catalog.routines[0].definition_sha256
        == hashlib.sha256(routine_definition.encode()).hexdigest()
    )
    assert catalog.routines[0].definition_sha256 != (
        spec.expected_postgresql_routines[0].definition_sha256
    )
    assert (
        catalog.triggers[0].definition_sha256
        == hashlib.sha256(trigger_definition.encode()).hexdigest()
    )
    assert {(item.object_kind, item.owner_role) for item in catalog.object_owners} == {
        ("database", "live_database_owner"),
        ("function", "live_routine_owner"),
        ("schema", "live_schema_owner"),
        ("sequence", "live_sequence_owner"),
        ("table", "live_table_owner"),
    }
    duplicate_owner = catalog.object_owners[0].model_copy(update={"owner_role": "variant_owner"})
    with pytest.raises(ValidationError, match="catalog projection is not canonical"):
        commissioning.QualificationPostgreSQLExecutionCatalogProjectionV1.model_validate(
            {
                **catalog.model_dump(mode="python"),
                "object_owners": tuple(
                    sorted(
                        (*catalog.object_owners, duplicate_owner),
                        key=lambda item: (
                            item.object_kind,
                            item.object_name,
                            item.owner_role,
                        ),
                    )
                ),
            }
        )


class _ScalarRows:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _AtomicProjectionConnection:
    def __init__(self):
        self.commands: list[str] = []

    @contextmanager
    def begin(self):
        yield

    def exec_driver_sql(self, command: str):
        self.commands.append(command)

    def execute(self, statement):
        assert "clock_timestamp" in str(statement)
        return _ScalarRows(NOW)


class _AtomicProjectionEngine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    @contextmanager
    def connect(self):
        yield self.connection

    def dispose(self):
        self.disposed = True


def test_deployment_projection_uses_one_repeatable_read_only_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, payloads = _request(monkeypatch, tmp_path)
    intent = _database_intent(request)
    spec = request.installation_request.deployment_spec
    state = _FakeHost(request, payloads)._state(intent)
    catalog = commissioning.LinuxQualificationAuthorityCommissioningHost._execution_catalog(
        _CatalogConnection(
            spec=spec,
            routine_definition="CREATE FUNCTION public.atomic_projection() ...\n",
            trigger_definition="CREATE TRIGGER atomic_projection ...",
        ),
        spec,
    )
    connection = _AtomicProjectionConnection()
    engine = _AtomicProjectionEngine(connection)
    monkeypatch.setattr(commissioning, "create_engine", lambda *_args, **_kwargs: engine)
    host = object.__new__(commissioning.LinuxQualificationAuthorityCommissioningHost)
    host.request = request
    monkeypatch.setattr(host, "_database_url", lambda _intent: ADMIN_URL)
    monkeypatch.setattr(host, "_state", lambda _connection, _intent: state)
    monkeypatch.setattr(host, "_execution_catalog", lambda _connection, _spec: catalog)
    monkeypatch.setattr(
        host,
        "_non_execution_public_routine_owners",
        lambda _connection, *, execution_prefix: (),
    )

    observed = host.observe_postgresql_deployment_projection(intent)

    assert observed.commissioned_state == state
    assert observed.execution_catalog == catalog
    assert observed.database_time == NOW
    assert connection.commands == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"]
    assert engine.disposed is True


def test_live_role_privilege_projection_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    spec = request.installation_request.deployment_spec
    role = spec.postgresql_outbox_role
    rows = [
        {
            "scope": "database",
            "object_name": spec.postgresql_database,
            "subobject_name": None,
            "privilege_type": "CONNECT",
            "is_grantable": False,
        },
        {
            "scope": "schema",
            "object_name": spec.postgresql_schema,
            "subobject_name": None,
            "privilege_type": "USAGE",
            "is_grantable": False,
        },
        *(
            {
                "scope": "table",
                "object_name": table_name,
                "subobject_name": None,
                "privilege_type": "SELECT",
                "is_grantable": False,
            }
            for table_name in (
                "alembic_version",
                "execution_outbox",
                "execution_qualification_terminal_outbox",
            )
        ),
        *(
            {
                "scope": "column",
                "object_name": "execution_outbox",
                "subobject_name": column_name,
                "privilege_type": "UPDATE",
                "is_grantable": False,
            }
            for column_name in ("publish_attempts", "published_at", "status")
        ),
    ]
    observed = commissioning.LinuxQualificationAuthorityCommissioningHost
    assert observed._role_privileges_sha256(_PrivilegeConnection(rows), spec, role) == (
        postgresql_role_privileges_sha256(spec, role_name=role)
    )
    drifted = [
        *rows,
        {
            "scope": "table",
            "object_name": "execution_outbox",
            "subobject_name": None,
            "privilege_type": "DELETE",
            "is_grantable": False,
        },
    ]
    assert observed._role_privileges_sha256(_PrivilegeConnection(drifted), spec, role) != (
        postgresql_role_privileges_sha256(spec, role_name=role)
    )


def test_role_projection_reads_unmasked_password_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    spec = request.installation_request.deployment_spec
    connection = _RoleConnection(spec.postgresql_outbox_role)
    projection = commissioning.LinuxQualificationAuthorityCommissioningHost._role_projection(
        connection,
        spec,
        spec.postgresql_outbox_role,
        observe_privileges=False,
    )
    assert projection is not None
    assert projection.password_is_null is True
    assert projection.role_config == ("search_path=pg_catalog, public",)
    assert any("JOIN pg_catalog.pg_authid AS authority" in sql for sql in connection.statements)


class _HbaRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class _HbaConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        return _HbaRows(self.rows)


def _database_intent(request):
    plan = commissioning.build_qualification_authority_commissioning_plan(request)
    return commissioning._postgresql_intent(request, plan)


def test_hba_peer_rules_must_be_exact_unshadowed_and_option_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _payloads = _request(monkeypatch, tmp_path)
    intent = _database_intent(request)
    exact_rows = tuple(
        {
            "line_number": 10 + index,
            "type": "local",
            "database": [intent.database_name],
            "user_name": [role],
            "auth_method": "peer",
            "options": None,
            "error": None,
        }
        for index, role in enumerate(sorted((intent.allocator_role, intent.outbox_role)))
    )
    rules = commissioning.LinuxQualificationAuthorityCommissioningHost._hba_peer_rules(
        _HbaConnection(exact_rows),
        intent,
    )
    assert tuple(item.role_name for item in rules) == tuple(
        sorted((intent.allocator_role, intent.outbox_role))
    )

    shadow = {
        "line_number": 1,
        "type": "local",
        "database": ["all"],
        "user_name": ["all"],
        "auth_method": "trust",
        "options": None,
        "error": None,
    }
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="shadowed",
    ):
        commissioning.LinuxQualificationAuthorityCommissioningHost._hba_peer_rules(
            _HbaConnection((shadow, *exact_rows)),
            intent,
        )

    variant = dict(exact_rows[0], options=["map=unexpected"])
    with pytest.raises(
        commissioning.QualificationAuthorityCommissioningError,
        match="option-free",
    ):
        commissioning.LinuxQualificationAuthorityCommissioningHost._hba_peer_rules(
            _HbaConnection((variant, exact_rows[1])),
            intent,
        )
