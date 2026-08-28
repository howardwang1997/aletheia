from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from sqlalchemy.engine import make_url

import aletheia.execution.qualification_deployment as deployment
from aletheia.execution.oci_deployment import (
    LoopbackQuotaProvisionerDeploymentPin,
    PinnedOCIImageLayout,
    PinnedRootExecutable,
    PinnedRootFile,
    SystemdWatchdogDeploymentPin,
)
from aletheia.execution.runtime_v2_contracts import PinnedOutputWorkspaceRoot
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.schemas import canonical_sha256

NOW = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
OBSERVER_PRIVATE_KEY = bytes(range(1, 33))


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _expected_file(path: str) -> deployment.QualificationExpectedRootFile:
    return deployment.QualificationExpectedRootFile(
        path=path,
        reviewed_sha256=_sha(path),
        expected_mode=0o444,
    )


def _expected_executable(path: str) -> deployment.QualificationExpectedRootExecutable:
    return deployment.QualificationExpectedRootExecutable(
        path=path,
        reviewed_sha256=_sha(path),
        expected_mode=0o555,
    )


def _spec(**updates: object) -> deployment.QualificationDeploymentSpecV1:
    code_root = "/opt/aletheia/release"
    python = _expected_executable("/opt/aletheia/runtime/bin/python")
    python_root = "/opt/aletheia/runtime"
    runners = (
        _expected_file("/opt/aletheia/release/scripts/run-workspace.py"),
        _expected_file("/opt/aletheia/release/scripts/run-quota.py"),
        _expected_file("/opt/aletheia/release/scripts/run-watchdog.py"),
        _expected_file("/opt/aletheia/release/scripts/run-node.py"),
        _expected_file("/opt/aletheia/release/scripts/run-outbox.py"),
    )
    code_directories = tuple(
        deployment.QualificationReviewedCodeDirectory(
            relative_path=path,
            expected_mode=0o555,
        )
        for path in ("aletheia", "aletheia/execution", "scripts")
    )
    code_entries = tuple(
        sorted(
            (
                *(
                    deployment.QualificationReviewedCodeFile(
                        relative_path=runner.path.removeprefix(f"{code_root}/"),
                        reviewed_sha256=runner.reviewed_sha256,
                        byte_length=100 + index,
                        expected_mode=runner.expected_mode,
                    )
                    for index, runner in enumerate(runners)
                ),
                deployment.QualificationReviewedCodeFile(
                    relative_path="aletheia/execution/oci_deployment.py",
                    reviewed_sha256=_sha("reviewed-oci-deployment-module"),
                    byte_length=200_001,
                    expected_mode=0o444,
                ),
                deployment.QualificationReviewedCodeFile(
                    relative_path="aletheia/execution/oci_runtime.py",
                    reviewed_sha256=_sha("reviewed-oci-runtime-module"),
                    byte_length=200_002,
                    expected_mode=0o444,
                ),
                deployment.QualificationReviewedCodeFile(
                    relative_path="aletheia/execution/qualification_deployment.py",
                    reviewed_sha256=_sha("reviewed-qualification-deployment-module"),
                    byte_length=200_003,
                    expected_mode=0o444,
                ),
            ),
            key=lambda entry: entry.relative_path,
        )
    )
    code_tree = deployment.QualificationReviewedCodeTree(
        root_path=code_root,
        expected_root_mode=0o555,
        directories=code_directories,
        entries=code_entries,
        manifest_sha256=deployment.reviewed_code_tree_manifest_sha256(
            root_path=code_root,
            directories=code_directories,
            entries=code_entries,
            expected_root_mode=0o555,
        ),
    )
    python_directories = tuple(
        deployment.QualificationReviewedCodeDirectory(
            relative_path=path,
            expected_mode=0o555,
        )
        for path in ("bin", "lib", "lib/python3.12", "lib/python3.12/site-packages")
    )
    python_entries = (
        deployment.QualificationReviewedCodeFile(
            relative_path="bin/python",
            reviewed_sha256=python.reviewed_sha256,
            byte_length=1_000_000,
            expected_mode=python.expected_mode,
        ),
        deployment.QualificationReviewedCodeFile(
            relative_path="lib/python3.12/os.py",
            reviewed_sha256=_sha("reviewed-stdlib-os"),
            byte_length=40_000,
            expected_mode=0o444,
        ),
    )
    python_environment = deployment.QualificationReviewedCodeTree(
        root_path=python_root,
        expected_root_mode=0o555,
        directories=python_directories,
        entries=python_entries,
        manifest_sha256=deployment.reviewed_code_tree_manifest_sha256(
            root_path=python_root,
            directories=python_directories,
            entries=python_entries,
            expected_root_mode=0o555,
        ),
    )
    python_import_paths = (code_root, f"{python_root}/lib/python3.12/site-packages")
    service_module = _expected_file(
        "/opt/aletheia/release/aletheia/execution/oci_deployment.py"
    ).model_copy(update={"reviewed_sha256": _sha("reviewed-oci-deployment-module")})
    deployment_manifest = _expected_file("/etc/aletheia/qualification/manifest.json").model_copy(
        update={"reviewed_sha256": _sha("deployment-manifest")}
    )
    seccomp_profile = _expected_file("/etc/aletheia/qualification/seccomp.json").model_copy(
        update={"reviewed_sha256": _sha("seccomp")}
    )
    losetup = _expected_executable("/usr/sbin/losetup")
    mkfs = _expected_executable("/usr/sbin/mkfs.ext4")
    mount = _expected_executable("/usr/bin/mount")

    def native_closure(
        executable: deployment.QualificationExpectedRootExecutable,
    ) -> deployment.ReviewedNativeDependencyClosure:
        interpreter = deployment.ReviewedNativeDependencyFile(
            path="/opt/aletheia/native/ld-linux-aarch64.so.1",
            reviewed_sha256=_sha("reviewed-elf-interpreter"),
            expected_mode=0o555,
            executable_required=True,
        )
        dependencies = (
            deployment.ReviewedNativeDependency(
                soname="libc.so.6",
                file=deployment.ReviewedNativeDependencyFile(
                    path="/opt/aletheia/native/libc.so.6",
                    reviewed_sha256=_sha("reviewed-libc"),
                    expected_mode=0o444,
                    executable_required=False,
                ),
                needed_sonames=(),
            ),
        )
        needed = ("libc.so.6",)
        return deployment.ReviewedNativeDependencyClosure(
            executable=executable,
            elf_interpreter=interpreter,
            executable_needed_sonames=needed,
            dependencies=dependencies,
            manifest_sha256=deployment.reviewed_native_dependency_closure_sha256(
                executable=executable,
                elf_interpreter=interpreter,
                executable_needed_sonames=needed,
                dependencies=dependencies,
            ),
        )

    native_closures = tuple(
        sorted(
            (native_closure(losetup), native_closure(mkfs), native_closure(mount)),
            key=lambda item: item.executable.path,
        )
    )
    routines = (
        deployment.PostgreSQLExpectedRoutine(
            routine_kind="function",
            routine_schema="public",
            execution_owned=True,
            routine_name="aletheia_execution_guard_attempt_v1",
            identity_argument_types=(),
            definition_sha256=_sha("guard-function-definition"),
            language="plpgsql",
            security_definer=True,
            configuration=("search_path=pg_catalog, public",),
            volatility="volatile",
        ),
        deployment.PostgreSQLExpectedRoutine(
            routine_kind="function",
            routine_schema="public",
            execution_owned=True,
            routine_name="aletheia_execution_project_budget_v1",
            identity_argument_types=("bigint",),
            definition_sha256=_sha("budget-function-definition"),
            language="sql",
            security_definer=False,
            configuration=(),
            volatility="stable",
        ),
        deployment.PostgreSQLExpectedRoutine(
            routine_kind="procedure",
            routine_schema="public",
            execution_owned=True,
            routine_name="aletheia_execution_archive_v1",
            identity_argument_types=("timestamp with time zone",),
            definition_sha256=_sha("archive-procedure-definition"),
            language="plpgsql",
            security_definer=True,
            configuration=("search_path=pg_catalog, public",),
            volatility="volatile",
        ),
    )
    triggers = (
        deployment.PostgreSQLExpectedTrigger(
            table_name="execution_attempts",
            trigger_name="aletheia_execution_attempt_guard_v1",
            function_identity=routines[0].identity,
            definition_sha256=_sha("attempt-guard-trigger-definition"),
            enabled="origin",
        ),
    )
    sequences = (
        deployment.PostgreSQLExpectedSequenceConfiguration(
            sequence_name="execution_budget_events_event_id_seq",
            data_type="bigint",
            persistence="permanent",
            start_value=1,
            minimum_value=1,
            maximum_value=9_223_372_036_854_775_807,
            increment_by=1,
            cache_size=1,
            cycles=False,
            owned_by_table="execution_budget_events",
            owned_by_column="event_id",
        ),
    )
    values: dict[str, object] = {
        "deployment_id": "qualification:prod",
        "node_id": "node:qualification-prod",
        "node_manifest_sha256": _sha("node-manifest"),
        "expected_cpu_architecture": "aarch64",
        "expected_oci_platform": "linux/arm64",
        "node_uid": 2101,
        "node_gid": 2101,
        "docker_gid": 998,
        "outbox_uid": 2102,
        "outbox_gid": 2102,
        "python_executable": python.path,
        "expected_python_executable": python,
        "reviewed_python_environment": python_environment,
        "expected_python_import_paths": python_import_paths,
        "code_root": code_root,
        "reviewed_code_tree": code_tree,
        "deployment_manifest_path": "/etc/aletheia/qualification/manifest.json",
        "deployment_manifest_sha256": _sha("deployment-manifest"),
        "expected_deployment_manifest": deployment_manifest,
        "workspace_source_root": "/srv/aletheia/workspace-source",
        "output_workspace_root": "/var/lib/aletheia/workspaces",
        "quota_backing_root": "/var/lib/aletheia/quota-backing",
        "quota_state_root": "/var/lib/aletheia/quota-state",
        "quota_socket_path": "/run/aletheia-quota/service.sock",
        "watchdog_state_root": "/var/lib/aletheia/watchdog-state",
        "watchdog_socket_path": "/run/aletheia-watchdog/service.sock",
        "runtime_journal_root": "/var/lib/aletheia/runtime-journal",
        "node_state_root": "/var/lib/aletheia/node-state",
        "artifact_store_root": "/var/lib/aletheia/artifact-store",
        "input_materialization_journal_root": "/var/lib/aletheia/input-journal",
        "authority_registry_root": "/opt/aletheia/authority-registry",
        "oci_layout_root": "/opt/aletheia/oci-layout",
        "outbox_spool_root": "/var/spool/aletheia-qualification",
        "seccomp_profile_path": "/etc/aletheia/qualification/seccomp.json",
        "expected_seccomp_profile": seccomp_profile,
        "apparmor_profile_path": "/etc/apparmor.d/aletheia-qualification",
        "apparmor_profile_name": "aletheia-qualification",
        "workspace_runner_path": "/opt/aletheia/release/scripts/run-workspace.py",
        "expected_workspace_runner": runners[0],
        "quota_runner_path": "/opt/aletheia/release/scripts/run-quota.py",
        "expected_quota_runner": runners[1],
        "watchdog_runner_path": "/opt/aletheia/release/scripts/run-watchdog.py",
        "expected_watchdog_runner": runners[2],
        "node_runner_path": "/opt/aletheia/release/scripts/run-node.py",
        "expected_node_runner": runners[3],
        "outbox_runner_path": "/opt/aletheia/release/scripts/run-outbox.py",
        "expected_outbox_runner": runners[4],
        "expected_quota_service_module": service_module,
        "expected_watchdog_service_module": service_module,
        "expected_losetup_executable": losetup,
        "expected_mkfs_ext4_executable": mkfs,
        "expected_mount_executable": mount,
        "reviewed_privileged_tool_native_closures": native_closures,
        "workspace_unit_name": "aletheia-qualification-workspace-prod.service",
        "quota_unit_name": "aletheia-qualification-output-quota-prod.service",
        "watchdog_unit_name": "aletheia-qualification-oci-watchdog-prod.service",
        "node_unit_name": "aletheia-qualification-node-prod.service",
        "outbox_unit_name": "aletheia-qualification-outbox-prod.service",
        "postgresql_database": "aletheia_qualification",
        "postgresql_owner_role": "aletheia_exec_owner",
        "postgresql_allocator_role": "aletheia_exec_allocator",
        "postgresql_outbox_role": "aletheia_exec_outbox",
        "expected_postgresql_routines": routines,
        "expected_postgresql_triggers": triggers,
        "expected_postgresql_sequences": sequences,
        "agent_implementation_sha256": deployment.qualification_agent_implementation_sha256(
            reviewed_code_tree=code_tree,
            reviewed_python_environment=python_environment,
            expected_python_executable=python,
            expected_runners=runners,
            expected_service_modules=(service_module, service_module),
            expected_python_import_paths=python_import_paths,
        ),
        "authority_bundle_sha256": _sha("authority-bundle"),
        "oci_policy_sha256": _sha("oci-policy"),
        "output_quota_policy_sha256": _sha("quota-policy"),
        "docker_security_projection_sha256": _sha("docker-security"),
        "postgresql_server_identity_sha256": _sha("postgres-server"),
        "image_manifest_sha256": _sha("image-manifest"),
        "image_config_sha256": _sha("image-config"),
        "launch_gate_executable_sha256": _sha("launch-gate"),
        "launch_gate_protocol_sha256": _sha("launch-protocol"),
        "seccomp_profile_sha256": _sha("seccomp"),
        "apparmor_profile_sha256": _sha("apparmor"),
    }
    values.update(updates)
    if "deployment_manifest_path" in updates and "expected_deployment_manifest" not in updates:
        values["expected_deployment_manifest"] = deployment_manifest.model_copy(
            update={"path": updates["deployment_manifest_path"]}
        )
    if "deployment_manifest_sha256" in updates and "expected_deployment_manifest" not in updates:
        values["expected_deployment_manifest"] = values["expected_deployment_manifest"].model_copy(
            update={"reviewed_sha256": updates["deployment_manifest_sha256"]}
        )
    return deployment.QualificationDeploymentSpecV1(**values)


def _root_file(
    path: str,
    *,
    sha256: str | None = None,
    inode: int,
    mode: int = 0o444,
) -> PinnedRootFile:
    return PinnedRootFile(
        path=path,
        sha256=sha256 or _sha(path),
        device=11,
        inode=inode,
        mode=mode,
        parent_chain_sha256=_sha(f"parent:{path}"),
    )


def _root_executable(path: str, *, inode: int) -> PinnedRootExecutable:
    return PinnedRootExecutable(
        path=path,
        sha256=_sha(path),
        device=11,
        inode=inode,
        mode=0o555,
        parent_chain_sha256=_sha(f"parent:{path}"),
    )


def _role(
    spec: deployment.QualificationDeploymentSpecV1,
    role_name: str,
) -> deployment.PostgreSQLRestrictedRoleObservation:
    return deployment.PostgreSQLRestrictedRoleObservation(
        role_name=role_name,
        can_login=True,
        is_superuser=False,
        can_create_database=False,
        can_create_role=False,
        inherits_roles=False,
        can_replicate=False,
        bypasses_row_security=False,
        member_of_owner_role=False,
        owns_execution_objects=False,
        can_create_in_schema=False,
        can_create_temporary_tables=False,
        can_delete_execution_rows=False,
        can_truncate_execution_tables=False,
        can_execute_ddl=False,
        can_mutate_triggers_or_functions=False,
        direct_role_memberships=(),
        transitive_role_memberships=(),
        role_members=(),
        dangerous_builtin_role_memberships=(),
        table_privileges_sha256=deployment.postgresql_role_privileges_sha256(
            spec,
            role_name=role_name,
        ),
    )


def _observation(
    spec: deployment.QualificationDeploymentSpecV1,
    **updates: object,
) -> deployment.QualificationLinuxDeploymentObservation:
    units = deployment.render_systemd_units(spec)
    unit_pins = tuple(
        _root_file(item.path, sha256=item.content_sha256, inode=100 + index)
        for index, item in enumerate(units)
    )
    unit_by_name = {item.unit_name: pin for item, pin in zip(units, unit_pins, strict=True)}
    python = _root_executable(spec.python_executable, inode=200)
    python_environment = deployment.QualificationObservedRootCodeTree(
        path=spec.reviewed_python_environment.root_path,
        device=11,
        inode=199,
        mode=spec.reviewed_python_environment.expected_root_mode,
        parent_chain_sha256=_sha("python-environment-parent"),
        tree_manifest_sha256=spec.reviewed_python_environment.manifest_sha256,
        directory_count=len(spec.reviewed_python_environment.directories),
        regular_file_count=len(spec.reviewed_python_environment.entries),
        total_regular_file_bytes=spec.reviewed_python_environment.total_bytes,
    )
    entrypoints = tuple(
        sorted(
            (
                _root_file(spec.workspace_runner_path, inode=201),
                _root_file(spec.quota_runner_path, inode=202),
                _root_file(spec.watchdog_runner_path, inode=203),
                _root_file(spec.node_runner_path, inode=204),
                _root_file(spec.outbox_runner_path, inode=205),
            ),
            key=lambda item: item.path,
        )
    )
    workspace = PinnedOutputWorkspaceRoot(
        path=spec.output_workspace_root,
        device=21,
        inode=301,
        mount_id=401,
        owner_gid=spec.node_gid,
        parent_chain_sha256=_sha("workspace-parent"),
    )
    custody_roots = tuple(
        deployment.QualificationObservedCustodyRoot(
            purpose=purpose,
            path=path,
            device=workspace.device if purpose == "workspace_source" else 31,
            inode=workspace.inode if purpose == "workspace_source" else 320 + index,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=mode,
            parent_chain_sha256=_sha(f"custody-parent:{purpose}"),
        )
        for index, (purpose, (path, owner_uid, owner_gid, mode)) in enumerate(
            deployment._expected_custody_root_policies(spec).items()
        )
    )
    losetup = _root_executable(spec.expected_losetup_executable.path, inode=302)
    mkfs = _root_executable(spec.expected_mkfs_ext4_executable.path, inode=303)
    mount = _root_executable(spec.expected_mount_executable.path, inode=304)

    live_tools = {item.path: item for item in (losetup, mkfs, mount)}

    def observed_native_closure(
        reviewed: deployment.ReviewedNativeDependencyClosure,
    ) -> deployment.ObservedNativeDependencyClosure:
        return deployment.ObservedNativeDependencyClosure(
            executable=live_tools[reviewed.executable.path],
            elf_interpreter=_root_file(
                reviewed.elf_interpreter.path,
                sha256=reviewed.elf_interpreter.reviewed_sha256,
                inode=315,
                mode=reviewed.elf_interpreter.expected_mode,
            ),
            executable_needed_sonames=reviewed.executable_needed_sonames,
            dependencies=tuple(
                deployment.ObservedNativeDependency(
                    soname=dependency.soname,
                    file=_root_file(
                        dependency.file.path,
                        sha256=dependency.file.reviewed_sha256,
                        inode=316 + index,
                        mode=dependency.file.expected_mode,
                    ),
                    needed_sonames=dependency.needed_sonames,
                )
                for index, dependency in enumerate(reviewed.dependencies)
            ),
            exhaustive=True,
            external_native_dependency_paths=(),
        )

    native_closures = tuple(
        observed_native_closure(reviewed)
        for reviewed in spec.reviewed_privileged_tool_native_closures
    )
    service_module = _root_file(
        spec.expected_quota_service_module.path,
        sha256=spec.expected_quota_service_module.reviewed_sha256,
        inode=305,
    )
    quota = LoopbackQuotaProvisionerDeploymentPin(
        deployment_id=f"{spec.deployment_id}:quota",
        systemd_unit_name=spec.quota_unit_name,
        workspace_root=spec.output_workspace_root,
        workspace_root_pin=workspace,
        backing_root=spec.quota_backing_root,
        state_root=spec.quota_state_root,
        socket_path=spec.quota_socket_path,
        allowed_client_uid=spec.node_uid,
        allowed_client_gid=spec.node_gid,
        provisioner_policy_sha256=spec.output_quota_policy_sha256,
        provisioner_principal_id="principal:qualification-quota",
        systemd_unit=unit_by_name[spec.quota_unit_name],
        service_executable=python,
        losetup=losetup,
        mkfs=mkfs,
        mount=mount,
        service_module_sha256=service_module.sha256,
        service_module_device=service_module.device,
        service_module_inode=service_module.inode,
        service_module_mode=service_module.mode,
        service_module_parent_chain_sha256=service_module.parent_chain_sha256,
        backing_root_device=21,
        backing_root_inode=306,
        backing_root_parent_chain_sha256=_sha("quota-backing-parent"),
        state_root_device=21,
        state_root_inode=307,
        state_root_parent_chain_sha256=_sha("quota-state-parent"),
        socket_parent_device=22,
        socket_parent_inode=308,
        socket_parent_parent_chain_sha256=_sha("quota-socket-parent"),
    )
    watchdog = SystemdWatchdogDeploymentPin(
        deployment_id=f"{spec.deployment_id}:watchdog",
        policy_sha256=spec.oci_policy_sha256,
        systemd_unit_name=spec.watchdog_unit_name,
        systemd_unit=unit_by_name[spec.watchdog_unit_name],
        service_executable=python,
        service_module_sha256=service_module.sha256,
        service_module_device=service_module.device,
        service_module_inode=service_module.inode,
        service_module_mode=service_module.mode,
        service_module_parent_chain_sha256=service_module.parent_chain_sha256,
        journal_root=spec.runtime_journal_root,
        journal_root_device=21,
        journal_root_inode=309,
        journal_root_parent_chain_sha256=_sha("runtime-journal-parent"),
        state_root=spec.watchdog_state_root,
        state_root_device=21,
        state_root_inode=310,
        state_root_parent_chain_sha256=_sha("watchdog-state-parent"),
        socket_path=spec.watchdog_socket_path,
        socket_parent_device=22,
        socket_parent_inode=311,
        socket_parent_parent_chain_sha256=_sha("watchdog-socket-parent"),
        allowed_client_uid=spec.node_uid,
        allowed_client_gid=spec.node_gid,
        maximum_active_jobs=spec.maximum_active_watchdog_jobs,
    )
    image = PinnedOCIImageLayout(
        policy_sha256=spec.oci_policy_sha256,
        layout_root=spec.oci_layout_root,
        layout_root_device=21,
        layout_root_inode=312,
        layout_root_mode=0o555,
        layout_parent_chain_sha256=_sha("oci-layout-parent"),
        reviewed_launch_gate_executable_sha256=spec.launch_gate_executable_sha256,
        reviewed_launch_gate_protocol_sha256=spec.launch_gate_protocol_sha256,
    )
    values: dict[str, object] = {
        "deployment_id": spec.deployment_id,
        "node_id": spec.node_id,
        "node_manifest_sha256": spec.node_manifest_sha256,
        "platform": "linux",
        "cpu_architecture": spec.expected_cpu_architecture,
        "oci_platform": spec.expected_oci_platform,
        "kernel_release": "6.8.0-qualification",
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "pid_one_comm": "systemd",
        "cgroup_version": 2,
        "docker_cgroup_driver": "systemd",
        "docker_security_projection_sha256": spec.docker_security_projection_sha256,
        "pid_one_mount_namespace": "mnt:[4026531841]",
        "quota_mount_namespace": "mnt:[4026531841]",
        "node_mount_namespace": "mnt:[4026531841]",
        "docker_mount_namespace": "mnt:[4026531841]",
        "shared_output_mount_visible": True,
        "host_clock_synchronized": True,
        "custody_roots": custody_roots,
        "python_executable": python,
        "python_environment_root": python_environment,
        "python_import_paths": spec.expected_python_import_paths,
        "python_external_loaded_native_object_paths": (),
        "entrypoint_files": entrypoints,
        "code_root": deployment.QualificationObservedRootCodeTree(
            path=spec.code_root,
            device=11,
            inode=207,
            mode=spec.reviewed_code_tree.expected_root_mode,
            parent_chain_sha256=_sha("code-root-parent"),
            tree_manifest_sha256=spec.reviewed_code_tree.manifest_sha256,
            directory_count=len(spec.reviewed_code_tree.directories),
            regular_file_count=len(spec.reviewed_code_tree.entries),
            total_regular_file_bytes=spec.reviewed_code_tree.total_bytes,
        ),
        "deployment_manifest_file": _root_file(
            spec.deployment_manifest_path,
            sha256=spec.deployment_manifest_sha256,
            inode=206,
        ),
        "systemd_unit_files": tuple(sorted(unit_pins, key=lambda item: item.path)),
        "service_module_files": (service_module,),
        "privileged_tool_native_closures": native_closures,
        "systemd_service_identities": deployment._expected_systemd_service_identities(spec),
        "seccomp_profile": _root_file(
            spec.seccomp_profile_path,
            sha256=spec.seccomp_profile_sha256,
            inode=313,
        ),
        "apparmor_profile": _root_file(
            spec.apparmor_profile_path,
            sha256=spec.apparmor_profile_sha256,
            inode=314,
        ),
        "loaded_apparmor_profile_name": spec.apparmor_profile_name,
        "apparmor_profile_enforcing": True,
        "agent_implementation_sha256": spec.agent_implementation_sha256,
        "authority_bundle_sha256": spec.authority_bundle_sha256,
        "output_workspace_root": workspace,
        "oci_image_layout": image,
        "loaded_image_manifest_sha256": spec.image_manifest_sha256,
        "loaded_image_config_sha256": spec.image_config_sha256,
        "quota_deployment": quota,
        "watchdog_deployment": watchdog,
        "quota_service_systemd_verified": True,
        "watchdog_service_systemd_verified": True,
        "schema_revision": spec.expected_schema_revision,
        "postgresql_server_identity_sha256": spec.postgresql_server_identity_sha256,
        "postgresql_acl_sha256": hashlib.sha256(deployment.render_postgresql_acl(spec)).hexdigest(),
        "postgresql_clock_healthy": True,
        "postgresql_roles": tuple(
            sorted(
                (
                    _role(spec, spec.postgresql_allocator_role),
                    _role(spec, spec.postgresql_outbox_role),
                ),
                key=lambda item: item.role_name,
            )
        ),
        "postgresql_owner_role_inherits": False,
        "postgresql_owner_direct_role_memberships": (),
        "postgresql_owner_transitive_role_memberships": (),
        "postgresql_owner_dangerous_builtin_role_memberships": (),
        "postgresql_owner_role_members": (),
        "postgresql_unexpected_database_grants": (),
        "postgresql_unexpected_schema_grants": (),
        "postgresql_unexpected_table_grants": (),
        "postgresql_unexpected_column_grants": (),
        "postgresql_unexpected_sequence_grants": (),
        "postgresql_unexpected_routine_execute_grants": (),
        "postgresql_unexpected_grant_options": (),
        "postgresql_unexpected_execution_routines": (),
        "postgresql_routines": spec.expected_postgresql_routines,
        "postgresql_triggers": spec.expected_postgresql_triggers,
        "postgresql_sequences": spec.expected_postgresql_sequences,
        "postgresql_non_execution_public_routine_owners": (
            deployment.PostgreSQLNonExecutionRoutineOwnerObservation(
                routine_kind="function",
                routine_schema="public",
                routine_name="unrelated_public_helper",
                identity_argument_types=("text",),
                owner_role="unrelated_app_owner",
            ),
        ),
        "postgresql_non_execution_public_routine_owner_projection_exhaustive": True,
        "postgresql_execution_object_owners": tuple(
            sorted(
                (
                    deployment.PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="database",
                        object_name=spec.postgresql_database,
                        owner_role=spec.postgresql_owner_role,
                    ),
                    deployment.PostgreSQLExecutionObjectOwnerObservation(
                        object_kind="schema",
                        object_name=spec.postgresql_schema,
                        owner_role=spec.postgresql_owner_role,
                    ),
                    *(
                        deployment.PostgreSQLExecutionObjectOwnerObservation(
                            object_kind="table",
                            object_name=table,
                            owner_role=spec.postgresql_owner_role,
                        )
                        for table in deployment.EXECUTION_TABLES
                    ),
                    *(
                        deployment.PostgreSQLExecutionObjectOwnerObservation(
                            object_kind="sequence",
                            object_name=sequence,
                            owner_role=spec.postgresql_owner_role,
                        )
                        for sequence in deployment.EXECUTION_SEQUENCES
                    ),
                    *(
                        deployment.PostgreSQLExecutionObjectOwnerObservation(
                            object_kind=routine.routine_kind,
                            object_name=routine.identity,
                            owner_role=spec.postgresql_owner_role,
                        )
                        for routine in spec.expected_postgresql_routines
                    ),
                ),
                key=lambda item: (item.object_kind, item.object_name),
            )
        ),
        "observation_started_at": updates.get("observed_at", NOW),
        "observed_at": NOW,
    }
    values.update(updates)
    return deployment.QualificationLinuxDeploymentObservation(**values)


def _replace_custody_root(
    observation: deployment.QualificationLinuxDeploymentObservation,
    purpose: str,
    **updates: object,
) -> deployment.QualificationLinuxDeploymentObservation:
    return observation.model_copy(
        update={
            "custody_roots": tuple(
                root.model_copy(update=updates) if root.purpose == purpose else root
                for root in observation.custody_roots
            )
        }
    )


def _observer_pin(
    *, private_key: bytes = OBSERVER_PRIVATE_KEY
) -> deployment.QualificationDeploymentObserverPin:
    public_key = (
        Ed25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return deployment.QualificationDeploymentObserverPin(
        policy_sha256=_sha("deployment-observer-policy"),
        principal_id="principal:qualification-deployment-observer",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
    )


def _sign_observation(
    observation: deployment.QualificationLinuxDeploymentObservation,
    pin: deployment.QualificationDeploymentObserverPin,
    *,
    spec: deployment.QualificationDeploymentSpecV1,
    private_key: bytes = OBSERVER_PRIVATE_KEY,
    expires_at: datetime | None = None,
) -> deployment.SignedQualificationLinuxDeploymentObservation:
    unsigned = deployment.SignedQualificationLinuxDeploymentObservation(
        observation=observation,
        spec_sha256=spec.spec_sha256,
        rendered_systemd_units_sha256=canonical_sha256(deployment.render_systemd_units(spec)),
        rendered_postgresql_acl_sha256=hashlib.sha256(
            deployment.render_postgresql_acl(spec)
        ).hexdigest(),
        observer_policy_sha256=pin.policy_sha256,
        observer_principal_id=pin.principal_id,
        observer_key_id=pin.key_id,
        signed_at=observation.observed_at,
        expires_at=expires_at or observation.observed_at + timedelta(seconds=30),
        signature_ed25519_hex="0" * 128,
    )
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(unsigned.message_bytes)
    return unsigned.model_copy(update={"signature_ed25519_hex": signature.hex()})


class _Observer:
    def __init__(
        self, observation: deployment.SignedQualificationLinuxDeploymentObservation
    ) -> None:
        self.observation = observation
        self.calls = 0

    def observe(self, *, spec, rendered_units, postgresql_acl):
        assert spec.deployment_id == self.observation.observation.deployment_id
        assert canonical_sha256(rendered_units) == canonical_sha256(
            deployment.render_systemd_units(spec)
        )
        assert postgresql_acl == deployment.render_postgresql_acl(spec)
        self.calls += 1
        return self.observation


def _freeze(monkeypatch, spec=None, observation=None, signed=None, pin=None, frozen_at=NOW):
    spec = spec or _spec()
    observation = observation or _observation(spec)
    pin = pin or _observer_pin()
    signed = signed or _sign_observation(observation, pin, spec=spec)
    observer = _Observer(signed)
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: frozen_at)
    return (
        deployment.freeze_installed_manifest(
            spec,
            observer,
            pin,
        ),
        observer,
        pin,
    )


def _verify(monkeypatch, manifest, observer, pin, *, verified_at):
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: verified_at)
    return deployment.verify_installed_manifest(manifest, observer, pin)


def test_spec_is_closed_hashed_and_cannot_grant_scientific_authority() -> None:
    spec = _spec()
    assert spec.spec_sha256 == canonical_sha256(spec)
    assert spec.qualification_only is True
    assert spec.scientific_admission_allowed is False
    assert spec.automatic_installation is False
    assert spec.automatic_start is False
    assert len(deployment.EXECUTION_TABLES) == 27

    with pytest.raises(ValidationError, match="extra_forbidden"):
        deployment.QualificationDeploymentSpecV1.model_validate(
            {**spec.model_dump(mode="python"), "unexpected": True}
        )
    with pytest.raises(ValidationError, match="scientific_admission_allowed"):
        _spec(scientific_admission_allowed=True)


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {"quota_state_root": "/var/lib/aletheia/quota-backing/state"},
            "custody roots must not overlap",
        ),
        (
            {"node_runner_path": "/var/lib/aletheia/node-state/run-node.py"},
            "runners must be distinct children",
        ),
        (
            {"postgresql_outbox_role": "aletheia_exec_allocator"},
            "roles must be distinct",
        ),
        ({"docker_gid": 2101}, "UID/GID identities must be distinct"),
        ({"apparmor_profile_name": "unconfined"}, "cannot select unconfined"),
    ),
)
def test_spec_rejects_path_identity_and_policy_overlap(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _spec(**updates)


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/etc/aletheia/qualification/../manifest.json",
        "/etc/aletheia/qualification/%n.json",
        "/etc/aletheia/qualification/$MANIFEST.json",
        "/etc/aletheia/qualification/manifest\\name.json",
        '/etc/aletheia/qualification/manifest"name.json',
        "/etc/aletheia/qualification/manifest'name.json",
    ),
)
def test_spec_rejects_systemd_path_expansion_and_noncanonical_components(
    unsafe_path: str,
) -> None:
    with pytest.raises(ValidationError, match="systemd-safe absolute path"):
        _spec(deployment_manifest_path=unsafe_path)


@pytest.mark.parametrize(
    "manifest_path",
    (
        "/opt/aletheia/release/runtime-manifest.json",
        "/var/lib/aletheia/node-state/runtime-manifest.json",
    ),
)
def test_deployment_manifest_cannot_overlap_code_or_worker_custody(
    manifest_path: str,
) -> None:
    with pytest.raises(ValidationError, match="manifest path must not overlap"):
        _spec(deployment_manifest_path=manifest_path)


def test_live_custody_root_projection_is_exhaustive_and_role_scoped() -> None:
    spec = _spec()
    observed = _observation(spec)
    roots = {root.purpose: root for root in observed.custody_roots}
    assert tuple(roots) == deployment._CUSTODY_ROOT_PURPOSES
    assert {
        purpose: (root.path, root.owner_uid, root.owner_gid, root.mode)
        for purpose, root in roots.items()
    } == deployment._expected_custody_root_policies(spec)
    assert roots["workspace_source"].device == observed.output_workspace_root.device
    assert roots["workspace_source"].inode == observed.output_workspace_root.inode
    assert all(root.parent_chain_root_controlled for root in roots.values())


@pytest.mark.parametrize("purpose", deployment._CUSTODY_ROOT_PURPOSES)
@pytest.mark.parametrize("field", ("path", "owner_uid", "owner_gid", "mode"))
def test_live_custody_root_policy_drift_cannot_be_first_frozen(
    monkeypatch, purpose: str, field: str
) -> None:
    spec = _spec()
    observed = _observation(spec)
    root = next(item for item in observed.custody_roots if item.purpose == purpose)
    changed_value: object = (
        f"{root.path}-drift"
        if field == "path"
        else root.mode ^ 0o040
        if field == "mode"
        else getattr(root, field) + 1
    )
    changed = _replace_custody_root(observed, purpose, **{field: changed_value})
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="files:service-custody-root-drift",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


def test_portable_expected_files_and_code_tree_contain_no_live_linux_inode_identity() -> None:
    assert {
        "device",
        "inode",
        "parent_chain_sha256",
    }.isdisjoint(deployment.QualificationExpectedRootFile.model_fields)
    assert {
        "device",
        "inode",
        "parent_chain_sha256",
    }.isdisjoint(deployment.QualificationExpectedRootExecutable.model_fields)
    spec = _spec()
    assert spec.reviewed_code_tree.exhaustive is True
    assert spec.reviewed_code_tree.symlinks_allowed is False
    assert spec.agent_implementation_sha256 == (
        deployment.qualification_agent_implementation_sha256(
            reviewed_code_tree=spec.reviewed_code_tree,
            reviewed_python_environment=spec.reviewed_python_environment,
            expected_python_executable=spec.expected_python_executable,
            expected_runners=(
                spec.expected_workspace_runner,
                spec.expected_quota_runner,
                spec.expected_watchdog_runner,
                spec.expected_node_runner,
                spec.expected_outbox_runner,
            ),
            expected_service_modules=(
                spec.expected_quota_service_module,
                spec.expected_watchdog_service_module,
            ),
            expected_python_import_paths=spec.expected_python_import_paths,
        )
    )
    with pytest.raises(ValidationError, match="agent implementation hash must derive"):
        _spec(agent_implementation_sha256=_sha("independent-agent-hash"))
    with pytest.raises(ValidationError, match="code-tree manifest hash is not derived"):
        deployment.QualificationReviewedCodeTree.model_validate(
            {
                **spec.reviewed_code_tree.model_dump(mode="python"),
                "manifest_sha256": _sha("caller-authored-tree-hash"),
            }
        )


def test_spec_mechanically_binds_each_runner_to_the_exhaustive_code_tree() -> None:
    spec = _spec()
    changed = spec.expected_node_runner.model_copy(
        update={"reviewed_sha256": _sha("unreviewed-node-runner")}
    )
    with pytest.raises(ValidationError, match="exact entry in the exhaustive code tree"):
        _spec(expected_node_runner=changed)


def test_spec_requires_one_reviewed_site_packages_path_after_code_root() -> None:
    spec = _spec()
    with pytest.raises(ValidationError, match="code root then one reviewed site-packages"):
        _spec(
            expected_python_import_paths=(
                spec.code_root,
                f"{spec.reviewed_python_environment.root_path}/lib/python3.12",
            )
        )


def test_systemd_render_is_deterministic_and_preserves_required_service_boundaries() -> None:
    spec = _spec()
    first = deployment.render_systemd_units(spec)
    second = deployment.render_systemd_units(spec)
    assert first == second
    assert tuple(item.unit_name for item in first) == tuple(
        sorted(
            (
                spec.workspace_unit_name,
                spec.quota_unit_name,
                spec.watchdog_unit_name,
                spec.node_unit_name,
                spec.outbox_unit_name,
            )
        )
    )
    assert canonical_sha256(first) == (
        "78e1282509ee39e1efb4ff0e41335979d94e70d838c5cffe8c38c6e7e3c0d337"
    )

    by_name = {item.unit_name: item.content for item in first}
    workspace = by_name[spec.workspace_unit_name]
    quota = by_name[spec.quota_unit_name]
    watchdog = by_name[spec.watchdog_unit_name]
    node = by_name[spec.node_unit_name]
    outbox = by_name[spec.outbox_unit_name]
    assert "User=root" in workspace and "PrivateMounts=no" in workspace
    assert "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SYS_ADMIN" in quota
    assert "User=root" in quota and "PrivateMounts=no" in quota
    assert "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_KILL CAP_SYS_ADMIN" in watchdog
    assert "Requires=docker.service" in watchdog and "PrivateMounts=no" in watchdog
    assert f"User={spec.node_uid}" in node
    assert f"SupplementaryGroups={spec.docker_gid}" in node
    assert "CapabilityBoundingSet=\n" in node and "PrivateMounts=no" in node
    assert f"run --poll-milliseconds {spec.worker_poll_milliseconds}" in node
    assert all(
        "--poll-milliseconds" not in content for content in (workspace, quota, watchdog, outbox)
    )
    assert f"User={spec.outbox_uid}" in outbox and "PrivateMounts=yes" in outbox
    assert "SupplementaryGroups=\n" in outbox
    assert all(" -S -s -P " in item.content for item in first)
    assert all(
        f"--manifest-sha256 {spec.deployment_manifest_sha256}" in item.content for item in first
    )
    assert all(f"WorkingDirectory={spec.code_root}" in item.content for item in first)
    assert all(
        f"Environment=PYTHONHOME={spec.reviewed_python_environment.root_path}" in item.content
        and f"Environment=PYTHONPATH={spec.code_root}" in item.content
        and (
            "Environment=ALETHEIA_QUALIFICATION_SITE_PACKAGES="
            f"{spec.expected_python_import_paths[1]}"
        )
        in item.content
        and "Environment=PYTHONNOUSERSITE=1" in item.content
        and "UnsetEnvironment=LD_LIBRARY_PATH LD_PRELOAD" in item.content
        for item in first
    )
    assert all("/bin/sh" not in item.content for item in first)
    node_url = deployment.qualification_postgresql_peer_database_url(
        spec,
        role_name=spec.postgresql_allocator_role,
    )
    outbox_url = deployment.qualification_postgresql_peer_database_url(
        spec,
        role_name=spec.postgresql_outbox_role,
    )
    assert f"Environment=ALETHEIA_DATABASE_URL={node_url}" in node
    assert f"Environment=ALETHEIA_DATABASE_URL={outbox_url}" in outbox
    assert "%" not in node_url
    assert "%" not in outbox_url
    assert all("ALETHEIA_DATABASE_URL" not in content for content in (workspace, quota, watchdog))
    assert all("PGHOST" in item.content and "PGPASSWORD" in item.content for item in first)
    for value, role in (
        (node_url, spec.postgresql_allocator_role),
        (outbox_url, spec.postgresql_outbox_role),
    ):
        parsed = make_url(value)
        assert parsed.username == role
        assert parsed.password is None
        assert parsed.database == spec.postgresql_database
        assert parsed.host is None
        assert parsed.query == {"host": deployment.QUALIFICATION_POSTGRESQL_SOCKET_DIRECTORY}
    identities = deployment._expected_systemd_service_identities(spec)
    assert {identity.unit_name: identity.worker_poll_milliseconds for identity in identities} == {
        spec.node_unit_name: spec.worker_poll_milliseconds,
        spec.outbox_unit_name: None,
        spec.quota_unit_name: None,
        spec.watchdog_unit_name: None,
        spec.workspace_unit_name: None,
    }


def test_postgresql_peer_url_rejects_foreign_roles() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="outside qualification"):
        deployment.qualification_postgresql_peer_database_url(
            spec,
            role_name=spec.postgresql_owner_role,
        )


@pytest.mark.parametrize(
    "identity_update",
    (
        lambda identity: {
            "fragment_path": f"/run/systemd/system/{identity.unit_name}",
        },
        lambda identity: {"loaded_fragment_sha256": _sha("stale-loaded-unit")},
        lambda identity: {"daemon_reload_generation_matches_fragment": False},
        lambda identity: {
            "drop_in_paths": (f"/etc/systemd/system/{identity.unit_name}.d/override.conf",)
        },
        lambda identity: {"exec_start_argvs": (("/bin/false",),)},
        lambda identity: {
            "exec_start_argvs": (*identity.exec_start_argvs, ("/bin/false", "extra"))
        },
        lambda identity: {"exec_start_pre_argvs": (("/bin/false", "prepare"),)},
        lambda identity: {"exec_start_post_argvs": (("/bin/false", "post"),)},
        lambda identity: {
            "effective_environment": (
                "PYTHONHOME=/tmp/unreviewed-runtime",
                *identity.effective_environment[1:],
            )
        },
        lambda identity: {
            "unset_environment_names": tuple(
                name for name in identity.unset_environment_names if name != "LD_PRELOAD"
            )
        },
    ),
)
def test_effective_loaded_systemd_state_drift_fails_closed(monkeypatch, identity_update) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    identities = list(current.systemd_service_identities)
    identities[0] = identities[0].model_copy(update=identity_update(identities[0]))
    changed = current.model_copy(update={"systemd_service_identities": tuple(identities)})
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "systemd:unit-bytes-or-custody-drift" in report.blockers
    assert report.ready_for_opt_in_campaign is False


def test_postgresql_acl_is_deterministic_explicit_and_contains_no_secret() -> None:
    spec = _spec()
    first = deployment.render_postgresql_acl(spec)
    second = deployment.render_postgresql_acl(spec)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == (
        "ece5c444d9abc39c25d637de07d26e1e6d97d9200cc5ac3c1b54f6c6bd783c85"
    )
    text = first.decode()
    assert text.count("ALTER TABLE public.") == 27
    assert "GRANT SELECT, INSERT ON" in text
    assert "GRANT UPDATE ON" in text
    assert (
        'GRANT USAGE ON SEQUENCE public."execution_budget_events_event_id_seq" '
        'TO "aletheia_exec_allocator";'
    ) in text
    assert (
        'ALTER SEQUENCE public."execution_budget_events_event_id_seq" '
        'OWNER TO "aletheia_exec_owner";'
    ) in text
    assert "PRECONDITION: application roles have no direct or transitive role memberships" in text
    assert "exact routine catalog projection sha256" in text
    assert "exact trigger catalog projection sha256" in text
    assert "exact sequence catalog projection sha256" in text
    assert "owner role retains memberships" in text
    assert "pg_catalog.aclexplode" in text
    assert text.count("pg_catalog.aclexplode(attribute.attacl)") == 2
    assert "ARRAY[]::pg_catalog.aclitem[]" not in text
    assert text.count("unnest(routine.proargtypes::oid[])") == 2
    assert "pg_get_function_identity_arguments" not in text
    assert "FROM pg_catalog.pg_attribute AS attribute" in text
    assert "REVOKE %s (%I) ON TABLE public.%I FROM %s" in text
    assert 'REVOKE ALL PRIVILEGES ON DATABASE "aletheia_qualification"' in text
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public" in text
    assert 'ALTER SCHEMA public OWNER TO "aletheia_exec_owner"' in text
    assert (
        'REVOKE ALL PRIVILEGES ON FUNCTION public."aletheia_execution_guard_attempt_v1"()'
    ) in text
    assert (
        "REVOKE ALL PRIVILEGES ON PROCEDURE "
        'public."aletheia_execution_archive_v1"(timestamp with time zone)'
    ) in text
    assert "execution routine signature set is not exact" in text
    assert "privilege.is_grantable" in text
    assert "unexpected grantee" in text
    assert "REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER" in text
    assert "GRANT USAGE ON SCHEMA public" in text
    assert "NOBYPASSRLS" in text and "NOINHERIT" in text
    assert "CREATE ROLE" not in text
    assert "PASSWORD" not in text
    assert "GRANT DELETE" not in text
    assert "GRANT TRUNCATE" not in text
    assert deployment.postgresql_role_privileges_sha256(
        spec, role_name=spec.postgresql_allocator_role
    ) != deployment.postgresql_role_privileges_sha256(spec, role_name=spec.postgresql_outbox_role)


def test_postgresql_acl_scopes_routine_ownership_to_execution_namespace() -> None:
    spec = _spec()
    text = deployment.render_postgresql_acl(spec).decode()
    unrelated = _observation(spec).postgresql_non_execution_public_routine_owners[0]
    assert "left(routine.proname, 19)" in text
    assert "routine.proname LIKE 'aletheia_execution_%'" not in text
    assert "protected roles own a non-execution public routine" in text
    assert unrelated.routine_name not in text
    assert unrelated.owner_role != spec.postgresql_owner_role
    assert text.count("ALTER FUNCTION public.") == sum(
        routine.routine_kind == "function" for routine in spec.expected_postgresql_routines
    )


def test_unrelated_public_routine_owner_drift_fails_closed(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    unrelated = current.postgresql_non_execution_public_routine_owners[0]
    changed = current.model_copy(
        update={
            "postgresql_non_execution_public_routine_owners": (
                unrelated.model_copy(update={"owner_role": spec.postgresql_owner_role}),
            )
        }
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "postgresql:grant-membership-or-owner-closure-drift" in report.blockers


@pytest.mark.parametrize(
    "protected_role",
    (
        "postgresql_owner_role",
        "postgresql_allocator_role",
        "postgresql_outbox_role",
    ),
)
def test_protected_role_cannot_own_unrelated_public_routine_at_first_freeze(
    monkeypatch, protected_role: str
) -> None:
    spec = _spec()
    observed = _observation(spec)
    unrelated = observed.postgresql_non_execution_public_routine_owners[0]
    changed = observed.model_copy(
        update={
            "postgresql_non_execution_public_routine_owners": (
                unrelated.model_copy(update={"owner_role": getattr(spec, protected_role)}),
            )
        }
    )
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="postgresql:grant-membership-or-owner-closure-drift",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


@pytest.mark.parametrize(
    "sequence_update",
    (
        {"persistence": "unlogged"},
        {"owned_by_table": None, "owned_by_column": None},
    ),
)
def test_sequence_persistence_and_owned_by_mutations_fail_closed(
    monkeypatch, sequence_update: dict[str, object]
) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    changed = current.model_copy(
        update={
            "postgresql_sequences": (
                current.postgresql_sequences[0].model_copy(update=sequence_update),
            )
        }
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "postgresql:grant-membership-or-owner-closure-drift" in report.blockers


def test_postgresql_grant_option_is_explicitly_rejected(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    grant_option = deployment.PostgreSQLUnexpectedPrivilegeObservation(
        object_kind="table",
        object_identity="execution_attempts",
        grantee=spec.postgresql_allocator_role,
        privilege_type="SELECT",
        is_grantable=True,
    )
    changed = current.model_copy(update={"postgresql_unexpected_grant_options": (grant_option,)})
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "postgresql:grant-membership-or-owner-closure-drift" in report.blockers
    assert report.ready_for_opt_in_campaign is False


def test_darwin_cannot_freeze_an_installed_manifest(monkeypatch) -> None:
    spec = _spec()

    class _MustNotRun:
        def observe(self, **_kwargs):
            raise AssertionError("non-Linux freeze must not call the observer")

    monkeypatch.setattr(deployment.sys, "platform", "darwin")
    with pytest.raises(
        deployment.QualificationDeploymentEnvironmentError,
        match="only on the real Linux target",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _MustNotRun(),
            _observer_pin(),
        )


def test_linux_observation_freezes_only_derived_nonqualified_manifest(monkeypatch) -> None:
    manifest, observer, _pin = _freeze(monkeypatch)
    assert observer.calls == 1
    assert manifest.spec_sha256 == manifest.spec.spec_sha256
    assert manifest.installed_observation_sha256 == (
        manifest.installed_observation.signed_observation_sha256
    )
    assert manifest.installed_stable_evidence_sha256 == (
        manifest.installed_observation.observation.stable_evidence_sha256
    )
    assert manifest.deployment_qualified is False
    assert manifest.qualification_only is True
    assert manifest.scientific_admission_allowed is False

    with pytest.raises(ValidationError, match="derived evidence"):
        deployment.QualificationInstalledDeploymentManifestV1.model_validate(
            {**manifest.model_dump(mode="python"), "postgresql_acl_sha256": _sha("changed")}
        )


def test_darwin_verification_never_calls_observer_or_claims_campaign_readiness(monkeypatch) -> None:
    manifest, _observer, pin = _freeze(monkeypatch)

    class _MustNotRun:
        def observe(self, **_kwargs):
            raise AssertionError("Darwin verification must not inspect fake Linux state")

    monkeypatch.setattr(deployment.sys, "platform", "darwin")
    report = _verify(
        monkeypatch,
        manifest,
        _MustNotRun(),
        pin,
        verified_at=NOW + timedelta(seconds=1),
    )
    assert report.blockers == ("host:linux-required",)
    assert report.ready_for_opt_in_campaign is False
    assert report.campaign_executed is False
    assert report.deployment_qualified is False
    assert report.scientific_admission_allowed is False
    assert not any(
        (
            report.linux_systemd_cgroup_verified,
            report.shared_mount_namespace_verified,
            report.installed_files_verified,
            report.systemd_units_verified,
            report.postgresql_acl_verified,
        )
    )


def test_exact_linux_reobservation_is_only_ready_for_later_opt_in_campaign(monkeypatch) -> None:
    spec = _spec()
    installed = _observation(spec)
    manifest, _observer, pin = _freeze(monkeypatch, spec, installed)
    reobserved = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(reobserved, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert report.blockers == ()
    assert report.ready_for_opt_in_campaign is True
    assert report.campaign_executed is False
    assert report.deployment_qualified is False
    assert report.scientific_admission_allowed is False


def test_legitimate_reboot_and_new_shared_mount_namespace_can_remain_ready(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current_boot = _observation(
        spec,
        observed_at=NOW + timedelta(seconds=1),
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        pid_one_mount_namespace="mnt:[4026532999]",
        quota_mount_namespace="mnt:[4026532999]",
        node_mount_namespace="mnt:[4026532999]",
        docker_mount_namespace="mnt:[4026532999]",
    )
    current_workspace = current_boot.output_workspace_root.model_copy(
        update={
            "device": 121,
            "inode": 1301,
            "mount_id": 1401,
            "parent_chain_sha256": _sha("workspace-parent-after-reboot"),
        }
    )
    rebooted_custody_roots = tuple(
        root.model_copy(
            update={
                "device": (
                    current_workspace.device
                    if root.purpose == "workspace_source"
                    else root.device + 100
                ),
                "inode": (
                    current_workspace.inode
                    if root.purpose == "workspace_source"
                    else root.inode + 100
                ),
                "parent_chain_sha256": _sha(f"custody-parent-after-reboot:{root.purpose}"),
            }
        )
        for root in current_boot.custody_roots
    )
    after_reboot = current_boot.model_copy(
        update={
            "output_workspace_root": current_workspace,
            "custody_roots": rebooted_custody_roots,
            "quota_deployment": current_boot.quota_deployment.model_copy(
                update={
                    "workspace_root_pin": current_workspace,
                    "socket_parent_device": 122,
                    "socket_parent_inode": 1308,
                    "socket_parent_parent_chain_sha256": _sha("quota-socket-parent-after-reboot"),
                }
            ),
            "watchdog_deployment": current_boot.watchdog_deployment.model_copy(
                update={
                    "socket_parent_device": 122,
                    "socket_parent_inode": 1311,
                    "socket_parent_parent_chain_sha256": _sha(
                        "watchdog-socket-parent-after-reboot"
                    ),
                }
            ),
        }
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(after_reboot, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert report.blockers == ()
    assert report.ready_for_opt_in_campaign is True


@pytest.mark.parametrize("field", ("device", "inode", "parent_chain_sha256"))
def test_same_boot_custody_root_identity_drift_fails_closed(monkeypatch, field: str) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    root = current.custody_roots[0]
    changed_value: object = (
        _sha("changed-custody-parent")
        if field == "parent_chain_sha256"
        else getattr(root, field) + 1
    )
    changed = _replace_custody_root(
        current,
        root.purpose,
        **{field: changed_value},
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "files:service-custody-root-drift" in report.blockers
    assert report.custody_roots_verified is False


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    (
        ("workspace", "host:shared-mount-namespace-drift"),
        ("quota_socket", "quota:deployment-drift"),
        ("watchdog_socket", "watchdog:deployment-drift"),
    ),
)
def test_same_boot_dynamic_identity_changes_fail_closed(
    monkeypatch, mutation: str, blocker: str
) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    updates: dict[str, object] = {}
    if mutation == "workspace":
        workspace = current.output_workspace_root.model_copy(
            update={"mount_id": current.output_workspace_root.mount_id + 1}
        )
        updates = {
            "output_workspace_root": workspace,
            "quota_deployment": current.quota_deployment.model_copy(
                update={"workspace_root_pin": workspace}
            ),
        }
    elif mutation == "quota_socket":
        updates = {
            "quota_deployment": current.quota_deployment.model_copy(
                update={"socket_parent_inode": current.quota_deployment.socket_parent_inode + 1}
            )
        }
    else:
        updates = {
            "watchdog_deployment": current.watchdog_deployment.model_copy(
                update={"socket_parent_inode": current.watchdog_deployment.socket_parent_inode + 1}
            )
        }
    changed = current.model_copy(update=updates)
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert blocker in report.blockers
    assert report.ready_for_opt_in_campaign is False


def test_verify_reads_monitored_now_only_after_observer_returns(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    signed = _sign_observation(
        _observation(spec, observed_at=NOW + timedelta(seconds=1)),
        pin,
        spec=spec,
    )
    state = {"returned": False}

    class _ImmediateObserver:
        def observe(self, **_kwargs):
            state["returned"] = True
            return signed

    def monitored_now() -> datetime:
        assert state["returned"] is True
        return NOW + timedelta(seconds=2)

    monkeypatch.setattr(deployment, "_monitored_utc_now", monitored_now)
    report = deployment.verify_installed_manifest(manifest, _ImmediateObserver(), pin)
    assert report.blockers == ()
    assert report.ready_for_opt_in_campaign is True


def test_observer_cannot_hide_an_overlong_observation_behind_a_fresh_signature(
    monkeypatch,
) -> None:
    spec = _spec(maximum_observation_duration_seconds=5)
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(
        spec,
        observation_started_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=7),
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(current, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=8),
    )
    assert report.blockers == ("observation:rollback-or-stale",)
    assert report.observation_freshness_verified is False


def test_root_service_capabilities_close_0700_traversal_and_chown_paths() -> None:
    spec = _spec()
    units = {item.unit_name: item.content for item in deployment.render_systemd_units(spec)}
    observed = _observation(spec)
    identities = {item.unit_name: item for item in observed.systemd_service_identities}

    assert observed.quota_deployment.backing_root_mode == 0o700
    assert observed.quota_deployment.state_root_mode == 0o700
    assert observed.watchdog_deployment.journal_root_mode == 0o700
    assert observed.watchdog_deployment.state_root_mode == 0o700
    assert identities[spec.quota_unit_name].effective_capabilities == (
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_SYS_ADMIN",
    )
    assert identities[spec.watchdog_unit_name].effective_capabilities == (
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_KILL",
        "CAP_SYS_ADMIN",
    )
    assert "CAP_FOWNER" not in identities[spec.watchdog_unit_name].effective_capabilities
    assert "CAP_FOWNER" in units[spec.quota_unit_name]
    assert "CAP_DAC_OVERRIDE" in units[spec.watchdog_unit_name]


@pytest.mark.parametrize(
    ("builder", "message"),
    (
        (
            lambda spec: spec.reviewed_code_tree.model_copy(update={"expected_root_mode": 0o500}),
            "worker-traversable",
        ),
        (
            lambda spec: spec.expected_node_runner.model_copy(update={"expected_mode": 0o400}),
            "worker-readable",
        ),
        (
            lambda spec: spec.expected_python_executable.model_copy(
                update={"expected_mode": 0o500}
            ),
            "worker-readable/executable",
        ),
    ),
)
def test_portable_modes_reject_false_ready_worker_access(builder, message: str) -> None:
    spec = _spec()
    field = (
        "reviewed_code_tree"
        if isinstance(builder(spec), deployment.QualificationReviewedCodeTree)
        else (
            "expected_python_executable"
            if isinstance(builder(spec), deployment.QualificationExpectedRootExecutable)
            else "expected_node_runner"
        )
    )
    with pytest.raises(ValidationError, match=message):
        deployment.QualificationDeploymentSpecV1.model_validate(
            {**spec.model_dump(mode="python"), field: builder(spec)}
        )

    with pytest.raises(ValidationError, match="canonical root-controlled"):
        deployment.QualificationReviewedCodeDirectory.model_validate(
            {
                **spec.reviewed_code_tree.directories[0].model_dump(mode="python"),
                "expected_mode": 0o500,
            }
        )


def test_portable_modes_require_root_and_worker_access_bits() -> None:
    with pytest.raises(ValidationError, match="worker-readable"):
        deployment.QualificationExpectedRootFile(
            path="/opt/aletheia/readable.json",
            reviewed_sha256=_sha("readable"),
            expected_mode=0o004,
        )
    with pytest.raises(ValidationError, match="worker-readable/executable"):
        deployment.QualificationExpectedRootExecutable(
            path="/opt/aletheia/tool",
            reviewed_sha256=_sha("tool"),
            expected_mode=0o005,
        )
    with pytest.raises(ValidationError, match="root-controlled custody"):
        deployment.QualificationReviewedCodeDirectory(
            relative_path="package",
            expected_mode=0o005,
        )
    with pytest.raises(ValidationError, match="native dependency"):
        deployment.ReviewedNativeDependencyFile(
            path="/opt/aletheia/native/libbad.so",
            reviewed_sha256=_sha("libbad"),
            expected_mode=0o004,
            executable_required=False,
        )


def test_seccomp_profile_is_a_portable_worker_readable_pin() -> None:
    spec = _spec()
    assert spec.expected_seccomp_profile.path == spec.seccomp_profile_path
    assert spec.expected_seccomp_profile.reviewed_sha256 == spec.seccomp_profile_sha256
    assert spec.expected_seccomp_profile.expected_mode == 0o444
    with pytest.raises(ValidationError, match="worker-readable"):
        deployment.QualificationExpectedRootFile.model_validate(
            {
                **spec.expected_seccomp_profile.model_dump(mode="python"),
                "expected_mode": 0o400,
            }
        )


@pytest.mark.parametrize("field", ("sha256", "mode"))
def test_seccomp_profile_drift_cannot_be_frozen(monkeypatch, field: str) -> None:
    spec = _spec()
    observed = _observation(spec)
    value = _sha("unreviewed-seccomp") if field == "sha256" else 0o400
    changed = observed.model_copy(
        update={"seccomp_profile": observed.seccomp_profile.model_copy(update={field: value})}
    )
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="files:installed-custody-drift",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


def test_seccomp_live_inode_drift_fails_reverification(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    changed = current.model_copy(
        update={
            "seccomp_profile": current.seccomp_profile.model_copy(
                update={"inode": current.seccomp_profile.inode + 1}
            )
        }
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "files:installed-custody-drift" in report.blockers


@pytest.mark.parametrize(
    "closure_update",
    (
        lambda closure: {
            "elf_interpreter": closure.elf_interpreter.model_copy(
                update={"sha256": _sha("unreviewed-interpreter")}
            )
        },
        lambda closure: {
            "dependencies": (
                closure.dependencies[0].model_copy(
                    update={
                        "file": closure.dependencies[0].file.model_copy(
                            update={"sha256": _sha("unreviewed-libc")}
                        )
                    }
                ),
            )
        },
        lambda closure: {"executable_needed_sonames": ("libm.so.6",)},
        lambda closure: {
            "external_native_dependency_paths": ("/usr/local/lib/unreviewed-native.so",)
        },
    ),
)
def test_privileged_tool_native_dependency_drift_cannot_be_frozen(
    monkeypatch, closure_update
) -> None:
    spec = _spec()
    observed = _observation(spec)
    closures = list(observed.privileged_tool_native_closures)
    closures[0] = closures[0].model_copy(update=closure_update(closures[0]))
    changed = observed.model_copy(update={"privileged_tool_native_closures": tuple(closures)})
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="quota:deployment-drift",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


def test_reviewed_native_dependency_closure_rejects_unresolved_edges() -> None:
    closure = _spec().reviewed_privileged_tool_native_closures[0]
    with pytest.raises(ValidationError, match="unresolved DT_NEEDED"):
        deployment.ReviewedNativeDependencyClosure.model_validate(
            {
                **closure.model_dump(mode="python"),
                "executable_needed_sonames": ("libmissing.so.1",),
            }
        )


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    (
        (
            lambda spec, observed: observed.model_copy(
                update={"node_mount_namespace": "mnt:[4026531999]"}
            ),
            "host:shared-mount-namespace-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={"postgresql_acl_sha256": _sha("changed-acl")}
            ),
            "postgresql:acl-bytes-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(update={"schema_revision": "wrong"}),
            "postgresql:schema-or-server-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={"agent_implementation_sha256": _sha("changed-code")}
            ),
            "code:implementation-or-authority-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "python_executable": observed.python_executable.model_copy(
                        update={"inode": observed.python_executable.inode + 1}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "entrypoint_files": (
                        observed.entrypoint_files[0].model_copy(
                            update={"sha256": _sha("changed-runner")}
                        ),
                        *observed.entrypoint_files[1:],
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "deployment_manifest_file": observed.deployment_manifest_file.model_copy(
                        update={"sha256": _sha("changed-deployment-manifest")}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "code_root": observed.code_root.model_copy(
                        update={"inode": observed.code_root.inode + 1}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "code_root": observed.code_root.model_copy(
                        update={"tree_manifest_sha256": _sha("changed-code-tree")}
                    )
                }
            ),
            "code:implementation-or-authority-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_roles": (
                        observed.postgresql_roles[0].model_copy(
                            update={"can_truncate_execution_tables": True}
                        ),
                        observed.postgresql_roles[1],
                    )
                }
            ),
            "postgresql:role-privilege-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_roles": (
                        observed.postgresql_roles[0].model_copy(
                            update={"role_members": ("legacy_login",)}
                        ),
                        observed.postgresql_roles[1],
                    )
                }
            ),
            "postgresql:role-privilege-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_roles": (
                        observed.postgresql_roles[0].model_copy(
                            update={
                                "direct_role_memberships": ("pg_write_all_data",),
                                "transitive_role_memberships": ("pg_write_all_data",),
                                "dangerous_builtin_role_memberships": ("pg_write_all_data",),
                            }
                        ),
                        observed.postgresql_roles[1],
                    )
                }
            ),
            "postgresql:role-privilege-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_roles": (
                        observed.postgresql_roles[0].model_copy(
                            update={
                                "direct_role_memberships": ("legacy_writer",),
                                "transitive_role_memberships": ("legacy_writer",),
                            }
                        ),
                        observed.postgresql_roles[1],
                    )
                }
            ),
            "postgresql:role-privilege-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={"postgresql_owner_role_members": ("legacy_owner_member",)}
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_unexpected_column_grants": (
                        deployment.PostgreSQLUnexpectedPrivilegeObservation(
                            object_kind="column",
                            object_identity="execution_outbox",
                            column_name="status",
                            grantee="legacy_writer",
                            privilege_type="UPDATE",
                            is_grantable=False,
                        ),
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_unexpected_schema_grants": (
                        deployment.PostgreSQLUnexpectedPrivilegeObservation(
                            object_kind="schema",
                            object_identity="public",
                            grantee="legacy_writer",
                            privilege_type="CREATE",
                            is_grantable=False,
                        ),
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_unexpected_routine_execute_grants": (
                        deployment.PostgreSQLUnexpectedPrivilegeObservation(
                            object_kind=spec.expected_postgresql_routines[0].routine_kind,
                            object_identity=spec.expected_postgresql_routines[0].identity,
                            grantee="legacy_writer",
                            privilege_type="EXECUTE",
                            is_grantable=False,
                        ),
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_unexpected_table_grants": (
                        deployment.PostgreSQLUnexpectedPrivilegeObservation(
                            object_kind="table",
                            object_identity="execution_attempts",
                            grantee="legacy_writer",
                            privilege_type="SELECT",
                            is_grantable=False,
                        ),
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_execution_object_owners": tuple(
                        item.model_copy(update={"owner_role": "legacy_owner"})
                        if item.object_kind == "function"
                        else item
                        for item in observed.postgresql_execution_object_owners
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
        (
            lambda spec, observed: observed.model_copy(
                update={
                    "postgresql_execution_object_owners": (
                        observed.postgresql_execution_object_owners[0].model_copy(
                            update={"owner_role": "legacy_owner"}
                        ),
                        *observed.postgresql_execution_object_owners[1:],
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
    ),
)
def test_linux_reobservation_drift_fails_closed(
    monkeypatch,
    mutate,
    blocker: str,
) -> None:
    spec = _spec()
    original = _observation(spec)
    manifest, _observer, pin = _freeze(monkeypatch, spec, original)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    changed = mutate(spec, current)
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert blocker in report.blockers
    assert report.ready_for_opt_in_campaign is False


def test_drifted_observation_cannot_be_frozen(monkeypatch) -> None:
    spec = _spec()
    changed = _observation(spec).model_copy(update={"docker_cgroup_driver": "cgroupfs"})
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="host:linux-systemd-cgroup-drift",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    (
        (
            lambda observed: observed.model_copy(update={"node_id": "node:foreign"}),
            "host:linux-systemd-cgroup-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={"node_manifest_sha256": _sha("foreign-node-manifest")}
            ),
            "host:linux-systemd-cgroup-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={"loaded_apparmor_profile_name": "unreviewed-profile"}
            ),
            "oci:image-layout-or-loaded-image-drift",
        ),
        (
            lambda observed: observed.model_copy(update={"apparmor_profile_enforcing": False}),
            "oci:image-layout-or-loaded-image-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "python_executable": observed.python_executable.model_copy(
                        update={"sha256": _sha("unreviewed-python")}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "entrypoint_files": (
                        observed.entrypoint_files[0].model_copy(update={"mode": 0o400}),
                        *observed.entrypoint_files[1:],
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "deployment_manifest_file": observed.deployment_manifest_file.model_copy(
                        update={"sha256": _sha("unreviewed-manifest")}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "code_root": observed.code_root.model_copy(
                        update={"tree_manifest_sha256": _sha("unreviewed-code-tree")}
                    )
                }
            ),
            "code:implementation-or-authority-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "python_environment_root": observed.python_environment_root.model_copy(
                        update={"tree_manifest_sha256": _sha("site-package-drift")}
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "python_external_loaded_native_object_paths": (
                        "/usr/local/lib/unreviewed-site-extension.so",
                    )
                }
            ),
            "files:installed-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "quota_deployment": observed.quota_deployment.model_copy(
                        update={
                            "losetup": observed.quota_deployment.losetup.model_copy(
                                update={"sha256": _sha("arbitrary-losetup")}
                            )
                        }
                    )
                }
            ),
            "quota:deployment-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "quota_deployment": observed.quota_deployment.model_copy(
                        update={
                            "systemd_unit": observed.quota_deployment.systemd_unit.model_copy(
                                update={"sha256": _sha("self-reported-unit")}
                            )
                        }
                    )
                }
            ),
            "quota:deployment-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "watchdog_deployment": observed.watchdog_deployment.model_copy(
                        update={"service_module_sha256": _sha("self-reported-service-module")}
                    )
                }
            ),
            "watchdog:deployment-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "systemd_service_identities": tuple(
                        item.model_copy(update={"supplementary_gids": (998,)})
                        if "outbox" in item.unit_name
                        else item
                        for item in observed.systemd_service_identities
                    )
                }
            ),
            "systemd:unit-bytes-or-custody-drift",
        ),
        (
            lambda observed: observed.model_copy(
                update={
                    "postgresql_unexpected_table_grants": (
                        deployment.PostgreSQLUnexpectedPrivilegeObservation(
                            object_kind="table",
                            object_identity="execution_attempts",
                            grantee="legacy_writer",
                            privilege_type="SELECT",
                            is_grantable=False,
                        ),
                    )
                }
            ),
            "postgresql:grant-membership-or-owner-closure-drift",
        ),
    ),
)
def test_unreviewed_files_and_postgresql_closure_cannot_be_first_frozen(
    monkeypatch,
    mutate,
    blocker: str,
) -> None:
    spec = _spec()
    changed = mutate(_observation(spec))
    pin = _observer_pin()
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(deployment.QualificationDeploymentObservationError, match=blocker):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(_sign_observation(changed, pin, spec=spec)),
            pin,
        )


@pytest.mark.parametrize(
    "update",
    (
        {"definition_sha256": _sha("mutated-routine-body")},
        {"language": "plpgsql"},
        {"security_definer": True},
        {"configuration": ("search_path=public",)},
        {"volatility": "immutable"},
    ),
)
def test_routine_catalog_mutations_fail_closed(monkeypatch, update: dict[str, object]) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    routines = list(current.postgresql_routines)
    # The SQL routine fixture makes every selected mutation differ from the reviewed projection.
    target = 1
    if all(getattr(routines[target], key) == value for key, value in update.items()):
        target = 0
    routines[target] = routines[target].model_copy(update=update)
    changed = current.model_copy(update={"postgresql_routines": tuple(routines)})
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "postgresql:grant-membership-or-owner-closure-drift" in report.blockers
    assert report.ready_for_opt_in_campaign is False


@pytest.mark.parametrize(
    "observation_update",
    (
        lambda observed: {
            "postgresql_triggers": (
                observed.postgresql_triggers[0].model_copy(
                    update={"definition_sha256": _sha("mutated-trigger-definition")}
                ),
            )
        },
        lambda observed: {
            "postgresql_triggers": (
                observed.postgresql_triggers[0].model_copy(update={"enabled": "disabled"}),
            )
        },
        lambda observed: {
            "postgresql_sequences": (
                observed.postgresql_sequences[0].model_copy(update={"cache_size": 32}),
            )
        },
        lambda observed: {"postgresql_owner_role_inherits": True},
        lambda observed: {
            "postgresql_owner_direct_role_memberships": ("legacy_owner_parent",),
            "postgresql_owner_transitive_role_memberships": ("legacy_owner_parent",),
        },
        lambda observed: {
            "postgresql_owner_direct_role_memberships": ("pg_write_all_data",),
            "postgresql_owner_transitive_role_memberships": ("pg_write_all_data",),
            "postgresql_owner_dangerous_builtin_role_memberships": ("pg_write_all_data",),
        },
    ),
)
def test_trigger_sequence_and_owner_membership_mutations_fail_closed(
    monkeypatch, observation_update
) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    changed = current.model_copy(update=observation_update(current))
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "postgresql:grant-membership-or-owner-closure-drift" in report.blockers
    assert report.ready_for_opt_in_campaign is False


def test_signed_host_clock_unsynchronized_fails_closed(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    changed = _observation(
        spec,
        observed_at=NOW + timedelta(seconds=1),
        host_clock_synchronized=False,
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(_sign_observation(changed, pin, spec=spec)),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert "host:linux-systemd-cgroup-drift" in report.blockers
    assert "postgresql:clock-unhealthy" in report.blockers


def test_unavailable_linux_observer_returns_canonical_blocker(monkeypatch) -> None:
    manifest, _observer, pin = _freeze(monkeypatch)

    class _Unavailable:
        def observe(self, **_kwargs):
            raise OSError("target unavailable")

    report = _verify(
        monkeypatch,
        manifest,
        _Unavailable(),
        pin,
        verified_at=NOW + timedelta(seconds=1),
    )
    assert report.blockers == ("observation:unavailable",)
    assert report.ready_for_opt_in_campaign is False


def test_external_observer_pin_rejects_forged_or_row_selected_signing_keys(monkeypatch) -> None:
    spec = _spec()
    observation = _observation(spec)
    trusted_pin = _observer_pin()
    signed = _sign_observation(observation, trusted_pin, spec=spec)
    forged = signed.model_copy(update={"signature_ed25519_hex": "0" * 128})
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="external observer pin",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(forged),
            trusted_pin,
        )

    foreign_private_key = bytes(range(33, 65))
    foreign_pin = _observer_pin(private_key=foreign_private_key)
    foreign_signed = _sign_observation(
        observation,
        foreign_pin,
        spec=spec,
        private_key=foreign_private_key,
    )
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="external observer pin",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(foreign_signed),
            trusted_pin,
        )
    assert "public_key_ed25519_hex" not in (
        deployment.SignedQualificationLinuxDeploymentObservation.model_fields
    )


def test_verify_rejects_forged_observer_signature_before_trusting_rows(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    forged = _sign_observation(current, pin, spec=spec).model_copy(
        update={"signature_ed25519_hex": "0" * 128}
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(forged),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert report.blockers == ("observer:provenance-invalid",)
    assert report.observer_provenance_verified is False
    assert report.ready_for_opt_in_campaign is False
    assert report.installed_files_verified is False


def test_signed_observation_binds_the_exact_spec_and_rendered_artifacts(monkeypatch) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=NOW + timedelta(seconds=1))
    wrong = _sign_observation(current, pin, spec=spec).model_copy(
        update={"spec_sha256": _sha("different-spec")}
    )
    signature = Ed25519PrivateKey.from_private_bytes(OBSERVER_PRIVATE_KEY).sign(wrong.message_bytes)
    wrong = wrong.model_copy(update={"signature_ed25519_hex": signature.hex()})
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(wrong),
        pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert report.blockers == ("observer:provenance-invalid",)
    assert report.ready_for_opt_in_campaign is False


@pytest.mark.parametrize(
    ("observed_at", "expires_at", "verified_at"),
    (
        (
            NOW,
            NOW + timedelta(seconds=30),
            NOW + timedelta(seconds=1),
        ),
        (
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=3),
        ),
        (
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=30),
            NOW + timedelta(seconds=2),
        ),
    ),
)
def test_verify_rejects_rollback_stale_and_future_observations(
    monkeypatch,
    observed_at: datetime,
    expires_at: datetime,
    verified_at: datetime,
) -> None:
    spec = _spec()
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec)
    current = _observation(spec, observed_at=observed_at)
    signed = _sign_observation(
        current,
        pin,
        spec=spec,
        expires_at=expires_at,
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(signed),
        pin,
        verified_at=verified_at,
    )
    assert report.blockers == ("observation:rollback-or-stale",)
    assert report.observer_provenance_verified is True
    assert report.observation_freshness_verified is False
    assert report.ready_for_opt_in_campaign is False


def test_monitored_now_rejects_a_2001_signed_observation_replay(monkeypatch) -> None:
    spec = _spec()
    broad_pin = _observer_pin().model_copy(
        update={
            "valid_from": datetime(2000, 1, 1, tzinfo=timezone.utc),
            "expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        }
    )
    manifest, _observer, _pin = _freeze(monkeypatch, spec=spec, pin=broad_pin)
    replay_time = datetime(2001, 1, 1, tzinfo=timezone.utc)
    replay = _observation(spec, observed_at=replay_time)
    signed = _sign_observation(
        replay,
        broad_pin,
        spec=spec,
        expires_at=replay_time + timedelta(seconds=30),
    )
    report = _verify(
        monkeypatch,
        manifest,
        _Observer(signed),
        broad_pin,
        verified_at=NOW + timedelta(seconds=2),
    )
    assert report.blockers == ("observation:rollback-or-stale",)
    assert report.observer_provenance_verified is True
    assert report.observation_freshness_verified is False


def test_freeze_and_verify_do_not_accept_caller_authored_clock_values(monkeypatch) -> None:
    spec = _spec()
    pin = _observer_pin()
    signed = _sign_observation(_observation(spec), pin, spec=spec)
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    with pytest.raises(TypeError, match="frozen_at"):
        deployment.freeze_installed_manifest(  # type: ignore[call-arg]
            spec,
            _Observer(signed),
            pin,
            frozen_at=NOW,
        )
    manifest, _observer, pin = _freeze(monkeypatch, spec=spec, pin=pin)
    with pytest.raises(TypeError, match="verified_at"):
        deployment.verify_installed_manifest(  # type: ignore[call-arg]
            manifest,
            _Observer(signed),
            pin,
            verified_at=NOW,
        )


def test_freeze_rejects_observation_outside_spec_ttl(monkeypatch) -> None:
    spec = _spec(observation_ttl_seconds=5, maximum_observation_duration_seconds=5)
    observation = _observation(spec)
    pin = _observer_pin()
    too_long = _sign_observation(
        observation,
        pin,
        spec=spec,
        expires_at=NOW + timedelta(seconds=6),
    )
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: NOW)
    with pytest.raises(
        deployment.QualificationDeploymentObservationError,
        match="stale, future-dated, or outside its pin",
    ):
        deployment.freeze_installed_manifest(
            spec,
            _Observer(too_long),
            pin,
        )


def test_verify_requires_the_same_external_observer_pin_as_the_manifest(monkeypatch) -> None:
    manifest, _observer, _pin = _freeze(monkeypatch)
    foreign_pin = _observer_pin(private_key=bytes(range(33, 65)))

    class _MustNotRun:
        def observe(self, **_kwargs):
            raise AssertionError("baseline pin mismatch must fail before target observation")

    report = _verify(
        monkeypatch,
        manifest,
        _MustNotRun(),
        foreign_pin,
        verified_at=NOW + timedelta(seconds=1),
    )
    assert report.blockers == ("observer:installed-provenance-invalid",)
    assert report.ready_for_opt_in_campaign is False


def test_observation_rejects_noncanonical_duplicate_file_and_role_sets() -> None:
    spec = _spec()
    observed = _observation(spec)
    with pytest.raises(ValidationError, match="entrypoint observations"):
        deployment.QualificationLinuxDeploymentObservation.model_validate(
            {
                **observed.model_dump(mode="python"),
                "entrypoint_files": (
                    observed.entrypoint_files[0],
                    observed.entrypoint_files[0],
                ),
            }
        )
    with pytest.raises(ValidationError, match="PostgreSQL role observations"):
        deployment.QualificationLinuxDeploymentObservation.model_validate(
            {
                **observed.model_dump(mode="python"),
                "postgresql_roles": (
                    observed.postgresql_roles[0],
                    observed.postgresql_roles[0],
                ),
            }
        )
    with pytest.raises(ValidationError, match="custody roots must be exhaustive"):
        deployment.QualificationLinuxDeploymentObservation.model_validate(
            {
                **observed.model_dump(mode="python"),
                "custody_roots": (
                    observed.custody_roots[0],
                    observed.custody_roots[0],
                    *observed.custody_roots[2:],
                ),
            }
        )


def test_preflight_rejects_a_caller_authored_ready_verdict(monkeypatch) -> None:
    manifest, _observer, _pin = _freeze(monkeypatch)
    with pytest.raises(ValidationError, match="verdict differs"):
        deployment.QualificationDeploymentPreflight(
            deployment_id=manifest.spec.deployment_id,
            spec_sha256=manifest.spec_sha256,
            installed_manifest_sha256=manifest.manifest_sha256,
            observed_at=NOW + timedelta(seconds=1),
            verified_at=NOW + timedelta(seconds=2),
            observer_provenance_verified=False,
            observation_freshness_verified=False,
            linux_systemd_cgroup_verified=False,
            shared_mount_namespace_verified=False,
            installed_files_verified=False,
            custody_roots_verified=False,
            systemd_units_verified=False,
            postgresql_acl_verified=False,
            postgresql_schema_verified=False,
            postgresql_roles_verified=False,
            postgresql_acl_closure_verified=False,
            postgresql_clock_verified=False,
            image_layout_verified=False,
            output_quota_service_verified=False,
            deadline_watchdog_service_verified=False,
            code_identity_verified=False,
            blockers=("host:linux-required",),
            ready_for_opt_in_campaign=True,
        )
