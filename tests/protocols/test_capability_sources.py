"""Signature, source custody and scope regressions for capability evidence."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from cryptography.exceptions import InvalidSignature

from aletheia.execution import capability_sources as closure
from aletheia.protocols.capabilities import CapabilityManifestV2


def verify(case):
    return closure.verify_capability_sources(
        case.request, root=case.root, trust=case.trust, runtime_sources=case.runtime
    )


def test_signed_sources_and_separate_qualification_are_verified(case):
    result = verify(case)
    assert len(result["verified_capabilities"]) == 1
    assert result["scientific_authority"] is False


def test_environment_identity_cannot_alias_another_retained_source(case):
    runtime = next(iter(case.runtime.values()))
    runtime["environment_source_path"] = str(case.impl)
    runtime["environment_source_sha256"] = runtime["implementation_sha256"]
    with pytest.raises(closure.CapabilitySourceVerificationError, match="retained source bytes"):
        verify(case)


@pytest.mark.parametrize(
    "case", ["load_raw_run", "prepare_validation_campaign", "run_cuprate_diagnostic"], indirect=True
)
def test_signed_local_service_contract_covers_actual_io_and_sources(case):
    assert verify(case)["verified_capabilities"]
    manifest = case.request.capability_catalog.manifests[0]
    assert manifest.runtime.runtime_kind.value == "external_service"
    assert (
        manifest.output_ports[0].schema_ref.schema_sha256
        == case.service.contract["schema_sources"]["output"]
    )


@pytest.mark.parametrize("case", ["load_raw_run"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "lookup", "envelope", "slot_mapping"])
def test_raw_service_requires_its_archive_input_contract(case, change):
    step = case.request.protocol.steps[0]
    if change == "missing":
        step.archived_observation_input = None
    elif change == "lookup":
        step.archived_observation_input.lookup_input_port_id = "input.other"
    elif change == "envelope":
        step.archived_observation_input.envelope_output_port_id = "output.other"
    else:
        step.archived_observation_input.replicate_mapping = "any_slot"
    with pytest.raises(closure.CapabilitySourceVerificationError, match="archive input"):
        verify(case)


@pytest.mark.parametrize("case", ["load_raw_run", "prepare_validation_campaign"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "contract_bytes",
        "source_missing",
        "source_path",
        "schema_missing",
        "wrong_operation",
        "wrong_role",
    ],
)
def test_local_service_closure_rejects_rebound_contracts(case, change):
    runtime = next(iter(case.runtime.values()))
    if change == "contract_bytes":
        path = Path(runtime["service_contract_path"])
        path.chmod(0o600)
        path.write_bytes(b"{}")
        path.chmod(0o400)
    elif change == "source_missing":
        digest = case.service.contract["source_files"]["research_controller/external_rpc_server.py"]
        (case.root / "objects" / digest[:2] / digest).unlink()
    elif change == "source_path":
        runtime["service_source_paths"]["research_controller/external_rpc_server.py"] = str(
            case.impl
        )
    elif change == "schema_missing":
        digest = case.service.contract["schema_sources"]["wire_response"]
        (case.root / "objects" / digest[:2] / digest).unlink()
    elif change == "wrong_operation":
        runtime["service_operation"] = "issue_validation_receipt"
    elif change == "wrong_role":
        case.request.protocol.steps[0].role = NS(value="scientific_executor")
    with pytest.raises((closure.CapabilitySourceVerificationError, FileNotFoundError)):
        verify(case)


@pytest.mark.parametrize("case", ["prepare_validation_campaign"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "wrong_schema", "optional"])
def test_validation_service_requires_the_persistent_campaign_artifact(case, change):
    step = case.request.protocol.steps[0]
    if change == "missing":
        step.expected_artifacts = ()
    elif change == "wrong_schema":
        step.expected_artifacts[0].schema_sha256 = case.service.contract["schema_sources"]["output"]
    else:
        step.expected_artifacts[0].required = False
    with pytest.raises(
        closure.CapabilitySourceVerificationError, match="committed campaign receipt"
    ):
        verify(case)


@pytest.mark.parametrize("case", ["prepare_validation_campaign"], indirect=True)
@pytest.mark.parametrize(
    "change", ["pure", "no_write", "wrong_output", "extra_port", "unbound_contract"]
)
def test_validation_service_rejects_inaccurate_capability_declarations(case, change):
    value = case.request.capability_catalog.manifests[0].model_dump(mode="json")
    runtime = next(iter(case.runtime.values()))
    if change == "pure":
        value["runtime"]["determinism"] = "deterministic"
    elif change == "no_write":
        value["side_effect_class"] = "none"
    elif change == "wrong_output":
        value["output_ports"][0]["schema_ref"]["schema_sha256"] = case.service.contract[
            "schema_sources"
        ]["committed_campaign"]
    elif change == "extra_port":
        extra = copy.deepcopy(value["output_ports"][0])
        extra["port_id"] = "output.whole_assessment"
        value["output_ports"].append(extra)
    else:
        value["applicability"]["required_condition_sha256s"].remove(
            runtime["service_contract_sha256"]
        )
    manifest = CapabilityManifestV2.model_validate(value)
    with pytest.raises(closure.CapabilitySourceVerificationError, match="local service"):
        closure._verify_local_service_sources(
            case.root, manifest, runtime, case.request.protocol.steps[0]
        )


@pytest.mark.parametrize(
    "name",
    [
        "qualification_rule",
        "authority",
        "required_condition",
        "excluded_condition",
        "safety_approval",
        "hazard",
        "license",
        "egress",
        "retention",
        "schema",
    ],
)
def test_signed_capability_requires_retained_contract_bodies(case, name):
    assert verify(case)["verified_capabilities"]
    digest = case.contract_digests[name]
    (case.root / "objects" / digest[:2] / digest).unlink()
    with pytest.raises((closure.CapabilitySourceVerificationError, FileNotFoundError)):
        verify(case)


def test_failure_and_retry_rule_bodies_are_required(case):
    manifest = case.request.capability_catalog.manifests[0]
    for digest in {
        *(f.detection_rule_sha256 for f in manifest.failure_modes),
        *filter(
            None,
            (manifest.retry.idempotency_rule_sha256, manifest.retry.reconciliation_rule_sha256),
        ),
    }:
        path = case.root / "objects" / digest[:2] / digest
        original = path.read_bytes()
        path.unlink()
        with pytest.raises((closure.CapabilitySourceVerificationError, FileNotFoundError)):
            verify(case)
        case.put(original)


def test_calibration_operating_envelope_source_is_required(case):
    value = case.request.capability_catalog.manifests[0].model_dump(mode="json")
    envelope = case.put(b"actual unit-test operating envelope")
    value["calibration"] = {
        "mode": "self_check",
        "maximum_age_seconds": 60,
        "calibration_receipt_schema": value["output_ports"][0]["schema_ref"],
        "operating_envelope_sha256": envelope,
    }
    manifest = CapabilityManifestV2.model_validate(value)
    assert envelope in closure.contract_sources(case.root, manifest)
    (case.root / "objects" / envelope[:2] / envelope).unlink()
    with pytest.raises((closure.CapabilitySourceVerificationError, FileNotFoundError)):
        closure.contract_sources(case.root, manifest)


@pytest.mark.parametrize(
    "change",
    [
        "missing_audit",
        "changed_audit",
        "changed_code",
        "wrong_key",
        "same_role",
        "missing_kind",
        "wrong_callable",
        "stale_pin",
    ],
)
def test_source_closure_refuses_incomplete_or_rebound_material(case, change):
    digest = next(iter(case.audits.values()))
    path = case.root / "objects" / digest[:2] / digest
    if change == "missing_audit":
        path.unlink()
    elif change == "changed_audit":
        path.chmod(0o600)
        path.write_bytes(b"{}")
        path.chmod(0o400)
    elif change == "changed_code":
        case.impl.chmod(0o600)
        case.impl.write_bytes(b"def execute(): return 2\n")
        case.impl.chmod(0o400)
    elif change == "wrong_key":
        case.trust["auditor"]["public_key_ed25519_hex"] = "01" * 32
        case.trust["auditor"]["key_id"] = closure.sha(bytes.fromhex("01" * 32))
    elif change == "same_role":
        case.trust["qualifier"] = copy.deepcopy(case.trust["auditor"])
    elif change == "missing_kind":
        req = case.request.protocol.steps[0].capability_requirement
        req.audit_bindings = req.audit_bindings[:-1]
    elif change == "wrong_callable":
        next(iter(case.runtime.values()))["adapter_ref"] = "check_impl:missing"
    elif change == "stale_pin":
        case.trust["auditor"]["expires_at"] = "2026-09-08T00:00:00+00:00"
    expected = (
        FileNotFoundError
        if change == "missing_audit"
        else closure.CapabilitySourceVerificationError
    )
    # The record must stay bound to its original trusted key identity.
    with pytest.raises(expected):
        verify(case)


def test_schema_reference_needs_its_actual_source_bytes(case):
    payload = b'{"type":"string"}'
    digest = case.put(payload)
    ref = dict(schema_name="aletheia.json_schema_ref", schema_sha256=digest)
    assert closure.schema_sources(case.root, {"ports": [ref]}) == {digest}
    ref["schema_sha256"] = "0" * 64
    with pytest.raises(FileNotFoundError):
        closure.schema_sources(case.root, ref)


def test_rehashed_forged_signature_is_rejected(case):
    digest = next(iter(case.audits.values()))
    record = json.loads(closure.source(case.root, digest))
    signature = bytes.fromhex(record["signature_ed25519_hex"])
    record["signature_ed25519_hex"] = (bytes([signature[0] ^ 1]) + signature[1:]).hex()
    forged = case.put(closure.canonical_json_bytes(record))
    with pytest.raises(InvalidSignature):
        closure.signed_record(
            case.root, forged, case.trust["auditor"], "aletheia.capability_engineering_audit.v1"
        )


def test_optimized_python_cannot_disable_verification():
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "from aletheia.execution.capability_sources import utc; utc('2026-09-09T00:00:00')",
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"CapabilitySourceVerificationError" in result.stderr


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "empty"])
def test_fresh_reads_reject_unsafe_objects(tmp_path, kind):
    path = tmp_path / "object"
    path.write_bytes(b"valid")
    path.chmod(0o400)
    if kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif kind == "hardlink":
        (tmp_path / "linked").hardlink_to(path)
    elif kind == "writable":
        path.chmod(0o620)
    elif kind == "empty":
        path.chmod(0o600)
        path.write_bytes(b"")
    with pytest.raises(closure.CapabilitySourceVerificationError):
        closure.fresh(path)


def test_source_root_cannot_be_rebound_through_a_symlink(case, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(case.root, target_is_directory=True)
    with pytest.raises(closure.CapabilitySourceVerificationError):
        closure.source(alias, next(iter(case.audits.values())))


@pytest.mark.parametrize(
    "change",
    ["failed", "empty", "scope", "missing_input", "late", "unretained", "unchecked_policy"],
)
def test_audit_requires_a_retained_scoped_check_result(case, change):
    audit = closure.signed_record(
        case.root,
        next(iter(case.audits.values())),
        case.trust["auditor"],
        "aletheia.capability_engineering_audit.v1",
    )
    result = json.loads(closure.source(case.root, audit["check_result_sha256"]))
    if change == "failed":
        result["checks"][0]["passed"] = False
    elif change == "empty":
        result["checks"] = []
    elif change == "scope":
        result["definition_sha256"] = "0" * 64
    elif change == "missing_input":
        result["checks"][0]["source_sha256s"] = ["0" * 64]
    elif change == "late":
        result["checked_at"] = "2026-09-10T00:00:00+00:00"
    elif change == "unchecked_policy":
        result["checks"][0]["source_sha256s"].remove(case.contract_digests["authority"])
    new_digest = case.put(closure.canonical_json_bytes(result))
    audit["check_result_sha256"] = new_digest
    if change != "unretained":
        audit["materials"] = sorted(set([*audit["materials"], new_digest]))
    else:
        audit["materials"] = [item for item in audit["materials"] if item != new_digest]
    with pytest.raises((closure.CapabilitySourceVerificationError, FileNotFoundError)):
        closure._verify_check_result(case.root, audit, required_sources=case.required_sources)


def _pinned_verifier(case, tmp_path):
    paths = {}
    for name, value in (("trust", case.trust), ("runtime_sources", case.runtime)):
        path = tmp_path / (name + ".json")
        payload = closure.canonical_json_bytes(value)
        path.write_bytes(payload)
        path.chmod(0o400)
        paths[name + "_path"] = str(path)
        paths[name + "_sha256"] = closure.sha(payload)
    implementation = Path(closure.__file__).resolve()
    config = closure.CapabilitySourceRuntimeConfigV1(
        source_root=str(case.root),
        implementation_source_path=str(implementation),
        implementation_source_sha256=closure.sha(implementation.read_bytes()),
        **paths,
    )
    case.request.model_dump = lambda **kwargs: {}
    return closure.PinnedCapabilitySourceVerifier(config)


def test_pinned_verifier_freshly_rechecks_sources_after_a_success(case, tmp_path):
    verifier = _pinned_verifier(case, tmp_path)
    result = verifier.verify(case.request, observed_at=case.request.protocol.authored_at)
    assert len(result["verified_capabilities"]) == 1
    path = Path(verifier.config.runtime_sources_path)
    path.chmod(0o600)
    path.write_bytes(b"{}")
    path.chmod(0o400)
    with pytest.raises(closure.CapabilitySourceVerificationError, match="deployment pin"):
        verifier.verify(case.request, observed_at=case.request.protocol.authored_at)


def test_pinned_verifier_rejects_source_and_verifier_identity_changes(case, tmp_path):
    verifier = _pinned_verifier(case, tmp_path)
    verifier.config = verifier.config.model_copy(update={"implementation_source_sha256": "0" * 64})
    with pytest.raises(closure.CapabilitySourceVerificationError, match="implementation differs"):
        verifier.verify(case.request, observed_at=case.request.protocol.authored_at)
