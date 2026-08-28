from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

import aletheia.execution.qualification_deployment as deployment
import aletheia.qualification_authority_commissioning as commissioning
import aletheia.qualification_campaign as campaign
import aletheia.qualification_installer as installer
import aletheia.qualification_observer as qualification_observer
from aletheia.execution.qualification_outbox_service import (
    QualificationTerminalSpoolEnvelopeV1,
)
from aletheia.execution.runtime_contracts import qualification_key_id
from aletheia.execution.runtime_v2_contracts import (
    MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
    AcceptedQualificationTerminalSubmission,
    OutputQuotaProvisioningReceipt,
)
from aletheia.execution.schemas import canonical_json_bytes, canonical_sha256
from aletheia.observations.execution_registration import (
    AtomicScientificExecutionRegistrationReceipt,
)
from aletheia.qualification_observer import (
    LinuxQualificationDeploymentObserver,
    QualificationDockerSecurityProjectionV1,
    QualificationLinuxObserverConfigV1,
    QualificationObserverError,
    QualificationObserverPrivateKeyPinV1,
    linux_capability_names_from_hex,
)
from .test_qualification_authority_commissioning import (
    ADMIN_URL,
    _FakeHost as _CommissioningHost,
    _clock as _commissioning_clock,
    _request as _commissioning_request,
)
from .test_qualification_deployment import (
    OBSERVER_PRIVATE_KEY,
    _observation,
    _sha,
    _sign_observation,
)
from .test_qualification_installer import _FakeHost as _InstallationHost

NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)


def test_reviewed_tree_streams_large_files_and_rejects_link_or_owner_drift(
    tmp_path: Path,
) -> None:
    payload = b"x" * (17 * 1024 * 1024)
    candidate = tmp_path / "libpython.so"
    candidate.write_bytes(payload)
    candidate.chmod(0o444)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = candidate.stat()

    observed_digest, observed = qualification_observer._hash_reviewed_tree_file(
        candidate,
        expected_sha256=digest,
        expected_byte_length=len(payload),
        expected_owner_uid=metadata.st_uid,
        expected_owner_gid=metadata.st_gid,
        expected_mode=0o444,
    )
    assert observed_digest == digest
    assert observed.st_size == len(payload)

    with pytest.raises(QualificationObserverError, match="custody differs"):
        qualification_observer._hash_reviewed_tree_file(
            candidate,
            expected_sha256=digest,
            expected_byte_length=len(payload),
            expected_owner_uid=metadata.st_uid + 1,
            expected_owner_gid=metadata.st_gid,
            expected_mode=0o444,
        )

    candidate.with_name("libpython-hardlink.so").hardlink_to(candidate)
    with pytest.raises(QualificationObserverError, match="custody differs"):
        qualification_observer._hash_reviewed_tree_file(
            candidate,
            expected_sha256=digest,
            expected_byte_length=len(payload),
            expected_owner_uid=metadata.st_uid,
            expected_owner_gid=metadata.st_gid,
            expected_mode=0o444,
        )


def test_observer_streams_large_reviewed_executable_without_relaxing_control_files(
    tmp_path: Path,
) -> None:
    payload = b"x" * (qualification_observer._MAX_FILE_BYTES + 1)  # noqa: SLF001
    candidate = tmp_path / "docker"
    candidate.write_bytes(payload)
    candidate.chmod(0o555)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = candidate.stat()

    observed_digest, observed, parent_chain_sha256 = (
        qualification_observer._stream_exact_executable(  # noqa: SLF001
            candidate,
            expected_sha256=digest,
            expected_owner_uid=metadata.st_uid,
            expected_owner_gid=metadata.st_gid,
            expected_mode=0o555,
        )
    )
    assert observed_digest == digest
    assert observed.st_size == len(payload)
    assert len(parent_chain_sha256) == 64

    with pytest.raises(QualificationObserverError, match="custody is unsafe"):
        qualification_observer._read_exact_file(candidate)  # noqa: SLF001
    with pytest.raises(QualificationObserverError, match="custody is unsafe"):
        qualification_observer._stream_exact_executable(  # noqa: SLF001
            candidate,
            expected_sha256=digest,
            expected_owner_uid=metadata.st_uid,
            expected_owner_gid=metadata.st_gid,
            expected_mode=0o555,
            maximum_bytes=qualification_observer._MAX_FILE_BYTES,  # noqa: SLF001
        )


def _clock_from(start: datetime):
    counter = 0

    def now() -> datetime:
        nonlocal counter
        counter += 1
        return start + timedelta(seconds=counter)

    return now


def _observer_pin() -> deployment.QualificationDeploymentObserverPin:
    public_key = (
        Ed25519PrivateKey.from_private_bytes(OBSERVER_PRIVATE_KEY)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return deployment.QualificationDeploymentObserverPin(
        policy_sha256=_sha("qualification-campaign-observer-policy"),
        principal_id="principal:qualification-campaign-observer",
        key_id=qualification_key_id(public_key),
        public_key_ed25519_hex=public_key,
        valid_from=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
    )


def _request_and_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    commissioning_request, private_payloads = _commissioning_request(monkeypatch, tmp_path)
    commissioning_receipt = commissioning.commission_qualification_authority(
        commissioning_request,
        _CommissioningHost(commissioning_request, private_payloads),
        clock=_commissioning_clock(),
    )
    installation_receipt = installer.install_qualification_service_files(
        commissioning_request.installation_request,
        _InstallationHost(),
        clock=_clock_from(commissioning_receipt.completed_at + timedelta(minutes=1)),
    )
    pin = _observer_pin()
    private_key_path = "/etc/aletheia/observer/qualification-observer.key"
    docker_projection = QualificationDockerSecurityProjectionV1(
        server_version="28.0.1",
        kernel_version="6.8.0-campaign",
        operating_system="Ubuntu 24.04 LTS",
        architecture="x86_64",
        storage_driver="overlay2",
        docker_root_dir="/var/lib/docker",
        docker_root_device=8,
        docker_root_inode=101,
        docker_root_mode=0o700,
        docker_root_parent_chain_sha256=_sha("docker-root-parent"),
        security_options=("name=apparmor", "name=seccomp,profile=default"),
    )
    prepared_at = installation_receipt.completed_at + timedelta(seconds=1)
    observer_config = QualificationLinuxObserverConfigV1(
        commissioning_request=commissioning_request,
        commissioning_receipt=commissioning_receipt,
        installation_receipt=installation_receipt,
        observer_pin=pin,
        observer_private_key=QualificationObserverPrivateKeyPinV1(
            path=private_key_path,
            file_sha256=hashlib.sha256(OBSERVER_PRIVATE_KEY).hexdigest(),
            key_id=pin.key_id,
        ),
        timedatectl_executable=commissioning_request.installation_request.systemctl_executable.model_copy(
            update={"path": "/usr/bin/timedatectl"}
        ),
        docker_security_projection=docker_projection,
        admin_database_url_sha256=hashlib.sha256(ADMIN_URL.encode()).hexdigest(),
        prepared_at=prepared_at,
    )
    requested_at = prepared_at + timedelta(seconds=1)
    registration = AtomicScientificExecutionRegistrationReceipt(
        authorization_sha256=_sha("campaign-authorization"),
        quest_id="qst_" + "1" * 32,
        scientific_slot_id="sos_" + "2" * 32,
        action_sha256=_sha("campaign-action"),
        execution_id="exe_" + "3" * 32,
        attempt_id="iat_" + "4" * 32,
        qualification_bundle_sha256=_sha("campaign-bundle"),
        qualification_grant_sha256=_sha("campaign-grant"),
        registered_at=requested_at - timedelta(minutes=2),
        qualification_admission_sha256=_sha("campaign-admission"),
        resource_reservation_sha256=_sha("campaign-reservation"),
        reserved_at=requested_at - timedelta(minutes=1),
    )
    request = campaign.QualificationTargetCampaignRequestV1(
        observer_config=observer_config,
        execution=campaign.QualificationCampaignExecutionExpectationV1(
            registration_receipt=registration
        ),
        campaign_journal_root=f"{commissioning_request.installation_request.journal_root}/campaign",
        requested_at=requested_at,
    )
    spec = commissioning_request.installation_request.deployment_spec
    frozen_at = requested_at + timedelta(seconds=1)
    observation = _observation(spec, observed_at=frozen_at)
    signed = _sign_observation(observation, pin, spec=spec, private_key=OBSERVER_PRIVATE_KEY)
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    monkeypatch.setattr(deployment, "_monitored_utc_now", lambda: frozen_at)
    manifest = deployment.freeze_installed_manifest(spec, _StaticObserver(signed), pin)
    preflight = deployment.QualificationDeploymentPreflight(
        deployment_id=spec.deployment_id,
        spec_sha256=spec.spec_sha256,
        installed_manifest_sha256=manifest.manifest_sha256,
        observed_at=frozen_at + timedelta(seconds=30),
        verified_at=frozen_at + timedelta(seconds=31),
        observer_provenance_verified=True,
        observation_freshness_verified=True,
        linux_systemd_cgroup_verified=True,
        shared_mount_namespace_verified=True,
        installed_files_verified=True,
        custody_roots_verified=True,
        systemd_units_verified=True,
        postgresql_acl_verified=True,
        postgresql_schema_verified=True,
        postgresql_roles_verified=True,
        postgresql_acl_closure_verified=True,
        postgresql_clock_verified=True,
        image_layout_verified=True,
        output_quota_service_verified=True,
        deadline_watchdog_service_verified=True,
        code_identity_verified=True,
        blockers=(),
        ready_for_opt_in_campaign=True,
    )
    return request, manifest, preflight


class _StaticObserver:
    def __init__(self, observation) -> None:
        self.observation = observation

    def observe(self, *, spec, rendered_units, postgresql_acl):
        del spec, rendered_units, postgresql_acl
        return self.observation


class _FakeCampaignHost:
    def __init__(
        self,
        request: campaign.QualificationTargetCampaignRequestV1,
        manifest: deployment.QualificationInstalledDeploymentManifestV1,
        preflight: deployment.QualificationDeploymentPreflight,
    ) -> None:
        self.request = request
        self.manifest = manifest
        self.preflight = preflight
        self.records: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.revalidations = 0
        self._build_evidence()

    def _build_evidence(self) -> None:
        spec = self.manifest.spec
        base = self.manifest.frozen_at + timedelta(seconds=1)
        unit_names = tuple(
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
        running = tuple(name for name in unit_names if name != spec.workspace_unit_name)
        self.activation = campaign.QualificationSystemdActivationReceiptV1(
            unit_names=unit_names,
            main_pids=tuple((name, 1000 + index) for index, name in enumerate(running)),
            activated_at=self.manifest.frozen_at - timedelta(seconds=1),
        )
        self.peers = {
            role: campaign.QualificationPostgreSQLPeerProbeReceiptV1(
                role_kind=role,
                process_uid=spec.node_uid if role == "node" else spec.outbox_uid,
                process_gid=spec.node_gid if role == "node" else spec.outbox_gid,
                postgresql_role=(
                    spec.postgresql_allocator_role
                    if role == "node"
                    else spec.postgresql_outbox_role
                ),
                database_name=spec.postgresql_database,
                schema_revision=spec.expected_schema_revision,
                database_clock=base + timedelta(seconds=12 if role == "node" else 13),
                probed_at=base + timedelta(seconds=12 if role == "node" else 13, milliseconds=100),
            )
            for role in ("node", "outbox")
        }
        quota = OutputQuotaProvisioningReceipt(
            node_manifest_sha256=spec.node_manifest_sha256,
            node_id=spec.node_id,
            boot_id="boot.campaign",
            execution_id="exe_" + "5" * 32,
            infrastructure_attempt_id="iat_" + "6" * 32,
            intent_sha256=_sha("quota-intent"),
            output_root=f"{spec.output_workspace_root}/campaign/output",
            output_quota_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
            output_root_device=7,
            output_root_inode=701,
            output_root_owner_uid=spec.node_uid,
            output_root_owner_gid=spec.node_gid,
            mount_id=70,
            mount_parent_id=7,
            block_device_major=7,
            block_device_minor=7,
            block_device_capacity_bytes=MINIMUM_LOOP_OUTPUT_FILESYSTEM_BYTES,
            filesystem_type="ext4",
            filesystem_uuid_sha256=_sha("quota-filesystem"),
            mount_options=("nodev", "noexec", "nosuid", "rw"),
            backing_file_identity_sha256=_sha("quota-backing"),
            provisioner_policy_sha256=_sha("quota-policy"),
            provisioner_principal_id="principal:quota-provisioner",
            provisioned_at=base + timedelta(seconds=14),
        )
        root_kills = tuple(
            sorted(
                (
                    self._kill(spec.quota_unit_name, 1100, base + timedelta(seconds=15)),
                    self._kill(spec.watchdog_unit_name, 1200, base + timedelta(seconds=17)),
                ),
                key=lambda item: item.unit_name,
            )
        )
        self.root = campaign.QualificationRootServiceCampaignEvidenceV1(
            quota_campaign=campaign.QualificationQuotaCampaignReceiptV1(
                quota_health_before_sha256=_sha("quota-health-before"),
                watchdog_health_before_sha256=_sha("watchdog-health-before"),
                provisioning_receipt=quota,
                loop_device="/dev/loop7",
                replayed_provisioning_receipt_sha256=quota.provisioning_receipt_sha256,
                quota_health_after_sha256=_sha("quota-health-after"),
                watchdog_health_after_sha256=_sha("watchdog-health-after"),
                completed_at=base + timedelta(seconds=19),
            ),
            service_kills=root_kills,
        )
        self.postgresql = campaign.QualificationPostgreSQLKillReceiptV1(
            postgresql_role=spec.postgresql_allocator_role,
            backend_pid=2200,
            advisory_lock_key=12345,
            transaction_started_at=base + timedelta(seconds=20),
            terminated_at=base + timedelta(seconds=21),
            reconnected_at=base + timedelta(seconds=22),
        )
        registration = self.request.execution.registration_receipt
        quiescence = campaign.QualificationOutboxQuiescenceReceiptV1(
            unit_name=spec.outbox_unit_name,
            prior_pid=2300,
            baseline_spool_filenames=(),
            stopped_at=base + timedelta(seconds=1),
        )
        node_kill = self._kill(spec.node_unit_name, 2400, base + timedelta(seconds=2))
        payload = AcceptedQualificationTerminalSubmission(
            attempt_id=registration.attempt_id,
            node_manifest_sha256=spec.node_manifest_sha256,
            terminal_submission_sha256=_sha("terminal-submission"),
            accepted_runtime_termination_sha256=_sha("runtime-termination"),
            artifact_manifest_sha256=_sha("artifact-manifest"),
            output_tree_sha256=_sha("output-tree"),
            artifact_verified_receipt_sha256s=(_sha("artifact-receipt"),),
            disposition="process_succeeded",
            node_submitted_at=base + timedelta(seconds=5),
            artifact_submission_deadline=base + timedelta(minutes=5),
            accepted_at=base + timedelta(seconds=6),
            runtime_control_policy_sha256=_sha("runtime-control-policy"),
            accepted_by_principal_id="principal:runtime-control",
            acceptance_key_id=_sha("runtime-control-key"),
            signature_ed25519_hex="a" * 128,
        )
        authority = payload.accepted_terminal_submission_sha256
        envelope = QualificationTerminalSpoolEnvelopeV1(
            source_kind="qualification_terminal_v2",
            outbox_id=f"qto_{authority}",
            terminal_authority_kind="accepted_terminal_submission",
            terminal_authority_sha256=authority,
            execution_id=registration.execution_id,
            attempt_id=registration.attempt_id,
            topic="execution.qualification_terminal.v2",
            delivery_key=(f"execution-v2:{registration.execution_id}:{registration.attempt_id}"),
            payload_sha256=authority,
            payload=payload,
            source_created_at=base + timedelta(seconds=7),
        )
        spool_path = f"{spec.outbox_spool_root}/{envelope.filename}"
        spool = campaign.QualificationSpoolFileObservationV1(
            path=spool_path,
            sha256=hashlib.sha256(canonical_json_bytes(envelope)).hexdigest(),
            byte_length=len(canonical_json_bytes(envelope)),
            device=8,
            inode=801,
            owner_uid=spec.outbox_uid,
            owner_gid=spec.outbox_gid,
        )
        outbox_kill = self._kill(spec.outbox_unit_name, 2500, base + timedelta(seconds=8))
        self.terminal = campaign.QualificationTerminalCampaignReceiptV1(
            execution_id=registration.execution_id,
            attempt_id=registration.attempt_id,
            outbox_quiescence=quiescence,
            node_kill=node_kill,
            outbox_kill=outbox_kill,
            terminal_envelope=envelope,
            spool_file=spool,
            completed_at=base + timedelta(seconds=11),
        )
        final_observation = _observation(spec, observed_at=self.preflight.observed_at)
        self.reobservation = campaign.QualificationCampaignReobservationEvidenceV1(
            signed_observation=_sign_observation(
                final_observation,
                self.request.observer_config.observer_pin,
                spec=spec,
                private_key=OBSERVER_PRIVATE_KEY,
            ),
            preflight=self.preflight,
        )

    @staticmethod
    def _kill(unit: str, pid: int, at: datetime) -> campaign.QualificationServiceKillReceiptV1:
        return campaign.QualificationServiceKillReceiptV1(
            unit_name=unit,
            pid_before=pid,
            pid_after=pid + 1,
            killed_at=at,
            recovered_at=at + timedelta(seconds=1),
        )

    def assert_target(self) -> None:
        self.calls.append("assert")

    @contextmanager
    def lock(self):
        self.calls.append("lock")
        yield

    def read_record(self, name: str) -> bytes | None:
        return self.records.get(name)

    def write_record_once(self, name: str, payload: bytes) -> None:
        existing = self.records.get(name)
        if existing is not None and existing != payload:
            raise campaign.QualificationCampaignError("journal variant")
        self.records[name] = payload

    def activate_services(self):
        self.calls.append("activation")
        return self.activation

    def freeze_installed_manifest(self):
        self.calls.append("manifest")
        return self.manifest

    def probe_postgresql_peer(self, role_kind):
        self.calls.append(f"peer:{role_kind}")
        return self.peers[role_kind]

    def run_root_service_campaign(self):
        self.calls.append("root")
        return self.root

    def run_postgresql_kill_campaign(self):
        self.calls.append("postgresql")
        return self.postgresql

    def run_terminal_campaign(self):
        self.calls.append("terminal")
        return self.terminal

    def reobserve(self, manifest):
        assert manifest == self.manifest
        self.calls.append("reobserve")
        return self.reobservation

    def revalidate_completed(self, receipt):
        assert receipt.request_id == self.request.request_id
        self.revalidations += 1


def test_plan_is_non_mutating_and_campaign_verdict_is_derived(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    plan = campaign.build_qualification_target_campaign_plan(request)
    assert len(plan.scenarios) == 10
    assert plan.campaign_executed is False
    assert plan.deployment_qualified is False
    assert plan.scientific_admission_allowed is False

    host = _FakeCampaignHost(request, manifest, preflight)
    receipt = campaign.run_qualification_target_campaign(
        request,
        host,
        clock=lambda: preflight.verified_at + timedelta(seconds=1),
    )
    assert receipt.campaign_executed is True
    assert receipt.deployment_qualified is True
    assert receipt.qualification_only is True
    assert receipt.scientific_admission_allowed is False
    assert receipt.terminal_campaign.terminal_row_exactly_once is True
    assert receipt.terminal_campaign.durable_spool_replayed_after_outbox_kill is True
    assert host.calls[2:5] == ["activation", "manifest", "terminal"]
    assert host.revalidations == 1

    replay = campaign.run_qualification_target_campaign(request, host)
    assert replay == receipt
    assert host.revalidations == 2
    assert host.calls.count("terminal") == 1


@pytest.mark.parametrize(
    "phase",
    (
        "after_operation:03-terminal-campaign.json",
        "after_record:06-root-services.json",
        "after_operation:08-final-reobservation.json",
    ),
)
def test_campaign_resumes_from_canonical_phase_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    host = _FakeCampaignHost(request, manifest, preflight)
    fired = False

    def fault(observed: str) -> None:
        nonlocal fired
        if not fired and observed == phase:
            fired = True
            raise RuntimeError("injected campaign crash")

    with pytest.raises(RuntimeError, match="injected campaign crash"):
        campaign.run_qualification_target_campaign(
            request,
            host,
            clock=lambda: preflight.verified_at + timedelta(seconds=1),
            fault=fault,
        )
    receipt = campaign.run_qualification_target_campaign(
        request,
        host,
        clock=lambda: preflight.verified_at + timedelta(seconds=1),
    )
    assert receipt.deployment_qualified is True
    assert receipt.scientific_admission_allowed is False


def test_campaign_contracts_reject_science_authority_and_variant_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    with pytest.raises(ValidationError, match="scientific_admission_allowed"):
        campaign.QualificationTargetCampaignRequestV1.model_validate(
            {**request.model_dump(mode="python"), "scientific_admission_allowed": True}
        )
    host = _FakeCampaignHost(request, manifest, preflight)
    host.records["01-activation.json"] = b"{}"
    with pytest.raises(campaign.QualificationCampaignError, match="invalid"):
        campaign.run_qualification_target_campaign(request, host)


def test_observer_config_keeps_external_docker_review_pin_separate_from_live_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _manifest, _preflight = _request_and_evidence(monkeypatch, tmp_path)
    spec = request.observer_config.commissioning_request.installation_request.deployment_spec
    assert spec.docker_security_projection_sha256 != (
        request.observer_config.docker_security_projection.projection_sha256
    )
    assert request.observer_config.scientific_admission_allowed is False


def test_concrete_observer_signs_only_the_frozen_call_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, manifest, _preflight = _request_and_evidence(monkeypatch, tmp_path)
    config = request.observer_config
    observer = LinuxQualificationDeploymentObserver(config)
    live = manifest.installed_observation.observation.model_copy(
        update={
            "observation_started_at": manifest.frozen_at + timedelta(seconds=1),
            "observed_at": manifest.frozen_at + timedelta(seconds=2),
        }
    )
    monkeypatch.setattr(observer, "_observe_live", lambda: live)
    monkeypatch.setattr(observer, "_load_private_key", lambda: OBSERVER_PRIVATE_KEY)
    spec = config.commissioning_request.installation_request.deployment_spec
    signed = observer.observe(
        spec=spec,
        rendered_units=deployment.render_systemd_units(spec),
        postgresql_acl=deployment.render_postgresql_acl(spec),
    )
    assert signed.observer_key_id == config.observer_pin.key_id
    assert signed.observation.docker_security_projection_sha256 == (
        spec.docker_security_projection_sha256
    )
    monkeypatch.setattr(
        deployment,
        "_monitored_utc_now",
        lambda: signed.signed_at + timedelta(milliseconds=1),
    )
    frozen = deployment.freeze_installed_manifest(
        spec,
        _StaticObserver(signed),
        config.observer_pin,
    )
    assert frozen.installed_observation == signed

    with pytest.raises(QualificationObserverError, match="frozen config"):
        observer.observe(
            spec=spec,
            rendered_units=(),
            postgresql_acl=deployment.render_postgresql_acl(spec),
        )


def test_scenario_evidence_order_cannot_be_rebound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    receipt = campaign.run_qualification_target_campaign(
        request,
        _FakeCampaignHost(request, manifest, preflight),
        clock=lambda: preflight.verified_at + timedelta(seconds=1),
    )
    values = list(receipt.scenario_evidence_sha256s)
    values[2], values[3] = values[3], values[2]
    with pytest.raises(ValidationError, match="embedded evidence"):
        campaign.QualificationTargetCampaignReceiptV1.model_validate(
            {
                **receipt.model_dump(mode="python", exclude={"receipt_id"}),
                "scenario_evidence_sha256s": tuple(values),
            }
        )


def test_offline_receipt_verifier_recomputes_retained_signed_reobservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, manifest, preflight = _request_and_evidence(monkeypatch, tmp_path)
    receipt = campaign.run_qualification_target_campaign(
        request,
        _FakeCampaignHost(request, manifest, preflight),
        clock=lambda: preflight.verified_at + timedelta(seconds=1),
    )
    assert campaign.verify_qualification_target_campaign_receipt(request, receipt).plan_id == (
        receipt.plan_id
    )

    rebound_signed = receipt.final_reobservation.signed_observation.model_copy(
        update={"signature_ed25519_hex": "b" * 128}
    )
    rebound_final = campaign.QualificationCampaignReobservationEvidenceV1(
        signed_observation=rebound_signed,
        preflight=receipt.final_reobservation.preflight,
    )
    evidence = (*receipt.scenario_evidence_sha256s[:-1], rebound_final.evidence_sha256)
    rebound_receipt = campaign.QualificationTargetCampaignReceiptV1(
        **receipt.model_dump(
            mode="python",
            exclude={"receipt_id", "final_reobservation", "scenario_evidence_sha256s"},
        ),
        final_reobservation=rebound_final,
        scenario_evidence_sha256s=evidence,
    )
    with pytest.raises(campaign.QualificationCampaignError, match="not derived"):
        campaign.verify_qualification_target_campaign_receipt(request, rebound_receipt)


def test_request_and_plan_hashes_are_canonical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _manifest, _preflight = _request_and_evidence(monkeypatch, tmp_path)
    plan = campaign.build_qualification_target_campaign_plan(request)
    assert request.request_sha256 == canonical_sha256(request)
    assert plan.plan_sha256 == canonical_sha256(plan)
    assert canonical_json_bytes(request) == canonical_json_bytes(
        campaign.QualificationTargetCampaignRequestV1.model_validate_json(
            canonical_json_bytes(request)
        )
    )


def test_campaign_request_loader_requires_digest_custody_and_unique_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _manifest, _preflight = _request_and_evidence(monkeypatch, tmp_path)
    payload = canonical_json_bytes(request)
    observed = SimpleNamespace(st_uid=0, st_gid=0, st_mode=0o100400)
    monkeypatch.setattr(
        campaign.LinuxQualificationTargetCampaignHost,
        "_read_file",
        staticmethod(lambda path: (payload, observed)),
    )
    loaded = campaign.load_qualification_target_campaign_request(
        "/root/campaign.json",
        expected_file_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert loaded == request
    with pytest.raises(campaign.QualificationCampaignError, match="canonical and absolute"):
        campaign.load_qualification_target_campaign_request(
            "campaign.json",
            expected_file_sha256=hashlib.sha256(payload).hexdigest(),
        )
    with pytest.raises(campaign.QualificationCampaignError, match="digest or custody"):
        campaign.load_qualification_target_campaign_request(
            "/root/campaign.json",
            expected_file_sha256=_sha("wrong-request"),
        )

    duplicate = b'{"schema_name":"rebound",' + payload[1:]
    monkeypatch.setattr(
        campaign.LinuxQualificationTargetCampaignHost,
        "_read_file",
        staticmethod(lambda path: (duplicate, observed)),
    )
    with pytest.raises(campaign.QualificationCampaignError, match="invalid"):
        campaign.load_qualification_target_campaign_request(
            "/root/campaign.json",
            expected_file_sha256=hashlib.sha256(duplicate).hexdigest(),
        )


def test_concrete_journal_recovers_final_linked_before_pending_unlink(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    final = root / "01-test.json"
    pending = root / ".01-test.json.pending"
    pending.write_bytes(b"{}")
    pending.chmod(0o400)
    final.hardlink_to(pending)
    assert final.stat().st_nlink == pending.stat().st_nlink == 2

    host = object.__new__(campaign.LinuxQualificationTargetCampaignHost)
    object.__setattr__(host, "_root", root)
    object.__setattr__(host, "_lock_descriptor", 1)
    original = campaign.LinuxQualificationTargetCampaignHost._read_file

    def root_owned(path, **keywords):
        payload, observed = original(path, **keywords)
        return payload, SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_uid=0,
            st_gid=0,
            st_nlink=observed.st_nlink,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns,
        )

    object.__setattr__(host, "_read_file", root_owned)
    assert host.read_record("01-test.json") == b"{}"
    assert not pending.exists()
    assert final.stat().st_nlink == 1


def test_concrete_journal_rejects_unlinked_pending_next_to_final(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    final = root / "01-test.json"
    pending = root / ".01-test.json.pending"
    final.write_bytes(b"{}")
    pending.write_bytes(b"{}")
    final.chmod(0o400)
    pending.chmod(0o400)

    host = object.__new__(campaign.LinuxQualificationTargetCampaignHost)
    object.__setattr__(host, "_root", root)
    object.__setattr__(host, "_lock_descriptor", 1)
    original = campaign.LinuxQualificationTargetCampaignHost._read_file

    def root_owned(path, **keywords):
        payload, observed = original(path, **keywords)
        return payload, SimpleNamespace(
            st_dev=observed.st_dev,
            st_ino=observed.st_ino,
            st_mode=observed.st_mode,
            st_uid=0,
            st_gid=0,
            st_nlink=observed.st_nlink,
            st_size=observed.st_size,
            st_mtime_ns=observed.st_mtime_ns,
        )

    object.__setattr__(host, "_read_file", root_owned)
    with pytest.raises(campaign.QualificationCampaignError, match="impossible pending"):
        host.read_record("01-test.json")


def test_live_capability_bitmap_decoder_is_exact_and_rejects_unknown_bits() -> None:
    assert linux_capability_names_from_hex("000000000020002b") == (
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_KILL",
        "CAP_SYS_ADMIN",
    )
    with pytest.raises(QualificationObserverError, match="unknown bits"):
        linux_capability_names_from_hex(hex(1 << 41)[2:])
