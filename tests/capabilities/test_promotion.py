"""F10-S7 signed capability-authoring and promotion security tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import aletheia.capabilities as c
import aletheia.capabilities.promotion as promotion_module
from aletheia.coder.executor import SandboxExecution


BASE = datetime(2026, 8, 16, tzinfo=timezone.utc)
IMAGE_ID = "sha256:" + "a" * 64
REPO_ROOT = Path(__file__).resolve().parents[2]


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def private_key(value: str) -> bytes:
    return hashlib.sha256(f"test-only-ed25519:{value}".encode("utf-8")).digest()


_KIND_PERMISSION = {
    c.PromotionArtifactKind.SANDBOX_AUTHORING: c.PromotionPermission.SANDBOX_ATTEST,
    c.PromotionArtifactKind.GENERATED_TEST_SUITE: c.PromotionPermission.TEST_SUITE_ATTEST,
    c.PromotionArtifactKind.INDEPENDENT_VALIDATION: c.PromotionPermission.VALIDATION_ATTEST,
    c.PromotionArtifactKind.DOMAIN_REVIEW: c.PromotionPermission.DOMAIN_REVIEW_ATTEST,
    c.PromotionArtifactKind.PROMOTION_AUDIT: c.PromotionPermission.PROMOTION_AUDIT,
    c.PromotionArtifactKind.REGISTRY_UPDATE: c.PromotionPermission.REGISTRY_PROMOTE,
}


@dataclass(frozen=True)
class PromotionCase:
    source: c.ExperimentCapabilityManifest
    snapshot: c.CapabilityRegistrySnapshot
    policy: c.CapabilityPromotionPolicy
    request: c.CapabilityPromotionRequest
    private_keys: dict[c.PromotionPermission, bytes]
    key_ids: dict[c.PromotionPermission, str]

    def signer(self, permission: c.PromotionPermission) -> dict[str, bytes]:
        return {self.key_ids[permission]: self.private_keys[permission]}


def _source_chain() -> tuple[c.ExperimentCapabilityManifest, ...]:
    names = (
        "materials_band_gap_range_compression_provisional_v1.yaml",
        "materials_band_gap_range_compression_provisional_v2.yaml",
        "materials_band_gap_range_compression_provisional_v2_1.yaml",
    )
    return tuple(
        c.ExperimentCapabilityManifest.model_validate(
            yaml.safe_load((REPO_ROOT / "configs/capabilities" / name).read_text(encoding="utf-8"))
        )
        for name in names
    )


def _trust_policy(
    snapshot: c.CapabilityRegistrySnapshot,
) -> tuple[
    c.CapabilityPromotionPolicy,
    dict[c.PromotionPermission, bytes],
    dict[c.PromotionPermission, str],
]:
    private_keys = {
        permission: private_key(permission.value) for permission in c.PromotionPermission
    }
    keys = {}
    trusted = []
    for permission, secret in private_keys.items():
        public = c.ed25519_public_key_hex(secret)
        key_id = c.ed25519_key_id(public)
        keys[permission] = key_id
        trusted.append(
            c.TrustedPromotionKey(
                key_id=key_id,
                principal_sha256=sha(f"principal:{permission.value}"),
                public_key_ed25519_hex=public,
                domains=("materials",),
                capability_prefixes=("materials.",),
                valid_from=BASE - timedelta(days=1),
                expires_at=BASE + timedelta(days=30),
            )
        )
    roles = tuple(
        sorted(
            (
                c.PromotionRolePolicy(
                    permission=permission,
                    key_ids=(keys[permission],),
                    threshold=1,
                )
                for permission in c.PromotionPermission
            ),
            key=lambda item: item.permission.value,
        )
    )
    policy = c.CapabilityPromotionPolicy(
        policy_id="materials-promotion-policy-2026-08",
        registry_id=snapshot.registry_id,
        source_registry_sha256=snapshot.snapshot_sha256,
        trusted_keys=tuple(sorted(trusted, key=lambda item: item.key_id)),
        roles=roles,
        allowed_sandbox_image_ids=(IMAGE_ID,),
        frozen_at=BASE,
        expires_at=BASE + timedelta(days=14),
    )
    return policy, private_keys, keys


def _sign(
    *,
    policy: c.CapabilityPromotionPolicy,
    kind: c.PromotionArtifactKind,
    artifact_sha256: str,
    source: c.ExperimentCapabilityManifest,
    issued_at: datetime,
    private_keys: dict[c.PromotionPermission, bytes],
    key_ids: dict[c.PromotionPermission, str],
) -> c.SignedPromotionArtifact:
    permission = _KIND_PERMISSION[kind]
    return c.sign_promotion_artifact(
        policy=policy,
        artifact_kind=kind,
        artifact_sha256=artifact_sha256,
        capability_id=source.capability_id,
        domain=source.domain,
        issued_at=issued_at,
        signer_private_keys={key_ids[permission]: private_keys[permission]},
    )


def build_case() -> PromotionCase:
    chain = _source_chain()
    source = chain[-1]
    snapshot = c.build_capability_registry_snapshot(
        registry_id="materials-promotion-test-v1",
        manifests=chain,
        created_at=BASE,
    )
    policy, private_keys, key_ids = _trust_policy(snapshot)
    author = next(
        item.principal_sha256 for item in source.roles if item.role is c.CapabilityRole.EXECUTOR
    )
    authoring = c.build_sandbox_authoring_receipt(
        provisional_manifest=source,
        author_principal_sha256=author,
        source_files={
            "capability.py": ("def build_capability():\n    return {'status': 'provisional'}\n"),
            "runner.py": "print('ALETHEIA_CAPABILITY_AUTHORING_OK')\n",
        },
        source_review_sha256=sha("static-source-review"),
        execution=SandboxExecution(
            0,
            "ALETHEIA_CAPABILITY_AUTHORING_OK\n",
            image_id=IMAGE_ID,
            output_total_bytes=33,
        ),
        success_sentinel="ALETHEIA_CAPABILITY_AUTHORING_OK",
        started_at=BASE + timedelta(hours=1),
        finished_at=BASE + timedelta(hours=1, minutes=5),
    )
    authoring_attestation = _sign(
        policy=policy,
        kind=c.PromotionArtifactKind.SANDBOX_AUTHORING,
        artifact_sha256=authoring.receipt_sha256,
        source=source,
        issued_at=authoring.finished_at,
        private_keys=private_keys,
        key_ids=key_ids,
    )

    test_generator = policy.key(key_ids[c.PromotionPermission.TEST_SUITE_ATTEST]).principal_sha256
    tests = c.GeneratedCapabilityTestSuiteReceipt(
        provisional_manifest_sha256=source.manifest_sha256,
        sandbox_authoring_receipt_sha256=authoring.receipt_sha256,
        test_generator_principal_sha256=test_generator,
        test_suite_sha256=sha("frozen-generated-test-suite"),
        reference_fixtures_sha256=sha("reference-fixtures"),
        adversarial_fixtures_sha256=sha("adversarial-fixtures"),
        positive_control_fixture_sha256=sha("positive-control-fixture"),
        negative_control_fixture_sha256=sha("negative-control-fixture"),
        reference_case_count=4,
        adversarial_case_count=7,
        positive_control_case_count=1,
        negative_control_case_count=1,
        sandbox_image_id=IMAGE_ID,
        frozen_at=BASE + timedelta(hours=2),
    )
    tests_attestation = _sign(
        policy=policy,
        kind=c.PromotionArtifactKind.GENERATED_TEST_SUITE,
        artifact_sha256=tests.receipt_sha256,
        source=source,
        issued_at=tests.frozen_at,
        private_keys=private_keys,
        key_ids=key_ids,
    )

    validator = policy.key(key_ids[c.PromotionPermission.VALIDATION_ATTEST]).principal_sha256
    validator_implementation = sha("independent-validator-implementation")
    validator_binding = c.CapabilityRoleBinding(
        role=c.CapabilityRole.VALIDATOR,
        adapter_ref="tests.capabilities.independent_validator:validate",
        implementation_sha256=validator_implementation,
        principal_sha256=validator,
        runtime=c.CapabilityRuntime.DETERMINISTIC,
        boundary=c.CapabilityBoundary.HARD_SANDBOX,
        allowed_tools=(),
        agent_authored=False,
        frozen_at=BASE + timedelta(hours=2, minutes=5),
    )
    validation = c.IndependentCapabilityValidationReceipt(
        provisional_manifest_sha256=source.manifest_sha256,
        sandbox_authoring_receipt_sha256=authoring.receipt_sha256,
        generated_test_suite_receipt_sha256=tests.receipt_sha256,
        test_suite_sha256=tests.test_suite_sha256,
        validator_principal_sha256=validator,
        validator_implementation_sha256=validator_implementation,
        sandbox_image_id=IMAGE_ID,
        reference_cases_total=tests.reference_case_count,
        reference_cases_passed=tests.reference_case_count,
        adversarial_cases_total=tests.adversarial_case_count,
        adversarial_cases_passed=tests.adversarial_case_count,
        positive_control=c.CapabilityControlExecutionReceipt(
            control_kind=c.ControlKind.POSITIVE,
            fixture_sha256=tests.positive_control_fixture_sha256,
            observed_output_sha256=sha("positive-control-output"),
        ),
        negative_control=c.CapabilityControlExecutionReceipt(
            control_kind=c.ControlKind.NEGATIVE,
            fixture_sha256=tests.negative_control_fixture_sha256,
            observed_output_sha256=sha("negative-control-output"),
        ),
        exact_reexecution_count=source.reproduction_policy.minimum_exact_reexecutions,
        independent_recomputation_receipt_sha256=sha("independent-recomputation"),
        reproduction_policy_evidence_sha256=sha("reproduction-policy-evidence"),
        independent_implementation_verified=False,
        independent_dataset_verified=False,
        started_at=BASE + timedelta(hours=2, minutes=10),
        validated_at=BASE + timedelta(hours=3),
    )
    validation_attestation = _sign(
        policy=policy,
        kind=c.PromotionArtifactKind.INDEPENDENT_VALIDATION,
        artifact_sha256=validation.receipt_sha256,
        source=source,
        issued_at=validation.validated_at,
        private_keys=private_keys,
        key_ids=key_ids,
    )

    reviewer = policy.key(key_ids[c.PromotionPermission.DOMAIN_REVIEW_ATTEST]).principal_sha256
    domain_review = c.DomainCapabilityReviewReceipt(
        provisional_manifest_sha256=source.manifest_sha256,
        independent_validation_receipt_sha256=validation.receipt_sha256,
        reviewer_principal_sha256=reviewer,
        approved_claim_types=tuple(sorted(item.value for item in source.claim_types_supported)),
        approved_maximum_evidence_level=c.CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL,
        safety_review_sha256=sha("independent-safety-review"),
        domain_review_receipt_sha256=sha("independent-domain-review-notes"),
        reviewed_at=BASE + timedelta(hours=4),
    )
    domain_attestation = _sign(
        policy=policy,
        kind=c.PromotionArtifactKind.DOMAIN_REVIEW,
        artifact_sha256=domain_review.receipt_sha256,
        source=source,
        issued_at=domain_review.reviewed_at,
        private_keys=private_keys,
        key_ids=key_ids,
    )
    request = c.CapabilityPromotionRequest(
        request_id="materials-range-compression-promotion-001",
        promotion_policy_sha256=policy.policy_sha256,
        source_registry_sha256=snapshot.snapshot_sha256,
        source_manifest=source,
        source_manifest_sha256=source.manifest_sha256,
        sandbox_authoring=authoring,
        sandbox_attestation=authoring_attestation,
        generated_test_suite=tests,
        test_suite_attestation=tests_attestation,
        independent_validation=validation,
        validation_attestation=validation_attestation,
        domain_review=domain_review,
        domain_review_attestation=domain_attestation,
        independent_validator_binding=validator_binding,
        target_version="2.2.0",
        target_maximum_evidence_level=c.CapabilityEvidenceLevel.CONFIRMATORY_INTERNAL,
        requested_at=BASE + timedelta(hours=4, minutes=10),
    )
    return PromotionCase(
        source=source,
        snapshot=snapshot,
        policy=policy,
        request=request,
        private_keys=private_keys,
        key_ids=key_ids,
    )


def audit(case: PromotionCase) -> c.SignedCapabilityPromotionAudit:
    return c.audit_capability_promotion(
        snapshot=case.snapshot,
        policy=case.policy,
        request=case.request,
        auditor_private_keys=case.signer(c.PromotionPermission.PROMOTION_AUDIT),
        audited_at=BASE + timedelta(hours=5),
    )


def promote(
    case: PromotionCase, signed_audit: c.SignedCapabilityPromotionAudit
) -> c.SignedCapabilityRegistryUpdate:
    return c.promote_capability_registry(
        source_snapshot=case.snapshot,
        policy=case.policy,
        request=case.request,
        signed_audit=signed_audit,
        promoter_private_keys=case.signer(c.PromotionPermission.REGISTRY_PROMOTE),
        promoted_at=BASE + timedelta(hours=6),
    )


def test_complete_provisional_to_registered_upgrade_is_signed_and_append_only():
    case = build_case()
    signed_audit = audit(case)
    assert signed_audit.audit.decision is c.PromotionDecision.APPROVED
    assert not signed_audit.audit.blockers

    update = promote(case, signed_audit)
    verified = c.verify_capability_registry_update(
        update=update,
        source_snapshot=case.snapshot,
        policy=case.policy,
        request=case.request,
        signed_audit=signed_audit,
    )

    assert verified.manifests[:-1] == case.snapshot.manifests
    registered = verified.manifests[-1]
    assert registered.lifecycle is c.CapabilityLifecycle.REGISTERED
    assert registered.version == "2.2.0"
    assert registered.supersedes_manifest_sha256 == case.source.manifest_sha256
    assert registered.registration_evidence is not None
    assert not registered.roles[-1].agent_authored
    assert c.CapabilityRegistry(verified).get(case.source.capability_id) == registered


def test_generated_test_principal_cannot_be_the_validator():
    case = build_case()
    validator = case.request.independent_validation.validator_principal_sha256
    generated = case.request.generated_test_suite.model_copy(
        update={"test_generator_principal_sha256": validator}
    )
    validation = case.request.independent_validation.model_copy(
        update={"generated_test_suite_receipt_sha256": generated.receipt_sha256}
    )
    domain = case.request.domain_review.model_copy(
        update={"independent_validation_receipt_sha256": validation.receipt_sha256}
    )
    generated_attestation = case.request.test_suite_attestation.model_copy(
        update={
            "artifact_sha256": generated.receipt_sha256,
            "signatures": tuple(
                item.model_copy(update={"principal_sha256": validator})
                for item in case.request.test_suite_attestation.signatures
            ),
        }
    )
    validation_attestation = case.request.validation_attestation.model_copy(
        update={"artifact_sha256": validation.receipt_sha256}
    )
    domain_attestation = case.request.domain_review_attestation.model_copy(
        update={"artifact_sha256": domain.receipt_sha256}
    )
    raw = case.request.model_dump(mode="python")
    raw.update(
        {
            "generated_test_suite": generated,
            "test_suite_attestation": generated_attestation,
            "independent_validation": validation,
            "validation_attestation": validation_attestation,
            "domain_review": domain,
            "domain_review_attestation": domain_attestation,
        }
    )
    with pytest.raises(ValidationError, match="test generator, validator"):
        c.CapabilityPromotionRequest.model_validate(raw)


def test_agent_authored_validator_cannot_self_promote():
    case = build_case()
    raw = case.request.model_dump(mode="python")
    raw["independent_validator_binding"]["agent_authored"] = True
    with pytest.raises(ValidationError, match="AI-authored validator"):
        c.CapabilityPromotionRequest.model_validate(raw)


def test_validator_cannot_rebind_generated_control_fixture():
    case = build_case()
    positive = case.request.independent_validation.positive_control.model_copy(
        update={"fixture_sha256": sha("validator-selected-easier-positive")}
    )
    validation = case.request.independent_validation.model_copy(
        update={"positive_control": positive}
    )
    domain = case.request.domain_review.model_copy(
        update={"independent_validation_receipt_sha256": validation.receipt_sha256}
    )
    validation_attestation = case.request.validation_attestation.model_copy(
        update={"artifact_sha256": validation.receipt_sha256}
    )
    domain_attestation = case.request.domain_review_attestation.model_copy(
        update={"artifact_sha256": domain.receipt_sha256}
    )
    raw = case.request.model_dump(mode="python")
    raw.update(
        {
            "independent_validation": validation,
            "validation_attestation": validation_attestation,
            "domain_review": domain,
            "domain_review_attestation": domain_attestation,
        }
    )
    with pytest.raises(ValidationError, match="controls differ"):
        c.CapabilityPromotionRequest.model_validate(raw)


def test_attestation_cannot_predate_the_artifact_it_signs():
    case = build_case()
    raw = case.request.model_dump(mode="python")
    raw["sandbox_attestation"]["issued_at"] = case.request.sandbox_authoring.started_at
    with pytest.raises(ValidationError, match="predates the artifact"):
        c.CapabilityPromotionRequest.model_validate(raw)


def test_forged_test_attestation_produces_rejected_audit_and_no_registry_update():
    case = build_case()
    signature = case.request.test_suite_attestation.signatures[0].model_copy(
        update={"signature_ed25519_hex": "0" * 128}
    )
    forged = case.request.test_suite_attestation.model_copy(update={"signatures": (signature,)})
    request = case.request.model_copy(update={"test_suite_attestation": forged})
    rejected = c.audit_capability_promotion(
        snapshot=case.snapshot,
        policy=case.policy,
        request=request,
        auditor_private_keys=case.signer(c.PromotionPermission.PROMOTION_AUDIT),
        audited_at=BASE + timedelta(hours=5),
    )
    assert rejected.audit.decision is c.PromotionDecision.REJECTED
    assert "test_suite_attestation_invalid" in rejected.audit.blockers
    with pytest.raises(c.CapabilityPromotionError, match="rejected"):
        c.promote_capability_registry(
            source_snapshot=case.snapshot,
            policy=case.policy,
            request=request,
            signed_audit=rejected,
            promoter_private_keys=case.signer(c.PromotionPermission.REGISTRY_PROMOTE),
            promoted_at=BASE + timedelta(hours=6),
        )


def test_signature_from_wrong_permission_is_rejected():
    case = build_case()
    wrong_kind = case.request.test_suite_attestation.model_copy(
        update={"artifact_kind": c.PromotionArtifactKind.INDEPENDENT_VALIDATION}
    )
    with pytest.raises(c.CapabilityPromotionError, match="required permission"):
        c.verify_promotion_artifact(envelope=wrong_kind, policy=case.policy)


def test_signing_key_cannot_escape_its_domain_and_capability_delegation():
    case = build_case()
    with pytest.raises(c.CapabilityPromotionError, match="delegated scope"):
        c.sign_promotion_artifact(
            policy=case.policy,
            artifact_kind=c.PromotionArtifactKind.SANDBOX_AUTHORING,
            artifact_sha256=case.request.sandbox_authoring.receipt_sha256,
            capability_id="biology.untrusted",
            domain="biology",
            issued_at=BASE + timedelta(hours=1),
            signer_private_keys=case.signer(c.PromotionPermission.SANDBOX_ATTEST),
        )


def test_policy_rejects_one_principal_controlling_test_and_validation_roles():
    case = build_case()
    raw = case.policy.model_dump(mode="python")
    test_key_id = case.key_ids[c.PromotionPermission.TEST_SUITE_ATTEST]
    validator_key_id = case.key_ids[c.PromotionPermission.VALIDATION_ATTEST]
    test_principal = case.policy.key(test_key_id).principal_sha256
    for key in raw["trusted_keys"]:
        if key["key_id"] == validator_key_id:
            key["principal_sha256"] = test_principal
    with pytest.raises(ValidationError, match="role-separated principals"):
        c.CapabilityPromotionPolicy.model_validate(raw)


def test_revoked_key_cannot_attest_even_when_it_was_valid_at_policy_freeze():
    case = build_case()
    raw = case.policy.model_dump(mode="python")
    sandbox_key = case.key_ids[c.PromotionPermission.SANDBOX_ATTEST]
    for key in raw["trusted_keys"]:
        if key["key_id"] == sandbox_key:
            key["revoked_at"] = BASE + timedelta(minutes=30)
    policy = c.CapabilityPromotionPolicy.model_validate(raw)
    with pytest.raises(c.CapabilityPromotionError, match="expired, premature, or revoked"):
        c.sign_promotion_artifact(
            policy=policy,
            artifact_kind=c.PromotionArtifactKind.SANDBOX_AUTHORING,
            artifact_sha256=case.request.sandbox_authoring.receipt_sha256,
            capability_id=case.source.capability_id,
            domain=case.source.domain,
            issued_at=BASE + timedelta(hours=1),
            signer_private_keys=case.signer(c.PromotionPermission.SANDBOX_ATTEST),
        )


def test_local_or_mutable_image_authoring_result_cannot_create_receipt():
    case = build_case()
    with pytest.raises(c.CapabilityPromotionError, match="immutable Docker image"):
        c.build_sandbox_authoring_receipt(
            provisional_manifest=case.source,
            author_principal_sha256=case.request.sandbox_authoring.author_principal_sha256,
            source_files={"runner.py": "print('OK')\n"},
            source_review_sha256=sha("review"),
            execution=SandboxExecution(0, "OK\n"),
            success_sentinel="OK",
            started_at=BASE,
            finished_at=BASE + timedelta(seconds=1),
        )


def test_authoring_entry_point_forces_the_production_docker_boundary(monkeypatch):
    case = build_case()
    observed: dict[str, object] = {}

    def fake_execute(files, **kwargs):
        observed["files"] = files
        observed.update(kwargs)
        return SandboxExecution(
            0,
            "ALETHEIA_CAPABILITY_AUTHORING_OK\n",
            image_id=IMAGE_ID,
            output_total_bytes=33,
        )

    timestamps = iter((BASE + timedelta(hours=1), BASE + timedelta(hours=1, minutes=1)))
    monkeypatch.setattr(promotion_module, "execute_python_files", fake_execute)
    monkeypatch.setattr(promotion_module, "_utc_now", lambda: next(timestamps))
    receipt = c.run_provisional_capability_authoring(
        provisional_manifest=case.source,
        author_principal_sha256=case.request.sandbox_authoring.author_principal_sha256,
        source_files={"runner.py": "print('ALETHEIA_CAPABILITY_AUTHORING_OK')\n"},
        script_name="runner.py",
        source_review_sha256=sha("source-review"),
        success_sentinel="ALETHEIA_CAPABILITY_AUTHORING_OK",
        timeout_s=30,
        image_id=IMAGE_ID,
    )
    assert observed["backend"] == "docker"
    assert observed["image_id"] == IMAGE_ID
    assert receipt.boundary is c.CapabilityBoundary.HARD_SANDBOX
    assert receipt.sandbox_image_id == IMAGE_ID


def test_registry_signature_tamper_and_source_rollback_fail_closed():
    case = build_case()
    signed_audit = audit(case)
    update = promote(case, signed_audit)
    signature = update.registry_attestation.signatures[0].model_copy(
        update={"signature_ed25519_hex": "f" * 128}
    )
    tampered = update.model_copy(
        update={
            "registry_attestation": update.registry_attestation.model_copy(
                update={"signatures": (signature,)}
            )
        }
    )
    with pytest.raises(c.CapabilityPromotionError, match="signature is invalid"):
        c.verify_capability_registry_update(
            update=tampered,
            source_snapshot=case.snapshot,
            policy=case.policy,
            request=case.request,
            signed_audit=signed_audit,
        )

    rolled_back_source = c.build_capability_registry_snapshot(
        registry_id=case.snapshot.registry_id,
        manifests=case.snapshot.manifests,
        created_at=case.snapshot.created_at + timedelta(seconds=1),
    )
    with pytest.raises(c.CapabilityPromotionError, match="source, request, audit, or policy"):
        c.verify_capability_registry_update(
            update=update,
            source_snapshot=rolled_back_source,
            policy=case.policy,
            request=case.request,
            signed_audit=signed_audit,
        )


def test_stale_source_policy_cannot_win_a_second_concurrent_promotion():
    case = build_case()
    signed_audit = audit(case)
    first = promote(case, signed_audit)
    with pytest.raises(c.CapabilityPromotionError, match="rejected"):
        c.promote_capability_registry(
            source_snapshot=first.target_snapshot,
            policy=case.policy,
            request=case.request,
            signed_audit=signed_audit,
            promoter_private_keys=case.signer(c.PromotionPermission.REGISTRY_PROMOTE),
            promoted_at=BASE + timedelta(hours=7),
        )


def test_promoter_rejects_a_signed_audit_that_omits_required_checks():
    case = build_case()
    signed_audit = audit(case)
    incomplete_audit = signed_audit.audit.model_copy(
        update={"checks": signed_audit.audit.checks[:-1]}
    )
    incomplete = signed_audit.model_copy(
        update={
            "audit": incomplete_audit,
            "attestation": signed_audit.attestation.model_copy(
                update={"artifact_sha256": incomplete_audit.receipt_sha256}
            ),
        }
    )
    with pytest.raises(c.CapabilityPromotionError, match="omits required checks"):
        promote(case, incomplete)


def test_readiness_audit_does_not_invent_missing_independence_or_signatures():
    case = build_case()
    before = c.build_capability_promotion_readiness_audit(
        audit_id="f10-s7-before",
        registry=case.snapshot,
        audited_at=BASE + timedelta(hours=4),
    )
    assert not before.production_promotion_ready
    assert before.registered_capability_count == 0
    assert before.candidates[0].validator_agent_authored
    assert "validator_is_agent_authored" in before.candidates[0].blockers

    signed_audit = audit(case)
    update = promote(case, signed_audit)
    after = c.build_capability_promotion_readiness_audit(
        audit_id="f10-s7-after",
        registry=update.target_snapshot,
        audited_at=BASE + timedelta(hours=7),
    )
    assert after.production_promotion_ready
    assert after.registered_capability_count == 1
    assert not after.candidates[0].blockers


def test_committed_materials_readiness_audit_exactly_reproduces_from_manifests():
    names = (
        "materials_band_gap_range_compression_provisional_v1.yaml",
        "materials_band_gap_range_compression_provisional_v2.yaml",
        "materials_band_gap_range_compression_provisional_v2_1.yaml",
        "materials_ase_emt_eos_reference_provisional_v1.yaml",
    )
    manifests = tuple(
        c.ExperimentCapabilityManifest.model_validate(
            yaml.safe_load((REPO_ROOT / "configs/capabilities" / name).read_text(encoding="utf-8"))
        )
        for name in names
    )
    registry = c.build_capability_registry_snapshot(
        registry_id="materials-capabilities-v4",
        manifests=manifests,
        created_at=datetime(2026, 8, 15, 9, 39, 9, 739376, tzinfo=timezone.utc),
    )
    assert registry.snapshot_sha256 == (
        "80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77"
    )
    actual = c.build_capability_promotion_readiness_audit(
        audit_id="f10-s7-materials-promotion-readiness-v1",
        registry=registry,
        audited_at=datetime(2026, 8, 16, 6, tzinfo=timezone.utc),
    )
    expected = c.CapabilityPromotionReadinessAudit.model_validate_json(
        (REPO_ROOT / "configs/capabilities/f10_promotion_readiness_audit_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert actual == expected
    assert actual.audit_sha256 == (
        "b1017ae5e7cbb8ffb7628ec9b0ce12a11bd060d272518e69b6d3a3a6f0dad9c0"
    )


def test_cli_audit_promote_and_verify_uses_owner_only_key_files(tmp_path):
    case = build_case()

    def write_model(name: str, value: object) -> Path:
        path = tmp_path / name
        payload = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    registry = write_model("registry.json", case.snapshot)
    policy = write_model("policy.json", case.policy)
    request = write_model("request.json", case.request)
    auditor_key = tmp_path / "auditor.key"
    auditor_key.write_bytes(case.private_keys[c.PromotionPermission.PROMOTION_AUDIT])
    auditor_key.chmod(0o600)
    promoter_key = tmp_path / "promoter.key"
    promoter_key.write_bytes(case.private_keys[c.PromotionPermission.REGISTRY_PROMOTE])
    promoter_key.chmod(0o600)
    audit_path = tmp_path / "audit.json"
    update_path = tmp_path / "update.json"
    script = str(REPO_ROOT / "scripts/capability_promotion.py")

    audit_command = [
        sys.executable,
        script,
        "audit",
        "--registry",
        str(registry),
        "--policy",
        str(policy),
        "--request",
        str(request),
        "--auditor-key",
        f"{case.key_ids[c.PromotionPermission.PROMOTION_AUDIT]}={auditor_key}",
        "--audited-at",
        (BASE + timedelta(hours=5)).isoformat(),
        "--output",
        str(audit_path),
    ]
    audited = subprocess.run(
        audit_command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert audited.returncode == 0, audited.stderr
    assert json.loads(audited.stdout)["decision"] == "approved"
    assert audit_path.stat().st_mode & 0o777 == 0o600
    frozen_audit = audit_path.read_bytes()
    duplicate = subprocess.run(
        audit_command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert duplicate.returncode != 0
    assert "refusing to replace frozen promotion artifact" in duplicate.stderr
    assert audit_path.read_bytes() == frozen_audit

    promoted = subprocess.run(
        [
            sys.executable,
            script,
            "promote",
            "--registry",
            str(registry),
            "--policy",
            str(policy),
            "--request",
            str(request),
            "--audit",
            str(audit_path),
            "--promoter-key",
            f"{case.key_ids[c.PromotionPermission.REGISTRY_PROMOTE]}={promoter_key}",
            "--promoted-at",
            (BASE + timedelta(hours=6)).isoformat(),
            "--output",
            str(update_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert promoted.returncode == 0, promoted.stderr
    assert update_path.stat().st_mode & 0o777 == 0o600

    verified = subprocess.run(
        [
            sys.executable,
            script,
            "verify",
            "--registry",
            str(registry),
            "--policy",
            str(policy),
            "--request",
            str(request),
            "--audit",
            str(audit_path),
            "--update",
            str(update_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["verified"] is True


def test_cli_rejects_group_readable_private_key(tmp_path):
    case = build_case()
    registry = tmp_path / "registry.json"
    registry.write_text(case.snapshot.model_dump_json(), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(case.policy.model_dump_json(), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(case.request.model_dump_json(), encoding="utf-8")
    key = tmp_path / "auditor.key"
    key.write_bytes(case.private_keys[c.PromotionPermission.PROMOTION_AUDIT])
    key.chmod(0o640)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/capability_promotion.py"),
            "audit",
            "--registry",
            str(registry),
            "--policy",
            str(policy),
            "--request",
            str(request),
            "--auditor-key",
            f"{case.key_ids[c.PromotionPermission.PROMOTION_AUDIT]}={key}",
            "--audited-at",
            (BASE + timedelta(hours=5)).isoformat(),
            "--output",
            str(tmp_path / "forbidden.json"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "group/world accessible" in result.stderr
    assert not (tmp_path / "forbidden.json").exists()


def test_cli_rejects_private_key_symlink(tmp_path):
    case = build_case()
    registry = tmp_path / "registry.json"
    registry.write_text(case.snapshot.model_dump_json(), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(case.policy.model_dump_json(), encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(case.request.model_dump_json(), encoding="utf-8")
    real_key = tmp_path / "real-auditor.key"
    real_key.write_bytes(case.private_keys[c.PromotionPermission.PROMOTION_AUDIT])
    real_key.chmod(0o600)
    linked_key = tmp_path / "linked-auditor.key"
    linked_key.symlink_to(real_key)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/capability_promotion.py"),
            "audit",
            "--registry",
            str(registry),
            "--policy",
            str(policy),
            "--request",
            str(request),
            "--auditor-key",
            f"{case.key_ids[c.PromotionPermission.PROMOTION_AUDIT]}={linked_key}",
            "--audited-at",
            (BASE + timedelta(hours=5)).isoformat(),
            "--output",
            str(tmp_path / "forbidden-symlink.json"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot be a symlink" in result.stderr
    assert not (tmp_path / "forbidden-symlink.json").exists()
