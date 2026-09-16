"""Shared indirect fixture for capability-source verification tests."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import pytest

from aletheia.execution import capability_sources as closure
from aletheia.protocols.capabilities import CapabilityManifestV2
from aletheia.protocols.schemas import CapabilityAuditBinding, CapabilityAuditKind
from tests.protocols.fixtures import fixture_by_name


@pytest.fixture
def case(tmp_path, request):
    service_operation = getattr(request, "param", None)
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
    service = None
    service_runtime = {}
    service_materials = set()
    provider_artifacts = ()
    if service_operation is not None:
        from aletheia.observations.service_capabilities import local_service_capability_sources

        service = local_service_capability_sources(service_operation)
        contract = service.contract
        contract_payload = closure.canonical_json_bytes(contract)
        service_sha = put(contract_payload)
        service_path = tmp_path / "service-contract.json"
        service_path.write_bytes(contract_payload)
        service_path.chmod(0o400)
        service_materials.update(put(payload) for payload in service.source_bytes.values())
        service_materials.update(
            put(closure.canonical_json_bytes(s)) for s in service.schemas.values()
        )
        service_materials.add(service_sha)
        impl = service.implementation_path
        impl_sha = closure.sha(impl.read_bytes())
        base.update(
            external_action_kind=service_operation,
            operation_id="operation." + service_operation,
            side_effect_class=contract["behavior"]["side_effect_class"],
        )
        base["runtime"].update(
            adapter_ref=contract["adapter_ref"],
            runtime_kind="external_service",
            implementation_sha256=impl_sha,
            determinism=(
                "frozen_seeds"
                if service_operation == "run_cuprate_diagnostic"
                else "declared_stochastic"
            ),
            frozen_seeds=[0] if service_operation == "run_cuprate_diagnostic" else [],
            checkpoint_supported=False,
            reconciliation_supported=service_operation != "run_cuprate_diagnostic",
        )
        base["applicability"].update(
            minimum_batch_size=1,
            maximum_batch_size=1,
            required_condition_sha256s=sorted(
                [*base["applicability"]["required_condition_sha256s"], service_sha]
            ),
        )
        base["retry"] = {"mode": "never", "maximum_attempts_per_scientific_slot": 1}
        for failure in base["failure_modes"]:
            failure["disposition"] = "blocked"
        for direction in ("input", "output"):
            port = copy.deepcopy(base[direction + "_ports"][0])
            spec = contract[direction + "_ports"][0]
            port.update(
                port_id=spec["port_id"], artifact_kind=spec["artifact_kind"], multiplicity="one"
            )
            port["schema_ref"]["schema_sha256"] = contract["schema_sources"][spec["schema"]]
            base[direction + "_ports"] = [port]
        if service_operation == "prepare_validation_campaign":
            provider_artifacts = (
                NS(
                    role=NS(value="provider_receipt"),
                    required=True,
                    schema_sha256=contract["schema_sources"]["committed_campaign"],
                ),
            )
        service_runtime = dict(
            service_operation=service_operation,
            service_contract_sha256=service_sha,
            service_contract_path=str(service_path),
            service_source_paths={n: str(p) for n, p in service.source_paths.items()},
        )
    draft = CapabilityManifestV2.model_validate(base)
    definition = closure.definition_sha256(draft)
    required_sources = closure.contract_sources(source_root, draft) | {impl_sha, env_sha}
    required_sources.update(service_materials)

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
                    role=NS(
                        value=service.contract["behavior"]["role"]
                        if service
                        else "scientific_executor"
                    ),
                    expected_artifacts=provider_artifacts,
                    archived_observation_input=(
                        NS(
                            lookup_input_port_id="input.raw_run_lookup",
                            envelope_output_port_id="intermediate.raw_run",
                            replicate_mapping="same_slot_index",
                        )
                        if service_operation == "load_raw_run"
                        else None
                    ),
                    capability_requirement=NS(
                        manifest_sha256=manifest.manifest_sha256, audit_bindings=bindings
                    ),
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
            adapter_ref=manifest.runtime.adapter_ref,
            implementation_path=str(impl),
            implementation_sha256=impl_sha,
            environment_sha256=env_sha,
            environment_source_sha256=env_sha,
            environment_source_path=str(env),
            **service_runtime,
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
        service=service,
    )
