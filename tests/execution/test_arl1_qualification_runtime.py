from __future__ import annotations

import hashlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from aletheia import arl1_qualification_runtime as qualification_runtime
from aletheia.arl1 import (
    ARL0GateKind,
    ARL1EvidenceVerifierPinV1,
    ARL1QualificationError,
    ARL1QualificationTrustAnchorV1,
)
from aletheia.arl1_qualification_runtime import (
    ARL1EvidenceVerifierRuntimeConfigV1,
    ARL1F9V2ArchiveReadConfigV1,
    ARL1PrivateSigningKeyPinV1,
    ARL1QualificationIssuanceDeploymentV1,
    ARL1QualificationRuntimeError,
    ARL1QualificationVerificationDeploymentV1,
    ARL1SourceVerificationDeploymentV1,
    issue_arl1_qualification_deployment,
    prepare_arl1_evidence_bundle_deployment,
    verify_arl1_qualification_deployment,
)
from aletheia.arl1_runtime import (
    ARL1CampaignRPCServiceSetV1,
    ARL1CampaignRuntimeConfigV1,
)
from aletheia.arl1_verifier import (
    ARL0GateCommandPinV1,
    ARL1EvidenceBundleSourceV1,
    SubprocessARL0GateReplayPort,
)
from aletheia.research_controller.external_rpc import ControllerWorkerRPCServicePin
from aletheia.research_controller.step_executor import (
    ControllerStepAuthorityBinding,
    ControllerStepAuthorityRole,
)
from aletheia.research_kernel.policy import ed25519_key_id, ed25519_public_key_hex
from aletheia.research_kernel.schemas import canonical_json_bytes

from .test_arl1_qualification import (
    VERIFIER_PRIVATE_KEY,
    arl1_case as arl1_case,
)
from .test_arl1_runtime import _replicate_bridge_cases, _runtime_config


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


_SERVICE_ROLES = {
    "execution_registration": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "raw_run_source": (ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,),
    "database_observation": (ControllerStepAuthorityRole.DATABASE_ATTESTATION,),
    "independent_validation": (ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,),
    "independent_admission": (ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,),
    "atomic_admission": (
        ControllerStepAuthorityRole.DATABASE_ATTESTATION,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
        ControllerStepAuthorityRole.KERNEL_COMMAND,
    ),
}

_PRIMARY_ROLES = {
    "database_observation": ControllerStepAuthorityRole.DATABASE_ATTESTATION,
    "independent_validation": ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
    "independent_admission": ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
}


def _bridge_bound_campaign_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[ARL1CampaignRuntimeConfigV1, object]:
    bridge_fixture_module = sys.modules[_replicate_bridge_cases.__module__]
    runtime_fixture_module = sys.modules[bridge_fixture_module._signed_case.__module__]
    protocol_fixture_module = sys.modules[bridge_fixture_module.fixture_by_name.__module__]
    evidence_now = bridge_fixture_module.NOW
    frozen_now = runtime_fixture_module._signed_case.__kwdefaults__["quote_at"]
    monkeypatch.setattr(bridge_fixture_module, "NOW", frozen_now)
    monkeypatch.setattr(runtime_fixture_module, "NOW", frozen_now)
    monkeypatch.setattr(protocol_fixture_module, "_NOW", frozen_now)
    base, _controller_manifest = _runtime_config(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge_fixture_module, "NOW", evidence_now)
    monkeypatch.setattr(runtime_fixture_module, "NOW", evidence_now)
    monkeypatch.setattr(protocol_fixture_module, "_NOW", evidence_now)
    bridge = _replicate_bridge_cases()[0]
    old_bindings = {item.role: item for item in base.authority_bindings}
    bridge_pins = {
        ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION: bridge.execution_pin,
        ControllerStepAuthorityRole.INDEPENDENT_VALIDATION: bridge.validator_pin,
        ControllerStepAuthorityRole.INDEPENDENT_ADMISSION: bridge.admission_pin,
    }
    bindings: list[ControllerStepAuthorityBinding] = []
    for role in sorted(old_bindings, key=lambda item: item.value):
        old = old_bindings[role]
        values = old.model_dump(mode="python")
        if role in bridge_pins:
            pin = bridge_pins[role]
            values.update(
                principal_id=pin.principal_id,
                key_id=pin.key_id,
                policy_sha256=pin.policy_sha256,
            )
        elif role is ControllerStepAuthorityRole.DATABASE_ATTESTATION:
            values.update(
                principal_id=bridge.database_pin.principal_id,
                key_id=bridge.database_pin.key_id,
                policy_sha256=bridge.database_pin.policy_sha256,
            )
        if role is ControllerStepAuthorityRole.INDEPENDENT_VALIDATION:
            values["service_manifest_sha256"] = (
                bridge.authorization.message.validator_manifest_sha256
            )
        bindings.append(ControllerStepAuthorityBinding.model_validate(values))
    by_role = {item.role: item for item in bindings}
    service_values: dict[str, ControllerWorkerRPCServicePin] = {}
    for name, old in base.rpc_services.named_pins:
        values = old.model_dump(mode="python", exclude={"service_id"})
        values["authority_binding_sha256s"] = tuple(
            sorted(by_role[role].binding_sha256 for role in _SERVICE_ROLES[name])
        )
        primary = _PRIMARY_ROLES.get(name)
        if primary is not None:
            binding = by_role[primary]
            values.update(
                service_principal_id=binding.principal_id,
                service_manifest_sha256=binding.service_manifest_sha256,
                service_policy_sha256=binding.policy_sha256,
            )
        service_values[name] = ControllerWorkerRPCServicePin.model_validate(values)
    services = ARL1CampaignRPCServiceSetV1.model_validate(service_values)
    campaign = ARL1CampaignRuntimeConfigV1.model_validate(
        {
            **base.model_dump(
                mode="python",
                exclude={"configuration_id", "authority_bindings", "rpc_services"},
            ),
            "authority_bindings": tuple(bindings),
            "rpc_services": services,
        }
    )
    return campaign, bridge


def _verifier_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bundle,
) -> ARL1EvidenceVerifierRuntimeConfigV1:
    campaign, bridge = _bridge_bound_campaign_runtime(monkeypatch, tmp_path)
    f9_root = (tmp_path / "arl1-f9-v2-read-archive").resolve()
    f9_root.mkdir(mode=0o700)
    f9_root.chmod(0o700)
    f9_metadata = f9_root.stat()
    executable = Path(sys.executable).resolve(strict=True)
    repository_root = Path(__file__).resolve().parents[2]
    qualification_source = (repository_root / "aletheia/arl1.py").resolve(strict=True)
    verifier_source = (repository_root / "aletheia/arl1_verifier.py").resolve(strict=True)
    runtime_source = (repository_root / "aletheia/arl1_qualification_runtime.py").resolve(
        strict=True
    )
    pins = tuple(
        ARL0GateCommandPinV1(
            gate_kind=kind,
            evaluated_scope_sha256=_sha(f"runtime-gate:{kind.value}"),
            executable_path=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            arguments=(),
            working_directory=str(repository_root),
            pinned_inputs=(),
            environment=(("PATH", "/usr/bin:/bin"),),
            replay_principal_id=bundle.policy.evidence_verifier_pins[0].principal_id,
            timeout_seconds=1,
        )
        for kind in ARL0GateKind
    )
    return ARL1EvidenceVerifierRuntimeConfigV1(
        campaign_runtime=campaign,
        execution_authority_pin=bridge.execution_pin,
        validator_authority_pin=bridge.validator_pin,
        admission_authority_pin=bridge.admission_pin,
        database_authority_pin=bridge.database_pin,
        validation_archive=ARL1F9V2ArchiveReadConfigV1(
            root=str(f9_root),
            owner_uid=f9_metadata.st_uid,
            group_gid=f9_metadata.st_gid,
            device_id=f9_metadata.st_dev,
            inode=f9_metadata.st_ino,
            validator_manifest_sha256=(bridge.authorization.message.validator_manifest_sha256),
            validator_authority_pin=bridge.validator_pin,
        ),
        arl0_gate_command_pins=pins,
        trusted_verifier_pins=tuple(
            sorted(bundle.policy.evidence_verifier_pins, key=lambda item: item.pin_sha256)
        ),
        qualification_contract_source_path=str(qualification_source),
        qualification_contract_source_sha256=hashlib.sha256(
            qualification_source.read_bytes()
        ).hexdigest(),
        verifier_implementation_source_path=str(verifier_source),
        verifier_implementation_source_sha256=hashlib.sha256(
            verifier_source.read_bytes()
        ).hexdigest(),
        runtime_implementation_source_path=str(runtime_source),
        runtime_implementation_source_sha256=hashlib.sha256(
            runtime_source.read_bytes()
        ).hexdigest(),
        prepared_at=bundle.prepared_at,
    )


def _write_canonical(path: Path, value) -> tuple[str, str]:
    payload = canonical_json_bytes(value)
    path.write_bytes(payload)
    path.chmod(0o400)
    return str(path.resolve()), hashlib.sha256(payload).hexdigest()


def _write_key(path: Path, value: bytes) -> ARL1PrivateSigningKeyPinV1:
    path.write_bytes(value)
    path.chmod(0o400)
    return ARL1PrivateSigningKeyPinV1(
        path=str(path.resolve()),
        file_sha256=hashlib.sha256(value).hexdigest(),
        key_id=ed25519_key_id(ed25519_public_key_hex(value)),
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )


def _enable_linux_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualification_runtime.sys, "platform", "linux")
    monkeypatch.setattr(qualification_runtime.os, "geteuid", os.geteuid)
    monkeypatch.setattr(qualification_runtime.os, "getegid", os.getegid)
    monkeypatch.setattr(qualification_runtime, "require_schema_exact", lambda: None)


def test_full_gate_command_set_uses_the_cumulative_arl0_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arl1_case,
) -> None:
    bundle, _qualification_key, _source_verifier = arl1_case
    config = _verifier_runtime_config(monkeypatch, tmp_path, bundle)

    replayer = SubprocessARL0GateReplayPort(config.arl0_gate_command_pins)

    assert tuple(item.gate_kind for item in replayer.pins) == tuple(ARL0GateKind)
    with pytest.raises(ValueError, match="canonical"):
        SubprocessARL0GateReplayPort(tuple(reversed(config.arl0_gate_command_pins)))


def test_verifier_runtime_config_rejects_an_evaluated_authority_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arl1_case,
) -> None:
    bundle, _qualification_key, _source_verifier = arl1_case
    config = _verifier_runtime_config(monkeypatch, tmp_path, bundle)
    overlapping = ARL1EvidenceVerifierPinV1(
        verification_policy_sha256=config.execution_authority_pin.policy_sha256,
        principal_id=config.execution_authority_pin.principal_id,
        key_id=config.trusted_verifier_pins[0].key_id,
        public_key_ed25519_hex=config.trusted_verifier_pins[0].public_key_ed25519_hex,
        valid_from=config.trusted_verifier_pins[0].valid_from,
        expires_at=config.trusted_verifier_pins[0].expires_at,
    )

    with pytest.raises(ValidationError, match="overlaps an evaluated runtime authority"):
        ARL1EvidenceVerifierRuntimeConfigV1.model_validate(
            {
                **config.model_dump(mode="python"),
                "configuration_id": None,
                "trusted_verifier_pins": (overlapping,),
            }
        )


def test_prepare_issue_and_keyless_audit_are_separate_exact_file_flows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arl1_case,
) -> None:
    bundle, qualification_private_key, source_verifier = arl1_case
    config = _verifier_runtime_config(monkeypatch, tmp_path, bundle)
    config_path, config_file_sha256 = _write_canonical(tmp_path / "verifier.json", config)
    source = ARL1EvidenceBundleSourceV1(
        policy=bundle.policy,
        arl0_integrity=bundle.arl0_integrity,
        target_campaign_request=bundle.target_campaign_request,
        target_campaign_receipt=bundle.target_campaign_receipt,
        protocol_campaigns=bundle.protocol_campaigns,
        evidence_archive_manifest_sha256=bundle.evidence_archive_manifest_sha256,
    )
    source_path, source_file_sha256 = _write_canonical(tmp_path / "source.json", source)
    verifier_key = _write_key(tmp_path / "source-verifier.key", VERIFIER_PRIVATE_KEY)
    source_signer = type(
        "SourceSigner",
        (),
        {"issue_source_receipts": lambda _self, _source: bundle.source_verification_receipts},
    )()
    compose_calls: list[tuple[bytes | None, str | None]] = []

    def compose(
        _config,
        *,
        source_signing_private_key=None,
        source_signing_pin_sha256=None,
        clock=None,
    ):
        assert clock is not None
        compose_calls.append((source_signing_private_key, source_signing_pin_sha256))
        return source_signer if source_signing_private_key is not None else source_verifier

    monkeypatch.setattr(qualification_runtime, "compose_arl1_evidence_verifier", compose)
    _enable_linux_runtime(monkeypatch)
    source_deployment = ARL1SourceVerificationDeploymentV1(
        configuration_path=config_path,
        configuration_file_sha256=config_file_sha256,
        configuration_sha256=config.configuration_sha256,
        source_path=source_path,
        source_file_sha256=source_file_sha256,
        source_sha256=hashlib.sha256(canonical_json_bytes(source)).hexdigest(),
        expected_policy_sha256=bundle.policy.policy_sha256,
        source_verifier_signing_key=verifier_key,
        signing_pin_sha256=bundle.policy.evidence_verifier_pins[0].pin_sha256,
        process_principal_id=bundle.policy.evidence_verifier_pins[0].principal_id,
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        approved_at=bundle.prepared_at,
    )

    prepared = prepare_arl1_evidence_bundle_deployment(source_deployment)

    assert prepared == bundle
    assert compose_calls == [
        (VERIFIER_PRIVATE_KEY, bundle.policy.evidence_verifier_pins[0].pin_sha256)
    ]
    bundle_path, bundle_file_sha256 = _write_canonical(tmp_path / "bundle.json", prepared)
    anchor = ARL1QualificationTrustAnchorV1.from_policy(bundle.policy)
    anchor_path, anchor_file_sha256 = _write_canonical(tmp_path / "anchor.json", anchor)
    qualification_key = _write_key(tmp_path / "qualification.key", qualification_private_key)
    qualified_at = bundle.prepared_at + timedelta(seconds=1)
    issuance = ARL1QualificationIssuanceDeploymentV1(
        configuration_path=config_path,
        configuration_file_sha256=config_file_sha256,
        configuration_sha256=config.configuration_sha256,
        bundle_path=bundle_path,
        bundle_file_sha256=bundle_file_sha256,
        bundle_sha256=bundle.bundle_sha256,
        trust_anchor_path=anchor_path,
        trust_anchor_file_sha256=anchor_file_sha256,
        trust_anchor_sha256=anchor.anchor_sha256,
        expected_policy_sha256=bundle.policy.policy_sha256,
        qualification_signing_key=qualification_key,
        process_principal_id=anchor.qualification_authority_principal_id,
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        issuance_not_before=qualified_at,
        issuance_deadline=qualified_at + timedelta(minutes=1),
        receipt_validity_seconds=86_400,
    )

    receipt = issue_arl1_qualification_deployment(issuance, clock=lambda: qualified_at)

    assert compose_calls[-1] == (None, None)
    with pytest.raises(ValidationError, match="issuance window"):
        ARL1QualificationIssuanceDeploymentV1.model_validate(
            {
                **issuance.model_dump(mode="python"),
                "deployment_id": None,
                "issuance_deadline": issuance.issuance_not_before + timedelta(hours=25),
            }
        )
    with pytest.raises(ARL1QualificationRuntimeError, match="approved time window"):
        issue_arl1_qualification_deployment(
            issuance,
            clock=lambda: issuance.issuance_deadline,
        )
    receipt_path, receipt_file_sha256 = _write_canonical(tmp_path / "receipt.json", receipt)
    audit = ARL1QualificationVerificationDeploymentV1(
        configuration_path=config_path,
        configuration_file_sha256=config_file_sha256,
        configuration_sha256=config.configuration_sha256,
        bundle_path=bundle_path,
        bundle_file_sha256=bundle_file_sha256,
        bundle_sha256=bundle.bundle_sha256,
        trust_anchor_path=anchor_path,
        trust_anchor_file_sha256=anchor_file_sha256,
        trust_anchor_sha256=anchor.anchor_sha256,
        receipt_path=receipt_path,
        receipt_file_sha256=receipt_file_sha256,
        receipt_sha256=receipt.receipt_sha256,
        expected_policy_sha256=bundle.policy.policy_sha256,
        process_principal_id="principal:arl1-receipt-auditor",
        process_uid=os.geteuid(),
        process_gid=os.getegid(),
        verification_not_before=qualified_at + timedelta(seconds=1),
        verification_deadline=qualified_at + timedelta(minutes=1),
    )

    assert (
        verify_arl1_qualification_deployment(
            audit,
            clock=lambda: qualified_at + timedelta(seconds=1),
        )
        == receipt
    )
    assert compose_calls[-1] == (None, None)
    with pytest.raises(ARL1QualificationRuntimeError, match="auditor separation"):
        verify_arl1_qualification_deployment(
            ARL1QualificationVerificationDeploymentV1.model_validate(
                {
                    **audit.model_dump(mode="python"),
                    "deployment_id": None,
                    "process_principal_id": bundle.policy.evidence_verifier_pins[0].principal_id,
                }
            ),
            clock=lambda: qualified_at + timedelta(seconds=1),
        )

    expired_audit = ARL1QualificationVerificationDeploymentV1.model_validate(
        {
            **audit.model_dump(mode="python"),
            "deployment_id": None,
            "verification_not_before": receipt.message.expires_at,
            "verification_deadline": receipt.message.expires_at + timedelta(minutes=1),
        }
    )
    with pytest.raises(ARL1QualificationError, match="not active"):
        verify_arl1_qualification_deployment(
            expired_audit,
            clock=lambda: receipt.message.expires_at,
        )


@pytest.mark.parametrize("operation", ("prepare", "issue", "verify"))
def test_every_programmatic_qualification_operation_checks_schema_before_inputs(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arl1_case,
) -> None:
    bundle, qualification_private_key, _source_verifier = arl1_case
    config = _verifier_runtime_config(monkeypatch, tmp_path, bundle)
    missing = str((tmp_path / "missing.json").resolve())
    common = {
        "configuration_path": missing,
        "configuration_file_sha256": "1" * 64,
        "configuration_sha256": config.configuration_sha256,
        "process_principal_id": "principal:arl1-runtime-schema-test",
        "process_uid": os.geteuid(),
        "process_gid": os.getegid(),
    }
    if operation == "prepare":
        deployment = ARL1SourceVerificationDeploymentV1(
            **common,
            source_path=str((tmp_path / "missing-source.json").resolve()),
            source_file_sha256="2" * 64,
            source_sha256="3" * 64,
            expected_policy_sha256=bundle.policy.policy_sha256,
            source_verifier_signing_key=_write_key(
                tmp_path / "schema-source.key", VERIFIER_PRIVATE_KEY
            ),
            signing_pin_sha256=bundle.policy.evidence_verifier_pins[0].pin_sha256,
            approved_at=bundle.prepared_at,
        )
        invoke = prepare_arl1_evidence_bundle_deployment
    elif operation == "issue":
        deployment = ARL1QualificationIssuanceDeploymentV1(
            **common,
            bundle_path=str((tmp_path / "missing-bundle.json").resolve()),
            bundle_file_sha256="2" * 64,
            bundle_sha256=bundle.bundle_sha256,
            trust_anchor_path=str((tmp_path / "missing-anchor.json").resolve()),
            trust_anchor_file_sha256="3" * 64,
            trust_anchor_sha256="4" * 64,
            expected_policy_sha256=bundle.policy.policy_sha256,
            qualification_signing_key=_write_key(
                tmp_path / "schema-qualification.key", qualification_private_key
            ),
            issuance_not_before=bundle.prepared_at + timedelta(seconds=1),
            issuance_deadline=bundle.prepared_at + timedelta(minutes=1),
            receipt_validity_seconds=86_400,
        )
        invoke = issue_arl1_qualification_deployment
    else:
        deployment = ARL1QualificationVerificationDeploymentV1(
            **common,
            bundle_path=str((tmp_path / "missing-bundle.json").resolve()),
            bundle_file_sha256="2" * 64,
            bundle_sha256=bundle.bundle_sha256,
            trust_anchor_path=str((tmp_path / "missing-anchor.json").resolve()),
            trust_anchor_file_sha256="3" * 64,
            trust_anchor_sha256="4" * 64,
            receipt_path=str((tmp_path / "missing-receipt.json").resolve()),
            receipt_file_sha256="5" * 64,
            receipt_sha256="6" * 64,
            expected_policy_sha256=bundle.policy.policy_sha256,
            verification_not_before=bundle.prepared_at + timedelta(seconds=1),
            verification_deadline=bundle.prepared_at + timedelta(minutes=1),
        )
        invoke = verify_arl1_qualification_deployment
    monkeypatch.setattr(qualification_runtime.sys, "platform", "linux")
    monkeypatch.setattr(qualification_runtime.os, "geteuid", os.geteuid)
    monkeypatch.setattr(qualification_runtime.os, "getegid", os.getegid)

    def reject_schema() -> None:
        raise RuntimeError("schema drift sentinel")

    monkeypatch.setattr(qualification_runtime, "require_schema_exact", reject_schema)

    with pytest.raises(RuntimeError, match="schema drift sentinel"):
        invoke(deployment)
