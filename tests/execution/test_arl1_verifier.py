from __future__ import annotations

import hashlib
import os
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import aletheia.arl1_verifier as verifier_module
from aletheia.arl1 import (
    ARL0GateKind,
    ARL0IntegrityEvidenceV1,
    ARL1ProtocolCampaignEvidenceV1,
    ARL1QualificationTrustAnchorV1,
    build_arl1_protocol_executor_report,
    verify_arl1_evidence_bundle,
)
from aletheia.arl1_verifier import (
    ARL0GateCommandPinV1,
    ARL0GateCommandResultV1,
    ARL0GateReplayProjectionV1,
    ARL0PinnedInputV1,
    ARL1EvidenceBundleSourceV1,
    ARL1SourceVerificationError,
    LocalARL1EvidenceArchive,
    PostgreSQLARL1EvidenceVerifier,
    SubprocessARL0GateReplayPort,
    build_arl0_evidence_archive_manifest,
    build_protocol_campaign_archive_manifest,
    prepare_arl1_evidence_bundle,
    retain_arl0_evidence_archive,
    retain_bundle_evidence_archive,
    retain_protocol_campaign_archive,
)
from aletheia.observations.adapters import CommittedValidationSourceVerificationContext
from aletheia.observations.store import (
    ObservationAdmissionWrite,
    ScientificExecutionAuthorizationWrite,
)
from aletheia.research_kernel.reducer import ActionLifecycle
from aletheia.research_kernel.schemas import canonical_json_bytes

from .test_arl1_qualification import VERIFIER_PRIVATE_KEY, arl1_case as arl1_case


class _GateReplayer:
    def replay_gate(
        self,
        *,
        gate,
        evidence_artifact,
        verification_receipt,
        observed_at,
    ):
        assert evidence_artifact
        assert verification_receipt
        return ARL0GateReplayProjectionV1(
            gate_kind=gate.gate_kind,
            gate_evidence_sha256=gate.evidence_sha256,
            evaluated_scope_sha256=gate.evaluated_scope_sha256,
            evidence_artifact_sha256=gate.evidence_artifact_sha256,
            verification_receipt_sha256=gate.verification_receipt_sha256,
            replayed_by_principal_id=gate.verified_by_principal_id,
            replayed_at=observed_at,
        )


class _ActionAuthority:
    def verify_action_protocol_binding(self, *, binding, observed_at):
        assert binding.bound_at <= observed_at
        return binding.binding_sha256


class _RawCustody:
    def __init__(self, campaign: ARL1ProtocolCampaignEvidenceV1) -> None:
        self._by_run = {
            item.raw_run.raw_run_sha256: item.raw_run_custody
            for item in campaign.replicate_executions
        }

    def verify_raw_run_custody(self, *, raw_run, observed_at):
        return self._by_run[raw_run.raw_run_sha256].model_copy(update={"verified_at": observed_at})


class _ValidationSource:
    def __init__(self, campaign: ARL1ProtocolCampaignEvidenceV1) -> None:
        self._by_slot = {
            item.scientific_slot_id: item.committed_validation
            for item in campaign.replicate_executions
        }

    def load_committed_validation(self, *, quest_id, action_sha256, scientific_slot_id):
        del quest_id, action_sha256
        return self._by_slot[scientific_slot_id]


class _KernelStore:
    def __init__(self, campaign: ARL1ProtocolCampaignEvidenceV1) -> None:
        self._campaign = campaign

    def audit(self, quest_id, *, expected_scope_binding=None):
        campaign = self._campaign
        primary = next(
            item
            for item in campaign.replicate_executions
            if item.scientific_slot_id == campaign.scientific_slot_id
        )
        binding = primary.authorization.message.action_protocol_binding
        assert quest_id == binding.action.quest_id
        assert (
            expected_scope_binding == binding.compilation_request.protocol.graph_scope.scope_binding
        )
        action = SimpleNamespace(
            action_ref=binding.action.object_ref,
            lifecycle=ActionLifecycle.APPLIED,
            decided_event_sha256=campaign.incorporation_event.event_sha256,
            observation_evidence_ref=campaign.incorporation_event.payload.evidence_ref,
        )
        return SimpleNamespace(
            events=(campaign.incorporation_event,),
            state=SimpleNamespace(actions=(action,)),
        )


@contextmanager
def _session_scope():
    yield object()


def _arl0_raw_objects(integrity: ARL0IntegrityEvidenceV1) -> dict[str, bytes]:
    values = {
        integrity.source_tree_sha256: b"source-tree",
        integrity.environment_lock_sha256: b"environment-lock",
        integrity.database_schema_verification_receipt_sha256: b"schema-verification",
    }
    for gate in integrity.gates:
        values[gate.evidence_artifact_sha256] = f"evidence:{gate.gate_kind.value}".encode()
        values[gate.verification_receipt_sha256] = f"verification:{gate.gate_kind.value}".encode()
    assert all(
        verifier_module.hashlib.sha256(payload).hexdigest() == digest
        for digest, payload in values.items()
    )
    return values


def _bind_real_arl0_manifest(
    archive: LocalARL1EvidenceArchive,
    integrity: ARL0IntegrityEvidenceV1,
):
    raw = _arl0_raw_objects(integrity)
    manifest = build_arl0_evidence_archive_manifest(
        integrity,
        raw_objects=raw,
        retained_at=integrity.completed_at,
    )
    rebound = ARL0IntegrityEvidenceV1.model_validate(
        {
            **integrity.model_dump(mode="python"),
            "evidence_archive_manifest_sha256": manifest.manifest_sha256,
        }
    )
    retained = retain_arl0_evidence_archive(
        archive,
        rebound,
        raw_objects=raw,
        retained_at=rebound.completed_at,
    )
    assert retained == manifest
    return rebound


def _bind_real_campaign_manifest(
    archive: LocalARL1EvidenceArchive,
    campaign: ARL1ProtocolCampaignEvidenceV1,
):
    manifest = build_protocol_campaign_archive_manifest(
        campaign,
        retained_at=campaign.report.reported_at,
    )
    report = build_arl1_protocol_executor_report(
        quest_id=campaign.report.quest_id,
        question_ref=campaign.report.question_ref,
        protocol_sha256=campaign.protocol_sha256
        if hasattr(campaign, "protocol_sha256")
        else campaign.compilation_request.protocol.protocol_sha256,
        compilation_receipt_sha256=campaign.report.compilation_receipt_sha256,
        work_order_sha256=campaign.report.work_order_sha256,
        work_order_node_id=campaign.work_order_node_id,
        exact_reexecution_evidence_sha256s=campaign.exact_reexecution_evidence_sha256s,
        all_attempts_manifest_sha256=campaign.all_attempts_manifest_sha256,
        committed_validation_receipt_sha256=campaign.committed_validation_receipt_sha256,
        committed_admission_sha256=campaign.committed_admission_sha256,
        incorporation_event_sha256=campaign.incorporation_event_sha256,
        outcome=campaign.outcome,
        reproduction_receipt_sha256=campaign.reproduction_receipt_sha256,
        source_evidence_archive_manifest_sha256=manifest.manifest_sha256,
        reported_at=campaign.report.reported_at,
    )
    rebound = ARL1ProtocolCampaignEvidenceV1.model_validate(
        {
            **campaign.model_dump(mode="python"),
            "campaign_id": None,
            "source_evidence_archive_manifest_sha256": manifest.manifest_sha256,
            "report": report,
        }
    )
    retained = retain_protocol_campaign_archive(
        archive,
        rebound,
        retained_at=rebound.report.reported_at,
    )
    assert retained == manifest
    return rebound


def _production_verifier(
    *,
    monkeypatch: pytest.MonkeyPatch,
    archive: LocalARL1EvidenceArchive,
    campaign: ARL1ProtocolCampaignEvidenceV1,
    pin,
    signing_key: bytes,
    observed_at,
):
    sea_rows = {
        item.scientific_slot_id: ScientificExecutionAuthorizationWrite.from_contract(
            item.authorization,
            registered_at=item.registration_receipt.registered_at,
        )
        for item in campaign.replicate_executions
    }
    admission_row = ObservationAdmissionWrite.from_contract(
        campaign.committed_admission,
        quest_id=campaign.report.quest_id,
        incorporated_event_sequence=campaign.incorporation_event.sequence,
        incorporated_event_sha256=campaign.incorporation_event.event_sha256,
        incorporated_event_type=campaign.incorporation_event.event_type.value,
    )
    monkeypatch.setattr(
        verifier_module,
        "get_scientific_execution_authorization_by_slot",
        lambda _session, *, quest_id, scientific_slot_id: sea_rows[scientific_slot_id],
    )
    monkeypatch.setattr(
        verifier_module,
        "get_observation_admission_by_slot",
        lambda _session, *, quest_id, scientific_slot_id: admission_row,
    )
    monkeypatch.setattr(
        verifier_module,
        "verify_committed_observation_admission",
        lambda **kwargs: kwargs["committed_admission"],
    )
    inert = object()
    observation_context = CommittedValidationSourceVerificationContext(
        qualification_authority=inert,
        action_authority=inert,
        qualification_custody=inert,
        raw_run_custody=inert,
        validation_campaign_custody=inert,
        execution_authority_pin=inert,
        validator_authority_pin=inert,
        admission_authority_pin=inert,
        database_authority_pin=inert,
    )
    return PostgreSQLARL1EvidenceVerifier(
        archive=archive,
        gate_replayer=_GateReplayer(),
        sessions=_session_scope,
        action_authority=_ActionAuthority(),
        raw_run_custody=_RawCustody(campaign),
        committed_validation_source=_ValidationSource(campaign),
        observation_verification=observation_context,
        kernel_store=_KernelStore(campaign),
        trusted_verifier_pins=(pin,),
        signing_private_key=signing_key,
        signing_pin_sha256=pin.pin_sha256,
        clock=lambda: observed_at,
    )


def test_production_verifier_freshly_replays_archive_database_and_kernel(
    arl1_case,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    original, _qualification_key, _recording_verifier = arl1_case
    archive = LocalARL1EvidenceArchive(tmp_path / "arl1-archive")
    integrity = _bind_real_arl0_manifest(archive, original.arl0_integrity)
    campaign = _bind_real_campaign_manifest(archive, original.protocol_campaigns[0])
    top = retain_bundle_evidence_archive(
        archive,
        policy=original.policy,
        arl0_integrity=integrity,
        target_campaign_request=original.target_campaign_request,
        target_campaign_receipt=original.target_campaign_receipt,
        protocol_campaigns=(campaign,),
        retained_at=max(integrity.completed_at, campaign.report.reported_at),
    )
    source = ARL1EvidenceBundleSourceV1(
        policy=original.policy,
        arl0_integrity=integrity,
        target_campaign_request=original.target_campaign_request,
        target_campaign_receipt=original.target_campaign_receipt,
        protocol_campaigns=(campaign,),
        evidence_archive_manifest_sha256=top.manifest_sha256,
    )
    observed_at = max(integrity.completed_at, campaign.report.reported_at) + timedelta(seconds=1)
    verifier = _production_verifier(
        monkeypatch=monkeypatch,
        archive=archive,
        campaign=campaign,
        pin=original.policy.evidence_verifier_pins[0],
        signing_key=VERIFIER_PRIVATE_KEY,
        observed_at=observed_at,
    )

    bundle = prepare_arl1_evidence_bundle(source, source_verifier=verifier)
    verified = verify_arl1_evidence_bundle(
        bundle,
        source_verifier=verifier,
        trust_anchor=ARL1QualificationTrustAnchorV1.from_policy(bundle.policy),
    )

    assert verified == bundle
    assert len(bundle.source_verification_receipts) == 3

    campaign_entry = next(
        item for item in top.entries if item.object_kind == "bundle:protocol_campaign:campaign-001"
    )
    object_path = (
        archive.root
        / "objects"
        / "sha256"
        / campaign_entry.object_sha256[:2]
        / campaign_entry.object_sha256
    )
    os.chmod(object_path, 0o600)
    object_path.write_bytes(b"x" * campaign_entry.byte_length)
    os.chmod(object_path, 0o400)
    with pytest.raises(ARL1SourceVerificationError, match="fresh rehash"):
        verify_arl1_evidence_bundle(
            bundle,
            source_verifier=verifier,
            trust_anchor=ARL1QualificationTrustAnchorV1.from_policy(bundle.policy),
        )


def test_archive_rejects_hardlinked_content(tmp_path) -> None:
    archive = LocalARL1EvidenceArchive(tmp_path / "archive")
    entry = archive.publish_bytes(
        object_kind="test:source",
        payload=b"retained-source",
        canonical_json=False,
    )
    object_path = (
        archive.root / "objects" / "sha256" / entry.object_sha256[:2] / entry.object_sha256
    )
    os.link(object_path, tmp_path / "second-link")
    with pytest.raises(ARL1SourceVerificationError, match="custody differs"):
        archive.load_entry(entry)


def test_archive_supports_group_read_only_independent_verifier_custody(tmp_path) -> None:
    process_uid = os.geteuid()
    process_gid = os.getegid()
    archive_root = tmp_path / "shared-archive"
    writer = LocalARL1EvidenceArchive(
        archive_root,
        expected_owner_uid=process_uid,
        expected_owner_gid=process_gid,
        object_mode=0o440,
        directory_mode=0o750,
    )
    entry = writer.publish_bytes(
        object_kind="test:shared-source",
        payload=b"group-readable-retained-source",
        canonical_json=False,
    )
    reader = LocalARL1EvidenceArchive(
        archive_root,
        read_only=True,
        expected_owner_uid=process_uid,
        expected_owner_gid=process_gid,
        object_mode=0o440,
        directory_mode=0o750,
    )

    assert reader.load_entry(entry).payload == b"group-readable-retained-source"
    assert stat.S_IMODE(archive_root.stat().st_mode) == 0o750
    with pytest.raises(ValueError, match="traversable"):
        LocalARL1EvidenceArchive(
            tmp_path / "untraversable",
            object_mode=0o440,
        )

    prefix = archive_root / "objects" / "sha256" / entry.object_sha256[:2]
    prefix.chmod(0o700)
    with pytest.raises(ARL1SourceVerificationError, match="directory custody"):
        reader.load_entry(entry)


def test_subprocess_arl0_replayer_runs_exact_pinned_command(tmp_path) -> None:
    scope_sha256 = hashlib.sha256(b"arl0-ledger-scope").hexdigest()
    result = ARL0GateCommandResultV1(
        gate_kind=ARL0GateKind.LEDGER_REPLAY,
        evaluated_scope_sha256=scope_sha256,
        checks=("canonical_snapshot_replay", "event_chain_replay"),
    )
    result_bytes = canonical_json_bytes(result)
    executable = Path(sys.executable).resolve(strict=True)
    pinned_input = tmp_path / "gate-input.json"
    pinned_input.write_bytes(b"frozen-input")
    pinned_input.chmod(0o400)
    pin = ARL0GateCommandPinV1(
        gate_kind=ARL0GateKind.LEDGER_REPLAY,
        evaluated_scope_sha256=scope_sha256,
        executable_path=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        arguments=(
            "-c",
            f"import sys;sys.stdout.buffer.write(bytes.fromhex('{result_bytes.hex()}'))",
        ),
        working_directory=str(tmp_path),
        pinned_inputs=(
            ARL0PinnedInputV1(
                absolute_path=str(pinned_input),
                content_sha256=hashlib.sha256(b"frozen-input").hexdigest(),
            ),
        ),
        environment=(),
        replay_principal_id="arl0-independent-replayer",
        timeout_seconds=10,
    )
    completed_at = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)
    replayer = SubprocessARL0GateReplayPort((pin,), clock=lambda: completed_at)

    gate, artifact, receipt = replayer.capture_gate(ARL0GateKind.LEDGER_REPLAY)
    projection = replayer.replay_gate(
        gate=gate,
        evidence_artifact=artifact,
        verification_receipt=receipt,
        observed_at=completed_at + timedelta(seconds=1),
    )

    assert projection.gate_evidence_sha256 == gate.evidence_sha256
    pinned_input.chmod(0o600)
    pinned_input.write_bytes(b"rebound-input")
    pinned_input.chmod(0o400)
    with pytest.raises(ARL1SourceVerificationError, match="fresh rehash"):
        replayer.replay_gate(
            gate=gate,
            evidence_artifact=artifact,
            verification_receipt=receipt,
            observed_at=completed_at + timedelta(seconds=1),
        )
