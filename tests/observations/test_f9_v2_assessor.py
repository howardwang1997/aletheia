from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from aletheia.observations import f9_v2_assessor as assessor_module
from aletheia.observations.f9_v2_assessor import (
    ExactContentF9V2ObservationAssessor,
    FrozenF9V2ExactContentAssessmentCatalog,
    FrozenF9V2ExactContentAssessmentTemplate,
)
from aletheia.observations.f9_v2_validation import build_f9_v2_validation_request
from aletheia.observations.scientific_bridge import BridgeValidationDisposition

_TESTS = Path(__file__).resolve().parents[1]
for _fixture_dir in (
    _TESTS / "research_controller",
    _TESTS / "execution",
    _TESTS / "protocols",
):
    sys.path.insert(0, str(_fixture_dir))

from test_f9_v2_validation import _f9_case  # noqa: E402
from test_scientific_bridge import _digest, _raw_run  # noqa: E402


def _catalog(raw_run, *, disposition=BridgeValidationDisposition.VALIDATED_CONFIRMATION):
    source_sha256 = hashlib.sha256(Path(assessor_module.__file__).read_bytes()).hexdigest()
    template = FrozenF9V2ExactContentAssessmentTemplate.from_raw_run(
        raw_run=raw_run,
        disposition=disposition,
        outcome_bin_id="outcome.negative",
        blocker_codes=(
            ()
            if disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
            else ("f9-v2:outside-preregistered-analysis",)
        ),
    )
    return FrozenF9V2ExactContentAssessmentCatalog(
        catalog_id="catalog:f9-v2:test",
        assessor_implementation_sha256=source_sha256,
        templates=(template,),
    )


def _assessor(catalog):
    source_sha256 = hashlib.sha256(Path(assessor_module.__file__).read_bytes()).hexdigest()
    return ExactContentF9V2ObservationAssessor(
        catalog=catalog,
        implementation_sha256=source_sha256,
    )


def test_exact_content_assessor_maps_only_full_frozen_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    requested_at = raw_run.assembled_at + timedelta(minutes=1)
    request = build_f9_v2_validation_request(raw_run=raw_run, requested_at=requested_at)
    assessed_at = requested_at + timedelta(seconds=1)
    assessor = _assessor(_catalog(raw_run))

    first = assessor.assess_observation(
        request=request,
        raw_run=raw_run,
        assessed_at=assessed_at,
    )
    retry = assessor.assess_observation(
        request=request,
        raw_run=raw_run,
        assessed_at=assessed_at,
    )

    assert first == retry
    assert first.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
    assert first.outcome_bin_id == "outcome.negative"
    assert first.validation_batch_sha256 is not None
    assert first.blocker_codes == ()


def test_exact_content_assessor_blocks_unknown_fresh_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    original = _raw_run(case)
    changed = _raw_run(
        case,
        artifact_entry_updates={"content_sha256": _digest("unknown-f9-v2-observation")},
    )
    requested_at = changed.assembled_at + timedelta(minutes=1)
    request = build_f9_v2_validation_request(raw_run=changed, requested_at=requested_at)

    assessment = _assessor(_catalog(original)).assess_observation(
        request=request,
        raw_run=changed,
        assessed_at=requested_at + timedelta(seconds=1),
    )

    assert assessment.disposition is BridgeValidationDisposition.BLOCKED_EXECUTION
    assert assessment.outcome_bin_id is None
    assert assessment.validation_batch_sha256 is None
    assert assessment.blocker_codes == ("f9-v2:unrecognized-exact-content",)


def test_exact_content_assessor_preserves_scientific_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    requested_at = raw_run.assembled_at + timedelta(minutes=1)
    request = build_f9_v2_validation_request(raw_run=raw_run, requested_at=requested_at)

    assessment = _assessor(
        _catalog(raw_run, disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC)
    ).assess_observation(
        request=request,
        raw_run=raw_run,
        assessed_at=requested_at + timedelta(seconds=1),
    )

    assert assessment.disposition is BridgeValidationDisposition.REJECTED_SCIENTIFIC
    assert assessment.outcome_bin_id == "outcome.negative"
    assert assessment.validation_batch_sha256 is not None
    assert assessment.blocker_codes == ("f9-v2:outside-preregistered-analysis",)


def test_exact_content_catalog_rejects_duplicate_scope_and_source_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _f9_case(monkeypatch)
    raw_run = _raw_run(case)
    catalog = _catalog(raw_run)

    with pytest.raises(ValueError, match="sorted and uniquely scoped"):
        FrozenF9V2ExactContentAssessmentCatalog(
            catalog_id="catalog:f9-v2:duplicate",
            assessor_implementation_sha256=catalog.assessor_implementation_sha256,
            templates=(catalog.templates[0], catalog.templates[0]),
        )
    with pytest.raises(ValueError, match="implementation differs"):
        ExactContentF9V2ObservationAssessor(
            catalog=catalog,
            implementation_sha256=_digest("another-assessor-source"),
        )
