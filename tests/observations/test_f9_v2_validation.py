from __future__ import annotations

import ast
import os
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aletheia.observations.f9_v2_validation import (
    F9V2BridgeVerificationContext,
    F9V2IndependentValidationAssessment,
    F9V2IndependentValidationService,
    F9V2ValidationError,
    WriteOnceF9V2ValidationCampaignArchive,
    build_f9_v2_validation_request,
    issue_f9_v2_validation_campaign,
    verify_f9_v2_validation_campaign,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    ScientificObservationOutcome,
    issue_validation_issuance_challenge,
)

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "research_controller",
    _TESTS / "execution",
    _TESTS / "protocols",
):
    sys.path.insert(0, str(_fixture_dir))

from test_vertical_cut import (  # noqa: E402
    _f9_enriched_grouped_fixture,
    runtime_fixture_support,
)
from test_scientific_bridge import (  # noqa: E402
    DATABASE_PRIVATE_KEY,
    VALIDATOR_PRIVATE_KEY,
    BridgeCase,
    _bridge_case,
    _digest,
    _raw_run,
)


@dataclass
class _Assessor:
    disposition: BridgeValidationDisposition = BridgeValidationDisposition.VALIDATED_CONFIRMATION
    outcome_bin_id: str | None = "outcome.negative"
    blocker_codes: tuple[str, ...] = ()
    calls: list[str] = field(default_factory=list)

    def assess_observation(self, *, request, raw_run, assessed_at):
        self.calls.append(raw_run.raw_run_sha256)
        batch = (
            _digest(f"f9-v2-validation-batch:{raw_run.raw_run_sha256}")
            if self.disposition is not BridgeValidationDisposition.BLOCKED_EXECUTION
            else None
        )
        return F9V2IndependentValidationAssessment(
            validation_request_sha256=request.request_sha256,
            raw_observation_content_sha256=request.raw_observation_content_sha256,
            disposition=self.disposition,
            outcome_bin_id=self.outcome_bin_id,
            validation_batch_sha256=batch,
            blocker_codes=self.blocker_codes,
            assessed_at=assessed_at,
        )


@dataclass
class _Clock:
    values: list[datetime]

    def __call__(self) -> datetime:
        if not self.values:
            raise AssertionError("F9-v2 test clock was consumed unexpectedly")
        return self.values.pop(0)


@dataclass
class _BackdatingAssessor(_Assessor):
    def assess_observation(self, *, request, raw_run, assessed_at):
        return super().assess_observation(
            request=request,
            raw_run=raw_run,
            assessed_at=assessed_at - timedelta(seconds=1),
        )


@dataclass
class _BarrierAssessor(_Assessor):
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2))

    def assess_observation(self, *, request, raw_run, assessed_at):
        result = super().assess_observation(
            request=request,
            raw_run=raw_run,
            assessed_at=assessed_at,
        )
        self.barrier.wait(timeout=10)
        return result


def _f9_case(monkeypatch: pytest.MonkeyPatch) -> BridgeCase:
    fixture = _f9_enriched_grouped_fixture()
    original = runtime_fixture_support.fixture_by_name

    def fixture_by_name(name: str):
        return fixture if name == "grouped_regression" else original(name)

    monkeypatch.setattr(runtime_fixture_support, "fixture_by_name", fixture_by_name)
    return _bridge_case()


def _context(case: BridgeCase) -> F9V2BridgeVerificationContext:
    return F9V2BridgeVerificationContext(
        qualification_authority=case.qualification_authority,
        action_authority=case.action_authority,
        qualification_custody=case.qualification_custody,
        raw_run_custody=case.raw_run_custody,
        execution_authority_pin=case.execution_pin,
        validator_authority_pin=case.validator_pin,
        admission_authority_pin=case.admission_pin,
        database_authority_pin=case.database_pin,
    )


def _archive(tmp_path: Path, case: BridgeCase) -> WriteOnceF9V2ValidationCampaignArchive:
    return WriteOnceF9V2ValidationCampaignArchive(
        tmp_path / "f9-v2-campaigns",
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
    )


def _service(
    *,
    archive: WriteOnceF9V2ValidationCampaignArchive,
    assessor: _Assessor,
    case: BridgeCase,
    clock: _Clock,
) -> F9V2IndependentValidationService:
    return F9V2IndependentValidationService(
        archive=archive,
        assessor=assessor,
        verification=_context(case),
        validator_private_key=VALIDATOR_PRIVATE_KEY,
        clock=clock,
    )


def test_f9_v2_service_signs_archives_and_bridges_exact_negative_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    assessor = _Assessor()
    archive = _archive(tmp_path, case)
    service = _service(
        archive=archive,
        assessor=assessor,
        case=case,
        clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
    )

    campaign_sha256 = service.prepare_validation_campaign(raw_run=raw_run)

    assert campaign_sha256 is not None
    challenge_at = start + timedelta(seconds=3)
    challenge = issue_validation_issuance_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
        nonce_sha256=_digest("f9-v2-validation-challenge"),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=challenge_at,
        expires_at=challenge_at + timedelta(minutes=5),
    )
    receipt = service.issue_validation_receipt(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
        issuance_challenge=challenge,
    )

    projection = receipt.message.validation_campaign_projection
    world_model = case.binding.compilation_request.protocol.world_model
    assert world_model is not None
    assert receipt.message.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
    assert receipt.message.outcome is ScientificObservationOutcome.NEGATIVE
    assert projection is not None
    assert projection.campaign_sha256 == campaign_sha256
    assert projection.raw_run_sha256 == raw_run.raw_run_sha256
    assert projection.protocol_sha256 == case.binding.compilation_request.protocol.protocol_sha256
    assert projection.prediction_commitment_sha256 == (
        case.authorization.message.scientific_observation_artifact_binding.prediction_commitment_sha256
    )
    committed = archive.load_committed_campaign(raw_run=raw_run, observed_at=challenge_at)
    assert committed is not None
    assert committed.campaign.message.request.world_model_snapshot_sha256 == (
        world_model.world_model_sha256
    )
    assert committed.campaign.message.request.prediction_sha256s
    assert assessor.calls == [raw_run.raw_run_sha256]

    for path in archive.root.rglob("*"):
        if path.is_dir():
            path.chmod(0o500)
    archive.root.chmod(0o500)
    reader = WriteOnceF9V2ValidationCampaignArchive(
        archive.root,
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
        read_only=True,
    )
    assert reader.load_committed_campaign(raw_run=raw_run, observed_at=challenge_at) == committed
    with pytest.raises(F9V2ValidationError, match="read-only"):
        reader.publish_campaign(
            campaign=committed.campaign,
            raw_run=raw_run,
            committed_at=challenge_at,
        )


def test_shared_f9_archive_seals_group_read_campaign_before_atomic_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    root = tmp_path / "shared-f9-v2-campaigns"
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    archive = WriteOnceF9V2ValidationCampaignArchive(
        root,
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
    )

    campaign_sha256 = _service(
        archive=archive,
        assessor=_Assessor(),
        case=case,
        clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
    ).prepare_validation_campaign(raw_run=raw_run)
    assert campaign_sha256 is not None
    target = root / "raw-runs" / raw_run.raw_run_sha256[:2] / f"{raw_run.raw_run_sha256}.json"
    assert stat.S_IMODE((root / "raw-runs").stat().st_mode) == 0o750
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(target.stat().st_mode) == 0o440
    assert target.stat().st_nlink == 1

    with pytest.raises(F9V2ValidationError, match="writable or inaccessible"):
        WriteOnceF9V2ValidationCampaignArchive(
            root,
            validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
            validator_authority_pin=case.validator_pin,
            read_only=True,
        )
    monkeypatch.setattr(os, "geteuid", lambda: target.stat().st_uid + 10_000)
    reader = WriteOnceF9V2ValidationCampaignArchive(
        root,
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
        read_only=True,
    )
    assert (
        reader.load_committed_campaign(
            raw_run=raw_run,
            observed_at=start + timedelta(seconds=3),
        ).campaign_sha256
        == campaign_sha256
    )
    target.parent.chmod(0o770)
    with pytest.raises(F9V2ValidationError, match="parent chain became unsafe"):
        reader.load_committed_campaign(
            raw_run=raw_run,
            observed_at=start + timedelta(seconds=3),
        )


def test_f9_v2_campaign_retry_returns_first_write_once_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    archive = _archive(tmp_path, case)
    first_assessor = _Assessor()
    first = _service(
        archive=archive,
        assessor=first_assessor,
        case=case,
        clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
    ).prepare_validation_campaign(raw_run=raw_run)
    retry_assessor = _Assessor(
        disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
        blocker_codes=("f9-v2:scientific-rejection",),
    )
    retry = _service(
        archive=archive,
        assessor=retry_assessor,
        case=case,
        clock=_Clock([start + timedelta(seconds=3)]),
    ).prepare_validation_campaign(raw_run=raw_run)

    assert retry == first
    assert first_assessor.calls == [raw_run.raw_run_sha256]
    assert retry_assessor.calls == []


def test_f9_v2_service_verifies_full_custody_before_assessment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    case.raw_run_custody.fail = True
    assessor = _Assessor()
    service = _service(
        archive=_archive(tmp_path, case),
        assessor=assessor,
        case=case,
        clock=_Clock([raw_run.assembled_at + timedelta(minutes=1)]),
    )

    with pytest.raises(F9V2ValidationError):
        service.prepare_validation_campaign(raw_run=raw_run)
    assert assessor.calls == []


def test_f9_v2_service_owns_the_assessment_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    service = _service(
        archive=_archive(tmp_path, case),
        assessor=_BackdatingAssessor(),
        case=case,
        clock=_Clock([start, start + timedelta(seconds=2)]),
    )

    with pytest.raises(F9V2ValidationError, match="service-owned assessment time"):
        service.prepare_validation_campaign(raw_run=raw_run)


def test_f9_v2_concurrent_campaigns_converge_on_one_raw_run_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    archive = _archive(tmp_path, case)
    barrier = threading.Barrier(2)
    services = tuple(
        _service(
            archive=archive,
            assessor=_BarrierAssessor(
                outcome_bin_id=outcome,
                barrier=barrier,
            ),
            case=case,
            clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
        )
        for outcome in ("outcome.negative", "outcome.positive")
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda service: service.prepare_validation_campaign(raw_run=raw_run),
                services,
            )
        )

    assert results[0] == results[1]
    committed = archive.load_committed_campaign(
        raw_run=raw_run,
        observed_at=start + timedelta(seconds=3),
    )
    assert committed is not None
    assert committed.campaign_sha256 == results[0]
    assert committed.campaign.message.assessment.outcome_bin_id in {
        "outcome.negative",
        "outcome.positive",
    }


def test_f9_v2_absent_lookup_does_not_create_archive_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    archive = _archive(tmp_path, case)

    assert (
        archive.load_committed_campaign(
            raw_run=raw_run,
            observed_at=raw_run.assembled_at + timedelta(minutes=1),
        )
        is None
    )
    assert not (archive.root / "raw-runs").exists()


def test_read_only_f9_v2_archive_requires_preexisting_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)

    with pytest.raises(F9V2ValidationError, match="already exist"):
        WriteOnceF9V2ValidationCampaignArchive(
            tmp_path / "missing-read-only-archive",
            validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
            validator_authority_pin=case.validator_pin,
            read_only=True,
        )


def test_read_only_f9_v2_archive_never_invokes_directory_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    root = tmp_path / "existing-read-only-archive"
    root.mkdir(mode=0o500)
    root.chmod(0o500)

    def reject_mkdir(*_args, **_kwargs):
        raise AssertionError("read-only archive attempted a filesystem mutation")

    monkeypatch.setattr(Path, "mkdir", reject_mkdir)
    archive = WriteOnceF9V2ValidationCampaignArchive(
        root,
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
        read_only=True,
    )

    assert archive.root == root


def test_f9_v2_archive_fresh_rehash_rejects_tampered_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    archive = _archive(tmp_path, case)
    campaign_sha256 = _service(
        archive=archive,
        assessor=_Assessor(),
        case=case,
        clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
    ).prepare_validation_campaign(raw_run=raw_run)
    assert campaign_sha256 is not None
    target = (
        archive.root / "raw-runs" / raw_run.raw_run_sha256[:2] / f"{raw_run.raw_run_sha256}.json"
    )
    os.chmod(target, 0o600)
    target.write_bytes(b"{}")
    os.chmod(target, 0o400)

    with pytest.raises(F9V2ValidationError):
        archive.verify_observation_validation_campaign(
            campaign_sha256=campaign_sha256,
            raw_run=raw_run,
            expected_validator_manifest_sha256=(
                case.authorization.message.validator_manifest_sha256
            ),
            expected_observation_validation_policy_sha256=(
                case.authorization.message.observation_validation_policy_sha256
            ),
            observed_at=start + timedelta(seconds=3),
        )


def test_f9_v2_signature_and_raw_run_rebinding_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    requested_at = raw_run.assembled_at + timedelta(minutes=1)
    request = build_f9_v2_validation_request(
        raw_run=raw_run,
        requested_at=requested_at,
    )
    assessment = _Assessor().assess_observation(
        request=request,
        raw_run=raw_run,
        assessed_at=requested_at + timedelta(seconds=1),
    )
    campaign = issue_f9_v2_validation_campaign(
        request=request,
        assessment=assessment,
        validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
        validator_authority_pin=case.validator_pin,
        private_key=VALIDATOR_PRIVATE_KEY,
    )

    with pytest.raises(F9V2ValidationError):
        verify_f9_v2_validation_campaign(
            campaign=campaign.model_copy(update={"signature_ed25519_hex": "0" * 128}),
            raw_run=raw_run,
            validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
            validator_authority_pin=case.validator_pin,
            observed_at=requested_at + timedelta(seconds=2),
        )
    rebound = _raw_run(case, artifact_entry_updates={"bytes": 2_048})
    with pytest.raises(F9V2ValidationError):
        verify_f9_v2_validation_campaign(
            campaign=campaign,
            raw_run=rebound,
            validator_manifest_sha256=case.authorization.message.validator_manifest_sha256,
            validator_authority_pin=case.validator_pin,
            observed_at=requested_at + timedelta(seconds=2),
        )


def test_f9_v2_preserves_scientific_rejection_without_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    start = raw_run.assembled_at + timedelta(minutes=1)
    archive = _archive(tmp_path, case)
    service = _service(
        archive=archive,
        assessor=_Assessor(
            disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
            blocker_codes=("f9-v2:outside-preregistered-analysis",),
        ),
        case=case,
        clock=_Clock([start, start + timedelta(seconds=1), start + timedelta(seconds=2)]),
    )
    campaign_sha256 = service.prepare_validation_campaign(raw_run=raw_run)
    assert campaign_sha256 is not None
    challenge_at = start + timedelta(seconds=3)
    challenge = issue_validation_issuance_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
        nonce_sha256=_digest("f9-v2-scientific-rejection-challenge"),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=challenge_at,
        expires_at=challenge_at + timedelta(minutes=5),
    )

    receipt = service.issue_validation_receipt(
        raw_run=raw_run,
        validation_campaign_sha256=campaign_sha256,
        issuance_challenge=challenge,
    )

    assert receipt.message.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC
    assert receipt.message.outcome is None
    assert receipt.message.scientific_observation_sha256 is None
    assert receipt.message.blocker_codes == ("f9-v2:outside-preregistered-analysis",)


def test_f9_v2_engineering_failure_never_creates_campaign_or_scientific_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case, "process_failed")
    validation_at = raw_run.assembled_at + timedelta(minutes=1)
    service = _service(
        archive=_archive(tmp_path, case),
        assessor=_Assessor(),
        case=case,
        clock=_Clock([validation_at]),
    )
    assert service.prepare_validation_campaign(raw_run=raw_run) is None
    challenge = issue_validation_issuance_challenge(
        raw_run=raw_run,
        validation_campaign_sha256=None,
        nonce_sha256=_digest("f9-v2-engineering-failure-challenge"),
        database_authority_pin=case.database_pin,
        private_key=DATABASE_PRIVATE_KEY,
        issued_at=validation_at,
        expires_at=validation_at + timedelta(minutes=5),
    )

    receipt = service.issue_validation_receipt(
        raw_run=raw_run,
        validation_campaign_sha256=None,
        issuance_challenge=challenge,
    )

    assert receipt.message.disposition is BridgeValidationDisposition.BLOCKED_EXECUTION
    assert receipt.message.outcome is None
    assert receipt.message.validation_campaign_projection is None


def test_f9_v2_request_requires_graph_world_model_and_live_utc_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)

    with pytest.raises(F9V2ValidationError):
        build_f9_v2_validation_request(
            raw_run=raw_run,
            requested_at=raw_run.assembled_at.replace(tzinfo=None),
        )
    with pytest.raises(F9V2ValidationError):
        build_f9_v2_validation_request(
            raw_run=raw_run,
            requested_at=case.authorization.message.observation_admission_deadline,
        )


def test_f9_v2_validation_module_has_no_legacy_control_plane_import() -> None:
    module_path = Path(__file__).resolve().parents[2] / "aletheia/observations/f9_v2_validation.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = tuple(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ) + tuple(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        name.startswith(
            (
                "aletheia.epistemics",
                "aletheia.migration.f9_v1_observation_compatibility",
                "aletheia.scheduler.driver",
            )
        )
        for name in imports
    )
