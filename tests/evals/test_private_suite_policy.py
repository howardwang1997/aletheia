"""F7 issue 10 private-suite custody, one-time access, contamination, and retirement."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import aletheia.evals.private_suite as private_suite_module
from aletheia.evals.baselines import (
    AgentScaffold,
    BaselineAnalysisPolicy,
    BaselineArm,
    BaselineArmId,
    BaselineMatrixPlan,
    MatrixPhase,
    build_baseline_run_plans,
)
from aletheia.evals.ledger import EvaluationLedger
from aletheia.evals.private_suite import (
    ContaminationRisk,
    ContaminationSeverity,
    ContaminationSource,
    EncryptedAssetEnvelope,
    EncryptedAssetRole,
    PrivateContaminationAssessment,
    PrivateContaminationReport,
    PrivateCustodyLedger,
    PrivateDomainReview,
    PrivateSourceRecord,
    PrivateRetirementRecord,
    PrivateSuiteAccessAuthorization,
    PrivateSuiteAccessGuard,
    PrivateSuiteManifest,
    PrivateSuitePolicyError,
    PrivateSuiteTier,
    PrivateTaskCase,
    PrivateTaskCustodyRecord,
    close_private_suite_access,
    fail_private_suite_materialization,
    load_materialized_private_suite,
    materialize_private_suite,
)
from aletheia.evals.runner import (
    EvaluationAccessRevokedError,
    EvaluationRunnerError,
    IndependentEvaluationRunner,
)
from aletheia.evals.schemas import (
    ArtifactRequirement,
    AttemptStatus,
    ContaminationPolicy,
    EvaluationAttemptSlot,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    EvalLayer,
    InvalidReason,
    ResourceBudget,
)

from .f7s2_fixtures import (
    EVALUATOR_HASH,
    SCORER_HASH,
    SIGNING_KEY,
    ExactAnswerScorer,
    HardExecutor,
    write_submission,
)

BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)
CUSTODY_OWNER = "a" * 64
AUDITOR = "b" * 64
RESEARCH = "c" * 64
EVALUATOR_PRINCIPAL = "d" * 64


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


class MemoryCiphertextStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read_ciphertext(self, storage_ref: str) -> bytes:
        return self.values[storage_ref]


class ReversingEnvelopeDecryptor:
    prefix = b"sealed-v1\0"

    def decrypt(self, _envelope, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(self.prefix):
            raise ValueError("not a test envelope")
        return ciphertext[len(self.prefix) :][::-1]


def _envelope(
    store: MemoryCiphertextStore,
    *,
    role: EncryptedAssetRole,
    asset_id: str,
    plaintext: bytes,
    key_digit: str,
    policy_digit: str,
) -> EncryptedAssetEnvelope:
    prefix = {
        EncryptedAssetRole.SUITE_MANIFEST: "custody://suite-manifests/",
        EncryptedAssetRole.TASK_MANIFEST: "custody://task-manifests/",
        EncryptedAssetRole.HIDDEN_ASSET: "custody://hidden-assets/",
        EncryptedAssetRole.GOLD_EVIDENCE: "custody://gold-evidence/",
    }[role]
    storage_ref = f"{prefix}{asset_id}.age"
    ciphertext = ReversingEnvelopeDecryptor.prefix + plaintext[::-1]
    store.values[storage_ref] = ciphertext
    return EncryptedAssetEnvelope(
        asset_id=asset_id,
        role=role,
        storage_ref=storage_ref,
        ciphertext_sha256=_sha(ciphertext),
        ciphertext_bytes=len(ciphertext),
        plaintext_sha256=_sha(plaintext),
        plaintext_bytes=len(plaintext),
        encryption_scheme="age-x25519-v1",
        key_id_sha256=key_digit * 64,
        access_policy_sha256=policy_digit * 64,
    )


def _arms() -> tuple[BaselineArm, ...]:
    shared = {
        "base_model_manifest_sha256": "1" * 64,
        "tool_policy_sha256": "2" * 64,
        "budget_policy_sha256": "3" * 64,
        "wall_time_policy_sha256": "4" * 64,
        "tool_names": (),
    }
    return (
        BaselineArm(
            arm_id=BaselineArmId.DIRECT_MODEL,
            system_manifest_sha256="5" * 64,
            agent_scaffold=AgentScaffold.DIRECT,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="6" * 64,
            description="Direct model private-test arm.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.GENERIC_AGENT,
            system_manifest_sha256="6" * 64,
            agent_scaffold=AgentScaffold.GENERIC,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="7" * 64,
            description="Generic agent private-test arm.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.ALETHEIA_NO_K2,
            system_manifest_sha256="7" * 64,
            agent_scaffold=AgentScaffold.ALETHEIA,
            campaign_learning_enabled=False,
            k2_enabled=False,
            prompt_manifest_sha256="8" * 64,
            description="Aletheia no-K2 private-test arm.",
            **shared,
        ),
        BaselineArm(
            arm_id=BaselineArmId.ALETHEIA_FULL_K2,
            system_manifest_sha256="8" * 64,
            agent_scaffold=AgentScaffold.ALETHEIA,
            campaign_learning_enabled=True,
            k2_enabled=True,
            prompt_manifest_sha256="9" * 64,
            description="Aletheia full-K2 private-test arm.",
            **shared,
        ),
    )


@dataclass
class PrivateCase:
    root: Path
    store: MemoryCiphertextStore
    decryptor: ReversingEnvelopeDecryptor
    task: EvaluationTask
    suite: EvaluationSuite
    matrix: BaselineMatrixPlan
    manifest: PrivateSuiteManifest
    authorization: PrivateSuiteAccessAuthorization
    custody: PrivateCustodyLedger

    @property
    def plans(self):
        return build_baseline_run_plans(self.matrix, self.suite)


def _build_case(tmp_path: Path) -> PrivateCase:
    root = tmp_path / "evaluator"
    hidden = b'{"answer":"42"}'
    task = EvaluationTask(
        task_id="opaque-private-eval-task",
        version="1.0.0",
        layer=EvalLayer.PRIVATE_PROSPECTIVE,
        public_prompt="Determine the supported conclusion from the supplied private observations.",
        hidden_asset_ref="evaluator://hidden/private/private-pilot-v1/task-001.json",
        hidden_asset_sha256=_sha(hidden),
        resource_budget=ResourceBudget(wall_time_s=10, cpu_seconds=5, memory_mb=128),
        expected_artifacts=(
            ArtifactRequirement(kind="answer", media_type="application/json", max_bytes=1024),
        ),
        scorer_ref="evaluator://scorers/private-exact-v1",
        scorer_sha256=SCORER_HASH,
        contamination_policy=ContaminationPolicy(
            corpus_cutoff=BASE,
            forbidden_sources=("private suite gold evidence",),
            test_access_limit=5,
            retire_after_access=True,
        ),
    )
    suite = EvaluationSuite(
        suite_id="private-evaluation-suite-v1",
        version="1.0.0",
        task_manifest_sha256s=(task.manifest_sha256,),
        scoring_policy_sha256="e" * 64,
    )
    slots = tuple(
        EvaluationAttemptSlot(
            task_manifest_sha256=task.manifest_sha256,
            repeat_index=index,
            seed=9100 + index,
        )
        for index in range(5)
    )
    matrix = BaselineMatrixPlan(
        matrix_id="private-frontier-test-matrix-v1",
        suite_manifest_sha256=suite.manifest_sha256,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        phase=MatrixPhase.TEST,
        parent_validation_matrix_sha256="f" * 64,
        arms=_arms(),
        slots=slots,
        block_randomization_seed=771,
        analysis=BaselineAnalysisPolicy(bootstrap_resamples=100, bootstrap_seed=772),
        frozen_at=BASE + timedelta(days=3),
    )

    store = MemoryCiphertextStore()
    suite_raw = suite.model_dump_json(exclude_none=True).encode()
    task_raw = task.model_dump_json(exclude_none=True).encode()
    gold = b'{"acceptable_conclusions":["supported","not_supported"]}'
    suite_envelope = _envelope(
        store,
        role=EncryptedAssetRole.SUITE_MANIFEST,
        asset_id="private-pilot-v1-suite",
        plaintext=suite_raw,
        key_digit="1",
        policy_digit="5",
    )
    task_envelope = _envelope(
        store,
        role=EncryptedAssetRole.TASK_MANIFEST,
        asset_id="private-pilot-v1-task-001",
        plaintext=task_raw,
        key_digit="2",
        policy_digit="6",
    )
    hidden_envelope = _envelope(
        store,
        role=EncryptedAssetRole.HIDDEN_ASSET,
        asset_id="private-pilot-v1-hidden-001",
        plaintext=hidden,
        key_digit="3",
        policy_digit="7",
    )
    gold_envelope = _envelope(
        store,
        role=EncryptedAssetRole.GOLD_EVIDENCE,
        asset_id="private-pilot-v1-gold-001",
        plaintext=gold,
        key_digit="4",
        policy_digit="8",
    )
    record = PrivateTaskCustodyRecord(
        private_task_id="private-task-001",
        evaluation_task_manifest_sha256=task.manifest_sha256,
        domain="materials",
        case_type=PrivateTaskCase.TRUE_EFFECT,
        structural_family_sha256="9" * 64,
        validation_analog_task_manifest_sha256="0" * 64,
        source=PrivateSourceRecord(
            source_type="commissioned_synthetic",
            provenance_sha256="1" * 64,
            license_id="private-evaluation-contract-v1",
            license_terms_sha256="2" * 64,
            retention_deadline=BASE + timedelta(days=730),
        ),
        review=PrivateDomainReview(
            reviewer_principal_sha256="3" * 64,
            expertise_record_sha256="4" * 64,
            conflict_check_sha256="5" * 64,
            gold_evidence_sha256=_sha(gold),
            acceptable_conclusions_sha256="6" * 64,
            reviewed_at=BASE + timedelta(days=2),
        ),
        contamination=PrivateContaminationAssessment(
            task_created_at=BASE + timedelta(days=1),
            prospective_after=BASE,
            assessed_at=BASE + timedelta(days=2),
            assessor_principal_sha256="7" * 64,
            risk=ContaminationRisk.LOW,
            similarity_audit_sha256="8" * 64,
        ),
        task_manifest_envelope=task_envelope,
        hidden_asset_envelope=hidden_envelope,
        gold_evidence_envelope=gold_envelope,
        scheduled_retire_at=BASE + timedelta(days=365),
    )
    manifest = PrivateSuiteManifest(
        suite_id="private-pilot-v1",
        version="1.0.0",
        tier=PrivateSuiteTier.PILOT,
        evaluation_suite_manifest_sha256=suite.manifest_sha256,
        evaluation_suite_envelope=suite_envelope,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        baseline_matrix_manifest_sha256=matrix.manifest_sha256,
        acceptance_config_sha256="a" * 64,
        custody_owner_principal_sha256=CUSTODY_OWNER,
        independent_auditor_principal_sha256=AUDITOR,
        research_principal_sha256=RESEARCH,
        tasks=(record,),
        frozen_at=BASE + timedelta(days=3),
    )
    plans = build_baseline_run_plans(matrix, suite)
    authorization = PrivateSuiteAccessAuthorization(
        authorization_id="private-test-access-v1",
        private_suite_manifest_sha256=manifest.manifest_sha256,
        evaluation_suite_manifest_sha256=suite.manifest_sha256,
        evaluator_manifest_sha256=EVALUATOR_HASH,
        baseline_matrix_manifest_sha256=matrix.manifest_sha256,
        acceptance_config_sha256=manifest.acceptance_config_sha256,
        allowed_run_plan_sha256s=tuple(item.run_plan.manifest_sha256 for item in plans),
        custody_approver_principal_sha256=CUSTODY_OWNER,
        independent_approver_principal_sha256=AUDITOR,
        custody_approval_evidence_sha256="d" * 64,
        independent_approval_evidence_sha256="e" * 64,
        authorized_at=BASE + timedelta(days=4),
        expires_at=BASE + timedelta(days=7),
    )
    return PrivateCase(
        root=root,
        store=store,
        decryptor=ReversingEnvelopeDecryptor(),
        task=task,
        suite=suite,
        matrix=matrix,
        manifest=manifest,
        authorization=authorization,
        custody=PrivateCustodyLedger(root / "custody" / "events.jsonl"),
    )


def _register_and_authorize(case: PrivateCase) -> None:
    case.custody.register_suite(case.manifest)
    case.custody.authorize_access(case.manifest, case.authorization)


def _materialize(case: PrivateCase):
    _register_and_authorize(case)
    return materialize_private_suite(
        manifest=case.manifest,
        authorization=case.authorization,
        baseline_matrix=case.matrix,
        ledger=case.custody,
        store=case.store,
        decryptor=case.decryptor,
        evaluator_root=case.root,
        opened_at=BASE + timedelta(days=5),
    )


def _guard(case: PrivateCase) -> PrivateSuiteAccessGuard:
    return PrivateSuiteAccessGuard(
        manifest=case.manifest,
        ledger=case.custody,
        authorization_id=case.authorization.authorization_id,
        evaluator_principal_sha256=EVALUATOR_PRINCIPAL,
        evaluator_root=case.root,
        clock=lambda: BASE + timedelta(days=5),
    )


def _runner(case: PrivateCase, *, action=write_submission, scorer=None, guard=True):
    return IndependentEvaluationRunner(
        root=case.root,
        ledger=EvaluationLedger(case.root / "evaluator_ledger" / "events.jsonl"),
        executor=HardExecutor(action),
        scorer=scorer or ExactAnswerScorer(),
        evaluator_manifest_sha256=EVALUATOR_HASH,
        receipt_key_id="private-test-key",
        receipt_signing_key=SIGNING_KEY,
        custody_guard=_guard(case) if guard else None,
    )


def test_registry_contains_only_ciphertext_identities_and_separates_keys(tmp_path):
    case = _build_case(tmp_path)
    serialized = json.dumps(case.manifest.model_dump(mode="json"), sort_keys=True)
    assert case.task.public_prompt not in serialized
    assert '"answer":"42"' not in serialized
    record = case.manifest.tasks[0]
    assert (
        len(
            {
                record.task_manifest_envelope.key_id_sha256,
                record.hidden_asset_envelope.key_id_sha256,
                record.gold_evidence_envelope.key_id_sha256,
            }
        )
        == 3
    )

    raw = record.model_dump()
    raw["hidden_asset_envelope"] = record.hidden_asset_envelope.model_copy(
        update={"key_id_sha256": record.task_manifest_envelope.key_id_sha256}
    )
    with pytest.raises(ValidationError, match="separate key identities"):
        PrivateTaskCustodyRecord.model_validate(raw)


def test_frontier_gate_schema_requires_ten_tasks_two_domains_and_all_case_types(tmp_path):
    case = _build_case(tmp_path)
    base = case.manifest.tasks[0]
    records = []
    case_types = list(PrivateTaskCase)
    for index in range(10):
        raw = base.model_dump()
        raw["private_task_id"] = f"private-task-{index:03d}"
        raw["evaluation_task_manifest_sha256"] = f"{1000 + index:064x}"
        raw["domain"] = "materials" if index < 5 else "molecules"
        raw["case_type"] = case_types[index % len(case_types)]
        raw["structural_family_sha256"] = f"{2000 + index:064x}"
        raw["validation_analog_task_manifest_sha256"] = f"{3000 + index:064x}"
        for field, role_name in (
            ("task_manifest_envelope", "task"),
            ("hidden_asset_envelope", "hidden"),
            ("gold_evidence_envelope", "gold"),
        ):
            envelope = getattr(base, field)
            raw[field] = envelope.model_copy(
                update={
                    "asset_id": f"formal-{role_name}-{index:03d}",
                    "storage_ref": envelope.storage_ref.rsplit("/", 1)[0]
                    + f"/formal-{role_name}-{index:03d}.age",
                }
            )
        records.append(PrivateTaskCustodyRecord.model_validate(raw))

    raw_manifest = case.manifest.model_dump()
    raw_manifest["tier"] = PrivateSuiteTier.FRONTIER_GATE
    raw_manifest["tasks"] = tuple(records)
    formal = PrivateSuiteManifest.model_validate(raw_manifest)
    assert len(formal.tasks) == 10
    assert {task.case_type for task in formal.tasks} == set(PrivateTaskCase)

    raw_manifest["tasks"] = tuple(records[:9])
    with pytest.raises(ValidationError, match="10–20"):
        PrivateSuiteManifest.model_validate(raw_manifest)


def test_authorization_is_two_person_and_cannot_change_frozen_test_config(tmp_path):
    case = _build_case(tmp_path)
    case.custody.register_suite(case.manifest)
    too_long = case.authorization.model_copy(
        update={"expires_at": case.authorization.authorized_at + timedelta(hours=73)}
    )
    with pytest.raises(PrivateSuitePolicyError, match="frozen TTL"):
        case.custody.authorize_access(case.manifest, too_long)
    wrong = case.authorization.model_copy(update={"acceptance_config_sha256": "b" * 64})
    with pytest.raises(PrivateSuitePolicyError, match="differs from frozen"):
        case.custody.authorize_access(case.manifest, wrong)

    case.custody.authorize_access(case.manifest, case.authorization)
    replacement = case.authorization.model_copy(
        update={
            "authorization_id": "replacement-access-v1",
            "allowed_run_plan_sha256s": tuple(
                reversed(case.authorization.allowed_run_plan_sha256s)
            ),
        }
    )
    with pytest.raises(PrivateSuitePolicyError, match="cannot change"):
        case.custody.authorize_access(case.manifest, replacement)


def test_concurrent_one_time_open_has_exactly_one_winner(tmp_path):
    case = _build_case(tmp_path)
    _register_and_authorize(case)
    barrier = threading.Barrier(2)
    winners = []
    failures = []

    def open_once():
        barrier.wait()
        try:
            winners.append(
                case.custody.open_access(
                    case.manifest,
                    case.authorization.authorization_id,
                    opened_at=BASE + timedelta(days=5),
                )
            )
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=open_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(failures) == 1
    assert "already been opened" in str(failures[0])


def test_materialization_verifies_envelopes_stages_mode_0400_and_cannot_reopen(tmp_path):
    case = _build_case(tmp_path)
    materialized = _materialize(case)
    assert materialized.suite == case.suite
    assert materialized.tasks == (case.task,)
    state = case.custody.state(case.manifest)
    assert state.materialization_receipt_sha256 == materialized.receipt.receipt_sha256

    suite_path = case.root / "private_manifests/private-pilot-v1/suite.v1.json"
    hidden_path = case.root / "hidden_assets/private/private-pilot-v1/task-001.json"
    assert stat.S_IMODE(suite_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(hidden_path.stat().st_mode) == 0o400
    assert hidden_path.read_bytes() == b'{"answer":"42"}'
    assert (
        load_materialized_private_suite(
            manifest=case.manifest,
            ledger=case.custody,
            access_id=case.authorization.authorization_id,
            evaluator_root=case.root,
        )
        == materialized
    )

    with pytest.raises(PrivateSuitePolicyError, match="already been opened"):
        materialize_private_suite(
            manifest=case.manifest,
            authorization=case.authorization,
            baseline_matrix=case.matrix,
            ledger=case.custody,
            store=case.store,
            decryptor=case.decryptor,
            evaluator_root=case.root,
            opened_at=BASE + timedelta(days=5),
        )


def test_close_disposes_exact_plaintext_scope_and_is_idempotent(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)
    receipt = close_private_suite_access(
        manifest=case.manifest,
        ledger=case.custody,
        access_id=case.authorization.authorization_id,
        evaluator_root=case.root,
        closed_at=BASE + timedelta(days=6),
    )
    assert receipt.expected_file_count == 3
    assert receipt.removed_file_count == 3
    assert not (case.root / "private_manifests/private-pilot-v1/suite.v1.json").exists()
    assert not (case.root / "hidden_assets/private/private-pilot-v1/task-001.json").exists()
    state = case.custody.state(case.manifest)
    assert state.access_closed is True
    assert state.suite_retired is True
    assert state.cleanup_receipt_sha256 == receipt.receipt_sha256
    assert (
        close_private_suite_access(
            manifest=case.manifest,
            ledger=case.custody,
            access_id=case.authorization.authorization_id,
            evaluator_root=case.root,
        )
        == receipt
    )


def test_partial_materialization_failure_removes_already_written_plaintext(tmp_path, monkeypatch):
    case = _build_case(tmp_path)
    _register_and_authorize(case)
    original = private_suite_module._write_new_private_file
    calls = 0

    def fail_second_write(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated evaluator storage failure")
        original(path, data)

    monkeypatch.setattr(private_suite_module, "_write_new_private_file", fail_second_write)
    with pytest.raises(PrivateSuitePolicyError, match="materialization failed"):
        materialize_private_suite(
            manifest=case.manifest,
            authorization=case.authorization,
            baseline_matrix=case.matrix,
            ledger=case.custody,
            store=case.store,
            decryptor=case.decryptor,
            evaluator_root=case.root,
            opened_at=BASE + timedelta(days=5),
        )
    assert not (case.root / "private_manifests/private-pilot-v1/suite.v1.json").exists()
    state = case.custody.state(case.manifest)
    assert state.materialization_failed is True
    assert state.access_closed is True


def test_preexisting_plaintext_scope_fails_before_open_without_deleting_it(tmp_path):
    case = _build_case(tmp_path)
    _register_and_authorize(case)
    marker = case.root / "hidden_assets/private/private-pilot-v1/unrelated.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("operator data", encoding="utf-8")
    with pytest.raises(PrivateSuitePolicyError, match="scopes must be absent"):
        materialize_private_suite(
            manifest=case.manifest,
            authorization=case.authorization,
            baseline_matrix=case.matrix,
            ledger=case.custody,
            store=case.store,
            decryptor=case.decryptor,
            evaluator_root=case.root,
            opened_at=BASE + timedelta(days=5),
        )
    assert case.custody.state(case.manifest).opened_access_id is None
    assert marker.read_text(encoding="utf-8") == "operator data"

    escaped = _build_case(tmp_path / "escaped")
    _register_and_authorize(escaped)
    outside = tmp_path / "outside-hidden"
    outside.mkdir()
    (escaped.root / "hidden_assets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PrivateSuitePolicyError, match="scope escaped"):
        materialize_private_suite(
            manifest=escaped.manifest,
            authorization=escaped.authorization,
            baseline_matrix=escaped.matrix,
            ledger=escaped.custody,
            store=escaped.store,
            decryptor=escaped.decryptor,
            evaluator_root=escaped.root,
            opened_at=BASE + timedelta(days=5),
        )
    assert escaped.custody.state(escaped.manifest).opened_access_id is None
    assert list(outside.iterdir()) == []


def test_crash_after_open_and_plaintext_writes_is_recoverable_and_terminal(tmp_path):
    case = _build_case(tmp_path)
    _register_and_authorize(case)
    case.custody.open_access(
        case.manifest,
        case.authorization.authorization_id,
        opened_at=BASE + timedelta(days=5),
    )
    suite_raw = case.decryptor.decrypt(
        case.manifest.evaluation_suite_envelope,
        case.store.values[case.manifest.evaluation_suite_envelope.storage_ref],
    )
    record = case.manifest.tasks[0]
    task_raw = case.decryptor.decrypt(
        record.task_manifest_envelope,
        case.store.values[record.task_manifest_envelope.storage_ref],
    )
    hidden_raw = case.decryptor.decrypt(
        record.hidden_asset_envelope,
        case.store.values[record.hidden_asset_envelope.storage_ref],
    )
    paths = (
        case.root / "private_manifests/private-pilot-v1/suite.v1.json",
        case.root / "private_manifests/private-pilot-v1/private-task-001.task.v1.json",
        case.root / "hidden_assets/private/private-pilot-v1/task-001.json",
    )
    for path, raw in zip(paths, (suite_raw, task_raw, hidden_raw), strict=True):
        private_suite_module._write_new_private_file(path, raw)

    receipt = fail_private_suite_materialization(
        manifest=case.manifest,
        ledger=case.custody,
        access_id=case.authorization.authorization_id,
        evaluator_root=case.root,
        error_evidence_sha256="f" * 64,
        failed_at=BASE + timedelta(days=5, minutes=1),
    )
    assert receipt.removed_file_count == 3
    assert all(not path.exists() for path in paths)
    state = case.custody.state(case.manifest)
    assert state.materialization_failed is True
    assert state.access_closed is True
    assert state.suite_retired is True
    assert (
        fail_private_suite_materialization(
            manifest=case.manifest,
            ledger=case.custody,
            access_id=case.authorization.authorization_id,
            evaluator_root=case.root,
            error_evidence_sha256="f" * 64,
        )
        == receipt
    )


def test_ciphertext_failure_does_not_open_but_decrypt_failure_consumes_access(tmp_path):
    case = _build_case(tmp_path / "ciphertext")
    _register_and_authorize(case)
    ref = case.manifest.tasks[0].hidden_asset_envelope.storage_ref
    original = case.store.values[ref]
    case.store.values[ref] = b"x" * len(original)
    with pytest.raises(PrivateSuitePolicyError, match="ciphertext hash"):
        materialize_private_suite(
            manifest=case.manifest,
            authorization=case.authorization,
            baseline_matrix=case.matrix,
            ledger=case.custody,
            store=case.store,
            decryptor=case.decryptor,
            evaluator_root=case.root,
            opened_at=BASE + timedelta(days=5),
        )
    assert case.custody.state(case.manifest).opened_access_id is None

    failed = _build_case(tmp_path / "plaintext")
    _register_and_authorize(failed)

    class BadDecryptor:
        def decrypt(self, envelope, ciphertext):
            value = ReversingEnvelopeDecryptor().decrypt(envelope, ciphertext)
            return b"x" * len(value)

    with pytest.raises(PrivateSuitePolicyError, match="plaintext hash"):
        materialize_private_suite(
            manifest=failed.manifest,
            authorization=failed.authorization,
            baseline_matrix=failed.matrix,
            ledger=failed.custody,
            store=failed.store,
            decryptor=BadDecryptor(),
            evaluator_root=failed.root,
            opened_at=BASE + timedelta(days=5),
        )
    state = failed.custody.state(failed.manifest)
    assert state.materialization_failed is True
    assert state.suite_retired is True
    assert state.access_closed is True


def test_development_leak_retires_suite_before_access(tmp_path):
    case = _build_case(tmp_path)
    case.custody.register_suite(case.manifest)
    report = PrivateContaminationReport(
        report_id="development-leak-001",
        private_suite_manifest_sha256=case.manifest.manifest_sha256,
        evaluation_task_manifest_sha256=case.task.manifest_sha256,
        source=ContaminationSource.DEVELOPMENT_DISCLOSURE,
        severity=ContaminationSeverity.CRITICAL,
        evidence_sha256="1" * 64,
        detail_sha256="2" * 64,
        reporter_principal_sha256=EVALUATOR_PRINCIPAL,
        detected_at=BASE + timedelta(days=3, hours=1),
    )
    case.custody.report_contamination(case.manifest, report)
    assert case.custody.state(case.manifest).suite_retired is True
    with pytest.raises(PrivateSuitePolicyError, match="retired"):
        case.custody.authorize_access(case.manifest, case.authorization)


def test_two_person_retirement_blocks_unlock_before_access(tmp_path):
    case = _build_case(tmp_path)
    case.custody.register_suite(case.manifest)
    retirement = PrivateRetirementRecord(
        retirement_id="operator-withdrawal-001",
        private_suite_manifest_sha256=case.manifest.manifest_sha256,
        scope="suite",
        reason="operator_withdrawal",
        evidence_sha256="1" * 64,
        custody_approver_principal_sha256=CUSTODY_OWNER,
        independent_approver_principal_sha256=AUDITOR,
        custody_approval_evidence_sha256="3" * 64,
        independent_approval_evidence_sha256="4" * 64,
        retired_at=BASE + timedelta(days=3, hours=1),
    )
    wrong = retirement.model_copy(update={"independent_approver_principal_sha256": "9" * 64})
    with pytest.raises(PrivateSuitePolicyError, match="two-person approval"):
        case.custody.retire(case.manifest, wrong)
    case.custody.retire(case.manifest, retirement)
    assert case.custody.state(case.manifest).suite_retired is True
    with pytest.raises(PrivateSuitePolicyError, match="retired"):
        case.custody.authorize_access(case.manifest, case.authorization)


def test_private_task_cannot_run_without_active_custody_guard(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)
    runner = _runner(case, guard=False)
    direct_plan = case.plans[0].run_plan
    with pytest.raises(EvaluationRunnerError, match="require.*custody guard"):
        runner.run(suite=case.suite, plan=direct_plan, task=case.task, repeat_index=0)
    assert runner.ledger.attempt_states() == ()


def test_active_guard_allows_only_authorized_plan_and_runner_scores_normally(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)
    runner = _runner(case)
    direct_plan = case.plans[0].run_plan
    outcome = runner.run(
        suite=case.suite,
        plan=direct_plan,
        task=case.task,
        repeat_index=0,
    )
    assert outcome.attempt.status is AttemptStatus.COMPLETED
    assert outcome.scorer_receipt is not None

    unauthorized = direct_plan.model_copy(update={"plan_id": "unauthorized-private-plan"})
    with pytest.raises(EvaluationAccessRevokedError, match="outside private-test authorization"):
        runner.run(
            suite=case.suite,
            plan=unauthorized,
            task=case.task,
            repeat_index=1,
        )


def test_declared_contamination_skips_hidden_scorer_retires_suite_and_blocks_next_attempt(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)

    def contaminated_submission(context):
        write_submission(context)
        path = context.submission_inbox / "submission.json"
        submission = EvaluationSubmission.model_validate_json(path.read_bytes())
        contaminated = submission.model_copy(
            update={"declared_contamination": ("possible private-task training overlap",)}
        )
        path.write_text(contaminated.model_dump_json(), encoding="utf-8")

    scorer = ExactAnswerScorer(fail=AssertionError("hidden scorer must not run"))
    runner = _runner(case, action=contaminated_submission, scorer=scorer)
    direct_plan = case.plans[0].run_plan
    outcome = runner.run(
        suite=case.suite,
        plan=direct_plan,
        task=case.task,
        repeat_index=0,
    )
    assert outcome.attempt.status is AttemptStatus.INVALID
    assert outcome.scorer_receipt is not None
    assert outcome.scorer_receipt.receipt.score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    state = case.custody.state(case.manifest)
    assert state.suite_retired is True
    assert state.access_closed is True
    assert state.cleanup_receipt_sha256 is not None
    assert len(state.contamination_report_ids) == 1
    assert not (case.root / "hidden_assets/private/private-pilot-v1/task-001.json").exists()

    with pytest.raises(EvaluationAccessRevokedError, match="not open, materialized, and active"):
        runner.run(
            suite=case.suite,
            plan=direct_plan,
            task=case.task,
            repeat_index=1,
        )
    created = [
        item for item in runner.ledger.attempt_states() if item.status is AttemptStatus.CREATED
    ]
    assert len(created) == 1


def test_scorer_canary_contamination_is_recorded_and_plaintext_is_disposed(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)

    class CanaryScorer:
        scorer_sha256 = SCORER_HASH

        def score(self, **_kwargs):
            return EvaluationScore(invalid_reasons=(InvalidReason.CONTAMINATION,))

    runner = _runner(case, scorer=CanaryScorer())
    outcome = runner.run(
        suite=case.suite,
        plan=case.plans[0].run_plan,
        task=case.task,
        repeat_index=0,
    )
    assert outcome.attempt.status is AttemptStatus.INVALID
    state = case.custody.state(case.manifest)
    assert state.contamination_report_ids
    assert state.access_closed is True
    assert not (case.root / "hidden_assets/private/private-pilot-v1/task-001.json").exists()


def test_concurrent_external_retirement_between_submission_and_scoring_fails_closed(tmp_path):
    case = _build_case(tmp_path)
    _materialize(case)

    def submit_then_retire(context):
        write_submission(context)
        report = PrivateContaminationReport(
            report_id="concurrent-external-report-001",
            private_suite_manifest_sha256=case.manifest.manifest_sha256,
            evaluation_task_manifest_sha256=case.task.manifest_sha256,
            source=ContaminationSource.OPERATOR_REPORT,
            severity=ContaminationSeverity.CRITICAL,
            evidence_sha256="1" * 64,
            detail_sha256="2" * 64,
            reporter_principal_sha256=EVALUATOR_PRINCIPAL,
            detected_at=BASE + timedelta(days=5),
        )
        case.custody.report_contamination(case.manifest, report)
        close_private_suite_access(
            manifest=case.manifest,
            ledger=case.custody,
            access_id=case.authorization.authorization_id,
            evaluator_root=case.root,
            closed_at=report.detected_at,
        )

    scorer = ExactAnswerScorer(fail=AssertionError("retired hidden scorer must not run"))
    runner = _runner(case, action=submit_then_retire, scorer=scorer)
    outcome = runner.run(
        suite=case.suite,
        plan=case.plans[0].run_plan,
        task=case.task,
        repeat_index=0,
    )
    assert outcome.attempt.status is AttemptStatus.INVALID
    assert outcome.scorer_receipt is not None
    assert outcome.scorer_receipt.receipt.score.invalid_reasons == (InvalidReason.CONTAMINATION,)
    assert case.custody.state(case.manifest).contamination_report_ids == (
        "concurrent-external-report-001",
    )


def test_custody_ledger_tampering_fails_closed(tmp_path):
    case = _build_case(tmp_path)
    case.custody.register_suite(case.manifest)
    raw = case.custody.path.read_text(encoding="utf-8")
    case.custody.path.chmod(0o600)
    case.custody.path.write_text(
        raw.replace("private-pilot-v1", "private-pilot-v2"), encoding="utf-8"
    )
    with pytest.raises(PrivateSuitePolicyError, match="invalid custody ledger"):
        case.custody.events()


def test_private_suite_cli_registers_materializes_reports_status_and_closes(tmp_path):
    case = _build_case(tmp_path / "case")
    manifest_path = tmp_path / "manifest.json"
    authorization_path = tmp_path / "authorization.json"
    matrix_path = tmp_path / "matrix.json"
    config_path = tmp_path / "operator-config.json"
    plugin_path = tmp_path / "private_operator.py"
    receipt_path = tmp_path / "materialization-receipt.json"
    recovered_receipt_path = tmp_path / "recovered-materialization-receipt.json"
    ledger_path = case.custody.path
    manifest_path.write_text(case.manifest.model_dump_json(), encoding="utf-8")
    now = datetime.now(timezone.utc)
    authorization = case.authorization.model_copy(
        update={
            "authorized_at": now - timedelta(minutes=1),
            "expires_at": now + timedelta(days=1),
        }
    )
    authorization_path.write_text(authorization.model_dump_json(), encoding="utf-8")
    matrix_path.write_text(case.matrix.model_dump_json(), encoding="utf-8")

    ciphertext_paths = {}
    for index, (storage_ref, payload) in enumerate(case.store.values.items()):
        path = tmp_path / "ciphertext" / f"asset-{index:02d}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        ciphertext_paths[storage_ref] = str(path)
    config_path.write_text(json.dumps({"ciphertext_paths": ciphertext_paths}), encoding="utf-8")
    plugin_path.write_text(
        """from pathlib import Path

class Store:
    def __init__(self, paths):
        self.paths = paths
    def read_ciphertext(self, storage_ref):
        return Path(self.paths[storage_ref]).read_bytes()

class Decryptor:
    def decrypt(self, envelope, ciphertext):
        prefix = b\"sealed-v1\\0\"
        if not ciphertext.startswith(prefix):
            raise ValueError(\"bad envelope\")
        return ciphertext[len(prefix):][::-1]

def build(*, config, **_kwargs):
    return {\"store\": Store(config[\"ciphertext_paths\"]), \"decryptor\": Decryptor()}
""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(tmp_path), os.getcwd()))

    def run_cli(*arguments):
        result = subprocess.run(
            [sys.executable, "scripts/manage_private_suite.py", *arguments],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result

    validation = run_cli("validate", "--manifest", str(manifest_path))
    assert json.loads(validation.stdout)["encrypted_asset_count"] == 4
    run_cli(
        "register",
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
    )
    run_cli(
        "authorize",
        "--manifest",
        str(manifest_path),
        "--authorization",
        str(authorization_path),
        "--ledger",
        str(ledger_path),
    )
    run_cli(
        "materialize",
        "--manifest",
        str(manifest_path),
        "--authorization",
        str(authorization_path),
        "--matrix",
        str(matrix_path),
        "--ledger",
        str(ledger_path),
        "--operator-factory",
        "private_operator:build",
        "--operator-config",
        str(config_path),
        "--evaluator-root",
        str(case.root),
        "--output",
        str(receipt_path),
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))["receipt"]
    assert receipt["access_id"] == authorization.authorization_id
    run_cli(
        "recover-materialized",
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
        "--access-id",
        authorization.authorization_id,
        "--evaluator-root",
        str(case.root),
        "--output",
        str(recovered_receipt_path),
    )
    recovered = json.loads(recovered_receipt_path.read_text(encoding="utf-8"))["receipt"]
    assert recovered == receipt
    status = run_cli(
        "status",
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
    )
    assert json.loads(status.stdout)["state"]["access_closed"] is False

    closed = run_cli(
        "close",
        "--manifest",
        str(manifest_path),
        "--ledger",
        str(ledger_path),
        "--access-id",
        authorization.authorization_id,
        "--evaluator-root",
        str(case.root),
    )
    closed_payload = json.loads(closed.stdout)
    assert closed_payload["status"]["state"]["access_closed"] is True
    assert closed_payload["cleanup_receipt"]["removed_file_count"] == 3
