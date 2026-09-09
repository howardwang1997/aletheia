"""Signature, source custody and scope regressions for capability evidence."""

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aletheia.protocols import capability_sources as closure
from aletheia.protocols.capabilities import CapabilityManifestV2
from aletheia.protocols.schemas import CapabilityAuditBinding, CapabilityAuditKind
from tests.protocols.fixtures import fixture_by_name


@pytest.fixture
def case(tmp_path):
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    source_root = tmp_path / "sources"
    keys = {name: Ed25519PrivateKey.generate() for name in ("auditor", "qualifier")}
    pins = {}
    for name, key in keys.items():
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        pins[name] = dict(
            principal_id="principal:engineering-" + name,
            key_id=closure.sha(public),
            public_key_ed25519_hex=public.hex(),
            valid_from=(now - timedelta(days=1)).isoformat(),
            expires_at=(now + timedelta(days=2)).isoformat(),
        )

    def put(payload):
        digest = closure.sha(payload)
        p = source_root / "objects" / digest[:2] / digest
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            assert p.read_bytes() == payload
            return digest
        p.write_bytes(payload)
        p.chmod(0o400)
        return digest

    implementation = b'def execute():\n    return {"value": 1}\n'
    impl = tmp_path / "check_impl.py"
    impl.write_bytes(implementation)
    impl.chmod(0o400)
    env = tmp_path / "environment.json"
    env.write_bytes(b'{"kind":"unit-test-environment"}')
    env.chmod(0o400)
    impl_sha, env_sha = put(implementation), put(env.read_bytes())
    fixture = fixture_by_name("grouped_regression")
    base = fixture.request.capability_catalog.manifests[0].model_dump(mode="json")
    base.update(external_action_kind=None, side_effect_class="none", frozen_at=now.isoformat())
    base["runtime"].update(
        adapter_ref="check_impl:execute",
        runtime_kind="deterministic_function",
        implementation_sha256=impl_sha,
        environment_sha256=env_sha,
    )
    base["calibration"] = {"mode": "not_applicable"}
    base["qualification"] = dict(
        status="provisional", qualification_rule_sha256=closure.canonical_sha256(closure.RULE)
    )
    contract_digests = {"qualification_rule": put(closure.canonical_json_bytes(closure.RULE))}

    def policy(name):
        digest = put(closure.canonical_json_bytes({"unit_test_contract": name}))
        contract_digests[name] = digest
        return digest

    base["principal"]["authority_policy_sha256"] = policy("authority")
    base["applicability"]["required_condition_sha256s"] = [policy("required_condition")]
    base["applicability"]["excluded_condition_sha256s"] = [policy("excluded_condition")]
    for failure in base["failure_modes"]:
        failure["detection_rule_sha256"] = policy("failure_" + failure["failure_id"])
    base["safety"]["approval_policy_sha256"] = policy("safety_approval")
    base["safety"]["hazard_sha256s"] = [policy("hazard")]
    for name in ("license", "egress", "retention"):
        base["license_egress"][name + "_policy_sha256"] = policy(name)
    for name in ("idempotency_rule_sha256", "reconciliation_rule_sha256"):
        if base["retry"].get(name) is not None:
            base["retry"][name] = policy(name)

    def retain_schema_refs(value):
        if isinstance(value, dict):
            if value.get("schema_name") == "aletheia.json_schema_ref":
                digest = put(closure.canonical_json_bytes({"type": "object"}))
                value["schema_sha256"] = digest
                contract_digests["schema"] = digest
            for nested in value.values():
                retain_schema_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                retain_schema_refs(nested)

    retain_schema_refs(base)
    draft = CapabilityManifestV2.model_validate(base)
    definition = closure.definition_sha256(draft)
    required_sources = closure.contract_sources(source_root, draft) | {impl_sha, env_sha}

    def issue(role, kind, issued_at, materials, **fields):
        pin = pins[role]
        msg = dict(
            schema_name="aletheia.capability_engineering_" + kind + ".v1",
            principal_id=pin["principal_id"],
            key_id=pin["key_id"],
            scientific_authority=False,
            issued_at=issued_at.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
            definition_sha256=definition,
            materials=sorted(set(materials)),
            **fields,
        )
        record = dict(
            message=msg,
            signature_ed25519_hex=keys[role].sign(closure.canonical_json_bytes(msg)).hex(),
        )
        return put(closure.canonical_json_bytes(record))

    audits = {}
    for kind in CapabilityAuditKind:
        if kind is CapabilityAuditKind.CALIBRATION:
            continue
        check_result = put(
            closure.canonical_json_bytes(
                {
                    "schema_name": "aletheia.capability_engineering_check.v1",
                    "definition_sha256": definition,
                    "audit_kind": kind.value,
                    "audit_policy_sha256": closure.expected_capability_audit_policy_sha256(
                        draft, kind
                    ),
                    "scientific_authority": False,
                    "result": "passed",
                    "checked_at": (now - timedelta(seconds=11)).isoformat(),
                    "checks": [
                        {
                            "check_id": "unit-test-source-contract",
                            "passed": True,
                            "source_sha256s": sorted(required_sources),
                        }
                    ],
                }
            )
        )
        audits[kind] = issue(
            "auditor",
            "audit",
            now - timedelta(seconds=10),
            [*required_sources, check_result],
            check_result_sha256=check_result,
            audit_kind=kind.value,
            audit_policy_sha256=closure.expected_capability_audit_policy_sha256(draft, kind),
            result="passed",
        )
    decision = issue(
        "qualifier",
        "qualification",
        now - timedelta(seconds=5),
        list(audits.values()),
        result="qualified",
        audit_receipt_sha256s=sorted(audits.values()),
    )
    base["qualification"] = dict(
        status="qualified",
        qualification_rule_sha256=closure.canonical_sha256(closure.RULE),
        evidence_receipt_sha256s=sorted([*audits.values(), decision]),
        qualified_by_principal_id=pins["qualifier"]["principal_id"],
        qualified_at=(now - timedelta(seconds=5)).isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    manifest = CapabilityManifestV2.model_validate(base)
    bindings = tuple(
        CapabilityAuditBinding(
            audit_kind=kind,
            capability_manifest_sha256=manifest.manifest_sha256,
            receipt_sha256=digest,
            audit_policy_sha256=closure.expected_capability_audit_policy_sha256(manifest, kind),
            auditor_principal_id=pins["auditor"]["principal_id"],
            valid_from=now - timedelta(seconds=10),
            expires_at=now + timedelta(days=1),
        )
        for kind, digest in audits.items()
    )
    request = NS(
        capability_catalog=NS(manifests=(manifest,)),
        protocol=NS(
            authored_at=now + timedelta(seconds=1),
            authored_by_principal_id="principal:protocol-author",
            independence=NS(
                executor_principal_ids=(),
                parser_principal_ids=(),
                validator_principal_ids=(),
                claim_approver_principal_ids=(),
            ),
            steps=(
                NS(
                    capability_requirement=NS(
                        manifest_sha256=manifest.manifest_sha256, audit_bindings=bindings
                    )
                ),
            ),
        ),
    )
    trust = dict(
        schema_name="aletheia.capability_engineering_source_trust.v1",
        rule_sha256=closure.canonical_sha256(closure.RULE),
        scientific_authority=False,
        **pins,
    )
    runtime = {
        manifest.manifest_sha256: dict(
            adapter_ref="check_impl:execute",
            implementation_path=str(impl),
            implementation_sha256=impl_sha,
            environment_sha256=env_sha,
            environment_source_sha256=env_sha,
            environment_source_path=str(env),
        )
    }
    return NS(
        root=source_root,
        request=request,
        trust=trust,
        runtime=runtime,
        impl=impl,
        audits=audits,
        put=put,
        contract_digests=contract_digests,
        required_sources=required_sources,
    )


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
            "from aletheia.protocols.capability_sources import utc; utc('2026-09-09T00:00:00')",
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
